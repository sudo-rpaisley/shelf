"""Bounded authoring check: selected owned AST + disposable in-memory SQL.

No app imports, application startup, central migration runner, providers, routes
or physical writer are executed. The deliberately minimal SQL fixture checks
authorization expressions, TEMP-view SQL and keyed-child atomicity, NOT schema
convergence or end-to-end behavior. Explicit acquisition intent is checked against
real isolated rows; physical creation/history handling remains a full-app oracle.
The full regression module is separate.

Run from the project root: python tests/security_writer_isolated_sql_checks.py
"""

import ast
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar, Token, copy_context
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _load(relative, names, namespace):
    path = ROOT / relative
    tree = ast.parse(path.read_text(), filename=str(path))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    assert {node.name for node in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)


class _Refusal(Exception):
    """Only a carrier for selected raise branches; not an HTTP implementation."""

    def __init__(self, status_code, detail):
        self.status_code = status_code
        super().__init__(detail)


def main():
    lib = {"LIBRARY_ROLE_LEVELS": {"viewer": 1, "editor": 2, "admin": 3}}
    # Central schema behavior is excluded. Require the initialized mapping in
    # this bounded fixture instead of simulating migrations or membership seeds.
    lib["ensure_schema"] = lambda db: db.execute("SELECT item_id, library_id FROM library_items LIMIT 0")
    _load("app/services/libraries.py", {"_account_admin", "_cap_allows", "membership_role", "has_library_role", "item_library_id", "has_item_role", "item_access_condition"}, lib)
    sec = {"ContextVar": ContextVar, "Token": Token, "HTTPException": _Refusal, "libraries": SimpleNamespace(**lib), "_actor": ContextVar("isolated_actor", default=None), "_EXPIRED": object()}
    _load("app/services/catalogue_security.py", {"set_actor", "reset_actor", "current_actor", "_principal", "require_item_role", "require_library_role", "access_condition", "item_projection", "install_catalogue_views"}, sec)
    writer = {"Any": Any, "contextmanager": contextmanager, "_savepoints": count(), "catalogue_security": SimpleNamespace(**sec), "libraries": SimpleNamespace(**lib), "ItemValueError": ValueError}
    _load("app/services/item_write.py", {"IdentifierUnavailable", "InvalidOwned", "_write_scope", "_refuse_hidden_collisions", "_coerce_owned", "_acquiring_item_ids", "ItemId", "was_restored"}, writer)
    merge = {"ItemValueError": ValueError}
    _load("app/services/item_merge.py", {"_quote", "_combined", "_merge_keyed"}, merge)
    trash = {"catalogue_security": SimpleNamespace(**sec)}
    _load("app/services/trash.py", {"expired_clause", "_count", "listing", "copy_state", "_unlink_scan_history"}, trash)

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE items(id INTEGER PRIMARY KEY,title TEXT,isbn TEXT,upc TEXT,media_type TEXT DEFAULT 'book',reading_status TEXT,date_started TEXT,date_finished TEXT,deleted_at TEXT,updated_at TEXT,owned INTEGER DEFAULT 1);
        CREATE TABLE libraries(id INTEGER PRIMARY KEY);
        CREATE TABLE library_items(item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,library_id INTEGER REFERENCES libraries(id));
        CREATE TABLE library_memberships(library_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(library_id,user_id));
        CREATE TABLE user_item_state(user_id INTEGER,item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,reading_status TEXT,date_started TEXT,date_finished TEXT,personal_notes TEXT,rating INTEGER,wishlist INTEGER DEFAULT 0,created_at TEXT,updated_at TEXT,PRIMARY KEY(user_id,item_id));
        CREATE TABLE item_copies(id INTEGER PRIMARY KEY,item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,deleted_at TEXT,copy_number INTEGER DEFAULT 1,location_id INTEGER);
        CREATE TABLE music_releases(item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,musicbrainz_release_id TEXT UNIQUE,edition_notes TEXT,artist_credit TEXT,media_count INTEGER);
        CREATE TABLE checkouts(item_id INTEGER,checked_in TEXT);
        CREATE TABLE locations(id INTEGER PRIMARY KEY,name TEXT);
        CREATE TABLE location_nodes(id INTEGER PRIMARY KEY,name TEXT);
        INSERT INTO locations VALUES(1,'WRONG flat location');
        INSERT INTO location_nodes VALUES(1,'Correct node location');
        INSERT INTO libraries VALUES(1),(2);
        INSERT INTO items(id,title,isbn,upc,media_type,reading_status,date_started,date_finished,deleted_at,updated_at) VALUES(1,'Visible','visible-code',NULL,'book','read',NULL,'2001-01-01',NULL,'2000'),(2,'SECRET','secret-code',NULL,'book','reading',NULL,NULL,NULL,'2000'),(3,'Deleted','deleted-code',NULL,'book','read',NULL,NULL,'2000-01-01','2000'),(4,'Unmapped',NULL,NULL,'book',NULL,NULL,NULL,NULL,'2000');
        INSERT INTO library_items VALUES(1,1),(2,2),(3,1);
        INSERT INTO library_memberships VALUES(1,7,'editor'),(2,8,'viewer');
        INSERT INTO user_item_state(user_id,item_id,reading_status,personal_notes) VALUES(8,1,'reading','theirs');
        INSERT INTO item_copies(id,item_id,location_id) VALUES(1,1,1),(2,2,1),(3,3,1),(4,4,1);
    """)
    token = sec["set_actor"]({"id": 7, "role": "viewer"})
    sec["install_catalogue_views"](db)
    rows = db.execute("SELECT * FROM items_live").fetchall()
    assert [row["id"] for row in rows] == [1]
    assert rows[0]["reading_status"] is None and rows[0]["date_finished"] is None
    assert [row[0] for row in db.execute("SELECT id FROM copies_live")] == [1]
    assert sec["require_item_role"](db, 1, "editor")["id"] == 1
    for item in (2, 3, 4):
        try:
            sec["require_item_role"](db, item, "viewer")
        except _Refusal as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("hidden/deleted/unmapped authorization")
    assert sec["require_item_role"](db, 3, "editor", include_deleted=True)["id"] == 3
    try:
        writer["_refuse_hidden_collisions"](db, {"isbn": "secret-code"})
    except writer["IdentifierUnavailable"] as exc:
        assert str(exc) == "This identifier is unavailable" and not hasattr(exc, "item_id")
    else:
        raise AssertionError("hidden identifier accepted")
    db.execute("UPDATE item_copies SET deleted_at='2000-01-01' WHERE id IN (1,2)")
    assert trash["_count"](db, 180) == 2
    listing = trash["listing"](db, expired_only=False, days=180)
    assert [row["id"] for row in listing["items"]] == [3]
    assert listing["items"][0]["reading_status"] is None
    assert [group["item_id"] for group in listing["copy_groups"]] == [1]
    assert listing["copy_groups"][0]["copies"][0]["location_name"] == "Correct node location"
    assert trash["copy_state"](db, 2) is None
    inherited = copy_context()
    sec["reset_actor"](token)
    assert sec["current_actor"]() is None
    try:
        inherited.run(sec["current_actor"])
    except RuntimeError as exc:
        assert "request scope has ended" in str(exc)
    else:
        raise AssertionError("expired HTTP context became trusted service")
    token = sec["set_actor"]({"id": 9, "role": "viewer", "account_role": "admin", "api_token_id": 12})
    sec["install_catalogue_views"](db)
    assert {row[0] for row in db.execute("SELECT id FROM items_live")} == {1, 2}
    assert not lib["has_item_role"](db, sec["current_actor"](), 1, "editor")
    sec["reset_actor"](token)
    sec["install_catalogue_views"](db)
    assert {row[0] for row in db.execute("SELECT id FROM items_live")} == {1, 2, 4}
    print("PASS: actual TEMP SQL, roles/cap, private overlays, hidden-collision refusal, scoped Trash/node join, expired-context denial")

    db.execute("UPDATE items SET owned = 0 WHERE id IN (2, 3)")
    for value in (1, "1", True):
        assert writer["_acquiring_item_ids"](db, [1, 2, 3, 4, 999], {"owned": value}) == {2}
    for fields in ({}, {"owned": 0}, {"owned": "0"}):
        assert writer["_acquiring_item_ids"](db, [1, 2, 3, 4], fields) == set()
    assert writer["_acquiring_item_ids"](db, [], {"owned": 1}) == set()
    db.execute("UPDATE items SET owned = 1 WHERE id = 2")
    assert writer["_acquiring_item_ids"](db, [1, 2, 3, 4], {"owned": 1}) == set()

    # Narrow caller-contract guard: the physical owner alone projects copies.
    writer_tree = ast.parse((ROOT / "app/services/item_write.py").read_text())
    project = next(node for node in writer_tree.body if isinstance(node, ast.FunctionDef) and node.name == "_project_item")
    physical_calls = [node for node in ast.walk(project) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and isinstance(node.func.value, ast.Name) and node.func.value.id in {"holdings", "item_copies"}]
    assert len(physical_calls) == 1
    physical_call = physical_calls[0]
    assert ast.unparse(physical_call.func) == "holdings.sync_item"
    intent = next(keyword.value for keyword in physical_call.keywords if keyword.arg == "create_copy")
    assert ast.unparse(intent) == "inserted or acquired"
    merge_tree = ast.parse((ROOT / "app/services/item_merge.py").read_text())
    reparent = next(node for node in merge_tree.body if isinstance(node, ast.FunctionDef) and node.name == "reparent_children")
    for call in ast.walk(reparent):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and ast.unparse(call.func) == "holdings.sync_item":
            flag = next(keyword.value for keyword in call.keywords if keyword.arg == "create_copy")
            assert isinstance(flag, ast.Constant) and flag.value is False
    print("PASS: locked pre-update live SQL distinguishes explicit 0->1 acquisition from routine owned=1; one creation-aware projection, no merge placeholders")

    db.execute("INSERT INTO user_item_state(user_id,item_id,rating,created_at) VALUES(7,1,5,'2002')")
    db.execute("INSERT INTO user_item_state(user_id,item_id,personal_notes,created_at) VALUES(7,2,'retained','2001')")
    with writer["_write_scope"](db):
        merge["_merge_keyed"](db, "user_item_state", 1, 2, ("user_id",))
    row = db.execute("SELECT * FROM user_item_state WHERE user_id=7 AND item_id=1").fetchone()
    assert row["rating"] == 5 and row["personal_notes"] == "retained" and row["created_at"] == "2001"
    db.execute("INSERT INTO user_item_state(user_id,item_id,personal_notes) VALUES(7,2,'CONFLICT')")
    before = tuple(db.iterdump())
    try:
        with writer["_write_scope"](db):
            db.execute("UPDATE items SET title='must roll back' WHERE id=1")
            merge["_merge_keyed"](db, "user_item_state", 1, 2, ("user_id",))
    except ValueError as exc:
        assert "CONFLICT" not in str(exc)
    else:
        raise AssertionError("conflicting private identity accepted")
    assert tuple(db.iterdump()) == before
    db.execute("INSERT INTO music_releases(item_id, edition_notes, media_count) VALUES(1, 'Collector notes', 0)")
    db.execute("INSERT INTO music_releases(item_id, musicbrainz_release_id, artist_credit) VALUES(2, 'one', 'Source artist')")
    # Negative control: this fixture MUST enforce the real global identity
    # constraint. An omitted UNIQUE used to hide the unsafe transfer ordering.
    try:
        db.execute("UPDATE music_releases SET musicbrainz_release_id = 'one' WHERE item_id = 1")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("release fixture does not enforce global UNIQUE")
    print("PASS: negative control enforces global UNIQUE release identity")
    before = tuple(db.iterdump())
    try:
        with writer["_write_scope"](db):
            merge["_merge_keyed"](db, "music_releases", 1, 2)
            raise ValueError("deliberate later refusal")
    except ValueError as exc:
        assert str(exc) == "deliberate later refusal"
    assert tuple(db.iterdump()) == before
    with writer["_write_scope"](db):
        merge["_merge_keyed"](db, "music_releases", 1, 2)
    release = db.execute("SELECT * FROM music_releases WHERE item_id = 1").fetchone()
    assert (release["musicbrainz_release_id"], release["edition_notes"], release["artist_credit"], release["media_count"]) == ("one", "Collector notes", "Source artist", 0)
    assert db.execute("SELECT 1 FROM music_releases WHERE item_id = 2").fetchone() is None
    db.execute("INSERT INTO music_releases(item_id, musicbrainz_release_id) VALUES(2, 'two')")
    before = tuple(db.iterdump())
    try:
        with writer["_write_scope"](db):
            merge["_merge_keyed"](db, "music_releases", 1, 2)
    except ValueError:
        pass
    else:
        raise AssertionError("conflicting release identity accepted")
    assert tuple(db.iterdump()) == before
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert writer["was_restored"](writer["ItemId"](7, restored=True)) and not writer["was_restored"](7)
    db.close()
    print("PASS: actual keyed-child SQL/savepoints preserve complementary state, refuse private/release conflicts atomically; FK and ItemId checks")

    history = sqlite3.connect(":memory:")
    history.execute("PRAGMA foreign_keys=ON")
    history.executescript("""
        CREATE TABLE items(id INTEGER PRIMARY KEY,updated_at TEXT);
        CREATE TABLE library_items(item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,library_id INTEGER);
        CREATE TABLE scan_log(id INTEGER PRIMARY KEY,isbn TEXT,result TEXT,item_id INTEGER REFERENCES items(id),security_library_id INTEGER,security_user_id INTEGER);
        INSERT INTO items VALUES(1,'unchanged'),(2,'unchanged');
        INSERT INTO library_items VALUES(1,11),(2,22);
        INSERT INTO scan_log VALUES(1,'capture','duplicate',1,NULL,7),(2,'saved','found',1,22,8),(3,'unknown-user','duplicate',1,NULL,NULL),(4,'orphan','error',NULL,NULL,NULL),(5,'unrelated','duplicate',2,NULL,9);
    """)
    before = tuple(history.iterdump())
    try:
        with writer["_write_scope"](history):
            trash["_unlink_scan_history"](history, 1)
            history.execute("DELETE FROM items WHERE id = 1")
            raise ValueError("deliberate later purge refusal")
    except ValueError as exc:
        assert str(exc) == "deliberate later purge refusal"
    assert tuple(history.iterdump()) == before
    with writer["_write_scope"](history):
        trash["_unlink_scan_history"](history, 1)
        history.execute("DELETE FROM items WHERE id = 1")
    assert history.execute("SELECT * FROM scan_log ORDER BY id").fetchall() == [
        (1, "capture", "duplicate", None, 11, 7),
        (2, "saved", "found", None, 22, 8),
        (3, "unknown-user", "duplicate", None, 11, None),
        (4, "orphan", "error", None, None, None),
        (5, "unrelated", "duplicate", 2, None, 9),
    ]
    assert not history.execute("PRAGMA foreign_key_check").fetchall()
    history.close()
    print("PASS: actual purge unlink SQL preserves/captures library provenance, never guesses users, retains raw/history IDs and rolls back on later refusal")

    broken = sqlite3.connect(":memory:")
    broken.executescript("CREATE TABLE items(id INTEGER); INSERT INTO items VALUES(1);")
    token = sec["set_actor"]({"id": 1, "role": "admin"})
    try:
        try:
            sec["install_catalogue_views"](broken)
        except sqlite3.OperationalError as exc:
            assert "library_items" in str(exc)
        else:
            raise AssertionError("missing security schema accepted")
        assert broken.execute("SELECT * FROM items_live").fetchall() == []
    finally:
        sec["reset_actor"](token)
        broken.close()
    print("PASS: missing initialized security schema poisons broad TEMP views even if caller catches the error")
    print("BOUNDARY: selected owned AST/SQL only; no app imports, full schema, HTTP dispatch, projections or whole-merge execution")


if __name__ == "__main__":
    main()
