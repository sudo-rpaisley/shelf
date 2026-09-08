"""Per-user adaptations for Browse and its HTMX search endpoint.

The catalogue remains shared, but consumption status and Wishlist are account
state. This module replaces only the two Browse read routes before FastAPI
mounts the existing page and item routers.
"""

from datetime import datetime, timedelta

from fastapi import Depends, Request

from app import browse_filters
from app.auth import require_role
from app.config import DEFAULT_PAGE_SIZE, MEDIA_TYPES
from app.database import get_db
from app.routers import items, pages
from app.routers.items_common import SORT_OPTIONS
from app.services import user_state_browse


def _remove_route(router, path: str, method: str) -> None:
    method = method.upper()
    prefix = getattr(router, "prefix", "") or ""
    full_path = f"{prefix}{path}" if prefix else path
    router.routes[:] = [
        route for route in router.routes
        if not (
            getattr(route, "path", None) == full_path
            and method in (getattr(route, "methods", None) or set())
        )
    ]


def _user_id(request: Request) -> int:
    return int(request.state.user["id"])


def _browse_rows(db, where: str, params: list, order_clause: str, *, limit: int, offset: int | None = None):
    from app.routers.checkouts import OVERDUE_CONDITION, get_overdue_days

    if offset is None:
        pagination_sql = " LIMIT ?"
        pagination_params = [limit]
    else:
        pagination_sql = " LIMIT ? OFFSET ?"
        pagination_params = [limit, offset]
    rows = db.execute(
        f"SELECT i.*, l.name as location_name, "
        f"(SELECT b.name FROM checkouts c JOIN borrowers b ON c.borrower_id = b.id "
        f" WHERE c.item_id = i.id AND c.checked_in IS NULL LIMIT 1) AS lent_to, "
        f"(SELECT 1 FROM checkouts c WHERE c.item_id = i.id AND {OVERDUE_CONDITION} LIMIT 1) AS lent_overdue "
        f"FROM items i "
        f"LEFT JOIN locations l ON i.location_id = l.id "
        f"{where} ORDER BY {order_clause}{pagination_sql}",
        [get_overdue_days(db)] + list(params) + pagination_params,
    ).fetchall()
    return [dict(row) for row in rows]


_remove_route(pages.router, "/browse", "GET")


@pages.router.get("/browse")
async def personal_browse(request: Request, _=Depends(require_role("viewer"))):
    values = browse_filters.values_from(request.query_params)
    values["q"] = values["q"][:200]
    uid = _user_id(request)
    where, params = browse_filters.build_where(values, user_id=uid)

    with get_db() as db:
        _, order_clause = SORT_OPTIONS.get(values["sort"], SORT_OPTIONS["newest"])
        result_items = _browse_rows(db, where, params, order_clause, limit=DEFAULT_PAGE_SIZE)
        user_state_browse.overlay_items(db, uid, result_items)
        total_filtered = db.execute(
            f"SELECT COUNT(*) as c FROM items i {where}", params
        ).fetchone()["c"]
        series_names = [
            row["series_name"] for row in db.execute(
                "SELECT DISTINCT series_name FROM items "
                "WHERE series_name IS NOT NULL AND TRIM(series_name) != '' "
                "ORDER BY series_name COLLATE NOCASE"
            ).fetchall()
        ]
        counts = user_state_browse.filter_counts(db, values, total_filtered, uid)
        lent_out_count = db.execute(
            "SELECT COUNT(DISTINCT item_id) as c FROM checkouts WHERE checked_in IS NULL"
        ).fetchone()["c"]
        from app.routers.tags import get_all_tags
        all_tags = get_all_tags(db)
        item_languages = [
            row["language"] for row in db.execute(
                "SELECT DISTINCT language FROM items "
                "WHERE language IS NOT NULL AND language != '' ORDER BY language"
            ).fetchall()
        ]
        has_more = len(result_items) < total_filtered
        load_more_url = "/api/search?" + browse_filters.querystring(values, extra=["page=2"])

    ctx = {
        "items": result_items,
        "media_types": MEDIA_TYPES,
        "series_names": series_names,
        "all_tags": all_tags,
        "lent_out_count": lent_out_count,
        "item_languages": item_languages,
        "has_more": has_more,
        "has_filters": browse_filters.has_active_filters(values),
        "load_more_url": load_more_url,
        "seven_days_ago": (datetime.now(tz=None) - timedelta(days=7)).strftime("%Y-%m-%d"),
        "initial_query": values["q"],
        "initial_filters": {name: values[name] for name in browse_filters.FILTER_NAMES},
    }
    ctx.update(counts)
    return request.app.state.templates.TemplateResponse(request, "browse.html", ctx)


_remove_route(items.router, "/search", "GET")


@items.router.get("/search")
async def personal_search_items(
    request: Request,
    page: int = 1,
    per_page: int = DEFAULT_PAGE_SIZE,
    _=Depends(require_role("viewer")),
):
    templates = request.app.state.templates
    values = browse_filters.values_from(request.query_params)
    values["q"] = values["q"][:200]
    uid = _user_id(request)
    where, params = browse_filters.build_where(values, user_id=uid)
    _, order_clause = SORT_OPTIONS.get(values["sort"], SORT_OPTIONS["newest"])
    offset = (max(page, 1) - 1) * per_page

    with get_db() as db:
        total = db.execute(f"SELECT COUNT(*) as c FROM items i {where}", params).fetchone()["c"]
        result_items = _browse_rows(db, where, params, order_clause, limit=per_page, offset=offset)
        user_state_browse.overlay_items(db, uid, result_items)
        counts = user_state_browse.filter_counts(db, values, total, uid) if page <= 1 else None

    has_more = (offset + per_page) < total
    load_more_url = "/api/search?" + browse_filters.querystring(values, extra=[f"page={page + 1}"])
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
        "seven_days_ago": (datetime.now(tz=None) - timedelta(days=7)).strftime("%Y-%m-%d"),
    }
    if counts:
        ctx.update(counts)
        ctx["render_oob_counts"] = True
    return templates.TemplateResponse(request, template, ctx)
