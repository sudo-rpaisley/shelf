"""Cornelius's reported pre-0.37 ledger/layout, not the later 0.37 fork.

Versions 24-66 and the copy columns/FK come from the deployment diagnostic.
Location/holding DDL is frozen from 43e244f's holdings.py. Private-state DDL is
shared with the other frozen fixture, not with current application bootstrap.
"""

import json
import sqlite3

import pytest

from app import database
from app.services import personal_library
from tests.conftest import _redirect_stale_path_constants
from tests.test_connected_legacy_upgrade import _LEGACY_TABLES, _OLD_FIELDS


_LEDGER = tuple(enumerate((
    "Add komga_id column", "Add komga_library_id column", "Add komga_series_id column",
    "Index Komga item IDs", "Add romm_id column", "Add romm_platform_id column",
    "Index RomM item IDs", "Add Discogs master ID", "Add Discogs label",
    "Add Discogs catalogue number", "Add Discogs format summary", "Add Discogs genres",
    "Add Discogs styles", "Add Discogs notes", "Add Discogs cache timestamp",
    "Add music identifier source", "Add item-series membership table", "Index item-series names",
    "Backfill primary item-series memberships", "Add collections table",
    "Add collection item membership table", "Index collection item memberships",
    "Add confirmed legacy book barcode mappings", "Add missing-since mark to item copies",
    "Add per-user item state table", "Index per-user item state by item",
    "Index per-user consumption status", "Index per-user wishlist", "Index per-user favourites",
    "Add per-user reading history table", "Index per-user reading history",
    "Snapshot legacy item state for existing users", "Snapshot legacy reading history for existing users",
    "Add Shelf libraries", "Add per-library user memberships", "Add one-library-per-item mapping",
    "Create default Main Library", "Assign existing catalogue items to Main Library",
    "Seed Main Library memberships from existing roles", "Index library memberships by user",
    "Index catalogue items by library", "Add connected-service series metadata",
    "Index connected-service series names",
), start=24))

_HOLDINGS = """
ALTER TABLE items ADD COLUMN komga_id TEXT;
ALTER TABLE items ADD COLUMN komga_library_id TEXT;
ALTER TABLE items ADD COLUMN komga_series_id TEXT;
ALTER TABLE items ADD COLUMN romm_id TEXT;
ALTER TABLE items ADD COLUMN romm_platform_id TEXT;
CREATE TABLE location_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES location_nodes(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    legacy_location_id INTEGER UNIQUE REFERENCES locations(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE item_copies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    copy_number INTEGER NOT NULL DEFAULT 1,
    location_id INTEGER REFERENCES location_nodes(id) ON DELETE SET NULL,
    position_order INTEGER,
    condition TEXT,
    notes TEXT,
    acquired_date TEXT,
    acquisition_price REAL,
    copy_barcode TEXT UNIQUE,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    missing_since TEXT,
    UNIQUE(item_id, copy_number)
);
CREATE INDEX idx_item_copies_location ON item_copies(location_id, position_order, id);
CREATE INDEX legacy_copy_notes_index ON item_copies(notes);
CREATE TABLE digital_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    library_id TEXT,
    format TEXT,
    source_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(item_id, provider, external_id)
);
CREATE TABLE periodical_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
    issn TEXT UNIQUE COLLATE NOCASE, publisher TEXT, language TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE periodical_issues (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    publication_id INTEGER NOT NULL REFERENCES periodical_publications(id) ON DELETE CASCADE,
    volume TEXT, issue_number TEXT, issue_date TEXT, barcode_supplement TEXT, cover_date_label TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE music_releases (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    artist_credit TEXT, musicbrainz_release_id TEXT, musicbrainz_release_group_id TEXT,
    discogs_release_id TEXT, discogs_master_id TEXT, discogs_label TEXT,
    discogs_catalog_number TEXT, discogs_format_summary TEXT, discogs_genres TEXT,
    discogs_styles TEXT, discogs_notes TEXT, discogs_updated_at TEXT,
    release_type TEXT, release_status TEXT, release_date TEXT, first_release_date TEXT,
    country TEXT, label TEXT, catalog_number TEXT, packaging TEXT, media_count INTEGER,
    format_summary TEXT, edition_notes TEXT, media_condition TEXT, packaging_condition TEXT,
    condition_notes TEXT, metadata_source TEXT, metadata_updated_at TEXT
);
CREATE TABLE music_identifiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    identifier_type TEXT NOT NULL, value TEXT NOT NULL, description TEXT,
    source TEXT NOT NULL DEFAULT 'manual', UNIQUE(item_id, identifier_type, value)
);
CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE library_items (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    library_id INTEGER NOT NULL REFERENCES libraries(id)
);
-- Extra old tables must remain untouched, including children of rebuilt copies.
CREATE TABLE legacy_series_metadata (id INTEGER PRIMARY KEY, details TEXT);
CREATE TABLE copy_annotations (
    copy_id INTEGER REFERENCES item_copies(id) ON DELETE CASCADE, note TEXT
);
"""


def _rows(conn, table):
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


@pytest.fixture
def pre037_fork(tmp_path, monkeypatch):
    from app import config

    path = tmp_path / "cornelius-layout.db"
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    _redirect_stale_path_constants(monkeypatch, config.DATA_DIR, path, config.COVERS_DIR)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(database.SCHEMA)
        for version, description, sql in database.MIGRATIONS:
            if version > 23:
                continue
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as exc:
                assert database._is_benign_migration_error(version, exc)
            conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                         (version, description))
        conn.executescript(_LEGACY_TABLES.split("ALTER TABLE items", 1)[0])
        conn.executescript(_HOLDINGS)
        conn.executemany("INSERT INTO schema_version (version, description) VALUES (?, ?)", _LEDGER)
        conn.executescript("""
INSERT INTO users (id, username, password, role) VALUES (1, 'first', 'unused', 'admin'), (2, 'second', 'unused', 'viewer');
INSERT INTO locations (id, name) VALUES (1, 'Living Room');
INSERT INTO location_nodes (id, parent_id, name, legacy_location_id)
VALUES (10, NULL, 'Living Room', 1), (11, 10, 'Bookcase', NULL), (12, 11, 'Shelf 1', NULL);
INSERT INTO items (id, title, location_id, updated_at) VALUES (1, 'Physical novel', 1, '2026-08-01 10:00:00');
INSERT INTO items (id, title, media_type, komga_id, komga_library_id, komga_series_id)
VALUES (2, 'Legacy manga', 'digital_manga', 'k-2', 'manga-library', 'series-2');
INSERT INTO items (id, title, media_type, romm_id, romm_platform_id)
VALUES (3, 'Legacy game', 'digital_game', 'rom-3', 'snes');
INSERT INTO items (id, title, media_type) VALUES (4, 'Legacy album', 'vinyl');
INSERT INTO items (id, title, location_id) VALUES (5, 'Unconverted physical novel', 1);
INSERT INTO items (id, title, media_type) VALUES (6, 'Legacy periodical', 'magazine');
INSERT INTO item_copies (id, item_id, copy_number, location_id, position_order, condition, notes,
                        acquisition_price, copy_barcode, is_primary, missing_since)
VALUES (100, 1, 1, 12, 7, 'good', 'Signed first copy', 4.5, 'COPY-100', 1, '2026-09-01'),
       (101, 1, 2, 11, 3, 'fair', 'Second copy', NULL, 'COPY-101', 0, NULL);
UPDATE sqlite_sequence SET seq = 5000 WHERE name = 'item_copies';
INSERT INTO copy_annotations VALUES (100, 'Preserve child FK and row');
INSERT INTO digital_holdings (item_id, provider, external_id, library_id)
VALUES (2, 'komga', 'k-2', 'manga-library'), (3, 'romm', 'rom-3', 'snes');
INSERT INTO music_releases (item_id, artist_credit, discogs_master_id, discogs_notes, edition_notes)
VALUES (4, 'Legacy artist', 'master-4', 'Preserve pressing notes', 'Original edition');
INSERT INTO music_identifiers (item_id, identifier_type, value, source) VALUES (4, 'barcode', '1234', 'discogs');
INSERT INTO periodical_publications (id, title) VALUES (1, 'Periodical');
INSERT INTO periodical_issues (item_id, publication_id, barcode_supplement) VALUES (6, 1, '05');
INSERT INTO user_item_state (user_id, item_id, reading_status, personal_notes, rating, favourite,
                             progress_value, progress_total, progress_unit, date_started, updated_at)
VALUES (1, 1, 'reading', 'first-private-note', 4, 1, 90, 300, 'pages', '2026-08-01', '2026-08-10 12:00:00'),
       (2, 1, 'want_to_read', 'second-private-note', 2, 0, NULL, NULL, NULL, NULL, '2026-08-11 12:00:00');
INSERT INTO user_reading_log (user_id, item_id, status, notes) VALUES (1, 1, 'read', 'Private history');
INSERT INTO libraries VALUES (1, 'Main Library');
INSERT INTO library_items SELECT id, 1 FROM items;
INSERT INTO legacy_series_metadata VALUES (1, 'Untouched old series metadata');
""")
    return path


def test_reported_pre037_database_upgrades_without_losing_old_data(pre037_fork):
    with database.get_db() as conn:
        preserved_tables = (
            "location_nodes", "digital_holdings", "user_reading_log", "music_releases",
            "music_identifiers", "libraries", "library_items", "legacy_series_metadata", "copy_annotations",
        )
        originals = {table: _rows(conn, table) for table in preserved_tables}
        private = [tuple(row) for row in conn.execute(
            f"SELECT {', '.join(_OLD_FIELDS)} FROM user_item_state ORDER BY user_id, item_id")]
    database.init_db()
    with database.get_db() as conn:
        assert {table: _rows(conn, table) for table in preserved_tables} == originals
        assert [tuple(row) for row in conn.execute(
            f"SELECT {', '.join(_OLD_FIELDS)} FROM user_item_state ORDER BY user_id, item_id")] == private
        leaf = conn.execute("SELECT id, parent_id FROM locations WHERE name = 'Living Room / Bookcase / Shelf 1'").fetchone()
        copy = conn.execute("SELECT * FROM item_copies WHERE id = 100").fetchone()
        assert (copy["location_id"], copy["position_order"], copy["notes"], copy["missing_since"]) == (
            leaf["id"], 7, "Signed first copy", "2026-09-01")
        assert copy["acquisition_price"] == 4.5 and copy["copy_barcode"] == "COPY-100"
        assert copy["acquisition_source"] is None and copy["provenance"] is None
        assert conn.execute("SELECT location_id, updated_at FROM items WHERE id = 1").fetchone()[:] == (
            leaf["id"], "2026-08-01 10:00:00")
        assert next(row for row in conn.execute("PRAGMA foreign_key_list(item_copies)") if row["from"] == "location_id")["table"] == "locations"
        assert next(row for row in conn.execute("PRAGMA foreign_key_list(copy_annotations)"))["table"] == "item_copies"
        assert conn.execute("SELECT COUNT(*) FROM item_copies WHERE item_id = 5").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM item_copies WHERE item_id IN (2, 3)").fetchone()[0] == 0
        assert personal_library.state_for(conn, 1, 1)["progress"] == 30
        assert personal_library.state_for(conn, 2, 1)["status"] == "planned"
        assert conn.execute("SELECT item_id, platform_id FROM romm_records WHERE romm_id = 'rom-3'").fetchone()[:] == (3, "snes")
        assert conn.execute("SELECT item_id, library_id, series_id, kind FROM komga_records WHERE komga_id = 'k-2'").fetchone()[:] == (
            2, "manga-library", "series-2", "manga")
        assert conn.execute("SELECT media_type FROM items WHERE id = 2").fetchone()[0] == "manga"
        assert conn.execute("SELECT media_type FROM items WHERE id = 3").fetchone()[0] == "video_game"
        assert "barcode_ean" in {row[1] for row in conn.execute("PRAGMA table_info(periodical_issues)")}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_reported_migration_ledger_is_archived_and_all_current_versions_are_truthful(pre037_fork):
    database.init_db()
    with database.get_db() as conn:
        actual = [tuple(row) for row in conn.execute("SELECT version, description FROM schema_version ORDER BY version")]
        assert actual == [(version, description) for version, description, sql in database.MIGRATIONS]
        archived = json.loads(conn.execute("SELECT value FROM settings WHERE key = 'connected_library_legacy_migrations_v1'").fetchone()[0])
        assert [(row["version"], row["description"]) for row in archived] == list(_LEDGER)
        assert all(row["applied_at"] for row in archived)
        assert conn.execute("SELECT value FROM settings WHERE key = 'feature.api'").fetchone()[0] == "0"


def test_real_startup_and_catalogue_routes_use_converted_copies_and_private_state(pre037_fork, client):
    from app.auth import create_token

    with client:
        assert client.get("/health").status_code == 200
        client.cookies.set("access_token", create_token(1, "first", "admin", "First"))
        for path in ("/library", "/browse", "/activity", "/item/1", "/music", "/sources", "/settings?tab=library"):
            assert client.get(path).status_code == 200, path
        first = client.get("/api/library/items/1/personal")
        assert "first-private-note" in first.text and "second-private-note" not in first.text
        client.cookies.set("access_token", create_token(2, "second", "viewer", "Second"))
        second = client.get("/api/library/items/1/personal")
        assert "second-private-note" in second.text and "first-private-note" not in second.text


@pytest.mark.parametrize("stage", ["_pre037_copies", "_pre037_provider_records"])
def test_interrupted_bridge_restores_old_schema_rows_ledger_and_connection_pragmas(pre037_fork, monkeypatch, stage):
    original = getattr(database, stage)

    def fail_after_work(*args):
        original(*args)
        raise RuntimeError("simulated bridge interruption")

    with database.get_db() as conn:
        before = {table: _rows(conn, table) for table in (
            "schema_version", "items", "item_copies", "copy_annotations", "locations", "user_item_state",
        )}
        with monkeypatch.context() as patch:
            patch.setattr(database, stage, fail_after_work)
            with pytest.raises(RuntimeError, match="simulated bridge interruption"):
                database._run_migrations(conn)
        assert {table: _rows(conn, table) for table in before} == before
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
        assert next(row for row in conn.execute("PRAGMA foreign_key_list(item_copies)") if row["from"] == "location_id")["table"] == "location_nodes"
        assert conn.execute("SELECT 1 FROM settings WHERE key = 'connected_library_legacy_migrations_v1'").fetchone() is None
    database.init_db()


@pytest.mark.parametrize("mutation", [
    "UPDATE location_nodes SET parent_id = 12 WHERE id = 10",
    "UPDATE item_copies SET acquisition_price = -1 WHERE id = 100",
    "ALTER TABLE item_copies ADD COLUMN custom_extension TEXT",
    "UPDATE schema_version SET description = 'Unknown future migration' WHERE version = 65",
])
def test_unrecognized_or_invalid_originals_are_not_silently_discarded(pre037_fork, mutation):
    with database.get_db() as conn:
        conn.execute(mutation)
    with pytest.raises((sqlite3.OperationalError, sqlite3.IntegrityError)):
        database.init_db()
    with database.get_db() as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 66
        assert next(row for row in conn.execute("PRAGMA foreign_key_list(item_copies)") if row["from"] == "location_id")["table"] == "location_nodes"
        assert conn.execute("SELECT note FROM copy_annotations").fetchone()[0] == "Preserve child FK and row"
        assert conn.execute("SELECT 1 FROM settings WHERE key = 'connected_library_pre037_upgrade_v1'").fetchone() is None


def test_restart_never_replays_old_locations_or_personal_values(pre037_fork):
    from app.services import locations

    database.init_db()
    with database.get_db() as conn:
        leaf = conn.execute("SELECT id, parent_id FROM locations WHERE label = 'Shelf 1'").fetchone()
        locations.update_location(conn, leaf["id"], "Renamed shelf", parent_id=leaf["parent_id"])
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        personal_library.save(conn, 1, 1, personal_library.parse_form({"status": "paused", "notes": "new note"}))
    database.init_db()
    with database.get_db() as conn:
        assert conn.execute("SELECT label FROM locations WHERE id = ?", (leaf["id"],)).fetchone()[0] == "Renamed shelf"
        assert personal_library.state_for(conn, 1, 1)["notes"] == "new note"
        assert len(json.loads(conn.execute("SELECT value FROM settings WHERE key = 'connected_library_legacy_migrations_v1'").fetchone()[0])) == len(_LEDGER)
        assert database._run_migrations(conn) == []


def test_rebuilt_copy_sequence_custom_index_and_live_view_survive(pre037_fork):
    database.init_db()
    with database.get_db() as conn:
        new_id = conn.execute("INSERT INTO item_copies (item_id, copy_number) VALUES (1, 3)").lastrowid
        assert new_id > 5000
        assert conn.execute("SELECT id FROM copies_live WHERE id = ?", (new_id,)).fetchone()[0] == new_id
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name = 'legacy_copy_notes_index'").fetchone()
        assert conn.execute("SELECT note FROM copy_annotations WHERE copy_id = 100").fetchone()[0] == "Preserve child FK and row"


@pytest.mark.parametrize("before_upgrade", [False, True])
def test_restore_of_original_and_converted_databases_preserves_copy_foreign_keys(
    pre037_fork, admin_client, tmp_path, before_upgrade,
):
    if not before_upgrade:
        database.init_db()
    backup_path = tmp_path / "pre037-backup.db"
    with database.get_db() as conn:
        conn.execute("VACUUM INTO ?", (str(backup_path),))
    database.init_db()
    with database.get_db() as conn:
        conn.execute("UPDATE user_item_state SET notes = 'changed' WHERE user_id = 1")
        conn.execute("UPDATE item_copies SET notes = 'changed' WHERE id = 100")
    result = admin_client.post("/api/settings/restore", files={
        "file": ("pre037-backup.db", backup_path.read_bytes(), "application/octet-stream"),
    })
    assert result.status_code == 200 and result.json()["ok"] is True
    with database.get_db() as conn:
        assert personal_library.state_for(conn, 1, 1)["notes"] == "first-private-note"
        assert conn.execute("SELECT notes FROM item_copies WHERE id = 100").fetchone()[0] == "Signed first copy"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_provider_library_ids_can_be_recovered_only_from_matching_holdings(pre037_fork):
    with database.get_db() as conn:
        conn.execute("UPDATE items SET komga_library_id = NULL, romm_platform_id = NULL")
    database.init_db()
    with database.get_db() as conn:
        assert conn.execute("SELECT library_id FROM komga_records WHERE komga_id = 'k-2'").fetchone()[0] == "manga-library"
        assert conn.execute("SELECT platform_id FROM romm_records WHERE romm_id = 'rom-3'").fetchone()[0] == "snes"


def test_existing_conflicting_provider_binding_is_not_overwritten(pre037_fork):
    with database.get_db() as conn:
        create = next(sql for sql in database._bootstrap_statements() if sql.startswith("CREATE TABLE IF NOT EXISTS romm_records ("))
        conn.execute(create)
        conn.execute("INSERT INTO romm_records (romm_id, item_id, platform_id) VALUES ('rom-3', 1, 'other-platform')")
    with pytest.raises(sqlite3.IntegrityError, match="Conflicting legacy provider binding"):
        database.init_db()
    with database.get_db() as conn:
        assert conn.execute("SELECT item_id FROM romm_records WHERE romm_id = 'rom-3'").fetchone()[0] == 1
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 66


def test_vocabulary_collision_never_silently_merges_distinct_items(pre037_fork):
    with database.get_db() as conn:
        conn.execute("UPDATE items SET isbn = '9780000000026' WHERE id = 2")
        conn.execute("INSERT INTO items (title, isbn, media_type) VALUES ('Existing manga edition', '9780000000026', 'manga')")
    with pytest.raises(sqlite3.IntegrityError):
        database.init_db()
    with database.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM items WHERE isbn = '9780000000026'").fetchone()[0] == 2
        assert conn.execute("SELECT media_type FROM items WHERE id = 2").fetchone()[0] == "digital_manga"


@pytest.mark.parametrize("converted", [False, True])
def test_upgrade_verifier_uses_only_a_disposable_copy(pre037_fork, converted, capfd):
    from app.verify_upgrade import verify_database

    if converted:
        database.init_db()
    before = {str(path): path.read_bytes() for path in pre037_fork.parent.glob("cornelius-layout.db*")}
    verify_database(pre037_fork)
    after = {str(path): path.read_bytes() for path in pre037_fork.parent.glob("cornelius-layout.db*")}
    assert after == before
    assert "Upgrade verification passed" in capfd.readouterr().out


def test_upgrade_verifier_failure_never_changes_original_database(pre037_fork):
    import subprocess
    from app.verify_upgrade import verify_database

    with database.get_db() as conn:
        conn.execute("UPDATE schema_version SET description = 'Unknown migration' WHERE version = 65")
    before = {str(path): path.read_bytes() for path in pre037_fork.parent.glob("cornelius-layout.db*")}
    with pytest.raises(subprocess.CalledProcessError):
        verify_database(pre037_fork)
    assert {str(path): path.read_bytes() for path in pre037_fork.parent.glob("cornelius-layout.db*")} == before


def test_overlapping_pre037_startups_recheck_namespace_under_write_lock(pre037_fork):
    class OneRow:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class LetOtherStartupFinish:
        def __init__(self, conn, other):
            self.conn, self.other, self.fired = conn, other, False

        def execute(self, sql, params=()):
            cursor = self.conn.execute(sql, params)
            if sql == "SELECT description FROM schema_version WHERE version = 24" and not self.fired:
                self.fired = True
                old_row = cursor.fetchone()
                cursor.close()
                database._run_migrations(self.other)
                self.other.commit()
                return OneRow(old_row)
            return cursor

        def __getattr__(self, name):
            return getattr(self.conn, name)

    with database.get_db() as conn, database.get_db() as other:
        assert database._run_migrations(LetOtherStartupFinish(conn, other)) == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 3
