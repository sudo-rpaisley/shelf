"""Manual-first Related Media UI over Shelf's existing item-links graph."""

from html import escape

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse

from app import i18n, nav
from app.auth import require_role
from app.config import MEDIA_TYPES
from app.database import get_db
from app.services import catalogue_security, item_write, libraries, media_groups
from app.services.catalogue_security import require_catalogue_role, require_item_role

router = APIRouter()


def _panel_context(db, item_id: int) -> dict | None:
    item = db.execute(
        "SELECT id, title, media_type FROM items_live WHERE id = ?", (item_id,)
    ).fetchone()
    if not item:
        return None

    user = catalogue_security.current_actor()
    can_edit = bool(user) and libraries.has_item_role(db, user, item_id, "editor")
    direct_rows = media_groups.direct_links(db, item_id)
    direct = {row["item_id"]: row for row in direct_rows}
    related = []
    for row in media_groups.related_items(db, item_id):
        value = dict(row)
        edge = direct.get(row["id"])
        value["direct"] = edge is not None
        value["link_type"] = edge["link_type"] if edge else None
        value["can_edit_item"] = bool(user) and libraries.has_item_role(db, user, row["id"], "editor")
        value["can_unlink"] = can_edit and value["can_edit_item"] and edge is not None
        related.append(value)
    # Only direct format edges claim the same work in another format; neither
    # adaptations nor transitive neighbours belong in "Also available as".
    linked_items = [
        {"id": row["item_id"], "media_type": row["media_type"]}
        for row in direct_rows if row["link_type"] == "format"
    ]
    return {
        "item": item, "related_items": related, "linked_items": linked_items,
        "can_edit": can_edit, "can_edit_item": can_edit,
    }


def _render_panel(request: Request, item_id: int, *, refresh_also_available: bool = False):
    with get_db() as db:
        context = _panel_context(db, item_id)
    if context is None:
        return HTMLResponse(i18n.gettext("Item not found"), status_code=404)
    context.update({
        "media_types": MEDIA_TYPES,
        "back": nav.back_target(request.query_params.get("from")),
        "also_available_oob": refresh_also_available,
    })
    return request.app.state.templates.TemplateResponse(
        request, "fragments/related_media_panel.html", context
    )


def _editable_candidates(db, item_id: int, query: str) -> list[dict]:
    """Both search surfaces offer only targets the anchor editor can link."""
    candidates = media_groups.search_candidates(db, item_id, query, limit=100)
    condition, params = catalogue_security.access_condition(role="editor")
    editable = {row["id"] for row in db.execute(
        f"SELECT i.id FROM items_live i WHERE {condition}", params,
    )}
    return [dict(row, can_edit_item=True) for row in candidates if row["id"] in editable][:20]


@router.get("/api/related-media/items/{item_id}/panel")
async def related_media_panel(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    return _render_panel(request, item_id)


@router.get("/api/related-media/items/{item_id}/search")
async def related_media_search(
    request: Request,
    item_id: int,
    q: str = Query("", max_length=120),
    _=Depends(require_catalogue_role("editor")),
):
    with get_db() as db:
        if not db.execute("SELECT 1 FROM items_live WHERE id = ?", (item_id,)).fetchone():
            return HTMLResponse(i18n.gettext("Item not found"), status_code=404)
        require_item_role(db, item_id, "editor")
        candidates = _editable_candidates(db, item_id, q)
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/related_media_search.html",
        {"item_id": item_id, "candidates": candidates, "query": q.strip(),
         "back": nav.back_target(request.query_params.get("from"))},
    )


@router.post("/api/related-media/items/{item_id}/links")
async def related_media_link(
    request: Request,
    item_id: int,
    other_item_id: int = Form(...),
    link_type: str = Form("related"),
    _=Depends(require_catalogue_role("editor")),
):
    if item_id == other_item_id:
        return HTMLResponse(i18n.gettext("An item cannot be related to itself"), status_code=400)

    try:
        with get_db() as db, item_write._write_scope(db):
            require_item_role(db, item_id, "editor")
            require_item_role(db, other_item_id, "editor")
            media_groups.link_items(db, item_id, other_item_id, link_type=link_type)
    except ValueError as exc:
        return HTMLResponse(escape(str(exc)), status_code=400)
    return _render_panel(request, item_id, refresh_also_available=True)


@router.delete("/api/related-media/items/{item_id}/links/{other_item_id}")
async def related_media_unlink(
    request: Request,
    item_id: int,
    other_item_id: int,
    _=Depends(require_catalogue_role("editor")),
):
    with get_db() as db, item_write._write_scope(db):
        require_item_role(db, item_id, "editor")
        require_item_role(db, other_item_id, "editor")
        if not media_groups.unlink_items(db, item_id, other_item_id):
            return HTMLResponse(i18n.gettext("Related-media link not found"), status_code=404)
    return _render_panel(request, item_id, refresh_also_available=True)
