import json
import sqlite3

import pytest

from app.services import fork_upgrade


def _legacy_db(*, include_provider_targets: bool = True):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT DEFAULT (datetime('now'))
        );
        INSERT INTO schema_version (version, description)
        VALUES (24, 'Add komga_id column'),
               (25, 'Add komga_library_id column'),
               (26, 'Add komga_series_id column'),
               (27, 'Index Komga item IDs'),
               (28, 'Add romm_id column'),
               (29, 'Add romm_platform_id column'),
               (30, 'Index RomM item IDs'),
               (31, 'Add Discogs master ID'),
               (32, 'Add Discogs label'),
               (46, 'Add confirmed legacy book barcode mappings'),
               (48, 'Add per-user item state table'),
               (64, 'Index catalogue items by library');

        CREATE TABLE locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            sort_order INTEGER DEFAULT 0
        );
        INSERT INTO locations (id, name, sort_order) VALUES (1, 'Living Room', 0);

        CREATE TABLE items (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            media_type TEXT NOT NULL,
            owned INTEGER NOT NULL DEFAULT 1,
            location_id INTEGER REFERENCES locations(id),
            platform TEXT,
            romm_id TEXT,
            romm_platform_id TEXT,
            komga_id TEXT,
            komga_library_id TEXT,
            komga_series_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE location_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER REFERENCES location_nodes(id) ON DELETE RESTRICT,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            legacy_location_id INTEGER UNIQUE REFERENCES locations(id) ON DELETE SET NULL
        );
        INSERT INTO location_nodes (id, parent_id, name, sort_order, legacy_location_id)
        VALUES (10, NULL, 'Living Room', 0, 1),
               (11, 10, 'Bookcase', 0, NULL),
               (12, 11, 'Shelf 1', 0, NULL);

        CREATE TABLE item_copies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            copy_number INTEGER NOT NULL DEFAULT 1,
            location_id INTEGER REFERENCES location_nodes(id) ON DELETE SET NULL,
            position_order INTEGER,
            condition TEXT,
            notes TEXT,
            acquired_date TEXT,
            acquisition_source TEXT,
            acquisition_price REAL,
            provenance TEXT,
            copy_barcode TEXT UNIQUE,
            is_primary INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(item_id, copy_number)
        );

        INSERT INTO items
            (id, title, media_type, owned, location_id)
        VALUES (1, 'Physical Book', 'book', 1, 1);
        INSERT INTO item_copies
            (id, item_id, copy_number, location_id, position_order, condition,
             notes, acquired_date, acquisition_source, acquisition_price,
             provenance, copy_barcode, is_primary)
        VALUES (100, 1, 1, 12, 7, 'good', 'Signed copy', '2026-01-02',
                'Charity shop', 4.50, 'Paisley collection', 'COPY-100', 1);

        INSERT INTO items
            (id, title, media_type, owned, komga_id, komga_library_id, komga_series_id)
        VALUES (2, 'Manga Volume', 'digital_manga', 1, 'k-2', 'lib-manga', 'series-9');
        INSERT INTO settings (key, value)
        VALUES ('komga_library_media_types', '{"lib-manga":"digital_manga"}');

        INSERT INTO items
            (id, title, media_type, owned, platform, romm_id, romm_platform_id)
        VALUES (3, 'Game', 'digital_game', 1, 'snes', 'rom-3', 'snes');

        CREATE TABLE digital_holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            external_id TEXT NOT NULL,
            library_id TEXT
        );
        INSERT INTO digital_holdings (item_id, provider, external_id, library_id)
        VALUES (2, 'komga', 'k-2', 'lib-manga'),
               (3, 'romm', 'rom-3', 'snes');

        CREATE TABLE periodical_issues (
            item_id INTEGER PRIMARY KEY,
            publication_id INTEGER NOT NULL,
            barcode_supplement TEXT
        );
        """
    )

    if include_provider_targets:
        db.executescript(
            """
            CREATE TABLE romm_records (
                romm_id TEXT PRIMARY KEY,
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                platform_id TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE komga_records (
                komga_id TEXT PRIMARY KEY,
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                library_id TEXT NOT NULL,
                series_id TEXT,
                kind TEXT NOT NULL CHECK(kind IN ('comic','manga')),
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
    return db


def test_pre_037_bridge_preserves_locations_copies_and_provider_identity():
    db = _legacy_db()

    logs = fork_upgrade.upgrade_pre_037_fork(db)

    assert logs
    marker = db.execute(
        "SELECT value FROM settings WHERE key = 'sudo_fork_037_schema_bridge_v1'"
    ).fetchone()
    assert marker["value"] == "1"

    shelf = db.execute(
        "SELECT id, name, label, parent_id FROM locations WHERE label = 'Shelf 1'"
    ).fetchone()
    assert shelf is not None
    assert shelf["name"] == "Living Room / Bookcase / Shelf 1"

    copy = db.execute(
        "SELECT location_id, position_order, condition, notes, acquired_date, "
        "acquisition_source, acquisition_price, provenance, copy_barcode "
        "FROM item_copies WHERE id = 100"
    ).fetchone()
    assert copy["location_id"] == shelf["id"]
    assert copy["position_order"] == 7
    assert copy["condition"] == "good"
    assert copy["notes"] == "Signed copy"
    assert copy["acquired_date"] == "2026-01-02"
    assert copy["acquisition_source"] == "Charity shop"
    assert copy["acquisition_price"] == 4.50
    assert copy["provenance"] == "Paisley collection"
    assert copy["copy_barcode"] == "COPY-100"
    assert db.execute("SELECT location_id FROM items WHERE id = 1").fetchone()[0] == shelf["id"]

    location_fk = next(
        row for row in db.execute("PRAGMA foreign_key_list(item_copies)").fetchall()
        if row["from"] == "location_id"
    )
    assert location_fk["table"] == "locations"

    romm = db.execute(
        "SELECT item_id, platform_id FROM romm_records WHERE romm_id = 'rom-3'"
    ).fetchone()
    assert tuple(romm) == (3, "snes")

    komga = db.execute(
        "SELECT item_id, library_id, series_id, kind FROM komga_records WHERE komga_id = 'k-2'"
    ).fetchone()
    assert tuple(komga) == (2, "lib-manga", "series-9", "manga")
    assert db.execute("SELECT media_type FROM items WHERE id = 2").fetchone()[0] == "manga"

    periodical_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(periodical_issues)").fetchall()
    }
    assert "barcode_ean" in periodical_columns
    assert db.execute(
        "SELECT description FROM schema_version WHERE version = 24"
    ).fetchone()[0] == "Add confirmed legacy book barcode mappings"

    # The bridge is one-shot and must not duplicate or rewrite data later.
    assert fork_upgrade.upgrade_pre_037_fork(db) == []
    assert db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 1


def test_old_fork_migration_ledger_is_archived_and_future_numbers_are_freed():
    db = _legacy_db()

    fork_upgrade.upgrade_pre_037_fork(db)

    raw = db.execute(
        "SELECT value FROM settings WHERE key = 'sudo_fork_037_migration_ledger_v1'"
    ).fetchone()[0]
    archived = json.loads(raw)
    archived_versions = {entry["version"] for entry in archived}
    assert {24, 31, 32, 46, 48, 64} <= archived_versions

    remaining = {
        row[0] for row in db.execute("SELECT version FROM schema_version").fetchall()
    }
    assert 24 in remaining
    assert 31 in remaining
    assert not ({32, 46, 48, 64} & remaining)


def test_prepare_is_retryable_until_finish_marks_bridge_complete():
    db = _legacy_db()

    assert fork_upgrade.prepare_pre_037_fork(db) is True
    assert fork_upgrade.is_pre_037_fork_database(db) is True
    assert db.execute(
        "SELECT value FROM settings WHERE key = 'sudo_fork_037_schema_bridge_v1'"
    ).fetchone() is None

    # A second startup after an interrupted first one must be harmless.
    assert fork_upgrade.prepare_pre_037_fork(db) is True
    assert db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 1

    logs = fork_upgrade.finish_pre_037_fork(db)
    assert logs
    assert fork_upgrade.is_pre_037_fork_database(db) is False


def test_finish_refuses_to_mark_complete_when_provider_target_is_missing():
    db = _legacy_db(include_provider_targets=False)

    assert fork_upgrade.prepare_pre_037_fork(db) is True
    with pytest.raises(RuntimeError, match="RomM target table is not installed yet"):
        fork_upgrade.finish_pre_037_fork(db)

    assert db.execute(
        "SELECT value FROM settings WHERE key = 'sudo_fork_037_schema_bridge_v1'"
    ).fetchone() is None
    assert fork_upgrade.is_pre_037_fork_database(db) is True


def test_clean_upstream_database_is_ignored():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, description TEXT NOT NULL);
        INSERT INTO schema_version (version, description)
        VALUES (24, 'Add confirmed legacy book barcode mappings');
        """
    )

    assert fork_upgrade.prepare_pre_037_fork(db) is False
    assert fork_upgrade.finish_pre_037_fork(db) == []
    assert fork_upgrade.upgrade_pre_037_fork(db) == []
