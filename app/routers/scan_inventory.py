"""Inventory scan mode — is this item on the shelf being audited?

Split out of `app/routers/items_scan_modes.py` by the copy-labels plan
(2026-10-19): one arm per module, because the inventory arm is the one that
grows — copy-aware relocation (copy-labels), then audit sessions. `_format_copy_places` stays with Lookup,
its other reader, and is imported from there.

**A copy label names the copy.** When the scan resolved a copy barcode
(`item["copy_id"]`, see `barcode_resolve`), the question is whether *that copy*
is here, and when it is not it is relocated whatever the item's copy count —
the barcode settled the ambiguity the `elsewhere` arm exists for. An ISBN or
UPC scan is unchanged.

**Audit sessions.** With an open `audit_id` every scan writes one
`audit_entries` row (`app/services/audits.py`) inside the same `BEGIN
IMMEDIATE` as its decision, and the session is re-read under that lock, so a
scan racing Finish writes nothing (G18, G118). A location audit takes scans
only at its own location. Without an `audit_id` the arm behaves exactly as it
always has and records nothing.

**What is "here" must agree with `audits.expected_rows`** — copies first, the
`items.location_id` seam only for an item with no live copy row (G86) — or an
audit's report contradicts its own scans.

A confirm or relocate that names one copy (a label, or the item's only copy)
clears that copy's missing mark, audit or not: it is in the user's hand.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app import i18n
from app.database import get_db
from app.routers import items_common
from app.routers.items_scan_modes import _format_copy_places, _live_scan_item
from app.media_types import is_physical_media
from app.services import audits, barcode_resolve, item_copies
from app.services.catalogue_security import require_catalogue_role, require_item_role
from app.services.item_write import ItemValueError, _write_scope, update_item_fields
from app.services.location_tree import (
    legacy_location_to_node, node_to_legacy_location, validated_node_id,
)


router = APIRouter(prefix="/api")


@router.post("/inventory/missing")
async def inventory_missing(
    request: Request, location_id: str = Form("", max_length=32),
    scanned_ids: str = Form("", max_length=100000),
    _=Depends(require_catalogue_role("editor")),
):
    """Read-only item-level compatibility report over the native NODE census.

    Precise copy accounting belongs to durable audit sessions. This older wire
    surface accepts item IDs, so a scanned item accounts for all its expected
    copies at this node. It neither records an audit nor moves or marks anything.
    """
    try:
        node_id = int(location_id)
    except (TypeError, ValueError):
        return HTMLResponse("Location not found", status_code=404)
    if not 0 < node_id <= 2**63 - 1:
        return HTMLResponse("Location not found", status_code=404)
    scanned = {int(value) for value in scanned_ids.split(",")
               if value.strip().isascii() and value.strip().isdigit() and len(value.strip()) <= 19}
    with get_db() as db:
        location = db.execute("SELECT name FROM location_nodes WHERE id = ?", (node_id,)).fetchone()
        if location is None:
            return HTMLResponse("Location not found", status_code=404)
        # The native expected set uses scoped live copies, then an explicit
        # NODE/FLAT mapping only for copy-less physical rows. Never raw items.
        missing = {}
        for row in audits.expected_rows(db, node_id):
            if row["item_id"] not in scanned:
                missing.setdefault(row["item_id"], row)
    return request.app.state.templates.TemplateResponse(
        request, "fragments/inventory_missing.html",
        {"missing_items": list(missing.values()), "location_name": location["name"]},
    )


def _card(request, templates, status: str, raw: str, item: dict, message: str):
    return templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {"status": status, "isbn": raw, "title": item["title"],
         "item_id": item["id"], "cover_path": item.get("cover_path"),
         "media_type": item.get("media_type"),
         "authors": item.get("authors"),
         "message": message},
    )


def _error(request, templates, raw: str, message: str):
    return templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {"status": "error", "isbn": raw, "message": message},
    )


def _decide(db, item: dict, location_id: int) -> dict:
    """Is the scanned object here — and if not, move it. Runs under the lock.

    Returns the outcome: `status` (`confirmed` / `relocated` / `elsewhere` /
    `error`), the item it is about now, the `copies` an ISBN scan read, the
    one copy the scan names (`copy_id`, else None) and any `value_error`.
    """
    item_copies.write_lock(db)
    validated_node_id(db, location_id)
    item = _live_scan_item(db, item, "editor")
    copy_id = item.get("copy_id")
    copy_label = i18n.gettext("Copy %(n)s ") % {"n": item["copy_number"]} if copy_id else ""
    copies = []
    if copy_id:
        # Re-read the labelled copy under the lock: the resolver's read
        # ran on its own connection, before this transaction began.
        copy = db.execute(
            "SELECT id, item_id, copy_number, is_primary, location_id FROM copies_live "
            "WHERE id = ?",
            (copy_id,),
        ).fetchone()
        if copy is None:
            return {"status": "error", "item": item, "copies": copies, "copy_id": None,
                    "copy_label": copy_label,
                    "value_error": copy_label + i18n.gettext("is no longer in the collection")}
        # A merge since the resolver's read moved the copy to another
        # item: report and log against the item that owns it now.
        item = barcode_resolve.current_item(db, item, copy)
        require_item_role(db, item["id"], "editor")
        copy_label = i18n.gettext("Copy %(n)s ") % {"n": item["copy_number"]}
        if copy["location_id"] == location_id:
            status = "confirmed"
        else:
            try:
                item_copies.relocate_copy(db, copy, location_id)
                status = "relocated"
            except ValueError as e:
                return {"status": "error", "item": item, "copies": copies, "copy_id": None,
                        "copy_label": copy_label, "value_error": str(e)}
        return {"status": status, "item": item, "copies": copies, "copy_id": copy["id"],
                "copy_label": copy_label, "value_error": None}

    copies = item_copies.copies_for_item(db, item["id"])
    # An item with no copy rows at all falls back to the items.location_id
    # seam, exactly as audits.expected_rows does — the two must agree about
    # what is expected on a shelf or an audit contradicts itself. That state
    # is real rather than a fixture artefact: migration 26 and
    # backfill_legacy_locations create copies for `owned = 1` rows only, so
    # an upgraded database's wishlist item with a location has none.
    here = (
        any(copy["location_id"] == location_id for copy in copies)
        if copies
        else legacy_location_to_node(db, item.get("location_id")) == location_id
    )
    # The one copy an ISBN scan can mean — only when the item has exactly one.
    named = copies[0]["id"] if len(copies) == 1 else None
    if here:
        status = "confirmed"
    elif len(copies) >= 2:
        # The destructive half of #116: an ISBN does not say which copy is in
        # the user's hand, and moving the primary onto this shelf destroys the
        # layout a merge preserved. Report instead, and write nothing.
        return {"status": "elsewhere", "item": item, "copies": copies, "copy_id": None,
                "copy_label": "", "value_error": None}
    else:
        # The inventory selector is NODE-domain. A known copy moves directly;
        # an initial physical placement requests creation explicitly, never by
        # passing its node integer to the legacy item-location column.
        try:
            if copies:
                item_copies.relocate_copy(db, copies[0], location_id)
            elif item["owned"] and is_physical_media(item["media_type"]):
                named = item_copies.sync_primary_location(
                    db, item["id"], item.get("location_id"), create_copy=True
                )
                if named is None:
                    raise ValueError("No live copy is available; restore a copy or add one explicitly")
                copy = db.execute("SELECT * FROM copies_live WHERE id = ?", (named,)).fetchone()
                item_copies.relocate_copy(db, copy, location_id)
            else:
                legacy_id = node_to_legacy_location(db, location_id)
                if legacy_location_to_node(db, legacy_id) != location_id:
                    raise ValueError("This item has no physical copy to place at that location")
                update_item_fields(db, item["id"], {"location_id": legacy_id})
            status = "relocated"
        except ValueError as e:
            return {"status": "error", "item": item, "copies": copies, "copy_id": None,
                    "copy_label": "", "value_error": str(e)}
    return {"status": status, "item": item, "copies": copies, "copy_id": named,
            "copy_label": "", "value_error": None}


def _scan_mode_inventory(
    request,
    templates,
    item: dict | None,
    location_id: int | None,
    raw: str,
    *,
    audit_id: int | None = None,
):
    """Handle inventory mode: verify item is at expected location."""
    if not location_id or location_id <= 0:
        return _error(request, templates, raw, i18n.gettext("No audit location selected"))

    try:
        with get_db() as db:
            validated_node_id(db, location_id)
    except ValueError as exc:
        return _error(request, templates, raw, str(exc))

    if not item:
        # Read before the audit location: nothing here writes, and an
        # unknown barcode is not worth opening a write transaction for —
        # unless an audit is recording it, which takes its own (G112).
        if audit_id:
            refusal = audits.record_standalone(
                audit_id, item_id=None, copy_id=None, location_id=location_id,
                raw_code=raw, result="not_owned")
            if refusal:
                items_common._log_scan(raw, "", "error", None, "inventory")
                return _error(request, templates, raw, refusal)
        items_common._log_scan(raw, "", "not_owned", None, "inventory")
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "not_owned", "isbn": raw, "message": i18n.gettext("Not in collection")},
        )

    # G18 — the decision, the write and the audit entry share one connection
    # and one write lock. The arm this scan takes depends on the item's copy
    # cardinality and on the session still being open, and a copy inserted or
    # an audit finished between a read on one connection and a write on
    # another would be acted on blind. BEGIN IMMEDIATE takes the write lock
    # before the first read; nothing below logs until the block is closed (G3).
    refusal = None
    with get_db() as db:
        try:
            with _write_scope(db):
                if audit_id:
                    session = audits.open_session_for_write(db, audit_id)
                    refusal = (audits.closed_session_message(db, audit_id) if session is None
                               else audits.scan_location_refusal(session, location_id))
                if refusal:
                    raise audits.AuditError(refusal)
                validated_node_id(db, location_id)
                loc = db.execute(
                    "SELECT name FROM location_nodes WHERE id = ?", (location_id,)
                ).fetchone()
                loc_name = loc["name"]
                outcome = _decide(db, item, location_id)
                item = outcome["item"]
                if outcome["status"] == "error":
                    raise ItemValueError(outcome["value_error"])
                if outcome["status"] in ("confirmed", "relocated") and outcome["copy_id"]:
                    item_copies.clear_missing(db, outcome["copy_id"])
                if audit_id:
                    audits.add_entry(db, audit_id, item_id=item["id"],
                                     copy_id=outcome["copy_id"], location_id=location_id,
                                     raw_code=raw, result=outcome["status"])
        except ValueError as exc:
            outcome = {"status": "error", "item": item, "copies": [], "copy_id": None,
                       "copy_label": "", "value_error": str(exc)}

    if refusal:
        items_common._log_scan(raw, item.get("media_type", ""), "error", item["id"], "inventory")
        return _error(request, templates, raw, refusal)

    status = outcome["status"]
    copy_label = outcome["copy_label"]
    items_common._log_scan(raw, item.get("media_type", ""), status, item["id"], "inventory")
    if status == "confirmed":
        labelled = bool(item.get("copy_id"))
        return _card(request, templates, "confirmed", raw, item,
                     copy_label + (i18n.gettext("confirmed at %(place)s") if labelled else i18n.gettext("Confirmed at %(place)s")) % {"place": loc_name})
    if status == "elsewhere":
        return _card(request, templates, "elsewhere", raw, item,
                     i18n.gettext("Copies at %(places)s; none here.") % {"places": _format_copy_places(outcome["copies"])})
    if status == "error":
        return _error(request, templates, raw, outcome["value_error"])
    if item.get("copy_id"):
        old_location = item.get("copy_location_name") or i18n.gettext("no location")
        message = copy_label + i18n.gettext("was at %(old)s, updated to %(new)s") % {"old": old_location, "new": loc_name}
    else:
        old_location = item.get("location_name") or i18n.gettext("No location")
        message = i18n.gettext("Was at %(old)s, updated to %(new)s") % {"old": old_location, "new": loc_name}
    return _card(request, templates, "relocated", raw, item, message)
