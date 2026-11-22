"""Migration 40 (`price_history`) on a database that predates it.

Built the G98 way: `SCHEMA` plus the numbered entries up to 39 plus
`bootstrap_sql_before(39)` with the `price_history` CREATE and its index
stripped, never the current `MIGRATION_TABLES`.

Deleting numbered entry 40 alone CANNOT red the legacy test below:
`_run_migrations` executes `MIGRATION_TABLES` on every boot and recreates the
table anyway, and `_is_benign_migration_error` forgives the "no such table"
a missing entry would leave behind. The pins that bite are
`test_schema_parity.py` (a numbered CREATE with no `MIGRATION_TABLES` copy) and
the `sqlite_master` absent-before assertion here, which proves the fixture is
a genuine legacy database.
"""

import re

from app.database import _run_migrations
from app.services import item_write, trash
from tests.conftest import _insert_item, bootstrap_sql_before
from tests.lineage import fork_before_upstream_operation, upstream_version


def _bootstrap_sql_before_40():
    sql = bootstrap_sql_before(39)
    sql = re.sub(
        r"CREATE TABLE IF NOT EXISTS price_history \(.*?\);\n", "", sql, flags=re.S
    )
    sql = re.sub(r"CREATE INDEX IF NOT EXISTS idx_price_history_item[^;]*;\n", "", sql)
    return sql


def _legacy_db(tmp_path, up_to=39):
    return fork_before_upstream_operation(tmp_path / "legacy.db", up_to + 1)


def _names(conn, kind):
    return {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    }


def test_the_fixture_predates_price_history(tmp_path):
    conn = _legacy_db(tmp_path)
    try:
        assert "price_history" not in _names(conn, "table")
        assert "idx_price_history_item" not in _names(conn, "index")
    finally:
        conn.close()


def test_migration_creates_table_index_and_version_row(tmp_path):
    conn = _legacy_db(tmp_path)
    try:
        assert "price_history" not in _names(conn, "table")

        _run_migrations(conn)

        assert "price_history" in _names(conn, "table")
        assert "idx_price_history_item" in _names(conn, "index")
        row = conn.execute(
            "SELECT description FROM schema_version WHERE version = 40"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_rerunning_the_migrations_changes_nothing(tmp_path):
    conn = _legacy_db(tmp_path)
    try:
        _run_migrations(conn)
        conn.execute("INSERT INTO items (title) VALUES ('X')")
        conn.execute("INSERT INTO price_history (item_id, price) VALUES (1, 9.5)")
        conn.commit()
        versions = conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"]

        _run_migrations(conn)

        assert conn.execute("SELECT COUNT(*) AS c FROM price_history").fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM schema_version"
        ).fetchone()["c"] == versions
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM schema_version WHERE version = ?", (upstream_version(40),)
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_fresh_database_has_the_table_columns(db):
    cols = {r["name"]: r for r in db.execute("PRAGMA table_info(price_history)")}
    assert list(cols) == ["id", "item_id", "price", "source", "observed_at"]
    assert cols["price"]["notnull"] == 0
    assert cols["item_id"]["notnull"] == 1
    assert cols["source"]["dflt_value"] == "'isbndb'"
    indexes = {r["name"] for r in db.execute("PRAGMA index_list(price_history)")}
    assert "idx_price_history_item" in indexes


def _seed_rows(db):
    item_id = _insert_item(db)
    db.execute(
        "INSERT INTO price_history (item_id, price) VALUES (?, 10.0), (?, NULL)",
        (item_id, item_id),
    )
    db.commit()
    return item_id


def _count(db, item_id):
    return db.execute(
        "SELECT COUNT(*) AS c FROM price_history WHERE item_id = ?", (item_id,)
    ).fetchone()["c"]


def test_purging_an_item_removes_its_price_history(db):
    item_id = _seed_rows(db)
    # A live row is refused and keeps its history.
    assert trash.purge_item(db, item_id) is False
    assert _count(db, item_id) == 2

    item_write.trash_item(db, item_id)
    assert trash.purge_item(db, item_id) is True
    db.commit()

    assert _count(db, item_id) == 0


def test_trash_then_restore_leaves_price_history_untouched(db):
    item_id = _seed_rows(db)

    assert item_write.trash_item(db, item_id) is True
    db.commit()
    assert _count(db, item_id) == 2

    assert item_write.restore_item(db, item_id) is True
    db.commit()
    assert _count(db, item_id) == 2
