"""Browse pagination helpers for collapsed catalogue groups.

Browse has two independent compact views:

* Digital Comics can collapse by series (Komga ``seriesId`` when available).
* Items connected through ``item_links`` collapse into one linked-media stack.

Grouping is performed before LIMIT/OFFSET so a group cannot consume a page or
reappear later. ``item_links`` remains the only source of truth for linked
media: Browse derives connected-component roots with a recursive CTE rather
than storing a second group id on ``items``.
"""

from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlencode

from app.config import MEDIA_TYPES
from app.database import get_setting

SETTING_KEY = "browse_group_digital_comics"
MEDIA_SETTING_KEY = "browse_group_related_media"
_FALSE_VALUES = {"0", "false", "off", "no"}
_GROUPABLE = (
    "i.media_type = 'digital_comic' "
    "AND i.series_name IS NOT NULL AND TRIM(i.series_name) != ''"
)
_SERIES_IDENTITY = (
    "CASE "
    "WHEN LOWER(COALESCE(i.source, '')) = 'komga' "
    "AND i.komga_series_id IS NOT NULL AND TRIM(i.komga_series_id) != '' "
    "THEN 'komga:' || TRIM(i.komga_series_id) "
    "ELSE 'series:' || LOWER(TRIM(i.series_name)) END"
)
_GROUP_KEY = (
    "CASE WHEN " + _GROUPABLE + " THEN " + _SERIES_IDENTITY + " "
    "ELSE 'item:' || CAST(i.id AS TEXT) END"
)

# Only nodes that actually occur in item_links seed the recursive walk. That
# keeps an installation with thousands of unlinked catalogue rows cheap while
# still deriving the full transitive component for every linked item.
_MEDIA_ROOTS_CTE = """WITH RECURSIVE
linked_nodes(id) AS (
    SELECT item_a_id FROM item_links
    UNION
    SELECT item_b_id FROM item_links
),
media_walk(seed, node) AS (
    SELECT id, id FROM linked_nodes
    UNION
    SELECT media_walk.seed,
           CASE
             WHEN il.item_a_id = media_walk.node THEN il.item_b_id
             ELSE il.item_a_id
           END
      FROM media_walk
      JOIN item_links il
        ON il.item_a_id = media_walk.node OR il.item_b_id = media_walk.node
),
media_roots(id, root) AS (
    SELECT seed, MIN(node)
      FROM media_walk
     GROUP BY seed
)"""


def grouping_enabled(db, values: dict) -> bool:
    """Whether this request should collapse Digital Comic series."""
    if values.get("series"):
        return False
    raw = get_setting(db, SETTING_KEY)
    return str(raw if raw is not None else "1").strip().lower() not in _FALSE_VALUES


def media_grouping_enabled(db) -> bool:
    """Whether linked-media connected components collapse in Browse."""
    raw = get_setting(db, MEDIA_SETTING_KEY)
    return str(raw if raw is not None else "1").strip().lower() not in _FALSE_VALUES


def _display_group_key(group_comics: bool, group_media: bool) -> str:
    """SQL expression that identifies one visible Browse entry."""
    cases: list[str] = ["CASE"]
    if group_comics:
        cases.append(f"WHEN {_GROUPABLE} THEN {_SERIES_IDENTITY}")
    if group_media:
        cases.append(
            "WHEN mr.root IS NOT NULL "
            "THEN 'media:' || CAST(mr.root AS TEXT)"
        )
    cases.append("ELSE 'item:' || CAST(i.id AS TEXT) END")
    return " ".join(cases)


def _media_join(group_media: bool) -> str:
    return "LEFT JOIN media_roots mr ON mr.id = i.id" if group_media else ""


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
        item["browse_media_group"] = False
        item["browse_media_count"] = 1
        item["browse_media_url"] = None
        items.append(item)
    return items, raw_total, raw_total


def _earliest_group_covers(db, group_keys: list[str]) -> dict[str, str | None]:
    """Return the cover belonging to the earliest item in each comic series."""
    if not group_keys:
        return {}
    placeholders = ",".join("?" for _ in group_keys)
    rows = db.execute(
        f"""WITH ranked AS (
            SELECT {_GROUP_KEY} AS browse_group_key, cover_path,
                   ROW_NUMBER() OVER (
                       PARTITION BY {_GROUP_KEY}
                       ORDER BY (series_position IS NULL), series_position ASC,
                                (publish_year IS NULL), publish_year ASC, id ASC
                   ) AS issue_rank
              FROM items i
             WHERE media_type = 'digital_comic'
               AND series_name IS NOT NULL AND TRIM(series_name) != ''
               AND {_GROUP_KEY} IN ({placeholders})
        )
        SELECT browse_group_key, cover_path FROM ranked WHERE issue_rank = 1""",
        group_keys,
    ).fetchall()
    return {row["browse_group_key"]: row["cover_path"] for row in rows}


def _media_group_metadata(db, roots: list[int]) -> dict[int, dict]:
    """Return stable display metadata for visible linked-media components.

    The minimum item id is the component root and therefore the canonical
    navigation target/title. A missing root cover falls back to the first cover
    found in the component. Counts and format labels intentionally describe the
    full linked component, even when the current Browse filter matched only one
    member.
    """
    if not roots:
        return {}
    placeholders = ",".join("?" for _ in roots)
    rows = db.execute(
        f"""{_MEDIA_ROOTS_CTE}
        SELECT mr.root, i.id, i.title, i.authors, i.cover_path, i.media_type
          FROM items i
          JOIN media_roots mr ON mr.id = i.id
         WHERE mr.root IN ({placeholders})
         ORDER BY mr.root, i.id""",
        roots,
    ).fetchall()

    by_root: dict[int, list] = defaultdict(list)
    for row in rows:
        by_root[row["root"]].append(row)

    result: dict[int, dict] = {}
    for root, members in by_root.items():
        canonical = next((row for row in members if row["id"] == root), members[0])
        cover_path = canonical["cover_path"] or next(
            (row["cover_path"] for row in members if row["cover_path"]), None
        )
        media_types = []
        media_labels = []
        for row in members:
            media_type = row["media_type"]
            if media_type in media_types:
                continue
            media_types.append(media_type)
            media_labels.append(MEDIA_TYPES.get(media_type, media_type))
        result[root] = {
            "browse_media_root": root,
            "browse_media_count": len(members),
            "browse_media_title": canonical["title"],
            "browse_media_authors": canonical["authors"],
            "browse_media_cover_path": cover_path,
            "browse_media_types": media_types,
            "browse_media_labels": media_labels,
            "browse_media_url": f"/item/{root}",
        }
    return result


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
    """Fetch one Browse page after collapsing enabled display groups.

    Returns ``(items, raw_total, display_total)``. ``raw_total`` remains the
    catalogue-item count used by filters. ``display_total`` is the number of
    visible cards/rows after grouping and therefore drives pagination.
    """
    group_comics = grouping_enabled(db, values)
    group_media = media_grouping_enabled(db)
    if not group_comics and not group_media:
        return _plain_page(
            db, where, params, order_clause, limit=limit, offset=offset
        )

    from app.routers.checkouts import OVERDUE_CONDITION, get_overdue_days

    raw_total = db.execute(
        f"SELECT COUNT(*) AS c FROM items i {where}", params
    ).fetchone()["c"]

    display_key = _display_group_key(group_comics, group_media)
    media_join = _media_join(group_media)
    if group_media:
        display_total = db.execute(
            f"""{_MEDIA_ROOTS_CTE}
            SELECT COUNT(DISTINCT {display_key}) AS c
              FROM items i
              {media_join}
              {where}""",
            params,
        ).fetchone()["c"]
        grouped_prefix = _MEDIA_ROOTS_CTE + ",\ngrouped AS ("
    else:
        display_total = db.execute(
            f"SELECT COUNT(DISTINCT {display_key}) AS c FROM items i {where}",
            params,
        ).fetchone()["c"]
        grouped_prefix = "WITH grouped AS ("

    # The member that determines a group's position in the current sort can be
    # different from the member that supplies its stable displayed identity.
    ranked = db.execute(
        f"""{grouped_prefix}
            SELECT i.id,
                   {display_key} AS browse_group_key,
                   COUNT(*) OVER (PARTITION BY {display_key}) AS browse_group_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY {display_key}
                       ORDER BY {order_clause}
                   ) AS browse_group_rank
              FROM items i
              {media_join}
              {where}
        )
        SELECT g.id, g.browse_group_key, g.browse_group_count
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
        row["browse_group_key"]
        for row in ranked
        if row["browse_group_key"].startswith(("series:", "komga:"))
    ]
    earliest_covers = _earliest_group_covers(db, series_keys)

    media_roots = [
        int(row["browse_group_key"][6:])
        for row in ranked
        if row["browse_group_key"].startswith("media:")
    ]
    media_metadata = _media_group_metadata(db, media_roots)

    items = []
    for item_id in ids:
        item = by_id[item_id]
        meta = group_meta[item_id]
        group_key = meta["browse_group_key"]
        is_series = group_key.startswith(("series:", "komga:"))
        is_media = group_key.startswith("media:")

        item["browse_series_group"] = is_series
        item["browse_series_count"] = meta["browse_group_count"] if is_series else 1
        item["browse_series_url"] = None
        item["browse_media_group"] = is_media
        item["browse_media_count"] = 1
        item["browse_media_url"] = None

        if is_series:
            item["cover_path"] = earliest_covers.get(group_key)
            detail_params = {
                "name": item["series_name"],
                "media_type": "digital_comic",
            }
            if group_key.startswith("komga:"):
                detail_params["komga_series_id"] = group_key[6:]
            item["browse_series_url"] = "/series/detail?" + urlencode(detail_params)
        elif is_media:
            root = int(group_key[6:])
            item.update(media_metadata[root])

        items.append(item)

    return items, raw_total, display_total
