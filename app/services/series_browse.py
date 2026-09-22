"""Provider-neutral series grouping and drill-down helpers.

Shelf's series identity is the case-insensitive ``items.series_name`` /
``series_meta.name`` value. Browse therefore groups every live item with a
non-blank series name by that identity, regardless of provider. Grouping is
performed before LIMIT/OFFSET so a long run consumes one Browse slot.
"""

from __future__ import annotations

from urllib.parse import quote

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


def find_gaps(positions: list) -> list[int]:
    """Return missing whole-numbered positions between 1 and the highest.

    Fractional positions such as 2.5 are valid series positions but do not
    create integer gaps of their own.
    """
    ints = set()
    for position in positions:
        if position is None:
            continue
        try:
            value = float(position)
        except (TypeError, ValueError):
            continue
        if value.is_integer() and value >= 1:
            ints.add(int(value))
    if not ints:
        return []
    return [n for n in range(1, max(ints) + 1) if n not in ints]


def _group_expr(alias: str = "i") -> str:
    return (
        "CASE WHEN " + alias + ".series_name IS NOT NULL "
        "AND TRIM(" + alias + ".series_name) != '' "
        "THEN 'series:' || LOWER(TRIM(" + alias + ".series_name)) "
        "ELSE 'item:' || CAST(" + alias + ".id AS TEXT) END"
    )


def _ranked_cte(where: str, order_clause: str) -> str:
    group_expr = _group_expr("i")
    series_test = "i.series_name IS NOT NULL AND TRIM(i.series_name) != ''"
    cover_order = (
        f"CASE WHEN {series_test} AND (i.cover_path IS NULL OR TRIM(i.cover_path) = '') "
        "THEN 1 ELSE 0 END, "
        f"CASE WHEN {series_test} AND i.series_position IS NULL THEN 1 ELSE 0 END, "
        f"CASE WHEN {series_test} THEN CAST(i.series_position AS REAL) ELSE NULL END, "
        "i.title COLLATE NOCASE, i.id"
    )
    return f"""
WITH ranked AS (
    SELECT
        i.*,
        CASE WHEN {series_test} THEN 1 ELSE 0 END AS browse_series_group,
        COUNT(*) OVER (PARTITION BY {group_expr}) AS browse_series_count,
        GROUP_CONCAT(i.id, ',') OVER (
            PARTITION BY {group_expr}
            ORDER BY i.id
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        ) AS browse_series_item_ids,
        FIRST_VALUE(i.cover_path) OVER (
            PARTITION BY {group_expr}
            ORDER BY {cover_order}
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        ) AS browse_series_cover_path,
        ROW_NUMBER() OVER (
            PARTITION BY {group_expr}
            ORDER BY {order_clause}, i.id ASC
        ) AS browse_group_rank
    FROM items_live i
    {where}
)
"""


def fetch_units(
    db,
    where: str,
    params: list,
    order_clause: str,
    *,
    limit: int = 60,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Return one Browse unit per series plus one per non-series item.

    Filters are applied before grouping, so a grouped card represents exactly
    the members visible under the active Browse filters. The returned member
    ids are consequently safe to use for bulk actions on that visible unit.
    """
    limit, offset = _bounds(limit, offset)
    cte = _ranked_cte(where, order_clause)

    display_total = db.execute(
        cte + "SELECT COUNT(*) AS c FROM ranked WHERE browse_group_rank = 1",
        params,
    ).fetchone()["c"]

    rows = db.execute(
        cte
        + f"""
SELECT i.*
FROM ranked i
WHERE i.browse_group_rank = 1
ORDER BY {order_clause}, i.id ASC
LIMIT ? OFFSET ?
""",
        list(params) + [limit, offset],
    ).fetchall()

    units: list[dict] = []
    for row in rows:
        unit = dict(row)
        raw_ids = str(unit.get("browse_series_item_ids") or unit["id"])
        member_ids = []
        for value in raw_ids.split(","):
            try:
                member_ids.append(int(value))
            except (TypeError, ValueError):
                continue
        if not member_ids:
            member_ids = [int(unit["id"])]
        unit["browse_series_item_ids"] = member_ids
        unit["browse_series_group"] = bool(unit.get("browse_series_group"))
        if unit["browse_series_group"]:
            name = str(unit.get("series_name") or "").strip()
            unit["browse_series_name"] = name
            unit["browse_series_url"] = "/series/" + quote(name, safe="")
        else:
            unit["browse_series_name"] = None
            unit["browse_series_url"] = None
            unit["browse_series_count"] = 1
            unit["browse_series_cover_path"] = unit.get("cover_path")
        units.append(unit)

    return units, int(display_total)


def merge_item_details(units: list[dict], rows) -> list[dict]:
    """Overlay ordinary Browse row details onto grouped unit metadata."""
    by_id = {int(row["id"]): dict(row) for row in rows}
    merged: list[dict] = []
    for unit in units:
        item = by_id.get(int(unit["id"]))
        if item is None:
            continue
        for key in (
            "browse_series_group",
            "browse_series_count",
            "browse_series_item_ids",
            "browse_series_cover_path",
            "browse_series_name",
            "browse_series_url",
        ):
            item[key] = unit.get(key)
        if item["browse_series_group"] and item.get("browse_series_cover_path"):
            item["cover_path"] = item["browse_series_cover_path"]
        merged.append(item)
    return merged


def series_detail(db, name: str) -> dict | None:
    """Return Shelf's name-keyed series detail from live catalogue rows."""
    name = (name or "").strip()
    if not name:
        return None

    rows = db.execute(
        "SELECT i.id, i.title, i.authors, i.cover_path, i.series_name, "
        "i.series_position, i.publish_year, i.owned, i.media_type, i.source, "
        "i.reading_status, "
        f"{lists.WISHLISTED_SQL} AS wishlisted "
        "FROM items_live i WHERE i.series_name = ? COLLATE NOCASE "
        "ORDER BY i.series_position IS NULL, i.series_position, "
        "i.publish_year IS NULL, i.publish_year, i.title COLLATE NOCASE, i.id",
        (name,),
    ).fetchall()
    if not rows:
        return None

    items = [dict(row) for row in rows]
    spellings: dict[str, int] = {}
    for item in items:
        spelling = str(item.get("series_name") or "").strip()
        spellings[spelling] = spellings.get(spelling, 0) + 1
    display_name = min(spellings.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    meta_row = db.execute(
        "SELECT name, description, complete, hc_total, hc_missing, hc_checked_at "
        "FROM series_meta WHERE name = ? COLLATE NOCASE",
        (display_name,),
    ).fetchone()
    meta = dict(meta_row) if meta_row else {}

    sources = sorted({str(item.get("source") or "manual") for item in items})
    return {
        "name": display_name,
        "items": items,
        "item_count": len(items),
        "owned_count": sum(1 for item in items if item["owned"]),
        "wishlist_count": sum(1 for item in items if item["wishlisted"]),
        "neither_count": sum(
            1 for item in items if not item["owned"] and not item["wishlisted"]
        ),
        "gaps": find_gaps([item["series_position"] for item in items]),
        "sources": sources,
        "description": meta.get("description"),
        "complete": meta.get("complete"),
        "hc_total": meta.get("hc_total"),
        "hc_missing": meta.get("hc_missing"),
        "hc_checked_at": meta.get("hc_checked_at"),
    }
