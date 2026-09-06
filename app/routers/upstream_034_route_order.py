"""Final route/function wiring for the upstream-0.34 integration.

Some fork extensions replace routes by removing and re-registering them.  That
can move a dynamic ``/items/{item_id}`` route ahead of static siblings such as
``/items/merge``.  It also leaves older extensions holding the original
``items.update_item`` function object.  Keep the final wiring explicit here
until these adapters are folded into their owning routers.
"""

from fastapi import Depends, Query, Request

from app.auth import require_role
from app.routers import item_barcode_edit, items, pages, upstream_034_compat


# Barcode editing delegates through ``items.update_item`` at request time.
# Point that name at the integrated boundary so /items/{id}/edit gets the same
# validation guarantees as the legacy /items/{id} endpoint.
items.update_item = upstream_034_compat.integrated_safe_update_item
items.merge_items = upstream_034_compat.integrated_merge_items


# Static routes must be tested before the integer catch-all route.  Replacing
# /items/{item_id} late in import order moved it ahead of /items/merge.
def _promote_static_route(path: str, method: str) -> None:
    method = method.upper()
    matches = [
        route
        for route in items.router.routes
        if getattr(route, "path", None) == path
        and method in (getattr(route, "methods", None) or set())
    ]
    if not matches:
        return
    items.router.routes[:] = [route for route in items.router.routes if route not in matches]
    catch_index = next(
        (
            index
            for index, route in enumerate(items.router.routes)
            if getattr(route, "path", None) == "/api/items/{item_id}"
            and method in (getattr(route, "methods", None) or set())
        ),
        len(items.router.routes),
    )
    for route in reversed(matches):
        items.router.routes.insert(catch_index, route)


_promote_static_route("/api/items/merge", "POST")


# item_barcode_edit replaces the edit GET route to add magazine barcode
# context.  Upstream 0.34 added an ``error`` query parameter for value-funnel
# banners afterwards; preserve both by wrapping the barcode-aware renderer.
pages.router.routes[:] = [
    route
    for route in pages.router.routes
    if not (
        getattr(route, "path", None) == "/item/{item_id}/edit"
        and "GET" in (getattr(route, "methods", None) or set())
    )
]


@pages.router.get("/item/{item_id}/edit")
async def item_edit_with_barcode_and_error_context(
    request: Request,
    item_id: int,
    from_: str = Query("", alias="from"),
    error: str | None = Query(None),
    _=Depends(require_role("editor")),
):
    response = await item_barcode_edit.item_edit_with_barcode_context(
        request,
        item_id,
        from_=from_,
        _=request.state.user,
    )
    context = getattr(response, "context", None)
    if context is not None:
        context["error"] = error
    return response
