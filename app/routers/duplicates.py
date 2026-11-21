"""The Duplicates page: probable duplicate records, merged or dismissed.

One page and two API routes over `app/services/duplicates.py`:

- `GET /duplicates` — the two lists (same identifier; same title and author)
  and the Dismissed section.
- `POST /api/duplicates/dismiss` — "Not duplicates" for a whole group, every
  pair in one request.
- `DELETE /api/duplicates/dismiss` — Restore one dismissed pair.

The merge itself is the existing `POST /api/items/merge`; the page's script
calls it directly. That route is admin, so these are **admin** too, unlike
their Labels and Audits siblings: an editor shown the finder would meet a
button they cannot press. Every route declares `require_role("admin")`
**then** `require_feature("collector")`, the order the registry lint checks.

The client is `fetch`, not htmx, so a refusal answers **200** with
`{"ok": false, "message": …}` — the merge route's own shape — and the script
shows the message. Both writes take `BEGIN IMMEDIATE` first (G18) and nothing
logs under it (G3).
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import require_role
from app.config import MEDIA_TYPES
from app.database import get_db
from app.features import require_feature
from app.services import duplicates, item_merge, item_write
from app.services.catalogue_security import require_item_role

page_router = APIRouter()
router = APIRouter(prefix="/api/duplicates")


@page_router.get("/duplicates")
async def duplicates_page(
    request: Request,
    _=Depends(require_role("admin")),
    __=Depends(require_feature("collector")),
):
    with get_db() as db:
        identifier, title_author, hidden = duplicates.groups(db)
        dismissed = duplicates.dismissed_pairs(db)
    return request.app.state.templates.TemplateResponse(
        request, "duplicates.html", {
            "lists": (
                ("identifier", "Same ISBN or barcode", None, identifier),
                ("title_author", "Same title and author",
                 "Probably duplicates — check before merging.", title_author),
            ),
            "hidden": hidden,
            "dismissed": dismissed,
            "media_types": MEDIA_TYPES,
            "fillable": sorted(item_merge.MERGE_FILLABLE),
            "max_ids": duplicates.MAX_IDS,
        })


async def _ids(request: Request, *, exact: int | None = None) -> list[int] | str:
    try:
        body = await request.json()
    except ValueError:
        return "Invalid request"
    if not isinstance(body, dict):
        return "Invalid request"
    return duplicates.parse_ids(body.get("item_ids"), exact=exact)


def _authorize_ids(db, ids: list[int], *, include_deleted: bool = False) -> None:
    for item_id in ids:
        require_item_role(db, item_id, "editor", include_deleted=include_deleted)


@router.post("/dismiss")
async def dismiss(
    request: Request,
    _=Depends(require_role("admin")),
    __=Depends(require_feature("collector")),
):
    ids = await _ids(request)
    if isinstance(ids, str):
        return {"ok": False, "message": ids}
    try:
        with get_db() as db, item_write._write_scope(db):
            _authorize_ids(db, ids)
            refusal = duplicates.dismiss(db, ids)
            if refusal:
                raise item_write.ItemValueError(refusal)
    except HTTPException as exc:
        if exc.status_code not in (403, 404):
            raise
        return {"ok": False, "message": "One of these records no longer exists — reload the page"}
    except item_write.ItemValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "pairs": len(ids) * (len(ids) - 1) // 2}


@router.delete("/dismiss")
async def restore(
    request: Request,
    _=Depends(require_role("admin")),
    __=Depends(require_feature("collector")),
):
    ids = await _ids(request, exact=2)
    if isinstance(ids, str):
        return {"ok": False, "message": ids}
    try:
        with get_db() as db, item_write._write_scope(db):
            _authorize_ids(db, ids, include_deleted=True)
            duplicates.restore(db, *ids)
    except HTTPException as exc:
        if exc.status_code not in (403, 404):
            raise
        return {"ok": False, "message": "One of these records no longer exists — reload the page"}
    except item_write.ItemValueError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True}
