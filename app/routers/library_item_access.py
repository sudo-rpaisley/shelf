"""Complete the first-class library read boundary across Shelf's core UI.

Browse/search is scoped in :mod:`app.routers.library_access`. This extension
covers the other high-value read surfaces: Home, dashboard metrics, Stats,
direct item/detail/edit URLs, personal-state fragments and the in-progress
feed. Inaccessible catalogue rows deliberately behave like missing rows.
"""

from __future__ import annotations

from datetime import date as _date, datetime, timedelta

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import nav
from app.auth import require_role
from app.config import MEDIA_FAMILIES, MEDIA_TYPES, MUSIC_MEDIA_TYPES
from app.currency import get_currency
from app.database import get_db
from app.routers import home_dashboard, items, pages, user_state_items
from app.routers.checkouts import OVERDUE_CONDITION, get_overdue_days
from app.services import charts, libraries, media_groups, user_state


_PHYSICAL_MEDIA_TYPES = (
    "book",
    "kids_book",
    "magazine",
    "dvd",
    "vinyl",
    "cassette",
    "cd",
    "music_other",
    "comic",
    "video_game",
)


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


def _user(request: Request) -> dict:
    return dict(request.state.user)


def _access(user: dict, *, minimum_role: str = "viewer") -> tuple[str, list]:
    return libraries.item_access_condition(
        user,
        item_alias="i",
        minimum_role=minimum_role,
    )


def _api_item_allowed(request: Request, item_id: int, minimum_role: str = "viewer") -> bool:
    with get_db() as db:
        return libraries.has_item_role(db, _user(request), item_id, minimum_role)


def _scoped_tag_suggestions(db, user: dict):
    condition, params = _access(user)
    return db.execute(
        "SELECT t.id, t.name, COUNT(DISTINCT it.item_id) AS count "
        "FROM tags t JOIN item_tags it ON it.tag_id = t.id "
        "JOIN items i ON i.id = it.item_id "
        f"WHERE {condition} GROUP BY t.id, t.name ORDER BY t.name COLLATE NOCASE",
        params,
    ).fetchall()


def _scoped_series_progress(db, user: dict, item: dict, previous: dict | None):
    series_name = str(item.get("series_name") or "").strip()
    if not series_name:
        return None
    condition, params = _access(user)
    siblings = db.execute(
        "SELECT i.owned, i.series_position FROM items i "
        f"WHERE i.series_name = ? COLLATE NOCASE AND ({condition})",
        [series_name] + params,
    ).fetchall()
    positions = [row["series_position"] for row in siblings]
    whole = [
        int(value)
        for value in positions
        if value is not None and float(value).is_integer() and float(value) >= 1
    ]
    from app.routers.series import find_gaps

    return {
        "count": len(siblings),
        "owned": sum(1 for row in siblings if row["owned"]),
        "top": max(whole) if whole else None,
        "gaps": find_gaps(positions),
        # Hardcover's published series total is external/public metadata rather
        # than a count of hidden Shelf records, so retaining it leaks no local
        # library membership.
        "hc_total": previous.get("hc_total") if previous else None,
    }


def _filter_item_detail_context(db, request: Request, context: dict) -> dict:
    """Remove secondary catalogue projections that cross the library boundary."""
    user = _user(request)
    ctx = dict(context)
    item = dict(ctx["item"])
    item_id = int(item["id"])
    visibility_sql, visibility_params = _access(user)

    visible_related = media_groups.related_items(
        db,
        item_id,
        visibility_sql=visibility_sql,
        visibility_params=visibility_params,
    )
    visible_ids = {int(row["id"]) for row in visible_related}
    ctx["linked_items"] = visible_related
    ctx["related_media"] = [
        related
        for related in (ctx.get("related_media") or [])
        if int(related["id"]) in visible_ids
    ]
    for key in ("linked_abs_items", "linked_komga_items", "linked_romm_items"):
        ctx[key] = [
            entry
            for entry in (ctx.get(key) or [])
            if int(entry["id"]) in visible_ids
        ]

    group_items = [item] + [dict(row) for row in visible_related]
    formats: list[str] = []
    visible_platforms: set[str] = set()
    for row in group_items:
        label = MEDIA_TYPES.get(row.get("media_type"), row.get("media_type"))
        if label not in formats:
            formats.append(label)
        if row.get("media_type") in ("video_game", "digital_game") and row.get("platform"):
            visible_platforms.add(str(row["platform"]))
    ctx["related_formats"] = formats
    ctx["related_game_platforms"] = [
        platform
        for platform in (ctx.get("related_game_platforms") or [])
        if str(platform.get("slug") or "") in visible_platforms
    ]

    ctx["all_tags"] = _scoped_tag_suggestions(db, user)
    ctx["series_progress"] = _scoped_series_progress(
        db,
        user,
        item,
        ctx.get("series_progress"),
    )
    ctx["can_edit_item"] = libraries.has_item_role(db, user, item_id, "editor")
    return ctx


# ---------------------------------------------------------------------------
# Home first paint
# ---------------------------------------------------------------------------

_remove_route(pages.router, "/", "GET")


@pages.router.get("/")
async def library_home(request: Request, _=Depends(require_role("viewer"))):
    user = _user(request)
    condition, params = _access(user)
    with get_db() as db:
        type_counts = {
            row["media_type"]: row["c"]
            for row in db.execute(
                "SELECT i.media_type, COUNT(*) AS c FROM items i "
                f"WHERE {condition} GROUP BY i.media_type",
                params,
            ).fetchall()
        }
        total_items = db.execute(
            f"SELECT COUNT(*) AS c FROM items i WHERE {condition}",
            params,
        ).fetchone()["c"]
        recent_items = db.execute(
            "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, i.created_at "
            "FROM items i "
            f"WHERE {condition} ORDER BY i.created_at DESC, i.id DESC LIMIT 8",
            params,
        ).fetchall()

    families = [
        {
            "key": key,
            "label": family["label"],
            "count": sum(type_counts.get(media_type, 0) for media_type in family["types"]),
            "href": "/music" if key == "music" else f"/browse?media_family_filter={key}",
        }
        for key, family in MEDIA_FAMILIES.items()
    ]
    visible_nav = nav.visible_tabs(request.state.user)
    return request.app.state.templates.TemplateResponse(
        request,
        "home.html",
        {
            "families": families,
            "total_items": total_items,
            "recent_items": recent_items,
            "media_types": MEDIA_TYPES,
            "music_media_types": MUSIC_MEDIA_TYPES,
            "intake_available": any(tab["key"] == "intake" for tab in visible_nav),
        },
    )


# ---------------------------------------------------------------------------
# Home dashboard metrics
# ---------------------------------------------------------------------------

_remove_route(pages.router, "/api/home/dashboard", "GET")


@pages.router.get("/api/home/dashboard")
async def library_home_dashboard(
    request: Request,
    _=Depends(require_role("viewer")),
):
    user = _user(request)
    user_id = int(user["id"])
    condition, params = _access(user)
    placeholders = ",".join("?" for _ in _PHYSICAL_MEDIA_TYPES)
    with get_db() as db:
        user_state.ensure_schema(db)
        owned_count = db.execute(
            f"SELECT COUNT(*) AS c FROM items i WHERE i.owned = 1 AND ({condition})",
            params,
        ).fetchone()["c"]
        wishlist_count = db.execute(
            "SELECT COUNT(*) AS c FROM user_item_state uis "
            "JOIN items i ON i.id = uis.item_id "
            f"WHERE uis.user_id = ? AND uis.wishlist = 1 AND ({condition})",
            [user_id] + params,
        ).fetchone()["c"]
        lent_out_count = db.execute(
            "SELECT COUNT(DISTINCT c.item_id) AS c FROM checkouts c "
            "JOIN items i ON i.id = c.item_id "
            f"WHERE c.checked_in IS NULL AND ({condition})",
            params,
        ).fetchone()["c"]
        overdue_count = db.execute(
            "SELECT COUNT(DISTINCT c.item_id) AS c FROM checkouts c "
            "JOIN items i ON i.id = c.item_id "
            f"WHERE {OVERDUE_CONDITION} AND ({condition})",
            [get_overdue_days(db)] + params,
        ).fetchone()["c"]
        missing_cover_count = db.execute(
            "SELECT COUNT(*) AS c FROM items i WHERE i.owned = 1 "
            f"AND (i.cover_path IS NULL OR TRIM(i.cover_path) = '') AND ({condition})",
            params,
        ).fetchone()["c"]
        unlocated_count = db.execute(
            f"SELECT COUNT(*) AS c FROM items i WHERE i.owned = 1 "
            f"AND i.media_type IN ({placeholders}) AND i.location_id IS NULL "
            f"AND ({condition})",
            list(_PHYSICAL_MEDIA_TYPES) + params,
        ).fetchone()["c"]
        attention_count = db.execute(
            f"SELECT COUNT(*) AS c FROM items i WHERE i.owned = 1 AND ("
            "i.cover_path IS NULL OR TRIM(i.cover_path) = '' OR "
            f"(i.media_type IN ({placeholders}) AND i.location_id IS NULL)) "
            f"AND ({condition})",
            list(_PHYSICAL_MEDIA_TYPES) + params,
        ).fetchone()["c"]
        locations = db.execute(
            "SELECT l.id, l.name, COUNT(i.id) AS item_count FROM locations l "
            "LEFT JOIN items i ON i.location_id = l.id AND i.owned = 1 "
            f"AND ({condition}) "
            "GROUP BY l.id, l.name, l.sort_order "
            "ORDER BY item_count DESC, l.sort_order, l.name COLLATE NOCASE LIMIT 6",
            params,
        ).fetchall()

    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/home_dashboard.html",
        {
            "owned_count": owned_count,
            "wishlist_count": wishlist_count,
            "lent_out_count": lent_out_count,
            "overdue_count": overdue_count,
            "missing_cover_count": missing_cover_count,
            "unlocated_count": unlocated_count,
            "attention_count": attention_count,
            "dashboard_locations": locations,
        },
    )


# ---------------------------------------------------------------------------
# Direct item pages
# ---------------------------------------------------------------------------

_original_item_detail = pages.item_detail
_remove_route(pages.router, "/item/{item_id}", "GET")


@pages.router.get("/item/{item_id}")
async def library_item_detail(
    request: Request,
    item_id: int,
    from_: str = Query("", alias="from"),
    _=Depends(require_role("viewer")),
):
    user = _user(request)
    with get_db() as db:
        if not libraries.has_item_role(db, user, item_id, "viewer"):
            return RedirectResponse(url="/browse", status_code=303)

    response = await _original_item_detail(
        request,
        item_id,
        from_=from_,
        _=request.state.user,
    )
    context = getattr(response, "context", None)
    if not context:
        return response
    with get_db() as db:
        filtered = _filter_item_detail_context(db, request, context)
    return request.app.state.templates.TemplateResponse(
        request,
        "item_detail.html",
        filtered,
        status_code=getattr(response, "status_code", 200),
    )


_original_item_edit = pages.item_edit
_remove_route(pages.router, "/item/{item_id}/edit", "GET")


@pages.router.get("/item/{item_id}/edit")
async def library_item_edit(
    request: Request,
    item_id: int,
    from_: str = Query("", alias="from"),
    _=Depends(require_role("viewer")),
):
    with get_db() as db:
        if not libraries.has_item_role(db, _user(request), item_id, "editor"):
            return RedirectResponse(url="/browse", status_code=303)
    return await _original_item_edit(
        request,
        item_id,
        from_=from_,
        _=request.state.user,
    )


# ---------------------------------------------------------------------------
# Personal state and in-progress feed
# ---------------------------------------------------------------------------

_original_personal_state_get = user_state_items.personal_state_fragment
_original_personal_state_post = user_state_items.update_personal_state
_original_legacy_status = user_state_items.personal_legacy_reading_status

_remove_route(items.router, "/items/{item_id}/personal-state", "GET")
_remove_route(items.router, "/items/{item_id}/personal-state", "POST")
_remove_route(items.router, "/items/{item_id}/reading-status", "POST")
_remove_route(items.router, "/home/personal-in-progress", "GET")


@items.router.get("/items/{item_id}/personal-state")
async def library_personal_state_get(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    if not _api_item_allowed(request, item_id, "viewer"):
        return HTMLResponse("Not found", status_code=404)
    return await _original_personal_state_get(request, item_id, _=request.state.user)


@items.router.post("/items/{item_id}/personal-state")
async def library_personal_state_post(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    if not _api_item_allowed(request, item_id, "viewer"):
        return HTMLResponse("Not found", status_code=404)
    return await _original_personal_state_post(request, item_id, _=request.state.user)


@items.router.post("/items/{item_id}/reading-status")
async def library_legacy_status(
    request: Request,
    item_id: int,
    status: str = "",
    _=Depends(require_role("viewer")),
):
    if not _api_item_allowed(request, item_id, "viewer"):
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    resolved = str(form.get("status") or status or "")
    return await _original_legacy_status(
        request,
        item_id,
        status=resolved,
        _=request.state.user,
    )


@items.router.get("/home/personal-in-progress")
async def library_personal_in_progress(
    request: Request,
    _=Depends(require_role("viewer")),
):
    user = _user(request)
    user_id = int(user["id"])
    condition, params = _access(user)
    with get_db() as db:
        user_state.ensure_schema(db)
        rows = db.execute(
            """SELECT i.id, i.title, i.authors, i.media_type, i.cover_path,
                      uis.progress_value, uis.progress_total, uis.progress_unit
                 FROM user_item_state uis
                 JOIN items i ON i.id = uis.item_id
                WHERE uis.user_id = ? AND uis.reading_status = 'reading'
                  AND (""" + condition + ") "
                "ORDER BY uis.updated_at DESC, i.id DESC LIMIT 6",
            [user_id] + params,
        ).fetchall()
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/personal_in_progress.html",
        {"in_progress": rows, "media_types": MEDIA_TYPES},
    )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

_remove_route(pages.router, "/stats", "GET")


@pages.router.get("/stats")
async def library_stats(request: Request, _=Depends(require_role("viewer"))):
    user = _user(request)
    user_id = int(user["id"])
    condition, params = _access(user)

    with get_db() as db:
        user_state.ensure_schema(db)
        by_type = db.execute(
            "SELECT i.media_type, COUNT(*) AS c FROM items i "
            f"WHERE {condition} GROUP BY i.media_type ORDER BY c DESC",
            params,
        ).fetchall()
        by_location = db.execute(
            "SELECT COALESCE(l.name, 'Unassigned') AS name, COUNT(*) AS c "
            "FROM items i LEFT JOIN locations l ON i.location_id = l.id "
            f"WHERE {condition} GROUP BY l.name ORDER BY c DESC",
            params,
        ).fetchall()
        total = db.execute(
            f"SELECT COUNT(*) AS c FROM items i WHERE {condition}", params
        ).fetchone()["c"]
        stats_owned = db.execute(
            f"SELECT COUNT(*) AS c FROM items i WHERE i.owned = 1 AND ({condition})",
            params,
        ).fetchone()["c"]
        stats_wishlist = db.execute(
            "SELECT COUNT(*) AS c FROM user_item_state uis "
            "JOIN items i ON i.id = uis.item_id "
            f"WHERE uis.user_id = ? AND uis.wishlist = 1 AND ({condition})",
            [user_id] + params,
        ).fetchone()["c"]
        with_covers = db.execute(
            "SELECT COUNT(*) AS c FROM items i "
            f"WHERE i.cover_path IS NOT NULL AND ({condition})",
            params,
        ).fetchone()["c"]
        without_isbn = db.execute(
            f"SELECT COUNT(*) AS c FROM items i WHERE i.isbn IS NULL AND ({condition})",
            params,
        ).fetchone()["c"]
        recent = db.execute(
            "SELECT i.*, l.name AS location_name FROM items i "
            "LEFT JOIN locations l ON i.location_id = l.id "
            "WHERE i.created_at >= datetime('now', '-30 days') "
            f"AND ({condition}) ORDER BY i.created_at DESC LIMIT 20",
            params,
        ).fetchall()
        read_by_year = db.execute(
            "SELECT substr(uis.date_finished, 1, 4) AS y, COUNT(*) AS c "
            "FROM user_item_state uis JOIN items i ON i.id = uis.item_id "
            "WHERE uis.user_id = ? AND uis.reading_status = 'read' "
            f"AND uis.date_finished IS NOT NULL AND ({condition}) "
            "GROUP BY y ORDER BY y",
            [user_id] + params,
        ).fetchall()
        growth_rows = db.execute(
            "SELECT substr(i.created_at, 1, 7) AS m, COUNT(*) AS c FROM items i "
            f"WHERE {condition} GROUP BY m ORDER BY m",
            params,
        ).fetchall()
        author_rows = db.execute(
            "SELECT i.authors, COUNT(*) AS c FROM items i "
            "WHERE i.authors IS NOT NULL AND TRIM(i.authors) != '' "
            f"AND ({condition}) GROUP BY i.authors",
            params,
        ).fetchall()
        current_value = db.execute(
            "SELECT COALESCE(SUM(COALESCE(i.manual_value, i.estimated_value)), 0) AS v "
            "FROM items i WHERE COALESCE(i.manual_value, i.estimated_value) IS NOT NULL "
            f"AND ({condition})",
            params,
        ).fetchone()["v"]
        # valuation_history stores one historical household aggregate and
        # cannot be reconstructed safely by library. Only a global admin may
        # see that historical series; everyone still receives their current
        # accessible-library value above.
        valuation_rows = (
            db.execute(
                "SELECT substr(created_at, 1, 10) AS d, total_value "
                "FROM valuation_history ORDER BY created_at"
            ).fetchall()
            if user.get("role") == "admin"
            else []
        )

    current_year = str(_date.today().year)
    read_pairs = [(row["y"], row["c"]) for row in read_by_year]
    read_this_year = dict(read_pairs).get(current_year, 0)

    running = 0
    growth_pairs = []
    for row in growth_rows:
        running += row["c"]
        growth_pairs.append((row["m"], running))

    author_counts: dict[str, int] = {}
    for row in author_rows:
        first = row["authors"].split(",")[0].strip()
        if first:
            author_counts[first] = author_counts.get(first, 0) + row["c"]
    top_authors = sorted(author_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    valuation_pairs = [(row["d"], row["total_value"]) for row in valuation_rows]

    chart_read = charts.column_chart(
        read_pairs,
        empty_message="Mark media as completed to build this chart",
    )
    chart_growth = charts.area_chart(growth_pairs, empty_message="No items yet")
    chart_authors = charts.hbar_chart(top_authors, empty_message="No authors yet")
    currency = get_currency()
    if currency.suffix:
        value_prefix, value_suffix = "", " " + currency.symbol
    else:
        value_prefix, value_suffix = currency.symbol, ""
    chart_valuation = (
        charts.area_chart(
            valuation_pairs,
            value_prefix=value_prefix,
            value_suffix=value_suffix,
            empty_message="Run a batch valuation to start tracking value over time",
        )
        if len(valuation_pairs) >= 2
        else None
    )

    return request.app.state.templates.TemplateResponse(
        request,
        "stats.html",
        {
            "by_type": by_type,
            "by_location": by_location,
            "total": total,
            "owned_count": stats_owned,
            "wishlist_count": stats_wishlist,
            "with_covers": with_covers,
            "without_isbn": without_isbn,
            "recent": recent,
            "media_types": MEDIA_TYPES,
            "read_this_year": read_this_year,
            "current_year": current_year,
            "current_value": current_value,
            "chart_read": chart_read,
            "chart_growth": chart_growth,
            "chart_authors": chart_authors,
            "chart_valuation": chart_valuation,
        },
    )
