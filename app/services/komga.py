import json
import logging

import httpx

from app.database import get_db, get_setting
from app.services import covers
from app.services.item_write import insert_item

logger = logging.getLogger(__name__)

PAGE_SIZE = 500


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
    """Fetch every non-deleted Komga book for one library."""
    page = 0
    books: list[dict] = []
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
            logger.warning("Timed out fetching Komga library %s", library_id)
            return None
        if response.status_code != 200:
            logger.warning(
                "Komga library %s returned HTTP %s", library_id, response.status_code
            )
            return None
        data = response.json()
        if not isinstance(data, dict):
            logger.warning("Komga library %s returned an invalid page", library_id)
            return None
        content = data.get("content") or []
        if not isinstance(content, list):
            logger.warning("Komga library %s returned an invalid book list", library_id)
            return None
        books.extend(book for book in content if isinstance(book, dict))
        if data.get("last") is True:
            break
        total_pages = data.get("totalPages")
        if isinstance(total_pages, int) and page + 1 >= total_pages:
            break
        if not content:
            break
        page += 1
    return books


async def sync(komga_url: str, api_key: str, on_progress=None) -> dict:
    """Sync comics/graphic novels from Komga into Shelf."""
    stats = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    excluded = get_excluded_libraries()

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.get(
                f"{komga_url}/api/v1/libraries", headers=_headers(api_key)
            )
        except httpx.TimeoutException:
            return {"error": "Timed out connecting to Komga"}
        if response.status_code != 200:
            return {"error": f"Failed to connect: HTTP {response.status_code}"}

        raw_libraries = response.json()
        if not isinstance(raw_libraries, list):
            return {"error": "Komga returned an invalid library response"}
        libraries = [
            library for library in raw_libraries
            if isinstance(library, dict)
            and library.get("id")
            and library.get("id") not in excluded
        ]

        library_books: list[tuple[dict, list[dict]]] = []
        for library in libraries:
            books = await _fetch_library_books(
                client, komga_url, api_key, library["id"]
            )
            if books is None:
                stats["errors"] += 1
                continue
            library_books.append((library, books))

        total = sum(len(books) for _, books in library_books)
        current = 0

        for library, books in library_books:
            library_id = library["id"]
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
                                  komga_id, komga_library_id, komga_series_id, cover_path
                           FROM items WHERE komga_id = ?""",
                        (komga_id,),
                    ).fetchone()

                    isbn_match = None
                    if isbn:
                        if existing:
                            isbn_match = db.execute(
                                """SELECT id, komga_id FROM items
                                   WHERE isbn = ? AND media_type = 'comic' AND id != ?
                                   ORDER BY id LIMIT 1""",
                                (isbn, existing["id"]),
                            ).fetchone()
                        else:
                            isbn_match = db.execute(
                                """SELECT id, komga_id FROM items
                                   WHERE isbn = ? AND media_type = 'comic'
                                   ORDER BY id LIMIT 1""",
                                (isbn,),
                            ).fetchone()

                    if isbn_match:
                        if existing or isbn_match["komga_id"]:
                            logger.warning(
                                "Skipping Komga book %s (%s): ISBN %s already belongs "
                                "to Shelf item %s",
                                komga_id,
                                title,
                                isbn,
                                isbn_match["id"],
                            )
                            stats["skipped"] += 1
                            if on_progress:
                                await on_progress(
                                    current,
                                    total,
                                    f"ISBN conflict with Shelf item {isbn_match['id']} — "
                                    f"{title} ({isbn})",
                                    "skipped",
                                )
                            continue
                        existing = db.execute(
                            """SELECT id, title, authors, isbn, series_name, series_position,
                                      publish_year, description, page_count, media_type,
                                      komga_id, komga_library_id, komga_series_id, cover_path
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
                        "media_type": "comic",
                        "komga_id": komga_id,
                        "komga_library_id": library_id,
                        "komga_series_id": series_id,
                    }

                    if existing:
                        changed = any(
                            existing[key] != value for key, value in desired.items()
                        )
                        if changed:
                            db.execute(
                                """UPDATE items SET title=?, authors=?, isbn=?,
                                   series_name=?, series_position=?, publish_year=?,
                                   description=?, page_count=?, media_type='comic',
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
                        fetch_cover = changed or not existing["cover_path"]
                        if on_progress:
                            await on_progress(current, total, title, status)
                    else:
                        item_id = insert_item(
                            db,
                            title=title,
                            authors=authors,
                            isbn=isbn,
                            media_type="comic",
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
                        fetch_cover = True
                        if on_progress:
                            await on_progress(current, total, title, "added")

                if not fetch_cover:
                    continue
                try:
                    cover_response = await client.get(
                        f"{komga_url}/api/v1/books/{komga_id}/thumbnail",
                        headers=_headers(api_key),
                    )
                    if (
                        cover_response.status_code == 200
                        and len(cover_response.content) > 500
                    ):
                        cover_dest = covers.COVERS_DIR / f"{item_id}.jpg"
                        covers.COVERS_DIR.mkdir(parents=True, exist_ok=True)
                        cover_dest.write_bytes(cover_response.content)
                        with get_db() as db:
                            db.execute(
                                "UPDATE items SET cover_path = ? WHERE id = ?",
                                (f"covers/{item_id}.jpg", item_id),
                            )
                except Exception:
                    logger.debug(
                        "Failed to download cover for Komga book %s",
                        komga_id,
                        exc_info=True,
                    )

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
    """Link Komga items to matching other-format Shelf items."""
    with get_db() as db:
        komga_items = db.execute(
            """SELECT id, title, authors, isbn, media_type
               FROM items WHERE komga_id IS NOT NULL"""
        ).fetchall()
        for komga_item in komga_items:
            if komga_item["isbn"]:
                matches = db.execute(
                    """SELECT id FROM items
                       WHERE isbn = ? AND id != ? AND media_type != ?""",
                    (
                        komga_item["isbn"],
                        komga_item["id"],
                        komga_item["media_type"],
                    ),
                ).fetchall()
            else:
                matches = []

            if not matches:
                normalized = _normalize_title(komga_item["title"])
                candidates = db.execute(
                    """SELECT id, title, authors, media_type FROM items
                       WHERE id != ? AND komga_id IS NULL""",
                    (komga_item["id"],),
                ).fetchall()
                matches = [
                    candidate
                    for candidate in candidates
                    if candidate["media_type"] != komga_item["media_type"]
                    and _normalize_title(candidate["title"]) == normalized
                    and _authors_compatible(
                        komga_item["authors"], candidate["authors"]
                    )
                ]

            for match in matches:
                match_id = match["id"] if hasattr(match, "keys") else match[0]
                a_id = min(komga_item["id"], match_id)
                b_id = max(komga_item["id"], match_id)
                db.execute(
                    """INSERT OR IGNORE INTO item_links (item_a_id, item_b_id)
                       VALUES (?, ?)""",
                    (a_id, b_id),
                )
