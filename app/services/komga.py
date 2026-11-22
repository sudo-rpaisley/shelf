"""Legacy Komga API adapters. All importing is owned by komga_sync/records."""

from app.database import get_db, get_setting
from app.services import holdings, komga_books, komga_libraries, komga_sync, media_groups
from app.services.catalogue_security import require_item_role
from app.services.item_write import update_item_fields

PAGE_SIZE = komga_books.PAGE_SIZE
COVER_BATCH_SIZE = 8
COVER_WALL_TIMEOUT = 15.0
KOMGA_LIBRARY_MEDIA_TYPES = frozenset({"digital_comic", "digital_manga"})
KOMGA_MEDIA_TYPE_LABELS = {"digital_comic": "Digital Comic", "digital_manga": "Digital Manga"}
_headers = komga_libraries._headers
_authors = komga_books._authors
_publish_year = komga_books._publish_year
_series_position = komga_books._series_position
_normalize_title = media_groups.normalise_title
_authors_compatible = media_groups._authors_compatible


def get_excluded_libraries() -> set[str]:
    return komga_sync.configuration()["excluded"]


def get_library_media_types() -> dict[str, str]:
    return {key: f"digital_{kind}" for key, kind in komga_sync.configuration()["kinds"].items()}


def suggest_library_media_type(name: str | None) -> str:
    """UI suggestion only; it is never evidence for a sync classification."""
    return f"digital_{komga_libraries.suggest_library_kind(name)}"


def library_media_type(library_id: str, library_name: str | None = None,
                       configured: dict[str, str] | None = None) -> str:
    mappings = get_library_media_types() if configured is None else configured
    selected = mappings.get(str(library_id))
    if selected not in KOMGA_LIBRARY_MEDIA_TYPES:
        raise komga_libraries.KomgaError("Select this Komga library's Comic/Manga classification before syncing")
    return selected


def _detach_automatic_format_links(db, item_id: int) -> None:
    db.execute("DELETE FROM item_links WHERE link_type = 'format' AND (item_a_id = ? OR item_b_id = ?)",
               (item_id, item_id))


def apply_library_media_types(db, mappings: dict[str, str]) -> int:
    """Apply an explicit user choice, never a migration/name-derived guess."""
    changed = 0
    for library_id, media_type in mappings.items():
        if media_type not in KOMGA_LIBRARY_MEDIA_TYPES:
            raise komga_libraries.KomgaError("Invalid Komga media type")
        kind = media_type.removeprefix("digital_")
        rows = db.execute(
            "SELECT * FROM items_live WHERE komga_library_id = ? "
            "AND media_type IN ('digital_comic','digital_manga') AND media_type != ?",
            (str(library_id), media_type),
        ).fetchall()
        for item in rows:
            require_item_role(db, item["id"], "editor")
            if item["source"] != "komga":
                raise komga_libraries.KomgaError("Komga reclassification would change a manually catalogued item")
            other = db.execute(
                "SELECT 1 FROM komga_records WHERE item_id = ? AND library_id != ? AND kind != ? LIMIT 1",
                (item["id"], str(library_id), kind),
            ).fetchone()
            if other:
                raise komga_libraries.KomgaError("Plural Komga holdings have incompatible classifications")
            update_item_fields(db, item["id"], {"media_type": media_type})
            _detach_automatic_format_links(db, item["id"])
            db.execute("UPDATE komga_records SET kind = ? WHERE item_id = ? AND library_id = ?",
                       (kind, item["id"], str(library_id)))
            holdings.sync_item(db, item["id"])
            changed += 1
    if changed:
        media_groups.auto_link_family(db, "comic")
        media_groups.auto_link_family(db, "manga")
    return changed


def get_browser_url(komga_url: str, komga_id: str) -> str:
    with get_db() as db:
        public_url = get_setting(db, "komga_public_url")
    return komga_libraries.browser_book_url(komga_url, str(komga_id), public_url=public_url or None)


async def _fetch_library_books(client, komga_url, api_key, library_id):
    try:
        return await komga_books.fetch_library_books(client, komga_url, api_key, library_id)
    except komga_libraries.KomgaError:
        return None


async def _download_cover(client, komga_url, api_key, komga_id, item_id) -> str:
    return "downloaded" if await komga_sync._ingest_cover(client, komga_url, api_key, item_id, komga_id) else "missing"


_download_cover_with_deadline = _download_cover


async def _drain_cover_batch(batch: list[tuple], stats: dict) -> None:
    jobs = [(client, url, key, item_id, book_id) for client, url, key, book_id, item_id in batch]
    batch.clear()
    await komga_sync._drain_cover_batch(jobs, stats)


async def sync(komga_url: str, api_key: str, on_progress=None) -> dict:
    """Preserve main's public signature/stats while delegating to one importer."""
    try:
        result = await komga_sync.sync(on_progress, url=komga_url, api_key=api_key)
    except komga_libraries.KomgaError as exc:
        return {"error": str(exc)}
    if result.get("error"):
        return result
    result = dict(result)
    result["added"] = result.pop("created", 0)
    result["updated"] += result.pop("adopted", 0)
    if not result.get("in_trash"):
        result.pop("in_trash", None)
    return result


def _auto_link_items() -> None:
    with get_db() as db:
        media_groups.auto_link_family(db, "comic")
        media_groups.auto_link_family(db, "manga")
