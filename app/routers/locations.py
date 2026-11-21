"""Main's LEGACY-flat Settings routes. Node administration is location_tree.

The path IDs below are never interpreted as location_nodes IDs. A nonempty
parent on the legacy form is refused rather than accidentally reparenting an
equal-numbered node; hierarchical forms post to /api/location-tree instead.
"""

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.services import locations as location_svc

router = APIRouter(prefix="/api/locations", dependencies=[Depends(require_role("admin"))])


def _settings_error(code: str) -> RedirectResponse:
    return RedirectResponse(url=f"/settings?location_error={code}", status_code=303)


@router.post("")
async def create_location(name: str = Form(...), sort_order: int = Form(0),
                          parent_id: str = Form("")):
    if parent_id.strip() not in ("", "0"):
        return _settings_error("invalid_parent")
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            location_svc.create_legacy_location(db, name, sort_order=sort_order)
    except location_svc.DuplicateLocation:
        return _settings_error("duplicate")
    except location_svc.LocationError:
        return _settings_error("blank")
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/{location_id}/update")
async def update_location(location_id: int, name: str = Form(...),
                          sort_order: int = Form(0), parent_id: str = Form("")):
    if parent_id.strip() not in ("", "0"):
        return _settings_error("invalid_parent")
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            location_svc.update_legacy_location(db, location_id, name, sort_order=sort_order)
    except location_svc.DuplicateLocation:
        return _settings_error("duplicate")
    except location_svc.LocationNotFound:
        return _settings_error("missing")
    except location_svc.LocationError:
        return _settings_error("blank")
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/{location_id}/delete")
async def delete_location(location_id: int):
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            location_svc.delete_legacy_location(db, location_id)
    except location_svc.LocationNotFound:
        return _settings_error("missing")
    return RedirectResponse(url="/settings", status_code=303)
