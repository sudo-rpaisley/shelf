"""Upgrade the older fork's private-state layout, not just upstream databases.

The frozen DDL/ledger below come from 5d94d2e and 6a46f467 (the 0.37/0.39
fork). Do not bootstrap this fixture with current MIGRATION_TABLES: that hides
both the reused migration numbers and the incompatible user_item_state table.
"""

import json
import re
import sqlite3

import pytest

from app import database
from app.services import personal_library
from tests.conftest import _redirect_stale_path_constants


_LEGACY_TABLES = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'viewer',
    token_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE user_item_state (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    reading_status TEXT CHECK(reading_status IS NULL OR reading_status IN ('want_to_read','reading','read')),
    date_started TEXT,
    date_finished TEXT,
    rating INTEGER CHECK(rating IS NULL OR (rating >= 1 AND rating <= 5)),
    wishlist INTEGER NOT NULL DEFAULT 0 CHECK(wishlist IN (0,1)),
    favourite INTEGER NOT NULL DEFAULT 0 CHECK(favourite IN (0,1)),
    personal_notes TEXT,
    progress_value REAL,
    progress_total REAL,
    progress_unit TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, item_id)
);
CREATE INDEX idx_user_item_state_status ON user_item_state(user_id, reading_status);
CREATE TABLE user_reading_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    date_started TEXT,
    date_finished TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
ALTER TABLE items ADD COLUMN cover_review_dismissed INTEGER NOT NULL DEFAULT 0;
"""
_LEGACY_LEDGER = (
    (32, "Add per-user item state table"),
    (33, "Index per-user item state by item"),
    (34, "Index per-user consumption status"),
    (35, "Index per-user wishlist"),
    (36, "Index per-user favourites"),
    (37, "Add per-user reading history table"),
    (38, "Index per-user reading history"),
    (39, "Snapshot legacy item state for existing users"),
    (40, "Snapshot legacy reading history for existing users"),
    (41, "Add Shelf libraries"),
    (42, "Add per-library user memberships"),
    (43, "Add one-library-per-item mapping"),
    (44, "Create default Main Library"),
    (45, "Assign existing catalogue items to Main Library"),
    (46, "Seed Main Library memberships from existing roles"),
    (47, "Index library memberships by user"),
    (48, "Index catalogue items by library"),
    (49, "Add external user identities"),
    (50, "Index external user identities by user"),
    (51, "Add library-scoped collections"),
    (52, "Add collection item memberships"),
    (53, "Index collection memberships by item"),
    (54, "Index collections by library"),
    (55, "Add durable cover-review dismissal"),
)
_OLD_FIELDS = (
    "user_id", "item_id", "reading_status", "date_started", "date_finished",
    "rating", "wishlist", "favourite", "personal_notes", "progress_value",
    "progress_total", "progress_unit", "created_at", "updated_at",
)


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


@pytest.fixture
def legacy_fork(tmp_path, monkeypatch):
    from app import config

    path = tmp_path / "legacy-fork.db"
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    _redirect_stale_path_constants(monkeypatch, config.DATA_DIR, path, config.COVERS_DIR)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(database.SCHEMA)
        # The common upstream 0.37 foundation is append-only. Everything
        # above 31 is deliberately frozen, not taken from current bootstrap.
        for version, description, sql in database.MIGRATIONS:
            if version > 31:
                continue
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as exc:
                assert database._is_benign_migration_error(version, exc)
            conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                         (version, description))
        conn.executescript(_LEGACY_TABLES)
        conn.executemany("INSERT INTO schema_version (version, description) VALUES (?, ?)", _LEGACY_LEDGER)
        conn.execute("INSERT INTO users (username, password) VALUES ('first', 'unused')")
        conn.execute("INSERT INTO users (username, password) VALUES ('second', 'unused')")
        conn.execute("INSERT INTO items (title, reading_status) VALUES ('Legacy novel', 'read')")
        conn.execute("INSERT INTO items (title, owned) VALUES ('Legacy plan', 0)")
        conn.execute("INSERT INTO items (title) VALUES ('Legacy finish')")
        conn.executemany(
            "INSERT INTO user_item_state (user_id, item_id, reading_status, rating, wishlist, favourite, "
            "personal_notes, progress_value, progress_total, progress_unit, date_started, date_finished, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (1, 1, "reading", 4, 0, 1, "first-private-note", 90, 300, "pages", "2026-08-01", None,
                 "2026-08-01 10:00:00", "2026-08-10 12:00:00"),
                (2, 1, "reading", 2, 1, 0, "second-private-note", 75, None, "percent", None, None,
                 "2026-08-02 10:00:00", "2026-08-11 12:00:00"),
                (1, 2, "want_to_read", None, 1, 0, None, None, None, None, None, None,
                 "2026-08-03 10:00:00", "2026-08-12 12:00:00"),
                (1, 3, "read", 5, 0, 0, "long legacy note: " + "n" * 6000, None, None, None,
                 "2026-08-04", "2026-08-13", "2026-08-04 10:00:00", "2026-08-13 12:00:00"),
            ),
        )
        conn.execute("INSERT INTO user_reading_log (user_id, item_id, status, notes) "
                     "VALUES (1, 3, 'read', 'private historical note')")
    yield path


def test_old_fork_boots_and_maps_private_state_without_changing_originals(legacy_fork):
    with database.get_db() as conn:
        assert "status" not in _columns(conn, "user_item_state")
        old_rows = [tuple(row) for row in conn.execute(
            f"SELECT {', '.join(_OLD_FIELDS)} FROM user_item_state ORDER BY user_id, item_id")]
        old_history = [tuple(row) for row in conn.execute("SELECT * FROM user_reading_log")]

    database.init_db()
    with database.get_db() as conn:
        assert [tuple(row) for row in conn.execute(
            f"SELECT {', '.join(_OLD_FIELDS)} FROM user_item_state ORDER BY user_id, item_id")] == old_rows
        assert [tuple(row) for row in conn.execute("SELECT * FROM user_reading_log")] == old_history
        assert [tuple(row) for row in conn.execute(
            "SELECT user_id, item_id, status, progress, on_list FROM user_item_state ORDER BY user_id, item_id"
        )] == [(1, 1, "in_progress", 30, 0), (1, 2, "planned", None, 1),
               (1, 3, "finished", 100, 0), (2, 1, "in_progress", 75, 1)]
        first = personal_library.state_for(conn, 1, 1)
        assert (first["notes"], first["started_at"], first["finished_at"], first["last_event_id"]) == (
            "first-private-note", "2026-08-01", None, 0)
        assert personal_library.state_for(conn, 1, 3)["notes"] == "long legacy note: " + "n" * 6000
        assert personal_library.state_for(conn, 1, 2)["notes"] == ""
        assert conn.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_colliding_ledger_does_not_skip_current_columns_or_seeds(legacy_fork):
    database.init_db()
    with database.get_db() as conn:
        for _version, _description, sql in database.MIGRATIONS:
            match = re.search(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", sql)
            if match:
                assert match[2] in _columns(conn, match[1])
        assert [tuple(row) for row in conn.execute(
            "SELECT version, description FROM schema_version ORDER BY version"
        )] == [(version, description) for version, description, _sql in database.MIGRATIONS]
        assert conn.execute("SELECT id FROM lists WHERE slug = 'wishlist'").fetchone()
        assert conn.execute("SELECT item_id FROM list_items").fetchone()[0] == 2
        assert conn.execute("SELECT value FROM settings WHERE key = 'feature.api'").fetchone()[0] == "0"
        archived = json.loads(conn.execute(
            "SELECT value FROM settings WHERE key = 'connected_library_legacy_migrations_v1'"
        ).fetchone()[0])
        assert [(row["version"], row["description"]) for row in archived] == list(_LEGACY_LEDGER)
        assert all(row["applied_at"] for row in archived)


def test_failed_original_startup_can_be_retried(legacy_fork):
    with database.get_db() as conn:
        for version, description, sql in database.MIGRATIONS:
            if version in (56, 57, 58, 59):
                conn.execute(sql)
                conn.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                             (version, description))
        conn.commit()
        with pytest.raises(sqlite3.OperationalError, match="no such column: status"):
            conn.executescript(database.MIGRATION_TABLES)
    database.init_db()
    database.init_db()
    with database.get_db() as conn:
        assert personal_library.state_for(conn, 1, 1)["status"] == "in_progress"
        assert "deleted_at" in _columns(conn, "items")
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_restart_never_replays_legacy_values_over_new_personal_edits(legacy_fork):
    database.init_db()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        personal_library.save(conn, 1, 1, personal_library.parse_form({
            "status": "paused", "progress": "42", "notes": "new private note",
        }))
    database.init_db()
    with database.get_db() as conn:
        state = personal_library.state_for(conn, 1, 1)
        assert (state["status"], state["progress"], state["notes"], state["favourite"]) == (
            "paused", 42, "new private note", 0)
        assert state["last_event_id"] > 0
        assert state["reading_status"] == "reading" and state["personal_notes"] == "first-private-note"
        assert conn.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 1
        assert database._run_migrations(conn) == []


def test_upgraded_routes_and_real_startup_keep_accounts_private(legacy_fork, client):
    from app.auth import create_token

    with client:
        assert client.get("/health").status_code == 200
        client.cookies.set("access_token", create_token(1, "first", "viewer", "First"))
        first = client.get("/api/library/items/1/personal")
        assert first.status_code == 200 and "first-private-note" in first.text
        assert "second-private-note" not in first.text
        assert client.get("/library").status_code == 200
        assert client.get("/activity").status_code == 200
        client.cookies.set("access_token", create_token(2, "second", "viewer", "Second"))
        second = client.get("/api/library/items/1/personal")
        assert second.status_code == 200 and "second-private-note" in second.text
        assert "first-private-note" not in second.text


@pytest.mark.parametrize("value,total,unit,expected", [
    (1, 3, "chapters", 33), (0, 300, "pages", 0), (300, 300, "pages", 100),
    (29, 100, "pages", 29), (1e308, 1e308, "bytes", 100),
    (42.9, None, "percent", 42), (42, None, "%", 42),
    (42, None, "pages", None), (-1, 100, "pages", None),
    (101, 100, "pages", None), (1, 0, "pages", None),
    ("invalid", 100, "pages", None), (float("inf"), 100, "pages", None),
    (1, float("inf"), "pages", None), (float("inf"), float("inf"), "pages", None),
])
def test_legacy_progress_is_converted_only_with_a_valid_scale(legacy_fork, value, total, unit, expected):
    with database.get_db() as conn:
        conn.execute("UPDATE user_item_state SET progress_value = ?, progress_total = ?, progress_unit = ? "
                     "WHERE user_id = 1 AND item_id = 1", (value, total, unit))
    database.init_db()
    with database.get_db() as conn:
        assert personal_library.state_for(conn, 1, 1)["progress"] == expected


def test_bridge_rolls_back_schema_projection_and_ledger_together(legacy_fork):
    class FailBeforeNotes:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=()):
            if sql.startswith("ALTER TABLE user_item_state ADD COLUMN notes "):
                raise sqlite3.OperationalError("simulated interrupted bridge")
            return self.conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    with database.get_db() as conn:
        with pytest.raises(sqlite3.OperationalError, match="simulated interrupted bridge"):
            database._run_migrations(FailBeforeNotes(conn))
        assert "status" not in _columns(conn, "user_item_state")
        assert "progress" not in _columns(conn, "user_item_state")
        assert conn.execute("SELECT description FROM schema_version WHERE version = 32").fetchone()[0] == _LEGACY_LEDGER[0][1]
        assert conn.execute("SELECT value FROM settings WHERE key = 'connected_library_legacy_migrations_v1'").fetchone() is None
        assert conn.execute("SELECT personal_notes FROM user_item_state WHERE user_id = 1 AND item_id = 1").fetchone()[0] == "first-private-note"
    database.init_db()
    with database.get_db() as conn:
        assert personal_library.state_for(conn, 1, 1)["status"] == "in_progress"


def test_overlapping_startups_recheck_the_bridge_under_the_lock(legacy_fork):
    class LetOtherStartupFinish:
        def __init__(self, conn, other):
            self.conn, self.other, self.fired = conn, other, False

        def execute(self, sql, params=()):
            cursor = self.conn.execute(sql, params)
            if sql == "PRAGMA table_info(user_item_state)" and not self.fired:
                self.fired = True
                old_columns = cursor.fetchall()
                database._run_migrations(self.other)
                self.other.commit()
                return old_columns
            return cursor

        def __getattr__(self, name):
            return getattr(self.conn, name)

    with database.get_db() as conn, database.get_db() as other:
        assert database._run_migrations(LetOtherStartupFinish(conn, other)) == []
        assert personal_library.state_for(conn, 1, 1)["progress"] == 30
        archived = json.loads(conn.execute(
            "SELECT value FROM settings WHERE key = 'connected_library_legacy_migrations_v1'"
        ).fetchone()[0])
        assert len(archived) == len(_LEGACY_LEDGER)


def test_partially_extended_legacy_table_does_not_overwrite_present_new_fields(legacy_fork):
    with database.get_db() as conn:
        conn.execute("ALTER TABLE user_item_state ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE user_item_state ADD COLUMN status TEXT")
        conn.execute("UPDATE user_item_state SET notes = 'newer note', status = 'paused' WHERE user_id = 1 AND item_id = 1")
    database.init_db()
    with database.get_db() as conn:
        state = personal_library.state_for(conn, 1, 1)
        assert (state["notes"], state["status"], state["progress"]) == ("newer note", "paused", 30)


@pytest.mark.parametrize("archived", ["not-json", "{}"])
def test_malformed_archived_ledger_is_not_overwritten(legacy_fork, archived):
    with database.get_db() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES ('connected_library_legacy_migrations_v1', ?)", (archived,))
    with pytest.raises((ValueError, sqlite3.OperationalError)):
        database.init_db()
    with database.get_db() as conn:
        assert "status" not in _columns(conn, "user_item_state")
        assert conn.execute("SELECT value FROM settings WHERE key = 'connected_library_legacy_migrations_v1'").fetchone()[0] == archived
        assert conn.execute("SELECT description FROM schema_version WHERE version = 32").fetchone()[0] == _LEGACY_LEDGER[0][1]


def test_unknown_migration_namespace_is_not_guessed_or_rewritten(legacy_fork):
    with database.get_db() as conn:
        conn.execute("UPDATE schema_version SET description = 'Unrecognized fork migration' WHERE version = 32")
    with pytest.raises(sqlite3.OperationalError, match="Unrecognized legacy migration namespace"):
        database.init_db()
    with database.get_db() as conn:
        assert "status" not in _columns(conn, "user_item_state")
        assert conn.execute("SELECT description FROM schema_version WHERE version = 32").fetchone()[0] == "Unrecognized fork migration"


def test_upgraded_legacy_table_does_not_weaken_strict_migration_errors(legacy_fork, monkeypatch):
    database.init_db()
    monkeypatch.setattr(database, "MIGRATIONS", database.MIGRATIONS + (
        (99, "Broken duplicate-column migration", "ALTER TABLE items ADD COLUMN title TEXT"),
    ))
    with pytest.raises(sqlite3.OperationalError, match="duplicate column name: title"):
        database.init_db()
    with database.get_db() as conn:
        assert conn.execute("SELECT 1 FROM schema_version WHERE version = 99").fetchone() is None


@pytest.mark.parametrize("before_upgrade", [False, True])
def test_real_restore_preserves_legacy_fields_and_reapplies_bridge_if_needed(
    legacy_fork, admin_client, tmp_path, before_upgrade,
):
    if not before_upgrade:
        database.init_db()
    backup_path = tmp_path / "legacy-backup.db"
    with database.get_db() as conn:
        conn.execute("VACUUM INTO ?", (str(backup_path),))
    database.init_db()
    with database.get_db() as conn:
        conn.execute("UPDATE user_item_state SET notes = 'changed', personal_notes = 'changed' "
                     "WHERE user_id = 1 AND item_id = 1")
        conn.execute("DELETE FROM user_reading_log")

    restored = admin_client.post("/api/settings/restore", files={
        "file": ("legacy-backup.db", backup_path.read_bytes(), "application/octet-stream"),
    })
    assert restored.status_code == 200 and restored.json()["ok"] is True
    assert admin_client.get("/activity", follow_redirects=False).status_code == 303
    with database.get_db() as conn:
        state = personal_library.state_for(conn, 1, 1)
        assert (state["status"], state["progress"], state["notes"], state["personal_notes"]) == (
            "in_progress", 30, "first-private-note", "first-private-note")
        assert conn.execute("SELECT notes FROM user_reading_log").fetchone()[0] == "private historical note"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        archived = json.loads(conn.execute(
            "SELECT value FROM settings WHERE key = 'connected_library_legacy_migrations_v1'"
        ).fetchone()[0])
        assert len(archived) == len(_LEGACY_LEDGER)
