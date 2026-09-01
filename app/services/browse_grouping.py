"""Browse pagination helpers for grouping Digital Comics by series.

Komga libraries can contain thousands of individual issues.  The ordinary
Browse page therefore treats a Digital Comic series as one display entry by
default while retaining ordinary item semantics everywhere else.  Grouping is
performed before LIMIT/OFFSET so a long series cannot consume an entire page or
reappear on later pages.
"""

from __future__ import annotations

from urllib.parse import urlencode

from app.database import get_setting

SETTING_KEY = "browse_group_digital_comics"
_FALSE_VALUES = {"0", "false", "off", "no"}
_GROUPABLE = (
    "i.media_type = 'digital_comic' "
    "AND i.series_name IS NOT NULL AND TRIM(i.series_name) != ''"
)
_GROUP_KEY = (
    "CASE WHEN " + _GROUPABLE + " "
    "THEN 'series:' || LOWER(TRIM(i.series_name)) "
    "ELSE 'item:' || CAST(i.id AS TEXT) END"
)


def grouping_enabled(db, values: dict) -> bool:
    """Whether this Browse request should collapse Digital Comic series.

    An explicit series drill-down always shows the individual issues.  With no
    saved preference the feature is on, so existing installations gain the
    compact Komga-friendly view without another setup step.
    """
    if values.get("series"):
        return False
    raw = get_setting(db, SETTING_KEY)
    return str(raw if raw is not None else "1").strip().lower() not in _FALSE_VALUES


def _plain_page(db, where: str, params: list, order_clause: str, *, limit: int, offset: int):
    from app.routers.checkouts import OVERDUE_CONDITION, get_overdue_days

    raw_total = db.execute(
        f"SELECT COUNT(*) AS c FROM items i {where}", params
    ).fetchone()["c"]
    rows = db.execute(
        f"SELECT i.*, l.name as location_name, "
        f"(SELECT b.name FROM checkouts c JOIN borrowers b ON c.borrower_id = b.id "
        f" WHERE c.item_id = i.id AND c.checked_in IS NULL LIMIT 1) AS lent_to, "
        f"(SELECT 1 FROM checkouts c WHERE c.item_id = i.id "
        f" AND {OVERDUE_CONDITION} LIMIT 1) AS lent_overdue "
        f"FROM items i LEFT JOIN locations l ON i.location_id = l.id "
        f"{where} ORDER BY {order_clause} LIMIT ? OFFSET ?",
        [get_overdue_days(db)] + list(params) + [limit, offset],
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["browse_series_group"] = False
        item["browse_series_count"] = 1
        item["browse_series_url"] = None
        items.append(item)
    return items, raw_total, raw_total


def _earliest_series_covers(db, series_keys: list[str]) -> dict[str, str | None]:
    """Return the cover belonging to the earliest issue in each series.

    Series position is authoritative.  Rows without a number sort after numbered
    issues, with publication year and item id providing deterministic fallbacks.
    The earliest issue is selected even if its cover is currently missing; a
    later Komga sync can fill that cover without silently changing which issue
    represents the series.
    """
    if not series_keys:
        return {}
    placeholders = ",".join("?" for _ in series_keys)
    rows = db.execute(
        f"""WITH ranked AS (
            SELECT LOWER(TRIM(series_name)) AS series_key, cover_path,
                   ROW_NUMBER() OVER (
                       PARTITION BY LOWER(TRIM(series_name))
                       ORDER BY (series_position IS NULL), series_position ASC,
                                (publish_year IS NULL), publish_year ASC, id ASC
                   ) AS issue_rank
              FROM items
             WHERE media_type = 'digital_comic'
               AND series_name IS NOT NULL AND TRIM(series_name) != ''
               AND LOWER(TRIM(series_name)) IN ({placeholders})
        )
        SELECT series_key, cover_path FROM ranked WHERE issue_rank = 1""",
        series_keys,
    ).fetchall()
    return {row["series_key"]: row["cover_path"] for row in rows}


def fetch_page(
    db,
    where: str,
    params: list,
    order_clause: str,
    *,
    limit: int,
    offset: int,
    values: dict,
):
    """Fetch one Browse page, collapsing Digital Comic series when enabled.

    Returns ``(items, raw_total, display_total)``. ``raw_total`` remains the
    number used by collection/filter counts; ``display_total`` is the number of
    cards/rows after grouping and therefore drives pagination.
    """
    if not grouping_enabled(db, values):
        return _plain_page(
            db, where, params, order_clause, limit=limit, offset=offset
        )

    from app.routers.checkouts import OVERDUE_CONDITION, get_overdue_days

    raw_total = db.execute(
        f"SELECT COUNT(*) AS c FROM items i {where}", params
    ).fetchone()["c"]
    display_total = db.execute(
        f"SELECT COUNT(DISTINCT {_GROUP_KEY}) AS c FROM items i {where}", params
    ).fetchone()["c"]

    # The row that determines a group's place in the current sort can differ
    # from the row that supplies its artwork.  This keeps normal Browse sorting
    # useful (for example a series with a newly-added issue can still sort as
    # recent), while _earliest_series_covers always supplies issue #1's cover.
    ranked = db.execute(
        f"""WITH grouped AS (
            SELECT i.id,
                   {_GROUP_KEY} AS browse_group_key,
                   COUNT(*) OVER (PARTITION BY {_GROUP_KEY}) AS browse_series_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY {_GROUP_KEY}
                       ORDER BY {order_clause}
                   ) AS browse_group_rank
              FROM items i
              {where}
        )
        SELECT g.id, g.browse_group_key, g.browse_series_count
          FROM grouped g
          JOIN items i ON i.id = g.id
         WHERE g.browse_group_rank = 1
         ORDER BY {order_clause}
         LIMIT ? OFFSET ?""",
        list(params) + [limit, offset],
    ).fetchall()

    if not ranked:
        return [], raw_total, display_total

    ids = [row["id"] for row in ranked]
    placeholders = ",".join("?" for _ in ids)
    detail_rows = db.execute(
        f"SELECT i.*, l.name as location_name, "
        f"(SELECT b.name FROM checkouts c JOIN borrowers b ON c.borrower_id = b.id "
        f" WHERE c.item_id = i.id AND c.checked_in IS NULL LIMIT 1) AS lent_to, "
        f"(SELECT 1 FROM checkouts c WHERE c.item_id = i.id "
        f" AND {OVERDUE_CONDITION} LIMIT 1) AS lent_overdue "
        f"FROM items i LEFT JOIN locations l ON i.location_id = l.id "
        f"WHERE i.id IN ({placeholders})",
        [get_overdue_days(db)] + ids,
    ).fetchall()
    by_id = {row["id"]: dict(row) for row in detail_rows}

    group_meta = {row["id"]: row for row in ranked}
    series_keys = [
        row["browse_group_key"][7:]
        for row in ranked
        if row["browse_group_key"].startswith("series:")
    ]
    earliest_covers = _earliest_series_covers(db, series_keys)

    items = []
    for item_id in ids:
        item = by_id[item_id]
        meta = group_meta[item_id]
        group_key = meta["browse_group_key"]
        is_series = group_key.startswith("series:")
        item["browse_series_group"] = is_series
        item["browse_series_count"] = meta["browse_series_count"]
        item["browse_series_url"] = None
        if is_series:
            series_key = group_key[7:]
            item["cover_path"] = earliest_covers.get(series_key)
            item["browse_series_url"] = "/browse?" + urlencode(
                {"media_type_filter": "digital_comic", "series": item["series_name"]}
            )
        items.append(item)

    return items, raw_total, display_total
