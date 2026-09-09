from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


# Home summary accepts an optional actor so direct service callers/tests keep
# the historical all-catalogue behaviour, while the actual Home route can
# project only libraries visible to the signed-in user.
replace_once(
    "app/services/home_dashboard.py",
    "from __future__ import annotations\n\n\n",
    "from __future__ import annotations\n\nfrom app.services import libraries\n\n\n",
)
p = Path("app/services/home_dashboard.py")
s = p.read_text()
start = s.index("def dashboard_summary(")
# This module intentionally contains only this one public function, so replace
# its implementation as one coherent query projection.
s = s[:start] + r'''def dashboard_summary(db, *, user: dict | None = None, recent_limit: int = 8) -> dict:
    """Return stable, presentation-neutral metrics for the visible catalogue."""
    if user is None:
        access_sql, access_params = "1 = 1", []
    else:
        access_sql, access_params = libraries.item_access_condition(
            user, item_alias="i"
        )

    total = db.execute(
        f"SELECT COUNT(*) AS c FROM items i WHERE {access_sql}", access_params
    ).fetchone()["c"]
    owned = db.execute(
        f"SELECT COUNT(*) AS c FROM items i WHERE i.owned = 1 AND {access_sql}",
        access_params,
    ).fetchone()["c"]
    wishlist = db.execute(
        f"SELECT COUNT(*) AS c FROM items i WHERE i.owned = 0 AND {access_sql}",
        access_params,
    ).fetchone()["c"]
    lent_out = db.execute(
        "SELECT COUNT(DISTINCT c.item_id) AS c FROM checkouts c "
        "JOIN items i ON i.id = c.item_id "
        f"WHERE c.checked_in IS NULL AND {access_sql}",
        access_params,
    ).fetchone()["c"]
    missing_cover = db.execute(
        "SELECT COUNT(*) AS c FROM items i "
        "WHERE (i.cover_path IS NULL OR TRIM(i.cover_path) = '') "
        f"AND i.cover_review_dismissed = 0 AND {access_sql}",
        access_params,
    ).fetchone()["c"]

    type_rows = db.execute(
        "SELECT i.media_type, COUNT(*) AS item_count, "
        "SUM(CASE WHEN i.owned = 1 THEN 1 ELSE 0 END) AS owned_count, "
        "SUM(CASE WHEN i.owned = 0 THEN 1 ELSE 0 END) AS wishlist_count "
        f"FROM items i WHERE {access_sql} GROUP BY i.media_type "
        "ORDER BY item_count DESC, i.media_type COLLATE NOCASE",
        access_params,
    ).fetchall()
    media_types = [dict(row) for row in type_rows]

    limit = max(0, min(int(recent_limit), 50))
    recent = []
    if limit:
        recent = [
            dict(row)
            for row in db.execute(
                "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, "
                "i.owned, i.created_at FROM items i "
                f"WHERE {access_sql} "
                "ORDER BY i.created_at DESC, i.id DESC LIMIT ?",
                [*access_params, limit],
            ).fetchall()
        ]

    return {
        "total_count": total,
        "owned_count": owned,
        "wishlist_count": wishlist,
        "lent_out_count": lent_out,
        "missing_cover_count": missing_cover,
        "media_types": media_types,
        "recent_items": recent,
    }
'''
p.write_text(s)

# Home passes the actor to the summary service.
replace_once(
    "app/routers/pages.py",
    '''    with get_db() as db:\n        summary = dashboard_summary(db, recent_limit=8)\n''',
    '''    actor = dict(request.state.user)\n    with get_db() as db:\n        summary = dashboard_summary(db, user=actor, recent_limit=8)\n''',
)

# Replace the Stats query block while preserving all chart rendering below it.
p = Path("app/routers/pages.py")
s = p.read_text()
old_start = '''@router.get("/stats")\nasync def stats(request: Request, _=Depends(require_role("viewer"))):\n    with get_db() as db:\n'''
if old_start not in s:
    raise SystemExit("stats route anchor not found")
start = s.index(old_start)
chart_marker = "\n    from datetime import date as _date\n"
end = s.index(chart_marker, start)
new_block = r'''@router.get("/stats")
async def stats(request: Request, _=Depends(require_role("viewer"))):
    actor = dict(request.state.user)
    with get_db() as db:
        access_sql, access_params = libraries.item_access_condition(
            actor, item_alias="i"
        )
        by_type = db.execute(
            "SELECT i.media_type, COUNT(*) as c FROM items i "
            f"WHERE {access_sql} GROUP BY i.media_type ORDER BY c DESC",
            access_params,
        ).fetchall()
        by_location = db.execute(
            "SELECT COALESCE(l.name, 'Unassigned') as name, COUNT(*) as c "
            "FROM items i LEFT JOIN locations l ON i.location_id = l.id "
            f"WHERE {access_sql} GROUP BY l.name ORDER BY c DESC",
            access_params,
        ).fetchall()
        total = db.execute(
            f"SELECT COUNT(*) as c FROM items i WHERE {access_sql}", access_params
        ).fetchone()["c"]
        stats_wishlist = db.execute(
            f"SELECT COUNT(*) as c FROM items i WHERE i.owned = 0 AND {access_sql}",
            access_params,
        ).fetchone()["c"]
        stats_owned = total - stats_wishlist
        with_covers = db.execute(
            f"SELECT COUNT(*) as c FROM items i WHERE i.cover_path IS NOT NULL AND {access_sql}",
            access_params,
        ).fetchone()["c"]
        without_isbn = db.execute(
            f"SELECT COUNT(*) as c FROM items i WHERE i.isbn IS NULL AND {access_sql}",
            access_params,
        ).fetchone()["c"]
        recent = db.execute(
            "SELECT i.*, l.name as location_name FROM items i "
            "LEFT JOIN locations l ON i.location_id = l.id "
            "WHERE i.created_at >= datetime('now', '-30 days') "
            f"AND {access_sql} ORDER BY i.created_at DESC LIMIT 20",
            access_params,
        ).fetchall()

        read_by_year = db.execute(
            "SELECT substr(i.date_finished, 1, 4) as y, COUNT(*) as c FROM items i "
            "WHERE i.reading_status = 'read' AND i.date_finished IS NOT NULL "
            f"AND {access_sql} GROUP BY y ORDER BY y",
            access_params,
        ).fetchall()
        growth_rows = db.execute(
            "SELECT substr(i.created_at, 1, 7) as m, COUNT(*) as c FROM items i "
            f"WHERE {access_sql} GROUP BY m ORDER BY m",
            access_params,
        ).fetchall()
        author_rows = db.execute(
            "SELECT i.authors, COUNT(*) as c FROM items i "
            "WHERE i.authors IS NOT NULL AND TRIM(i.authors) != '' "
            f"AND {access_sql} GROUP BY i.authors",
            access_params,
        ).fetchall()

        # valuation_history stores one global aggregate with no library key, so
        # it cannot be safely projected for a restricted user. Keep historical
        # valuation visible only to the site-wide admin until history itself is
        # library-aware; current value below is safely recomputed from visible rows.
        valuation_rows = []
        if actor.get("role") == "admin":
            valuation_rows = db.execute(
                "SELECT substr(created_at, 1, 10) as d, total_value "
                "FROM valuation_history ORDER BY created_at"
            ).fetchall()
        current_value = db.execute(
            "SELECT COALESCE(SUM(COALESCE(i.manual_value, i.estimated_value)), 0) as v "
            "FROM items i WHERE COALESCE(i.manual_value, i.estimated_value) IS NOT NULL "
            f"AND {access_sql}",
            access_params,
        ).fetchone()["v"]
'''
s = s[:start] + new_block + s[end:]
p.write_text(s)

Path("tests/test_library_home_stats_039.py").write_text(r'''"""Home and Stats aggregate reads respect first-class Shelf libraries."""

from app.services import home_dashboard, libraries
from tests.conftest import _insert_item


def _item(db, library_id: int, title: str, **fields) -> int:
    item_id = _insert_item(db, title=title, isbn=None, **fields)
    libraries.assign_item(db, item_id, library_id)
    return item_id


def test_home_summary_counts_only_visible_libraries(db, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    _item(db, 1, "Visible Home Item", media_type="book", owned=1)
    private = libraries.create_library(db, "Private Home")
    _item(db, private["id"], "Hidden Home Item", media_type="dvd", owned=0)

    summary = home_dashboard.dashboard_summary(db, user=viewer_user, recent_limit=10)
    assert summary["total_count"] == 1
    assert summary["owned_count"] == 1
    assert summary["wishlist_count"] == 0
    assert [row["title"] for row in summary["recent_items"]] == ["Visible Home Item"]
    assert {row["media_type"] for row in summary["media_types"]} == {"book"}


def test_home_page_does_not_render_private_recent_item(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    _item(db, 1, "Visible Home Card")
    private = libraries.create_library(db, "Private Home Cards")
    _item(db, private["id"], "Hidden Home Card")
    db.commit()

    html = viewer_client.get("/").text
    assert "Visible Home Card" in html
    assert "Hidden Home Card" not in html


def test_stats_hides_private_titles_locations_authors_and_value(
    db, viewer_client, viewer_user
):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    visible = _item(
        db, 1, "Visible Stats Item", authors="Visible Author", manual_value=10.0
    )
    private = libraries.create_library(db, "Private Stats")
    hidden = _item(
        db,
        private["id"],
        "Hidden Stats Item",
        authors="Secret Stats Author",
        manual_value=9999.0,
    )
    hidden_location = db.execute(
        "INSERT INTO locations (name) VALUES ('Secret Stats Vault')"
    ).lastrowid
    db.execute("UPDATE items SET location_id = ? WHERE id = ?", (hidden_location, hidden))
    db.commit()

    html = viewer_client.get("/stats").text
    assert "Visible Stats Item" in html
    assert "Hidden Stats Item" not in html
    assert "Secret Stats Author" not in html
    assert "Secret Stats Vault" not in html
    assert "9999" not in html
    assert visible


def test_non_admin_stats_do_not_render_global_valuation_history(
    db, viewer_client, viewer_user
):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    _item(db, 1, "Visible Value", manual_value=5.0)
    db.execute(
        "INSERT INTO valuation_history (total_value, created_at) VALUES (?, datetime('now', '-1 day'))",
        (123456.0,),
    )
    db.execute(
        "INSERT INTO valuation_history (total_value, created_at) VALUES (?, datetime('now'))",
        (234567.0,),
    )
    db.commit()

    html = viewer_client.get("/stats").text
    assert "123456" not in html
    assert "234567" not in html


def test_admin_home_summary_retains_global_recovery_view(db, admin_user):
    private = libraries.create_library(db, "Admin Home Recovery")
    _item(db, private["id"], "Admin Global Home")
    summary = home_dashboard.dashboard_summary(db, user=admin_user, recent_limit=10)
    assert any(row["title"] == "Admin Global Home" for row in summary["recent_items"])
''')
