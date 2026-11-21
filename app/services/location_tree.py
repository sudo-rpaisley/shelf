"""Explicit NODE-domain queries and LEGACY-flat conversion adapters."""

import sqlite3
from collections import defaultdict


def validated_node_id(db, node_id: int | None) -> int | None:
    from app.services.write_targets import UnknownLocationError

    if node_id is None:
        return None
    if isinstance(node_id, bool) or not isinstance(node_id, int) or node_id <= 0:
        raise UnknownLocationError("Invalid location", value=node_id)
    if node_id >= 2**63 or not db.execute("SELECT 1 FROM location_nodes WHERE id = ?", (node_id,)).fetchone():
        raise UnknownLocationError("Location not found", value=node_id)
    return node_id


def legacy_location_to_node(db, legacy_location_id: int | None, *,
                             create: bool = False) -> int | None:
    """Map a LEGACY flat ID by legacy_location_id only; never by integer/name.

    ``create=True`` explicitly projects a newly added legacy location. It does
    not install schema or create any copy, and does not claim an unrelated node.
    """
    if legacy_location_id is None:
        return None
    from app.services.write_targets import UnknownLocationError

    legacy = db.execute(
        "SELECT id, name, sort_order FROM locations WHERE id = ?", (legacy_location_id,)
    ).fetchone()
    if legacy is None:
        raise UnknownLocationError("Location not found", value=legacy_location_id)
    node = db.execute(
        "SELECT id FROM location_nodes WHERE legacy_location_id = ?", (legacy_location_id,)
    ).fetchone()
    if node is not None:
        return node["id"]
    if not create:
        return None
    try:
        return db.execute(
            "INSERT INTO location_nodes (name, sort_order, legacy_location_id) VALUES (?, ?, ?)",
            (legacy["name"], legacy["sort_order"], legacy_location_id),
        ).lastrowid
    except sqlite3.IntegrityError as exc:
        raise ValueError("Legacy location conflicts with an unrelated location node") from exc


def location_path(db, location_id: int | None) -> list[dict]:
    """NODE ancestry, bounded even for externally corrupted cyclic data."""
    result, seen = [], set()
    while location_id is not None and location_id not in seen:
        seen.add(location_id)
        row = db.execute(
            "SELECT id, parent_id, name, legacy_location_id FROM location_nodes WHERE id = ?",
            (location_id,),
        ).fetchone()
        if row is None:
            break
        result.append(dict(row))
        location_id = row["parent_id"]
    return list(reversed(result))


def node_to_legacy_location(db, node_id: int | None) -> int | None:
    """Nearest explicitly mapped LEGACY ancestor of a NODE, or None."""
    if node_id is None:
        return None
    validated_node_id(db, node_id)
    for row in reversed(location_path(db, node_id)):
        if row["legacy_location_id"] is not None:
            return row["legacy_location_id"]
    return None


nearest_legacy_location = node_to_legacy_location


def location_rows(db) -> list[dict]:
    rows = db.execute(
        "SELECT n.id, n.parent_id, n.name, n.sort_order, n.legacy_location_id, "
        "COUNT(DISTINCT c.id) AS direct_copy_count FROM location_nodes n "
        "LEFT JOIN copies_live c ON c.location_id = n.id "
        "GROUP BY n.id ORDER BY n.sort_order, n.name COLLATE NOCASE, n.id"
    ).fetchall()
    return [dict(row) for row in rows]


def flattened_tree(db) -> list[dict]:
    rows = location_rows(db)
    by_id = {row["id"]: row for row in rows}
    children = defaultdict(list)
    for row in rows:
        children[row["parent_id"]].append(row)

    def total(node_id, seen):
        if node_id in seen:
            return 0
        return by_id[node_id]["direct_copy_count"] + sum(
            total(child["id"], seen | {node_id}) for child in children[node_id]
        )

    result, emitted = [], set()

    def walk(node, ancestors, names):
        if node["id"] in emitted:
            return
        emitted.add(node["id"])
        names = [*names, node["name"]]
        result.append({
            **node, "location_node_id": node["id"], "label": node["name"],
            "depth": len(ancestors), "ancestor_ids": ancestors,
            "path": " › ".join(names), "full_path": " / ".join(names),
            "copy_count": total(node["id"], set()),
        })
        for child in children[node["id"]]:
            walk(child, [*ancestors, node["id"]], names)

    for root in children[None]:
        walk(root, [], [])
    for row in rows:
        if row["id"] not in emitted:
            walk(row, [], [])
    return result


def descendant_ids(db, location_id: int, *, include_self: bool = True) -> list[int]:
    rows = db.execute(
        "WITH RECURSIVE descendants(id) AS ("
        " SELECT id FROM location_nodes WHERE id = ? UNION "
        " SELECT n.id FROM location_nodes n JOIN descendants d ON n.parent_id = d.id"
        ") SELECT id FROM descendants", (location_id,)
    ).fetchall()
    return [r["id"] for r in rows if include_self or r["id"] != location_id]


def direct_copies(db, location_id: int) -> list[dict]:
    from app.services.location_order import direct_copies as query
    return query(db, location_id)


def apply_copy_order(db, location_id: int, copy_ids: list[int]) -> None:
    from app.services.location_order import apply_copy_order as apply
    apply(db, location_id, copy_ids)


def auto_order_copies(db, location_id: int, sort_key: str) -> list[int]:
    from app.services.location_order import auto_order_copies as arrange
    return arrange(db, location_id, sort_key)
