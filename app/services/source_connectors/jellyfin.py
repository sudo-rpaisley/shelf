"""Read-only Jellyfin catalogue adapter, without Shelf persistence.

Supported Jellyfin types are exactly Movie -> digital_video, Series -> tv_series,
MusicAlbum -> digital_music, Book -> ebook, and AudioBook -> audiobook. Episodes,
seasons, audio tracks (including audiobook files reported as Audio), playlists,
collections and other types are not imported. Albums and series stay whole;
there is no playback, transcoding, image download, progress sync or remote write.

``credentials.token`` is required. ``options.user_id`` selects the Jellyfin user;
otherwise Users/Me must resolve a user-owned token. Server API keys need an
explicit user_id. ``options.library_ids`` optionally selects visible library/view
IDs; absent or empty means all user-visible views. Options can also be read from
options_json. ``base_url`` and optional ``browser_url`` are credential-free HTTP(S)
server roots, including any reverse-proxy base path. LAN roots are intentional.

Requests use X-Emby-Token only, never follow redirects, and enumerate user views
before querying Items with both UserId and ParentId. ProductionYear, RunTimeTicks,
Container and album artists are intrinsic DTO fields, not ItemFields enum values.
Metadata ``year`` is a production year; ``duration`` is whole seconds. Exceptions
stringify to shared safe codes; their adapter-authored ``hint`` explains recovery.
Warnings use shared safe codes too, so the service can retain their meaning.
Limits apply across the entire fetch: 100 views, 250 item pages, 5,000 entries,
8 MiB per identity-encoded response, 64 MiB total, 20 seconds per request and
120 seconds overall. Partial results are explicitly incomplete; callers must not
mark unseen records unavailable on that basis. Changing catalogues are not atomic
snapshots, and detected count changes or overlapping pages also mean incomplete.
Requests require identity encoding; compressed responses are refused before
reading their bodies so an automatic decoder cannot allocate an unbounded chunk.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import unicodedata
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlencode, urlsplit

import httpx

from app.services.authors import join_names

from .base import Catalogue, ConnectorError, SourceRecord

PAGE_SIZE = 200
MAX_PAGES = 250
MAX_ENTRIES = 5000
MAX_LIBRARIES = 100
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
REQUEST_TIMEOUT = 20.0
CATALOGUE_TIMEOUT = 120.0

TYPE_MAP = {
    "Movie": "digital_video",
    "Series": "tv_series",
    "MusicAlbum": "digital_music",
    "Book": "ebook",
    "AudioBook": "audiobook",
}
_FIELDS = "ProviderIds,Overview,People,Genres,Studios,ParentId"
_AUTH_QUERY_KEYS = {
    "api_key", "api-key", "apikey", "token", "access_token", "auth_token",
    "x-emby-token", "x-plex-token", "authorization", "password", "secret", "key",
}
_PROVIDER_KEYS = {
    "imdb": "Imdb", "tmdb": "Tmdb", "tvdb": "Tvdb",
    "musicbrainzalbum": "MusicBrainzAlbum",
    "musicbrainzreleasegroup": "MusicBrainzReleaseGroup",
    "isbn": "Isbn", "isbn13": "Isbn13", "isbn10": "Isbn10",
}


def _error(code: str, message: str) -> ConnectorError:
    code = {
        "auth": "auth_failed", "redirect": "unsafe_redirect",
        "user_required": "invalid_options", "http": "http_error",
        "limit": "limit_reached", "invalid_response": "malformed_response",
        "network": "unreachable", "pagination": "incomplete",
        "library_unavailable": "not_found", "credentials": "invalid_credentials",
    }.get(code, code)
    error = ConnectorError(code)
    # The shared exception deliberately stringifies to a stable code only.
    # This optional hint is adapter-authored, never remote content or a URL.
    error.hint = message
    return error


def _contains_token(value: str, token: str) -> bool:
    # Check encoded echoes too, before ever putting an opaque ID in a URL/log.
    for _ in range(4):
        if token in value:
            return True
        value = html.unescape(unquote(value))
    return False


def _root(value: object, token: str) -> str:
    message = "Jellyfin roots must be HTTP(S) URLs with a hostname, without credentials, query or fragment."
    if not isinstance(value, str) or not value.strip():
        raise _error("invalid_url", message)
    value = value.strip()
    if len(value) > 2048 or any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        raise _error("invalid_url", message)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.port == 0
            or "%" in parsed.netloc or any(c.isspace() for c in parsed.netloc)
            or _contains_token(value, token)
        ):
            raise ValueError
        url = httpx.URL(value)
        if not url.host:
            raise ValueError
    except (ValueError, httpx.InvalidURL):
        raise _error("invalid_url", message) from None
    return str(url).rstrip("/")


def _identity(value: object, token: str) -> str | None:
    if (
        not isinstance(value, str) or not value or len(value) > 512
        or value.strip() != value or value in {".", ".."}
        or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)
        or _contains_token(value, token)
    ):
        return None
    return value


def _text(value: object, token: str, limit: int = 1024) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFC", html.unescape(value))
    # Remote overviews may contain HTML or credential-bearing hyperlinks. Keep
    # plain text, never copy hrefs or any authentication-bearing provider URLs.
    # Excluding another '<' keeps malformed runs of open tags linear-time.
    value = re.sub(r"<[^<>]*>", " ", value)

    def redact_url(match: re.Match) -> str:
        try:
            url = urlsplit(match.group())
            queries = [url.query, url.fragment.split("?", 1)[-1]]
            if url.username is not None or any(
                key.casefold() in _AUTH_QUERY_KEYS for query in queries
                for key, _ in parse_qsl(query)
            ) or "/devkey/" in unquote(url.path).casefold():
                return "[redacted link]"
        except ValueError:
            return "[redacted link]"
        return match.group()

    value = re.sub(r"https?://[^\s<>\"']+", redact_url, value, flags=re.I)
    for secret in {token, unicodedata.normalize("NFC", token), quote(token, safe=""), quote_plus(token)}:
        value = value.replace(secret, "[redacted]")
    if _contains_token(value, token):
        return "[redacted]"
    value = "".join(" " if unicodedata.category(c) in {"Cc", "Cs"} else c for c in value)
    return " ".join(value.split())[:limit]


def _options(source: dict, credentials: dict, token: str) -> tuple[str | None, list[str]]:
    options = source.get("options")
    if options is None:
        raw = source.get("options_json") or "{}"
        try:
            if not isinstance(raw, str) or len(raw) > 64 * 1024:
                raise ValueError
            options = json.loads(raw)
        except (ValueError, RecursionError):
            raise _error("invalid_options", "Jellyfin options must be a JSON object.") from None
    if not isinstance(options, dict):
        raise _error("invalid_options", "Jellyfin options must be a JSON object.")
    if set(options) - {"user_id", "library_ids"}:
        raise _error("invalid_options", "Jellyfin supports only the user_id and library_ids options.")
    user = options.get("user_id", credentials.get("user_id"))
    user_id = _identity(user, token) if user not in (None, "") else None
    if user not in (None, "") and user_id is None:
        raise _error("invalid_options", "Jellyfin options.user_id must be a nonempty user ID string.")
    libraries = options.get("library_ids", [])
    if not isinstance(libraries, list) or len(libraries) > MAX_LIBRARIES:
        raise _error("invalid_options", f"Jellyfin library_ids must be a list of at most {MAX_LIBRARIES} IDs.")
    result = []
    for library in libraries:
        library_id = _identity(library, token)
        if library_id is None:
            raise _error("invalid_options", "Jellyfin library_ids must contain nonempty ID strings.")
        if library_id not in result:
            result.append(library_id)
    return user_id, result


def _warn(catalogue: Catalogue, code: str) -> None:
    catalogue.complete = False
    if code not in catalogue.warnings:
        catalogue.warnings.append(code)


class _Reader:
    def __init__(self, client: httpx.AsyncClient, base: str, token: str):
        self.client = client
        self.base = base
        self.headers = {"X-Emby-Token": token, "Accept": "application/json", "Accept-Encoding": "identity"}
        self.bytes_read = 0
        self.pages = 0
        self.entries = 0

    async def get(self, path: str, params: dict | None = None, *, me: bool = False) -> object:
        # Only adapter-owned relative paths reach this boundary. Redirects,
        # response-supplied URLs, environment proxies and netrc auth are unused.
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self.client.stream(
                    "GET", f"{self.base}/{path}", params=params, headers=self.headers,
                ) as response:
                    status = response.status_code
                    if status in {401, 403}:
                        raise _error("auth", f"Jellyfin refused access (HTTP {status}); check the token and user permissions. API keys need options.user_id.")
                    if status == 429:
                        raise _error("rate_limited", "Jellyfin rate-limited this catalogue request; try again later.")
                    if 300 <= status < 400:
                        raise _error("redirect", "Jellyfin redirected the request; configure the final server root. Redirects are not followed.")
                    if me and status in {400, 404, 405, 501}:
                        raise _error("user_required", "Set Jellyfin options.user_id to the user whose visible catalogue should be imported; this token cannot resolve Users/Me.")
                    if status != 200:
                        raise _error("http", f"Jellyfin returned HTTP {status}; check the server root, user and selected libraries.")
                    if response.headers.get("content-encoding", "identity").strip().casefold() not in {"", "identity"}:
                        raise _error("unsupported_encoding", "Jellyfin must honor Accept-Encoding: identity; compressed catalogue responses are not supported.")
                    length = response.headers.get("content-length", "")
                    if length.isdecimal() and int(length) > MAX_RESPONSE_BYTES:
                        raise _error("limit", "Jellyfin response exceeded the catalogue response byte limit.")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        self.bytes_read += len(chunk)
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES or self.bytes_read > MAX_TOTAL_BYTES:
                            raise _error("limit", "Jellyfin response exceeded a catalogue byte limit; narrow the selected libraries.")
                        body.extend(chunk)
            try:
                return json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                raise _error("invalid_response", "Jellyfin returned malformed catalogue JSON.") from None
        except (httpx.TimeoutException, TimeoutError):
            raise _error("timeout", "Jellyfin catalogue request timed out; try again or narrow the selected libraries.") from None
        except (httpx.HTTPError, OSError, ValueError):
            # Never interpolate the original exception: it can contain headers,
            # credentials, a remote response body, or a redirect destination.
            raise _error("network", "Could not read the Jellyfin catalogue; check connectivity and the server root.") from None


def _page(data: object, offset: int = 0) -> tuple[list, int | None]:
    if not isinstance(data, dict) or not isinstance(data.get("Items"), list):
        raise _error("invalid_response", "Jellyfin returned an invalid catalogue page.")
    total = data.get("TotalRecordCount")
    start = data.get("StartIndex")
    if total is not None and (type(total) is not int or total < 0):
        raise _error("invalid_response", "Jellyfin returned an invalid catalogue count.")
    if start is not None and (type(start) is not int or start != offset):
        raise _error("pagination", "Jellyfin returned an unexpected page offset; the catalogue is incomplete.")
    return data["Items"], total


def _names(value: object, token: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [name for raw in value[:30] if (name := _text(raw, token, 512))]


def _creators(item: dict, token: str) -> str:
    kind = item["Type"]
    people = item.get("People")
    people = people[:100] if isinstance(people, list) else []

    def roles(*wanted: str) -> list[str]:
        return [
            person.get("Name") for person in people if isinstance(person, dict)
            and isinstance(person.get("Type"), str) and person["Type"].casefold() in wanted
        ]

    if kind == "MusicAlbum":
        names = _names(item.get("AlbumArtists"), token) or _names(item.get("AlbumArtist"), token)
        names = names or _names(item.get("Artists"), token) or _names(roles("albumartist", "artist", "composer"), token)
    elif kind in {"Book", "AudioBook"}:
        names = _names(roles("author", "writer"), token) or _names(item.get("Artists"), token)
    elif kind == "Series":
        names = _names(roles("creator"), token) or _names(roles("director"), token) or _names(roles("writer"), token)
    else:
        names = _names(roles("director"), token)
    return _text(join_names(names) or "", token, 2000)


def _isbn(value: object, token: str) -> str | None:
    if not isinstance(value, str) or len(value) > 64 or _contains_token(value, token):
        return None
    clean = re.sub(r"^ISBN(?:-1[03])?:?\s*", "", value.strip(), flags=re.I)
    clean = re.sub(r"[\s-]+", "", clean).upper()
    # Check digits and conversion to ISBN-13 belong to the persistence service.
    return clean if re.fullmatch(r"(?:[0-9]{9}[0-9X]|[0-9]{13})", clean) else None


def _record(item: dict, library_id: str, browser: str, token: str) -> SourceRecord | None:
    kind = item.get("Type")
    if not isinstance(kind, str) or kind not in TYPE_MAP:
        return None
    external_id = _identity(item.get("Id"), token)
    title = _text(item.get("Name"), token)
    if not external_id or not title:
        return None
    metadata = {"jellyfin_type": kind}
    year = item.get("ProductionYear")
    if type(year) is int and 1 <= year <= 9999:
        metadata["year"] = year
    ticks = item.get("RunTimeTicks")
    if type(ticks) is int and 0 <= ticks <= 2**63 - 1:
        metadata["duration"] = ticks // 10_000_000
    for field, key in (("Overview", "description"), ("SeriesName", "series_name")):
        if text := _text(item.get(field), token, 8000 if field == "Overview" else 1024):
            metadata[key] = text
    if kind == "Series":
        metadata.setdefault("series_name", title)
    if genres := _names(item.get("Genres"), token):
        metadata["genres"] = list(dict.fromkeys(genres))
    studios = item.get("Studios")
    if isinstance(studios, list):
        names = _names([studio.get("Name") for studio in studios[:30] if isinstance(studio, dict)], token)
        if names:
            metadata["studios"] = list(dict.fromkeys(names))
    provider_ids = {}
    raw_ids = item.get("ProviderIds")
    if isinstance(raw_ids, dict):
        for key, value in raw_ids.items():
            key = _PROVIDER_KEYS.get(key.casefold()) if isinstance(key, str) else None
            if key and isinstance(value, str) and not _contains_token(value, token):
                if key.startswith("Isbn"):
                    value = _isbn(value, token)
                elif not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                    value = None
                if value:
                    provider_ids[key] = value
    if provider_ids:
        metadata["provider_ids"] = provider_ids
    isbn = None
    if kind in {"Book", "AudioBook"}:
        isbn = next((provider_ids[key] for key in ("Isbn13", "Isbn", "Isbn10") if key in provider_ids), None)
    container = item.get("Container")
    if not isinstance(container, str) or not re.fullmatch(r"[A-Za-z0-9]{1,16}(?:,[A-Za-z0-9]{1,16}){0,3}", container) or _contains_token(container, token):
        container = None
    query = {"id": external_id}
    # The same-origin web client selects its own logged-in server. Do not copy
    # remote ServerId/URL fields into this public link (or require extra auth).
    open_url = f"{browser}/web/index.html#!/details?{urlencode(query, quote_via=quote)}"
    return SourceRecord(
        external_id=external_id, title=title, media_type=TYPE_MAP[kind],
        authors=_creators(item, token), isbn=isbn, library_id=library_id,
        format=container.upper() if container else kind, open_url=open_url, metadata=metadata,
    )


async def _libraries(reader: _Reader, user_id: str, selected: list[str], token: str, catalogue: Catalogue) -> list[str]:
    data = await reader.get(f"Users/{quote(user_id, safe='')}/Views")
    views, total = _page(data)
    if total is not None and total != len(views):
        _warn(catalogue, "incomplete")
    visible = []
    visible_set: set[str] = set()
    for view in views:
        library_id = _identity(view.get("Id"), token) if isinstance(view, dict) else None
        if library_id is None:
            _warn(catalogue, "invalid_record")
        elif library_id not in visible_set:
            visible_set.add(library_id)
            visible.append(library_id)
    if views and not visible:
        raise _error("invalid_response", "Jellyfin returned no usable user-visible library IDs.")
    if selected:
        if any(library_id not in visible_set for library_id in selected):
            _warn(catalogue, "forbidden")
        visible = [library_id for library_id in selected if library_id in visible_set]
        if not visible:
            raise _error("library_unavailable", "None of the selected Jellyfin libraries is visible to this user; check options.user_id and library_ids.")
    if len(visible) > MAX_LIBRARIES:
        _warn(catalogue, "limit_reached")
    return visible[:MAX_LIBRARIES]


async def _walk(reader: _Reader, user_id: str, library_id: str, browser: str, token: str, catalogue: Catalogue, seen: set[str]) -> None:
    offset = 0
    expected = None
    library_seen: set[str] = set()
    while True:
        if reader.pages >= MAX_PAGES or reader.entries >= MAX_ENTRIES:
            raise _error("limit", "Jellyfin exceeded the catalogue page or entry limit; narrow the selected libraries.")
        limit = min(PAGE_SIZE, MAX_ENTRIES - reader.entries)
        params = {
            "UserId": user_id, "ParentId": library_id, "Recursive": "true",
            "IncludeItemTypes": ",".join(TYPE_MAP), "Fields": _FIELDS,
            "EnableUserData": "false", "EnableImages": "false",
            "EnableTotalRecordCount": "true", "SortBy": "SortName", "SortOrder": "Ascending",
            "StartIndex": offset, "Limit": limit,
        }
        reader.pages += 1
        rows, total = _page(await reader.get("Items", params), offset)
        if total is not None:
            if expected is not None and expected != total:
                _warn(catalogue, "incomplete")
            expected = total
        if not rows:
            if expected is not None and offset < expected:
                raise _error("pagination", "Jellyfin ended pagination before its reported total; the catalogue is incomplete.")
            return
        new_ids = 0
        for item in rows[:limit]:
            reader.entries += 1
            if not isinstance(item, dict):
                _warn(catalogue, "invalid_record")
                continue
            external_id = _identity(item.get("Id"), token)
            if external_id is None:
                _warn(catalogue, "invalid_record")
                continue
            if external_id in library_seen:
                _warn(catalogue, "duplicate_record")
                continue
            library_seen.add(external_id)
            new_ids += 1
            record = _record(item, library_id, browser, token)
            if record is None:
                if not isinstance(item.get("Type"), str) or item["Type"] in TYPE_MAP:
                    _warn(catalogue, "invalid_record")
                continue
            if external_id not in seen:
                seen.add(external_id)
                catalogue.records.append(record)
        if len(rows) > limit:
            raise _error("limit", "Jellyfin ignored the requested page size; the catalogue is incomplete.")
        if not new_ids:
            raise _error("pagination", "Jellyfin pagination made no progress; the catalogue is incomplete.")
        offset += len(rows)
        if expected is not None and offset >= expected:
            if offset > expected:
                _warn(catalogue, "incomplete")
            return
        if expected is None and len(rows) < limit:
            return


async def fetch_catalogue(source: dict, credentials: dict) -> Catalogue:
    """Fetch supported records visible to one Jellyfin user, without any writes."""
    token = credentials.get("token")
    if not isinstance(token, str) or not token or len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise _error("credentials", "A valid Jellyfin API/access token is required.")
    base = _root(source.get("base_url"), token)
    browser = _root(source.get("browser_url") or source.get("base_url"), token)
    user_id, selected = _options(source, credentials, token)
    catalogue = Catalogue(records=[])
    seen: set[str] = set()
    failures: list[ConnectorError] = []
    finished_libraries = 0
    try:
        async with asyncio.timeout(CATALOGUE_TIMEOUT):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=5.0),
                follow_redirects=False, trust_env=False,
            ) as client:
                reader = _Reader(client, base, token)
                if user_id is None:
                    me = await reader.get("Users/Me", me=True)
                    user_id = _identity(me.get("Id"), token) if isinstance(me, dict) else None
                    if user_id is None:
                        raise _error("user_required", "Set Jellyfin options.user_id to the user whose visible catalogue should be imported; this token has no user identity.")
                libraries = await _libraries(reader, user_id, selected, token, catalogue)
                for library_id in libraries:
                    try:
                        await _walk(reader, user_id, library_id, browser, token, catalogue, seen)
                        finished_libraries += 1
                    except ConnectorError as exc:
                        failures.append(exc)
                        _warn(catalogue, exc.code)
                        if exc.code in {"auth_failed", "rate_limited", "limit_reached"}:
                            break
    except TimeoutError:
        failure = _error("timeout", "Jellyfin catalogue exceeded the overall time limit; narrow the selected libraries.")
        if not catalogue.records and not finished_libraries:
            raise failure from None
        _warn(catalogue, failure.code)
    if failures and not catalogue.records and not finished_libraries:
        raise failures[0] from None
    return catalogue
