"""Explicit editor control over refilling deliberately removed artwork."""

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import i18n
from app.auth import require_role
from app.database import get_db
from app.routers import items_common
from app.services import cover_sync_policy


router = APIRouter()


@router.post("/api/items/{item_id}/cover-sync/allow")
async def allow_cover_sync(
    request: Request,
    item_id: int = Path(..., gt=0, le=2**63 - 1),
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if not cover_sync_policy.allow(db, item_id):
            raise HTTPException(404, i18n.gettext("Item not found"))
    target = f"/item/{item_id}"
    if request.headers.get("HX-Request", "").lower() == "true":
        return HTMLResponse("", headers={
            "HX-Redirect": target,
            "HX-Trigger": items_common._toast_header(i18n.gettext("Missing-cover sync is allowed again")),
        })
    return RedirectResponse(target, status_code=303)
