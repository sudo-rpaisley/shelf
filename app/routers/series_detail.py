"""Focused series drill-down used by grouped Browse cards.

The existing ``/series`` page remains the management overview. This route gives
one series enough space for ordered covers, local gap hints and focused
series-level actions such as adding a known run of physical comic issues.

For Komga-backed Digital Comics, ``komga_series_id`` is the authoritative
series identity. The human-readable title remains display metadata and is not
used to merge distinct Komga series.
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
    komga_series_id: str | None = Query(default=None),
    _=Depends(require_role("viewer")),
):
    """Render one series with members ordered by ``series_position``.

    A Komga series ID, when supplied, takes precedence over ``name`` for
    membership. That mirrors Komga even if two source series share a title.
    Non-Komga callers keep Shelf's case-insensitive name-based behaviour.
    """
    requested_name = name.strip()
    requested_komga_id = str(komga_series_id or "").strip() or None
    if not requested_name and not requested_komga_id:
        raise HTTPException(status_code=404, detail="Series not found")

    params: list[object]
    if requested_komga_id:
        identity_clause = (
            "LOWER(COALESCE(source, '')) = 'komga' AND komga_series_id = ?"
        )
        params = [requested_komga_id]
    else:
        identity_clause = "series_name = ? COLLATE NOCASE"
        params = [requested_name]

    media_clause = ""
    if media_type:
        media_clause = " AND media_type = ?"
        params.append(media_type)

    with get_db() as db:
        rows = db.execute(
            "SELECT id, title, authors, cover_path, series_name, series_position, "
            "publish_year, owned, reading_status, media_type, source, komga_series_id "
            f"FROM items WHERE {identity_clause}"
            f"{media_clause} "
            "ORDER BY (series_position IS NULL), series_position ASC, "
            "(publish_year IS NULL), publish_year ASC, title COLLATE NOCASE, id ASC",
            params,
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Series not found")

    items = [dict(row) for row in rows]
    spellings = Counter(str(item["series_name"] or requested_name) for item in items)
    display_name = min(spellings.items(), key=lambda pair: (-pair[1], pair[0]))[0]

    with get_db() as db:
        meta = db.execute(
            "SELECT description, complete, hc_total, hc_missing, hc_checked_at "
            "FROM series_meta WHERE name = ? COLLATE NOCASE",
            (display_name,),
        ).fetchone()

    unit = infer_series_unit(items)
    gaps = find_gaps(item.get("series_position") for item in items)
    owned_count = sum(1 for item in items if item.get("owned"))
    wishlist_count = len(items) - owned_count

    # If the detail URL did not need an explicit media filter, a uniform series
    # can still use media-specific actions. Mixed physical/digital groups stay
    # deliberately ambiguous until the caller scopes the detail view.
    media_types = {item.get("media_type") for item in items if item.get("media_type")}
    resolved_media_type = media_type or (next(iter(media_types)) if len(media_types) == 1 else None)
    can_bulk_add = bool(not requested_komga_id and resolved_media_type == "comic")

    # Name-based Browse filtering cannot faithfully isolate two Komga series
    # with the same title, so omit that shortcut for source-ID scoped views.
    browse_url = None
    if not requested_komga_id:
        browse_params: dict[str, str] = {"series": display_name}
        if media_type:
            browse_params["media_type_filter"] = media_type
        browse_url = "/browse?" + urlencode(browse_params)

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
        "media_type": resolved_media_type,
        "komga_series_id": requested_komga_id,
        "browse_url": browse_url,
        "can_bulk_add": can_bulk_add,
    }

    return request.app.state.templates.TemplateResponse(
        request,
        "series_detail.html",
        {
            "series": series,
            "bulk_added": request.query_params.get("bulk_added"),
            "bulk_skipped": request.query_params.get("bulk_skipped"),
            "bulk_error": request.query_params.get("bulk_error"),
        },
    )
