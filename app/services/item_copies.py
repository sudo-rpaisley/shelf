"""The physical-copy write funnel.

``item_copies.location_id`` ALWAYS names ``location_nodes``; the item-level
compatibility field ALWAYS names flat ``locations``. Only the explicit legacy
adapter below crosses that boundary. Copies (including Trash) retain their IDs,
numbers, barcodes and acquisition history. No read/ensure operation backfills.

Mutations take/upgrade the caller's transaction's write lock, then re-read and
authorize the parent. They never commit, log, or open a second connection.
"""

from collections.abc import Mapping
from functools import wraps
from typing import Any

from app.media_types import is_physical_media
from app.services import trash
from app.services.catalogue_security import require_item_role

_MANAGED = frozenset({"id", "deleted_at"})
_MANAGED_ON_UPDATE = frozenset({"id", "created_at", "deleted_at"})
_columns: frozenset[str] | None = None


def write_lock(db) -> None:
    """Acquire a reserved lock, including when handed a deferred transaction."""
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    else:
        # BEGIN cannot be nested. A zero-row write upgrades a deferred BEGIN
        # before any authorization/census read, without touching timestamps.
        db.execute("UPDATE item_copies SET updated_at = updated_at WHERE 0")


def _atomic_copy_write(function):
    """A caller may catch a refusal without committing half a copy/seam edit."""
    @wraps(function)
    def apply(db, *args, **kwargs):
        write_lock(db)
        db.execute("SAVEPOINT physical_copy_write")
        try:
            result = function(db, *args, **kwargs)
        except Exception:
            db.execute("ROLLBACK TO physical_copy_write")
            db.execute("RELEASE physical_copy_write")
            raise
        db.execute("RELEASE physical_copy_write")
        return result
    return apply


def _read_columns(db) -> frozenset[str]:
    rows = db.execute("PRAGMA table_info(item_copies)").fetchall()
    if not rows:
        raise RuntimeError("item_copies requires an initialized central schema")
    return frozenset(r["name"] if hasattr(r, "keys") else r[1] for r in rows)


def copy_columns(db) -> frozenset[str]:
    # Connections can represent different restored schemas in one process.
    # Never let another connection's cached columns authorize an SQL identifier.
    global _columns
    _columns = _read_columns(db)
    return _columns


def reset_column_cache() -> None:
    global _columns
    _columns = None


def _validated_names(db, values: Mapping[str, Any], managed: frozenset[str],
                     who: str) -> None:
    unknown = set(values) - copy_columns(db)
    if unknown:
        raise ValueError(f"{who}() got field(s) not on the item_copies table: {sorted(unknown)}")
    hit = set(values) & managed
    if hit:
        raise ValueError(f"{who}() cannot set {sorted(hit)} — the database assigns it.")


def _parent(db, item_id: int, *, include_deleted: bool = False):
    require_item_role(db, item_id, "editor", include_deleted=include_deleted)
    relation = "items" if include_deleted else "items_live"
    item = db.execute(f"SELECT * FROM {relation} WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise ValueError("Item not found")
    return item


def _eligible(item) -> bool:
    return bool(item["owned"] and is_physical_media(item["media_type"]))


def _copy_for_write(db, copy_id: int, *, item_id: int | None = None,
                    include_deleted: bool = False):
    relation = "item_copies" if include_deleted else "copies_live"
    sql, params = f"SELECT * FROM {relation} WHERE id = ?", [copy_id]
    if item_id is not None:
        sql += " AND item_id = ?"
        params.append(item_id)
    copy = db.execute(sql, params).fetchone()
    if copy is not None:
        _parent(db, copy["item_id"], include_deleted=include_deleted)
    return copy


def _node(db, value):
    from app.services.location_tree import validated_node_id
    return validated_node_id(db, value)


def _sync_legacy_seam(db, item_id: int, node_id: int | None) -> None:
    """Project a primary NODE to its nearest LEGACY ancestor through item_write.

    The callback may invoke sync_primary_location. That adapter never moves a
    precise child placement, and a root's own projection is a no-op move.
    """
    from app.services import item_write
    from app.services.location_tree import node_to_legacy_location

    legacy_id = node_to_legacy_location(db, node_id)
    current = db.execute(
        "SELECT location_id FROM items_live WHERE id = ?", (item_id,)
    ).fetchone()
    if current is not None and current["location_id"] != legacy_id:
        item_write.update_item_fields(db, item_id, {"location_id": legacy_id})


@_atomic_copy_write
def insert_copy(db, fields: Mapping[str, Any], *, replay: bool = False) -> int:
    """Insert a copy with NODE placement. Database-managed fields are refused.

    ``is_primary`` is explicit here for archive/merge; the public ``add_copy``
    decides it itself. Restore of an existing copy uses restore_copy instead.
    ``replay`` is the trusted archive's explicit history replay: a previously
    physical copy may outlive an ownership/type edit. It never bypasses ACLs.
    """
    values = dict(fields)
    _validated_names(db, values, _MANAGED, "insert_copy")
    for required in ("item_id", "copy_number"):
        if values.get(required) is None:
            raise ValueError(f"insert_copy() requires a non-null {required!r}")
    write_lock(db)
    item = _parent(db, values["item_id"])
    if not replay and not _eligible(item):
        raise ValueError("Only owned physical media can have copies")
    if "location_id" in values:
        values["location_id"] = _node(db, values["location_id"])
    names = list(values)
    cursor = db.execute(
        f"INSERT INTO item_copies ({', '.join(names)}) "
        f"VALUES ({', '.join('?' for _ in names)})",
        [values[n] for n in names],
    )
    return cursor.lastrowid


@_atomic_copy_write
def update_copy(db, copy_id: int, fields: Mapping[str, Any], *,
                expect_location_id: int | None = None) -> bool:
    """Update a live copy; NODE moves clear only location-scoped position.

    A structural reparent (item_id, copy_number, is_primary only) may also
    move a trashed copy for item_merge without losing its reversible state.
    Both old and new parents are authorized inside the lock. Ordinary edits
    cannot touch a hidden/deleted copy or change its deletion timestamp.
    """
    values = dict(fields)
    values.pop("updated_at", None)
    _validated_names(db, values, _MANAGED_ON_UPDATE, "update_copy")
    write_lock(db)
    reparent = "item_id" in values
    if reparent and set(values) - {"item_id", "copy_number", "is_primary"}:
        raise ValueError("Reparent copies separately from editing their metadata")
    copy = _copy_for_write(db, copy_id, include_deleted=reparent)
    if copy is None:
        return False
    if reparent:
        _parent(db, values["item_id"], include_deleted=True)
    if expect_location_id is not None and copy["location_id"] != expect_location_id:
        return False
    if "location_id" in values:
        values["location_id"] = _node(db, values["location_id"])
    assignments = [f"{name} = ?" for name in values]
    params = list(values.values())
    if "location_id" in values and "position_order" not in values:
        assignments.append("position_order = CASE WHEN location_id IS ? THEN position_order ELSE NULL END")
        params.append(values["location_id"])
    assignments.append("updated_at = datetime('now')")
    params.append(copy_id)
    where = "id = ?"
    if expect_location_id is not None:
        where += " AND location_id = ?"
        params.append(expect_location_id)
    cursor = db.execute(
        f"UPDATE item_copies SET {', '.join(assignments)} WHERE {where}", params
    )
    if cursor.rowcount and "location_id" in values and copy["is_primary"]:
        _sync_legacy_seam(db, copy["item_id"], values["location_id"])
    return cursor.rowcount > 0


@_atomic_copy_write
def relocate_copy(db, copy: Mapping[str, Any], location_id: int) -> None:
    """Relocate the exact live copy; never trust an earlier parent/primary read."""
    write_lock(db)
    current = _copy_for_write(db, copy["id"], item_id=copy["item_id"])
    if current is None:
        raise ValueError("Copy not found")
    update_copy(db, current["id"], {"location_id": _node(db, location_id)})


@_atomic_copy_write
def mark_missing(db, copy_id: int) -> bool:
    write_lock(db)
    copy = _copy_for_write(db, copy_id)
    if copy is None or copy["missing_since"] is not None:
        return False
    today = db.execute("SELECT date('now')").fetchone()[0]
    return update_copy(db, copy_id, {"missing_since": today})


@_atomic_copy_write
def clear_missing(db, copy_id: int) -> bool:
    write_lock(db)
    copy = _copy_for_write(db, copy_id)
    if copy is None or copy["missing_since"] is None:
        return False
    return update_copy(db, copy_id, {"missing_since": None})


@_atomic_copy_write
def sync_primary_location(db, item_id: int, location_id: int | None, *,
                          create_copy: bool = False) -> int | None:
    """Adapt the LEGACY item seam, preserving precise child NODE placement.

    Creation is explicit, for a newly inserted/explicitly acquired physical
    item, not a metadata edit or a read. Any physical copy history (including
    Trash) suppresses creation. An intentionally empty archive stays empty.
    """
    from app.services.location_tree import legacy_location_to_node

    write_lock(db)
    item = _parent(db, item_id)
    if not _eligible(item):
        return None
    node_id = legacy_location_to_node(db, location_id, create=True)
    primary = db.execute(
        "SELECT c.id, c.location_id, n.legacy_location_id FROM copies_live c "
        "LEFT JOIN location_nodes n ON n.id = c.location_id "
        "WHERE c.item_id = ? AND c.is_primary = 1 ORDER BY c.copy_number, c.id LIMIT 1",
        (item_id,),
    ).fetchone()
    if primary is not None:
        # A mapped legacy node may follow a flat-to-flat move. A precise
        # child/unmapped node must NEVER be flattened by this compatibility arm.
        if primary["location_id"] != node_id and (
            primary["location_id"] is None or primary["legacy_location_id"] is not None
        ):
            update_copy(db, primary["id"], {"location_id": node_id})
        return primary["id"]
    existing = db.execute(
        "SELECT 1 FROM item_copies WHERE item_id = ? LIMIT 1", (item_id,)
    ).fetchone()
    if not create_copy or existing is not None:
        return None
    return insert_copy(db, {
        "item_id": item_id, "copy_number": 1,
        "location_id": node_id, "is_primary": 1,
    })


def backfill_legacy_locations(db) -> int:
    """Compatibility entry: central initialization ALONE owns one-time seeds.

    Do not sweep here, even if the foundation marker exists but copies are
    deliberately absent. The return count describes only central schema work.
    """
    from app.database import ensure_holdings_schema

    before = db.total_changes
    ensure_holdings_schema(db)
    return db.total_changes - before


@_atomic_copy_write
def delete_copies_for_item(db, item_id: int) -> int:
    """Archive replay's explicit placeholder removal, not ordinary removal."""
    write_lock(db)
    _parent(db, item_id, include_deleted=True)
    return db.execute("DELETE FROM item_copies WHERE item_id = ?", (item_id,)).rowcount


def copies_for_item(db, item_id: int):
    """Live visible copies in stable number order, with NODE path display."""
    from app.services.location_tree import location_path

    rows = db.execute(
        "SELECT c.*, n.name AS location_name FROM copies_live c "
        "JOIN items_live i ON i.id = c.item_id "
        "LEFT JOIN location_nodes n ON n.id = c.location_id "
        "WHERE c.item_id = ? ORDER BY c.copy_number, c.id", (item_id,)
    ).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        data["location_node_id"] = row["location_id"]
        data["location_path"] = " › ".join(
            part["name"] for part in location_path(db, row["location_id"])
        ) or None
        result.append(data)
    return result


def _lowest_numbered_copy(db, item_id: int):
    return db.execute(
        "SELECT id, location_id FROM copies_live WHERE item_id = ? "
        "ORDER BY copy_number, id LIMIT 1", (item_id,)
    ).fetchone()


@_atomic_copy_write
def add_copy(db, item_id: int, fields: Mapping[str, Any] | None = None) -> int:
    """Add an owned physical copy; number above all history, primary if first."""
    values = dict(fields or {})
    forbidden = {"item_id", "copy_number", "is_primary"} & set(values)
    if forbidden:
        raise ValueError(f"add_copy() does not take {sorted(forbidden)}")
    write_lock(db)
    item = _parent(db, item_id)
    if not _eligible(item):
        raise ValueError("Only owned physical media can have copies")
    highest = db.execute(
        "SELECT COALESCE(MAX(copy_number), 0) AS n FROM item_copies WHERE item_id = ?",
        (item_id,),
    ).fetchone()["n"]
    has_primary = db.execute(
        "SELECT 1 FROM copies_live WHERE item_id = ? AND is_primary = 1", (item_id,)
    ).fetchone()
    values.update(item_id=item_id, copy_number=highest + 1, is_primary=int(not has_primary))
    copy_id = insert_copy(db, values)
    if not has_primary:
        _sync_legacy_seam(db, item_id, values.get("location_id"))
    return copy_id


def _settle_after_removal(db, item_id: int, was_primary: bool) -> dict[str, Any]:
    survivor = _lowest_numbered_copy(db, item_id)
    promoted = None
    if survivor is not None and was_primary:
        update_copy(db, survivor["id"], {"is_primary": 1})
        promoted = survivor["id"]
        _sync_legacy_seam(db, item_id, survivor["location_id"])
    elif survivor is None:
        _sync_legacy_seam(db, item_id, None)
    remaining = db.execute(
        "SELECT COUNT(*) AS n FROM copies_live WHERE item_id = ?", (item_id,)
    ).fetchone()["n"]
    return {"item_id": item_id, "was_primary": was_primary,
            "promoted_copy_id": promoted, "remaining": remaining}


@_atomic_copy_write
def trash_copy(db, copy_id: int, *, at: str | None = None,
                replay: bool = False) -> dict[str, Any] | None:
    """Reversible removal; refuse the last live owned physical copy.

    ``at`` is an already validated historical timestamp, NOT a last-copy bypass.
    ``replay=True`` explicitly reproduces the archive's entirely trashed source,
    unlike interactive removal. No ordinary route accepts either argument.
    """
    write_lock(db)
    copy = _copy_for_write(db, copy_id)
    if copy is None:
        return None
    item = _parent(db, copy["item_id"])
    count = db.execute(
        "SELECT COUNT(*) AS n FROM copies_live WHERE item_id = ?", (copy["item_id"],)
    ).fetchone()["n"]
    if not replay and _eligible(item) and count <= 1:
        raise ValueError("An owned physical item must keep at least one copy")
    db.execute(
        "UPDATE item_copies SET deleted_at = COALESCE(?, datetime('now')), "
        "is_primary = 0, updated_at = datetime('now') WHERE id = ?", (at, copy_id)
    )
    trash.invalidate(db)
    return _settle_after_removal(db, copy["item_id"], bool(copy["is_primary"]))


@_atomic_copy_write
def restore_copy(db, copy_id: int) -> dict[str, Any] | None:
    """Restore the same row/number/barcode; a trashed parent must restore first."""
    write_lock(db)
    # Physical table, deliberately: restore finds the row hidden by the view.
    copy = db.execute(
        "SELECT * FROM item_copies WHERE id = ? AND deleted_at IS NOT NULL", (copy_id,)
    ).fetchone()
    if copy is None:
        return None
    require_item_role(db, copy["item_id"], "editor", include_deleted=True)
    if db.execute("SELECT 1 FROM items_live WHERE id = ?", (copy["item_id"],)).fetchone() is None:
        return None
    _parent(db, copy["item_id"])
    has_primary = db.execute(
        "SELECT 1 FROM copies_live WHERE item_id = ? AND is_primary = 1", (copy["item_id"],)
    ).fetchone()
    db.execute(
        "UPDATE item_copies SET deleted_at = NULL, is_primary = ?, "
        "updated_at = datetime('now') WHERE id = ?", (int(not has_primary), copy_id)
    )
    if not has_primary:
        _sync_legacy_seam(db, copy["item_id"], copy["location_id"])
    remaining = db.execute(
        "SELECT COUNT(*) AS n FROM copies_live WHERE item_id = ?", (copy["item_id"],)
    ).fetchone()["n"]
    trash.settle_marker(db)
    trash.invalidate(db)
    return {"item_id": copy["item_id"], "copy_id": copy_id,
            "is_primary": not bool(has_primary), "remaining": remaining}


@_atomic_copy_write
def purge_copy(db, copy_id: int) -> bool:
    """Native Trash admin route's destructive arm; never purges a live row."""
    write_lock(db)
    copy = db.execute(
        "SELECT item_id FROM item_copies WHERE id = ? AND deleted_at IS NOT NULL", (copy_id,)
    ).fetchone()
    if copy is None:
        return False
    require_item_role(db, copy["item_id"], "admin", include_deleted=True)
    db.execute("DELETE FROM item_copies WHERE id = ? AND deleted_at IS NOT NULL", (copy_id,))
    trash.settle_marker(db)
    trash.invalidate(db)
    return True
