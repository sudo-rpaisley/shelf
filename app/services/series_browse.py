"""Provider-neutral series grouping for Browse.

Series identity follows Shelf's existing model: a case-insensitive
``items.series_name``.  When Browse asks to group series, this service applies
all ordinary Browse filters first, collapses those filtered rows into units,
and only then paginates.  A long series therefore consumes one page slot while
items without a series remain independent units.
"""

from __future__ import annotations

from app.services import lists


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


def _unit_expression() -> str:
    return (
        "CASE WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != '' "
        "THEN 'series:' || LOWER(TRIM(i.series_name)) "
        "ELSE 'item:' || CAST(i.id AS TEXT) END"
    )


def _cte(where: str) -> str:
    unit = _unit_expression()
    return f"""
WITH ranked AS (
    SELECT
        i.*,
        {unit} AS unit_key,
        CASE
            WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
            THEN TRIM(i.series_name)
            ELSE i.title
        END AS unit_label,
        CASE
            WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
            THEN 1 ELSE 0
        END AS is_series_unit,
        COUNT(*) OVER (PARTITION BY {unit}) AS member_count,
        GROUP_CONCAT(i.id) OVER (PARTITION BY {unit}) AS unit_item_ids,
        MAX(i.created_at) OVER (PARTITION BY {unit}) AS unit_newest,
        MIN(i.created_at) OVER (PARTITION BY {unit}) AS unit_oldest,
        MAX(i.publish_year) OVER (PARTITION BY {unit}) AS unit_year_newest,
        MIN(i.publish_year) OVER (PARTITION BY {unit}) AS unit_year_oldest,
        ROW_NUMBER() OVER (
            PARTITION BY {unit}
            ORDER BY
                CASE
                    WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
                         AND (i.cover_path IS NULL OR TRIM(i.cover_path) = '')
                    THEN 1 ELSE 0
                END,
                CASE
                    WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
                         AND i.series_position IS NULL
                    THEN 1 ELSE 0
                END,
                CASE
                    WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
                    THEN CAST(i.series_position AS REAL)
                    ELSE NULL
                END,
                i.title COLLATE NOCASE,
                i.id
        ) AS representative_rank
    FROM items_live i
    {where}
), units AS (
    SELECT * FROM ranked WHERE representative_rank = 1
)
"""


def _order(sort: str) -> str:
    """Sort grouped units with semantics matching Browse where possible."""
    return {
        "newest": "i.unit_newest DESC, i.unit_label COLLATE NOCASE ASC, i.id ASC",
        "oldest": "i.unit_oldest ASC, i.unit_label COLLATE NOCASE ASC, i.id ASC",
        "title_asc": "i.unit_label COLLATE NOCASE ASC, i.id ASC",
        "title_desc": "i.unit_label COLLATE NOCASE DESC, i.id ASC",
        "author": "i.authors COLLATE NOCASE ASC, i.unit_label COLLATE NOCASE ASC, i.id ASC",
        "year_desc": "(i.unit_year_newest IS NULL), i.unit_year_newest DESC, i.unit_label COLLATE NOCASE ASC, i.id ASC",
        "year_asc": "(i.unit_year_oldest IS NULL), i.unit_year_oldest ASC, i.unit_label COLLATE NOCASE ASC, i.id ASC",
    }.get(sort, "i.unit_newest DESC, i.unit_label COLLATE NOCASE ASC, i.id ASC")


def fetch_page(
    db,
    *,
    where: str = "",
    params: list | tuple = (),
    sort: str = "newest",
    overdue_condition: str,
    overdue_days: int,
    limit: int = 60,
    offset: int = 0,
):
    """Return one item-like row per series/non-series Browse unit and its total.

    ``where`` and ``params`` come directly from ``browse_filters.build_where``.
    They are applied inside the CTE before the window functions, so filters and
    grouping agree.  The representative prefers the earliest numbered member
    that actually has artwork; the member id list lets Browse bulk actions act
    on every collapsed item rather than silently dropping series members.
    """
    limit, offset = _bounds(limit, offset)
    cte = _cte(where)

    total = db.execute(
        cte + "SELECT COUNT(*) AS c FROM units",
        list(params),
    ).fetchone()["c"]

    rows = db.execute(
        cte
        + f"""
        SELECT i.*, l.name AS location_name,
               (SELECT b.name FROM checkouts c JOIN borrowers b ON c.borrower_id = b.id
                WHERE c.item_id = i.id AND c.checked_in IS NULL LIMIT 1) AS lent_to,
               (SELECT 1 FROM checkouts c WHERE c.item_id = i.id
                AND {overdue_condition} LIMIT 1) AS lent_overdue,
               {lists.WISHLISTED_SQL} AS wishlisted
        FROM units i
        LEFT JOIN locations l ON i.location_id = l.id
        ORDER BY {_order(sort)}
        LIMIT ? OFFSET ?
        """,
        [overdue_days] + list(params) + [limit, offset],
    ).fetchall()

    return rows, int(total)
