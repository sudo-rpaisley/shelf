"""Related-media groups built from Shelf's existing ``item_links`` graph.

A group is the connected component of item links, not a second collection
record. That means A↔B and B↔C naturally presents A/B/C as one related-media
group without duplicating membership state.

This foundation is intentionally manual-first. Automatic matching by title,
ISBN or provider identity can be layered on later with stronger evidence;
cross-media relationships such as a novel and its film adaptation should
never be guessed from a title alone.
"""

from __future__ import annotations


LINK_TYPES = frozenset({"format", "related", "adaptation"})


def link_items(
    db,
    item_a_id: int,
    item_b_id: int,
    *,
    link_type: str = "related",
) -> bool:
    """Create one undirected item link, returning True only when newly added."""
    if item_a_id == item_b_id:
        return False
    if link_type not in LINK_TYPES:
        raise ValueError("Unknown related-media link type")

    rows = db.execute(
        "SELECT id FROM items WHERE id IN (?, ?)", (item_a_id, item_b_id)
    ).fetchall()
    if {row["id"] for row in rows} != {item_a_id, item_b_id}:
        return False

    a_id, b_id = sorted((item_a_id, item_b_id))
    cursor = db.execute(
        "INSERT OR IGNORE INTO item_links (item_a_id, item_b_id, link_type) "
        "VALUES (?, ?, ?)",
        (a_id, b_id, link_type),
    )
    return bool(cursor.rowcount)


def unlink_items(db, item_a_id: int, item_b_id: int) -> bool:
    """Remove the direct edge between two items, regardless of orientation."""
    if item_a_id == item_b_id:
        return False
    a_id, b_id = sorted((item_a_id, item_b_id))
    cursor = db.execute(
        "DELETE FROM item_links WHERE item_a_id = ? AND item_b_id = ?",
        (a_id, b_id),
    )
    return bool(cursor.rowcount)


def related_ids(
    db,
    item_id: int,
    *,
    include_self: bool = False,
    visibility_sql: str | None = None,
    visibility_params: list | tuple = (),
) -> list[int]:
    """Return the transitive item-link component containing ``item_id``.

    With a visibility predicate, inaccessible nodes are removed from the graph
    itself. A hidden B in A↔B↔C therefore cannot act as an invisible bridge.
    The predicate must reference the ``i`` alias and remain parameter-bound.
    """
    if visibility_sql:
        rows = db.execute(
            f"""WITH RECURSIVE
            visible(id) AS (
                SELECT i.id FROM items i WHERE {visibility_sql}
            ),
            connected(id) AS (
                SELECT ? WHERE EXISTS (SELECT 1 FROM visible WHERE id = ?)
                UNION
                SELECT CASE
                         WHEN il.item_a_id = connected.id THEN il.item_b_id
                         ELSE il.item_a_id
                       END
                  FROM item_links il
                  JOIN connected
                    ON il.item_a_id = connected.id OR il.item_b_id = connected.id
                  JOIN visible v
                    ON v.id = CASE
                                WHEN il.item_a_id = connected.id THEN il.item_b_id
                                ELSE il.item_a_id
                              END
            )
            SELECT id FROM connected ORDER BY id""",
            [*visibility_params, item_id, item_id],
        ).fetchall()
    else:
        if not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            return []
        rows = db.execute(
            """WITH RECURSIVE connected(id) AS (
                   SELECT ?
                   UNION
                   SELECT CASE
                            WHEN il.item_a_id = connected.id THEN il.item_b_id
                            ELSE il.item_a_id
                          END
                   FROM item_links il
                   JOIN connected
                     ON il.item_a_id = connected.id OR il.item_b_id = connected.id
               )
               SELECT id FROM connected ORDER BY id""",
            (item_id,),
        ).fetchall()
    ids = [row["id"] for row in rows]
    if not include_self:
        ids = [value for value in ids if value != item_id]
    return ids


def related_items(
    db,
    item_id: int,
    *,
    include_self: bool = False,
    visibility_sql: str | None = None,
    visibility_params: list | tuple = (),
):
    """Hydrate a related-media component in stable display order."""
    ids = related_ids(
        db,
        item_id,
        include_self=include_self,
        visibility_sql=visibility_sql,
        visibility_params=visibility_params,
    )
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return db.execute(
        f"SELECT * FROM items WHERE id IN ({placeholders}) "
        "ORDER BY title COLLATE NOCASE, media_type, id",
        tuple(ids),
    ).fetchall()


def direct_links(db, item_id: int) -> list[dict]:
    """Return direct neighbours and edge types for edit/detail UIs."""
    rows = db.execute(
        """SELECT il.link_type,
                  CASE WHEN il.item_a_id = ? THEN il.item_b_id ELSE il.item_a_id END AS item_id,
                  i.title, i.media_type, i.cover_path
           FROM item_links il
           JOIN items i ON i.id = CASE
               WHEN il.item_a_id = ? THEN il.item_b_id ELSE il.item_a_id END
           WHERE il.item_a_id = ? OR il.item_b_id = ?
           ORDER BY i.title COLLATE NOCASE, i.media_type, i.id""",
        (item_id, item_id, item_id, item_id),
    ).fetchall()
    return [dict(row) for row in rows]


def search_candidates(
    db,
    item_id: int,
    query: str,
    *,
    limit: int = 20,
    visibility_sql: str | None = None,
    visibility_params: list | tuple = (),
):
    """Find catalogue items outside this related group, optionally ACL-scoped."""
    if not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
        return []
    excluded = sorted(related_ids(db, item_id, include_self=True))
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    placeholders = ",".join("?" for _ in excluded)
    visibility_clause = f" AND ({visibility_sql})" if visibility_sql else ""
    rows = db.execute(
        f"""SELECT i.* FROM items i
            WHERE (i.title LIKE ? COLLATE NOCASE
               OR i.authors LIKE ? COLLATE NOCASE
               OR i.series_name LIKE ? COLLATE NOCASE)
              AND i.id NOT IN ({placeholders})
              {visibility_clause}
            ORDER BY i.title COLLATE NOCASE, i.media_type, i.id
            LIMIT ?""",
        (like, like, like, *excluded, *visibility_params, limit),
    ).fetchall()
    return rows
