"""Physical-copy, digital-holding and hierarchical-location foundations.

These tables are deliberately additive and trigger-free.  Shelf's historical
``MIGRATION_TABLES`` script is also executed directly by legacy-schema tests,
so making that script reference newer provider columns couples unrelated
migrations together.  Instead this focused service installs its own idempotent
schema on first use and projects the existing flat/provider fields into it in
application code.

The result is safe for existing databases and for Shelf's hardened restore
path: there are no SQLite triggers, views or virtual tables to whitelist.
"""

from app.media_types import is_physical_media


_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS location_nodes (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_id          INTEGER REFERENCES location_nodes(id) ON DELETE RESTRICT,
        name               TEXT NOT NULL,
        sort_order         INTEGER NOT NULL DEFAULT 0,
        legacy_location_id INTEGER UNIQUE REFERENCES locations(id) ON DELETE SET NULL,
        created_at         TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_location_nodes_root_name
       ON location_nodes(name COLLATE NOCASE) WHERE parent_id IS NULL""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_location_nodes_child_name
       ON location_nodes(parent_id, name COLLATE NOCASE) WHERE parent_id IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS idx_location_nodes_parent
       ON location_nodes(parent_id, sort_order, name COLLATE NOCASE)""",
    """CREATE TABLE IF NOT EXISTS item_copies (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id           INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        copy_number       INTEGER NOT NULL DEFAULT 1,
        location_id       INTEGER REFERENCES location_nodes(id) ON DELETE SET NULL,
        position_order    INTEGER,
        condition         TEXT,
        notes             TEXT,
        acquired_date     TEXT,
        acquisition_price REAL,
        copy_barcode      TEXT UNIQUE,
        is_primary        INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
        created_at        TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(item_id, copy_number)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_item_copies_item ON item_copies(item_id)",
    """CREATE INDEX IF NOT EXISTS idx_item_copies_location
       ON item_copies(location_id, position_order, id)""",
    """CREATE TABLE IF NOT EXISTS digital_holdings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        provider    TEXT NOT NULL,
        external_id TEXT NOT NULL,
        library_id  TEXT,
        format      TEXT,
        source_url  TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(item_id, provider, external_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_digital_holdings_item ON digital_holdings(item_id)",
    """CREATE INDEX IF NOT EXISTS idx_digital_holdings_provider
       ON digital_holdings(provider, external_id)""",
    """CREATE TABLE IF NOT EXISTS periodical_publications (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL,
        issn       TEXT UNIQUE COLLATE NOCASE,
        publisher  TEXT,
        language   TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE INDEX IF NOT EXISTS idx_periodical_publications_title
       ON periodical_publications(title COLLATE NOCASE)""",
    """CREATE TABLE IF NOT EXISTS periodical_issues (
        item_id             INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
        publication_id      INTEGER NOT NULL REFERENCES periodical_publications(id) ON DELETE CASCADE,
        volume              TEXT,
        issue_number        TEXT,
        issue_date          TEXT,
        barcode_supplement  TEXT,
        cover_date_label    TEXT,
        created_at          TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE INDEX IF NOT EXISTS idx_periodical_issues_publication
       ON periodical_issues(publication_id, issue_date, issue_number)""",
)

_REQUIRED_TABLES = frozenset({
    "location_nodes",
    "item_copies",
    "digital_holdings",
    "periodical_publications",
    "periodical_issues",
})
_PROVIDER_FIELDS = (
    ("audiobookshelf", "abs_id", "abs_library_id"),
    ("komga", "komga_id", "komga_library_id"),
    ("romm", "romm_id", "romm_platform_id"),
)


def install_schema(db) -> None:
    """Install the additive holdings schema without committing caller work."""
    for statement in _SCHEMA_STATEMENTS:
        db.execute(statement)


def schema_available(db) -> bool:
    """Whether this connection already has the complete holdings schema."""
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('location_nodes', 'item_copies', 'digital_holdings', "
        "'periodical_publications', 'periodical_issues')"
    ).fetchall()
    return {row["name"] for row in rows} == _REQUIRED_TABLES


def ensure_legacy_location_nodes(db) -> None:
    """Project legacy flat locations to root nodes, idempotently."""
    install_schema(db)
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
    # Project only fields actually present on the live row.
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
    install_schema(db)
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
        elif copy["legacy_location_id"] is not None and copy["location_id"] != root_id:
            # A compatibility root may follow an old-style move. Child/shelf
            # nodes have no legacy id and are deliberately preserved.
            db.execute(
                "UPDATE item_copies SET location_id = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (root_id, copy["id"]),
            )

    for provider, id_key, library_key in _PROVIDER_FIELDS:
        _sync_provider(db, item, provider, id_key, library_key)


def sync_all_holdings(db) -> None:
    """Idempotently install/backfill compatibility projections for all items."""
    install_schema(db)
    ensure_legacy_location_nodes(db)
    item_ids = [row["id"] for row in db.execute("SELECT id FROM items").fetchall()]
    for item_id in item_ids:
        sync_item_holding(db, item_id)
