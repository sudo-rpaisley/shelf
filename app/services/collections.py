"""Library-scoped curated Collections.

Collections are intentional shared groupings such as favourites, projects or
course lists. They are distinct from Series (ordered publication membership),
Tags (lightweight labels) and per-user favourites. A collection belongs to one
Shelf library so its name, counts and previews obey the same access boundary as
its items.
"""

from __future__ import annotations

import re
import sqlite3

from app.services import libraries

MAX_COLLECTION_NAME = 100
MAX_COLLECTION_DESCRIPTION = 500


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:MAX_COLLECTION_NAME]


def normalize_description(value: str | None) -> str | None:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()[:MAX_COLLECTION_DESCRIPTION]
    return clean or None


def editable_libraries(db, user: dict) -> list[dict]:
    rows = libraries.list_libraries(db)
    return [row for row in rows if libraries.has_library_role(db, user, row["id"], "editor")]


def _visible_library_ids(db, user: dict) -> list[int]:
    return libraries.accessible_library_ids(db, user)


def get_collection(db, user: dict, collection_id: int) -> dict | None:
    row = db.execute(
        "SELECT c.*, l.name AS library_name FROM collections c "
        "JOIN libraries l ON l.id = c.library_id WHERE c.id = ?",
        (collection_id,),
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    if not libraries.has_library_role(db, user, result["library_id"], "viewer"):
        return None
    result["can_edit"] = libraries.has_library_role(
        db, user, result["library_id"], "editor"
    )
    return result


def list_cards(db, user: dict) -> list[dict]:
    library_ids = _visible_library_ids(db, user)
    if not library_ids:
        return []
    placeholders = ",".join("?" for _ in library_ids)
    rows = db.execute(
        "SELECT c.id, c.library_id, c.name, c.description, c.created_at, c.updated_at, "
        "l.name AS library_name, COUNT(ci.item_id) AS item_count "
        "FROM collections c JOIN libraries l ON l.id = c.library_id "
        "LEFT JOIN collection_items ci ON ci.collection_id = c.id "
        f"WHERE c.library_id IN ({placeholders}) "
        "GROUP BY c.id ORDER BY l.name COLLATE NOCASE, c.name COLLATE NOCASE",
        library_ids,
    ).fetchall()
    cards = []
    for row in rows:
        card = dict(row)
        card["can_edit"] = libraries.has_library_role(
            db, user, card["library_id"], "editor"
        )
        card["preview_items"] = [dict(item) for item in db.execute(
            "SELECT i.id, i.title, i.cover_path, i.media_type FROM collection_items ci "
            "JOIN items i ON i.id = ci.item_id "
            "JOIN library_items li ON li.item_id = i.id "
            "WHERE ci.collection_id = ? AND li.library_id = ? "
            "ORDER BY ci.created_at DESC, i.id DESC LIMIT 4",
            (card["id"], card["library_id"]),
        ).fetchall()]
        cards.append(card)
    return cards


def list_items(db, user: dict, collection_id: int) -> tuple[dict | None, list[dict]]:
    collection = get_collection(db, user, collection_id)
    if not collection:
        return None, []
    rows = db.execute(
        "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, i.publish_year "
        "FROM collection_items ci JOIN items i ON i.id = ci.item_id "
        "JOIN library_items li ON li.item_id = i.id "
        "WHERE ci.collection_id = ? AND li.library_id = ? "
        "ORDER BY i.title COLLATE NOCASE, i.id",
        (collection_id, collection["library_id"]),
    ).fetchall()
    return collection, [dict(row) for row in rows]


def create(db, user: dict, library_id: int, name: str, description: str | None = None) -> int:
    if not libraries.has_library_role(db, user, library_id, "editor"):
        raise PermissionError("Library editor access required")
    clean_name = normalize_name(name)
    if not clean_name:
        raise ValueError("Collection name required")
    try:
        cur = db.execute(
            "INSERT INTO collections (library_id, name, description) VALUES (?, ?, ?)",
            (library_id, clean_name, normalize_description(description)),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("A collection with that name already exists in this library") from exc
    return int(cur.lastrowid)


def update(db, user: dict, collection_id: int, name: str, description: str | None = None) -> None:
    collection = get_collection(db, user, collection_id)
    if not collection:
        raise LookupError("Collection not found")
    if not collection["can_edit"]:
        raise PermissionError("Library editor access required")
    clean_name = normalize_name(name)
    if not clean_name:
        raise ValueError("Collection name required")
    try:
        db.execute(
            "UPDATE collections SET name = ?, description = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (clean_name, normalize_description(description), collection_id),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError("A collection with that name already exists in this library") from exc


def delete(db, user: dict, collection_id: int) -> None:
    collection = get_collection(db, user, collection_id)
    if not collection:
        raise LookupError("Collection not found")
    if not collection["can_edit"]:
        raise PermissionError("Library editor access required")
    db.execute("DELETE FROM collections WHERE id = ?", (collection_id,))


def add_item(db, user: dict, collection_id: int, item_id: int) -> None:
    collection = get_collection(db, user, collection_id)
    if not collection:
        raise LookupError("Collection not found")
    if not collection["can_edit"] or not libraries.has_item_role(db, user, item_id, "editor"):
        raise PermissionError("Library editor access required")
    item_library = libraries.item_library_id(db, item_id)
    if item_library != collection["library_id"]:
        raise ValueError("Collection and item must belong to the same library")
    db.execute(
        "INSERT OR IGNORE INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (collection_id, item_id),
    )


def remove_item(db, user: dict, collection_id: int, item_id: int) -> None:
    collection = get_collection(db, user, collection_id)
    if not collection:
        raise LookupError("Collection not found")
    if not collection["can_edit"] or not libraries.has_item_role(db, user, item_id, "editor"):
        raise PermissionError("Library editor access required")
    result = db.execute(
        "DELETE FROM collection_items WHERE collection_id = ? AND item_id = ?",
        (collection_id, item_id),
    )
    if result.rowcount != 1:
        raise LookupError("Collection membership not found")


def accessible_options(db, user: dict) -> list[dict]:
    """Collections the user may see, labelled with their library."""
    library_ids = _visible_library_ids(db, user)
    if not library_ids:
        return []
    marks = ",".join("?" for _ in library_ids)
    rows = db.execute(
        "SELECT c.id, c.library_id, c.name, l.name AS library_name "
        "FROM collections c JOIN libraries l ON l.id = c.library_id "
        f"WHERE c.library_id IN ({marks}) "
        "ORDER BY l.name COLLATE NOCASE, c.name COLLATE NOCASE",
        library_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def item_options(db, user: dict, item_id: int) -> tuple[list[dict], bool]:
    """Same-library Collections for one visible item plus edit capability."""
    if not libraries.has_item_role(db, user, item_id, "viewer"):
        return [], False
    library_id = libraries.item_library_id(db, item_id)
    if library_id is None:
        return [], False
    rows = db.execute(
        "SELECT c.id, c.library_id, c.name, l.name AS library_name, "
        "EXISTS(SELECT 1 FROM collection_items ci "
        "       WHERE ci.collection_id = c.id AND ci.item_id = ?) AS selected "
        "FROM collections c JOIN libraries l ON l.id = c.library_id "
        "WHERE c.library_id = ? ORDER BY c.name COLLATE NOCASE",
        (item_id, library_id),
    ).fetchall()
    can_edit = libraries.has_item_role(db, user, item_id, "editor")
    return [dict(row) for row in rows], can_edit
