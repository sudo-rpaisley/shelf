"""Cross-media personal watch/read/listen/play list.

Shelf stores one neutral personal status value (``want_to_read``) and presents
media-aware language around it.  My List is the unified destination for that
state: books become Want to Read, films Want to Watch, music/audiobooks Want
to Listen and games Want to Play.

The page is always projected through the Shelf library ACL.  Losing access to
a library therefore removes its titles from My List immediately without
deleting the user's personal state, so access can be restored safely later.
"""

from __future__ import annotations

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_role
from app.config import MEDIA_TYPES
from app.database import get_db
from app.routers import pages
from app.services import libraries, user_state


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


@pages.router.get("/my-list")
async def my_list(
    request: Request,
    kind: str = "all",
    _=Depends(require_role("viewer")),
):
    """Show the signed-in user's accessible Want-to items across all media."""
    if kind not in VALID_KINDS:
        kind = "all"

    user = request.state.user
    uid = _user_id(request)
    with get_db() as db:
        user_state.ensure_schema(db)
        access_sql, access_params = libraries.item_access_condition(
            user,
            item_alias="i",
            minimum_role="viewer",
        )
        rows = db.execute(
            f"""SELECT i.id, i.title, i.subtitle, i.authors, i.media_type,
                       i.cover_path, i.publish_year, i.platform,
                       uis.updated_at
                  FROM user_item_state uis
                  JOIN items i ON i.id = uis.item_id
                 WHERE uis.user_id = ?
                   AND uis.reading_status = 'want_to_read'
                   AND ({access_sql})
                 ORDER BY uis.updated_at DESC, i.title COLLATE NOCASE, i.id DESC""",
            [uid] + access_params,
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


@pages.router.post("/my-list/{item_id}/remove")
async def remove_from_my_list(
    request: Request,
    item_id: int,
    kind: str = Form("all"),
    _=Depends(require_role("viewer")),
):
    """Remove one accessible item from My List without changing Wishlist."""
    user = request.state.user
    if kind not in VALID_KINDS:
        kind = "all"

    with get_db() as db:
        if not libraries.has_item_role(db, user, item_id, "viewer"):
            return HTMLResponse("Item not found", status_code=404)
        state = user_state.get_state(db, _user_id(request), item_id)
        if state is None:
            return HTMLResponse("Item not found", status_code=404)
        if state.get("reading_status") == "want_to_read":
            user_state.set_reading_status(db, _user_id(request), item_id, None)

    target = "/my-list" if kind == "all" else f"/my-list?kind={kind}"
    return RedirectResponse(target, status_code=303)
