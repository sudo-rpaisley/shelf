"""Compatibility synchronisation for the holding/location model.

The new tables are deliberately additive. Existing routes still write legacy
item/location/provider columns while the richer UI is introduced, so this
module projects that state into copies, location nodes and digital holdings in
application code. Keeping synchronisation out of SQLite triggers preserves
Shelf's restore rule that uploaded databases must not contain triggers.
"""

from app.media_types import is_physical_media


_REQUIRED_TABLES = frozenset({"location_nodes", "item_copies", "digital_holdings"})
_PROVIDER_FIELDS = (
    ("audiobookshelf", "abs_id", "abs_library_id"),
    ("komga", "komga_id", "komga_library_id"),
    ("romm", "romm_id", "romm_platform_id"),
)


def schema_available(db) -> bool:
    """Whether this connection has the additive holdings schema installed."""
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('location_nodes', 'item_copies', 'digital_holdings')"
    ).fetchall()
    return {row["name"] for row in rows} == _REQUIRED_TABLES


def ensure_legacy_location_nodes(db) -> None:
    """Project legacy flat locations to root nodes, idempotently."""
    if not schema_available(db):
        return
    db.execute(
        "INSERT OR IGNORE INTO location_nodes (name, sort_order, legacy_location_id) "
        "SELECT name, sort_order, id FROM locations"
    )
    # Existing roots may have been renamed/reordered since the previous sync.
    db.execute(
        "UPDATE location_nodes SET "
        "name = (SELECT l.name FROM locations l WHERE l.id = legacy_location_id), "
        "sort_order = (SELECT l.sort_order FROM locations l WHERE l.id = legacy_location_id), "
        "updated_at = datetime('now') "
        "WHERE legacy_location_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM locations l WHERE l.id = legacy_location_id)"
    )


def _sync_provider(db, item, provider: str, id_key: str, library_key: str) -> None:
    # Legacy migration fixtures can legitimately omit a provider's columns.
    # A startup sync must therefore project only fields present on this row,
    # rather than making the additive tables depend on migration order.
    keys = set(item.keys())
    if id_key not in keys:
        return

    db.execute(
        "DELETE FROM digital_holdings WHERE item_id = ? AND provider = ?",
        (item["id"], provider),
    )
    external_id = item[id_key]
    if external_id and str(external_id).strip():
        library_id = item[library_key] if library_key in keys else None
        db.execute(
            "INSERT INTO digital_holdings "
            "(item_id, provider, external_id, library_id, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (item["id"], provider, str(external_id), library_id),
        )


def sync_item_holding(db, item_id: int) -> None:
    """Project one legacy item row into the additive holdings tables.

    A legacy root-to-root move follows ``items.location_id``. Once a copy has
    been placed in a more precise child node, however, the flat compatibility
    field must never pull it back out of that shelf. This distinction lets old
    routes keep working while nested locations become authoritative.
    """
    if not schema_available(db):
        return

    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return

    ensure_legacy_location_nodes(db)

    if item["owned"] and is_physical_media(item["media_type"]):
        root = None
        if item["location_id"] is not None:
            root = db.execute(
                "SELECT id FROM location_nodes WHERE legacy_location_id = ?",
                (item["location_id"],),
            ).fetchone()
        root_id = root["id"] if root else None

        copy = db.execute(
            "SELECT c.id, c.location_id, n.parent_id, n.legacy_location_id "
            "FROM item_copies c "
            "LEFT JOIN location_nodes n ON n.id = c.location_id "
            "WHERE c.item_id = ? AND c.is_primary = 1 ORDER BY c.id LIMIT 1",
            (item_id,),
        ).fetchone()
        if copy is None:
            db.execute(
                "INSERT INTO item_copies "
                "(item_id, copy_number, location_id, is_primary) VALUES (?, 1, ?, 1)",
                (item_id, root_id),
            )
        elif copy["location_id"] is None:
            if root_id is not None:
                db.execute(
                    "UPDATE item_copies SET location_id = ?, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (root_id, copy["id"]),
                )
        elif copy["legacy_location_id"] is not None:
            # The copy is still on a compatibility root, so an old-style move
            # may safely move that root. Child/shelf nodes have no legacy id
            # and are deliberately preserved.
            if copy["location_id"] != root_id:
                db.execute(
                    "UPDATE item_copies SET location_id = ?, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (root_id, copy["id"]),
                )

    for provider, id_key, library_key in _PROVIDER_FIELDS:
        _sync_provider(db, item, provider, id_key, library_key)


def sync_all_holdings(db) -> None:
    """Idempotently backfill compatibility projections for every item."""
    if not schema_available(db):
        return
    ensure_legacy_location_nodes(db)
    item_ids = [row["id"] for row in db.execute("SELECT id FROM items").fetchall()]
    for item_id in item_ids:
        sync_item_holding(db, item_id)
