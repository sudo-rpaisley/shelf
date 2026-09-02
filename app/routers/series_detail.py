"""Focused series drill-down used by grouped Browse cards.

The existing ``/series`` page remains the management overview.  This route is
purposefully read-only and gives one series enough space for ordered covers,
local gap hints and source-aware issue/volume wording.
"""

from __future__ import annotations

from collections import Counter
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Query, Request

from app.auth import require_role
from app.database import get_db
from app.routers.series import router
from app.services.series_display import count_label, find_gaps, infer_series_unit


@router.get("/series/detail")
async def series_detail_page(
    request: Request,
    name: str = Query(...),
    media_type: str | None = Query(default=None),
    _=Depends(require_role("viewer")),
):
    """Render one series with members ordered by ``series_position``.

    ``media_type`` is optional so the route can later become the common series
    detail surface for mixed-format collections.  Grouped Digital Comic cards
    currently pass ``digital_comic`` to avoid counting a separately catalogued
    physical copy as another issue/volume before Shelf gains first-class
    work/holding modelling.
    """
    requested_name = name.strip()
    if not requested_name:
        raise HTTPException(status_code=404, detail="Series not found")

    params: list[object] = [requested_name]
    media_clause = ""
    if media_type:
        media_clause = " AND media_type = ?"
        params.append(media_type)

    with get_db() as db:
        rows = db.execute(
            "SELECT id, title, authors, cover_path, series_name, series_position, "
            "publish_year, owned, reading_status, media_type, source, komga_series_id "
            "FROM items WHERE series_name = ? COLLATE NOCASE"
            f"{media_clause} "
            "ORDER BY (series_position IS NULL), series_position ASC, "
            "(publish_year IS NULL), publish_year ASC, title COLLATE NOCASE, id ASC",
            params,
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Series not found")

        meta = db.execute(
            "SELECT description, complete, hc_total, hc_missing, hc_checked_at "
            "FROM series_meta WHERE name = ? COLLATE NOCASE",
            (requested_name,),
        ).fetchone()

    items = [dict(row) for row in rows]
    spellings = Counter(str(item["series_name"]) for item in items)
    display_name = min(spellings.items(), key=lambda pair: (-pair[1], pair[0]))[0]
    unit = infer_series_unit(items)
    gaps = find_gaps(item.get("series_position") for item in items)
    owned_count = sum(1 for item in items if item.get("owned"))
    wishlist_count = len(items) - owned_count

    browse_params: dict[str, str] = {"series": display_name}
    if media_type:
        browse_params["media_type_filter"] = media_type

    series = {
        "name": display_name,
        "items": items,
        "item_count": len(items),
        "count_label": count_label(len(items), unit),
        "owned_count": owned_count,
        "wishlist_count": wishlist_count,
        "gaps": gaps,
        "complete": bool(meta and meta["complete"] == 1),
        "description": meta["description"] if meta else None,
        "hc_total": meta["hc_total"] if meta else None,
        "hc_missing": meta["hc_missing"] if meta else None,
        "hc_checked_at": meta["hc_checked_at"] if meta else None,
        "unit": unit,
        "media_type": media_type,
        "browse_url": "/browse?" + urlencode(browse_params),
    }

    return request.app.state.templates.TemplateResponse(
        request,
        "series_detail.html",
        {"series": series},
    )
