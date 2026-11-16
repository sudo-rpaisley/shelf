"""Independent PREF-STORE-1 schema oracle and bounded bootstrap/backup proofs.

The literal below is the ratified v2 DDL, NOT derived from database.py. Historical
fixtures never call current init_db and never create/drop the new table to pretend
to predate it. All databases are disposable local files. These tests do not certify
global concurrent cold starts, restore atomicity, or F8 restore/auth fencing.
"""

import asyncio
import io
import json
import queue
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


_PREFERENCES_DDL = """CREATE TABLE IF NOT EXISTS library_preferences (
    user_id INTEGER NOT NULL PRIMARY KEY
        REFERENCES users(id) ON DELETE CASCADE
        CHECK (typeof(user_id) = 'integer'
            AND user_id BETWEEN 1 AND 9223372036854775807),
    values_json TEXT NOT NULL DEFAULT '{}'
        CHECK (typeof(values_json) = 'text'),
    revision INTEGER NOT NULL DEFAULT 1
        CHECK (typeof(revision) = 'integer'
            AND revision BETWEEN 1 AND 9223372036854775807),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        CHECK (typeof(updated_at) = 'text')
);"""
_STORED_SQL = _PREFERENCES_DDL.replace(
    "CREATE TABLE IF NOT EXISTS library_preferences (",
    "CREATE TABLE library_preferences (", 1,
)[:-1]
_MAX = 9_223_372_036_854_775_807
_BAD_STORAGE = (
    ("user_id", 0), ("user_id", -1), ("user_id", 1.5),
    ("user_id", "not-an-id"), ("user_id", 2),  # valid positive id, missing parent
    ("values_json", None), ("values_json", b"{}"),
    ("revision", 0), ("revision", -1), ("revision", 1.5),
    ("revision", "wrong"), ("revision", None), ("updated_at", None),
    ("updated_at", b"not-text"),
)

# Frozen a0bc798a users declaration. Used only for native oracle/disposable old
# schema fixtures, not to extract the new expected schema from production.
_USERS_DDL = """CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password TEXT NOT NULL,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'viewer' CHECK(role IN ('admin','editor','viewer')),
    token_version INTEGER NOT NULL DEFAULT 1,
    language TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);"""


def _native(path, **kwargs):
    conn = sqlite3.connect(str(path), **kwargs)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _schema_rows(conn):
    return tuple(tuple(row) for row in conn.execute(
        "SELECT type, name, tbl_name, sql FROM main.sqlite_master "
        "WHERE name = ? COLLATE NOCASE OR tbl_name = ? COLLATE NOCASE "
        "ORDER BY type, name", ("library_preferences", "library_preferences"),
    ))


def _assert_canonical(conn):
    assert _schema_rows(conn) == (
        ("table", "library_preferences", "library_preferences", _STORED_SQL),
    )
    assert conn.execute("PRAGMA index_list(library_preferences)").fetchall() == []
    assert tuple(tuple(row) for row in conn.execute(
        "PRAGMA table_info(library_preferences)"
    )) == (
        (0, "user_id", "INTEGER", 1, None, 1),
        (1, "values_json", "TEXT", 1, "'{}'", 0),
        (2, "revision", "INTEGER", 1, "1", 0),
        (3, "updated_at", "TEXT", 1, "datetime('now')", 0),
    )
    assert tuple(tuple(row) for row in conn.execute(
        "PRAGMA foreign_key_list(library_preferences)"
    )) == ((0, 0, "users", "user_id", "id", "NO ACTION", "CASCADE", "NONE"),)


def _logical_state(conn):
    """Physical main schema plus every table's logical rows, including ledger.

    File bytes/WAL/sidecars and TEMP views are deliberately not an oracle.
    """
    schema = tuple(tuple(row) for row in conn.execute(
        "SELECT type, name, tbl_name, sql FROM main.sqlite_master ORDER BY type, name"
    ))
    data = []
    for kind, name, _table, _sql in schema:
        if kind == "table":
            quoted = '"' + name.replace('"', '""') + '"'
            rows = tuple(sorted((tuple(row) for row in conn.execute(
                f"SELECT * FROM {quoted}"
            )), key=repr))
            data.append((name, rows))
    return schema, tuple(data)


def _point_database(monkeypatch, path):
    from app import database

    monkeypatch.setattr(database, "DATABASE_PATH", path)
    return database


def _old_database(path):
    """A genuinely pre-preferences, disposable legacy schema.

    SCHEMA is the frozen base kernel (unchanged by this grant). No current
    MIGRATION_TABLES/init_db/private new helper is used to manufacture history.
    The empty old ledger is intentional; only SERIAL upgrade is asserted here.
    """
    from app import database

    conn = _native(path)
    conn.executescript(database.SCHEMA)
    # language was numbered migration 54: do not pre-create it in an old DB.
    conn.executescript(_USERS_DDL.replace("    language TEXT,\n", ""))
    conn.execute("INSERT INTO users (id, username, password, role) "
                 "VALUES (17, 'Old account', 'test-data-not-auth', 'viewer')")
    conn.execute("INSERT INTO items (id, title, media_type) "
                 "VALUES (23, 'Old catalogue sentinel', 'book')")
    conn.execute("INSERT INTO settings (key, value) VALUES ('old.sentinel', '0')")
    conn.execute("INSERT INTO settings (key, value) VALUES ('feature.connected_library', '0')")
    conn.commit()
    assert _schema_rows(conn) == ()
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0
    conn.close()


def _sentinel_database(path):
    conn = _native(path)
    conn.executescript(_USERS_DDL)
    conn.executescript("""
        CREATE TABLE bootstrap_sentinel (id INTEGER PRIMARY KEY, value TEXT);
        INSERT INTO bootstrap_sentinel VALUES (1, 'keep 0 false null');
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY, description TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO schema_version VALUES (700, 'ledger sentinel', '2000-01-01');
    """)
    conn.execute("INSERT INTO users (id, username, password) "
                 "VALUES (17, 'sentinel account', 'test-data-not-auth')")
    conn.commit()
    conn.close()


def _instrument_connections(monkeypatch, database, observe):
    """Observe actual init_db -> existing get_db -> native connection calls.

    The frozen _Connection still owns its callback behavior. No new private
    preference helper name is assumed and get_db itself is never replaced.
    """
    original = sqlite3.connect
    instances = []

    class Observed(database._Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.events = []
            instances.append(self)

        def execute(self, sql, parameters=()):
            self.events.append(("execute", sql, tuple(parameters), self.in_transaction))
            return observe(self, "execute", sql, parameters,
                           lambda: super(Observed, self).execute(sql, parameters))

        def executescript(self, sql):
            self.events.append(("executescript", sql, (), self.in_transaction))
            return observe(self, "executescript", sql, (),
                           lambda: super(Observed, self).executescript(sql))

        def commit(self):
            self.events.append(("commit", "", (), self.in_transaction))
            return super().commit()

        def rollback(self):
            self.events.append(("rollback", "", (), self.in_transaction))
            return super().rollback()

        def close(self):
            self.events.append(("close", "", (), self.in_transaction))
            return super().close()

    def connect(*args, **kwargs):
        kwargs["factory"] = Observed
        return original(*args, **kwargs)

    monkeypatch.setattr(database.sqlite3, "connect", connect)
    return instances, original


def test_oracle_control_literal_ddl_matches_sqlite_and_survives_vacuum(tmp_path):
    """Contract-local native control; not evidence of the absent implementation."""
    conn = _native(tmp_path / "literal.db")
    try:
        conn.executescript(_USERS_DDL + _PREFERENCES_DDL)
        _assert_canonical(conn)
        conn.execute("INSERT INTO users (id, username, password) VALUES (1, 'a', 'data')")
        conn.execute("INSERT INTO library_preferences (user_id) VALUES (1)")
        conn.commit()
        row = conn.execute("SELECT values_json, revision, typeof(updated_at) "
                           "FROM library_preferences").fetchone()
        assert tuple(row) == ("{}", 1, "text")
        target = tmp_path / "literal-vacuum.db"
        conn.execute("VACUUM INTO ?", (str(target),))
        backup = _native(target)
        try:
            _assert_canonical(backup)
            assert _logical_state(backup) == _logical_state(conn)
        finally:
            backup.close()
        conn.execute("DELETE FROM users WHERE id=1")
        assert conn.execute("SELECT * FROM library_preferences").fetchall() == []
    finally:
        conn.close()


@pytest.mark.parametrize("column,value", _BAD_STORAGE)
def test_oracle_control_native_constraints_reject_bad_storage(tmp_path, column, value):
    conn = _native(tmp_path / "constraints.db")
    try:
        conn.executescript(_USERS_DDL + _PREFERENCES_DDL)
        conn.execute("INSERT INTO users (id, username, password) VALUES (1, 'a', 'data')")
        values = {"user_id": 1, "values_json": "{}", "revision": 1, "updated_at": "raw"}
        values[column] = value
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO library_preferences "
                         "(user_id, values_json, revision, updated_at) VALUES (?, ?, ?, ?)",
                         tuple(values.values()))
        assert conn.execute("SELECT * FROM library_preferences").fetchall() == []
    finally:
        conn.close()


def test_oracle_control_native_rowid_null_assignment_and_sqlite_affinity_are_not_python_validation(tmp_path):
    conn = _native(tmp_path / "rowid-affinity.db")
    try:
        conn.executescript(_USERS_DDL + _PREFERENCES_DDL)
        conn.execute("INSERT INTO users (id,username,password) VALUES (1,'a','data')")
        # SQLite's INTEGER PRIMARY KEY assigns a rowid for a submitted SQL NULL;
        # a row storing SQL NULL is NOT created. Affinity also converts these
        # numeric inputs. Domain actor/revision/field validation stays strict.
        conn.execute("INSERT INTO library_preferences (user_id,values_json,revision,updated_at) "
                     "VALUES (NULL, ?, ?, ?)", (123, "1", 456))
        assert tuple(conn.execute(
            "SELECT user_id, typeof(user_id), values_json, typeof(values_json), "
            "revision, typeof(revision), updated_at, typeof(updated_at) FROM library_preferences"
        ).fetchone()) == (1, "integer", "123", "text", 1, "integer", "456", "text")
        _assert_canonical(conn)
    finally:
        conn.close()


def test_oracle_control_disposable_historical_fixture_really_omits_preferences(tmp_path):
    path = tmp_path / "history-control.db"
    _old_database(path)
    conn = _native(path)
    try:
        assert _schema_rows(conn) == ()
        assert tuple(conn.execute("SELECT id,username,token_version FROM users").fetchone()) == (17, "Old account", 1)
        assert conn.execute("SELECT title FROM items WHERE id=23").fetchone()[0] == "Old catalogue sentinel"
        assert conn.execute("SELECT value FROM settings WHERE key='old.sentinel'").fetchone()[0] == "0"
        assert conn.execute("SELECT value FROM settings WHERE key='feature.connected_library'").fetchone()[0] == "0"
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("variant", ["check-case", "default", "fk", "index", "sql-case"])
def test_oracle_control_schema_oracle_detects_independent_mutants(tmp_path, variant):
    ddl = _PREFERENCES_DDL
    if variant == "check-case":
        ddl = ddl.replace("'integer'", "'INTEGER'", 1)
    elif variant == "default":
        ddl = ddl.replace("DEFAULT 1", "DEFAULT 2")
    elif variant == "fk":
        ddl = ddl.replace("ON DELETE CASCADE", "ON DELETE RESTRICT")
    elif variant == "sql-case":
        ddl = ddl.replace("values_json TEXT", "values_json text")
    conn = _native(tmp_path / "mutant.db")
    try:
        conn.executescript(_USERS_DDL + ddl)
        if variant == "index":
            conn.execute("CREATE INDEX extra ON library_preferences(revision)")
        with pytest.raises(AssertionError):
            _assert_canonical(conn)
    finally:
        conn.close()


def test_central_declaration_and_fresh_boot_match_literal_oracle(db):
    from app import database

    assert database.MIGRATION_TABLES.count(_PREFERENCES_DDL) == 1
    assert "library_preferences" not in database.SCHEMA
    assert all("library_preferences" not in (sql or "")
               for _version, _description, sql in database.MIGRATIONS)
    _assert_canonical(db)
    assert db.execute("SELECT * FROM library_preferences").fetchall() == []
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("column,value", _BAD_STORAGE)
def test_actual_fresh_init_native_constraints_and_foreign_key_reject_bad_storage(db, column, value):
    _assert_canonical(db)
    db.execute("INSERT INTO users (id,username,password) VALUES (1,'schema constraint account','test-data')")
    values = {"user_id": 1, "values_json": "{}", "revision": 1, "updated_at": "raw"}
    values[column] = value
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO library_preferences "
                   "(user_id,values_json,revision,updated_at) VALUES (?,?,?,?)", tuple(values.values()))
    assert db.execute("SELECT * FROM library_preferences").fetchall() == []


def test_genuinely_old_database_upgrades_serially_without_seeding_preferences(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    _old_database(path)
    database = _point_database(monkeypatch, path)
    database.init_db()
    with database.get_db() as conn:
        _assert_canonical(conn)
        assert conn.execute("SELECT * FROM library_preferences").fetchall() == []
        assert tuple(conn.execute("SELECT id, username, role, token_version FROM users "
                                  "WHERE id=17").fetchone()) == (17, "Old account", "viewer", 1)
        assert tuple(conn.execute("SELECT id, title, media_type FROM items_live "
                                  "WHERE id=23").fetchone()) == (23, "Old catalogue sentinel", "book")
        assert conn.execute("SELECT value FROM settings WHERE key='old.sentinel'").fetchone()[0] == "0"
        assert conn.execute("SELECT value FROM settings WHERE key='feature.connected_library'").fetchone()[0] == "0"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        before = _logical_state(conn)
    database.init_db()
    with database.get_db() as conn:
        assert _logical_state(conn) == before


def test_own_canonical_rows_second_init_vacuum_and_account_cascade(db, tmp_path):
    from app import database

    raw = '{ "opaque": [false,0,"",null], "recent": false, "shelves": [] }'
    for uid in (11, 12):
        db.execute("INSERT INTO users (id, username, password) VALUES (?, ?, 'test-data')",
                   (uid, f"account-{uid}"))
        db.execute("INSERT INTO library_preferences (user_id, values_json, revision, updated_at) "
                   "VALUES (?, ?, ?, 'fixed-raw-timestamp')", (uid, raw, uid))
    db.commit()
    before = _logical_state(db)
    database.init_db()
    database.init_db()
    assert _logical_state(db) == before
    _assert_canonical(db)
    target = tmp_path / "full-backup.db"
    db.execute("VACUUM INTO ?", (str(target),))
    copy = _native(target)
    try:
        _assert_canonical(copy)
        assert _logical_state(copy) == before
        assert tuple(copy.execute("SELECT values_json, revision, updated_at "
                                  "FROM library_preferences WHERE user_id=11").fetchone()) == (
            raw, 11, "fixed-raw-timestamp",
        )
        copy.execute("DELETE FROM users WHERE id=11")
        assert [row[0] for row in copy.execute("SELECT user_id FROM library_preferences")] == [12]
        assert db.execute("SELECT COUNT(*) FROM library_preferences").fetchone()[0] == 2
        assert copy.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        copy.close()


_COLLISIONS = (
    "wrong-check-literal", "wrong-fk", "wrong-default", "missing-check",
    "missing-column", "extra-column", "wrong-primary-key", "case-name", "view",
    "virtual", "plain-index", "unique-index", "expression-index", "trigger",
    "named-trigger-other-table", "named-index-other-table",
)


@pytest.mark.parametrize("shape", _COLLISIONS)
def test_incompatible_schema_refuses_before_bootstrap_and_preserves_logical_state(
    tmp_path, monkeypatch, shape,
):
    path = tmp_path / "collision.db"
    _sentinel_database(path)
    conn = _native(path)
    try:
        ddl = _PREFERENCES_DDL
        if shape == "wrong-check-literal":
            ddl = ddl.replace("typeof(revision) = 'integer'", "typeof(revision) = 'INTEGER'")
        elif shape == "wrong-fk":
            ddl = ddl.replace("ON DELETE CASCADE", "ON DELETE RESTRICT")
        elif shape == "wrong-default":
            ddl = ddl.replace("DEFAULT '{}'", "DEFAULT '[]'")
        elif shape == "missing-check":
            ddl = ddl.replace("\n        CHECK (typeof(values_json) = 'text')", "")
        elif shape == "missing-column":
            ddl = ddl.replace(
                ",\n    updated_at TEXT NOT NULL DEFAULT (datetime('now'))\n"
                "        CHECK (typeof(updated_at) = 'text')", "",
            )
        elif shape == "extra-column":
            ddl = ddl.replace("\n);", ",\n    extra TEXT\n);")
        elif shape == "wrong-primary-key":
            ddl = ddl.replace("user_id INTEGER NOT NULL PRIMARY KEY", "user_id INTEGER NOT NULL UNIQUE")
        elif shape == "case-name":
            ddl = ddl.replace("library_preferences", "Library_Preferences")
        elif shape == "view":
            ddl = "CREATE VIEW library_preferences AS SELECT id AS user_id FROM users;"
        elif shape == "virtual":
            # Native core RTree, not an extension download or provider mock.
            ddl = "CREATE VIRTUAL TABLE library_preferences USING rtree(user_id, low, high);"
        elif shape == "named-trigger-other-table":
            ddl = ("CREATE TRIGGER library_preferences AFTER UPDATE ON bootstrap_sentinel "
                   "BEGIN SELECT 1; END;")
        elif shape == "named-index-other-table":
            ddl = "CREATE INDEX library_preferences ON bootstrap_sentinel(value);"
        conn.executescript(ddl)
        if shape in {"plain-index", "unique-index", "expression-index"}:
            index = {
                "plain-index": "CREATE INDEX collision_plain ON library_preferences(revision)",
                "unique-index": "CREATE UNIQUE INDEX collision_unique ON library_preferences(user_id)",
                "expression-index": "CREATE INDEX collision_expr ON library_preferences(length(values_json))",
            }[shape]
            conn.execute(index)
        elif shape == "trigger":
            conn.execute("CREATE TRIGGER collision_trigger AFTER UPDATE ON library_preferences "
                         "BEGIN UPDATE bootstrap_sentinel SET value='must never run'; END")
        if shape not in {"view", "virtual", "wrong-check-literal", "named-trigger-other-table", "named-index-other-table"}:
            conn.execute("INSERT INTO library_preferences (user_id, values_json, revision) "
                         "VALUES (17, '{\"sentinel\":[false,0,null]}', 9)")
        elif shape == "virtual":
            conn.execute("INSERT INTO library_preferences VALUES (17, 1, 2)")
        conn.commit()
        before = _logical_state(conn)
    finally:
        conn.close()
    database = _point_database(monkeypatch, path)
    with pytest.raises(RuntimeError) as error:
        database.init_db()
    assert str(error.value) == "preferences_schema_incompatible"
    conn = _native(path)
    try:
        assert _logical_state(conn) == before
        assert conn.execute("SELECT value FROM bootstrap_sentinel").fetchone()[0] == "keep 0 false null"
    finally:
        conn.close()


@pytest.mark.parametrize("shape", ["missing", "duplicate"])
def test_missing_or_duplicate_canonical_definition_is_fixed_programmer_error(tmp_path, monkeypatch, shape):
    path = tmp_path / "definition.db"
    _sentinel_database(path)
    database = _point_database(monkeypatch, path)
    sql = "CREATE TABLE IF NOT EXISTS unrelated (id INTEGER);\n"
    if shape == "duplicate":
        sql += _PREFERENCES_DDL + "\n" + _PREFERENCES_DDL + "\n"
    monkeypatch.setattr(database, "MIGRATION_TABLES", sql)
    conn = _native(path)
    before = _logical_state(conn)
    conn.close()
    with pytest.raises(RuntimeError) as error:
        database.init_db()
    assert str(error.value) == "preferences_schema_definition_invalid"
    conn = _native(path)
    try:
        assert _logical_state(conn) == before
    finally:
        conn.close()


def test_active_transaction_is_not_committed_by_preference_bootstrap(tmp_path, monkeypatch):
    path = tmp_path / "active.db"
    _sentinel_database(path)
    database = _point_database(monkeypatch, path)

    def observe(conn, operation, sql, parameters, proceed):
        result = proceed()
        if operation == "execute" and sql.startswith("CREATE TEMP VIEW IF NOT EXISTS copies_live"):
            sqlite3.Connection.execute(conn, "BEGIN")
            sqlite3.Connection.execute(conn, "UPDATE bootstrap_sentinel SET value='uncommitted'")
        return result

    instances, original = _instrument_connections(monkeypatch, database, observe)
    with pytest.raises(RuntimeError) as error:
        database.init_db()
    assert str(error.value) == "preferences_bootstrap_transaction_active"
    assert len(instances) == 1
    assert not [event for event in instances[0].events if event[0] == "commit"]
    assert [event[0] for event in instances[0].events][-2:] == ["rollback", "close"]
    conn = original(str(path))
    try:
        assert conn.execute("SELECT value FROM bootstrap_sentinel").fetchone()[0] == "keep 0 false null"
        assert _schema_rows(conn) == ()
    finally:
        conn.close()


def test_preference_create_is_verified_and_committed_before_ordinary_schema(tmp_path, monkeypatch):
    path = tmp_path / "fresh-observed.db"
    database = _point_database(monkeypatch, path)
    instances, _original = _instrument_connections(
        monkeypatch, database, lambda conn, operation, sql, parameters, proceed: proceed(),
    )
    database.init_db()
    assert len(instances) == 1, "init_db must reuse its existing get_db connection"
    events = instances[0].events
    start = next(i for i, event in enumerate(events)
                 if event[0] == "execute" and event[1].strip().upper() == "BEGIN IMMEDIATE")
    script = next(i for i, event in enumerate(events) if event[0] == "executescript")
    prefix = events[start + 1:script]
    assert [event[0] for event in prefix] == ["execute", "execute", "execute", "commit"]
    assert "FROM main.sqlite_master" in prefix[0][1]
    assert prefix[0][2] == ("library_preferences", "library_preferences")
    assert prefix[0][1].strip().removesuffix(";").split() == (
        "SELECT type, name, tbl_name, sql FROM main.sqlite_master WHERE name = ? "
        "COLLATE NOCASE OR tbl_name = ? COLLATE NOCASE"
    ).split()
    assert prefix[1][1] == _PREFERENCES_DDL
    assert prefix[2][1] == prefix[0][1]
    assert prefix[2][2] == prefix[0][2]
    assert all(event[3] for event in prefix)
    assert events[script][3] is False
    assert not any("FROM users" in event[1] or "foreign_key_check" in event[1].lower()
                   for event in prefix if event[0] == "execute")


def test_fault_after_create_before_verify_rolls_back_only_bounded_bootstrap(tmp_path, monkeypatch):
    path = tmp_path / "create-fault.db"
    _sentinel_database(path)
    database = _point_database(monkeypatch, path)
    conn = _native(path)
    before = _logical_state(conn)
    conn.close()
    reached = []

    def observe(conn, operation, sql, parameters, proceed):
        result = proceed()
        if operation == "execute" and sql == _PREFERENCES_DDL:
            reached.append(conn.in_transaction)
            raise sqlite3.OperationalError("injected_after_preference_create")
        return result

    instances, original = _instrument_connections(monkeypatch, database, observe)
    with pytest.raises(sqlite3.OperationalError, match="^injected_after_preference_create$"):
        database.init_db()
    assert reached == [True]
    assert not [event for event in instances[0].events if event[0] in {"commit", "executescript"}]
    assert [event[0] for event in instances[0].events][-2:] == ["rollback", "close"]
    conn = original(str(path))
    try:
        assert _logical_state(conn) == before
        assert _schema_rows(conn) == ()
    finally:
        conn.close()


def test_later_migration_failure_can_leave_committed_additive_preference_table(tmp_path, monkeypatch):
    path = tmp_path / "later-fault.db"
    database = _point_database(monkeypatch, path)
    observed = []

    def fail_migrations(conn):
        witness = _native(path)
        try:
            _assert_canonical(witness)
            assert witness.execute("SELECT * FROM library_preferences").fetchall() == []
            observed.append("already committed before later failure")
        finally:
            witness.close()
        raise sqlite3.OperationalError("injected_ordinary_migration_failure")

    monkeypatch.setattr(database, "_run_migrations", fail_migrations)
    with pytest.raises(sqlite3.OperationalError, match="^injected_ordinary_migration_failure$"):
        database.init_db()
    assert observed == ["already committed before later failure"]
    conn = _native(path)
    try:
        _assert_canonical(conn)
        assert conn.execute("SELECT * FROM library_preferences").fetchall() == []
    finally:
        conn.close()


def test_preference_bootstrap_waits_then_rereads_without_claiming_global_cold_start(tmp_path, monkeypatch):
    path = tmp_path / "bounded-overlap.db"
    database = _point_database(monkeypatch, path)
    # Existing get_db still performs WAL/FK/TEMP setup; prepare WAL before the
    # held lock so the event below measures preference BEGIN, not journal setup.
    with database.get_db():
        pass
    holder = _native(path)
    holder.execute("BEGIN IMMEDIATE")
    attempting, inspected = threading.Event(), threading.Event()
    results = queue.Queue()

    def observe(conn, operation, sql, parameters, proceed):
        if operation == "execute" and sql.strip().upper() == "BEGIN IMMEDIATE":
            attempting.set()
        if operation == "execute" and "FROM main.sqlite_master" in sql:
            inspected.set()
        return proceed()

    instances, original = _instrument_connections(monkeypatch, database, observe)

    def stop_after_bounded_bootstrap(conn):
        raise sqlite3.OperationalError("intentional_end_of_bounded_overlap_probe")

    monkeypatch.setattr(database, "_run_migrations", stop_after_bounded_bootstrap)

    def initialize():
        try:
            database.init_db()
        except BaseException as error:
            results.put(error)

    thread = threading.Thread(target=initialize, name="preference-bootstrap-waiter")
    thread.start()
    try:
        assert attempting.wait(10), "initializer never attempted its writer lock"
        assert not inspected.is_set(), "schema must be inspected after lock acquisition"
        holder.execute(_PREFERENCES_DDL)
        holder.commit()
    finally:
        holder.rollback()
        holder.close()
        thread.join(15)
    assert not thread.is_alive()
    error = results.get_nowait()
    assert type(error) is sqlite3.OperationalError
    assert str(error) == "intentional_end_of_bounded_overlap_probe"
    assert inspected.is_set()
    assert len(instances) == 1
    assert not [event for event in instances[0].events
                if event[0] == "execute" and event[1] == _PREFERENCES_DDL]
    conn = original(str(path))
    try:
        _assert_canonical(conn)
    finally:
        conn.close()


def test_existing_connection_after_commit_and_rollback_callbacks_are_unchanged():
    from app import database

    seen = []
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        database.after_commit(conn, lambda: seen.append("first"))
        assert seen == []
        conn.commit()
        assert seen == ["first"]
        conn.execute("BEGIN IMMEDIATE")
        database.after_commit(conn, lambda: seen.append("rolled-back"))
        conn.rollback()
        conn.commit()
        assert seen == ["first"]


def test_old_vacuum_backup_has_no_preferences_and_initializes_to_row_absence(tmp_path, monkeypatch):
    source, backup = tmp_path / "old-source.db", tmp_path / "old-backup.db"
    _old_database(source)
    conn = _native(source)
    try:
        assert _schema_rows(conn) == ()
        conn.execute("VACUUM INTO ?", (str(backup),))
    finally:
        conn.close()
    conn = _native(backup)
    try:
        assert _schema_rows(conn) == ()
    finally:
        conn.close()
    database = _point_database(monkeypatch, backup)
    database.init_db()
    with database.get_db() as conn:
        _assert_canonical(conn)
        assert conn.execute("SELECT * FROM library_preferences").fetchall() == []
        assert conn.execute("SELECT username FROM users WHERE id=17").fetchone()[0] == "Old account"


def test_quiescent_existing_backup_restore_retains_json_revision_and_invalidates_old_actor(db):
    """Direct existing restore function, NOT HTTP/auth or concurrent F8 evidence."""
    from starlette.datastructures import UploadFile
    from app import database
    from app.routers import settings
    from app.services import preferences

    db.execute("INSERT INTO users (id, username, password, role) "
               "VALUES (31, 'restore account', 'test-data-not-auth', 'viewer')")
    raw = '{ "opaque":[false,0,"",null], "recent":false, "shelves":[] }'
    db.execute("INSERT INTO library_preferences (user_id, values_json, revision) VALUES (31, ?, 7)", (raw,))
    db.commit()
    backup = settings._vacuum_backup()
    content = Path(backup).read_bytes()
    with database.get_db() as conn:
        conn.execute("UPDATE library_preferences SET revision=8, values_json='{}' WHERE user_id=31")
        conn.execute("UPDATE users SET token_version=4 WHERE id=31")
    captured_before_restore = preferences.PreferenceActor(
        31, "restore account", "viewer", 4, 253_402_300_799,
    )

    async def form():
        return {"file": UploadFile(io.BytesIO(content), filename="test-backup.db")}

    result = asyncio.run(settings.restore_backup(SimpleNamespace(form=form)))
    assert result["ok"] is True
    with database.get_db() as conn:
        _assert_canonical(conn)
        row = conn.execute("SELECT values_json, revision FROM library_preferences WHERE user_id=31").fetchone()
        assert tuple(row) == (raw, 7)
        assert json.loads(row[0]) == {"opaque": [False, 0, "", None], "recent": False, "shelves": []}
        assert conn.execute("SELECT token_version FROM users WHERE id=31").fetchone()[0] > 4
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(preferences.PreferenceStoreError) as error:
            preferences.save_preferences(conn, captured_before_restore, 7, {})
        assert error.value.code == str(error.value) == "actor_changed"
        conn.rollback()
    # No claim about a token change racing between live_max and installation:
    # current restore/auth collision remains the separately blocked F8 boundary.
