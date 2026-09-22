"""Provider-neutral series grouping for Browse.

A Browse *unit* is either one ordinary item or one whole series. Series
identity follows Shelf's existing model: ``series_name`` with NOCASE
semantics. The collapse happens in SQL before LIMIT/OFFSET so a long series
consumes one Browse slot, while filters still apply to the member items before
the collapse.

This service deliberately knows nothing about Komga, RomM or any other
provider. It also does not import router constants: callers pass the current
Browse WHERE/order clauses and overdue predicate in, keeping dependencies
pointing from routers down into services.
"""

from __future__ import annotations

from collections.abc import Sequence

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


def fetch_page(
    db,
    *,
    where: str = "",
    params: Sequence = (),
    order_clause: str = "i.created_at DESC",
    overdue_condition: str,
    overdue_days: int,
    limit: int = 60,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return one Browse unit per series plus one per non-series item.

    ``where`` is the clause produced by :mod:`app.browse_filters`; it is
    applied to live items before grouping. The representative row supplies the
    normal Browse fields and prefers actual artwork, then the earliest numbered
    member. ``series_member_ids`` carries every real item id in the unit so
    Browse bulk actions can expand a grouped card back to the items it
    represents.
    """
    limit, offset = _bounds(limit, offset)

    cte = f"""
    WITH filtered AS (
        SELECT
            i.*,
            l.name AS location_name,
            (SELECT b.name
               FROM checkouts c
               JOIN borrowers b ON c.borrower_id = b.id
              WHERE c.item_id = i.id AND c.checked_in IS NULL
              LIMIT 1) AS lent_to,
            (SELECT 1
               FROM checkouts c
              WHERE c.item_id = i.id AND {overdue_condition}
              LIMIT 1) AS lent_overdue,
            {lists.WISHLISTED_SQL} AS wishlisted,
            CASE
                WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
                THEN 'series:' || LOWER(TRIM(i.series_name))
                ELSE 'item:' || CAST(i.id AS TEXT)
            END AS browse_unit_key,
            CASE
                WHEN i.series_name IS NOT NULL AND TRIM(i.series_name) != ''
                THEN 1 ELSE 0
            END AS is_series
        FROM items_live i
        LEFT JOIN locations l ON i.location_id = l.id
        {where}
    ), ranked AS (
        SELECT
            f.*,
            COUNT(*) OVER (PARTITION BY browse_unit_key) AS member_count,
            GROUP_CONCAT(id) OVER (PARTITION BY browse_unit_key) AS series_member_ids,
            ROW_NUMBER() OVER (
                PARTITION BY browse_unit_key
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
                        WHEN is_series = 1 THEN CAST(series_position AS REAL)
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

    bound = [overdue_days, *params]
    total = db.execute(
        cte + "SELECT COUNT(*) AS c FROM units",
        bound,
    ).fetchone()["c"]

    rows = db.execute(
        cte
        + f"""
        SELECT i.*
          FROM units i
         ORDER BY {order_clause}, i.id ASC
         LIMIT ? OFFSET ?
        """,
        [*bound, limit, offset],
    ).fetchall()

    return [dict(row) for row in rows], int(total)
