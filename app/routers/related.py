from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse

from app.auth import require_role
from app.config import MEDIA_TYPES
from app.database import get_db, get_game_platforms
from app.services import media_groups

router = APIRouter(prefix="/api/items")


@router.post("/connections/rebuild")
async def rebuild_connected_items(
    request: Request,
    _=Depends(require_role("admin")),
):
    """Retroactively create safe automatic format connections.

    Uses the same conservative matching rules as normal inserts and provider
    syncs, so this is safe to run repeatedly against an existing library.
    """
    with get_db() as db:
        result = media_groups.rebuild_automatic_connections(db)
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/settings/connection_rebuild.html",
        {"connection_rebuild_result": result},
    )


@router.get("/{item_id}/related/search")
async def search_related_media(
    request: Request,
    item_id: int,
    q: str = Query("", max_length=200),
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        item = db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            return request.app.state.templates.TemplateResponse(
                request,
                "fragments/related_search_results.html",
                {"item_id": item_id, "candidates": [], "media_types": MEDIA_TYPES,
                 "game_platforms": {}},
                status_code=404,
            )
        candidates = media_groups.search_candidates(db, item_id, q)
        game_platforms = get_game_platforms(db)
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/related_search_results.html",
        {
            "item_id": item_id,
            "candidates": candidates,
            "media_types": MEDIA_TYPES,
            "game_platforms": game_platforms,
        },
    )


@router.post("/{item_id}/related/{other_id}")
async def add_related_media(
    item_id: int,
    other_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        media_groups.link_items(db, item_id, other_id, "related")
    return RedirectResponse(url=f"/item/{item_id}", status_code=303)


@router.post("/{item_id}/related/{other_id}/remove")
async def remove_related_media(
    item_id: int,
    other_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        media_groups.remove_manual_group_edges(db, item_id, other_id)
    return RedirectResponse(url=f"/item/{item_id}", status_code=303)
