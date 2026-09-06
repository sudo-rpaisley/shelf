"""Design spike for first-class physical copies (upstream issue #97).

This module is deliberately standalone while the upstream data model is being
agreed.  It proves the copy-level invariants without changing Shelf's current
item write paths, lending model, or flat location UI.

The compatibility rule is intentionally conservative: only an existing owned
item with an explicit flat ``items.location_id`` is projected automatically.
No media type is guessed to be physical or digital.  Items with no location
remain at zero copy rows until a future copy-management surface explicitly
creates one.
"""

_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS item_copies (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id            INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        copy_number        INTEGER NOT NULL,
        location_id        INTEGER REFERENCES locations(id) ON DELETE SET NULL,
        condition          TEXT,
        notes              TEXT,
        acquired_date      TEXT,
        acquisition_source TEXT,
        acquisition_price  REAL CHECK(acquisition_price IS NULL OR acquisition_price >= 0),
        provenance         TEXT,
        copy_barcode       TEXT UNIQUE,
        is_primary         INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
        created_at         TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(item_id, copy_number)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_item_copies_item ON item_copies(item_id, copy_number)",
    "CREATE INDEX IF NOT EXISTS idx_item_copies_location ON item_copies(location_id, item_id)",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_item_copies_one_primary
       ON item_copies(item_id) WHERE is_primary = 1""",
)


def install_schema(db) -> None:
    """Install the spike schema without committing caller work."""
    for statement in _SCHEMA_STATEMENTS:
        db.execute(statement)


def backfill_legacy_locations(db) -> int:
    """Create a primary copy only where the old row proves a physical place.

    ``owned`` alone is not enough evidence: current Shelf can mark digital
    Audiobookshelf rows as owned, and ``media_type`` does not reliably encode
    manifestation (an audiobook may be physical, digital, or linked to both).
    An explicit flat location, however, already means Shelf is treating the
    row as something placed on a shelf.  The operation is idempotent.
    """
    install_schema(db)
    before = db.total_changes
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
        "SELECT i.id, 1, i.location_id, 1 FROM items i "
        "WHERE i.owned = 1 AND i.location_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM item_copies c WHERE c.item_id = i.id)"
    )
    return db.total_changes - before


def sync_primary_location(db, item_id: int, location_id: int | None) -> int | None:
    """Mirror the legacy flat location into the primary copy when one exists.

    This is the compatibility seam a later implementation can call from the
    central item-write funnel.  A non-null legacy location creates the first
    copy if none exists; clearing a location never invents a copy merely to
    store ``NULL``.  Existing non-primary copies are untouched.

    Returns the primary copy id, or ``None`` when no copy exists/was needed.
    """
    install_schema(db)

    if location_id is not None and not db.execute(
        "SELECT 1 FROM locations WHERE id = ?", (location_id,)
    ).fetchone():
        raise ValueError("Location not found")

    primary = db.execute(
        "SELECT id FROM item_copies WHERE item_id = ? AND is_primary = 1",
        (item_id,),
    ).fetchone()
    if primary:
        db.execute(
            "UPDATE item_copies SET location_id = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (location_id, primary["id"]),
        )
        return primary["id"]

    if location_id is None:
        return None

    item = db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        raise ValueError("Item not found")

    next_number = db.execute(
        "SELECT COALESCE(MAX(copy_number), 0) + 1 AS n FROM item_copies WHERE item_id = ?",
        (item_id,),
    ).fetchone()["n"]
    cursor = db.execute(
        "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
        "VALUES (?, ?, ?, 1)",
        (item_id, next_number, location_id),
    )
    return cursor.lastrowid


def copies_for_item(db, item_id: int):
    """Return physical copies in stable user-facing order."""
    install_schema(db)
    return db.execute(
        "SELECT c.*, l.name AS location_name FROM item_copies c "
        "LEFT JOIN locations l ON l.id = c.location_id "
        "WHERE c.item_id = ? ORDER BY c.copy_number, c.id",
        (item_id,),
    ).fetchall()
