"""Related-media groups built from the existing ``item_links`` graph.

Shelf historically treated an item link as a one-hop relation: a physical book
could see its audiobook, but the audiobook could not necessarily see another
edition that was linked through the physical copy. A media group is the
*connected component* of that graph. No second source of truth is required.

Automatic grouping stays inside four format families (books, comics, manga and
games). Cross-family relationships such as a novel and its game adaptation are
user-managed links.
"""

from __future__ import annotations

import re
from collections import defaultdict


FAMILIES: dict[str, frozenset[str]] = {
    "book": frozenset({"book", "kids_book", "ebook", "audiobook"}),
    "comic": frozenset({"comic", "digital_comic"}),
    "manga": frozenset({"manga", "digital_manga"}),
    "game": frozenset({"video_game", "digital_game"}),
}

_PROVIDER_SYNC_SOURCES = frozenset({"audiobookshelf", "komga", "romm"})


def family_for(media_type: str | None) -> str | None:
    for family, media_types in FAMILIES.items():
        if media_type in media_types:
            return family
    return None


def normalise_title(title: str | None) -> str:
    """Normalise harmless title differences without discarding subtitles.

    Removing everything after a colon is useful for loose search but unsafe for
    grouping games (``Zelda: Ocarina of Time`` and ``Zelda: Majora's Mask``).
    Grouping therefore keeps subtitle words and only normalises punctuation,
    articles and whitespace.
    """
    value = (title or "").casefold().strip().replace("&", " and ")
    value = re.sub(r"^(the|a|an)\s+", "", value)
    value = re.sub(r"[’'`]+", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _authors_compatible(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return True
    first_a = a.split(",")[0].strip().casefold()
    first_b = b.split(",")[0].strip().casefold()
    return bool(first_a and first_b) and (
        first_a in b.casefold() or first_b in a.casefold()
    )


def _match(a, b, family: str) -> bool:
    if family in ("book", "comic", "manga"):
        if a["isbn"] and b["isbn"] and a["isbn"] == b["isbn"]:
            return True
        return (
            normalise_title(a["title"]) == normalise_title(b["title"])
            and _authors_compatible(a["authors"], b["authors"])
        )
    if family == "game":
        # Related media intentionally spans ports, remasters and later platform
        # releases. Exact normalised title is the safe automatic boundary;
        # subtitles are retained by normalise_title so distinct entries such as
        # Zelda: Ocarina of Time / Majora's Mask do not collapse together.
        return normalise_title(a["title"]) == normalise_title(b["title"])
    return False


def link_items(db, item_a_id: int, item_b_id: int, link_type: str = "related") -> bool:
    """Link two items. Returns True only when a new edge was created."""
    if item_a_id == item_b_id:
        return False
    existing_ids = {
        row["id"]
        for row in db.execute(
            "SELECT id FROM items WHERE id IN (?, ?)", (item_a_id, item_b_id)
        ).fetchall()
    }
    if existing_ids != {item_a_id, item_b_id}:
        return False
    a_id, b_id = sorted((item_a_id, item_b_id))
    cursor = db.execute(
        """INSERT OR IGNORE INTO item_links (item_a_id, item_b_id, link_type)
           VALUES (?, ?, ?)""",
        (a_id, b_id, link_type),
    )
    return bool(cursor.rowcount)


def related_ids(
    db,
    item_id: int,
    include_self: bool = False,
    *,
    visibility_sql: str | None = None,
    visibility_params: list | tuple = (),
) -> list[int]:
    """Return the transitive item-link component containing ``item_id``.

    When ``visibility_sql`` is supplied it must reference the ``i`` item alias.
    Inaccessible nodes are removed from the recursive graph itself, not merely
    from the final result. This is important for library permissions: a hidden
    B in A↔B↔C must not act as an invisible bridge that tells a user A and C
    are related.
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
    include_self: bool = False,
    *,
    visibility_sql: str | None = None,
    visibility_params: list | tuple = (),
):
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
        f"""SELECT * FROM items WHERE id IN ({placeholders})
            ORDER BY title COLLATE NOCASE, media_type, platform, id""",
        tuple(ids),
    ).fetchall()


def auto_link_item(db, item_id: int) -> int:
    """Attach one item to an existing automatic family group when safe."""
    item = db.execute(
        """SELECT id, title, authors, isbn, media_type, publish_year
           FROM items WHERE id = ?""",
        (item_id,),
    ).fetchone()
    if not item:
        return 0
    family = family_for(item["media_type"])
    if not family or not normalise_title(item["title"]):
        return 0
    media_types = tuple(FAMILIES[family])
    placeholders = ",".join("?" for _ in media_types)
    candidates = db.execute(
        f"""SELECT id, title, authors, isbn, media_type, publish_year
            FROM items
            WHERE media_type IN ({placeholders}) AND id != ?
            ORDER BY id""",
        (*media_types, item_id),
    ).fetchall()
    matches = [candidate for candidate in candidates if _match(item, candidate, family)]
    if not matches:
        return 0
    # One edge is enough: the group is a connected component, not a clique.
    return int(link_items(db, item_id, matches[0]["id"], "format"))


def auto_link_family(db, family: str) -> int:
    """Build sparse automatic groups for one media family.

    Provider syncs call this once after their batch instead of performing an
    O(n) search for every imported item. Each natural cluster becomes a star
    around its first item, so N representations require N-1 links rather than
    N² pairwise edges.
    """
    media_types = FAMILIES.get(family)
    if not media_types:
        return 0
    placeholders = ",".join("?" for _ in media_types)
    rows = db.execute(
        f"""SELECT id, title, authors, isbn, media_type, publish_year
            FROM items WHERE media_type IN ({placeholders}) ORDER BY id""",
        tuple(media_types),
    ).fetchall()
    by_title: dict[str, list] = defaultdict(list)
    by_isbn: dict[str, list] = defaultdict(list)
    for row in rows:
        title_key = normalise_title(row["title"])
        if title_key:
            by_title[title_key].append(row)
        if family in ("book", "comic", "manga") and row["isbn"]:
            by_isbn[row["isbn"]].append(row)

    created = 0
    # ISBN is the strongest book/comic/manga signal and may bridge title variations.
    for group in by_isbn.values():
        if len(group) < 2:
            continue
        canonical = group[0]
        for row in group[1:]:
            created += int(link_items(db, canonical["id"], row["id"], "format"))

    for group in by_title.values():
        if len(group) < 2:
            continue
        clusters: list[list] = []
        for row in group:
            target = None
            for cluster in clusters:
                if _match(cluster[0], row, family):
                    target = cluster
                    break
            if target is None:
                clusters.append([row])
                continue
            created += int(link_items(db, target[0]["id"], row["id"], "format"))
            target.append(row)
    return created


def rebuild_automatic_connections(db) -> dict:
    """Retroactively build safe automatic format links for existing items.

    This is the maintenance equivalent of the per-insert/provider-sync
    autolinking paths. It deliberately uses the same conservative family
    boundaries and matching rules, so running it cannot create cross-media
    adaptation links. ``link_items`` is idempotent, making this safe to run
    repeatedly after imports or metadata clean-up.
    """
    family_results = []
    total_scanned = 0
    total_created = 0
    for family, media_types in FAMILIES.items():
        placeholders = ",".join("?" for _ in media_types)
        scanned = db.execute(
            f"SELECT COUNT(*) AS c FROM items WHERE media_type IN ({placeholders})",
            tuple(media_types),
        ).fetchone()["c"]
        created = auto_link_family(db, family)
        total_scanned += scanned
        total_created += created
        family_results.append({
            "family": family,
            "scanned": scanned,
            "created": created,
        })
    return {
        "scanned": total_scanned,
        "created": total_created,
        "families": family_results,
    }


def should_autolink_on_insert(source: str | None) -> bool:
    """Provider batches group once after sync; ordinary inserts group immediately."""
    return (source or "manual") not in _PROVIDER_SYNC_SOURCES


def search_candidates(db, item_id: int, query: str, limit: int = 20):
    """Find items that can be manually attached to the current media group."""
    current = db.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()
    if not current:
        return []
    excluded = set(related_ids(db, item_id, include_self=True))
    q = query.strip()
    if q:
        like = f"%{q}%"
        rows = db.execute(
            """SELECT * FROM items
               WHERE title LIKE ? COLLATE NOCASE
                  OR authors LIKE ? COLLATE NOCASE
                  OR series_name LIKE ? COLLATE NOCASE
               ORDER BY title COLLATE NOCASE, media_type, id
               LIMIT 100""",
            (like, like, like),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM items ORDER BY title COLLATE NOCASE, media_type, id"
        ).fetchall()
        target = normalise_title(current["title"])
        rows = [row for row in rows if normalise_title(row["title"]) == target]
    rows = [row for row in rows if row["id"] not in excluded]
    if q:
        target = normalise_title(current["title"])
        rows.sort(
            key=lambda row: (
                normalise_title(row["title"]) != target,
                row["title"].casefold(),
                row["id"],
            )
        )
    return rows[:limit]


def has_manual_group_edge(db, anchor_id: int, target_id: int) -> bool:
    """Whether target has a manual edge into anchor's current group."""
    group = set(related_ids(db, anchor_id, include_self=True))
    if target_id not in group:
        return False
    group.discard(target_id)
    if not group:
        return False
    placeholders = ",".join("?" for _ in group)
    row = db.execute(
        f"""SELECT 1 FROM item_links
            WHERE link_type = 'related'
              AND ((item_a_id = ? AND item_b_id IN ({placeholders}))
                OR (item_b_id = ? AND item_a_id IN ({placeholders})))
            LIMIT 1""",
        (target_id, *group, target_id, *group),
    ).fetchone()
    return bool(row)


def remove_manual_group_edges(db, anchor_id: int, target_id: int) -> int:
    """Detach target's user-created edges into this group.

    Automatic format links are intentionally left alone; a provider sync should
    remain authoritative about safe same-family matches.
    """
    group = set(related_ids(db, anchor_id, include_self=True))
    if target_id not in group:
        return 0
    group.discard(target_id)
    if not group:
        return 0
    placeholders = ",".join("?" for _ in group)
    cursor = db.execute(
        f"""DELETE FROM item_links
            WHERE link_type = 'related'
              AND ((item_a_id = ? AND item_b_id IN ({placeholders}))
                OR (item_b_id = ? AND item_a_id IN ({placeholders})))""",
        (target_id, *group, target_id, *group),
    )
    return cursor.rowcount