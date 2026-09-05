"""Add connected-service metadata to the ACL-scoped Series detail response.

Name-based detail views resolve through ``item_series`` so secondary provider
memberships (notably RomM franchises) are first-class. Komga keeps its stable
source-ID route. Provider metadata is shown only when an item already admitted
by the library ACL anchors that provider record, preventing metadata itself
from becoming a hidden-library side channel.
"""

from __future__ import annotations

import json
from collections import Counter
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Query, Request

from app.auth import require_role
from app.database import get_db
from app.routers import library_secondary_reads, series
from app.services import libraries, provider_series
from app.services.series_display import count_label, find_gaps, infer_series_unit


def _remove_route(path: str, method: str) -> None:
    method = method.upper()
    full_path = f"{series.router.prefix}{path}" if series.router.prefix else path
    series.router.routes[:] = [
        route
        for route in series.router.routes
        if not (
            getattr(route, "path", None) == full_path
            and method in (getattr(route, "methods", None) or set())
        )
    ]


def _provider_row_is_anchored(row: dict, visible_items: list[dict]) -> bool:
    provider = row.get("provider")
    if provider == "komga":
        provider_id = str(row.get("provider_series_id") or "")
        return any(
            str(item.get("source") or "").casefold() == "komga"
            and str(item.get("komga_series_id") or "") == provider_id
            for item in visible_items
        )

    if provider == "audiobookshelf":
        library_id = None
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
            if isinstance(metadata, dict) and metadata.get("library_id") is not None:
                library_id = str(metadata["library_id"])
        except (TypeError, ValueError):
            pass
        return any(
            item.get("abs_id")
            and (library_id is None or str(item.get("abs_library_id") or "") == library_id)
            for item in visible_items
        )

    if provider == "romm":
        return any(item.get("romm_id") for item in visible_items)

    # Unknown future providers do not surface until an explicit anchoring rule
    # exists. That is safer than matching only on a human-readable series name.
    return False


def _attach_provider_metadata(db, series_ctx: dict) -> None:
    rows = provider_series.for_series(db, series_ctx["name"])
    rows = [
        row
        for row in rows
        if _provider_row_is_anchored(row, series_ctx.get("items") or [])
    ]
    description, source = provider_series.preferred_description(
        rows, series_ctx.get("media_type")
    )
    series_ctx["provider_metadata"] = rows
    series_ctx["provider_description"] = description
    series_ctx["provider_description_source"] = source


def _name_based_context(
    request: Request,
    *,
    name: str,
    media_type: str | None,
) -> dict:
    requested_name = name.strip()
    if not requested_name:
        raise HTTPException(status_code=404, detail="Series not found")

    user = dict(request.state.user)
    with get_db() as db:
        series._sync_item_series_memberships(db)
        access_sql, access_params = libraries.item_access_condition(
            user, item_alias="i"
        )
        params: list[object] = [requested_name]
        media_clause = ""
        if media_type:
            media_clause = " AND i.media_type = ?"
            params.append(media_type)
        rows = db.execute(
            "SELECT i.id, i.title, i.authors, i.cover_path, "
            "s.series_name, s.position AS series_position, i.publish_year, "
            "i.owned, i.reading_status, i.media_type, i.source, "
            "i.komga_series_id, i.abs_id, i.abs_library_id, i.romm_id "
            "FROM item_series s JOIN items i ON i.id = s.item_id "
            "WHERE s.series_name = ? COLLATE NOCASE "
            f"AND ({access_sql}){media_clause} "
            "ORDER BY (s.position IS NULL), s.position ASC, "
            "(i.publish_year IS NULL), i.publish_year ASC, "
            "i.title COLLATE NOCASE, i.id ASC",
            [*params[:1], *access_params, *params[1:]],
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Series not found")

        items = [dict(row) for row in rows]
        spellings = Counter(str(item["series_name"] or requested_name) for item in items)
        display_name = min(
            spellings.items(), key=lambda pair: (-pair[1], pair[0])
        )[0]
        meta = db.execute(
            "SELECT description, complete, hc_total, hc_missing, hc_checked_at "
            "FROM series_meta WHERE name = ? COLLATE NOCASE",
            (display_name,),
        ).fetchone()

        unit = infer_series_unit(items)
        media_types = {
            item.get("media_type") for item in items if item.get("media_type")
        }
        resolved_media_type = media_type or (
            next(iter(media_types)) if len(media_types) == 1 else None
        )
        browse_params: dict[str, str] = {"series": display_name}
        if media_type:
            browse_params["media_type_filter"] = media_type
        series_ctx = {
            "name": display_name,
            "items": items,
            "item_count": len(items),
            "count_label": count_label(len(items), unit),
            "owned_count": sum(1 for item in items if item.get("owned")),
            "wishlist_count": sum(1 for item in items if not item.get("owned")),
            "gaps": find_gaps(item.get("series_position") for item in items),
            "complete": bool(meta and meta["complete"] == 1),
            "description": meta["description"] if meta else None,
            "hc_total": meta["hc_total"] if meta else None,
            "hc_missing": meta["hc_missing"] if meta else None,
            "hc_checked_at": meta["hc_checked_at"] if meta else None,
            "unit": unit,
            "media_type": resolved_media_type,
            "komga_series_id": None,
            "browse_url": "/browse?" + urlencode(browse_params),
            "can_bulk_add": resolved_media_type == "comic",
        }
        _attach_provider_metadata(db, series_ctx)

    return {
        "series": series_ctx,
        "bulk_added": request.query_params.get("bulk_added"),
        "bulk_skipped": request.query_params.get("bulk_skipped"),
        "bulk_error": request.query_params.get("bulk_error"),
    }


_original_series_detail = library_secondary_reads.library_series_detail
_remove_route("/series/detail", "GET")


@series.router.get("/series/detail")
async def provider_series_detail(
    request: Request,
    name: str = Query(...),
    media_type: str | None = Query(default=None),
    komga_series_id: str | None = Query(default=None),
    _=Depends(require_role("viewer")),
):
    """Render an authorised Shelf series plus anchored provider metadata."""
    if not str(komga_series_id or "").strip():
        ctx = _name_based_context(request, name=name, media_type=media_type)
        return request.app.state.templates.TemplateResponse(
            request, "series_detail.html", ctx
        )

    # Komga's stable series ID remains authoritative when supplied. Let the
    # existing ACL-scoped route resolve/filter those items first.
    response = await _original_series_detail(
        request,
        name=name,
        media_type=media_type,
        komga_series_id=komga_series_id,
        _=request.state.user,
    )
    context = getattr(response, "context", None)
    if not context or not context.get("series"):
        return response

    ctx = dict(context)
    series_ctx = dict(ctx["series"])
    visible_ids = [int(item["id"]) for item in series_ctx.get("items") or []]
    if visible_ids:
        placeholders = ",".join("?" for _ in visible_ids)
        with get_db() as db:
            source_rows = db.execute(
                "SELECT id, source, komga_series_id, abs_id, abs_library_id, romm_id "
                f"FROM items WHERE id IN ({placeholders})",
                visible_ids,
            ).fetchall()
            by_id = {int(row["id"]): dict(row) for row in source_rows}
            series_ctx["items"] = [
                {**item, **by_id.get(int(item["id"]), {})}
                for item in series_ctx.get("items") or []
            ]
            rows = provider_series.for_series(
                db,
                series_ctx["name"],
                provider="komga",
                provider_series_id=str(komga_series_id),
            )
            rows = [
                row
                for row in rows
                if _provider_row_is_anchored(row, series_ctx["items"])
            ]
            description, source = provider_series.preferred_description(
                rows, series_ctx.get("media_type")
            )
            series_ctx["provider_metadata"] = rows
            series_ctx["provider_description"] = description
            series_ctx["provider_description_source"] = source
    else:
        series_ctx["provider_metadata"] = []
        series_ctx["provider_description"] = None
        series_ctx["provider_description_source"] = None

    ctx["series"] = series_ctx
    return request.app.state.templates.TemplateResponse(
        request, "series_detail.html", ctx
    )
