from html import escape

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_role
from app.config import MEDIA_TYPES
from app.database import get_db, get_game_platforms
from app.services import item_write, media_groups
from app.services.catalogue_security import require_catalogue_role, require_item_role
from app.routers.related_media import _editable_candidates
from app.services import series_memberships as series_memberships_svc

router = APIRouter(prefix="/api/items")


@router.post("/connections/rebuild")
async def rebuild_connected_items(
    request: Request,
    _=Depends(require_role("admin")),
):
    """Retroactively create safe automatic connections and series rows.

    Connected-item matching uses the same conservative rules as normal inserts
    and provider syncs. Series reconciliation is metadata-only: any existing
    ``series_name``/``series_position`` values are promoted into ``item_series``
    so the Series page and item rows appear immediately. Neither operation
    contacts a metadata provider, and both are safe to run repeatedly.
    """
    with get_db() as db, item_write._write_scope(db):
        # This is deliberately global-admin maintenance, not a library editor
        # action. Even an unmapped live row must refuse rather than be skipped.
        for row in db.execute("SELECT id FROM items WHERE deleted_at IS NULL ORDER BY id").fetchall():
            require_item_role(db, row["id"], "editor")
        result = media_groups.rebuild_automatic_connections(db)
        result["series_sync"] = series_memberships_svc.sync_all_legacy(db)
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
    _=Depends(require_catalogue_role("editor")),
):
    with get_db() as db:
        item = db.execute("SELECT id FROM items_live WHERE id = ?", (item_id,)).fetchone()
        if not item:
            return request.app.state.templates.TemplateResponse(
                request,
                "fragments/related_search_results.html",
                {"item_id": item_id, "candidates": [], "media_types": MEDIA_TYPES,
                 "game_platforms": {}},
                status_code=404,
            )
        require_item_role(db, item_id, "editor")
        candidates = _editable_candidates(db, item_id, q)
        game_platforms = get_game_platforms(db)
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/related_search_results.html",
        {
            "item_id": item_id,
            "candidates": candidates,
            "media_types": MEDIA_TYPES,
            "game_platforms": game_platforms,
            "can_edit_item": True,
        },
    )


@router.post("/{item_id}/related/{other_id}")
async def add_related_media(
    item_id: int,
    other_id: int,
    _=Depends(require_catalogue_role("editor")),
):
    try:
        with get_db() as db, item_write._write_scope(db):
            require_item_role(db, item_id, "editor")
            require_item_role(db, other_id, "editor")
            if item_id == other_id:
                return HTMLResponse("An item cannot be related to itself", status_code=400)
            media_groups.link_items(db, item_id, other_id, "related")
    except item_write.ItemValueError as exc:
        return HTMLResponse(escape(str(exc)), status_code=400)
    return RedirectResponse(url=f"/item/{item_id}", status_code=303)


def manual_unlink_parent_ids(db, item_id: int, other_id: int) -> set[int]:
    """Exact shared-effect parents; never return these physical IDs to clients."""
    return {row["id"] for row in db.execute(
        """WITH RECURSIVE connected(id) AS (
               SELECT id FROM items WHERE id = ? AND deleted_at IS NULL
               UNION
               SELECT i.id FROM item_links il JOIN connected c
                 ON il.item_a_id = c.id OR il.item_b_id = c.id
               JOIN items i ON i.id = CASE WHEN il.item_a_id = c.id
                 THEN il.item_b_id ELSE il.item_a_id END
               WHERE i.deleted_at IS NULL
           )
           SELECT CASE WHEN il.item_a_id = ? THEN il.item_b_id ELSE il.item_a_id END AS id
           FROM item_links il
           WHERE il.link_type = 'related'
             AND (il.item_a_id = ? OR il.item_b_id = ?)
             AND il.item_a_id IN (SELECT id FROM connected)
             AND il.item_b_id IN (SELECT id FROM connected)
           ORDER BY id""",
        (item_id, other_id, other_id, other_id),
    ).fetchall()}


@router.post("/{item_id}/related/{other_id}/remove")
async def remove_related_media(
    item_id: int,
    other_id: int,
    _=Depends(require_catalogue_role("editor")),
):
    with get_db() as db, item_write._write_scope(db):
        require_item_role(db, item_id, "editor")
        require_item_role(db, other_id, "editor")
        # The fork action removes the target's manual edges into the whole
        # component, not just its direct edge to the anchor. Enumerate the
        # physical live component for preflight: scoped traversal could omit
        # a hidden edge parent and falsely report a successful partial detach.
        parents = manual_unlink_parent_ids(db, item_id, other_id)
        for parent_id in sorted(parents):
            require_item_role(db, parent_id, "editor")
        for parent_id in sorted(parents):
            a, b = sorted((other_id, parent_id))
            db.execute(
                "DELETE FROM item_links WHERE item_a_id = ? AND item_b_id = ? AND link_type = 'related'",
                (a, b),
            )
    return RedirectResponse(url=f"/item/{item_id}", status_code=303)
