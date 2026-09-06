"""Preserve 0.34 reading-status error detail with per-user state."""

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.auth import require_role
from app.routers import items, user_state_items


items.router.routes[:] = [
    route
    for route in items.router.routes
    if not (
        getattr(route, "path", None) == "/api/items/{item_id}/reading-status"
        and "POST" in (getattr(route, "methods", None) or set())
    )
]


@items.router.post("/items/{item_id}/reading-status")
async def integrated_personal_reading_status(
    request: Request,
    item_id: int,
    status: str = Form(""),
    _=Depends(require_role("viewer")),
):
    if status not in ("want_to_read", "reading", "read", ""):
        return HTMLResponse(f"Invalid reading status: {status!r}", status_code=400)
    return await user_state_items.personal_legacy_reading_status(
        request,
        item_id,
        status=status,
        _=request.state.user,
    )
