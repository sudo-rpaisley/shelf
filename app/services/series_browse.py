"""Provider-neutral series grouping for Browse.

Shelf's series identity is the existing ``series_name`` value with SQLite
``NOCASE`` semantics.  This module turns a filtered set of live items into
Browse *units*: one unit for every series and one unit for every item that is
not in a series.  The collapse happens before LIMIT/OFFSET so a long series
uses one Browse slot rather than one slot per volume.

The service deliberately knows nothing about providers, routes, templates or
permissions. Komga, Hardcover, manual and future sources all participate
through the same ``items_live.series_name`` field.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.services import lists


# Kept at the service layer rather than importing app.routers.checkouts: a
# service must never depend on a router. This is the same one-parameter overdue
# predicate Browse already used before grouping; the route passes the resolved
# fallback-day setting into fetch_units().
_OVERDUE_CONDITION = (
    "c.checked_in IS NULL AND ("
    "  (c.due_date IS NOT NULL AND c.due_date < date('now'))"
    "  OR (c.due_date IS NULL AND julianday('now') - julianday(c.checked_out) > ?)"
    ")"
)

# Unit-level equivalents of Browse's normal item sorts. The caller supplies
# only the public sort key; SQL never comes from the request directly.
_SORT_ORDERS = {
    "newest": "unit_newest DESC, unit_label COLLATE NOCASE, i.id",
    "oldest": "unit_oldest ASC, unit_label COLLATE NOCASE, i.id",
    "title_asc": "unit_label COLLATE NOCASE ASC, i.id",
    "title_desc": "unit_label COLLATE NOCASE DESC, i.id DESC",
    "author": "unit_author COLLATE NOCASE ASC, unit_label COLLATE NOCASE ASC, i.id",
    "year_desc": (
        "(unit_year_newest IS NULL), unit_year_newest DESC, "
        "unit_label COLLATE NOCASE ASC, i.id"
    ),
    "year_asc": (
        "(unit_year_oldest IS NULL), unit_year_oldest ASC, "
        "unit_label COLLATE NOCASE ASC, i.id"
    ),
}


def _bounds(limit: int, offset: int) -> tuple[int, int]:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 60
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, 200)), max(0, offset)


def _cte(where: str) -> str:
    """Return the grouping CTE for an already-built Browse WHERE clause.

    ``where`` comes from :mod:`app.browse_filters`, whose conditions are all
    written against alias ``i``. Keeping the registry-built clause intact is
    what makes grouping preserve every normal Browse filter.
    """
    return f"""
WITH filtered AS (
    SELECT
        i.*,
        CASE
            WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
            THEN TRIM(i.series_name)
            ELSE '__item__:' || CAST(i.id AS TEXT)
        END AS unit_group,
        CASE
            WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
            THEN 'series:' || LOWER(TRIM(i.series_name))
            ELSE 'item:' || CAST(i.id AS TEXT)
        END AS unit_key,
        CASE
            WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
            THEN TRIM(i.series_name)
            ELSE i.title
        END AS unit_label,
        CASE
            WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
            THEN 1 ELSE 0
        END AS is_series
    FROM items_live i
    {where}
), ranked AS (
    SELECT
        f.*,
        COUNT(*) OVER (
            PARTITION BY unit_group COLLATE NOCASE
        ) AS member_count,
        GROUP_CONCAT(id, ',') OVER (
            PARTITION BY unit_group COLLATE NOCASE
        ) AS member_ids_csv,
        MAX(created_at) OVER (
            PARTITION BY unit_group COLLATE NOCASE
        ) AS unit_newest,
        MIN(created_at) OVER (
            PARTITION BY unit_group COLLATE NOCASE
        ) AS unit_oldest,
        MIN(authors) OVER (
            PARTITION BY unit_group COLLATE NOCASE
        ) AS unit_author,
        MAX(publish_year) OVER (
            PARTITION BY unit_group COLLATE NOCASE
        ) AS unit_year_newest,
        MIN(publish_year) OVER (
            PARTITION BY unit_group COLLATE NOCASE
        ) AS unit_year_oldest,
        ROW_NUMBER() OVER (
            PARTITION BY unit_group COLLATE NOCASE
            ORDER BY
                CASE
                    WHEN is_series = 1
                         AND (cover_path IS NULL OR TRIM(cover_path) = '')
                    THEN 1 ELSE 0
                END,
                CASE
                    WHEN is_series = 1 AND series_position IS NULL
                    THEN 1 ELSE 0
                END,
                CASE
                    WHEN is_series = 1
                    THEN CAST(series_position AS REAL)
                    ELSE NULL
                END,
                title COLLATE NOCASE,
                id
        ) AS representative_rank
    FROM filtered f
), units AS (
    SELECT * FROM ranked WHERE representative_rank = 1
)
"""


def fetch_units(
    db,
    *,
    where: str = "",
    params: Sequence = (),
    sort: str = "title_asc",
    limit: int = 60,
    offset: int = 0,
    overdue_days: int = 28,
) -> tuple[list[dict], int]:
    """Return a page of grouped Browse units and the grouped total.

    Filters are applied to items first, then matching items are collapsed by
    Shelf's normal case-insensitive series identity, and only then are sorting
    and pagination applied. ``member_ids`` contains exactly the matching
    members represented by the unit; this lets Browse bulk selection retain
    the same "act on the visible result set" semantics it has for plain items.

    Every unit carries the normal representative-item fields plus Browse's
    location, loan and wishlist overlays, so existing item templates keep the
    same data contract for non-series units. Series templates can use the
    additional ``is_series``, ``unit_label``, ``member_count`` and
    ``member_ids`` fields.
    """
    limit, offset = _bounds(limit, offset)
    order = _SORT_ORDERS.get(sort, _SORT_ORDERS["newest"])
    cte = _cte(where)
    bound = list(params)

    total = db.execute(
        cte + "SELECT COUNT(*) AS c FROM units",
        bound,
    ).fetchone()["c"]

    rows = db.execute(
        cte
        + f"""
        SELECT i.*, l.name AS location_name,
               (SELECT b.name FROM checkouts c
                JOIN borrowers b ON c.borrower_id = b.id
                WHERE c.item_id = i.id AND c.checked_in IS NULL LIMIT 1) AS lent_to,
               (SELECT 1 FROM checkouts c
                WHERE c.item_id = i.id AND {_OVERDUE_CONDITION} LIMIT 1) AS lent_overdue,
               {lists.WISHLISTED_SQL} AS wishlisted
        FROM units i
        LEFT JOIN locations l ON i.location_id = l.id
        ORDER BY {order}
        LIMIT ? OFFSET ?
        """,
        [*bound, overdue_days, limit, offset],
    ).fetchall()

    units: list[dict] = []
    for row in rows:
        unit = dict(row)
        ids = {
            int(value)
            for value in (unit.pop("member_ids_csv", "") or "").split(",")
            if value
        }
        unit["member_ids"] = sorted(ids)
        unit["representative_id"] = unit["id"]
        units.append(unit)

    return units, int(total)
