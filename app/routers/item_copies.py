"""Physical-copy management for an item's owned holdings.

Catalogue metadata belongs to ``items``; this module owns the individual
physical objects the household actually has. Copies may share the same exact
shelf, carry their own condition/acquisition/barcode metadata, and one copy is
kept as the compatibility primary while legacy item-level location fields are
still in use.
"""

from datetime import date
import sqlite3

from fastapi import Depends, Form, Request

from app.auth import require_role
from app.database import get_db
from app.media_types import is_physical_media
from app.routers import pages
from app.services import holdings
from app.services import location_tree as location_svc


CONDITIONS = (
    ("new", "New"),
    ("like_new", "Like new"),
    ("very_good", "Very good"),
    ("good", "Good"),
    ("fair", "Fair"),
    ("poor", "Poor"),
)
_CONDITION_VALUES = {value for value, _label in CONDITIONS}


def _clean_location(db, value: str | int | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        location_id = int(value)
    except (TypeError, ValueError):
        raise ValueError("Invalid location")
    if not db.execute(
        "SELECT 1 FROM location_nodes WHERE id = ?", (location_id,)
    ).fetchone():
        raise ValueError("Location not found")
    return location_id


def _clean_price(value: str | None) -> float | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        price = float(raw)
    except ValueError:
        raise ValueError("Acquisition price must be a number")
    if price < 0:
        raise ValueError("Acquisition price cannot be negative")
    return price


def _clean_date(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise ValueError("Acquisition date must be a valid date")


def _clean_condition(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw not in _CONDITION_VALUES:
        raise ValueError("Unknown condition")
    return raw


def _copy_context(db, item_id: int, *, message: str = "", error: str = "") -> dict:
    holdings.ensure_foundation(db)
    item = db.execute(
        "SELECT id, title, media_type, owned FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if not item:
        return {"missing": True, "item_id": item_id, "copies": []}

    physical = bool(item["owned"] and is_physical_media(item["media_type"]))
    copies = []
    if physical:
        for row in db.execute(
            "SELECT c.*, n.name AS location_name FROM item_copies c "
            "LEFT JOIN location_nodes n ON n.id = c.location_id "
            "WHERE c.item_id = ? ORDER BY c.copy_number, c.id",
            (item_id,),
        ).fetchall():
            data = dict(row)
            data["location_path"] = (
                " › ".join(
                    part["name"]
                    for part in location_svc.location_path(db, row["location_id"])
                )
                if row["location_id"] else None
            )
            copies.append(data)

    return {
        "missing": False,
        "item": item,
        "item_id": item_id,
        "is_physical_holding": physical,
        "copies": copies,
        "location_nodes": location_svc.flattened_tree(db) if physical else [],
        "conditions": CONDITIONS,
        "message": message,
        "copy_error": error,
    }


def _render(request: Request, item_id: int, *, message: str = "", error: str = ""):
    with get_db() as db:
        context = _copy_context(db, item_id, message=message, error=error)
    return request.app.state.templates.TemplateResponse(
        request, "fragments/item_copies.html", context
    )


def _sync_primary_legacy_location(db, item_id: int, location_id: int | None) -> None:
    legacy_id = (
        location_svc.nearest_legacy_location(db, location_id)
        if location_id else None
    )
    db.execute("UPDATE items SET location_id = ? WHERE id = ?", (legacy_id, item_id))


@pages.router.get("/api/items/{item_id}/copies")
async def item_copies_fragment(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    return _render(request, item_id)


@pages.router.post("/api/items/{item_id}/copies")
async def add_item_copy(
    request: Request,
    item_id: int,
    location_id: str = Form(""),
    condition: str = Form(""),
    notes: str = Form(""),
    acquired_date: str = Form(""),
    acquisition_price: str = Form(""),
    copy_barcode: str = Form(""),
    _=Depends(require_role("editor")),
):
    error = ""
    next_number = None
    try:
        with get_db() as db:
            holdings.ensure_foundation(db)
            item = db.execute(
                "SELECT id, media_type, owned FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if not item:
                error = "Item not found"
            elif not item["owned"] or not is_physical_media(item["media_type"]):
                error = "Only owned physical media can have copies"
            else:
                loc_id = _clean_location(db, location_id)
                condition_value = _clean_condition(condition)
                price = _clean_price(acquisition_price)
                acquired = _clean_date(acquired_date)
                barcode = copy_barcode.strip() or None
                next_number = db.execute(
                    "SELECT COALESCE(MAX(copy_number), 0) + 1 AS n "
                    "FROM item_copies WHERE item_id = ?",
                    (item_id,),
                ).fetchone()["n"]
                db.execute(
                    "INSERT INTO item_copies "
                    "(item_id, copy_number, location_id, condition, notes, acquired_date, "
                    "acquisition_price, copy_barcode, is_primary) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (
                        item_id, next_number, loc_id, condition_value,
                        notes.strip() or None, acquired, price, barcode,
                    ),
                )
    except ValueError as exc:
        error = str(exc)
    except sqlite3.IntegrityError:
        error = "That copy barcode is already in use"

    if error:
        return _render(request, item_id, error=error)
    return _render(request, item_id, message=f"Copy {next_number} added")


@pages.router.post("/api/items/{item_id}/copies/{copy_id}")
async def update_item_copy(
    request: Request,
    item_id: int,
    copy_id: int,
    location_id: str = Form(""),
    condition: str = Form(""),
    notes: str = Form(""),
    acquired_date: str = Form(""),
    acquisition_price: str = Form(""),
    copy_barcode: str = Form(""),
    _=Depends(require_role("editor")),
):
    error = ""
    try:
        with get_db() as db:
            holdings.ensure_foundation(db)
            copy = db.execute(
                "SELECT id, is_primary FROM item_copies WHERE id = ? AND item_id = ?",
                (copy_id, item_id),
            ).fetchone()
            if not copy:
                error = "Copy not found"
            else:
                loc_id = _clean_location(db, location_id)
                db.execute(
                    "UPDATE item_copies SET location_id = ?, condition = ?, notes = ?, "
                    "acquired_date = ?, acquisition_price = ?, copy_barcode = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (
                        loc_id,
                        _clean_condition(condition),
                        notes.strip() or None,
                        _clean_date(acquired_date),
                        _clean_price(acquisition_price),
                        copy_barcode.strip() or None,
                        copy_id,
                    ),
                )
                if copy["is_primary"]:
                    _sync_primary_legacy_location(db, item_id, loc_id)
    except ValueError as exc:
        error = str(exc)
    except sqlite3.IntegrityError:
        error = "That copy barcode is already in use"

    if error:
        return _render(request, item_id, error=error)
    return _render(request, item_id, message="Copy updated")


@pages.router.delete("/api/items/{item_id}/copies/{copy_id}")
async def delete_item_copy(
    request: Request,
    item_id: int,
    copy_id: int,
    _=Depends(require_role("editor")),
):
    error = ""
    removed_number = None
    with get_db() as db:
        holdings.ensure_foundation(db)
        copy = db.execute(
            "SELECT id, copy_number, is_primary FROM item_copies "
            "WHERE id = ? AND item_id = ?",
            (copy_id, item_id),
        ).fetchone()
        if not copy:
            error = "Copy not found"
        else:
            count = db.execute(
                "SELECT COUNT(*) AS c FROM item_copies WHERE item_id = ?", (item_id,)
            ).fetchone()["c"]
            if count <= 1:
                error = "An owned physical item must keep at least one copy"
            else:
                removed_number = copy["copy_number"]
                was_primary = bool(copy["is_primary"])
                db.execute("DELETE FROM item_copies WHERE id = ?", (copy_id,))
                if was_primary:
                    replacement = db.execute(
                        "SELECT id, location_id FROM item_copies WHERE item_id = ? "
                        "ORDER BY copy_number, id LIMIT 1",
                        (item_id,),
                    ).fetchone()
                    db.execute(
                        "UPDATE item_copies SET is_primary = 1, "
                        "updated_at = datetime('now') WHERE id = ?",
                        (replacement["id"],),
                    )
                    _sync_primary_legacy_location(
                        db, item_id, replacement["location_id"]
                    )

    if error:
        return _render(request, item_id, error=error)
    return _render(request, item_id, message=f"Copy {removed_number} removed")
