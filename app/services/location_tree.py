"""Queries and ordering helpers for hierarchical physical locations."""

from collections import defaultdict


def location_rows(db) -> list[dict]:
    rows = db.execute(
        "SELECT n.id, n.parent_id, n.name, n.sort_order, n.legacy_location_id, "
        "COUNT(DISTINCT c.id) AS direct_copy_count "
        "FROM location_nodes n "
        "LEFT JOIN item_copies c ON c.location_id = n.id "
        "GROUP BY n.id, n.parent_id, n.name, n.sort_order, n.legacy_location_id "
        "ORDER BY n.sort_order, n.name COLLATE NOCASE, n.id"
    ).fetchall()
    return [dict(row) for row in rows]


def flattened_tree(db) -> list[dict]:
    """Return nodes in display order with depth/path and aggregate copy counts."""
    rows = location_rows(db)
    by_id = {row["id"]: row for row in rows}
    children: dict[int | None, list[dict]] = defaultdict(list)
    for row in rows:
        children[row["parent_id"]].append(row)

    aggregate: dict[int, int] = {}

    def total(node_id: int, seen: set[int]) -> int:
        if node_id in aggregate:
            return aggregate[node_id]
        if node_id in seen:  # corrupted legacy/manual SQL should not recurse forever
            return 0
        next_seen = seen | {node_id}
        value = by_id[node_id]["direct_copy_count"] + sum(
            total(child["id"], next_seen) for child in children.get(node_id, [])
        )
        aggregate[node_id] = value
        return value

    result: list[dict] = []

    def walk(node: dict, depth: int, path: list[str], seen: set[int]) -> None:
        if node["id"] in seen:
            return
        current_path = [*path, node["name"]]
        result.append({
            **node,
            "depth": depth,
            "path": " › ".join(current_path),
            "copy_count": total(node["id"], set()),
        })
        next_seen = seen | {node["id"]}
        for child in children.get(node["id"], []):
            walk(child, depth + 1, current_path, next_seen)

    for root in children.get(None, []):
        walk(root, 0, [], set())

    # If a database has an orphan/cycle caused by external SQL, still expose it
    # to an admin instead of making the record disappear from the manager.
    emitted = {row["id"] for row in result}
    for row in rows:
        if row["id"] not in emitted:
            walk(row, 0, [], set())
    return result


def location_path(db, location_id: int) -> list[dict]:
    rows = db.execute(
        "WITH RECURSIVE ancestors(id, parent_id, name, depth) AS ("
        " SELECT id, parent_id, name, 0 FROM location_nodes WHERE id = ?"
        " UNION ALL "
        " SELECT n.id, n.parent_id, n.name, a.depth + 1 "
        " FROM location_nodes n JOIN ancestors a ON a.parent_id = n.id"
        ") SELECT id, parent_id, name, depth FROM ancestors ORDER BY depth DESC",
        (location_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def descendant_ids(db, location_id: int, *, include_self: bool = True) -> list[int]:
    rows = db.execute(
        "WITH RECURSIVE descendants(id) AS ("
        " SELECT id FROM location_nodes WHERE id = ?"
        " UNION ALL "
        " SELECT n.id FROM location_nodes n JOIN descendants d ON n.parent_id = d.id"
        ") SELECT id FROM descendants",
        (location_id,),
    ).fetchall()
    values = [row["id"] for row in rows]
    return values if include_self else [value for value in values if value != location_id]


def direct_copies(db, location_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT c.id AS copy_id, c.copy_number, c.position_order, c.condition, "
        "c.copy_barcode, i.id AS item_id, i.title, i.authors, i.media_type, "
        "i.cover_path, i.series_name, i.series_position, i.publish_year, "
        "mr.release_date, pi.issue_date, pi.issue_number "
        "FROM item_copies c JOIN items i ON i.id = c.item_id "
        "LEFT JOIN music_releases mr ON mr.item_id = i.id "
        "LEFT JOIN periodical_issues pi ON pi.item_id = i.id "
        "WHERE c.location_id = ? "
        "ORDER BY CASE WHEN c.position_order IS NULL THEN 1 ELSE 0 END, "
        "c.position_order, i.title COLLATE NOCASE, c.copy_number, c.id",
        (location_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def apply_copy_order(db, location_id: int, copy_ids: list[int]) -> None:
    existing = {
        row["id"] for row in db.execute(
            "SELECT id FROM item_copies WHERE location_id = ?", (location_id,)
        ).fetchall()
    }
    supplied = set(copy_ids)
    if len(copy_ids) != len(supplied) or supplied != existing:
        raise ValueError("Copy order must contain every copy in this location exactly once")
    for position, copy_id in enumerate(copy_ids, start=1):
        db.execute(
            "UPDATE item_copies SET position_order = ?, updated_at = datetime('now') "
            "WHERE id = ? AND location_id = ?",
            (position, copy_id, location_id),
        )


_SORT_KEYS = {
    "title": lambda row: ((row.get("title") or "").casefold(), row["copy_id"]),
    "author": lambda row: (
        (row.get("authors") or "").casefold(),
        (row.get("title") or "").casefold(),
        row["copy_id"],
    ),
    "series": lambda row: (
        (row.get("series_name") or row.get("title") or "").casefold(),
        row.get("series_position") if row.get("series_position") is not None else float("inf"),
        (row.get("title") or "").casefold(),
        row["copy_id"],
    ),
    "release": lambda row: (
        row.get("release_date") or (str(row["publish_year"]) if row.get("publish_year") else "9999"),
        (row.get("title") or "").casefold(),
        row["copy_id"],
    ),
    "issue": lambda row: (
        row.get("issue_date") or "9999",
        row.get("issue_number") or "",
        (row.get("title") or "").casefold(),
        row["copy_id"],
    ),
}


def auto_order_copies(db, location_id: int, sort_key: str) -> list[int]:
    if sort_key not in _SORT_KEYS:
        raise ValueError("Unknown sort order")
    copies = direct_copies(db, location_id)
    ordered = sorted(copies, key=_SORT_KEYS[sort_key])
    ids = [row["copy_id"] for row in ordered]
    apply_copy_order(db, location_id, ids)
    return ids
