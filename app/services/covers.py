import asyncio
import ipaddress
import logging
import shutil
import socket
import tempfile
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx

from app.config import COVERS_DIR
from app.services import audnexus, book_cover_search, igdb, national, outbound, porbase, provider_result, tmdb
from app.services.transaction_resources import cover_publication_guard

logger = logging.getLogger(__name__)


class CoverStage:
    """Own only a fresh download ID's finite image filenames, never a glob.

    Connectors can retain their validated format (e.g. ``.webp``). Cleanup on
    denial covers that SAME stage even when publication never starts. Download
    routines remain responsible for validating bytes and provider origins.
    """

    SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")

    def __init__(self, item_id):
        self.item_id = item_id
        for _ in range(5):
            self.download_id = -((uuid4().int & ((1 << 62) - 1)) + 1)
            self._paths = {f"covers/{self.download_id}{suffix}": COVERS_DIR / f"{self.download_id}{suffix}"
                           for suffix in self.SUFFIXES}
            if not any(path.exists() or path.is_symlink() for path in self._paths.values()):
                break
        else:
            raise RuntimeError("Could not allocate an unused cover stage")
        self.path = self.path_for(".jpg")

    def path_for(self, suffix):
        """Exact writable path for an owned, supported image suffix."""
        if suffix not in self.SUFFIXES:
            raise ValueError("Unsupported staged cover suffix")
        return self._paths[f"covers/{self.download_id}{suffix}"]

    def owns(self, cover_path):
        return cover_path in self._paths

    def __enter__(self):
        return self

    def __exit__(self, *_):
        for path in self._paths.values():
            path.unlink(missing_ok=True)


def publish_staged_cover(db, stage: CoverStage, cover_path: str, *, authorize, store) -> str:
    """Publish one stage through the caller's exact locked identity callbacks.

    ``authorize(db)`` must refuse stale liveness/identity/library/principal;
    ``store(db, published_path)`` uses the authoritative domain writer. Both
    are synchronous, inside the same write scope, with no provider await.
    The connection retains the file preimage until its real outer commit or
    rollback/failed commit/close. Do not use raw SQL transaction boundaries.
    No router imports or independent connector filesystem writer are needed.
    """
    from app.services import item_write
    from app.services.transaction_resources import register_transaction_resource

    if not stage.owns(cover_path):
        raise ValueError("Cover does not belong to this stage")
    source = stage._paths[cover_path]
    published = f"covers/{stage.item_id}{source.suffix}"
    dest = source.with_name(f"{stage.item_id}{source.suffix}")
    backup = None
    installed = False
    had_file = False
    resource = None

    def finish():
        if backup is not None:
            backup.unlink(missing_ok=True)

    def undo():
        nonlocal installed
        if installed:
            if had_file:
                # Keep the preimage even if restoration fails (including an
                # error observed AFTER its actual rename). A retry must still
                # have the original bytes, not a consumed/missing journal.
                with tempfile.NamedTemporaryFile(prefix=".cover-restore-", dir=dest.parent, delete=False) as handle:
                    restoring = Path(handle.name)
                try:
                    shutil.copyfile(backup, restoring)
                    restoring.replace(dest)
                finally:
                    restoring.unlink(missing_ok=True)
            else:
                dest.unlink(missing_ok=True)
            installed = False
        finish()

    try:
        with item_write._write_scope(db):
            authorize(db)
            if not source.is_file() or source.is_symlink():
                raise ValueError("Staged cover bytes are missing")
            # Enrollment owns the namespace fence BEFORE inspecting any shared
            # destination/preimage. Callbacks are safe even before copying it.
            resource = register_transaction_resource(
                db, publication_dir=COVERS_DIR, after_commit=finish, after_rollback=undo,
            )
            if dest.is_symlink():
                raise ValueError("Cover destination must not be a symlink")
            if dest.exists() and not dest.is_file():
                raise ValueError("Cover destination must be a file")
            had_file = dest.is_file()
            if had_file:
                # Exclusive creation makes the preimage a journal-owned
                # resource, never an existing user's similarly named file.
                with tempfile.NamedTemporaryFile(prefix=".cover-backup-", dir=dest.parent, delete=False) as handle:
                    backup = Path(handle.name)
                shutil.copyfile(dest, backup)
            store(db, published)
            # Own recovery BEFORE the atomic replace: an injected I/O error or
            # interruption can be observed after the rename has taken effect.
            installed = True
            source.replace(dest)
    except BaseException:
        # _write_scope already undid the local DB savepoint. A caller may
        # catch this error and commit unrelated work, so undo locally too.
        # The resource marks recovery required BEFORE undo, retains a failed
        # journal, and cancels/releases only after successful recovery.
        if resource is None:
            undo()
        else:
            resource.rollback()
        raise
    return published


# Trusted domains for cover image downloads
ALLOWED_COVER_DOMAINS = {
    "covers.openlibrary.org",
    "openlibrary.org",
    "books.google.com",
    "books.googleapis.com",
    "www.googleapis.com",
    "lh3.googleusercontent.com",  # Google Books CDN redirect target
    "images-na.ssl-images-amazon.com",
    "m.media-amazon.com",
    "hardcover.app",
    "assets.hardcover.app",
    "images.igdb.com",
    "image.tmdb.org",  # TMDb posters (tmdb.TMDB_IMAGE_BASE)
    "portal.dnb.de",  # DNB/MVB cover service for German (978-3) ISBNs
    "coverartarchive.org",  # MusicBrainz release artwork
    "archive.org",  # Cover Art Archive redirect target
    "rnod.bnportugal.gov.pt",  # PORBASE cover scans for Portuguese (978-989, 978-972) ISBNs
    "neodb.social",  # NeoDB cover images for Chinese (978-7, Taiwan, Hong Kong) ISBNs
}

# Suffix-matched domains (subdomain rotates): covers.openlibrary.org and the
# Cover Art Archive serve images via Internet Archive hosts such as
# ia800505.us.archive.org.
ALLOWED_COVER_SUFFIXES = (".us.archive.org",)


async def download_cover(item_id: int, isbn: str | None, cover_url: str | None, cover_id: int | None, client: httpx.AsyncClient, hardcover_cover_url: str | None = None) -> str | None:
    """Download a cover image and return the relative path, or None on failure."""
    dest = COVERS_DIR / f"{item_id}.jpg"

    # Try Open Library cover by cover ID first (best quality)
    if cover_id:
        url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
        if await _download(url, dest, client):
            return f"covers/{item_id}.jpg"

    # Try Open Library cover by ISBN
    if isbn:
        url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
        if await _download(url, dest, client):
            return f"covers/{item_id}.jpg"

    # Try Hardcover cover image
    if hardcover_cover_url and is_allowed_cover_url(hardcover_cover_url):
        if await _download(hardcover_cover_url, dest, client):
            return f"covers/{item_id}.jpg"

    # Try the DNB/MVB cover service for German-group ISBNs (probe 2026-08-20:
    # stable ISBN-keyed URL, image/jpeg on hit, 404 on miss, no redirects) —
    # non-German ISBNs never pay the extra call
    if isbn and isbn.startswith("9783"):
        url = f"https://portal.dnb.de/opac/mvb/cover?isbn={isbn}"
        if await _download(url, dest, client):
            return f"covers/{item_id}.jpg"

    # Try Amazon product image (reliable for most books, but only for 978-prefix ISBNs)
    if isbn and isbn.startswith("978"):
        isbn10 = _isbn13_to_isbn10_for_amazon(isbn)
        if isbn10 != isbn:  # only if conversion succeeded
            url = f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01._SCLZZZZZZZ_SX500_.jpg"
            if await _download(url, dest, client):
                return f"covers/{item_id}.jpg"

    # Try provided cover URL (e.g., from Google Books)
    if cover_url and is_allowed_cover_url(cover_url):
        if await _download(cover_url, dest, client):
            return f"covers/{item_id}.jpg"

    # Last resort for Portuguese-group ISBNs: the National Library's own scan
    # of the cover, linked from the PORBASE record. The link carries record
    # ids, so it costs one resolver request to find; other ISBNs never pay it.
    # The registry decides which ISBNs these are, so the prefixes live in one
    # place.
    if isbn and national.provider_for(isbn) is porbase:
        url = await porbase.cover_url(isbn, client)
        if url and await _download(url, dest, client):
            return f"covers/{item_id}.jpg"

    return None


MAX_COVER_SIZE = 10 * 1024 * 1024  # 10 MB
MIN_COVER_SIZE = 100  # bytes

# Format detection is shared by the existing validator and format-preserving
# staging. RIFF alone is not WebP (it also identifies WAV/AVI containers).
_IMAGE_FORMATS = (
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
)


def _image_format(content: bytes) -> tuple[str, str] | None:
    for signature, suffix, content_type in _IMAGE_FORMATS:
        if content.startswith(signature):
            return suffix, content_type
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def _looks_like_image(content: bytes) -> bool:
    """Check if content starts with known image magic bytes."""
    return _image_format(content) is not None


def validate_uploaded_cover(content: bytes) -> bool:
    """True if `content` is an acceptable cover upload (size and image magic).

    Pure: no mkdir, no write. Safe to call before a route knows whether it
    will go on to save.
    """
    if len(content) < MIN_COVER_SIZE or len(content) > MAX_COVER_SIZE:
        return False
    return _looks_like_image(content)


def stage_uploaded_cover(stage: CoverStage, content: bytes, *, content_type: str | None = None) -> str | None:
    """Validate once, infer the actual image suffix, and write ONLY this stage.

    An optional declared MIME type must match the detected bytes. The original
    accepted bytes are retained, not converted or mislabeled as JPEG. This is
    the common service boundary for connector responses and pending uploads;
    publication still requires ``publish_staged_cover`` and its locked checks.
    """
    if not validate_uploaded_cover(content):
        return None
    detected = _image_format(content)
    if detected is None:
        return None
    suffix, detected_type = detected
    if content_type is not None and content_type.split(";", 1)[0].strip().lower() != detected_type:
        return None
    path = stage.path_for(suffix)
    if path.is_symlink():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return f"covers/{stage.download_id}{suffix}"


def write_uploaded_cover(item_id: int, content: bytes, *, db=None) -> str:
    """Write already-validated cover bytes to `{item_id}.jpg`. Returns the relative path.

    Does no validation: the caller must have passed `content` through
    `validate_uploaded_cover` first. The two are separate so a route that may
    still refuse the save (#127) can validate early and write last, leaving
    the existing cover untouched when it refuses.

    Only an explicitly supplied owning connection can borrow a publication
    fence. With no owner this protects the write only, not subsequent SQL.
    Negative IDs belong to private stages/previews, not shared destinations.
    """
    # The stable lock lives beside covers, whose parent may not exist yet.
    # Bootstrap only that parent, not the shared destination, before locking.
    COVERS_DIR.parent.mkdir(parents=True, exist_ok=True)
    with nullcontext() if item_id < 0 else cover_publication_guard(COVERS_DIR, conn=db):
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        (COVERS_DIR / f"{item_id}.jpg").write_bytes(content)
    return f"covers/{item_id}.jpg"


_MANUAL_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MANUAL_MAX_REDIRECTS = 5


async def _resolve_host_addresses(host: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname away from the event loop for manual cover safety."""
    def _resolve():
        return socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)

    infos = await asyncio.to_thread(_resolve)
    addresses = set()
    for info in infos:
        raw = info[4][0]
        try:
            addresses.add(ipaddress.ip_address(raw))
        except ValueError:
            continue
    return addresses


def _address_is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Only globally routable destinations are valid manual cover sources."""
    return address.is_global


async def is_public_cover_url(url: str) -> bool:
    """Validate a user-pasted image URL before the server requests it.

    Provider cover URLs use the tighter static allowlist elsewhere. Manual
    covers intentionally support arbitrary sites, so the boundary here is
    public HTTPS: no credentials, localhost, RFC1918, link-local, multicast,
    reserved or otherwise non-global destinations. Every redirect is checked
    again by ``download_manual_cover``.
    """
    try:
        parsed = urlparse((url or "").strip())
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        host = parsed.hostname.rstrip(".")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                addresses = await _resolve_host_addresses(host)
            except (OSError, socket.gaierror):
                return False
            return bool(addresses) and all(_address_is_public(addr) for addr in addresses)
        return _address_is_public(literal)
    except (ValueError, UnicodeError):
        return False


async def download_manual_cover(
    item_id: int, url: str, client: httpx.AsyncClient
) -> str | None:
    """Download a manually supplied public HTTPS image into Shelf.

    Redirects are followed manually rather than through httpx so each target
    is revalidated before a request is sent. Bytes are checked before the
    destination file is touched, so a failed URL never destroys an existing
    manually selected/uploaded cover.
    """
    current = (url or "").strip()
    for _ in range(_MANUAL_MAX_REDIRECTS + 1):
        if not await is_public_cover_url(current):
            return None
        try:
            resp = await outbound.fetch(
                client, "GET", current, follow_redirects=False, retry_timeouts=True
            )
        except Exception:
            logger.debug("Manual cover download failed for %s", current, exc_info=True)
            return None

        if resp.status_code in _MANUAL_REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if not location:
                return None
            current = urljoin(current, location)
            continue
        if resp.status_code != 200:
            return None

        content = resp.content
        if len(content) < MIN_COVER_SIZE or len(content) > MAX_COVER_SIZE:
            return None
        if not _looks_like_image(content):
            return None

        # Network/validation is complete. Never await or begin SQL while this
        # legacy file-only guard is held; private stages do not contend.
        COVERS_DIR.parent.mkdir(parents=True, exist_ok=True)
        with nullcontext() if item_id < 0 else cover_publication_guard(COVERS_DIR):
            COVERS_DIR.mkdir(parents=True, exist_ok=True)
            dest = COVERS_DIR / f"{item_id}.jpg"
            dest.write_bytes(content)
        return f"covers/{item_id}.jpg"
    return None


async def search_cover_by_title(
    title: str,
    author: str | None,
    client: httpx.AsyncClient,
    *,
    google_api_key: str | None = None,
) -> list[dict]:
    """Search for cover candidates by title/author. Returns list of {url, source, thumbnail}."""
    result = await search_book_covers(
        title, author, None, client, google_api_key=google_api_key,
    )
    return result.payload if result.found else []


def save_uploaded_cover(item_id: int, content: bytes, *, db=None) -> str | None:
    """Save an uploaded cover image. Returns relative path or None."""
    return write_uploaded_cover(item_id, content, db=db) if validate_uploaded_cover(content) else None


async def search_book_covers(
    title: str, author: str | None, isbn: str | None,
    client: httpx.AsyncClient, *, google_api_key: str | None = None,
) -> provider_result.ProviderResult:
    """Search exact ISBN editions first, then offer title matches for review."""
    return await book_cover_search.search(
        title, author, isbn, client, google_api_key=google_api_key,
    )

# --- Media-type dispatch for the cover picker -------------------------------
#
# The picker searches book endpoints for every media type unless it is told
# otherwise. These two tables are the one place that says which media type
# reaches which service and what that service needs to be configured with.
#
# G49: the router's "which credential is missing?" gate derives its key list
# from `required_credentials()` rather than keeping its own copy, so the gate
# and the dispatch cannot disagree about what "configured" means. IGDB's
# two-field credential is exactly the shape that entry documents.

MEDIA_TYPE_PROVIDERS: dict[str, str] = {
    "dvd": "tmdb",
    "video_game": "igdb",
}

CREDENTIAL_KEYS: dict[str, tuple[str, ...]] = {
    "tmdb": ("tmdb_api_key",),
    "igdb": ("igdb_client_id", "igdb_client_secret"),
}

# Six rows of the picker's two-column grid. Comparable to the book path's 10
# (5 Google Books + 5 Open Library).
MAX_CANDIDATES = 12

# How much of a game's title fits the tile caption before it is cut.
_LABEL_CHARS = 24


def required_credentials(media_type: str | None) -> tuple[str, ...]:
    """Settings keys the given media type's cover provider needs.

    `()` for the book default — the book endpoints need no credential.
    """
    provider = MEDIA_TYPE_PROVIDERS.get(media_type or "")
    return CREDENTIAL_KEYS.get(provider or "", ())


def _col(item, name: str):
    """Read an optional column off a `sqlite3.Row` *or* a plain dict.

    `sqlite3.Row` raises `IndexError` for a column that is not in the SELECT
    and has no `.get()`, so `item.get(name)` is not available here and a bare
    subscript would turn a narrow SELECT into a 500.
    """
    try:
        return item[name]
    except (IndexError, KeyError):
        return None


def _label(text: str | None, limit: int = _LABEL_CHARS) -> str:
    """Truncate a game title to the tile caption's width, ellipsis when cut."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


async def search_covers(
    item, query: str, client: httpx.AsyncClient, *, creds: dict
) -> provider_result.ProviderResult:
    """Find cover candidates for `item`, dispatching on its media type.

    Returns a `ProviderResult` whose payload is a `list[dict]`, each
    `{"url", "thumbnail", "source"}` — the shape `fragments/cover_search.html`
    renders. **Never raises**, which is the half of the old contract that
    stays; what went is `[]`-for-everything, which is why a rejected TMDb key
    and a film with no posters were the same thing on screen (G47's
    `_search_note` face).

    A provider's non-`found` outcome is carried out as it stands, so the route
    can project it through `scan_outcome.not_found_status`. `found` with an
    **empty** payload is a real answer and stays the generic "No covers found"
    line: the provider replied, and nothing survived the filter.

    `item` is a mapping with `media_type`, `title` and `authors`, plus
    `publish_year` and `platform` for the two new branches. It is a
    `sqlite3.Row` in production and a plain dict in tests, so optional columns
    are read through `_col`.

    Anything not in `MEDIA_TYPE_PROVIDERS` — an unrecognised string, `None`,
    or `""` — takes the book branch. `media_type` has no CHECK constraint in
    the schema (`app/database.py:19`), so that default is load-bearing: an
    unknown value must reach the book path, not raise.

    G11: every candidate is a URL string handed to `_download_to_item` later,
    so the post-redirect allowlist re-check stays on the path. No provider
    fetches an image itself.
    """
    provider = MEDIA_TYPE_PROVIDERS.get(_col(item, "media_type") or "")
    if provider == "tmdb":
        return await _tmdb_candidates(item, query, client, creds)
    if provider == "igdb":
        return await _igdb_candidates(item, query, client, creds)
    typed = query.strip() != (_col(item, "title") or "").strip()
    result = await search_book_covers(
        query,
        None if typed else _col(item, "authors"),
        None if typed else _col(item, "isbn"),
        client,
        google_api_key=creds.get("google_books_api_key"),
    )
    audible = await _audible_asin_candidate(item, typed, client, creds)
    if audible:
        merged = await _book_leg_candidates(item, typed, client, creds, result)
        candidates = _dedupe_by_url(audible + (merged.payload if merged.found else []))
        return provider_result.found(merged.provider if merged.found else "audnexus", candidates)
    return await _book_leg_candidates(item, typed, client, creds, result)


async def _audible_asin_candidate(item, typed: bool, client, creds: dict) -> list[dict]:
    """The Audible edition's own cover, first in the grid, by ASIN.

    Audiobook with an ASIN and an untyped query only: a typed query searches
    its text alone. Any Audnexus outcome but `found` with an image yields
    nothing, leaving the book path's result untouched. The image is fetched
    later through `_download_to_item` (G11).
    """
    asin = (_col(item, "asin") or "").strip()
    if typed or _col(item, "media_type") != "audiobook" or not asin:
        return []
    found = await audnexus.lookup(asin, client, region=creds.get("audible_region") or "us")
    url = found.payload.get("cover_url") if found.found else None
    if not url:
        return []
    return [{"url": url, "thumbnail": url, "source": "Audible · ASIN match", "match": "asin"}]


async def _book_leg_candidates(item, typed: bool, client, creds: dict, result):
    from app.services import librarything
    # A typed query searches on that text alone (docs/user-guide/items.md), so
    # the stored ISBN must not reach LibraryThing either.
    if (not typed and _col(item, "media_type") in librarything.BOOK_TYPES
            and _col(item, "isbn") and creds.get("librarything_token")):
        mapped = await librarything.search_covers(
            _col(item, "isbn"), creds["librarything_token"], client,
            creds.get("google_books_api_key"))
        candidates = _dedupe_by_url((result.payload if result.found else []) + (mapped.payload if mapped.found else []))
        if candidates:
            return provider_result.found("librarything", candidates)
        return provider_result.combine(
            [provider_result.no_match(result.provider) if result.found else result, mapped],
            provider="librarything",
        )
    if result.found:
        return result.with_payload(_dedupe_by_url(result.payload))
    return result


def _dedupe_by_url(candidates: list[dict]) -> list[dict]:
    """Drop later candidates repeating an earlier URL, keeping the first's position.

    A later duplicate that carries `edition_isbn` lends it (and
    `edition_language`) to the kept candidate, so picking that URL still records
    the LibraryThing provenance. Never onto an exact `match == "isbn"` tile:
    Open Library editions share cover IDs, so a LibraryThing tile can repeat
    the exact tile's URL, and lending there would store "English cover from
    another edition" against the item's own edition. `label` is never copied:
    the kept tile keeps its own second line. Kept dicts are copied before any
    key is added.
    """
    kept: dict[str, dict] = {}
    for candidate in candidates:
        url = candidate.get("url")
        first = kept.get(url)
        if first is None:
            kept[url] = candidate
        elif (candidate.get("edition_isbn") and not first.get("edition_isbn")
              and first.get("match") != "isbn"):
            kept[url] = {**first, "edition_isbn": candidate["edition_isbn"],
                         "edition_language": candidate.get("edition_language")}
    return list(kept.values())


async def _tmdb_candidates(
    item, query: str, client: httpx.AsyncClient, creds: dict
) -> provider_result.ProviderResult:
    """TMDb's poster set for a film, as a `ProviderResult`. Never raises.

    Two upstream calls, because `items` stores no `tmdb_id`: find the film,
    then list its posters. `tmdb.search_movies` returns a `ProviderResult`
    whose payload is a **list** (G45), so the `[0]` unwrap below is explicit
    at this call site rather than hidden in a shared helper.

    A non-`found` search outcome is returned as it stands — that is what lets
    the picker say "TMDb rejected the configured key" instead of "no covers".
    Everything after a successful search is `found` with whatever survived:
    an empty poster list is a genuine miss, not a failure.

    Labelled by language: TMDb's poster sets are largely the same film in
    different languages and are otherwise indistinguishable at tile size.
    """
    key = (creds.get("tmdb_api_key") or "").strip()
    if not key:
        # Unreachable from the two picker routes, which check credentials
        # first — but it is the honest value, and `_search_note` still wins on
        # screen for the unconfigured case.
        return provider_result.no_credential("tmdb")
    try:
        result = await tmdb.search_movies(query, key, client, limit=5)
        # Never `if not result:` — `ProviderResult.__bool__` raises, and the
        # `except Exception` below would swallow it into a miss: green and wrong.
        if not result.found:
            return result
        results = result.payload

        hit = None
        year = _col(item, "publish_year")
        if year:
            hit = next((r for r in results if r.get("publish_year") == year), None)
        if hit is None:
            hit = results[0] if results else None
        if hit is None:
            return provider_result.found("tmdb", [])

        tmdb_id = hit.get("tmdb_id")
        if not tmdb_id:
            return provider_result.found("tmdb", [])

        posters = await tmdb.search_posters(tmdb_id, key, client, limit=MAX_CANDIDATES)
        candidates = []
        for poster in posters:
            file_path = poster.get("file_path")
            if not file_path:
                continue
            lang = poster.get("iso_639_1")
            label = f"TMDb · {lang.upper()}" if isinstance(lang, str) and lang.strip() else "TMDb"
            candidates.append({
                "url": tmdb.image_url(file_path, "w500"),
                "thumbnail": tmdb.image_url(file_path, "w185"),
                "source": label,
            })
            if len(candidates) >= MAX_CANDIDATES:
                break
        return provider_result.found("tmdb", candidates)
    except Exception:
        logger.debug("TMDb cover search failed", exc_info=True)
        return provider_result.transport_failed("tmdb")


async def _igdb_candidates(
    item, query: str, client: httpx.AsyncClient, creds: dict
) -> provider_result.ProviderResult:
    """IGDB cover art and artwork for a game, as a `ProviderResult`. Never raises.

    One upstream call. `igdb.search_game_art` returns a `ProviderResult` whose
    payload is a **list** (G45), one entry per game; this emits one candidate
    per *image*, each game's cover before its artwork. A non-`found` outcome is
    returned as it stands, so a rejected Twitch credential reaches the picker
    as a rejection rather than as an empty gallery.

    The label carries provider, which game, and cover-versus-artwork, because
    region-variant covers and key art are indistinguishable at thumbnail size.
    """
    cid = (creds.get("igdb_client_id") or "").strip()
    secret = (creds.get("igdb_client_secret") or "").strip()
    if not cid or not secret:
        # Unreachable from the routes (G49: they check both fields first), but
        # the honest value; `_search_note` is what the user actually sees.
        return provider_result.no_credential("igdb")
    try:
        result = await igdb.search_game_art(
            query, cid, secret, client,
            platform=_col(item, "platform"),
            limit=5,
        )
        # Never `if not result:` — `ProviderResult.__bool__` raises, and the
        # `except Exception` below would swallow it into `[]`: green and wrong.
        if not result.found:
            return result
        games = result.payload
        candidates = []
        for game in games:
            name = _label(game.get("title"))
            cover_id = game.get("cover_image_id")
            if cover_id:
                candidates.append({
                    "url": igdb.image_url(cover_id, "t_cover_big"),
                    "thumbnail": igdb.image_url(cover_id, "t_cover_small"),
                    "source": f"IGDB · {name} · cover",
                })
            for art_id in game.get("artwork_image_ids") or []:
                candidates.append({
                    "url": igdb.image_url(art_id, "t_720p"),
                    "thumbnail": igdb.image_url(art_id, "t_screenshot_med"),
                    "source": f"IGDB · {name} · art",
                })
        return provider_result.found("igdb", candidates[:MAX_CANDIDATES])
    except Exception:
        logger.debug("IGDB cover search failed", exc_info=True)
        return provider_result.transport_failed("igdb")


def _isbn13_to_isbn10_for_amazon(isbn13: str) -> str:
    """Convert ISBN-13 to ISBN-10 for Amazon image URLs."""
    from app.services.isbn import isbn13_to_isbn10
    result = isbn13_to_isbn10(isbn13)
    return result if result else isbn13


def is_allowed_cover_url(url: str) -> bool:
    """Check if a URL is from a trusted cover image domain."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if parsed.scheme != "https" or not host:
            return False
        return host in ALLOWED_COVER_DOMAINS or host.endswith(ALLOWED_COVER_SUFFIXES)
    except Exception:
        return False


async def _download_to_item(item_id: int, url: str, client: httpx.AsyncClient) -> str | None:
    """Download a URL as the cover for an item. Returns relative path or None."""
    if not is_allowed_cover_url(url):
        return None
    dest = COVERS_DIR / f"{item_id}.jpg"
    if await _download(url, dest, client):
        return f"covers/{item_id}.jpg"
    return None


async def _download(url: str, dest, client: httpx.AsyncClient) -> bool:
    """Download an image URL to dest. Returns True on success."""
    try:
        resp = await outbound.fetch(client, "GET", url, follow_redirects=True, retry_timeouts=True)
        if resp.status_code != 200:
            logger.debug("Cover download failed for %s: HTTP %d", url, resp.status_code)
            return False
        # Validate the final URL after any redirects to prevent allowlist bypass
        final_url = str(resp.url)
        if not is_allowed_cover_url(final_url):
            logger.warning("Cover redirect to untrusted domain: %s -> %s", url, final_url)
            return False
        content = resp.content
        # Open Library returns a 1x1 pixel for missing covers
        if len(content) < 1000 or len(content) > MAX_COVER_SIZE:
            logger.debug("Cover download rejected for %s: size=%d bytes", url, len(content))
            return False
        try:
            private_stage = int(dest.stem) < 0
        except ValueError:
            private_stage = False
        # Legacy provider writers protect only their final filesystem mutation;
        # the fence is released before any caller's later SQL. No network await
        # occurs under it, and no ambient connection ownership is borrowed.
        COVERS_DIR.parent.mkdir(parents=True, exist_ok=True)
        with nullcontext() if private_stage else cover_publication_guard(COVERS_DIR):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        return True
    except Exception:
        logger.debug("Cover download error for %s", url, exc_info=True)
        return False
