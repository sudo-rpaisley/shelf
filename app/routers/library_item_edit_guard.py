"""Preserve barcode-aware item editing behind the library ACL.

The broad library item-access adapter predates the magazine barcode editor and
captures the original item-edit handler. This final focused replacement keeps
the library Editor boundary while delegating rendering to the barcode-aware
edit handler registered by :mod:`app.routers.item_barcode_edit`.
"""

from fastapi import Depends, Query, Request
from fastapi.responses import RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.routers import item_barcode_edit, pages
from app.services import libraries


def _remove_edit_route() -> None:
    pages.router.routes[:] = [
        route
        for route in pages.router.routes
        if not (
            getattr(route, "path", None) == "/item/{item_id}/edit"
            and "GET" in (getattr(route, "methods", None) or set())
        )
    ]


_remove_edit_route()


@pages.router.get("/item/{item_id}/edit")
async def library_barcode_item_edit(
    request: Request,
    item_id: int,
    from_: str = Query("", alias="from"),
    _=Depends(require_role("viewer")),
):
    """Render the barcode-aware edit form only for an item-library Editor."""
    user = dict(request.state.user)
    with get_db() as db:
        if not libraries.has_item_role(db, user, item_id, "editor"):
            return RedirectResponse(url="/browse", status_code=303)

    return await item_barcode_edit.item_edit_with_barcode_context(
        request,
        item_id,
        from_=from_,
        _=request.state.user,
    )
