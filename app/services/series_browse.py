"""Provider-neutral series grouping for Browse.

Shelf's series identity is the existing ``series_name`` value with SQLite
``NOCASE`` semantics.  This module turns a filtered set of live items into
Browse *units*: one unit for every series and one unit for every item that is
not in a series.  The collapse happens before LIMIT/OFFSET so a long series
uses one Browse slot rather than one slot per volume.

The service deliberately knows nothing about providers, routes, templates,
loans or permissions.  Komga, Hardcover, manual and future sources all
participate through the same ``items_live.series_name`` field.
"""

from __future__ import annotations

from collections.abc import Sequence


# Unit-level equivalents of Browse's normal item sorts.  The caller supplies
# only the public sort key; SQL never comes from the request directly.
_SORT_ORDERS = {
    "newest": "unit_newest DESC, unit_label COLLATE NOCASE, id",
    "oldest": "unit_oldest ASC, unit_label COLLATE NOCASE, id",
    "title_asc": "unit_label COLLATE NOCASE ASC, id",
    "title_desc": "unit_label COLLATE NOCASE DESC, id DESC",
    "author": "unit_author COLLATE NOCASE ASC, unit_label COLLATE NOCASE ASC, id",
    "year_desc": (
        "(unit_year_newest IS NULL), unit_year_newest DESC, "
        "unit_label COLLATE NOCASE ASC, id"
    ),
    "year_asc": (
        "(unit_year_oldest IS NULL), unit_year_oldest ASC, "
        "unit_label COLLATE NOCASE ASC, id"
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
    written against alias ``i``.  Keeping the registry-built clause intact is
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
) -> tuple[list[dict], int]:
    """Return a page of grouped Browse units and the grouped total.

    Filters are applied to items first, then matching items are collapsed by
    Shelf's normal case-insensitive series identity, and only then are sorting
    and pagination applied.  ``member_ids`` contains exactly the matching
    members represented by the unit; this lets Browse bulk selection retain
    the same "act on the visible result set" semantics it has for plain items.

    Representative artwork prefers a member that actually has a cover, then
    the earliest numbered member, then a stable title/id tiebreak.  Sorting is
    unit-aware: for example, "newest" uses the newest matching member in a
    series while title sorting uses the series name.
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
        SELECT id, title, authors, cover_path, media_type, source, created_at,
               unit_key, unit_label, is_series, member_count,
               member_ids_csv, series_name, series_position
        FROM units
        ORDER BY {order}
        LIMIT ? OFFSET ?
        """,
        [*bound, limit, offset],
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
