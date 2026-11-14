"""Read-only exact-copy location context for the current shared catalogue.

This is not household isolation, an observation or evidence of physical presence.
The caller holds one read transaction across the target, hierarchy and window.
"""

from app.services import location_order

MAX_LOCATION_ID = (1 << 63) - 1
MAX_ANCESTRY_NODES = 32  # includes the current location; at most 32 point reads


def _live_id(value) -> bool:
    return type(value) is int and 0 < value <= MAX_LOCATION_ID


def _ancestry(db, location_id: int | None) -> tuple[list[dict], str | None]:
    """Rootward point reads; labels are never parsed into invented nodes."""
    nodes = []
    seen = set()
    current = location_id
    issue = None
    while current is not None:
        if current in seen:
            issue = "cycle"
            break
        if len(nodes) >= MAX_ANCESTRY_NODES:
            issue = "limit"
            break
        seen.add(current)
        row = db.execute(
            "SELECT id, name, label, parent_id FROM locations WHERE id = ?",
            (current,),
        ).fetchone()
        if row is None:
            issue = "unavailable"
            break
        nodes.append(dict(row))
        current = row["parent_id"]
    return nodes, issue


def locate_copy(db, item_id: int, copy_id: int, *, at: int | None = None) -> dict | None:
    """Return this exact live pair or None; never substitute the primary/seam.

    ``at`` is only a bounded old-location ID hint, with 0 meaning previously
    unlocated. It does not select records or authorize access. In particular we
    never read another location's metadata just because the caller named it.
    """
    if not _live_id(item_id) or not _live_id(copy_id):
        return None
    if at is not None and (type(at) is not int or not 0 <= at <= MAX_LOCATION_ID):
        raise ValueError("Invalid location hint")
    row = db.execute(
        "SELECT c.id AS copy_id, c.item_id, c.copy_number, c.copy_barcode, "
        "c.location_id, c.position_order, c.missing_since, c.updated_at AS changed_at, "
        "i.title, i.media_type FROM copies_live c JOIN items_live i ON i.id = c.item_id "
        "WHERE c.id = ? AND i.id = ?",
        (copy_id, item_id),
    ).fetchone()
    if row is None:
        return None
    target = dict(row)
    nodes, issue = _ancestry(db, target["location_id"])
    neighbours = location_order.copy_neighbours(db, target) if nodes else {"before": [], "after": []}
    return {
        "target": target,
        "location": nodes[0] if nodes else None,
        "ancestors": list(reversed(nodes[1:])),
        "ancestry_incomplete": issue is not None,
        "ancestry_issue": issue,
        "hint_supplied": at is not None,
        "previously_unlocated": at == 0,
        "location_changed": at is not None and at != (target["location_id"] or 0),
        "window_limited": bool(nodes),
        "before_limit_reached": len(neighbours["before"]) == location_order.NEIGHBOURS_EACH_SIDE,
        "after_limit_reached": len(neighbours["after"]) == location_order.NEIGHBOURS_EACH_SIDE,
        **neighbours,
    }
