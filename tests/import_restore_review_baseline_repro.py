"""Reproduce all four faults against the captured PRE-EDIT owned bytes.

Deliberately exits 1 once all four baseline bugs are confirmed. This is not a
passing test or application verification; it never imports an app module.
"""

import ast
import builtins
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

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "execution/import-restore-review-before"


def extract(path, names, namespace):
    tree = ast.parse((BASELINE / path).read_text())
    code = ast.Module(body=[node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                           and node.name in names], type_ignores=[])
    exec(compile(code, path, "exec"), namespace)


def main():
    with tempfile.TemporaryDirectory(dir=ROOT / "execution", prefix="import-review-baseline-") as directory:
        config = SimpleNamespace(DATA_DIR=Path(directory), COVERS_DIR=Path(directory) / "covers")
        payload = {"actor": "A"}
        ns = dict(Path=Path, config=config, json=json, zipfile=zipfile, datetime=datetime, timezone=timezone,
                  FORMAT_NAME="shelf-archive", FORMAT_VERSION=3, current_actor=lambda: None,
                  _build_items=lambda db: ([{"personal_state": {"personal_notes": payload["actor"]}}], {}, []))
        for suffix in ("locations", "tags", "borrowers", "series", "checkouts", "valuation_history"):
            ns["_fetch_" + suffix] = lambda *args: []
        ns["_fetch_reading_log"] = lambda *args: [{"notes": payload["actor"]}]
        extract("app/services/archive.py", {"archive_tmp_path", "build_archive"}, ns)
        first = ns["build_archive"](None)
        payload["actor"] = "B"
        second = ns["build_archive"](None)
        with zipfile.ZipFile(first) as archive:
            observed = json.loads(archive.read("library.json"))
        assert first == second and observed["reading_log"][0]["notes"] == "B"
        first.unlink()
        assert not second.exists()
        print("REPRODUCED: delayed A response reads B private payload; A cleanup deletes B file")

        @contextmanager
        def scope(db):
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            db.execute("SAVEPOINT probe")
            try:
                yield
                db.execute("RELEASE probe")
            except BaseException:
                db.execute("ROLLBACK TO probe")
                db.execute("RELEASE probe")
                raise

        config.COVERS_DIR.mkdir()
        path = config.COVERS_DIR / "1.jpg"
        path.write_bytes(b"original handpicked cover")
        ns = dict(config=config, os=os, wraps=wraps, _cover_journals=ContextVar("baseline", default=()),
                  item_write=SimpleNamespace(_write_scope=scope), ArchiveReader=object)
        extract("app/services/archive.py", {"_undo_covers", "_atomic_archive_apply", "_install_cover"}, ns)
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE items(id INTEGER PRIMARY KEY,title TEXT)")

        @ns["_atomic_archive_apply"]
        def apply(conn):
            conn.execute("INSERT INTO items VALUES(1,'uncommitted')")
            ns["_install_cover"](SimpleNamespace(read_cover=lambda name: b"new archive cover"), 1, "cover")

        apply(db)
        db.rollback()
        assert not db.execute("SELECT 1 FROM items").fetchone()
        assert path.read_bytes() == b"new archive cover"
        db.close()
        print("REPRODUCED: successful apply then outer rollback loses original cover bytes")

        stage = sqlite3.connect(":memory:")
        stage.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)")
        stage.executemany("INSERT INTO settings VALUES(?,?)", [("secret_key", "synthetic-source-signing-key"),
                                                              ("romm_token", "synthetic-plaintext")])
        stage.commit()
        database = SimpleNamespace(_run_migrations=lambda conn: None, inspect_schema_lineage=lambda conn: "integrated")

        def imports(name, *args, **kwargs):
            if name == "app.database":
                return database
            if name == "app.services":
                return SimpleNamespace(author_index=SimpleNamespace(rebuild_all=lambda conn: None))
            raise AssertionError(f"Unexpected import: {name}")

        ns = dict(sqlite3=sqlite3, __builtins__={**vars(builtins), "__import__": imports},
                  _validate_restore_schema=lambda conn: None)
        extract("app/routers/settings.py", {"_prepare_restore"}, ns)
        ns["_prepare_restore"](stage)
        assert stage.execute("SELECT value FROM settings WHERE key='secret_key'").fetchone()[0] == "synthetic-source-signing-key"
        assert stage.execute("SELECT value FROM settings WHERE key='romm_token'").fetchone()[0] == "synthetic-plaintext"
        stage.close()
        print("REPRODUCED: prepared staging retains source signing key and plaintext sensitive alias")

        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("CREATE TABLE items(id INTEGER PRIMARY KEY,location_id INTEGER); INSERT INTO items VALUES(1,NULL); "
                         "CREATE TABLE item_copies(id INTEGER PRIMARY KEY,item_id INTEGER,copy_number INTEGER,location_id INTEGER,"
                         "is_primary INTEGER,position_order INTEGER,condition TEXT,acquired_date TEXT,acquisition_source TEXT,"
                         "acquisition_price REAL,provenance TEXT,notes TEXT,missing_since TEXT,copy_barcode TEXT);")

        def insert_copy(conn, fields, **kwargs):
            return conn.execute("INSERT INTO item_copies(" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")",
                                list(fields.values())).lastrowid

        ns = dict(item_copies=SimpleNamespace(delete_copies_for_item=lambda conn, ident: None, insert_copy=insert_copy),
                  _copy_location_id=lambda conn, copy, resolver: 182)
        extract("app/services/archive.py", {"_import_copies"}, ns)
        ns["_import_copies"](db, 1, [{"copy_number": 1, "is_primary": 1, "position_order": 17}], lambda name: 81, [], 1, "probe")
        assert db.execute("SELECT location_id FROM items").fetchone()[0] is None
        assert db.execute("SELECT location_id,position_order FROM item_copies").fetchone()[:] == (182, 17)
        db.close()
        print("REPRODUCED: final primary NODE is not projected to legacy item location seam")
    print("Expected pre-fix failures: 4. Baseline-only AST/SQL reproduction, not app verification.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
