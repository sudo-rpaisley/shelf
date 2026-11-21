"""Location APIs with explicit, incompatible NODE and LEGACY ID domains.

Modern hierarchy/physical selectors use nodes. The main Settings/CSV/item seam
uses flat legacy locations. Neither domain is a fallback for the other's IDs.
Schema preparation is exclusively app.database's responsibility.
"""

import sqlite3
from contextlib import contextmanager

from app.services import location_tree as tree
from app.services.catalogue_security import require_item_role


class LocationError(ValueError):
    pass


class LocationNotFound(LocationError):
    pass


class DuplicateLocation(LocationError):
    pass


class InvalidLocationParent(LocationError):
    pass


class LocationHasChildren(LocationError):
    pass


class LocationHasCopies(LocationError):
    pass


def _lock(db):
    from app.services.item_copies import write_lock
    write_lock(db)


@contextmanager
def _atomic(db):
    """A caught domain error must not leave half of a two-domain edit behind."""
    db.execute("SAVEPOINT physical_location_change")
    try:
        yield
    except Exception:
        db.execute("ROLLBACK TO physical_location_change")
        db.execute("RELEASE physical_location_change")
        raise
    else:
        db.execute("RELEASE physical_location_change")


def _label(value: str) -> str:
    value = value.strip()
    if not value:
        raise LocationError("Location name cannot be blank")
    return value


def _row(db, node_id: int):
    row = db.execute("SELECT * FROM location_nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        raise LocationNotFound("Location not found")
    return row


def _parent(db, parent_id):
    return _row(db, parent_id) if parent_id is not None else None


def create_location(db, label: str, *, parent_id: int | None = None,
                    sort_order: int = 0) -> int:
    """Create a NODE (not a legacy location), returning its NODE ID."""
    label = _label(label)
    _lock(db)
    _parent(db, parent_id)
    try:
        return db.execute(
            "INSERT INTO location_nodes (name, parent_id, sort_order) VALUES (?, ?, ?)",
            (label, parent_id, sort_order),
        ).lastrowid
    except sqlite3.IntegrityError as exc:
        raise DuplicateLocation("A location with that name already exists here") from exc


def update_location(db, location_id: int, label: str, *,
                    parent_id: int | None = None, sort_order: int = 0) -> None:
    """Rename/reparent a NODE, preserving IDs, children and copy placement."""
    label = _label(label)
    _lock(db)
    node = _row(db, location_id)
    _parent(db, parent_id)
    if parent_id in tree.descendant_ids(db, location_id):
        raise InvalidLocationParent("A location cannot be moved inside itself")
    # Moving a mapped ancestor changes the legacy projection of its precise
    # copies. Authorize every affected parent before the first mutation.
    affected = [r["item_id"] for r in db.execute(
        "WITH RECURSIVE nodes(id) AS (SELECT ? UNION SELECT n.id FROM location_nodes n "
        "JOIN nodes p ON n.parent_id = p.id) "
        "SELECT DISTINCT c.item_id FROM item_copies c JOIN nodes n ON c.location_id = n.id",
        (location_id,),
    ).fetchall()]
    for item_id in affected:
        require_item_role(db, item_id, "editor", include_deleted=True)
    try:
        with _atomic(db):
            if node["legacy_location_id"] is not None:
                db.execute(
                    "UPDATE locations SET name = ?, sort_order = ? WHERE id = ?",
                    (label, sort_order, node["legacy_location_id"]),
                )
            db.execute(
                "UPDATE location_nodes SET name = ?, parent_id = ?, sort_order = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (label, parent_id, sort_order, location_id),
            )
            from app.services.item_copies import _sync_legacy_seam
            for item_id in affected:
                primary = db.execute(
                    "SELECT location_id FROM copies_live WHERE item_id = ? AND is_primary = 1", (item_id,)
                ).fetchone()
                if primary is not None:
                    _sync_legacy_seam(db, item_id, primary["location_id"])
    except sqlite3.IntegrityError as exc:
        raise DuplicateLocation("A location with that name already exists here") from exc


def delete_location(db, location_id: int) -> None:
    """Delete an empty NODE leaf, never flatten or discard placed/trashed copies."""
    _lock(db)
    node = _row(db, location_id)
    if db.execute("SELECT 1 FROM location_nodes WHERE parent_id = ?", (location_id,)).fetchone():
        raise LocationHasChildren("Move or remove child locations first")
    # Physical rows, deliberately: a trashed copy still owns its exact place.
    if db.execute("SELECT 1 FROM item_copies WHERE location_id = ?", (location_id,)).fetchone():
        raise LocationHasCopies("Move or remove copies before deleting this location")
    if node["legacy_location_id"] is not None:
        delete_legacy_location(db, node["legacy_location_id"])
    db.execute("DELETE FROM location_nodes WHERE id = ?", (location_id,))


def location_tree(db) -> list[dict]:
    """Modern display superset: NODE IDs, full-path name and local label."""
    return [{**row, "name": row["full_path"]} for row in tree.flattened_tree(db)]


def find_node_by_path(db, name: str) -> int | None:
    matches = [row["id"] for row in location_tree(db)
               if row["name"].casefold() == name.strip().casefold()]
    if len(matches) > 1:
        raise LocationError("Location path is ambiguous")
    return matches[0] if matches else None


def legacy_locations(db):
    return db.execute("SELECT * FROM locations ORDER BY sort_order, name COLLATE NOCASE, id").fetchall()


def create_legacy_location(db, name: str, *, sort_order: int = 0) -> int:
    """Settings/item compatibility: return a LEGACY ID, with an explicit map."""
    name = _label(name)
    _lock(db)
    try:
        with _atomic(db):
            legacy_id = db.execute(
                "INSERT INTO locations (name, sort_order) VALUES (?, ?)", (name, sort_order)
            ).lastrowid
            tree.legacy_location_to_node(db, legacy_id, create=True)
    except (sqlite3.IntegrityError, ValueError) as exc:
        raise DuplicateLocation("A location with that name already exists here") from exc
    return legacy_id


def update_legacy_location(db, legacy_location_id: int, name: str, *, sort_order: int = 0) -> None:
    name = _label(name)
    _lock(db)
    if not db.execute("SELECT 1 FROM locations WHERE id = ?", (legacy_location_id,)).fetchone():
        raise LocationNotFound("Location not found")
    try:
        with _atomic(db):
            db.execute("UPDATE locations SET name = ?, sort_order = ? WHERE id = ?",
                       (name, sort_order, legacy_location_id))
            node_id = tree.legacy_location_to_node(db, legacy_location_id, create=True)
            db.execute(
                "UPDATE location_nodes SET name = ?, sort_order = ?, updated_at = datetime('now') "
                "WHERE id = ?", (name, sort_order, node_id)
            )
    except (sqlite3.IntegrityError, ValueError) as exc:
        raise DuplicateLocation("A location with that name already exists here") from exc


def delete_legacy_location(db, legacy_location_id: int) -> None:
    """Atomically remove a LEGACY alias, not its independent NODE placements.

    Published items.location_id is NO ACTION, not SET NULL. The live item writer
    cannot faithfully perform this relationship-only cleanup: it excludes Trash,
    stamps metadata and projects copies. Authorize ALL physical parents first,
    then detach the alias without restoring items, touching timestamps or running
    any copy projection. This exact NULL clear is a narrow FK-cleanup exception,
    NOT another catalogue/copy metadata writer.
    """
    _lock(db)
    if not db.execute("SELECT 1 FROM locations WHERE id = ?", (legacy_location_id,)).fetchone():
        raise LocationNotFound("Location not found")
    # Physical rows, deliberately, for ALL affected parents including Trash.
    # Also protect a precise copy whose item seam names a different flat alias:
    # detaching its node's mapping must not bypass that parent's library boundary.
    affected = db.execute(
        "WITH RECURSIVE nodes(id) AS ("
        " SELECT id FROM location_nodes WHERE legacy_location_id = ? UNION "
        " SELECT n.id FROM location_nodes n JOIN nodes p ON n.parent_id = p.id"
        ") SELECT id FROM items WHERE location_id = ? UNION "
        "SELECT c.item_id AS id FROM item_copies c JOIN nodes n ON n.id = c.location_id "
        "ORDER BY id", (legacy_location_id, legacy_location_id),
    ).fetchall()
    for row in affected:
        require_item_role(db, row["id"], "editor", include_deleted=True)
    with _atomic(db):
        # Detach BEFORE clearing the item seam: the physical node still exists
        # and must never be mistaken for a mapped root to flatten/unplace later.
        db.execute(
            "UPDATE location_nodes SET legacy_location_id = NULL WHERE legacy_location_id = ?",
            (legacy_location_id,),
        )
        # Exact published-FK relationship cleanup, including Trash; deliberately
        # no updated_at, other fields, item restoration, or projection callback.
        db.execute("UPDATE items SET location_id = NULL WHERE location_id = ?", (legacy_location_id,))
        db.execute("DELETE FROM locations WHERE id = ?", (legacy_location_id,))


def find_by_path(db, name: str) -> int | None:
    """CSV/item compatibility lookup: LEGACY ID, never a NODE ID."""
    row = db.execute("SELECT id FROM locations WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchone()
    return row["id"] if row else None


def resolve_or_create(db, name: str) -> tuple[int, bool]:
    """Resolve a LEGACY item location; unknown foreign paths are not invented."""
    name = _label(name)
    found = find_by_path(db, name)
    if found is not None:
        return found, False
    if " / " in name:
        raise LocationError("Location path not found")
    return create_legacy_location(db, name), True
