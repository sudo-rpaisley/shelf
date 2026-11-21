"""Browse and arrange physical copies stored at one location."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.services.catalogue_security import require_catalogue_role as require_role
from app.database import get_db
from app.services import libraries
from app.services import location_order as order_svc

router = APIRouter()


def _can_edit_copies(db, user, copies) -> bool:
    """Match the ordering API's authority over ALL visible parents, not a role badge."""
    parents = {copy["item_id"] for copy in copies}
    if parents:
        return all(libraries.has_item_role(db, user, item_id, "editor") for item_id in parents)
    # An empty location has no item targets. Preserve the editor affordance only
    # for actual library editors; token caps still apply in has_library_role.
    return any(libraries.has_library_role(db, user, library_id, "editor")
               for library_id in libraries.accessible_library_ids(db, user))


@router.get("/locations/{location_id}/arrange")
async def arrange_location(
    request: Request,
    location_id: int,
    user=Depends(require_role("viewer")),
):
    with get_db() as db:
        location = db.execute(
            "SELECT id, name, name AS label, parent_id, legacy_location_id FROM location_nodes WHERE id = ?",
            (location_id,),
        ).fetchone()
        if not location:
            return RedirectResponse(url="/settings?location_error=missing", status_code=303)
        children = db.execute(
            "SELECT id, name, name AS label FROM location_nodes WHERE parent_id = ? "
            "ORDER BY sort_order, name COLLATE NOCASE, id",
            (location_id,),
        ).fetchall()
        copies = order_svc.direct_copies(db, location_id)
        can_edit = _can_edit_copies(db, user, copies)

    return request.app.state.templates.TemplateResponse(
        request,
        "location_order.html",
        {
            "location": dict(location),
            "children": [dict(row) for row in children],
            "copies": copies,
            "can_edit": can_edit,
            "selected_location": dict(location),
            "child_locations": [dict(row) for row in children],
            "location_copies": copies,
        },
    )


def _integer_ids(value) -> list[int] | None:
    if not isinstance(value, list):
        return None
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        return None
    return value


@router.post("/api/locations/{location_id}/order")
async def save_location_order(
    request: Request,
    location_id: int,
    _=Depends(require_role("editor")),
):
    try:
        body = await request.json()
    except Exception:
        body = {}
    copy_ids = _integer_ids(body.get("copy_ids") if isinstance(body, dict) else None)
    if copy_ids is None:
        return JSONResponse({"ok": False, "message": "Invalid copy order"}, status_code=400)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM location_nodes WHERE id = ?", (location_id,)).fetchone():
            return JSONResponse({"ok": False, "message": "Location not found"}, status_code=404)
        try:
            order_svc.apply_copy_order(db, location_id, copy_ids)
        except ValueError as exc:
            db.rollback()
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
    return {"ok": True, "copy_ids": copy_ids}


@router.post("/api/locations/{location_id}/auto-order")
async def auto_location_order(
    request: Request,
    location_id: int,
    _=Depends(require_role("editor")),
):
    try:
        body = await request.json()
    except Exception:
        body = {}
    sort_key = body.get("sort_key") if isinstance(body, dict) else None
    if not isinstance(sort_key, str):
        return JSONResponse({"ok": False, "message": "Invalid sort order"}, status_code=400)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM location_nodes WHERE id = ?", (location_id,)).fetchone():
            return JSONResponse({"ok": False, "message": "Location not found"}, status_code=404)
        try:
            ids = order_svc.auto_order_copies(db, location_id, sort_key)
        except ValueError as exc:
            db.rollback()
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    return {"ok": True, "copy_ids": ids}
