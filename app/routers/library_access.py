"""Library-aware read surfaces layered over Browse and personal-state search.

This focused extension owns the first catalogue security boundary: Browse and
its HTMX search endpoint. It is loaded after the personal-state route extension
so both personal filters and library visibility are applied in one query.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Depends, Request

from app import browse_filters
from app.auth import require_role
from app.config import DEFAULT_PAGE_SIZE, MEDIA_FAMILIES, MEDIA_TYPES
from app.database import get_db
from app.routers import items, pages
from app.routers.items_common import SORT_OPTIONS
from app.services import browse_grouping, libraries, user_state, user_state_browse


def _remove_route(router, path: str, method: str) -> None:
    method = method.upper()
    full_path = f"{router.prefix}{path}" if router.prefix else path
    router.routes[:] = [
        route
        for route in router.routes
        if not (
            getattr(route, "path", None) == full_path
            and method in (getattr(route, "methods", None) or set())
        )
    ]


def _access(user: dict) -> tuple[str, list]:
    return libraries.item_access_condition(user, item_alias="i", minimum_role="viewer")


def _scoped_series_names(db, access_sql: str, access_params: list) -> list[str]:
    return [
        row["series_name"]
        for row in db.execute(
            "SELECT DISTINCT i.series_name FROM items i "
            "WHERE i.series_name IS NOT NULL AND TRIM(i.series_name) != '' "
            f"AND ({access_sql}) ORDER BY i.series_name COLLATE NOCASE",
            access_params,
        ).fetchall()
    ]


def _scoped_tags(db, access_sql: str, access_params: list):
    return db.execute(
        "SELECT DISTINCT t.* FROM tags t "
        "JOIN item_tags it ON it.tag_id = t.id "
        "JOIN items i ON i.id = it.item_id "
        f"WHERE {access_sql} ORDER BY t.name COLLATE NOCASE",
        access_params,
    ).fetchall()


def _scoped_languages(db, access_sql: str, access_params: list) -> list[str]:
    return [
        row["language"]
        for row in db.execute(
            "SELECT DISTINCT i.language FROM items i "
            "WHERE i.language IS NOT NULL AND i.language != '' "
            f"AND ({access_sql}) ORDER BY i.language",
            access_params,
        ).fetchall()
    ]


def _scoped_lent_count(db, access_sql: str, access_params: list) -> int:
    return int(
        db.execute(
            "SELECT COUNT(DISTINCT c.item_id) AS c FROM checkouts c "
            "JOIN items i ON i.id = c.item_id "
            f"WHERE c.checked_in IS NULL AND ({access_sql})",
            access_params,
        ).fetchone()["c"]
    )


_remove_route(pages.router, "/browse", "GET")


@pages.router.get("/browse")
async def library_browse(
    request: Request,
    _=Depends(require_role("viewer")),
):
    """Render only catalogue records visible through the user's libraries."""
    values = browse_filters.values_from(request.query_params)
    values["q"] = values["q"][:200]
    user = dict(request.state.user)
    user_id = int(user["id"])
    where, params = browse_filters.build_where(values, user_id=user_id)
    where, params = libraries.scope_where(where, params, user)
    access_sql, access_params = _access(user)

    with get_db() as db:
        user_state.ensure_schema(db)
        libraries.ensure_schema(db)
        _, order_clause = SORT_OPTIONS.get(values["sort"], SORT_OPTIONS["newest"])

        result_items, total_filtered, display_total = browse_grouping.fetch_page(
            db,
            where,
            params,
            order_clause,
            limit=DEFAULT_PAGE_SIZE,
            offset=0,
            values=values,
            visibility_sql=access_sql,
            visibility_params=access_params,
        )
        user_state_browse.overlay_items(db, user_id, result_items)

        series_names = _scoped_series_names(db, access_sql, access_params)
        counts = user_state_browse.filter_counts(
            db,
            values,
            total_filtered,
            user_id,
            user=user,
        )
        lent_out_count = _scoped_lent_count(db, access_sql, access_params)
        all_tags = _scoped_tags(db, access_sql, access_params)
        item_languages = _scoped_languages(db, access_sql, access_params)
        has_more = len(result_items) < display_total
        load_more_url = "/api/search?" + browse_filters.querystring(
            values, extra=["page=2"]
        )

    ctx = {
        "items": result_items,
        "media_types": MEDIA_TYPES,
        "media_families": MEDIA_FAMILIES,
        "series_names": series_names,
        "all_tags": all_tags,
        "lent_out_count": lent_out_count,
        "item_languages": item_languages,
        "has_more": has_more,
        "has_filters": browse_filters.has_active_filters(values),
        "load_more_url": load_more_url,
        "seven_days_ago": (
            datetime.now(tz=None) - timedelta(days=7)
        ).strftime("%Y-%m-%d"),
        "initial_query": values["q"],
        "initial_filters": {
            name: values[name] for name in browse_filters.FILTER_NAMES
        },
    }
    ctx.update(counts)
    return request.app.state.templates.TemplateResponse(
        request,
        "browse.html",
        ctx,
    )


_remove_route(items.router, "/search", "GET")


@items.router.get("/search")
async def library_search_items(
    request: Request,
    page: int = 1,
    per_page: int = DEFAULT_PAGE_SIZE,
    _=Depends(require_role("viewer")),
):
    """HTMX Browse results scoped to the user's library memberships."""
    templates = request.app.state.templates
    values = browse_filters.values_from(request.query_params)
    values["q"] = values["q"][:200]
    user = dict(request.state.user)
    user_id = int(user["id"])
    where, params = browse_filters.build_where(values, user_id=user_id)
    where, params = libraries.scope_where(where, params, user)
    access_sql, access_params = _access(user)
    _, order_clause = SORT_OPTIONS.get(values["sort"], SORT_OPTIONS["newest"])
    offset = (max(page, 1) - 1) * per_page

    with get_db() as db:
        user_state.ensure_schema(db)
        libraries.ensure_schema(db)
        result_items, total, display_total = browse_grouping.fetch_page(
            db,
            where,
            params,
            order_clause,
            limit=per_page,
            offset=offset,
            values=values,
            visibility_sql=access_sql,
            visibility_params=access_params,
        )
        user_state_browse.overlay_items(db, user_id, result_items)
        counts = (
            user_state_browse.filter_counts(
                db,
                values,
                total,
                user_id,
                user=user,
            )
            if page <= 1
            else None
        )

    has_more = (offset + per_page) < display_total
    load_more_url = "/api/search?" + browse_filters.querystring(
        values, extra=[f"page={page + 1}"]
    )
    if page <= 1:
        template = "fragments/item_grid.html"
    elif values["view"] == "list":
        template = "fragments/item_rows_page.html"
    else:
        template = "fragments/item_cards_page.html"

    ctx = {
        "items": result_items,
        "media_types": MEDIA_TYPES,
        "has_more": has_more,
        "load_more_url": load_more_url,
        "page": page,
        "total": total,
        "has_filters": browse_filters.has_active_filters(values),
        "seven_days_ago": (
            datetime.now(tz=None) - timedelta(days=7)
        ).strftime("%Y-%m-%d"),
    }
    if counts:
        ctx.update(counts)
        ctx["render_oob_counts"] = True
    return templates.TemplateResponse(request, template, ctx)
