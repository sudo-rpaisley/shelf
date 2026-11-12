"""Additive connected-library schema, upgrade safety and feature profiles."""

import sqlite3

from app import database


CONNECTED_TABLES = {
    "source_instances", "source_records", "user_item_state", "user_item_events",
    "library_saved_views", "connection_dismissals", "item_metadata_provenance",
    "cover_sync_blocks",
}


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_connected_tables_exist_on_fresh_boot(db):
    assert CONNECTED_TABLES <= _tables(db)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    for table in CONNECTED_TABLES:
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_existing_database_upgrades_without_inventing_personal_or_source_data(tmp_path, monkeypatch):
    """Build the actual pre-feature auxiliary schema, not current DDL (G98)."""
    path = tmp_path / "legacy-connected.db"
    monkeypatch.setattr(database, "DATABASE_PATH", path)
    old_migrations = tuple(entry for entry in database.MIGRATIONS if entry[0] < 58)
    old_tables = database.MIGRATION_TABLES.split("-- Connected-library tables", 1)[0]
    with monkeypatch.context() as old:
        old.setattr(database, "MIGRATIONS", old_migrations)
        old.setattr(database, "MIGRATION_TABLES", old_tables)
        database.init_db()
    with database.get_db() as conn:
        assert not (_tables(conn) & CONNECTED_TABLES)
        conn.execute("INSERT INTO items (title, media_type, reading_status) VALUES ('Legacy novel', 'book', 'read')")
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO reading_log (item_id, status) VALUES (?, 'read')", (item_id,))

    database.init_db()
    database.init_db()
    with database.get_db() as conn:
        assert CONNECTED_TABLES <= _tables(conn)
        assert conn.execute("SELECT title, reading_status FROM items_live WHERE id = ?", (item_id,)).fetchone()[:] == ("Legacy novel", "read")
        assert conn.execute("SELECT COUNT(*) FROM reading_log WHERE item_id = ?", (item_id,)).fetchone()[0] == 1
        for table in CONNECTED_TABLES:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_minimal_upgrade_does_not_enable_new_surfaces(db):
    for key in ("lending", "series", "stats", "store", "music", "periodicals", "shelf_fill"):
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '0')", ("feature." + key,))
    _, _, sql = next(entry for entry in database.MIGRATIONS if entry[0] == 58)
    db.execute(sql)
    assert db.execute("SELECT value FROM settings WHERE key = 'feature.connected_library'").fetchone()[0] == "0"


def test_upgrade_preserves_existing_connected_library_choice(db):
    db.execute("INSERT INTO settings (key, value) VALUES ('feature.connected_library', '1')")
    for key in ("lending", "series", "stats", "store", "music", "periodicals", "shelf_fill"):
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '0')", ("feature." + key,))
    _, _, sql = next(entry for entry in database.MIGRATIONS if entry[0] == 58)
    db.execute(sql)
    assert db.execute("SELECT value FROM settings WHERE key = 'feature.connected_library'").fetchone()[0] == "1"


def test_source_external_identity_is_scoped_to_instance(db):
    from tests.conftest import _insert_item
    item_id = _insert_item(db, isbn=None)
    for name in ("First server", "Second server"):
        source_id = db.execute("INSERT INTO source_instances (provider, name, base_url) VALUES ('opds', ?, 'http://books.local/opds')", (name,)).lastrowid
        db.execute("INSERT INTO source_records (source_id, item_id, external_id, title, media_type) VALUES (?, ?, 'same-id', 'Novel', 'ebook')", (source_id, item_id))
    assert db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 2
    try:
        db.execute("INSERT INTO source_records (source_id, item_id, external_id, title, media_type) VALUES (?, ?, 'same-id', 'Novel', 'ebook')", (source_id, item_id))
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("one source must not contain an external identity twice")
