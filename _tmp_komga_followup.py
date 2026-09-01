from pathlib import Path


def patch(path, old, new):
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


# New media type: Komga is a digital library, so keep its copies distinct
# from physical Comic / Graphic Novel items.
patch(
    "app/config.py",
    '    "comic": "Comic / Graphic Novel",\n',
    '    "comic": "Comic / Graphic Novel",\n    "digital_comic": "Digital Comic",\n',
)

# Komga library API should expose the actual Shelf media type it creates.
patch(
    "app/routers/komga.py",
    '            "media_type": "comic",\n',
    '            "media_type": "digital_comic",\n',
)

# Replace the Komga service with the hardened implementation.
Path("app/services/komga.py").write_text(r'''import json
import logging

import httpx

from app.database import get_db, get_setting
from app.services import covers
from app.services.item_write import insert_item

logger = logging.getLogger(__name__)

# Keep individual Komga responses modest. Large 500-item pages plus cover
# traffic made big libraries look as if they had stopped at a page boundary.
PAGE_SIZE = 200
COVER_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
COVER_RETRIES = 1


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
                headers=_headers(api_key),
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


async def sync(komga_url: str, api_key: str, on_progress=None) -> dict:
    """Sync digital comics from Komga into Shelf."""
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
                                  komga_id, komga_library_id, komga_series_id, cover_path,
                                  source
                           FROM items WHERE komga_id = ?""",
                        (komga_id,),
                    ).fetchone()

                    # Early versions of this integration adopted a physical
                    # `comic` row on ISBN match. Komga is a digital copy, so
                    # repair that state in-place: preserve the physical item,
                    # detach its Komga IDs, and create/link a Digital Comic.
                    if (
                        existing
                        and existing["source"] != "komga"
                        and existing["media_type"] == "comic"
                    ):
                        db.execute(
                            """UPDATE items SET komga_id = NULL,
                               komga_library_id = NULL, komga_series_id = NULL
                               WHERE id = ?""",
                            (existing["id"],),
                        )
                        existing = None

                    # Shelf allows one ISBN in different formats. Only another
                    # Digital Comic is a collision/adoption candidate; a
                    # physical Comic / Graphic Novel should coexist and link.
                    isbn_match = None
                    if isbn:
                        if existing:
                            isbn_match = db.execute(
                                """SELECT id, komga_id FROM items
                                   WHERE isbn = ? AND media_type = 'digital_comic'
                                     AND id != ?
                                   ORDER BY id LIMIT 1""",
                                (isbn, existing["id"]),
                            ).fetchone()
                        else:
                            isbn_match = db.execute(
                                """SELECT id, komga_id FROM items
                                   WHERE isbn = ? AND media_type = 'digital_comic'
                                   ORDER BY id LIMIT 1""",
                                (isbn,),
                            ).fetchone()

                    if isbn_match:
                        if existing or isbn_match["komga_id"]:
                            logger.warning(
                                "Skipping Komga book %s (%s): ISBN %s already belongs "
                                "to Digital Comic item %s",
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
                                    f"Digital Comic ISBN conflict with Shelf item "
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
                        "media_type": "digital_comic",
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
                                   description=?, page_count=?, media_type='digital_comic',
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
                            media_type="digital_comic",
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

                # Cover work is part of the item before progress advances. A
                # single bad thumbnail has a bounded retry/timeout instead of
                # making the progress bar look frozen for minutes.
                if fetch_cover:
                    cover_status = await _download_cover(
                        client, komga_url, api_key, komga_id, item_id
                    )
                    if cover_status == "downloaded":
                        stats["covers"] += 1
                    elif cover_status == "error":
                        stats["cover_errors"] += 1

                if on_progress:
                    await on_progress(current, total, title, status)

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
    """Link Komga Digital Comics to matching other-format Shelf items.

    Build candidate indexes once. The original implementation queried and
    normalized the entire Shelf collection once per Komga item, which became
    quadratic and could look like a sync stall after the progress bar reached
    a few hundred items.
    """
    with get_db() as db:
        komga_items = db.execute(
            """SELECT id, title, authors, isbn, media_type
               FROM items WHERE komga_id IS NOT NULL"""
        ).fetchall()
        candidates = db.execute(
            """SELECT id, title, authors, isbn, media_type
               FROM items WHERE komga_id IS NULL"""
        ).fetchall()

        by_isbn: dict[str, list] = {}
        by_title: dict[str, list] = {}
        for candidate in candidates:
            if candidate["isbn"]:
                by_isbn.setdefault(candidate["isbn"], []).append(candidate)
            normalized = _normalize_title(candidate["title"])
            if normalized:
                by_title.setdefault(normalized, []).append(candidate)

        for komga_item in komga_items:
            matches = []
            if komga_item["isbn"]:
                matches = [
                    candidate
                    for candidate in by_isbn.get(komga_item["isbn"], [])
                    if candidate["media_type"] != komga_item["media_type"]
                ]

            if not matches:
                normalized = _normalize_title(komga_item["title"])
                matches = [
                    candidate
                    for candidate in by_title.get(normalized, [])
                    if candidate["media_type"] != komga_item["media_type"]
                    and _authors_compatible(
                        komga_item["authors"], candidate["authors"]
                    )
                ]

            for match in matches:
                a_id = min(komga_item["id"], match["id"])
                b_id = max(komga_item["id"], match["id"])
                db.execute(
                    """INSERT OR IGNORE INTO item_links (item_a_id, item_b_id)
                       VALUES (?, ?)""",
                    (a_id, b_id),
                )
''')

# Settings: put public/browser roots beside the internal/API connection rather
# than leaving them at the very bottom of the whole Integrations tab.
Path("app/templates/fragments/settings/abs_public_url.html").write_text(r'''<div class="mt-4 pt-4 border-t border-shelf-border">
    <h3 class="text-sm font-semibold text-shelf-text mb-1">Browser / Public URL</h3>
    <p class="text-xs text-shelf-muted mb-3">
        Optional address opened by your browser for Listen/Read links. Use your HTTPS reverse-proxy
        domain here when Shelf talks to Audiobookshelf through an internal Docker or LAN address.
        Leave blank to use the Internal / API URL.
    </p>

    {% if request.query_params.get('abs_public_url_error') == 'invalid' %}
    <div data-testid="abs-public-url-error"
         class="bg-shelf-bg text-shelf-error border border-shelf-border rounded-lg px-3 py-2 text-sm mb-3">
        Browser / Public URL must be a valid http:// or https:// URL.
    </div>
    {% endif %}

    <form action="/api/sync/audiobookshelf/public-url" method="post" class="space-y-3">
        <div>
            <label for="abs_public_url" class="block text-sm font-medium text-shelf-muted mb-1">Audiobookshelf Browser / Public URL</label>
            <input type="url" id="abs_public_url" name="abs_public_url"
                   value="{{ settings.get('abs_public_url', '') }}"
                   placeholder="https://audiobooks.example.com"
                   class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">
            <p class="text-xs text-shelf-muted mt-1">This never changes the address Shelf uses for sync or API traffic.</p>
        </div>
        <button type="submit" class="px-4 py-2 bg-shelf-hover text-shelf-text rounded-lg text-sm hover:bg-shelf-border transition-colors">
            Save Public URL
        </button>
    </form>
</div>
''')

Path("app/templates/fragments/settings/komga_public_url.html").write_text(r'''<div class="mt-4 pt-4 border-t border-shelf-border">
    <h3 class="text-sm font-semibold text-shelf-text mb-1">Browser / Public URL</h3>
    <p class="text-xs text-shelf-muted mb-3">
        Optional address opened by your browser for Open in Komga links. Use your HTTPS reverse-proxy
        domain here when Shelf talks to Komga through an internal Docker or LAN address.
        Leave blank to use the Internal / API URL.
    </p>

    {% if request.query_params.get('komga_public_url_error') == 'invalid' %}
    <div data-testid="komga-public-url-error"
         class="bg-shelf-bg text-shelf-error border border-shelf-border rounded-lg px-3 py-2 text-sm mb-3">
        Browser / Public URL must be a valid http:// or https:// URL.
    </div>
    {% endif %}

    <form action="/api/sync/komga/public-url" method="post" class="space-y-3">
        <div>
            <label for="komga_public_url" class="block text-sm font-medium text-shelf-muted mb-1">Komga Browser / Public URL</label>
            <input type="url" id="komga_public_url" name="komga_public_url"
                   value="{{ settings.get('komga_public_url', '') }}"
                   placeholder="https://comics.example.com"
                   class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">
            <p class="text-xs text-shelf-muted mt-1">This never changes the address Shelf uses for sync or API traffic.</p>
        </div>
        <button type="submit" class="px-4 py-2 bg-shelf-hover text-shelf-text rounded-lg text-sm hover:bg-shelf-border transition-colors">
            Save Public URL
        </button>
    </form>
</div>
''')

# Remove the old far-away standalone cards from settings.html.
patch(
    "app/templates/settings.html",
    '    {% include "fragments/settings/integrations.html" %}\n\n    {% include "fragments/settings/abs_public_url.html" %}\n\n    {% include "fragments/settings/komga_public_url.html" %}\n',
    '    {% include "fragments/settings/integrations.html" %}\n',
)

# Put ABS public URL inside the ABS card and make the server URL purpose clear.
patch(
    "app/templates/fragments/settings/integrations.html",
    '<label for="abs_url" class="block text-sm font-medium text-shelf-muted mb-1">Audiobookshelf URL</label>',
    '<label for="abs_url" class="block text-sm font-medium text-shelf-muted mb-1">Audiobookshelf Internal / API URL</label>',
)
patch(
    "app/templates/fragments/settings/integrations.html",
    '                <input type="url" id="abs_url" name="abs_url" x-model="absUrl" value="{{ settings.get(\'abs_url\', \'\') }}" placeholder="http://localhost:13378"\n                       class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">\n            </div>',
    '                <input type="url" id="abs_url" name="abs_url" x-model="absUrl" value="{{ settings.get(\'abs_url\', \'\') }}" placeholder="http://audiobookshelf:80"\n                       class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">\n                <p class="text-xs text-shelf-muted mt-1">Server-side address Shelf uses for API and sync traffic. Docker/LAN hostnames are fine here.</p>\n            </div>',
)
patch(
    "app/templates/fragments/settings/integrations.html",
    '        </form>\n\n        <div class="border-t border-shelf-border pt-4">\n            <button @click="startSync()"',
    '        </form>\n\n        {% include "fragments/settings/abs_public_url.html" %}\n\n        <div class="border-t border-shelf-border pt-4 mt-4">\n            <button @click="startSync()"',
)

# Komga card: clarify URL roles, put public URL inline, improve result detail,
# and make transient x-show labels cloaked until Alpine initialises.
patch(
    "app/templates/fragments/settings/komga.html",
    '<label for="komga_url" class="block text-sm font-medium text-shelf-muted mb-1">Komga URL</label>',
    '<label for="komga_url" class="block text-sm font-medium text-shelf-muted mb-1">Komga Internal / API URL</label>',
)
patch(
    "app/templates/fragments/settings/komga.html",
    '                       value="{{ settings.get(\'komga_url\', \'\') }}" placeholder="http://komga:25600"\n                       class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">\n            </div>',
    '                       value="{{ settings.get(\'komga_url\', \'\') }}" placeholder="http://komga:25600"\n                       class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">\n                <p class="text-xs text-shelf-muted mt-1">Server-side address Shelf uses for API and sync traffic. Docker/LAN hostnames are fine here.</p>\n            </div>',
)
patch(
    "app/templates/fragments/settings/komga.html",
    '        </form>\n\n        <div class="border-t border-shelf-border pt-4">\n            <button @click="startSync"',
    '        </form>\n\n        {% include "fragments/settings/komga_public_url.html" %}\n\n        <div class="border-t border-shelf-border pt-4 mt-4">\n            <button @click="startSync"',
)
patch(
    "app/templates/fragments/settings/komga.html",
    '<p>Sync imports books from selected Komga libraries as Comic / Graphic Novel items, including issue metadata and cover art. Matching manually catalogued comics are adopted by ISBN instead of duplicated.</p>',
    '<p>Sync imports books from selected Komga libraries as Digital Comic items, including issue metadata and Komga cover art. Physical comics remain separate and are linked to their digital copy by ISBN or title.</p>',
)
patch(
    "app/templates/fragments/settings/komga.html",
    '<span class="text-xs text-shelf-muted">(Comic / Graphic Novel)</span>',
    '<span class="text-xs text-shelf-muted">(Digital Comic)</span>',
)
patch(
    "app/templates/fragments/settings/komga.html",
    '                        Skipped: <span x-text="result.skipped"></span>\n                    </p>',
    '                        Skipped: <span x-text="result.skipped"></span>\n                    </p>\n                    <p class="text-xs text-shelf-muted mt-1">\n                        Covers imported: <span x-text="result.covers"></span> ·\n                        Cover errors: <span x-text="result.cover_errors"></span>\n                    </p>',
)
# Hide only transient alternate states until Alpine has removed x-cloak.
for old, new in [
    ('<span x-show="testing">Testing...</span>', '<span x-show="testing" x-cloak>Testing...</span>'),
    ('<span x-show="syncing">Syncing...</span>', '<span x-show="syncing" x-cloak>Syncing...</span>'),
    ('<span x-show="libsLoading">Loading&hellip;</span>', '<span x-show="libsLoading" x-cloak>Loading&hellip;</span>'),
    ('<span x-show="libsSaving">Saving&hellip;</span>', '<span x-show="libsSaving" x-cloak>Saving&hellip;</span>'),
    ('<span x-show="cleaning">Removing&hellip;</span>', '<span x-show="cleaning" x-cloak>Removing&hellip;</span>'),
]:
    patch("app/templates/fragments/settings/komga.html", old, new)

# Rewrite focused Komga sync regressions for digital-copy semantics, covers,
# pagination and timeout resilience.
Path("tests/test_komga_sync.py").write_text(r'''"""Komga sync, library selection, mapping, cover and scale regressions."""
import asyncio
import json

import httpx
import respx

from app.services import covers
from app.services.komga import PAGE_SIZE, get_excluded_libraries, sync
from tests.conftest import _insert_item

KOMGA = "http://komga.example:25600"
KEY = "test-api-key"
ISBN = "9780000000998"


def _set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.execute("COMMIT")


def _libraries():
    return httpx.Response(
        200,
        json=[
            {"id": "lib_comics", "name": "Comics"},
            {"id": "lib_manga", "name": "Manga"},
        ],
    )


def _book(book_id="book_1", title="Watchmen", isbn=ISBN, library_id="lib_comics"):
    return {
        "id": book_id,
        "libraryId": library_id,
        "seriesId": "series_1",
        "seriesTitle": "Watchmen",
        "media": {"pagesCount": 416, "mediaType": "application/zip", "mediaProfile": "DIVINA"},
        "metadata": {
            "title": title,
            "authors": [
                {"name": "Alan Moore", "role": "writer"},
                {"name": "Dave Gibbons", "role": "penciller"},
            ],
            "isbn": isbn,
            "number": "1",
            "numberSort": 1.0,
            "releaseDate": "1987-09-01",
            "summary": "A landmark graphic novel.",
        },
    }


def _books_page(*books, last=True, total_pages=1, number=0):
    return httpx.Response(
        200,
        json={
            "content": list(books),
            "last": last,
            "totalPages": total_pages,
            "number": number,
        },
    )


def _mock_single_library(*books):
    respx.get(f"{KOMGA}/api/v1/libraries").mock(
        return_value=httpx.Response(200, json=[{"id": "lib_comics", "name": "Comics"}])
    )
    respx.post(f"{KOMGA}/api/v1/books/list").mock(return_value=_books_page(*books))


class TestKomgaSettings:
    def test_excluded_libraries_default_and_json(self, db):
        assert get_excluded_libraries() == set()
        _set_setting(db, "komga_excluded_libraries", json.dumps(["lib_manga"]))
        assert get_excluded_libraries() == {"lib_manga"}

    def test_garbage_excluded_setting_is_safe(self, db):
        _set_setting(db, "komga_excluded_libraries", "not-json")
        assert get_excluded_libraries() == set()


class TestKomgaSync:
    @respx.mock
    def test_maps_komga_book_to_digital_comic_metadata(self, db):
        _mock_single_library(_book())
        cover = respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            return_value=httpx.Response(404)
        )

        stats = asyncio.run(sync(KOMGA, KEY))

        assert stats == {
            "added": 1,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "errors": 0,
            "covers": 0,
            "cover_errors": 0,
        }
        row = db.execute(
            """SELECT title, authors, isbn, media_type, series_name,
                      series_position, publish_year, page_count, description,
                      komga_id, komga_library_id, komga_series_id, source
               FROM items"""
        ).fetchone()
        assert row["title"] == "Watchmen"
        assert row["authors"] == "Alan Moore, Dave Gibbons"
        assert row["isbn"] == ISBN
        assert row["media_type"] == "digital_comic"
        assert row["series_name"] == "Watchmen"
        assert row["series_position"] == 1.0
        assert row["publish_year"] == 1987
        assert row["page_count"] == 416
        assert row["description"] == "A landmark graphic novel."
        assert row["komga_id"] == "book_1"
        assert row["komga_library_id"] == "lib_comics"
        assert row["komga_series_id"] == "series_1"
        assert row["source"] == "komga"
        assert cover.calls[0].request.headers["X-API-Key"] == KEY

        listing = respx.calls[1].request
        assert listing.headers["X-API-Key"] == KEY
        assert listing.url.params["size"] == str(PAGE_SIZE)
        payload = json.loads(listing.content)
        conditions = payload["condition"]["allOf"]
        assert {"libraryId": {"operator": "is", "value": "lib_comics"}} in conditions
        assert {"deleted": {"operator": "isFalse"}} in conditions

    @respx.mock
    def test_imports_komga_thumbnail_as_cover(self, db):
        _mock_single_library(_book())
        image = b"\xff\xd8" + (b"cover-data" * 150)
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            return_value=httpx.Response(200, content=image, headers={"Content-Type": "image/jpeg"})
        )

        stats = asyncio.run(sync(KOMGA, KEY))

        row = db.execute("SELECT id, cover_path FROM items").fetchone()
        assert stats["covers"] == 1
        assert stats["cover_errors"] == 0
        assert row["cover_path"] == f"covers/{row['id']}.jpg"
        assert (covers.COVERS_DIR / f"{row['id']}.jpg").read_bytes() == image

    @respx.mock
    def test_missing_cover_is_retried_on_later_sync(self, db):
        _mock_single_library(_book())
        route = respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            side_effect=[
                httpx.Response(404),
                httpx.Response(200, content=b"x" * 1200),
            ]
        )
        first = asyncio.run(sync(KOMGA, KEY))
        second = asyncio.run(sync(KOMGA, KEY))
        assert first["covers"] == 0
        assert second["covers"] == 1
        assert route.call_count == 2
        assert db.execute("SELECT cover_path FROM items").fetchone()["cover_path"]

    @respx.mock
    def test_cover_timeout_does_not_abort_remaining_books(self, db):
        _mock_single_library(
            _book("book_1", "One", "9780000000103"),
            _book("book_2", "Two", "9780000000110"),
        )
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            side_effect=httpx.ReadTimeout("slow cover")
        )
        respx.get(f"{KOMGA}/api/v1/books/book_2/thumbnail").mock(
            return_value=httpx.Response(200, content=b"y" * 1200)
        )

        stats = asyncio.run(sync(KOMGA, KEY))
        assert stats["added"] == 2
        assert stats["cover_errors"] == 1
        assert stats["covers"] == 1
        rows = db.execute("SELECT title, cover_path FROM items ORDER BY title").fetchall()
        assert rows[0]["cover_path"] is None
        assert rows[1]["cover_path"] is not None

    @respx.mock
    def test_paginates_large_library_without_repeating_page(self, db):
        respx.get(f"{KOMGA}/api/v1/libraries").mock(
            return_value=httpx.Response(200, json=[{"id": "lib_comics", "name": "Comics"}])
        )
        listing = respx.post(f"{KOMGA}/api/v1/books/list").mock(
            side_effect=[
                _books_page(_book("book_1", "One", None), last=False, total_pages=2, number=0),
                _books_page(_book("book_2", "Two", None), last=True, total_pages=2, number=1),
            ]
        )
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(return_value=httpx.Response(404))
        respx.get(f"{KOMGA}/api/v1/books/book_2/thumbnail").mock(return_value=httpx.Response(404))

        stats = asyncio.run(sync(KOMGA, KEY))
        assert stats["added"] == 2
        assert listing.call_count == 2
        assert listing.calls[0].request.url.params["page"] == "0"
        assert listing.calls[1].request.url.params["page"] == "1"

    @respx.mock
    def test_excluded_library_is_not_listed_for_sync(self, db):
        _set_setting(db, "komga_excluded_libraries", json.dumps(["lib_manga"]))
        respx.get(f"{KOMGA}/api/v1/libraries").mock(return_value=_libraries())
        list_route = respx.post(f"{KOMGA}/api/v1/books/list").mock(
            return_value=_books_page(_book())
        )
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            return_value=httpx.Response(404)
        )

        stats = asyncio.run(sync(KOMGA, KEY))

        assert stats["added"] == 1
        assert list_route.call_count == 1
        assert b"lib_comics" in list_route.calls[0].request.content
        assert b"lib_manga" not in list_route.calls[0].request.content

    @respx.mock
    def test_physical_comic_and_komga_copy_coexist_and_link(self, db):
        physical_id = _insert_item(
            db,
            title="Physical Watchmen",
            isbn=ISBN,
            media_type="comic",
            source="manual",
        )
        db.execute("COMMIT")
        _mock_single_library(_book())
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(return_value=httpx.Response(404))

        first = asyncio.run(sync(KOMGA, KEY))
        assert first["added"] == 1
        rows = db.execute(
            "SELECT id, media_type, komga_id, source FROM items ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["id"] == physical_id
        assert rows[0]["media_type"] == "comic"
        assert rows[0]["komga_id"] is None
        assert rows[0]["source"] == "manual"
        assert rows[1]["media_type"] == "digital_comic"
        assert rows[1]["komga_id"] == "book_1"
        assert rows[1]["source"] == "komga"
        link = db.execute(
            "SELECT item_a_id, item_b_id FROM item_links"
        ).fetchone()
        assert {link["item_a_id"], link["item_b_id"]} == {physical_id, rows[1]["id"]}

        db.execute(
            "UPDATE items SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
            (rows[1]["id"],),
        )
        db.execute("COMMIT")
        second = asyncio.run(sync(KOMGA, KEY))
        assert second["unchanged"] == 1
        assert db.execute(
            "SELECT updated_at FROM items WHERE id = ?", (rows[1]["id"],)
        ).fetchone()["updated_at"] == "2000-01-01 00:00:00"

    @respx.mock
    def test_repairs_legacy_physical_item_that_was_adopted(self, db):
        physical_id = _insert_item(
            db,
            title="Old Adopted Watchmen",
            isbn=ISBN,
            media_type="comic",
            source="manual",
            komga_id="book_1",
            komga_library_id="lib_comics",
        )
        db.execute("COMMIT")
        _mock_single_library(_book())
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(return_value=httpx.Response(404))

        stats = asyncio.run(sync(KOMGA, KEY))
        assert stats["added"] == 1
        physical = db.execute(
            "SELECT media_type, komga_id, source FROM items WHERE id = ?", (physical_id,)
        ).fetchone()
        assert physical["media_type"] == "comic"
        assert physical["komga_id"] is None
        assert physical["source"] == "manual"
        digital = db.execute(
            "SELECT media_type, komga_id, source FROM items WHERE id != ?", (physical_id,)
        ).fetchone()
        assert digital["media_type"] == "digital_comic"
        assert digital["komga_id"] == "book_1"
        assert digital["source"] == "komga"

    @respx.mock
    def test_duplicate_komga_digital_isbn_is_skipped(self, db):
        _mock_single_library(
            _book("book_1", "First", ISBN),
            _book("book_2", "Duplicate", ISBN),
        )
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            return_value=httpx.Response(404)
        )

        stats = asyncio.run(sync(KOMGA, KEY))
        assert stats["added"] == 1
        assert stats["skipped"] == 1
        assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 1


class TestKomgaLibraryEndpoints:
    @respx.mock
    def test_list_libraries_uses_saved_api_key(self, admin_client, db):
        _set_setting(db, "komga_url", KOMGA)
        _set_setting(db, "komga_api_key", KEY)
        _set_setting(db, "komga_excluded_libraries", json.dumps(["lib_manga"]))
        route = respx.get(f"{KOMGA}/api/v1/libraries").mock(return_value=_libraries())

        data = admin_client.get("/api/sync/komga/libraries").json()
        assert data["ok"] is True
        by_id = {library["id"]: library for library in data["libraries"]}
        assert by_id["lib_comics"]["included"] is True
        assert by_id["lib_comics"]["media_type"] == "digital_comic"
        assert by_id["lib_manga"]["included"] is False
        assert route.calls[0].request.headers["X-API-Key"] == KEY

    def test_save_library_selection_validates_shape(self, admin_client):
        ok = admin_client.post(
            "/api/sync/komga/libraries", json={"excluded": ["lib_manga"]}
        )
        assert ok.json()["ok"] is True
        bad = admin_client.post(
            "/api/sync/komga/libraries", json={"excluded": [123]}
        )
        assert bad.json()["ok"] is False

    def test_cleanup_deletes_synced_but_detaches_adopted_items(self, admin_client, db):
        _set_setting(db, "komga_excluded_libraries", json.dumps(["lib_manga"]))
        synced = _insert_item(
            db,
            title="Synced",
            isbn=None,
            media_type="digital_comic",
            komga_id="k1",
            komga_library_id="lib_manga",
            source="komga",
        )
        adopted = _insert_item(
            db,
            title="Manual Digital",
            isbn=None,
            media_type="digital_comic",
            komga_id="k2",
            komga_library_id="lib_manga",
            source="manual",
        )
        keep = _insert_item(
            db,
            title="Keep",
            isbn=None,
            media_type="digital_comic",
            komga_id="k3",
            komga_library_id="lib_comics",
            source="komga",
        )
        db.execute("COMMIT")

        data = admin_client.post("/api/sync/komga/libraries/cleanup").json()
        assert data == {"ok": True, "deleted": 1, "detached": 1}
        assert db.execute(
            "SELECT 1 FROM items WHERE id = ?", (synced,)
        ).fetchone() is None
        adopted_row = db.execute(
            "SELECT komga_id, komga_library_id FROM items WHERE id = ?", (adopted,)
        ).fetchone()
        assert adopted_row["komga_id"] is None
        assert adopted_row["komga_library_id"] is None
        assert db.execute(
            "SELECT komga_id FROM items WHERE id = ?", (keep,)
        ).fetchone()["komga_id"] == "k3"
''')

# Public URL/detail tests should use the new Digital Comic type and verify that
# both server and browser URL concepts are visible in Settings.
patch(
    "tests/test_komga_public_url.py",
    "        assert 'name=\"komga_public_url\"' in html\n",
    "        assert 'name=\"komga_public_url\"' in html\n        assert 'Komga Internal / API URL' in html\n        assert 'Komga Browser / Public URL' in html\n        assert 'Audiobookshelf Internal / API URL' in html\n        assert 'Audiobookshelf Browser / Public URL' in html\n",
)
# Both Komga fixture rows in this file represent Komga digital copies.
p = Path("tests/test_komga_public_url.py")
text = p.read_text().replace('media_type="comic",\n            komga_id=', 'media_type="digital_comic",\n            komga_id=')
p.write_text(text)
