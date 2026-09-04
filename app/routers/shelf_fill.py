"""Context-aware physical shelf filling.

Shelf Fill deliberately layers on top of the existing barcode scanner rather
than duplicating its metadata/provider logic. A scan first resolves through the
normal Add flow, then the physical copy is placed into the selected hierarchical
location node. Existing catalogue items skip metadata lookup and are moved
straight away.

The exact ``item_copies.location_id`` is authoritative. ``items.location_id``
is kept in sync only with the nearest legacy/root location so older screens and
exports remain truthful while nested shelves stay precise.
"""

from fastapi import Depends, Form, Request

from app.auth import require_role
from app.database import get_db
from app.routers import items_common, pages
from app.services import holdings
from app.services import location_tree as location_svc
from app.media_types import is_physical_media


def _location_target(db, location_node_id: int):
    holdings.ensure_foundation(db)
    node = db.execute(
        "SELECT id, name FROM location_nodes WHERE id = ?", (location_node_id,)
    ).fetchone()
    if not node:
        return None
    path = location_svc.location_path(db, location_node_id)
    return {
        "id": node["id"],
        "name": node["name"],
        "path": " › ".join(part["name"] for part in path),
        "legacy_location_id": location_svc.nearest_legacy_location(db, location_node_id),
    }


def _copy_from_barcode(db, raw: str):
    """Resolve a copy-specific barcode before falling back to item barcodes."""
    holdings.ensure_foundation(db)
    row = db.execute(
        "SELECT c.id AS copy_id, c.item_id, c.copy_number, i.title, i.authors, "
        "i.media_type, i.cover_path, i.owned "
        "FROM item_copies c JOIN items i ON i.id = c.item_id "
        "WHERE c.copy_barcode = ? LIMIT 1",
        (raw,),
    ).fetchone()
    return dict(row) if row else None


def _place_copy(
    db,
    *,
    item_id: int,
    location_node_id: int,
    copy_id: int | None = None,
) -> dict:
    """Place one physical copy at a precise node and append it to shelf order."""
    holdings.ensure_foundation(db)
    target = _location_target(db, location_node_id)
    if target is None:
        raise ValueError("Shelf location no longer exists")

    item = db.execute(
        "SELECT id, title, authors, media_type, cover_path, owned "
        "FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not item:
        raise ValueError("Item no longer exists")
    if not is_physical_media(item["media_type"]):
        raise ValueError("Shelf Fill only works with physical media")

    was_wishlist = not bool(item["owned"])
    if was_wishlist:
        # Physically placing an item is stronger evidence than a wishlist flag.
        db.execute(
            "UPDATE items SET owned = 1, updated_at = datetime('now') WHERE id = ?",
            (item_id,),
        )

    # Creates the primary copy for an item that was previously wishlisted or
    # predates the holdings foundation. Existing precise child placement is not
    # disturbed until the explicit UPDATE below.
    holdings.sync_item_holding(db, item_id)

    copy = None
    if copy_id is not None:
        copy = db.execute(
            "SELECT id, copy_number, is_primary FROM item_copies "
            "WHERE id = ? AND item_id = ?",
            (copy_id, item_id),
        ).fetchone()
    if copy is None:
        copy = db.execute(
            "SELECT id, copy_number, is_primary FROM item_copies "
            "WHERE item_id = ? AND is_primary = 1 ORDER BY id LIMIT 1",
            (item_id,),
        ).fetchone()
    if copy is None:
        raise ValueError("Physical copy could not be created")

    max_row = db.execute(
        "SELECT COALESCE(MAX(position_order), 0) AS p FROM item_copies "
        "WHERE location_id = ? AND id != ?",
        (location_node_id, copy["id"]),
    ).fetchone()
    next_position = int(max_row["p"] or 0) + 1

    db.execute(
        "UPDATE item_copies SET location_id = ?, position_order = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (location_node_id, next_position, copy["id"]),
    )
    if copy["is_primary"]:
        db.execute(
            "UPDATE items SET location_id = ?, updated_at = datetime('now') WHERE id = ?",
            (target["legacy_location_id"], item_id),
        )

    return {
        "item_id": item["id"],
        "title": item["title"],
        "authors": item["authors"],
        "media_type": item["media_type"],
        "cover_path": item["cover_path"],
        "copy_id": copy["id"],
        "copy_number": copy["copy_number"],
        "position_order": next_position,
        "location_path": target["path"],
        "was_wishlist": was_wishlist,
    }


def _render_result(request: Request, placed: dict, *, newly_added: bool = False):
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/shelf_fill_result.html",
        {**placed, "newly_added": newly_added},
    )


def _render_error(request: Request, raw: str, message: str):
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/scan_result.html",
        {"status": "error", "isbn": raw, "message": message},
    )


@pages.router.get("/api/shelf-fill/location-picker")
async def shelf_fill_location_picker(
    request: Request,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        holdings.ensure_foundation(db)
        locations = location_svc.flattened_tree(db)
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/shelf_fill_location_picker.html",
        {"locations": locations},
    )


@pages.router.post("/api/shelf-fill/place")
async def shelf_fill_place(
    request: Request,
    item_id: int = Form(...),
    location_node_id: int = Form(...),
    _=Depends(require_role("editor")),
):
    try:
        with get_db() as db:
            placed = _place_copy(
                db,
                item_id=item_id,
                location_node_id=location_node_id,
            )
    except ValueError as exc:
        return _render_error(request, str(item_id), str(exc))

    items_common._log_scan(
        str(item_id), placed["media_type"], "shelved", item_id, "shelf_fill"
    )
    return _render_result(request, placed)


@pages.router.post("/api/shelf-fill/scan")
async def shelf_fill_scan(
    request: Request,
    isbn: str = Form(...),
    media_type: str = Form("auto"),
    location_node_id: int | None = Form(None),
    platform: str = Form(""),
    _=Depends(require_role("editor")),
):
    """Add-or-place one barcode at a precise location node."""
    raw = isbn.strip()
    if not location_node_id:
        return _render_error(request, raw, "Choose a shelf location before scanning")

    with get_db() as db:
        target = _location_target(db, location_node_id)
        if target is None:
            return _render_error(request, raw, "Shelf location no longer exists")
        exact_copy = _copy_from_barcode(db, raw)

    # A copy-specific barcode identifies exactly which physical copy is in the
    # user's hand, so it wins over item-level ISBN/UPC matching.
    if exact_copy:
        try:
            with get_db() as db:
                placed = _place_copy(
                    db,
                    item_id=exact_copy["item_id"],
                    copy_id=exact_copy["copy_id"],
                    location_node_id=location_node_id,
                )
        except ValueError as exc:
            return _render_error(request, raw, str(exc))
        items_common._log_scan(
            raw, placed["media_type"], "shelved", placed["item_id"], "shelf_fill"
        )
        return _render_result(request, placed)

    # Reuse the existing item-level barcode resolver before asking metadata
    # providers. This keeps repeated shelf work fast and avoids quota use.
    from app.routers import items

    existing = items._find_item_by_barcode(raw)
    if existing:
        try:
            with get_db() as db:
                placed = _place_copy(
                    db,
                    item_id=existing["id"],
                    location_node_id=location_node_id,
                )
        except ValueError as exc:
            return _render_error(request, raw, str(exc))
        items_common._log_scan(
            raw, placed["media_type"], "shelved", placed["item_id"], "shelf_fill"
        )
        return _render_result(request, placed)

    # Unknown barcode: let the mature Add scanner do every normal provider,
    # detection and duplicate check. The nearest legacy/root location is only a
    # compatibility seed; successful items are immediately moved to the exact
    # node below.
    response = await items.scan_isbn(
        request,
        isbn=raw,
        media_type=media_type,
        location_id=target["legacy_location_id"],
        platform=platform,
        mode="add",
        borrower_id=None,
        _=_,
    )
    context = getattr(response, "context", None) or {}
    item_id = context.get("item_id")
    status = context.get("status")
    if item_id and status in {"added", "duplicate"}:
        try:
            with get_db() as db:
                placed = _place_copy(
                    db,
                    item_id=int(item_id),
                    location_node_id=location_node_id,
                )
        except ValueError as exc:
            return _render_error(request, raw, str(exc))
        items_common._log_scan(
            raw, placed["media_type"], "shelved", placed["item_id"], "shelf_fill"
        )
        return _render_result(request, placed, newly_added=status == "added")

    # Metadata-miss and magazine issue-detail forms still work unchanged. The
    # client watches their eventual Added/Duplicate card and calls /place so
    # the exact shelf context survives that extra human-input step.
    return response
