"""Compatibility synchronisation for the new holding/location model.

The new tables are deliberately additive. Existing routes still write the
legacy item/location/provider columns, so startup can project that state into
copies, location nodes and digital holdings without database triggers. Keeping
this in application code also preserves Shelf's restore rule that uploaded
SQLite databases must not contain triggers.
"""

from app.media_types import is_physical_media


def ensure_legacy_location_nodes(db) -> None:
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
    db.execute(
        "DELETE FROM digital_holdings WHERE item_id = ? AND provider = ?",
        (item["id"], provider),
    )
    external_id = item[id_key]
    if external_id and str(external_id).strip():
        db.execute(
            "INSERT INTO digital_holdings "
            "(item_id, provider, external_id, library_id, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (item["id"], provider, str(external_id), item[library_key]),
        )


def sync_item_holding(db, item_id: int) -> None:
    """Project one legacy item row into the new compatibility tables.

    Never moves an existing primary copy out of a nested location. The legacy
    flat location is used only when creating the first copy, or when that copy
    currently has no precise location at all.
    """
    item = db.execute(
        "SELECT id, media_type, owned, location_id, abs_id, abs_library_id, "
        "komga_id, komga_library_id, romm_id, romm_platform_id "
        "FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
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
            "SELECT id, location_id FROM item_copies "
            "WHERE item_id = ? AND is_primary = 1 ORDER BY id LIMIT 1",
            (item_id,),
        ).fetchone()
        if copy is None:
            db.execute(
                "INSERT INTO item_copies "
                "(item_id, copy_number, location_id, is_primary) VALUES (?, 1, ?, 1)",
                (item_id, root_id),
            )
        elif copy["location_id"] is None and root_id is not None:
            db.execute(
                "UPDATE item_copies SET location_id = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (root_id, copy["id"]),
            )

    _sync_provider(db, item, "audiobookshelf", "abs_id", "abs_library_id")
    _sync_provider(db, item, "komga", "komga_id", "komga_library_id")
    _sync_provider(db, item, "romm", "romm_id", "romm_platform_id")


def sync_all_holdings(db) -> None:
    ensure_legacy_location_nodes(db)
    item_ids = [row["id"] for row in db.execute("SELECT id FROM items").fetchall()]
    for item_id in item_ids:
        sync_item_holding(db, item_id)
