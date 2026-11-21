"""Copy fragments with one service write funnel and NODE-domain placements.

Correctable failures are 200 inline fragments (htmx swaps them); barcode
collisions never name the other item, library, title, ID or Trash status.
The parent and live copy/URL binding are checked again inside the write lock.
"""

import logging
import math
import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from app.database import get_db
from app.media_types import is_physical_media
from app.routers import items_common
from app.services import item_copies
from app.services import locations as location_svc
from app.services.catalogue_security import (
    current_actor, require_item_role, require_catalogue_role as require_role,
)
from app.services.location_tree import validated_node_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
CONDITIONS = (
    ("new", "New"), ("like_new", "Like new"), ("very_good", "Very good"),
    ("good", "Good"), ("fair", "Fair"), ("poor", "Poor"),
    ("New", "New"), ("Fine", "Fine"), ("Good", "Good"),
    ("Fair", "Fair"), ("Poor", "Poor"), ("Ex-library", "Ex-library"),
)
CONDITION_MAX_LENGTH = 60
_EDITABLE = (
    "condition", "notes", "acquired_date", "acquisition_source",
    "acquisition_price", "provenance", "copy_barcode",
)


def _clean(value) -> str | None:
    return str(value).strip() or None if value is not None else None


def _copy_id_in_bounds(copy_id: int) -> bool:
    """Python path integers outside SQLite's signed range name no copy."""
    return -(2**63) <= copy_id < 2**63


def _clean_location(db, value) -> int | None:
    value = _clean(value)
    if value is None:
        return None
    try:
        node_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid location") from exc
    return validated_node_id(db, node_id)


def _clean_price(value) -> float | None:
    value = _clean(value)
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Acquisition price must be a number") from exc
    if not math.isfinite(price):
        raise ValueError("Acquisition price must be a finite number")
    if price < 0:
        raise ValueError("Acquisition price cannot be negative")
    return price


def _clean_date(value) -> str | None:
    value = _clean(value)
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("Acquisition date must be a valid date") from exc


def _clean_condition(value, *, existing: str | None = None) -> str | None:
    value = _clean(value)
    # Suggestions are not an enum: collectors' existing grades and free text
    # keep their exact case/spelling. Match the form's established input bound.
    if value is not None and len(value) > CONDITION_MAX_LENGTH:
        if existing is not None and value == existing.strip():
            return existing  # Unchanged historical overflow is not a new grade.
        raise ValueError(f"Condition must be {CONDITION_MAX_LENGTH} characters or fewer")
    return value


def _condition_choices(copies, *extra) -> tuple:
    """Offer both preset vocabularies AND every existing/unsaved custom grade."""
    choices = list(CONDITIONS)
    seen = {value for value, _label in choices}
    for value in [*(copy["condition"] for copy in copies), *extra]:
        if value and value not in seen:
            choices.append((value, value))
            seen.add(value)
    return tuple(choices)


def _item_with_location(db, item_id: int):
    return db.execute(
        "SELECT i.*, l.name AS location_name FROM items_live i "
        "LEFT JOIN locations l ON l.id = i.location_id WHERE i.id = ?", (item_id,)
    ).fetchone()


def _all_locations(db):
    # These selectors edit copies, not items. Every value is a NODE ID.
    return location_svc.location_tree(db)


def _copy_context(db, item_id: int, *, message: str = "", error: str = "") -> dict:
    item = _item_with_location(db, item_id)
    physical = bool(item and item["owned"] and is_physical_media(item["media_type"]))
    nodes = _all_locations(db) if physical else []
    can_edit = item is not None
    if can_edit and current_actor() is not None:
        try:
            require_item_role(db, item_id, "editor")
        except HTTPException as exc:
            if exc.status_code not in (403, 404):
                raise
            can_edit = False
    copies = item_copies.copies_for_item(db, item_id) if item else []
    can_remove = can_edit and (not physical or len(copies) > 1)
    for copy in copies:
        copy["can_remove_copy"] = can_remove
    return {
        "missing": item is None, "item": item, "item_id": item_id,
        "is_physical_holding": physical,
        "copies": copies, "can_edit_item": can_edit, "can_remove_copy": can_remove,
        "locations": nodes, "location_nodes": nodes, "conditions": _condition_choices(copies),
        "condition_presets": CONDITIONS, "condition_max_length": CONDITION_MAX_LENGTH,
        "message": message, "copy_error": error, "error": error,
    }


def _render_block(request: Request, db, item_id: int, *, message: str = "", error: str = "",
                  unsaved: dict | None = None):
    context = _copy_context(db, item_id, message=message, error=error)
    context["unsaved_copy"] = unsaved or {}
    return request.app.state.templates.TemplateResponse(
        request, "fragments/item_copies.html", context,
    )


def _require_parent(db, item_id: int, role: str):
    """Keep parent404 distinct from correctable child200, without disclosure."""
    try:
        return require_item_role(db, item_id, role)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(404, "Item not found", headers={
                "HX-Trigger": items_common._toast_header("Item not found", "error"),
            }) from exc
        raise


def _render(request: Request, item_id: int, *, message: str = "", error: str = ""):
    with get_db() as db:
        _require_parent(db, item_id, "viewer")
        return _render_block(request, db, item_id, message=message, error=error)


def _barcode_conflict(db, barcode: str, copy_id: int | None):
    # Physical table solely to predict UNIQUE, including hidden/trashed rows.
    # A boolean only: no cross-library identifying data enters the response.
    return db.execute(
        "SELECT 1 FROM item_copies WHERE copy_barcode = ? AND id IS NOT ? LIMIT 1",
        (barcode, copy_id),
    ).fetchone() is not None


def _barcode_taken_message(_row=None) -> str:
    return "That copy barcode is already in use"


def _refusal(request: Request, db, item_id: int, message: str, *,
             copy_id: int | None = None, unsaved: dict | None = None):
    """A real, complete #item-copies fragment, safe for repeated outerHTML swaps."""
    if copy_id is not None:
        response = _render_edit_panel(request, db, item_id, copy_id, unsaved, error=message)
    else:
        response = _render_block(request, db, item_id, error=message, unsaved=unsaved)
    response.headers["HX-Trigger"] = items_common._toast_header(message, "error")
    return response


def _render_edit_panel(request: Request, db, item_id: int, copy_id: int,
                       unsaved: dict | None = None, *, error: str = ""):
    _require_parent(db, item_id, "editor")
    copy = db.execute(
        "SELECT * FROM copies_live WHERE id = ? AND item_id = ?", (copy_id, item_id)
    ).fetchone() if _copy_id_in_bounds(copy_id) else None
    if copy is None:
        return _refusal(request, db, item_id, error or "Copy not found")
    context = _copy_context(db, item_id, error=error)
    context["copy"] = {**dict(copy), **(unsaved or {})}
    context["conditions"] = _condition_choices(context["copies"], context["copy"].get("condition"))
    return request.app.state.templates.TemplateResponse(
        request, "fragments/item_copy_edit.html", context
    )


def _unsaved_fields(form) -> dict:
    """Only editable values may overlay a re-render; never IDs/primary state."""
    unsaved = {name: form[name] for name in _EDITABLE if name in form and isinstance(form[name], str)}
    if "location_id" in form:
        raw = form["location_id"]
        try:
            unsaved["location_id"] = int(raw) if isinstance(raw, str) and raw.isascii() and raw.isdigit() else None
        except ValueError:
            # Re-rendering must not fail before the normal location validator
            # can report a malformed/oversized submitted integer.
            unsaved["location_id"] = None
    return unsaved


def _fields(db, location_id, condition, notes, acquired_date, acquisition_source,
            acquisition_price, provenance, copy_barcode, *, existing_condition=None):
    return {
        "location_id": _clean_location(db, location_id),
        "condition": _clean_condition(condition, existing=existing_condition), "notes": _clean(notes),
        "acquired_date": _clean_date(acquired_date),
        "acquisition_source": _clean(acquisition_source),
        "acquisition_price": _clean_price(acquisition_price),
        "provenance": _clean(provenance), "copy_barcode": _clean(copy_barcode),
    }


@router.get("/items/{item_id}/copies")
async def collapsed_block(request: Request, item_id: int, _=Depends(require_role("viewer"))):
    return _render(request, item_id)


@router.get("/items/{item_id}/copies/{copy_id}/edit")
async def edit_panel(request: Request, item_id: int, copy_id: int,
                     _=Depends(require_role("editor"))):
    with get_db() as db:
        return _render_edit_panel(request, db, item_id, copy_id)


@router.post("/items/{item_id}/copies")
async def add_copy(
    request: Request, item_id: int, location_id: str = Form(""),
    condition: str = Form(""), notes: str = Form(""), acquired_date: str = Form(""),
    acquisition_source: str = Form(""), acquisition_price: str = Form(""),
    provenance: str = Form(""), copy_barcode: str = Form(""),
    _=Depends(require_role("editor")),
):
    error, number = "", None
    unsaved = _unsaved_fields(await request.form())
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_parent(db, item_id, "editor")
        try:
            fields = _fields(db, location_id, condition, notes, acquired_date,
                             acquisition_source, acquisition_price, provenance, copy_barcode)
            if fields["copy_barcode"] and _barcode_conflict(db, fields["copy_barcode"], None):
                raise ValueError(_barcode_taken_message())
            copy_id = item_copies.add_copy(db, item_id, fields)
            number = db.execute("SELECT copy_number FROM copies_live WHERE id = ?", (copy_id,)).fetchone()[0]
        except sqlite3.IntegrityError:
            db.rollback()
            error = _barcode_taken_message()
        except ValueError as exc:
            db.rollback()
            error = str(exc)
        if not error:
            return _render_block(request, db, item_id, message=f"Copy {number} added")
        response = _refusal(request, db, item_id, error, unsaved=unsaved)
    # SQLiteHandler writes on a second connection. Roll back AND leave the
    # connection block before logging, or the record blocks on our own lock.
    logger.warning("Copy add refused for item %s: %s", item_id, error)
    return response


@router.post("/items/{item_id}/copies/{copy_id}")
async def update_copy(
    request: Request, item_id: int, copy_id: int, location_id: str = Form(""),
    condition: str = Form(""), notes: str = Form(""), acquired_date: str = Form(""),
    acquisition_source: str = Form(""), acquisition_price: str = Form(""),
    provenance: str = Form(""), copy_barcode: str = Form(""),
    _=Depends(require_role("editor")),
):
    error = ""
    submitted = await request.form()
    unsaved = _unsaved_fields(submitted)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_parent(db, item_id, "editor")
        try:
            copy = db.execute("SELECT condition FROM copies_live WHERE id = ? AND item_id = ?",
                              (copy_id, item_id)).fetchone() if _copy_id_in_bounds(copy_id) else None
            if copy is None:
                raise ValueError("Copy not found")
            fields = _fields(db, location_id, condition, notes, acquired_date,
                             acquisition_source, acquisition_price, provenance, copy_barcode,
                             existing_condition=copy["condition"])
            # Older fork forms do not know provenance/acquisition_source;
            # modern forms may omit notes. Omission is not a request to erase.
            fields = {name: value for name, value in fields.items() if name in submitted}
            if fields.get("copy_barcode") and _barcode_conflict(db, fields["copy_barcode"], copy_id):
                raise ValueError(_barcode_taken_message())
            item_copies.update_copy(db, copy_id, fields)
        except sqlite3.IntegrityError:
            db.rollback()
            error = _barcode_taken_message()
        except ValueError as exc:
            db.rollback()
            error = str(exc)
        if not error:
            return _render_block(request, db, item_id, message="Copy updated")
        response = _refusal(request, db, item_id, error, copy_id=copy_id, unsaved=unsaved)
    logger.warning("Copy update refused for copy %s: %s", copy_id, error)
    return response


@router.delete("/items/{item_id}/copies/{copy_id}")
async def remove_copy(request: Request, item_id: int, copy_id: int,
                      _=Depends(require_role("editor"))):
    error, number = "", None
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_parent(db, item_id, "editor")
        try:
            row = db.execute("SELECT copy_number FROM copies_live WHERE id = ? AND item_id = ?",
                             (copy_id, item_id)).fetchone() if _copy_id_in_bounds(copy_id) else None
            if row is None:
                raise ValueError("Copy not found")
            number = row["copy_number"]
            item_copies.trash_copy(db, copy_id)
        except ValueError as exc:
            db.rollback()
            error = str(exc)
        if error:
            return _refusal(request, db, item_id, error)
        return _render_block(request, db, item_id, message=f"Copy {number} removed")


@router.post("/items/{item_id}/copies/{copy_id}/found")
async def copy_found(request: Request, item_id: int, copy_id: int,
                     _=Depends(require_role("editor"))):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_parent(db, item_id, "editor")
        if not _copy_id_in_bounds(copy_id) or db.execute(
            "SELECT 1 FROM copies_live WHERE id = ? AND item_id = ?", (copy_id, item_id)
        ).fetchone() is None:
            db.rollback()
            return _refusal(request, db, item_id, "Copy not found")
        item_copies.clear_missing(db, copy_id)
        return _render_block(request, db, item_id)


# Established main Python imports, without duplicate route registration.
copy_context = _copy_context
item_copies_fragment = collapsed_block
add_item_copy = add_copy
update_item_copy = update_copy
delete_item_copy = remove_copy
