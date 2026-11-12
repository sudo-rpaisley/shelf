"""Needs Attention and explicit, single-pair format-connection review."""

from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app import i18n
from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.routers import items_common
from app.services import connection_suggestions, format_groups, library_attention, media_groups


router = APIRouter()
Category = Literal["all", "covers", "creators", "locations", "periodicals", "sources", "unavailable", "connections"]
Result = Literal["accepted", "dismissed", "restored"]


def _refuse(status: int, message: str):
    raise HTTPException(status, message, headers={
        "HX-Trigger": items_common._toast_header(message, "error"),
        "Cache-Control": "no-store",
    })


def _notice(db, result, a, b):
    if not result or a is None or b is None or a == b:
        return None
    items = db.execute(
        "SELECT id, title FROM items_live WHERE id IN (?, ?) ORDER BY id", (a, b),
    ).fetchall()
    if len(items) != 2:
        return None
    if result == "accepted" and b not in format_groups.format_ids(db, a):
        return None
    return {"result": result, "items": [dict(item) for item in items]}


@router.get("/needs-attention")
async def needs_attention(
    request: Request,
    category: Category = "all",
    limit: int = Query(50, ge=1, le=library_attention.MAX_QUEUE_LIMIT),
    result: Result | None = None,
    item_a_id: int | None = Query(None, gt=0, le=2**63 - 1),
    item_b_id: int | None = Query(None, gt=0, le=2**63 - 1),
    _=Depends(require_role("editor")),
    __=Depends(require_feature("connected_library")),
):
    with get_db() as db:
        context = library_attention.summary(db, limit, category=category)
        context["connection_notice"] = _notice(db, result, item_a_id, item_b_id)
    return request.app.state.templates.TemplateResponse(
        request, "needs_attention.html", context, headers={"Cache-Control": "no-store"},
    )


def _review_pair(db, a, b, evidence_key, *, accepting=False):
    """This helper is called only after BEGIN IMMEDIATE (G18)."""
    if a == b:
        _refuse(400, i18n.gettext("Choose two different items"))
    a, b = sorted((a, b))
    if db.execute("SELECT COUNT(*) FROM items_live WHERE id IN (?, ?)", (a, b)).fetchone()[0] != 2:
        _refuse(404, i18n.gettext("Item not found"))
    # Legacy merges could leave a reversed edge, or even one edge of each
    # orientation with different types. Any nonformat relationship vetoes the
    # action, including an otherwise idempotent acceptance of a format duplicate.
    nonformat = db.execute(
        "SELECT 1 FROM item_links WHERE "
        "((item_a_id = ? AND item_b_id = ?) OR (item_a_id = ? AND item_b_id = ?)) "
        "AND link_type != 'format'", (a, b, b, a),
    ).fetchone()
    if nonformat:
        _refuse(409, i18n.gettext("These items already have a different relationship; review it under Related Media"))
    proposal = connection_suggestions.evidence_for_pair(db, a, b)
    if proposal is None or proposal["evidence_key"] != evidence_key:
        _refuse(409, i18n.gettext("The connection evidence changed; reload and review the current records"))
    connected = b in format_groups.format_ids(db, a)
    if connected and not accepting:
        _refuse(409, i18n.gettext("These items are already connected; manage the link under Related Media"))
    return proposal, connected


def _saved(request: Request, proposal: dict, result: str):
    # The write transaction has closed before any rendering, logging-capable
    # template helpers, or second database connection is reached (G3).
    if request.headers.get("HX-Request", "").lower() != "true":
        query = urlencode({"category": "connections", "result": result,
                           "item_a_id": proposal["item_a_id"], "item_b_id": proposal["item_b_id"]})
        # Fixed local destination and server-selected parameters, never next,
        # Referer, HX-Current-URL or a provider-supplied address.
        return RedirectResponse(f"/needs-attention?{query}", status_code=303,
                                headers={"Cache-Control": "no-store"})
    with get_db() as db:
        context = library_attention.summary(db, category="connections")
        context["connection_notice"] = _notice(
            db, result, proposal["item_a_id"], proposal["item_b_id"],
        )
    return request.app.state.templates.TemplateResponse(
        request, "fragments/connection_suggestions.html", context,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/library/connections/accept")
async def accept_connection(
    request: Request,
    item_a_id: int = Form(..., gt=0, le=2**63 - 1),
    item_b_id: int = Form(..., gt=0, le=2**63 - 1),
    evidence_key: str = Form(..., pattern=r"^[0-9a-f]{64}$"),
    _=Depends(require_role("editor")),
    __=Depends(require_feature("connected_library")),
):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        proposal, connected = _review_pair(db, item_a_id, item_b_id, evidence_key, accepting=True)
        a, b = proposal["item_a_id"], proposal["item_b_id"]
        if not connected:
            media_groups.link_items(db, a, b, link_type="format")
        db.execute("DELETE FROM connection_dismissals WHERE item_a_id = ? AND item_b_id = ?", (a, b))
    return _saved(request, proposal, "accepted")


@router.post("/api/library/connections/dismiss")
async def dismiss_connection(
    request: Request,
    item_a_id: int = Form(..., gt=0, le=2**63 - 1),
    item_b_id: int = Form(..., gt=0, le=2**63 - 1),
    evidence_key: str = Form(..., pattern=r"^[0-9a-f]{64}$"),
    _=Depends(require_role("editor")),
    __=Depends(require_feature("connected_library")),
):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        proposal, _connected = _review_pair(db, item_a_id, item_b_id, evidence_key)
        db.execute(
            "INSERT INTO connection_dismissals (item_a_id, item_b_id, evidence_key) VALUES (?, ?, ?) "
            "ON CONFLICT(item_a_id, item_b_id) DO UPDATE SET "
            "evidence_key = excluded.evidence_key, created_at = datetime('now')",
            (proposal["item_a_id"], proposal["item_b_id"], proposal["evidence_key"]),
        )
    return _saved(request, proposal, "dismissed")


@router.post("/api/library/connections/restore")
async def restore_connection(
    request: Request,
    item_a_id: int = Form(..., gt=0, le=2**63 - 1),
    item_b_id: int = Form(..., gt=0, le=2**63 - 1),
    evidence_key: str = Form(..., pattern=r"^[0-9a-f]{64}$"),
    _=Depends(require_role("editor")),
    __=Depends(require_feature("connected_library")),
):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        proposal, _connected = _review_pair(db, item_a_id, item_b_id, evidence_key)
        a, b = proposal["item_a_id"], proposal["item_b_id"]
        dismissal = db.execute(
            "SELECT evidence_key FROM connection_dismissals WHERE item_a_id = ? AND item_b_id = ?", (a, b),
        ).fetchone()
        if dismissal and dismissal["evidence_key"] != evidence_key:
            _refuse(409, i18n.gettext("The dismissal evidence changed; reload and review the current records"))
        db.execute("DELETE FROM connection_dismissals WHERE item_a_id = ? AND item_b_id = ?", (a, b))
    return _saved(request, proposal, "restored")
