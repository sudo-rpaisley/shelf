"""Scanner catalogue policy and personal-state orchestration.

Keeps access decisions, duplicate handling and scan-created item state out of
``items_common.py`` so that router helper remains a transport adapter rather
than regrowing into the catalogue god-object it was split to remove.
"""

from __future__ import annotations

import time

from app.config import MEDIA_TYPES
from app.database import get_db
from app.services import libraries, user_state
from app.services.item_write import insert_item

_SCAN_LOG_RETENTION_DAYS = 90
_SCAN_LOG_PRUNE_INTERVAL = 3600
_scan_log_last_prune: float = float("-inf")


def log_scan(
    barcode: str,
    media_type: str,
    result: str,
    item_id: int | None = None,
    mode: str = "add",
) -> None:
    """Record one scan and periodically prune the bounded activity log."""
    global _scan_log_last_prune
    with get_db() as db:
        db.execute(
            "INSERT INTO scan_log (isbn, media_type, result, item_id, mode) "
            "VALUES (?, ?, ?, ?, ?)",
            (barcode, media_type, result, item_id, mode),
        )
        now = time.monotonic()
        if now - _scan_log_last_prune >= _SCAN_LOG_PRUNE_INTERVAL:
            _scan_log_last_prune = now
            db.execute(
                "DELETE FROM scan_log WHERE created_at < datetime('now', ?)",
                (f"-{_SCAN_LOG_RETENTION_DAYS} days",),
            )


def default_library_edit_allowed(actor: dict) -> bool:
    """Whether ``actor`` may create a shared row in Main Library."""
    with get_db() as db:
        return libraries.has_library_role(
            db, actor, libraries.DEFAULT_LIBRARY_ID, "editor"
        )


def duplicate_response(
    request,
    templates,
    existing,
    barcode: str,
    *,
    mode: str,
    media_type: str | None = None,
):
    """Resolve a duplicate without leaking a catalogue row the actor cannot see.

    Wishlist is personal: a visible existing row gains only the acting user's
    Wishlist flag; shared ``owned`` remains untouched.
    """
    item_id = int(existing["id"])
    actor = dict(request.state.user)
    with get_db() as db:
        if not libraries.has_item_role(db, actor, item_id, "viewer"):
            log_scan(barcode, media_type or "", "duplicate", None, mode)
            return templates.TemplateResponse(
                request,
                "fragments/scan_result.html",
                {
                    "status": "error",
                    "isbn": barcode,
                    "message": "This barcode cannot be added here",
                },
            )

        row = db.execute(
            "SELECT id, title, authors, cover_path, media_type, source "
            "FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            return templates.TemplateResponse(
                request,
                "fragments/scan_result.html",
                {"status": "error", "isbn": barcode, "message": "Item not found"},
            )
        full = dict(row)
        if mode == "wishlist":
            user_state.save_state(db, int(actor["id"]), item_id, wishlist=1)

    effective_type = media_type or full.get("media_type") or ""
    if mode == "wishlist":
        log_scan(barcode, effective_type, "wishlisted", item_id, mode)
        context = {
            "status": "wishlisted",
            "isbn": barcode,
            "title": full["title"],
            "authors": full.get("authors"),
            "cover_path": full.get("cover_path"),
            "item_id": item_id,
            "source": full.get("source") or "catalogue",
            "media_type_label": MEDIA_TYPES.get(effective_type, effective_type),
        }
    else:
        log_scan(barcode, effective_type, "duplicate", item_id, mode)
        context = {
            "status": "duplicate",
            "isbn": barcode,
            "title": full["title"],
            "item_id": item_id,
        }
    return templates.TemplateResponse(request, "fragments/scan_result.html", context)


def save_scanned_item(
    metadata: dict,
    isbn13: str,
    media_type: str,
    location_id: int | None,
    source: str,
    hc_ids: dict,
    *,
    owned: int = 1,
    wishlist_user_id: int | None = None,
) -> int:
    """Insert one scanner-resolved item and optional personal Wishlist atomically."""
    with get_db() as db:
        item_id = insert_item(
            db,
            title=metadata["title"],
            subtitle=metadata.get("subtitle"),
            authors=metadata.get("authors"),
            isbn=isbn13,
            media_type=media_type,
            publisher=metadata.get("publisher"),
            publish_year=metadata.get("publish_year"),
            page_count=metadata.get("page_count"),
            description=metadata.get("description"),
            series_name=metadata.get("series_name"),
            series_position=metadata.get("series_position"),
            location_id=location_id,
            source=source,
            language=metadata.get("language"),
            hardcover_book_id=hc_ids.get("hardcover_book_id"),
            hardcover_edition_id=hc_ids.get("hardcover_edition_id"),
            owned=owned,
        )
        if wishlist_user_id is not None:
            user_state.save_state(db, int(wishlist_user_id), item_id, wishlist=1)
        return item_id
