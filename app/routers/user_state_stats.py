"""Per-user activity metrics for Shelf's shared Stats page."""

from datetime import date as _date

from fastapi import Depends, Request

from app.auth import require_role
from app.config import MEDIA_TYPES
from app.currency import get_currency
from app.database import get_db
from app.routers import pages
from app.services import charts, user_state


# pages.router is not mounted into the FastAPI app until app.main finishes
# importing this package, so replacing the route here is deterministic.
pages.router.routes[:] = [
    route
    for route in pages.router.routes
    if not (
        getattr(route, "path", None) == "/stats"
        and "GET" in (getattr(route, "methods", None) or set())
    )
]


@pages.router.get("/stats")
async def personal_stats(request: Request, _=Depends(require_role("viewer"))):
    """Render shared catalogue metrics plus the signed-in user's activity."""
    user_id = int(request.state.user["id"])
    with get_db() as db:
        user_state.ensure_schema(db)
        by_type = db.execute(
            "SELECT media_type, COUNT(*) as c FROM items GROUP BY media_type ORDER BY c DESC"
        ).fetchall()
        by_location = db.execute(
            "SELECT COALESCE(l.name, 'Unassigned') as name, COUNT(*) as c "
            "FROM items i LEFT JOIN locations l ON i.location_id = l.id "
            "GROUP BY l.name ORDER BY c DESC"
        ).fetchall()
        total = db.execute("SELECT COUNT(*) as c FROM items").fetchone()["c"]
        stats_owned = db.execute(
            "SELECT COUNT(*) as c FROM items WHERE owned = 1"
        ).fetchone()["c"]
        stats_wishlist = db.execute(
            "SELECT COUNT(*) as c FROM user_item_state "
            "WHERE user_id = ? AND wishlist = 1",
            (user_id,),
        ).fetchone()["c"]
        with_covers = db.execute(
            "SELECT COUNT(*) as c FROM items WHERE cover_path IS NOT NULL"
        ).fetchone()["c"]
        without_isbn = db.execute(
            "SELECT COUNT(*) as c FROM items WHERE isbn IS NULL"
        ).fetchone()["c"]
        recent = db.execute(
            "SELECT i.*, l.name as location_name FROM items i "
            "LEFT JOIN locations l ON i.location_id = l.id "
            "WHERE i.created_at >= datetime('now', '-30 days') "
            "ORDER BY i.created_at DESC LIMIT 20"
        ).fetchall()

        read_by_year = db.execute(
            """SELECT substr(date_finished, 1, 4) AS y, COUNT(*) AS c
                 FROM user_item_state
                WHERE user_id = ?
                  AND reading_status = 'read'
                  AND date_finished IS NOT NULL
                GROUP BY y ORDER BY y""",
            (user_id,),
        ).fetchall()
        growth_rows = db.execute(
            "SELECT substr(created_at, 1, 7) as m, COUNT(*) as c FROM items "
            "GROUP BY m ORDER BY m"
        ).fetchall()
        author_rows = db.execute(
            "SELECT authors, COUNT(*) as c FROM items "
            "WHERE authors IS NOT NULL AND TRIM(authors) != '' GROUP BY authors"
        ).fetchall()
        valuation_rows = db.execute(
            "SELECT substr(created_at, 1, 10) as d, total_value FROM valuation_history "
            "ORDER BY created_at"
        ).fetchall()
        current_value = db.execute(
            "SELECT COALESCE(SUM(COALESCE(manual_value, estimated_value)), 0) as v FROM items "
            "WHERE COALESCE(manual_value, estimated_value) IS NOT NULL"
        ).fetchone()["v"]

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
        chart_value_prefix, chart_value_suffix = "", " " + currency.symbol
    else:
        chart_value_prefix, chart_value_suffix = currency.symbol, ""
    chart_valuation = (
        charts.area_chart(
            valuation_pairs,
            value_prefix=chart_value_prefix,
            value_suffix=chart_value_suffix,
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
