import asyncio
import json
import logging

import httpx

from app.database import get_db, get_setting
from app.services import covers
from app.services import series_memberships as series_memberships_svc
from app.services.item_write import insert_item

logger = logging.getLogger(__name__)

# Keep individual Komga responses modest. Large 500-item pages plus cover
# traffic made big libraries look as if they had stopped at a page boundary.
PAGE_SIZE = 200
COVER_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
COVER_RETRIES = 1
# Covers must never serialize a large metadata sync behind one slow image.
# Process a small batch concurrently and also cap total wall-clock time per
# thumbnail; httpx's read timeout is an inactivity timeout, not a total one.
COVER_BATCH_SIZE = 8
COVER_WALL_TIMEOUT = 15.0

KOMGA_LIBRARY_MEDIA_TYPES = frozenset({"digital_comic", "digital_manga"})
KOMGA_MEDIA_TYPE_LABELS = {
    "digital_comic": "Digital Comic",
    "digital_manga": "Digital Manga",
}


def get_excluded_libraries() -> set[str]:
    """Komga library IDs the user has opted out of syncing (JSON setting)."""
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'komga_excluded_libraries'"
        ).fetchone()
    if not row or not row["value"]:
        return set()
    try:
        return set(json.loads(row["value"]))
    except (json.JSONDecodeError, TypeError):
        return set()


def get_library_media_types() -> dict[str, str]:
    """Return explicit Komga library -> Shelf digital media type mappings."""
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'komga_library_media_types'"
        ).fetchone()
    if not row or not row["value"]:
        return {}
    try:
        raw = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(library_id): media_type
        for library_id, media_type in raw.items()
        if str(library_id).strip() and media_type in KOMGA_LIBRARY_MEDIA_TYPES
    }


def suggest_library_media_type(name: str | None) -> str:
    """Suggest Manga for clearly named Manga libraries, Comic otherwise."""
    if "manga" in str(name or "").casefold():
        return "digital_manga"
    return "digital_comic"


def library_media_type(
    library_id: str,
    library_name: str | None = None,
    configured: dict[str, str] | None = None,
) -> str:
    """Resolve a library's selected media type, falling back to name detection."""
    mappings = configured if configured is not None else get_library_media_types()
    selected = mappings.get(str(library_id))
    if selected in KOMGA_LIBRARY_MEDIA_TYPES:
        return selected
    return suggest_library_media_type(library_name)


def _detach_automatic_format_links(db, item_id: int) -> None:
    """Remove automatic related-format edges before changing media family."""
    db.execute(
        """DELETE FROM item_links
           WHERE link_type = 'format' AND (item_a_id = ? OR item_b_id = ?)""",
        (item_id, item_id),
    )


def apply_library_media_types(db, mappings: dict[str, str]) -> int:
    """Immediately reclassify existing Komga-linked digital items.

    This is used when the user changes a library mapping in Settings. Automatic
    format links are rebuilt in the new family, while manual ``related`` links
    remain untouched.
    """
    changed = 0
    for library_id, media_type in mappings.items():
        if media_type not in KOMGA_LIBRARY_MEDIA_TYPES:
            continue
        rows = db.execute(
            """SELECT id, media_type FROM items
               WHERE komga_library_id = ?
                 AND media_type IN ('digital_comic', 'digital_manga')
                 AND media_type != ?""",
            (library_id, media_type),
        ).fetchall()
        for row in rows:
            _detach_automatic_format_links(db, row["id"])
            db.execute(
                """UPDATE items SET media_type = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (media_type, row["id"]),
            )
            changed += 1

    if changed:
        from app.services import media_groups

        media_groups.auto_link_family(db, "comic")
        media_groups.auto_link_family(db, "manga")
    return changed


def get_browser_url(komga_url: str, komga_id: str) -> str:
    """Construct the browser-facing Komga book URL.

    ``komga_url`` remains the API/sync endpoint. When ``komga_public_url`` is
    configured, links opened by the user's browser use that root instead.
    """
    with get_db() as db:
        public_url = get_setting(db, "komga_public_url")
    base_url = (public_url or komga_url).rstrip("/")
    return f"{base_url}/book/{komga_id}"


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key, "Accept": "application/json"}


def _authors(metadata: dict) -> str | None:
    names: list[str] = []
    for author in metadata.get("authors") or []:
        if not isinstance(author, dict):
            continue
        name = str(author.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return ", ".join(names) or None


def _publish_year(value) -> int | None:
    if not value:
        return None
    text = str(value).strip()
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def _series_position(metadata: dict) -> float | None:
    value = metadata.get("numberSort")
    if value in (None, ""):
        value = metadata.get("number")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _fetch_library_books(
    client: httpx.AsyncClient,
    komga_url: str,
    api_key: str,
    library_id: str,
) -> list[dict] | None:
    """Fetch every non-deleted Komga book for one library.

    Pagination is guarded against a server returning the same page forever:
    if a page makes no progress we stop that library rather than hanging the
    complete sync.
    """
    page = 0
    books: list[dict] = []
    seen_ids: set[str] = set()
    body = {
        "condition": {
            "allOf": [
                {"libraryId": {"operator": "is", "value": library_id}},
                {"deleted": {"operator": "isFalse"}},
            ]
        }
    }
    while True:
        try:
            response = await client.post(
                f"{komga_url}/api/v1/books/list",
                headers=_headers(api_key),
                params={"page": page, "size": PAGE_SIZE},
                json=body,
            )
        except httpx.TimeoutException:
            logger.warning("Timed out fetching Komga library %s page %s", library_id, page)
            return None
        except httpx.HTTPError:
            logger.warning(
                "Failed fetching Komga library %s page %s", library_id, page,
                exc_info=True,
            )
            return None
        if response.status_code != 200:
            logger.warning(
                "Komga library %s page %s returned HTTP %s",
                library_id,
                page,
                response.status_code,
            )
            return None
        try:
            data = response.json()
        except ValueError:
            logger.warning("Komga library %s returned invalid JSON", library_id)
            return None
        if not isinstance(data, dict):
            logger.warning("Komga library %s returned an invalid page", library_id)
            return None
        content = data.get("content") or []
        if not isinstance(content, list):
            logger.warning("Komga library %s returned an invalid book list", library_id)
            return None

        added_this_page = 0
        for book in content:
            if not isinstance(book, dict):
                continue
            book_id = str(book.get("id") or "").strip()
            if book_id and book_id in seen_ids:
                continue
            if book_id:
                seen_ids.add(book_id)
            books.append(book)
            added_this_page += 1

        if data.get("last") is True:
            break
        total_pages = data.get("totalPages")
        if isinstance(total_pages, int) and page + 1 >= total_pages:
            break
        if not content:
            break
        if added_this_page == 0:
            logger.warning(
                "Komga library %s pagination made no progress at page %s; stopping",
                library_id,
                page,
            )
            break
        page += 1
    return books


async def _download_cover(
    client: httpx.AsyncClient,
    komga_url: str,
    api_key: str,
    komga_id: str,
    item_id: int,
) -> str:
    """Download one Komga thumbnail.

    Returns ``downloaded``, ``missing`` or ``error``. A transient timeout or
    5xx gets one retry, but a bad cover can never hold the entire library sync
    for repeated 30-second waits.
    """
    for attempt in range(COVER_RETRIES + 1):
        try:
            response = await client.get(
                f"{komga_url}/api/v1/books/{komga_id}/thumbnail",
                headers={"X-API-Key": api_key, "Accept": "image/jpeg"},
                timeout=COVER_TIMEOUT,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt < COVER_RETRIES:
                continue
            logger.warning("Komga cover timed out for book %s", komga_id)
            return "error"

        if response.status_code in (404, 204):
            return "missing"
        if response.status_code >= 500 and attempt < COVER_RETRIES:
            continue
        if response.status_code != 200:
            logger.warning(
                "Komga cover for book %s returned HTTP %s",
                komga_id,
                response.status_code,
            )
            return "error"
        if len(response.content) <= 500:
            return "missing"

        try:
            covers.COVERS_DIR.mkdir(parents=True, exist_ok=True)
            cover_dest = covers.COVERS_DIR / f"{item_id}.jpg"
            tmp_dest = covers.COVERS_DIR / f".{item_id}.jpg.tmp"
            tmp_dest.write_bytes(response.content)
            tmp_dest.replace(cover_dest)
            with get_db() as db:
                db.execute(
                    "UPDATE items SET cover_path = ? WHERE id = ?",
                    (f"covers/{item_id}.jpg", item_id),
                )
            return "downloaded"
        except OSError:
            logger.warning(
                "Failed writing Komga cover for book %s", komga_id, exc_info=True
            )
            return "error"

    return "error"


async def _download_cover_with_deadline(
    client: httpx.AsyncClient,
    komga_url: str,
    api_key: str,
    komga_id: str,
    item_id: int,
) -> str:
    """Download a cover with a real wall-clock deadline.

    httpx timeouts reset while bytes continue to arrive, so a server that
    trickles a broken thumbnail can otherwise keep one request alive
    indefinitely.
    """
    try:
        async with asyncio.timeout(COVER_WALL_TIMEOUT):
            return await _download_cover(
                client, komga_url, api_key, komga_id, item_id
            )
    except TimeoutError:
        logger.warning(
            "Komga cover exceeded %.1fs wall-clock deadline for book %s",
            COVER_WALL_TIMEOUT,
            komga_id,
        )
        return "error"


async def _drain_cover_batch(cover_batch: list[tuple], stats: dict) -> None:
    """Fetch one small cover batch concurrently and fold results into stats."""
    if not cover_batch:
        return

    results = await asyncio.gather(
        *(
            _download_cover_with_deadline(*job)
            for job in cover_batch
        ),
        return_exceptions=True,
    )
    cover_batch.clear()

    for result in results:
        if isinstance(result, BaseException):
            logger.warning("Unexpected Komga cover failure: %r", result)
            stats["cover_errors"] += 1
        elif result == "downloaded":
            stats["covers"] += 1
        elif result == "error":
            stats["cover_errors"] += 1


async def sync(komga_url: str, api_key: str, on_progress=None) -> dict:
    """Sync selected Komga libraries into Shelf as digital comics or Manga."""
    stats = {
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "errors": 0,
        "covers": 0,
        "cover_errors": 0,
    }
    excluded = get_excluded_libraries()
    configured_types = get_library_media_types()

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.get(
                f"{komga_url}/api/v1/libraries", headers=_headers(api_key)
            )
        except httpx.TimeoutException:
            return {"error": "Timed out connecting to Komga"}
        except httpx.HTTPError:
            return {"error": "Failed to connect to Komga"}
        if response.status_code != 200:
            return {"error": f"Failed to connect: HTTP {response.status_code}"}

        try:
            raw_libraries = response.json()
        except ValueError:
            return {"error": "Komga returned an invalid library response"}
        if not isinstance(raw_libraries, list):
            return {"error": "Komga returned an invalid library response"}
        libraries = [
            library
            for library in raw_libraries
            if isinstance(library, dict)
            and library.get("id")
            and str(library.get("id")) not in excluded
        ]

        library_books: list[tuple[dict, list[dict]]] = []
        for library in libraries:
            library_id = str(library["id"])
            books = await _fetch_library_books(
                client, komga_url, api_key, library_id
            )
            if books is None:
                stats["errors"] += 1
                continue
            library_books.append((library, books))

        total = sum(len(books) for _, books in library_books)
        current = 0
        cover_batch: list[tuple] = []

        for library, books in library_books:
            library_id = str(library["id"])
            media_type = library_media_type(
                library_id, library.get("name"), configured_types
            )
            media_label = KOMGA_MEDIA_TYPE_LABELS[media_type]
            for book in books:
                current += 1
                komga_id = str(book.get("id") or "").strip()
                metadata = book.get("metadata") or {}
                if not isinstance(metadata, dict):
                    metadata = {}
                title = str(metadata.get("title") or book.get("name") or "").strip()

                if not komga_id or not title:
                    stats["skipped"] += 1
                    if on_progress:
                        reason = "Missing Komga book ID" if not komga_id else "Missing title"
                        await on_progress(
                            current,
                            total,
                            f"{reason} — {title or komga_id or 'unknown book'}",
                            "skipped",
                        )
                    continue

                isbn = str(metadata.get("isbn") or "").strip() or None
                series_name = str(book.get("seriesTitle") or "").strip() or None
                authors = _authors(metadata)
                publish_year = _publish_year(metadata.get("releaseDate"))
                description = str(metadata.get("summary") or "").strip() or None
                series_position = _series_position(metadata)
                media = book.get("media") or {}
                page_count = media.get("pagesCount") if isinstance(media, dict) else None
                if isinstance(page_count, bool) or not isinstance(page_count, int):
                    page_count = None
                series_id = str(book.get("seriesId") or "").strip() or None

                with get_db() as db:
                    existing = db.execute(
                        """SELECT id, title, authors, isbn, series_name, series_position,
                                  publish_year, description, page_count, media_type,
                                  komga_id, komga_library_id, komga_series_id, cover_path,
                                  source
                           FROM items WHERE komga_id = ?""",
                        (komga_id,),
                    ).fetchone()

                    # Early versions of this integration could adopt a physical
                    # comic row on ISBN match. Komga represents a digital copy,
                    # so preserve any physical Comic or Manga and detach it.
                    if (
                        existing
                        and existing["source"] != "komga"
                        and existing["media_type"] in ("comic", "manga")
                    ):
                        db.execute(
                            """UPDATE items SET komga_id = NULL,
                               komga_library_id = NULL, komga_series_id = NULL
                               WHERE id = ?""",
                            (existing["id"],),
                        )
                        existing = None

                    # Shelf allows one ISBN in different formats. Only another
                    # item of this same digital Komga type is a collision or
                    # adoption candidate; physical copies coexist and can link.
                    isbn_match = None
                    if isbn:
                        if existing:
                            isbn_match = db.execute(
                                """SELECT id, komga_id FROM items
                                   WHERE isbn = ? AND media_type = ? AND id != ?
                                   ORDER BY id LIMIT 1""",
                                (isbn, media_type, existing["id"]),
                            ).fetchone()
                        else:
                            isbn_match = db.execute(
                                """SELECT id, komga_id FROM items
                                   WHERE isbn = ? AND media_type = ?
                                   ORDER BY id LIMIT 1""",
                                (isbn, media_type),
                            ).fetchone()

                    if isbn_match:
                        if existing or isbn_match["komga_id"]:
                            logger.warning(
                                "Skipping Komga book %s (%s): ISBN %s already belongs "
                                "to %s item %s",
                                komga_id,
                                title,
                                isbn,
                                media_label,
                                isbn_match["id"],
                            )
                            stats["skipped"] += 1
                            if on_progress:
                                await on_progress(
                                    current,
                                    total,
                                    f"{media_label} ISBN conflict with Shelf item "
                                    f"{isbn_match['id']} — {title} ({isbn})",
                                    "skipped",
                                )
                            continue
                        existing = db.execute(
                            """SELECT id, title, authors, isbn, series_name,
                                      series_position, publish_year, description,
                                      page_count, media_type, komga_id,
                                      komga_library_id, komga_series_id,
                                      cover_path, source
                               FROM items WHERE id = ?""",
                            (isbn_match["id"],),
                        ).fetchone()

                    desired = {
                        "title": title,
                        "authors": authors,
                        "isbn": isbn,
                        "series_name": series_name,
                        "series_position": series_position,
                        "publish_year": publish_year,
                        "description": description,
                        "page_count": page_count,
                        "media_type": media_type,
                        "komga_id": komga_id,
                        "komga_library_id": library_id,
                        "komga_series_id": series_id,
                    }

                    if existing:
                        changed = any(
                            existing[key] != value for key, value in desired.items()
                        )
                        if changed:
                            if existing["media_type"] != media_type:
                                _detach_automatic_format_links(db, existing["id"])
                            db.execute(
                                """UPDATE items SET title=?, authors=?, isbn=?,
                                   series_name=?, series_position=?, publish_year=?,
                                   description=?, page_count=?, media_type=?,
                                   komga_id=?, komga_library_id=?, komga_series_id=?,
                                   updated_at=datetime('now') WHERE id=?""",
                                (
                                    title,
                                    authors,
                                    isbn,
                                    series_name,
                                    series_position,
                                    publish_year,
                                    description,
                                    page_count,
                                    media_type,
                                    komga_id,
                                    library_id,
                                    series_id,
                                    existing["id"],
                                ),
                            )
                            stats["updated"] += 1
                            status = "updated"
                        else:
                            stats["unchanged"] += 1
                            status = "unchanged"
                        item_id = existing["id"]
                        # Do not overwrite a user-selected cover. Missing
                        # covers are retried on every sync until Komga supplies
                        # one, so interrupted runs self-heal.
                        fetch_cover = not existing["cover_path"]
                    else:
                        item_id = insert_item(
                            db,
                            title=title,
                            authors=authors,
                            isbn=isbn,
                            media_type=media_type,
                            publish_year=publish_year,
                            description=description,
                            series_name=series_name,
                            series_position=series_position,
                            page_count=page_count,
                            komga_id=komga_id,
                            komga_library_id=library_id,
                            komga_series_id=series_id,
                            source="komga",
                        )
                        stats["added"] += 1
                        status = "added"
                        fetch_cover = True

                    series_memberships_svc.add_metadata_memberships(
                        db,
                        item_id,
                        [{"name": series_name, "position": series_position}] if series_name else [],
                    )

                # Metadata progress is independent of cover I/O. Queue the cover
                # first, report this item immediately, then periodically drain a
                # small concurrent batch. One pathological thumbnail can pause
                # the bar for at most COVER_WALL_TIMEOUT, not indefinitely.
                if fetch_cover:
                    cover_batch.append(
                        (client, komga_url, api_key, komga_id, item_id)
                    )

                if on_progress:
                    await on_progress(current, total, title, status)

                if len(cover_batch) >= COVER_BATCH_SIZE:
                    await _drain_cover_batch(cover_batch, stats)

        # Finish the final partial batch while the shared HTTP client is open.
        await _drain_cover_batch(cover_batch, stats)

    _auto_link_items()
    return stats


def _normalize_title(title: str) -> str:
    import re

    value = title.lower().strip()
    value = re.sub(r"^(the|a|an)\s+", "", value)
    value = re.sub(r"\s*[:—–-]\s.*$", "", value)
    value = re.sub(r"[^a-z0-9\s]", "", value)
    return value.strip()


def _authors_compatible(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return True
    first_a = a.split(",")[0].strip().casefold()
    first_b = b.split(",")[0].strip().casefold()
    return bool(first_a) and bool(first_b) and (
        first_a in b.casefold() or first_b in a.casefold()
    )


def _auto_link_items():
    """Group physical/digital Comic and Manga representations of a work."""
    from app.services import media_groups

    with get_db() as db:
        media_groups.auto_link_family(db, "comic")
        media_groups.auto_link_family(db, "manga")
