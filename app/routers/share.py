"""Public read-only share links. See .devdocs/archive/completed/SHARE_LINKS.md.

/share/<token> is the app's only intentionally unauthenticated page. It is
GET-only, rate-limited, marked noindex, and renders a deliberately minimal
field set — never locations, loans, valuations, notes, or ISBNs.
"""
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import features
from app.auth import require_role
from app.database import get_db
from app.services import item_write, libraries, lists
from app.services.catalogue_security import require_item_role, require_library_role

router = APIRouter()

SCOPES = ("wishlist", "collection")
SHARE_ITEM_CAP = 500


def _scope_where(scope: str) -> str:
    return lists.WISHLISTED_SQL if scope == "wishlist" else "i.owned = 1"


def _shared_library_id(token: str) -> int | None:
    """The stored capability fixes ONE library, independently of the visitor.

    Existing urlsafe tokens contain no dots: their original household scope is
    the Main Library, NEVER every later-created private library. New tokens
    record an explicit library selection in a versioned prefix. Exact database
    token lookup authenticates this prefix; editing it invalidates the token.
    """
    if "." not in token:
        return libraries.DEFAULT_LIBRARY_ID
    parts = token.split(".")
    if (len(parts) != 3 or parts[0] != "s1" or len(parts[1]) > 19
            or not parts[1].isascii() or not parts[1].isdigit()):
        return None
    library_id = int(parts[1])
    return library_id if 0 < library_id < 2**63 and parts[2] else None


def _public_items(db, *, scope: str, library_id: int) -> list[dict]:
    """Deliberate capability projection, not an absent-actor catalogue bypass.

    Do not use private request overlays, history or notes here, even for an
    authenticated visitor. The frozen library helper validates each candidate's
    mapping against the capability's stored selection BEFORE the output cap.
    No schema/provenance fallback can turn this into an all-libraries query.
    """
    items = []
    for row in db.execute(
        "SELECT i.id, i.title, i.authors, i.cover_path, i.media_type, i.publish_year, "
        "i.series_name, i.series_position FROM items i "
        f"WHERE i.deleted_at IS NULL AND ({_scope_where(scope)}) ORDER BY i.title COLLATE NOCASE, i.id",
    ):
        if libraries.item_library_id(db, row["id"]) != library_id:
            continue
        item = dict(row)
        del item["id"]  # Not part of the intentionally minimal public payload.
        items.append(item)
        if len(items) == SHARE_ITEM_CAP:
            break
    return items


@router.get("/share/{token}")
async def share_page(request: Request, token: str):
    templates = request.app.state.templates
    # With Sharing turned off, every token takes the unknown-token path
    # below: the same statement, so the same bytes. An anonymous visitor
    # must not learn that sharing exists and is off (the design's decision f).
    enabled = features.feature_enabled("share")
    with get_db() as db:
        link = db.execute(
            "SELECT * FROM share_links WHERE token = ?", (token,)
        ).fetchone() if enabled else None
        library_id = _shared_library_id(token) if link else None
        if not link or link["scope"] not in SCOPES or library_id is None:
            return HTMLResponse("Not found", status_code=404,
                                headers={"X-Robots-Tag": "noindex"})

        items = _public_items(db, scope=link["scope"], library_id=library_id)

    resp = templates.TemplateResponse(
        request, "share.html",
        {"link": link, "items": items, "scope": link["scope"]},
    )
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@router.post("/api/share")
async def create_share_link(
    scope: str = Form("wishlist"),
    label: str = Form(""),
    library_id: int = Form(libraries.DEFAULT_LIBRARY_ID, ge=1, le=2**63 - 1),
    _=Depends(require_role("admin")),
    __=Depends(features.require_feature("share")),
):
    if scope not in SCOPES:
        return JSONResponse(
            {"ok": False, "message": "Invalid share scope"}, status_code=400
        )
    # The random suffix retains the native 128-bit entropy. Library provenance
    # is stored with the exact token; it is not a user-supplied read predicate.
    token = f"s1.{library_id}.{secrets.token_urlsafe(16)}"
    with get_db() as db, item_write._write_scope(db):
        require_library_role(db, library_id, "editor")
        for row in db.execute(
            f"SELECT i.id FROM items i WHERE i.deleted_at IS NULL AND ({_scope_where(scope)}) ORDER BY i.id",
        ).fetchall():
            if libraries.item_library_id(db, row["id"]) == library_id:
                require_item_role(db, row["id"], "editor")
        db.execute(
            "INSERT INTO share_links (token, scope, label) VALUES (?, ?, ?)",
            (token, scope, label.strip()[:100] or None),
        )
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/api/share/{link_id}/delete")
async def revoke_share_link(link_id: int, _=Depends(require_role("admin")),
                            __=Depends(features.require_feature("share"))):
    with get_db() as db, item_write._write_scope(db):
        cursor = db.execute("DELETE FROM share_links WHERE id = ?", (link_id,))
        if cursor.rowcount != 1:
            return JSONResponse(
                {"ok": False, "message": "Share link not found"}, status_code=404
            )
    return RedirectResponse(url="/settings", status_code=303)
