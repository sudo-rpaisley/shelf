from pathlib import Path

pages = Path("app/routers/pages.py")
text = pages.read_text()

old_index = '''    with get_db() as db:
        summary = dashboard_summary(db, recent_limit=8)
'''
new_index = '''    with get_db() as db:
        summary = dashboard_summary(
            db,
            recent_limit=8,
            user=dict(request.state.user),
        )
'''
if old_index not in text:
    raise SystemExit("Home summary anchor not found")
text = text.replace(old_index, new_index, 1)

start = text.index('@router.get("/stats")')
end = text.index('@router.get("/logs")', start)
new_stats = '''@router.get("/stats")
async def stats(request: Request, _=Depends(require_role("viewer"))):
    user = dict(request.state.user)
    user_id = int(user["id"])

    with get_db() as db:
        access_sql, access_params = libraries.item_access_condition(
            user, item_alias="i"
        )

        by_type = db.execute(
            "SELECT i.media_type, COUNT(*) as c FROM items i "
            f"WHERE ({access_sql}) GROUP BY i.media_type ORDER BY c DESC",
            access_params,
        ).fetchall()
        by_location = db.execute(
            "SELECT COALESCE(l.name, 'Unassigned') as name, COUNT(*) as c "
            "FROM items i LEFT JOIN locations l ON i.location_id = l.id "
            f"WHERE ({access_sql}) GROUP BY l.name ORDER BY c DESC",
            access_params,
        ).fetchall()
        total = db.execute(
            f"SELECT COUNT(*) as c FROM items i WHERE ({access_sql})",
            access_params,
        ).fetchone()["c"]
        stats_owned = db.execute(
            f"SELECT COUNT(*) as c FROM items i WHERE i.owned = 1 AND ({access_sql})",
            access_params,
        ).fetchone()["c"]
        stats_wishlist = db.execute(
            "SELECT COUNT(*) as c FROM items i "
            "JOIN user_item_state uis ON uis.item_id = i.id AND uis.user_id = ? "
            f"WHERE uis.wishlist = 1 AND ({access_sql})",
            [user_id, *access_params],
        ).fetchone()["c"]
        with_covers = db.execute(
            "SELECT COUNT(*) as c FROM items i "
            f"WHERE i.cover_path IS NOT NULL AND ({access_sql})",
            access_params,
        ).fetchone()["c"]
        without_isbn = db.execute(
            "SELECT COUNT(*) as c FROM items i "
            f"WHERE i.isbn IS NULL AND ({access_sql})",
            access_params,
        ).fetchone()["c"]
        recent = db.execute(
            "SELECT i.*, l.name as location_name FROM items i "
            "LEFT JOIN locations l ON i.location_id = l.id "
            "WHERE i.created_at >= datetime('now', '-30 days') "
            f"AND ({access_sql}) ORDER BY i.created_at DESC LIMIT 20",
            access_params,
        ).fetchall()

        # Consumption is personal state, not the legacy shared item columns.
        read_by_year = db.execute(
            "SELECT substr(uis.date_finished, 1, 4) as y, COUNT(*) as c "
            "FROM user_item_state uis JOIN items i ON i.id = uis.item_id "
            "WHERE uis.user_id = ? AND uis.reading_status = 'read' "
            "AND uis.date_finished IS NOT NULL "
            f"AND ({access_sql}) GROUP BY y ORDER BY y",
            [user_id, *access_params],
        ).fetchall()
        growth_rows = db.execute(
            "SELECT substr(i.created_at, 1, 7) as m, COUNT(*) as c FROM items i "
            f"WHERE ({access_sql}) GROUP BY m ORDER BY m",
            access_params,
        ).fetchall()
        author_rows = db.execute(
            "SELECT i.authors, COUNT(*) as c FROM items i "
            "WHERE i.authors IS NOT NULL AND TRIM(i.authors) != '' "
            f"AND ({access_sql}) GROUP BY i.authors",
            access_params,
        ).fetchall()

        # Historical valuation snapshots are whole-catalogue records with no
        # per-library dimensions. Non-admins must not see those global totals.
        if user.get("role") == "admin":
            valuation_rows = db.execute(
                "SELECT substr(created_at, 1, 10) as d, total_value "
                "FROM valuation_history ORDER BY created_at"
            ).fetchall()
        else:
            valuation_rows = []

        current_value = db.execute(
            "SELECT COALESCE(SUM(COALESCE(i.manual_value, i.estimated_value)), 0) as v "
            "FROM items i WHERE COALESCE(i.manual_value, i.estimated_value) IS NOT NULL "
            f"AND ({access_sql})",
            access_params,
        ).fetchone()["v"]

    from datetime import date as _date
    current_year = str(_date.today().year)
    read_pairs = [(r["y"], r["c"]) for r in read_by_year]
    read_this_year = dict(read_pairs).get(current_year, 0)

    running = 0
    growth_pairs = []
    for r in growth_rows:
        running += r["c"]
        growth_pairs.append((r["m"], running))

    # Aggregate by first author (the authors column is a comma-joined string).
    author_counts: dict[str, int] = {}
    for r in author_rows:
        first = r["authors"].split(",")[0].strip()
        if first:
            author_counts[first] = author_counts.get(first, 0) + r["c"]
    top_authors = sorted(author_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]

    valuation_pairs = [(r["d"], r["total_value"]) for r in valuation_rows]

    from app.services import charts
    chart_read = charts.column_chart(
        read_pairs,
        empty_message="Mark items as read (with a finish date) to build this chart",
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
            "valuation_history_restricted": user.get("role") != "admin",
        },
    )


'''
text = text[:start] + new_stats + text[end:]
pages.write_text(text)

home = Path("app/templates/home.html")
home_text = home.read_text()
old_badge = "{% if not item.owned %}"
if old_badge not in home_text:
    raise SystemExit("Home wishlist badge anchor not found")
home.write_text(home_text.replace(old_badge, "{% if item.wishlist %}", 1))

stats = Path("app/templates/stats.html")
stats_text = stats.read_text()
old_empty = '''        {% else %}
        <p class="text-sm text-shelf-muted py-8 text-center">Run batch valuations (Settings &rarr; Integrations &rarr; ISBNdb) at least twice to see value history.</p>
        {% endif %}'''
new_empty = '''        {% elif valuation_history_restricted %}
        <p class="text-sm text-shelf-muted py-8 text-center">Value-history snapshots cover the whole catalogue, so this chart is available to administrators only.</p>
        {% else %}
        <p class="text-sm text-shelf-muted py-8 text-center">Run batch valuations (Settings &rarr; Integrations &rarr; ISBNdb) at least twice to see value history.</p>
        {% endif %}'''
if old_empty not in stats_text:
    raise SystemExit("Stats valuation empty-state anchor not found")
stats.write_text(stats_text.replace(old_empty, new_empty, 1))
