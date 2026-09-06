"""Catalogue preflight retained across the upstream-0.34 integration."""

from fastapi import Depends, Form, Request

from app.auth import require_role
from app.database import get_db
from app.routers import items_catalog
from app.services.item_write import ItemValueError, validated_location_id


_original_add_game = items_catalog.add_game_from_search

items_catalog.router.routes[:] = [
    route
    for route in items_catalog.router.routes
    if not (
        getattr(route, "path", None) == "/api/games/add"
        and "POST" in (getattr(route, "methods", None) or set())
    )
]


@items_catalog.router.post("/games/add")
async def add_game_with_location_preflight(
    request: Request,
    igdb_id: int = Form(...),
    platform: str = Form(""),
    location_id: int | None = Form(None),
    _=Depends(require_role("editor")),
):
    templates = request.app.state.templates
    try:
        with get_db() as db:
            location_id = validated_location_id(db, location_id)
    except ItemValueError as exc:
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {"status": "error", "isbn": "", "message": str(exc)},
        )

    return await _original_add_game(
        request,
        igdb_id=igdb_id,
        platform=platform,
        location_id=location_id,
        _=request.state.user,
    )
