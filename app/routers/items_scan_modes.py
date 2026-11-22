"""The scan-mode handlers, split out of items.py under its size cap.

Each mode is what a scan *does* to an item that already exists — lend it,
return it, move it, count it in an inventory sweep, look it up, rate it.
They are pure request handlers: everything they need arrives as an argument
except the database and the scan log.

Split from app/routers/items.py on 2026-09-07, when #86 and #112 landed
independently and together pushed that module past its 1600-line cap. The
guard's own advice is "split by feature area, as items_covers/csv/catalog
were"; this is that split.
"""

from app import i18n
from app.database import get_db
from app.routers import items_common
from app.services import audits
from app.services import barcode_resolve
from app.services import item_copies
from app.services.catalogue_security import require_item_role
from app.services.item_write import _write_scope, update_item_fields
from app.services.location_tree import legacy_location_to_node
from app.services.write_targets import validated_location_id
from fastapi import HTTPException


def _find_item_by_barcode(raw: str) -> dict | None:
    """Find an existing item by copy barcode, ISBN or UPC — `barcode_resolve.resolve`."""
    return barcode_resolve.resolve(raw)


#: How many copy locations a scan card names before it says "and N more".
#: A scan card is two lines on a phone; three names plus a remainder is as
#: much as it holds and still reads. Chosen here rather than left to the
#: caller so Inventory and Lookup cannot drift apart.
_PLACE_CAP = 3


def _format_copy_places(copies) -> str:
    """Name the distinct locations a set of copies occupies, in row order.

    `copies` is `item_copies.copies_for_item` output. Duplicate location
    names collapse — two copies on one shelf is one place — and a copy with
    no location contributes the literal "no location", because "where is it?"
    has an answer there and it is not silence. Returns "" for no copies.
    """
    places: list[str] = []
    for copy in copies:
        name = copy["location_name"] if copy["location_id"] else i18n.gettext("no location")
        if name not in places:
            places.append(name)
    if not places:
        return ""
    if len(places) > _PLACE_CAP:
        named, rest = places[:_PLACE_CAP], len(places) - _PLACE_CAP
        return f"{', '.join(named)} and {rest} more"
    if len(places) == 1:
        return places[0]
    return f"{', '.join(places[:-1])} and {places[-1]}"


def _scan_mode_lend(request, templates, item: dict, borrower_id: int | None, raw: str):
    """Compatibility API over the canonical locked lending business helper."""
    from app.routers.checkouts import scan_lend

    return scan_lend(request, templates, item, borrower_id, raw)


def _scan_mode_return(request, templates, item: dict, raw: str):
    """Compatibility API over the canonical locked lending business helper."""
    from app.routers.checkouts import scan_return

    return scan_return(request, templates, item, raw)


def _live_scan_item(db, item: dict, role: str) -> dict:
    """Rebind a copy label and re-read live/private fields on the decision DB."""
    copy = None
    if item.get("copy_id"):
        copy = db.execute(
            "SELECT c.*, n.name AS copy_location_name FROM copies_live c "
            "LEFT JOIN location_nodes n ON n.id = c.location_id WHERE c.id = ?",
            (item["copy_id"],),
        ).fetchone()
        if copy is None:
            raise HTTPException(404, "Item not found")
    item_id = copy["item_id"] if copy is not None else item["id"]
    require_item_role(db, item_id, role)
    row = db.execute(
        "SELECT i.*, l.name AS location_name FROM items_live i "
        "LEFT JOIN locations l ON l.id = i.location_id WHERE i.id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Item not found")
    result = {**item, **dict(row)}
    if copy is not None:
        result.update(copy_id=copy["id"], copy_number=copy["copy_number"],
                      copy_location_id=copy["location_id"],
                      copy_location_name=copy["copy_location_name"])
    return result


def _scan_mode_move(request, templates, item: dict, location_id: int | None, raw: str):
    """Handle move mode: update item location."""
    if not location_id or location_id <= 0:
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "error", "isbn": raw, "message": i18n.gettext("No target location selected")},
        )

    copy_id = item.get("copy_id")
    if copy_id:
        old_location = item.get("copy_location_name") or i18n.gettext("no location")
    else:
        old_location = item.get("location_name") or i18n.gettext("No location")

    # A deleted location used to be a foreign-key 500 here (#54).
    value_error = None
    with get_db() as db:
        try:
            with _write_scope(db):
                item = _live_scan_item(db, item, "editor")
                old_location = (item.get("copy_location_name") if copy_id else item.get("location_name"))
                old_location = old_location or i18n.gettext("No location")
                validated_location_id(db, location_id)
                if not copy_id:
                    update_item_fields(db, item["id"], {"location_id": location_id})
                else:
                    # Move mode's selector is LEGACY; copy placement is NODE.
                    node_id = legacy_location_to_node(db, location_id, create=True)
                    copy = db.execute(
                        "SELECT * FROM copies_live WHERE id = ? AND item_id = ?",
                        (copy_id, item["id"]),
                    ).fetchone()
                    if copy is None:
                        raise HTTPException(404, "Item not found")
                    item_copies.relocate_copy(db, copy, node_id)
                    item_copies.clear_missing(db, copy["id"])
                new_loc = db.execute(
                    "SELECT name FROM locations WHERE id = ?", (location_id,)
                ).fetchone()
        except ValueError as exc:
            value_error = str(exc)
    if value_error:
        items_common._log_scan(raw, item.get("media_type", ""), "error", item["id"], "move")
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "error", "isbn": raw, "message": value_error},
        )

    new_name = new_loc["name"] if new_loc else i18n.gettext("Unknown")
    message = f"{old_location} → {new_name}"
    if copy_id:
        message = i18n.gettext("Copy %(n)s: %(detail)s") % {"n": item["copy_number"], "detail": message}
    items_common._log_scan(raw, item.get("media_type", ""), "moved", item["id"], "move")
    resp = templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {"status": "moved", "isbn": raw, "title": item["title"],
         "item_id": item["id"], "cover_path": item.get("cover_path"),
         "media_type": item.get("media_type"),
         "authors": item.get("authors"), "message": message},
    )
    return resp


def _scan_mode_lookup(request, templates, item: dict | None, raw: str):
    """Handle lookup mode: check if item exists in collection."""
    if not item:
        items_common._log_scan(raw, "", "not_owned", None, "lookup")
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "not_owned", "isbn": raw, "message": i18n.gettext("Not in your collection")},
        )

    with get_db() as db:
        item = _live_scan_item(db, item, "viewer")
    # Lookup is the fourth reader (#116). It is display only, so unlike Scan
    # Move — which is deliberately left on the seam and owned by its own plan
    # — making it copy-aware costs one formatter call. A two-copy item that
    # reports two rooms on its page must not report one when scanned.
    # An upgraded database holds located items with no copy rows (G86), so the
    # seam is the fallback before the empty label — otherwise Lookup reports
    # "No location set" for an item whose page shows a location. B5.
    if item.get("copy_id"):
        # A copy label answers for its own copy, not the item's spread.
        place = item.get("copy_location_name") or i18n.gettext("no location")
        message = i18n.gettext("Copy %(n)s · %(place)s") % {"n": item["copy_number"], "place": place}
    else:
        with get_db() as db:
            copies = item_copies.copies_for_item(db, item["id"])
        location_str = (
            _format_copy_places(copies) or item.get("location_name") or i18n.gettext("No location set")
        )
        message = i18n.gettext("Location: %(place)s") % {"place": location_str}
    items_common._log_scan(raw, item.get("media_type", ""), "found", item["id"], "lookup")
    return templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {"status": "found", "isbn": raw, "title": item["title"],
         "item_id": item["id"], "cover_path": item.get("cover_path"),
         "media_type": item.get("media_type"),
         "authors": item.get("authors"), "message": message},
    )


def _scan_mode_quick_rate(request, templates, item: dict, raw: str):
    """Compatibility API over the canonical personal-state business helper."""
    from app.routers.user_state_items import quick_rate

    return quick_rate(request, templates, item, raw)


def _scan_mode_in_trash(request, templates, item: dict, raw: str, mode: str, *,
                        location_id: int | None = None, audit_id: int | None = None):
    """Any existing-item mode, when the barcode names an item in Trash.

    Reports it and offers Restore; the mode's own action does **not** run —
    no loan, no move, no count, no rating. A lookup never changes the
    collection, and neither does a lend of something the user deleted.
    Restoring is a separate, explicit click (`POST /api/trash/items/{id}/
    restore` with `render=scan`), which answers with the `restored` card.
    An Inventory scan in an open audit records it as `in_trash` first.
    """
    with get_db() as db:
        require_item_role(db, item["id"], "viewer", include_deleted=True)
    if mode == "inventory" and audit_id:
        refusal = audits.record_standalone(
            audit_id, item_id=item["id"], copy_id=item.get("copy_id"),
            location_id=location_id, raw_code=raw, result="in_trash")
        if refusal:
            items_common._log_scan(raw, item.get("media_type", ""), "error", item["id"], mode)
            return templates.TemplateResponse(
                request, "fragments/scan_result.html",
                {"status": "error", "isbn": raw, "message": refusal})
    items_common._log_scan(raw, item.get("media_type", ""), "in_trash", item["id"], mode)
    return templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {"status": "in_trash", "isbn": raw, "mode": mode, "title": item["title"],
         "item_id": item["id"], "cover_path": item.get("cover_path"),
         "media_type": item.get("media_type"),
         "authors": item.get("authors"), "deleted_at": item.get("deleted_at"),
         "copy_in_trash": item.get("copy_in_trash"), "copy_number": item.get("copy_number")},
    )


def _copy_label_duplicate(request, templates, raw: str, mode: str):
    """Add/Wishlist: a scanned copy label names a copy already owned.

    Runs before the Add path's provider lookups (decision 9 of the copy-labels
    plan): an exact label match is the more specific claim, so the scan
    reports the existing item instead of spending a lookup on a code that is
    not a product barcode. Returns `None` when `raw` is not a copy label, and
    the caller carries on with the ordinary Add path.
    """
    match = barcode_resolve.resolve(raw)
    if not match or not match.get("copy_id"):
        return None
    if match.get("deleted_at"):
        return _scan_mode_in_trash(request, templates, match, raw, mode)
    items_common._log_scan(raw, match.get("media_type", ""), "duplicate", match["id"], mode)
    return templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {"status": "duplicate", "isbn": raw, "title": match["title"], "item_id": match["id"]},
    )
