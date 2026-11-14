"""Browse and arrange physical copies stored at one location."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import require_role
from app.database import get_db
from app import nav
from app.config import MEDIA_TYPES
from app.services import copy_locator
from app.services import location_order as order_svc

router = APIRouter()


@router.get("/locations/{location_id}/arrange")
async def arrange_location(
    request: Request,
    location_id: int,
    user=Depends(require_role("viewer")),
):
    with get_db() as db:
        location = db.execute(
            "SELECT id, name, label, parent_id FROM locations WHERE id = ?",
            (location_id,),
        ).fetchone()
        if not location:
            return RedirectResponse(url="/settings?location_error=missing", status_code=303)
        children = db.execute(
            "SELECT id, name, label FROM locations WHERE parent_id = ? "
            "ORDER BY sort_order, lower(COALESCE(label, name)), id",
            (location_id,),
        ).fetchall()
        copies = order_svc.direct_copies(db, location_id)

    return request.app.state.templates.TemplateResponse(
        request,
        "location_order.html",
        {
            "location": dict(location),
            "children": [dict(row) for row in children],
            "copies": copies,
            "can_edit": user["role"] in {"admin", "editor"},
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
        if not db.execute("SELECT 1 FROM locations WHERE id = ?", (location_id,)).fetchone():
            return JSONResponse({"ok": False, "message": "Location not found"}, status_code=404)
        try:
            order_svc.apply_copy_order(db, location_id, copy_ids)
        except ValueError as exc:
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
        if not db.execute("SELECT 1 FROM locations WHERE id = ?", (location_id,)).fetchone():
            return JSONResponse({"ok": False, "message": "Location not found"}, status_code=404)
        try:
            ids = order_svc.auto_order_copies(db, location_id, sort_key)
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    return {"ok": True, "copy_ids": ids}


@router.get("/item/{item_id}/copies/{copy_id}/locate")
async def locate_physical_copy(
    request: Request,
    item_id: int,
    copy_id: int,
    at: int | None = Query(None, ge=0, le=copy_locator.MAX_LOCATION_ID),
    from_: str = Query("", alias="from"),
    _=Depends(require_role("viewer")),
):
    back = nav.back_target(from_)
    with get_db() as db:
        # A deferred read transaction gives all reads one snapshot, without a
        # write lock, ordering repair, missing-clear or any other mutation.
        db.execute("BEGIN")
        context = copy_locator.locate_copy(db, item_id, copy_id, at=at)
    if context is None:
        raise HTTPException(status_code=404, detail="Copy not found")
    return request.app.state.templates.TemplateResponse(
        request, "copy_locator.html", {**context, "back": back, "media_types": MEDIA_TYPES},
    )
