"""Equivalent-format components, not the broader Related Media graph.

Only explicit ``format`` edges whose *two* endpoints are live participate.
Neither adaptations, related works, title similarity nor a path through Trash
can join editions. These helpers never write or contact a provider.
"""

from __future__ import annotations


def series_name_key(name: str | None) -> str:
    """A local series identity; retain punctuation, accents and every script."""
    return (name or "").strip().casefold()


def format_ids(db, item_id: int, include_self: bool = True) -> list[int]:
    """Sorted live format-component IDs; an absent/trashed start returns []."""
    rows = db.execute(
        """WITH RECURSIVE live_edges(a, b) AS (
               SELECT edge.item_a_id, edge.item_b_id FROM item_links edge
               JOIN items_live a ON a.id = edge.item_a_id
               JOIN items_live b ON b.id = edge.item_b_id
               WHERE edge.link_type = 'format'
           ), connected(id) AS (
               SELECT id FROM items_live WHERE id = ?
               UNION
               SELECT CASE WHEN edge.a = connected.id THEN edge.b ELSE edge.a END
               FROM live_edges edge JOIN connected
                 ON edge.a = connected.id OR edge.b = connected.id
           )
           SELECT id FROM connected ORDER BY id""",
        (item_id,),
    ).fetchall()
    return [row["id"] for row in rows if include_self or row["id"] != item_id]


def format_components(db) -> list[tuple[int, ...]]:
    """Every live item exactly once, including singletons, in stable ID order.

    Two batched reads and iterative traversal avoid a query per item, recursion
    limits and any size/depth truncation on cyclic or long format stacks.
    """
    neighbours = {row["id"]: set() for row in db.execute("SELECT id FROM items_live")}
    edges = db.execute(
        """SELECT edge.item_a_id, edge.item_b_id FROM item_links edge
           JOIN items_live a ON a.id = edge.item_a_id
           JOIN items_live b ON b.id = edge.item_b_id
           WHERE edge.link_type = 'format'"""
    )
    for edge in edges:
        a, b = edge["item_a_id"], edge["item_b_id"]
        neighbours[a].add(b)
        neighbours[b].add(a)
    seen = set()
    components = []
    for start in sorted(neighbours):
        if start in seen:
            continue
        pending, members = [start], []
        seen.add(start)
        while pending:
            item_id = pending.pop()
            members.append(item_id)
            for other in neighbours[item_id] - seen:
                seen.add(other)
                pending.append(other)
        components.append(tuple(sorted(members)))
    return components
