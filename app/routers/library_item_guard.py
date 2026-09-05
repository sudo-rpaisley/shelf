"""Final ACL guard for personal item-state routes.

Per-user state was introduced through two extension generations: the original
``pages.router`` routes and the later ``items.router`` compatibility routes.
Both still exist on upgraded code, so replacing only one router leaves an
earlier matching endpoint that FastAPI can dispatch before the ACL-aware one.
This module deliberately removes *all* registrations for these paths and adds
one guarded implementation after the other route extensions have loaded.
"""

from __future__ import annotations

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.auth import require_role
from app.config import MEDIA_TYPES
from app.database import get_db
from app.routers import items, pages, user_state_items
from app.services import libraries, user_state


def _remove_route(router, path: str, method: str) -> None:
    """Remove a route whether FastAPI stored it with or without router prefix."""
    method = method.upper()
    candidates = {path}
    if getattr(router, "prefix", ""):
        candidates.add(f"{router.prefix}{path}")
    router.routes[:] = [
        route
        for route in router.routes
        if not (
            getattr(route, "path", None) in candidates
            and method in (getattr(route, "methods", None) or set())
        )
    ]


def _user(request: Request) -> dict:
    return dict(request.state.user)


def _allowed(request: Request, item_id: int) -> bool:
    with get_db() as db:
        return libraries.has_item_role(db, _user(request), item_id, "viewer")


# Remove both generations of personal-state routes before registering the one
# authoritative ACL-aware copy on items.router.
for _router in (pages.router, items.router):
    _remove_route(_router, "/api/items/{item_id}/personal-state", "GET")
    _remove_route(_router, "/api/items/{item_id}/personal-state", "POST")
    _remove_route(_router, "/api/home/personal-in-progress", "GET")
    _remove_route(_router, "/items/{item_id}/personal-state", "GET")
    _remove_route(_router, "/items/{item_id}/personal-state", "POST")
    _remove_route(_router, "/home/personal-in-progress", "GET")

_remove_route(items.router, "/items/{item_id}/reading-status", "POST")


@items.router.get("/items/{item_id}/personal-state")
async def guarded_personal_state_get(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    if not _allowed(request, item_id):
        return HTMLResponse("Not found", status_code=404)
    return await user_state_items.personal_state_fragment(
        request, item_id, _=request.state.user
    )


@items.router.post("/items/{item_id}/personal-state")
async def guarded_personal_state_post(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    if not _allowed(request, item_id):
        return HTMLResponse("Not found", status_code=404)
    return await user_state_items.update_personal_state(
        request, item_id, _=request.state.user
    )


@items.router.post("/items/{item_id}/reading-status")
async def guarded_legacy_reading_status(
    request: Request,
    item_id: int,
    status: str = Form(""),
    _=Depends(require_role("viewer")),
):
    if not _allowed(request, item_id):
        return HTMLResponse("Not found", status_code=404)
    return await user_state_items.personal_legacy_reading_status(
        request,
        item_id,
        status=status,
        _=request.state.user,
    )


@items.router.get("/home/personal-in-progress")
async def guarded_personal_in_progress(
    request: Request,
    _=Depends(require_role("viewer")),
):
    user = _user(request)
    user_id = int(user["id"])
    condition, params = libraries.item_access_condition(user, item_alias="i")
    with get_db() as db:
        user_state.ensure_schema(db)
        rows = db.execute(
            """SELECT i.id, i.title, i.authors, i.media_type, i.cover_path,
                      uis.progress_value, uis.progress_total, uis.progress_unit
                 FROM user_item_state uis
                 JOIN items i ON i.id = uis.item_id
                WHERE uis.user_id = ? AND uis.reading_status = 'reading'
                  AND (""" + condition + ") "
                "ORDER BY uis.updated_at DESC, i.id DESC LIMIT 6",
            [user_id] + params,
        ).fetchall()

    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/personal_in_progress.html",
        {"in_progress": rows, "media_types": MEDIA_TYPES},
    )
