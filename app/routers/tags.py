"""Custom item tags — free-form labels (signed, first-edition, book-club…)
edited as chips on the item detail page and filterable on Browse.

The tag logic itself lives in app.services.tags; this router is the thin
HTTP wrapper around it."""

import logging

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse

from app.auth import require_role
from app.database import get_db
from app.services import tags as tags_svc
from app.services.tags import (  # noqa: F401 — re-exported
    MAX_TAG_LENGTH,
    normalize_tag,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _render_fragment(request: Request, db, item_id: int):
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/item_tags.html",
        {"item_id": item_id, "item_tags": tags_svc.get_item_tags(db, item_id),
         "all_tags": tags_svc.get_all_tags(db)},
    )


@router.post("/items/{item_id}/tags")
async def add_tag(request: Request, item_id: int, name: str = Form(...),
                  _=Depends(require_role("editor"))):
    tag_name = tags_svc.normalize_tag(name)
    if not tag_name:
        return HTMLResponse("Tag name required", status_code=400)

    with get_db() as db:
        item = db.execute("SELECT id FROM items_live WHERE id = ?", (item_id,)).fetchone()
        if not item:
            return HTMLResponse("Item not found", status_code=404)
        tags_svc.attach_tags(db, item_id, [tag_name])
        return _render_fragment(request, db, item_id)


@router.delete("/items/{item_id}/tags/{tag_id}")
async def remove_tag(request: Request, item_id: int, tag_id: int,
                     _=Depends(require_role("editor"))):
    with get_db() as db:
        item = db.execute("SELECT id FROM items_live WHERE id = ?", (item_id,)).fetchone()
        if not item:
            return HTMLResponse("Item not found", status_code=404)

        removed = tags_svc.detach_tags(db, item_id, [tag_id])
        if removed != 1:
            return HTMLResponse("Tag not found on item", status_code=404)

        # Garbage-collect orphaned tags so the Browse dropdown stays clean,
        # but only after an association was actually removed.
        tags_svc.gc_orphans(db, [tag_id])
        return _render_fragment(request, db, item_id)
