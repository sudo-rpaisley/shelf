"""Provider-neutral series grouping primitives for Browse design work.

This module deliberately has no route or template caller yet.  It proves the
hard part of collapsing Shelf series *before* pagination while keeping ordinary
(non-series) items as independent browse units.  Series identity follows
Shelf's existing model: ``series_name`` with ``COLLATE NOCASE`` semantics.
"""

from __future__ import annotations


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


_BASE_CTE = """
WITH ranked AS (
    SELECT
        i.id,
        i.title,
        i.authors,
        i.cover_path,
        i.media_type,
        i.series_name,
        i.series_position,
        i.owned,
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
        END AS is_series,
        COUNT(*) OVER (
            PARTITION BY
                CASE
                    WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
                    THEN TRIM(i.series_name) COLLATE NOCASE
                    ELSE 'item:' || CAST(i.id AS TEXT)
                END
        ) AS member_count,
        ROW_NUMBER() OVER (
            PARTITION BY
                CASE
                    WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
                    THEN TRIM(i.series_name) COLLATE NOCASE
                    ELSE 'item:' || CAST(i.id AS TEXT)
                END
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
), units AS (
    SELECT * FROM ranked WHERE representative_rank = 1
)
"""


def fetch_units(db, *, limit: int = 60, offset: int = 0) -> tuple[list[dict], int]:
    """Return one Browse unit per series plus one per non-series item.

    The CTE ranks members and collapses each series before ``LIMIT/OFFSET`` is
    applied.  That means a 100-volume run consumes one page slot, not 100.
    Representative artwork prefers a member that actually has a cover, then
    the earliest numbered member, then a stable title/id tiebreak.

    This is intentionally provider-neutral: Komga, manual, Hardcover and other
    sources all participate through Shelf's normal ``series_name`` field.
    """
    limit, offset = _bounds(limit, offset)

    total = db.execute(
        _BASE_CTE + "SELECT COUNT(*) AS c FROM units"
    ).fetchone()["c"]

    rows = db.execute(
        _BASE_CTE
        + """
        SELECT id, title, authors, cover_path, media_type,
               series_name, series_position, owned,
               unit_key, unit_label, is_series, member_count
        FROM units
        ORDER BY unit_label COLLATE NOCASE, id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()

    return [dict(row) for row in rows], int(total)
