"""A genuine pre-scan manifest upgrades before any HTTP history is served."""

from contextlib import closing
import sqlite3

from app import config, database
from app.auth import create_token
from tests.conftest import _insert_item
from tests.test_fork_schema_bridge import historical_bridge_before_scan


def test_pre_scan_manifest_backfills_only_bound_history_then_http_scopes_trash_and_purge(client):
    old = historical_bridge_before_scan(database)
    try:
        people = {}
        for name, role in (("migration-admin", "admin"), ("migration-owner", "editor"),
                           ("migration-colleague", "viewer"), ("migration-peer", "editor")):
            uid = old.execute("INSERT INTO users(username,password,role) VALUES(?,'not-a-login',?)",
                              (name, role)).lastrowid
            people[name] = {"id": uid, "username": name, "role": role}
        private = old.execute("INSERT INTO libraries(name) VALUES('Pre-scan private library')").lastrowid
        old.executemany("INSERT INTO library_memberships(library_id,user_id,role) VALUES(?,?,'editor')", [
            (private, people["migration-owner"]["id"]),
            (private, people["migration-colleague"]["id"]),
            (1, people["migration-peer"]["id"]),
        ])
        item = _insert_item(old, title="PRE-SCAN PRIVATE TITLE", isbn=None, _library_id=private)
        bound = old.execute(
            "INSERT INTO scan_log(isbn,media_type,result,item_id,mode) VALUES('PRE-SCAN-BOUND','book','duplicate',?,'add')",
            (item,),
        ).lastrowid
        orphan = old.execute(
            "INSERT INTO scan_log(isbn,media_type,result,mode) VALUES('PRE-SCAN-ORPHAN','book','error','add')",
        ).lastrowid
        before = [tuple(row) for row in old.execute("SELECT id,isbn,result,mode,created_at FROM scan_log ORDER BY id")]
        assert "security_library_id" not in {row[1] for row in old.execute("PRAGMA table_info(scan_log)")}
        old.commit()
        with closing(sqlite3.connect(config.DATABASE_PATH)) as serving:
            old.backup(serving)
    finally:
        old.close()

    # The actual initializer admits the pinned old manifest and performs the
    # one-time bound-only backfill. No hot-path lazy inference is necessary.
    database.init_db()

    def request_as(name, method, path):
        user = people[name]
        client.cookies.set("access_token", create_token(user["id"], user["username"], user["role"]))
        return client.request(method, path)

    def assert_scopes():
        hidden = request_as("migration-peer", "GET", "/api/recent-scans?mode=add")
        assert hidden.status_code == 200
        assert not {bound, orphan} & {row["id"] for row in hidden.context["recent_scans"]}
        assert "PRE-SCAN" not in hidden.text
        for name in ("migration-owner", "migration-colleague"):
            shown = request_as(name, "GET", "/api/recent-scans?mode=add")
            assert shown.status_code == 200
            assert {row["id"] for row in shown.context["recent_scans"]} == {bound}
            assert "PRE-SCAN-ORPHAN" not in shown.text

    assert_scopes()
    assert request_as("migration-owner", "DELETE", f"/api/items/{item}").status_code == 200
    assert_scopes()
    assert request_as("migration-admin", "DELETE", f"/api/trash/items/{item}").status_code == 200
    assert_scopes()
    with database.get_db() as db:
        assert [tuple(row) for row in db.execute("SELECT id,isbn,result,mode,created_at FROM scan_log ORDER BY id")] == before
        assert tuple(db.execute("SELECT item_id,security_library_id,security_user_id FROM scan_log WHERE id=?", (bound,)).fetchone()) == (
            None, private, None,
        )
        assert tuple(db.execute("SELECT security_library_id,security_user_id FROM scan_log WHERE id=?", (orphan,)).fetchone()) == (None, None)
        assert db.execute("SELECT COUNT(*) FROM schema_version WHERE version BETWEEN 119 AND 121").fetchone()[0] == 3
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
