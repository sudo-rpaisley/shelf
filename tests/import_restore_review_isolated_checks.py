"""Standalone AST/SQL probes, NOT pytest or whole-app verification.

Run with the project Python after source authoring. No app modules are imported.
The hook-capable connection below is a TEST MODEL ONLY: an AST-extracted class
with stdlib globals, not an import of the database module or the application.
Canonical connection/HTTP regressions in test_import_restore_review_fixes.py
remain unrun.
"""

import ast
import asyncio
import base64
import builtins
import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet, InvalidToken
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

ROOT = Path(__file__).resolve().parents[1]


def extract(relative, names, namespace, *, undecorated=False):
    tree = ast.parse((ROOT / relative).read_text())
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name in names]
    assert {node.name for node in selected} == names
    if undecorated:
        for node in selected:
            node.decorator_list = []
    exec(compile(ast.Module(body=selected, type_ignores=[]), relative, "exec"), namespace)


def _protected_connection_model():
    # Bind the ACTUAL explicit critical-phase protocol, not a guessed pair of
    # ordinary callback lists. No other database source or app import executes.
    namespace = {"sqlite3": sqlite3}
    extract("app/database.py", {"_Connection"}, namespace)
    connection = namespace["_Connection"]
    assert callable(getattr(connection, "_register_critical_resource_outcome", None))
    return connection


class HookContractProbe(_protected_connection_model()):
    """TEST MODEL ONLY: AST-extracted protected connection; no application import."""


@contextmanager
def write_scope(db):
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    db.execute("SAVEPOINT probe_write")
    try:
        yield
        db.execute("RELEASE probe_write")
    except BaseException:
        db.execute("ROLLBACK TO probe_write")
        db.execute("RELEASE probe_write")
        raise


def export_probe(directory):
    config = SimpleNamespace(DATA_DIR=directory)
    actor = {"notes": "actor A private"}
    ns = dict(Path=Path, os=os, tempfile=tempfile, config=config, json=json, zipfile=zipfile,
              datetime=datetime, timezone=timezone, FORMAT_NAME="shelf-archive", FORMAT_VERSION=3,
              current_actor=lambda: None,
              _build_items=lambda db: ([{"personal_state": {"personal_notes": actor["notes"]}}], {}, []),
              _fetch_reading_log=lambda *args: [{"notes": actor["notes"]}])
    for suffix in ("locations", "tags", "borrowers", "series", "checkouts", "valuation_history"):
        ns["_fetch_" + suffix] = lambda *args: []
    extract("app/services/archive.py", {"archive_tmp_path", "build_archive"}, ns)

    @contextmanager
    def get_db():
        yield None

    ns.update(get_db=get_db, Depends=lambda value: None, require_role=lambda role: None,
              FileResponse=FileResponse, BackgroundTask=BackgroundTask)
    extract("app/routers/archive.py", {"export_archive"}, ns, undecorated=True)
    first = asyncio.run(ns["export_archive"]())
    actor["notes"] = "actor B private"
    second = asyncio.run(ns["export_archive"]())
    a, b = Path(first.path), Path(second.path)
    assert a != b and a.exists() and b.exists()
    assert a.stat().st_mode & 0o777 == 0o600

    async def stream(response):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            messages.append(message)

        await response({"type": "http", "method": "GET", "headers": [], "extensions": {},
                        "asgi": {"spec_version": "2.4"}}, receive, send)
        import io
        with zipfile.ZipFile(io.BytesIO(b"".join(message.get("body", b"") for message in messages))) as archive:
            return json.loads(archive.read("library.json"))

    assert asyncio.run(stream(first))["reading_log"] == [{"notes": "actor A private"}]
    assert not a.exists() and b.exists()
    assert asyncio.run(stream(second))["items"][0]["personal_state"]["personal_notes"] == "actor B private"
    assert not b.exists()
    kept = ns["build_archive"](None)
    ns["_build_items"] = lambda db: ([], {}, [("covers/missing.jpg", directory / "not-a-file")])
    try:
        ns["build_archive"](None)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("zip failure was not injected")
    assert set(directory.glob("shelf_archive_*.zip")) == {kept}
    kept.unlink()
    print("PASS export: two delayed ASGI opens preserve actor payload, owned cleanup, 0600, failed-build cleanup")


def cover_probe(directory):
    resources = {}
    exec(compile(ast.parse((ROOT / "app/services/transaction_resources.py").read_text()), "transaction_resources.py", "exec"),
         resources)
    register = resources["register_transaction_resource"]
    config = SimpleNamespace(COVERS_DIR=directory / "covers")
    config.COVERS_DIR.mkdir()
    ns = dict(Path=Path, os=os, tempfile=tempfile, config=config, wraps=wraps, ArchiveReader=object,
              item_write=SimpleNamespace(_write_scope=write_scope), register_transaction_resource=register,
              _cover_journals=ContextVar("isolated_cover_scopes", default=()))
    extract("app/services/archive.py", {"_replace_cover", "_undo_covers", "_CoverJournal", "_atomic_archive_apply", "_install_cover"}, ns)
    path = config.COVERS_DIR / "1.jpg"
    path.write_bytes(b"original")

    def connection():
        db = sqlite3.connect(":memory:", factory=HookContractProbe)
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("CREATE TABLE marker(id INTEGER PRIMARY KEY,value TEXT); INSERT INTO marker VALUES(1,'old'); "
                         "CREATE TABLE child(parent INTEGER REFERENCES marker(id) DEFERRABLE INITIALLY DEFERRED);")
        return db

    @ns["_atomic_archive_apply"]
    def apply(db, content, fail=False):
        db.execute("UPDATE marker SET value='uncommitted'")
        ns["_install_cover"](SimpleNamespace(read_cover=lambda name: content), 1, "covers/1.jpg")
        if fail:
            raise RuntimeError("local refusal")

    db = connection()
    apply(db, b"first")
    fence = db._transaction_resources.fence
    assert fence is not None and db._critical_resource_outcomes
    assert db._after_commit == db._after_rollback == []
    apply(db, b"second")
    assert db._transaction_resources.fence is fence
    assert path.read_bytes() == b"second" and db.in_transaction
    assert ns["_cover_journals"].get() == ()  # recovery is NOT ContextVar-owned
    undo = ns["_undo_covers"]
    phases = []

    def observe_undo(entries):
        phases.append(("critical undo", db.in_transaction, fence.fd is not None))
        undo(entries)

    def observe_rollback():
        phases.append(("ordinary rollback", db.in_transaction, fence.fd is not None))
        assert db._transaction_resources is None
        assert path.read_bytes() == b"original"

    ns["_undo_covers"] = observe_undo
    db._after_commit.append(lambda: phases.append("wrong success"))
    db._after_rollback.append(observe_rollback)
    db.rollback()
    ns["_undo_covers"] = undo
    assert phases == [("critical undo", True, True), ("critical undo", True, True),
                      ("ordinary rollback", False, False)]
    assert path.read_bytes() == b"original"
    assert db.execute("SELECT value FROM marker").fetchone()[0] == "old"
    assert db._after_commit == db._after_rollback == [] and db._transaction_resources is None

    @ns["_atomic_archive_apply"]
    def nested(conn, fail=False):
        apply(conn, b"nested success")
        try:
            apply(conn, b"nested failure", True)
        except RuntimeError:
            pass
        assert path.read_bytes() == b"nested success"
        # Reproduce the exact row savepoint/journal protocol, including a file
        # first introduced in a failed row, then successfully written on retry.
        row = ns["_CoverJournal"](conn)
        token = ns["_cover_journals"].set((*ns["_cover_journals"].get(), row))
        try:
            ns["_install_cover"](SimpleNamespace(read_cover=lambda name: b"failed row"), 2, "cover")
            row.rollback()
        finally:
            ns["_cover_journals"].reset(token)
        assert not (config.COVERS_DIR / "2.jpg").exists()
        ns["_install_cover"](SimpleNamespace(read_cover=lambda name: b"retry row"), 2, "cover")
        if fail:
            raise RuntimeError("outer refusal")

    nested(db)
    db.rollback()
    assert path.read_bytes() == b"original" and not (config.COVERS_DIR / "2.jpg").exists()
    try:
        nested(db, True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("nested failure did not propagate")
    assert path.read_bytes() == b"original"
    assert db._transaction_resources.entries == [] and db._transaction_resources.fence is None
    db.rollback()
    assert db._transaction_resources is None
    apply(db, b"failed commit")
    db.execute("INSERT INTO child VALUES(-1)")
    try:
        db.commit()
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("deferred FK commit unexpectedly succeeded")
    assert path.read_bytes() == b"original" and not db.in_transaction
    apply(db, b"actual auto-abort")
    fence = db._transaction_resources.fence
    try:
        db.execute("INSERT OR ROLLBACK INTO marker VALUES(1,'duplicate')")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("native INSERT OR ROLLBACK did not abort")
    assert not db.in_transaction and fence.fd is not None
    try:
        db.execute("UPDATE marker SET value='must not start a new transaction'")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("pending auto-aborted resource transaction accepted new SQL")
    phases.clear()
    ns["_undo_covers"] = observe_undo
    db.rollback()
    ns["_undo_covers"] = undo
    assert phases == [("critical undo", False, True)] and fence.fd is None
    assert path.read_bytes() == b"original" and db._transaction_resources is None

    db.execute("UPDATE marker SET value='caller before local failure'")
    root = ns["_CoverJournal"](db)
    root.remember(path)
    path.write_bytes(b"failed local undo")
    replace = ns["_replace_cover"]

    def refuse_restore(*args):
        raise OSError("injected local recovery failure")

    ns["_replace_cover"] = refuse_restore
    try:
        root.rollback()
    except OSError:
        pass
    else:
        raise AssertionError("local undo failure was not injected")
    assert root.entries == {path: b"original"} and root.resource is not None
    assert db._transaction_resources is not None and db._transaction_resources.fence.fd is not None
    ns["_replace_cover"] = replace
    try:
        db.commit()
    except resources["TransactionResourceError"]:
        pass
    else:
        raise AssertionError("failed local recovery was silently committed")
    assert path.read_bytes() == b"original" and root.entries == {} and root.resource is None
    assert db.execute("SELECT value FROM marker").fetchone()[0] == "old"
    assert db._transaction_resources is None

    @ns["_atomic_archive_apply"]
    def caught_nested_failure(conn):
        apply(conn, b"accepted before inner failure")

        def fail_preimage(path, content):
            if content in (b"original", b"accepted before inner failure"):
                raise OSError("nested undo refusal")
            replace(path, content)

        ns["_replace_cover"] = fail_preimage
        try:
            apply(conn, b"unrestored inner bytes", True)
        except OSError:
            pass
        else:
            raise AssertionError("nested undo failure was not injected")

    db.execute("UPDATE marker SET value='uncommitted caller work'")
    try:
        caught_nested_failure(db)
    except OSError:
        pass
    else:
        raise AssertionError("caught nested recovery failure escaped as outer success")
    assert path.read_bytes() == b"unrestored inner bytes" and db._transaction_resources is not None
    assert db.execute("SELECT value FROM marker").fetchone()[0] == "uncommitted caller work"
    ns["_replace_cover"] = replace
    try:
        db.commit()
    except resources["TransactionResourceError"]:
        pass
    else:
        raise AssertionError("caught nested recovery failure allowed caller work to commit")
    assert path.read_bytes() == b"original" and db._transaction_resources is None
    assert db.execute("SELECT value FROM marker").fetchone()[0] == "old"
    apply(db, b"closed")
    db.close()
    assert path.read_bytes() == b"original"
    db = connection()
    apply(db, b"committed")
    fence = db._transaction_resources.fence

    def observe_commit():
        assert not db.in_transaction and db._transaction_resources is None and fence.fd is None
        assert path.read_bytes() == b"committed"

    db._after_commit.append(observe_commit)
    db.commit()
    db.close()
    assert path.read_bytes() == b"committed"
    raw = sqlite3.connect(":memory:")
    raw.execute("CREATE TABLE refusal(value TEXT)")
    raw.execute("INSERT INTO refusal VALUES('active raw transaction')")
    commit_only = SimpleNamespace(_after_commit=[], in_transaction=True)
    list_only = SimpleNamespace(_after_commit=[], _after_rollback=[], in_transaction=True)
    for unsafe in (raw, commit_only, list_only):
        try:
            register(unsafe, publication_dir=config.COVERS_DIR, after_commit=lambda: None, after_rollback=lambda: None)
        except resources["TransactionResourceError"]:
            pass
        else:
            raise AssertionError("unsafe critical-resource connection accepted")
    assert commit_only._after_commit == list_only._after_commit == list_only._after_rollback == []
    assert getattr(list_only, "_transaction_resources", None) is None
    raw.close()
    print("PASS covers TEST MODEL ONLY: AST critical/ordinary order and fence lifetime, deferred-FK/auto-abort, "
          "failed-local-undo commit refusal, close/commit, sequential/nested undo, row retry, raw/list-only refusal")


def crypto_probe(directory):
    crypto = dict(base64=base64, hashlib=hashlib, Fernet=Fernet, InvalidToken=InvalidToken)
    tree = ast.parse((ROOT / "app/crypto.py").read_text())
    constants = [node for node in tree.body if
                 (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "SENSITIVE_KEYS")
                 or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_FERNET_PREFIX"
                                                        for target in node.targets))]
    exec(compile(ast.Module(body=constants, type_ignores=[]), "crypto_constants", "exec"), crypto)
    extract("app/crypto.py", {"_fernet", "encrypt_value", "is_fernet_token", "normalize_sensitive_settings"}, crypto)
    encrypt = crypto["encrypt_value"]
    source, dest_enc, dest_sign = "synthetic-source-jwt", "synthetic-destination-encryption", "synthetic-destination-signing"
    db = sqlite3.connect(directory / "staged-probe.db")
    db.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)")
    unknown = encrypt("foreign", "synthetic-unavailable-key")
    db.executemany("INSERT INTO settings VALUES(?,?)", [
        ("secret_key", source), ("abs_token", encrypt("source legacy plaintext", source)),
        ("romm_api_token", "native alias plaintext"), ("romm_token", "fork alias plaintext"),
        ("hardcover_token", unknown),
    ])
    db.commit()
    database = SimpleNamespace(inspect_schema_lineage=lambda conn: "integrated",
                               validate_and_migrate_staged_connection=lambda conn: {"lineage": "integrated"})
    author_index = SimpleNamespace(rebuild_all=lambda conn: None)

    def imports(name, *args, **kwargs):
        if name == "app.database":
            return database
        if name == "app.crypto":
            return SimpleNamespace(**crypto)
        if name == "app.services":
            return SimpleNamespace(author_index=author_index)
        raise AssertionError(f"Unexpected import in staged preparation: {name}")

    stage_ns = dict(sqlite3=sqlite3, _validate_restore_schema=lambda conn: None,
                    __builtins__={**vars(builtins), "__import__": imports})
    extract("app/routers/settings.py", {"_prepare_restore"}, stage_ns)
    result = stage_ns["_prepare_restore"](db, encryption_key=dest_enc, signing_key=dest_sign)
    assert result == {"rewritten": 3, "undecryptable": ["hardcover_token"]}
    rows = dict(db.execute("SELECT key,value FROM settings"))
    assert "secret_key" not in rows and rows["hardcover_token"] == unknown
    expected = {"abs_token": "source legacy plaintext", "romm_api_token": "native alias plaintext", "romm_token": "fork alias plaintext"}
    for key, plaintext in expected.items():
        assert crypto["_fernet"](dest_enc).decrypt(rows[key].encode()).decode() == plaintext
        assert plaintext.encode() not in (directory / "staged-probe.db").read_bytes()
        try:
            crypto["_fernet"](source).decrypt(rows[key].encode())
        except InvalidToken:
            pass
        else:
            raise AssertionError("source signing key adopted as destination encryption key")
    assert source.encode() not in (directory / "staged-probe.db").read_bytes()
    assert crypto["normalize_sensitive_settings"](db, encryption_key=dest_enc, signing_key=dest_sign) == {
        "rewritten": 0, "undecryptable": ["hardcover_token"],
    }
    db.close()
    # Resolve only synthetic existing material. No environment/keyfile access
    # from this probe reaches the host, and every generation rail is forbidden.
    def forbidden(*args):
        raise AssertionError("staged restore attempted key generation")

    crypto.update(os=SimpleNamespace(environ={}), _cached_encryption_key=None,
                  _read_keyfile=lambda name: {"encryption.key": dest_enc, "signing.key": dest_sign}.get(name),
                  _read_or_create_keyfile=forbidden, _write_keyfile=forbidden)
    extract("app/crypto.py", {"get_existing_encryption_key"}, crypto)
    auth = SimpleNamespace(_cached_secret_key=None, SECRET_KEY="", SIGNING_KEY_FILE="signing.key", get_secret_key=forbidden)

    def key_imports(name, *args, **kwargs):
        assert name == "app"
        return SimpleNamespace(auth=auth, crypto=SimpleNamespace(**crypto))

    key_ns = {"__builtins__": {**vars(builtins), "__import__": key_imports}}
    extract("app/routers/settings.py", {"_restore_destination_keys"}, key_ns)
    assert key_ns["_restore_destination_keys"]() == (dest_enc, dest_sign)
    crypto["_read_keyfile"] = lambda name: None
    try:
        key_ns["_restore_destination_keys"]()
    except ValueError as exc:
        assert "existing destination encryption key" in str(exc)
    else:
        raise AssertionError("restore generated a missing encryption key")
    crypto["_cached_encryption_key"] = dest_enc
    try:
        key_ns["_restore_destination_keys"]()
    except ValueError as exc:
        assert "existing destination signing key" in str(exc)
    else:
        raise AssertionError("restore generated a missing signing key")
    print("PASS crypto: staged source-JWT conversion, both aliases, unknown preservation/report, idempotence, source-key/plaintext bytes removed")
    print("PASS key capture: explicit existing destination keys; missing encryption/signing keys refuse without generation")


def copy_probe():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE items(id INTEGER PRIMARY KEY,location_id INTEGER); INSERT INTO items VALUES(1,NULL); "
                     "CREATE TABLE copies_live(id INTEGER PRIMARY KEY,item_id INTEGER,copy_number INTEGER,location_id INTEGER,is_primary INTEGER,"
                     "position_order INTEGER,condition TEXT,acquired_date TEXT,acquisition_source TEXT,acquisition_price REAL,provenance TEXT,"
                     "notes TEXT,missing_since TEXT,copy_barcode TEXT);")
    calls = []

    def insert_copy(conn, fields, **kwargs):
        return conn.execute("INSERT INTO copies_live(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")",
                            list(fields.values())).lastrowid

    def native_seam_adapter(conn, item_id, node_id):
        # Explicit fixture binding; there is deliberately NO integer/name match.
        assert node_id == 182
        calls.append((item_id, node_id))
        conn.execute("UPDATE items SET location_id=81 WHERE id=?", (item_id,))

    ns = dict(item_copies=SimpleNamespace(delete_copies_for_item=lambda conn, ident: None, insert_copy=insert_copy,
                                         _sync_legacy_seam=native_seam_adapter),
              _copy_location_id=lambda conn, copy, resolver: 182)
    extract("app/services/archive.py", {"_import_copies"}, ns)
    ns["_import_copies"](db, 1, [{"copy_number": 4, "is_primary": 1, "position_order": 17}], lambda name: 81, [], 1, "probe")
    assert calls == [(1, 182)] and db.execute("SELECT location_id FROM items").fetchone()[0] == 81
    assert db.execute("SELECT copy_number,location_id,position_order FROM copies_live").fetchone()[:] == (4, 182, 17)
    db.execute("DELETE FROM copies_live")
    calls.clear()
    ns["_import_copies"](db, 1, [], lambda name: 81, [], 1, "zero copy")
    assert calls == [] and db.execute("SELECT location_id FROM items").fetchone()[0] == 81
    db.close()
    print("PASS copy replay: final primary explicitly handed to native seam adapter; zero-copy seam untouched (adapter fixture)")


def main():
    with tempfile.TemporaryDirectory(prefix="import-restore-review-probes-", dir=ROOT / "execution") as name:
        directory = Path(name)
        export_probe(directory)
        cover_probe(directory)
        crypto_probe(directory)
        copy_probe()
    print("ISOLATED PROBES ONLY: central hooks, lineage migrations, real item funnel and HTTP application remain unverified until freeze")


if __name__ == "__main__":
    main()
