"""The Upcoming releases page: announced volumes in watched series.

- `GET /upcoming` — every undismissed announcement the release radar has
  recorded, minus any the library now owns or wishlists. Viewer-visible;
  reached from the Series page header, not the nav.
- `POST /api/upcoming/{release_id}/dismiss` — hide one announcement. HTMX
  swaps the row out with the empty 200 body; a missing id answers 404 with
  an `HX-Trigger` toast, since htmx never swaps a 4xx body (G145).

Both need `release_radar` and `hardcover` on: the rows come from Hardcover,
and the page's + Wishlist button calls a Hardcover-gated route. Role first,
then features — the order the registry lint checks.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.routers import items_common
from app.services import catalogue_security, item_write, libraries, release_radar
from app.services.catalogue_security import require_catalogue_role, require_item_role

page_router = APIRouter()
router = APIRouter(prefix="/api/upcoming")


def _series_items(db, series_name: str) -> list[int]:
    # A dismissal is shared across every library with this watched series.
    # Deliberately enumerate physical live parents for all-target preflight;
    # filtering this set through items_live would silently omit hidden effects.
    return [row["id"] for row in db.execute(
        "SELECT id FROM items WHERE deleted_at IS NULL AND series_name = ? COLLATE NOCASE ORDER BY id",
        (series_name,),
    )]


@page_router.get("/upcoming")
async def upcoming_page(
    request: Request,
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("release_radar")),
    ___=Depends(require_feature("hardcover")),
):
    with get_db() as db:
        visible_series = {
            row["series_name"].casefold() for row in db.execute(
                "SELECT DISTINCT series_name FROM items_live WHERE series_name IS NOT NULL"
            )
        }
        # Apply visibility BEFORE the modern output cap; hidden watches must
        # neither appear nor crowd accessible announcements out of the bound.
        rows = [row for row in release_radar.visible_rows(db, limit=None)
                if row["series_name"].casefold() in visible_series][:release_radar.VISIBLE_LIMIT]
        user = catalogue_security.current_actor()
        for row in rows:
            parents = _series_items(db, row["series_name"])
            row["can_edit"] = bool(user) and bool(parents) and all(
                libraries.has_item_role(db, user, item_id, "editor") for item_id in parents
            )
    return request.app.state.templates.TemplateResponse(
        request, "upcoming.html", {"releases": rows},
    )


@router.post("/{release_id}/dismiss")
async def dismiss_release(
    release_id: int,
    _=Depends(require_catalogue_role("editor")),
    __=Depends(require_feature("release_radar")),
    ___=Depends(require_feature("hardcover")),
):
    with get_db() as db, item_write._write_scope(db):
        row = db.execute(
            "SELECT w.series_name FROM upcoming_releases u "
            "JOIN release_watches w ON w.id = u.watch_id WHERE u.id = ? "
            "AND EXISTS (SELECT 1 FROM items_live i WHERE i.series_name = w.series_name COLLATE NOCASE)",
            (release_id,),
        ).fetchone()
        found = row is not None
        if found:
            for item_id in _series_items(db, row["series_name"]):
                require_item_role(db, item_id, "editor")
            db.execute("UPDATE upcoming_releases SET dismissed = 1 WHERE id = ?", (release_id,))
    if not found:
        return Response(status_code=404, headers={
            "HX-Trigger": items_common._toast_header("That announcement is gone", "error")})
    return Response(status_code=200)
