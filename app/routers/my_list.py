"""Signed-in user's cross-media Want-to list.

My List is consumption intent, not acquisition intent.  It projects the
per-user ``reading_status='want_to_read'`` state with media-aware labels while
leaving the independent ``wishlist`` flag untouched.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_role
from app.config import MEDIA_TYPES
from app.database import get_db
from app.services import user_state

router = APIRouter()

VALID_KINDS = ("all", "read", "watch", "listen", "play")


def _kind_for(media_type: str | None) -> str:
    media_type = media_type or ""
    if media_type == "dvd":
        return "watch"
    if media_type == "audiobook" or media_type in {
        "vinyl", "cassette", "cd", "digital_music", "music_other",
    }:
        return "listen"
    if media_type in {"video_game", "digital_game"}:
        return "play"
    return "read"


def _kind_label(kind: str) -> str:
    return {
        "read": "Read",
        "watch": "Watch",
        "listen": "Listen",
        "play": "Play",
    }.get(kind, "All")


def _user_id(request: Request) -> int:
    return int(request.state.user["id"])


@router.get("/my-list")
async def my_list(
    request: Request,
    kind: str = "all",
    _=Depends(require_role("viewer")),
):
    """Show this account's Want-to items across all media families."""
    if kind not in VALID_KINDS:
        kind = "all"

    with get_db() as db:
        rows = db.execute(
            """SELECT i.id, i.title, i.subtitle, i.authors, i.media_type,
                      i.cover_path, i.publish_year, i.platform, uis.updated_at
                 FROM user_item_state uis
                 JOIN items i ON i.id = uis.item_id
                WHERE uis.user_id = ?
                  AND uis.reading_status = 'want_to_read'
                ORDER BY uis.updated_at DESC, i.title COLLATE NOCASE, i.id DESC""",
            (_user_id(request),),
        ).fetchall()

    items = []
    counts = {name: 0 for name in VALID_KINDS}
    for row in rows:
        item = dict(row)
        item_kind = _kind_for(item.get("media_type"))
        labels = user_state.status_labels(item.get("media_type") or "book")
        item["kind"] = item_kind
        item["kind_label"] = _kind_label(item_kind)
        item["want_label"] = labels["want_to_read"]
        item["media_type_label"] = MEDIA_TYPES.get(
            item.get("media_type"), item.get("media_type") or "Media"
        )
        counts["all"] += 1
        counts[item_kind] += 1
        if kind == "all" or kind == item_kind:
            items.append(item)

    tabs = [
        {"key": key, "label": _kind_label(key), "count": counts[key]}
        for key in VALID_KINDS
    ]
    return request.app.state.templates.TemplateResponse(
        request,
        "my_list.html",
        {
            "items": items,
            "tabs": tabs,
            "active_kind": kind,
            "total": counts["all"],
        },
    )


@router.post("/my-list/{item_id}/remove")
async def remove_from_my_list(
    request: Request,
    item_id: int,
    kind: str = Form("all"),
    _=Depends(require_role("viewer")),
):
    """Clear only this user's Want-to status; never alter Wishlist."""
    if kind not in VALID_KINDS:
        kind = "all"

    uid = _user_id(request)
    with get_db() as db:
        state = user_state.get_state(db, uid, item_id)
        if state is None:
            return HTMLResponse("Item not found", status_code=404)
        if state.get("reading_status") == "want_to_read":
            user_state.set_reading_status(db, uid, item_id, None)

    target = "/my-list" if kind == "all" else f"/my-list?kind={kind}"
    return RedirectResponse(target, status_code=303)
