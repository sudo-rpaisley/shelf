"""MusicBrainz and Cover Art Archive integration for Shelf music releases.

MusicBrainz is the canonical identity source for music in Shelf. A Shelf
``items`` row represents an exact catalogued release; ``music_releases`` stores
that release's MusicBrainz identity and Release Group. Multiple physical or
digital formats can therefore share a Release Group while retaining distinct
release metadata and track lists.

All public requests use ``app.services.outbound``. MusicBrainz requires a
meaningful User-Agent and no more than one request per second for ordinary
clients; the identifying header lives here and the host interval is supplied
through Shelf's normal outbound rate-limit configuration.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.services import outbound, provider_result

logger = logging.getLogger(__name__)

MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
COVER_ART_BASE = "https://coverartarchive.org"
USER_AGENT = "Shelf/1.0 (https://github.com/dgahagan/shelf)"

_RELEASE_INCLUDES = "artist-credits+labels+recordings+release-groups"


def _headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT, "Accept": "application/json"}


def _artist_credit(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    parts: list[str] = []
    for credit in value:
        if not isinstance(credit, dict):
            continue
        name = credit.get("name")
        if not name and isinstance(credit.get("artist"), dict):
            name = credit["artist"].get("name")
        if name:
            parts.append(str(name))
        join = credit.get("joinphrase")
        if join:
            parts.append(str(join))
    text = "".join(parts).strip()
    return text or None


def _first_label(release: dict) -> tuple[str | None, str | None]:
    for entry in release.get("label-info") or []:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label") or {}
        name = label.get("name") if isinstance(label, dict) else None
        catno = entry.get("catalog-number")
        if name or catno:
            return (str(name) if name else None, str(catno) if catno else None)
    return None, None


def _release_group(release: dict) -> tuple[str | None, str | None, str | None]:
    group = release.get("release-group") or {}
    if not isinstance(group, dict):
        return None, None, None
    secondary = group.get("secondary-types") or []
    release_type = group.get("primary-type")
    if secondary:
        suffix = ", ".join(str(v) for v in secondary if v)
        if suffix:
            release_type = f"{release_type} · {suffix}" if release_type else suffix
    return group.get("id"), release_type, group.get("first-release-date")


def _normalise_medium(medium: dict, fallback_position: int) -> dict:
    tracks: list[dict] = []
    for index, track in enumerate(medium.get("tracks") or [], start=1):
        if not isinstance(track, dict):
            continue
        recording = track.get("recording") or {}
        recording = recording if isinstance(recording, dict) else {}
        tracks.append({
            "position": index,
            # Text deliberately preserves vinyl positions such as A1/B2.
            "number": str(track.get("number") or index),
            "title": track.get("title") or recording.get("title") or "Untitled",
            "artist_credit": _artist_credit(track.get("artist-credit"))
                or _artist_credit(recording.get("artist-credit")),
            "duration_ms": track.get("length") or recording.get("length"),
            "musicbrainz_recording_id": recording.get("id"),
        })
    return {
        "position": medium.get("position") or fallback_position,
        "format": medium.get("format"),
        "title": medium.get("title"),
        "track_count": medium.get("track-count") or len(tracks),
        "tracks": tracks,
    }


def normalise_release(release: dict) -> dict:
    """Convert a MusicBrainz release payload into Shelf's music shape."""
    label, catalog_number = _first_label(release)
    group_id, release_type, first_release_date = _release_group(release)
    media = [
        _normalise_medium(medium, index)
        for index, medium in enumerate(release.get("media") or [], start=1)
        if isinstance(medium, dict)
    ]
    format_names = [m.get("format") for m in media if m.get("format")]
    return {
        "title": release.get("title") or "Untitled",
        "artist_credit": _artist_credit(release.get("artist-credit")),
        "musicbrainz_release_id": release.get("id"),
        "musicbrainz_release_group_id": group_id,
        "release_type": release_type,
        "release_status": release.get("status"),
        "release_date": release.get("date"),
        "first_release_date": first_release_date,
        "country": release.get("country"),
        "label": label,
        "catalog_number": catalog_number,
        "barcode": release.get("barcode"),
        "packaging": release.get("packaging"),
        "media_count": len(media),
        "format_summary": ", ".join(dict.fromkeys(format_names)) if format_names else None,
        "media": media,
        "source": "musicbrainz",
    }


def _search_summary(release: dict) -> dict:
    normalised = normalise_release(release)
    summary = {
        key: normalised.get(key)
        for key in (
            "title", "artist_credit", "musicbrainz_release_id",
            "musicbrainz_release_group_id", "release_type", "release_status",
            "release_date", "country", "label", "catalog_number", "barcode",
            "packaging", "media_count", "format_summary",
        )
    }
    summary["disambiguation"] = str(release.get("disambiguation") or "")[:500]
    summary["track_count"] = sum(m.get("track_count") or 0 for m in normalised["media"])
    return summary


def artwork_url(value: Any) -> str | None:
    """CAA still returns HTTP links. Upgrade only its declared image hosts."""
    from app.services import covers

    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "http":
            value = urlunsplit(parsed._replace(scheme="https"))
        return value if covers.is_allowed_cover_url(value) else None
    except ValueError:
        return None


async def _request_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
    provider: str = "musicbrainz",
) -> provider_result.ProviderResult:
    try:
        # CAA's listing endpoint normally redirects to archive.org/index.json.
        # httpx does not follow it by default; treating that 307 as a miss made
        # otherwise valid music releases appear to have no artwork at all.
        for hop in range(6):
            resp = await outbound.fetch(
                client, "GET", url, params=params if hop == 0 else None,
                headers=_headers(), timeout=15, follow_redirects=False,
            )
            if provider != "coverartarchive" or resp.status_code not in (301, 302, 303, 307, 308):
                break
            next_url = artwork_url(urljoin(url, resp.headers.get("location", "")))
            if not resp.headers.get("location") or not next_url or hop == 5:
                return provider_result.transport_failed(provider)
            url = next_url
    except (httpx.ConnectError, httpx.TimeoutException):
        return provider_result.transport_failed(provider)
    except Exception:
        logger.debug("%s request failed", provider, exc_info=True)
        return provider_result.transport_failed(provider)

    classified = provider_result.classify_response(provider, resp)
    if classified is not None:
        if resp.status_code == 503:
            return provider_result.transport_failed(provider)
        return classified
    try:
        return provider_result.found(provider, resp.json(), status=resp.status_code)
    except ValueError:
        return provider_result.transport_failed(provider)


async def search_releases(
    query: str,
    client: httpx.AsyncClient,
    *,
    artist: str | None = None,
    barcode: str | None = None,
    catalog_number: str | None = None,
    media_type: str = "",
    country: str = "",
    limit: int = 15,
) -> provider_result.ProviderResult:
    """Search exact MusicBrainz releases, not Release Groups."""
    clauses: list[str] = []
    def literal(value):
        # Quoting alone does not escape Lucene: AC/DC, quotes and colons in
        # genuine album/artist names otherwise alter or invalidate the query.
        return re.sub(r'([+\-!(){}\[\]^"~*?:\\/|&])', r'\\\1', value.strip())

    if barcode:
        clauses.append(f'barcode:"{literal(barcode)}"')
    if catalog_number:
        clauses.append(f'catno:"{literal(catalog_number)}"')
    if query and query.strip():
        clauses.append(f'release:"{literal(query)}"')
    if artist and artist.strip():
        clauses.append(f'artist:"{literal(artist)}"')
    if not clauses:
        return provider_result.no_match("musicbrainz")
    formats = {"vinyl": "(Vinyl OR 7vinyl OR 10vinyl OR 12vinyl)",
               "cd": "CD", "cassette": "Cassette", "digital_music": "DigitalMedia"}
    if media_type in formats:
        clauses.append("format:" + formats[media_type])
    if country.strip():
        clauses.append(f'country:"{literal(country)}"')

    result = await _request_json(
        client,
        f"{MUSICBRAINZ_BASE}/release/",
        params={
            "query": " AND ".join(clauses),
            "fmt": "json",
            "limit": str(max(1, min(limit, 100))),
        },
    )
    if not result.found:
        return result
    releases = (result.payload.get("releases") or []) if isinstance(result.payload, dict) else None
    if not isinstance(releases, list):
        return provider_result.transport_failed("musicbrainz")
    try:
        return result.with_payload([_search_summary(r) for r in releases[:100] if isinstance(r, dict)])
    except (TypeError, ValueError, AttributeError):
        return provider_result.transport_failed("musicbrainz")


async def search_by_barcode(
    barcode: str, client: httpx.AsyncClient, *, limit: int = 15
) -> provider_result.ProviderResult:
    return await search_releases("", client, barcode=barcode, limit=limit)


async def search_by_catalog_number(
    catalog_number: str,
    client: httpx.AsyncClient,
    *,
    artist: str | None = None,
    limit: int = 15,
) -> provider_result.ProviderResult:
    return await search_releases(
        "", client, artist=artist, catalog_number=catalog_number, limit=limit
    )


async def lookup_release(
    release_id: str, client: httpx.AsyncClient
) -> provider_result.ProviderResult:
    release_id = (release_id or "").strip()
    if not release_id:
        return provider_result.no_match("musicbrainz")
    result = await _request_json(
        client,
        f"{MUSICBRAINZ_BASE}/release/{release_id}",
        params={"inc": _RELEASE_INCLUDES, "fmt": "json"},
    )
    if not result.found:
        return result
    if not isinstance(result.payload, dict) or not result.payload.get("id"):
        return provider_result.no_match("musicbrainz")
    try:
        return result.with_payload(normalise_release(result.payload))
    except (TypeError, ValueError, AttributeError):
        return provider_result.transport_failed("musicbrainz")


async def cover_art(
    release_id: str, client: httpx.AsyncClient, *, group: bool = False
) -> provider_result.ProviderResult:
    """Return Cover Art Archive candidates for one exact MusicBrainz release."""
    release_id = (release_id or "").strip()
    if not release_id:
        return provider_result.no_match("coverartarchive")
    result = await _request_json(
        client,
        f"{COVER_ART_BASE}/{'release-group' if group else 'release'}/{release_id}",
        provider="coverartarchive",
    )
    if not result.found:
        if result.status == 404:
            return provider_result.found("coverartarchive", [], status=404)
        return result

    images = (result.payload.get("images") or []) if isinstance(result.payload, dict) else None
    if not isinstance(images, list):
        return provider_result.transport_failed("coverartarchive")
    candidates: list[dict] = []
    for image in images[:100]:
        if not isinstance(image, dict):
            continue
        thumbnails = image.get("thumbnails") or {}
        thumbnails = thumbnails if isinstance(thumbnails, dict) else {}
        full = artwork_url(image.get("image"))
        thumb = artwork_url(thumbnails.get("500")) or artwork_url(thumbnails.get("250")) or full
        if not full:
            continue
        candidates.append({
            "url": full,
            "thumbnail": thumb,
            "front": bool(image.get("front")),
            "back": bool(image.get("back")),
            "types": [str(v) for v in image.get("types") or []],
            "comment": image.get("comment") or "",
            "source": "Cover Art Archive",
        })
    candidates.sort(key=lambda c: (not c["front"], c["back"]))
    return result.with_payload(candidates)
