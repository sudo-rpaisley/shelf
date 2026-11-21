"""Migration 52: the `api_tokens` table.

The legacy fixture is built *without* the current `MIGRATION_TABLES` (G98):
`bootstrap_sql_before(51)` with the api_tokens CREATE stripped. Running the
current bootstrap first would pre-create the table and turn the numbered entry
into an `IF NOT EXISTS` no-op.

The table has no seed rows, so only deleting the numbered entry reds the
legacy test (it loses its `schema_version` row; G98). Replacing its SQL does
not: `executescript(MIGRATION_TABLES)` creates the table from its copy, and
`tests/test_schema_parity.py` checks the numbered body against that copy.
"""

import re
import sqlite3

import pytest

from app.database import _run_migrations
from tests.conftest import bootstrap_sql_before
from tests.lineage import fork_before_upstream_operation, upstream_operation, upstream_version

_N = 52


def _bootstrap_sql_before_52():
    sql = bootstrap_sql_before(_N - 1)
    return re.sub(
        r"CREATE TABLE IF NOT EXISTS api_tokens \(.*?\n?\);\n",
        "",
        sql,
        flags=re.S,
    )


def _legacy_db(tmp_path):
    return fork_before_upstream_operation(tmp_path / "legacy.db", _N)


def _tables(conn):
    return {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _versions(conn):
    return [
        tuple(r) for r in conn.execute(
            "SELECT version, description FROM schema_version ORDER BY version"
        )
    ]


def test_migration_52_is_the_api_tokens_entry():
    assert upstream_operation(_N)[1] == "Bridge upstream 52: Add API tokens"
    assert upstream_version(_N) > 66


def test_a_legacy_database_gains_the_table(tmp_path):
    conn = _legacy_db(tmp_path)
    try:
        assert "api_tokens" not in _tables(conn)
        _run_migrations(conn)
        assert "api_tokens" in _tables(conn)
        assert upstream_version(_N) in {v for v, _ in _versions(conn)}
    finally:
        conn.close()


def test_a_second_run_changes_nothing(tmp_path):
    conn = _legacy_db(tmp_path)
    try:
        _run_migrations(conn)
        before = _versions(conn)
        _run_migrations(conn)
        assert _versions(conn) == before
        assert "api_tokens" in _tables(conn)
    finally:
        conn.close()


def test_a_fresh_database_has_the_designed_columns(db):
    cols = {r["name"] for r in db.execute("PRAGMA table_info(api_tokens)")}
    assert cols == {
        "id", "user_id", "name", "token_hash", "prefix", "last_used_at",
        "created_at",
    }


def test_user_id_cascades_from_users(db):
    fks = db.execute("PRAGMA foreign_key_list(api_tokens)").fetchall()
    # PRAGMA foreign_key_list: on_delete is index 6.
    assert [(r["table"], r["from"], r[6]) for r in fks] == [
        ("users", "user_id", "CASCADE"),
    ]


def _user(db, name):
    return db.execute(
        "INSERT INTO users (username, password, display_name, role)"
        " VALUES (?, 'x', ?, 'viewer')",
        (name, name),
    ).lastrowid


def _token(db, user_id, token_hash):
    db.execute(
        "INSERT INTO api_tokens (user_id, name, token_hash, prefix)"
        " VALUES (?, 'n', ?, 'shf_')",
        (user_id, token_hash),
    )


def test_deleting_a_user_removes_their_tokens(db):
    keep, drop = _user(db, "keep"), _user(db, "drop")
    _token(db, keep, "h1")
    _token(db, drop, "h2")
    _token(db, drop, "h3")
    db.execute("DELETE FROM users WHERE id = ?", (drop,))
    rows = db.execute("SELECT user_id FROM api_tokens").fetchall()
    assert [r["user_id"] for r in rows] == [keep]


def test_token_hash_is_unique(db):
    uid = _user(db, "u")
    _token(db, uid, "same")
    with pytest.raises(sqlite3.IntegrityError):
        _token(db, uid, "same")
