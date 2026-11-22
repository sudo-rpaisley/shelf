"""Owned-input SQL regressions, runnable WITHOUT importing the application.

The loader executes physical-owned modules against an explicit SQLite domain
fixture; do NOT run it while application execution is held. Security,
core item writing and central schema are interface doubles, NOT proof of their
implementations; full HTTP/security/schema gates belong to post-freeze pytest.
No sys.modules substitution, global app patches, provider or application DB.
Disposable SQLite files let a rival connection prove reserved-lock ownership;
idle/deferred transactions alone are not evidence of a write lock.
"""

import asyncio
import ast
import builtins
import json
import re
import sqlite3
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DDL = """
PRAGMA foreign_keys = ON;
CREATE TABLE locations(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, sort_order INTEGER DEFAULT 0);
CREATE TABLE location_nodes(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES location_nodes(id) ON DELETE RESTRICT,
 name TEXT NOT NULL, sort_order INTEGER DEFAULT 0, legacy_location_id INTEGER UNIQUE REFERENCES locations(id) ON DELETE SET NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE UNIQUE INDEX roots ON location_nodes(name COLLATE NOCASE) WHERE parent_id IS NULL;
CREATE UNIQUE INDEX children ON location_nodes(parent_id,name COLLATE NOCASE) WHERE parent_id IS NOT NULL;
CREATE TABLE items(id INTEGER PRIMARY KEY, title TEXT, media_type TEXT DEFAULT 'book', owned INTEGER DEFAULT 1,
 location_id INTEGER REFERENCES locations(id), deleted_at TEXT,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
 visible INTEGER DEFAULT 1, permission INTEGER DEFAULT 3, isbn TEXT, upc TEXT, authors TEXT, cover_path TEXT,
 series_name TEXT, series_position REAL, publish_year INTEGER, abs_id TEXT, abs_library_id TEXT,
 komga_id TEXT, komga_library_id TEXT, romm_id TEXT, romm_platform_id TEXT);
CREATE TABLE libraries(id INTEGER PRIMARY KEY);
INSERT INTO libraries VALUES(1),(2);
CREATE TABLE library_items(item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
 library_id INTEGER NOT NULL REFERENCES libraries(id));
CREATE TABLE item_copies(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 copy_number INTEGER NOT NULL, location_id INTEGER REFERENCES location_nodes(id) ON DELETE SET NULL,
 position_order INTEGER, condition TEXT, notes TEXT, acquired_date TEXT, acquisition_price REAL,
 acquisition_source TEXT, provenance TEXT, copy_barcode TEXT UNIQUE, is_primary INTEGER DEFAULT 0,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
 deleted_at TEXT, missing_since TEXT, UNIQUE(item_id,copy_number));
CREATE UNIQUE INDEX one_primary ON item_copies(item_id) WHERE is_primary = 1;
CREATE TEMP VIEW items_live AS SELECT * FROM items WHERE deleted_at IS NULL AND visible = 1;
CREATE TEMP VIEW copies_live AS SELECT c.* FROM item_copies c JOIN items_live i ON i.id=c.item_id WHERE c.deleted_at IS NULL;
CREATE TABLE digital_holdings(id INTEGER PRIMARY KEY, item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
 provider TEXT, external_id TEXT, library_id TEXT, format TEXT, source_url TEXT,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(item_id,provider,external_id));
CREATE TABLE komga_records(komga_id TEXT PRIMARY KEY, item_id INTEGER REFERENCES items(id), library_id TEXT);
CREATE TABLE romm_records(romm_id TEXT PRIMARY KEY, item_id INTEGER REFERENCES items(id), platform_id TEXT);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
INSERT INTO settings VALUES('holdings_foundation_backfill_v1','1');
CREATE TABLE audit_sessions(id INTEGER PRIMARY KEY, location_id INTEGER REFERENCES location_nodes(id) ON DELETE SET NULL,
 scope TEXT, location_name TEXT, started_at TEXT DEFAULT CURRENT_TIMESTAMP, finished_at TEXT, notes TEXT);
CREATE TABLE audit_entries(id INTEGER PRIMARY KEY, session_id INTEGER REFERENCES audit_sessions(id) ON DELETE CASCADE,
 item_id INTEGER REFERENCES items(id) ON DELETE SET NULL, copy_id INTEGER REFERENCES item_copies(id) ON DELETE SET NULL,
 location_id INTEGER REFERENCES location_nodes(id) ON DELETE SET NULL, raw_code TEXT, result TEXT, copy_specific INTEGER,
 security_library_id INTEGER, security_user_id INTEGER,
 scanned_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE checkouts(item_id INTEGER, checked_in TEXT);
INSERT INTO locations(id,name) VALUES(1,'Legacy A'),(2,'Legacy B');
INSERT INTO location_nodes(id,name,legacy_location_id) VALUES(40,'Legacy A',1),(1,'Legacy B',2);
INSERT INTO location_nodes(id,parent_id,name) VALUES(41,40,'Shelf A'),(42,1,'Shelf B');
"""


class Refused(Exception):
    def __init__(self, status_code, detail, headers=None):
        self.status_code, self.detail = status_code, detail
        self.headers = headers or {}
        super().__init__(detail)


class UnknownLocation(ValueError):
    def __init__(self, message, *, value=None):
        super().__init__(message)
        self.value = value


class Router:
    def __init__(self, *args, **kwargs):
        self.routes = []

    def __getattr__(self, method):
        def route(path, **kwargs):
            def register(fn):
                self.routes.append((method, path, fn.__name__))
                return fn
            return register
        return route


class Response:
    def __init__(self, content="", status_code=200, **kwargs):
        self.body = str(content).encode()
        self.status_code, self.headers = status_code, {}
        self.context = kwargs.get("context", {})


class Harness:
    def __init__(self):
        # A real competing connection can distinguish a reserved write lock
        # from an idle/deferred read transaction. in_transaction cannot do so.
        self.directory = tempfile.TemporaryDirectory(prefix="physical-bindings-006-")
        self.database = Path(self.directory.name) / "isolated.sqlite"
        self.db = sqlite3.connect(self.database, timeout=0)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(DDL)
        self.modules, self.authorization, self.schema_calls = {}, [], 0
        self.events, self.check_mutation_authorization = [], False
        self.db.set_trace_callback(lambda sql: self.events.append(("sql", sql)))
        self.actor = None
        self.services = types.ModuleType("app.services")
        self.services.__getattr__ = lambda name: self.load("app.services." + name)
        self.modules["app.services"] = self.services
        self.stub("fastapi", HTTPException=Refused, APIRouter=Router, Request=object,
                  Depends=lambda value: value, Form=lambda value=None, **kwargs: value,
                  Query=lambda value=None, **kwargs: value)
        self.stub("fastapi.responses", HTMLResponse=Response, Response=Response,
                  JSONResponse=Response, RedirectResponse=Response)
        self.stub("app.auth", require_role=lambda role: ("global", role))
        self.stub("app.features", require_feature=lambda key: ("feature", key), feature_enabled=lambda key: True)
        self.stub("app.database", ensure_holdings_schema=self.ensure_schema,
                  get_setting=lambda db, key: (db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone() or [None])[0],
                  get_db=self.connection)
        self.stub("app.media_types", is_physical_media=lambda media: media in {"book", "comic", "music_cd", "kids_book"})
        self.stub("app.services.catalogue_security", require_item_role=self.require,
                  require_catalogue_role=lambda role: ("catalogue", role), current_actor=lambda: self.actor)
        self.stub("app.services.libraries", has_library_role=self.has_library_role,
                  has_item_role=self.has_item_role, accessible_library_ids=self.accessible_library_ids,
                  _account_admin=lambda user: user.get("account_role", user.get("role")) == "admin")
        self.stub("app.services.write_targets", UnknownLocationError=UnknownLocation, ItemValueError=ValueError)
        self.stub("app.services.code128")  # Imported by labels; printing is not exercised here.
        self.stub("app.services.trash", invalidate=lambda db: None, settle_marker=lambda db: None)
        self.stub("app.services.item_write", update_item_fields=self.item_update)
        self.stub("app.routers.items_common", _toast_header=lambda message, kind: json.dumps({kind: message}))
        self.routers = self.stub("app.routers", items_common=self.modules["app.routers.items_common"])
        self.routers.__getattr__ = lambda name: self.load("app.routers." + name)

    def stub(self, name, **attrs):
        module = types.ModuleType(name)
        vars(module).update(attrs)
        self.modules[name] = module
        return module

    def load(self, name):
        if name in self.modules:
            return self.modules[name]
        relative = name.replace(".", "/") + ".py"
        allowed = {
            "app/services/" + stem + ".py" for stem in
            ("item_copies", "holdings", "location_tree", "locations", "location_order", "copy_labels", "audits")
        } | {"app/routers/" + stem + ".py" for stem in ("item_copies", "labels", "audits", "location_order")}
        if relative not in allowed:
            raise AssertionError("Isolated loader attempted an unowned import: " + name)
        module = types.ModuleType(name)
        self.modules[name] = module

        def importer(import_name, globals=None, locals=None, fromlist=(), level=0):
            if import_name.startswith("app.") or import_name == "fastapi" or import_name.startswith("fastapi."):
                return self.load(import_name)
            return builtins.__import__(import_name, globals, locals, fromlist, level)

        module.__dict__["__builtins__"] = {**vars(builtins), "__import__": importer}
        exec(compile((ROOT / relative).read_text(), relative, "exec"), vars(module))
        return module

    @contextmanager
    def connection(self):
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def ensure_schema(self, db):
        self.schema_calls += 1
        db.execute("SELECT 1 FROM item_copies LIMIT 0")

    def require(self, db, item_id, role, user=None, include_deleted=False):
        # This is a permission check, including on idle edit GETs and on the
        # read-only re-render AFTER a failed mutation has rolled back. Explicit
        # mutation tests below separately require a real reserved write lock.
        if self.check_mutation_authorization and role != "viewer":
            self.assert_reserved_write_lock(db)
        self.authorization.append((item_id, role, include_deleted))
        self.events.append(("authorize", item_id, role, include_deleted))
        row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None or not row["visible"] or (row["deleted_at"] and not include_deleted):
            raise Refused(404, "Item not found")
        principal = self.actor if self.actor is not None else user
        if principal is not None:
            if not self.has_item_role(db, principal, item_id, "viewer"):
                raise Refused(404, "Item not found")
            if not self.has_item_role(db, principal, item_id, role):
                raise Refused(403, "Forbidden")
        if row["permission"] < {"viewer": 1, "editor": 2, "admin": 3}[role]:
            raise Refused(403, "Forbidden")
        return row

    def assert_reserved_write_lock(self, db):
        assert db is self.db
        rival = sqlite3.connect(self.database, timeout=0)
        try:
            try:
                rival.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert exc.sqlite_errorcode == sqlite3.SQLITE_BUSY, str(exc)
            else:
                rival.rollback()
                raise AssertionError("mutation authorization ran before a reserved write lock")
        finally:
            rival.close()

    @contextmanager
    def mutation_authorization(self):
        assert not self.check_mutation_authorization
        self.check_mutation_authorization = True
        try:
            yield
        finally:
            self.check_mutation_authorization = False

    def has_library_role(self, db, user, library_id, role="viewer"):
        if not db.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone():
            return False
        if user.get("api_token_id") and role != "viewer":
            return False
        effective = ("admin" if user.get("account_role", user.get("role")) == "admin"
                     else user.get("library_roles", {}).get(library_id))
        levels = {None: 0, "viewer": 1, "editor": 2, "admin": 3}
        return levels[effective] >= levels[role]

    def has_item_role(self, db, user, item_id, role="viewer"):
        row = db.execute("SELECT library_id FROM library_items WHERE item_id=?", (item_id,)).fetchone()
        return row is not None and self.has_library_role(db, user, row[0], role)

    def accessible_library_ids(self, db, user):
        return [row[0] for row in db.execute("SELECT id FROM libraries")
                if self.has_library_role(db, user, row[0], "viewer")]

    def item_update(self, db, item_id, fields):
        self.require(db, item_id, "editor")
        db.execute("UPDATE items SET location_id=? WHERE id=?", (fields["location_id"], item_id))
        self.load("app.services.item_copies").sync_primary_location(db, item_id, fields["location_id"])

    def item(self, item_id=10, legacy=1, **values):
        library_id = values.pop("_library_id", 1)
        values = {"id": item_id, "title": f"Item {item_id}", "location_id": legacy, **values}
        self.db.execute(f"INSERT INTO items ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))
        self.db.execute("INSERT INTO library_items VALUES(?,?)", (item_id, library_id))
        return item_id

    def request(self, form=None):
        async def get_form():
            return form or {}

        def template(request, name, context):
            response = Response(context=context)
            response.template_name = name
            return response

        return types.SimpleNamespace(form=get_form, app=types.SimpleNamespace(
            state=types.SimpleNamespace(templates=types.SimpleNamespace(TemplateResponse=template))))


class PhysicalDomainIsolation(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.db = self.h.db
        self.c = self.h.load("app.services.item_copies")
        self.t = self.h.load("app.services.location_tree")
        self.holdings = self.h.load("app.services.holdings")

    def tearDown(self):
        self.db.close()
        self.h.directory.cleanup()

    def row(self, copy_id):
        return self.db.execute("SELECT * FROM item_copies WHERE id=?", (copy_id,)).fetchone()

    def initial(self, **values):
        item_id = self.h.item(**values)
        self.holdings.sync_item(self.db, item_id, create_copy=True)
        return self.db.execute("SELECT id FROM copies_live WHERE item_id=?", (item_id,)).fetchone()[0]

    def _alias_fixture(self):
        first = self.initial(updated_at="2001-01-02 03:04:05")
        self.c.update_copy(self.db, first, {
            "location_id": 41, "position_order": 7, "copy_barcode": "ALIAS-PRECISE",
            "condition": "Collector A+", "notes": "Signed copy", "acquisition_price": 12.5,
            "acquired_date": "2020-02-03", "provenance": "Original history"})
        historical = self.c.add_copy(self.db, 10, {
            "location_id": 41, "position_order": 9, "copy_barcode": "ALIAS-COPY-TRASH"})
        self.c.trash_copy(self.db, historical, at="2021-03-04 05:06:07", replay=True)
        self.h.item(11, updated_at="2002-03-04 05:06:07")
        trashed_parent_copy = self.c.add_copy(self.db, 11, {
            "location_id": 41, "position_order": 2, "copy_barcode": "ALIAS-ITEM-TRASH"})
        self.db.execute("UPDATE items SET deleted_at='2022-04-05 06:07:08' WHERE id=11")
        root = self.initial(item_id=13, updated_at="2003-04-05 06:07:08")
        unrelated = self.initial(item_id=12, legacy=2, updated_at="2004-05-06 07:08:09")
        self.c.update_copy(self.db, unrelated, {"position_order": 11, "copy_barcode": "ALIAS-UNRELATED"})
        return first, historical, trashed_parent_copy, root, unrelated

    def _alias_snapshot(self):
        return {table: [dict(row) for row in self.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("items", "item_copies", "location_nodes", "locations", "library_items", "settings")}

    def test_unequal_and_equal_but_unrelated_ids_are_never_coalesced(self):
        copy_id = self.initial()
        self.assertEqual(self.row(copy_id)["location_id"], 40)
        self.c.sync_primary_location(self.db, 10, 1)
        self.assertEqual(self.row(copy_id)["location_id"], 40)
        self.c.sync_primary_location(self.db, 10, 2)
        self.assertEqual(self.row(copy_id)["location_id"], 1)
        self.assertEqual(self.t.node_to_legacy_location(self.db, 1), 2)
        self.assertEqual(self.t.legacy_location_to_node(self.db, 1), 40)

    def test_precise_move_updates_only_legacy_ancestor_and_preserves_metadata(self):
        copy_id = self.initial()
        self.c.update_copy(self.db, copy_id, {"copy_barcode": "SIGNED", "notes": "note", "position_order": 8,
                           "acquisition_price": 12.5, "acquisition_source": "Shop", "provenance": "Signed"})
        self.c.relocate_copy(self.db, self.row(copy_id), 42)
        row = self.row(copy_id)
        self.assertEqual(row["location_id"], 42)
        self.assertIsNone(row["position_order"])
        self.assertEqual((row["copy_barcode"], row["notes"], row["acquisition_price"], row["provenance"]),
                         ("SIGNED", "note", 12.5, "Signed"))
        self.assertEqual(self.db.execute("SELECT location_id FROM items WHERE id=10").fetchone()[0], 2)
        self.holdings.sync_item(self.db, 10)
        self.assertEqual(self.row(copy_id)["location_id"], 42)

    def test_unlocated_owned_physical_initial_copy_but_never_digital_or_unowned(self):
        copy_id = self.initial(legacy=None)
        self.assertEqual(self.row(copy_id)["is_primary"], 1)
        self.assertIsNone(self.row(copy_id)["location_id"])
        for item_id, fields in [(11, {"media_type": "ebook"}), (12, {"owned": 0})]:
            self.h.item(item_id, **fields)
            self.holdings.sync_item(self.db, item_id, create_copy=True)
            with self.assertRaisesRegex(ValueError, "Only owned physical"):
                self.c.add_copy(self.db, item_id)
            self.assertEqual(self.c.copies_for_item(self.db, item_id), [])

    def test_ensure_and_metadata_sync_never_recreate_intentionally_absent_copy(self):
        self.initial()
        self.c.delete_copies_for_item(self.db, 10)
        for ensure in (self.holdings.install_schema, self.holdings.ensure_foundation,
                       self.holdings.sync_all_holdings, self.c.backfill_legacy_locations):
            ensure(self.db)
        self.holdings.sync_item(self.db, 10)
        self.assertEqual(self.c.copies_for_item(self.db, 10), [])
        self.assertEqual(self.db.execute("SELECT value FROM settings WHERE key='holdings_foundation_backfill_v1'").fetchone()[0], "1")

    def test_primary_trash_promotes_and_last_owned_copy_is_refused(self):
        first = self.initial()
        second = self.c.add_copy(self.db, 10, {"location_id": 42, "position_order": 6, "copy_barcode": "TWO"})
        result = self.c.trash_copy(self.db, first)
        self.assertEqual(result["promoted_copy_id"], second)
        self.assertIsNotNone(self.row(first)["deleted_at"])
        self.assertEqual((self.row(second)["is_primary"], self.row(second)["position_order"]), (1, 6))
        self.assertEqual(self.db.execute("SELECT location_id FROM items WHERE id=10").fetchone()[0], 2)
        with self.assertRaisesRegex(ValueError, "must keep at least one copy"):
            self.c.trash_copy(self.db, second)
        with self.assertRaisesRegex(ValueError, "must keep at least one copy"):
            self.c.trash_copy(self.db, second, at="2026-09-03 12:00:00")
        self.assertIsNone(self.row(second)["deleted_at"])

    def test_explicit_archive_replay_preserves_history_without_auto_recreation(self):
        self.h.item(10, owned=0)
        archived = self.c.insert_copy(self.db, {"item_id": 10, "copy_number": 9,
                                      "provenance": "Old physical history", "copy_barcode": "ARCHIVED"}, replay=True)
        self.c.trash_copy(self.db, archived, at="2026-09-03 12:00:00", replay=True)
        self.db.execute("UPDATE items SET owned=1 WHERE id=10")
        self.holdings.sync_item(self.db, 10, create_copy=True)
        self.assertEqual(self.c.copies_for_item(self.db, 10), [])
        self.assertEqual(self.row(archived)["deleted_at"], "2026-09-03 12:00:00")
        self.assertEqual(self.c.restore_copy(self.db, archived)["copy_id"], archived)
        self.assertEqual(self.row(archived)["provenance"], "Old physical history")

    def test_missing_trash_restore_retains_same_id_barcode_and_acquisition(self):
        first = self.initial()
        self.c.add_copy(self.db, 10)
        self.c.update_copy(self.db, first, {"copy_barcode": "KEPT", "acquired_date": "2026-09-03", "provenance": "Signed"})
        self.assertTrue(self.c.mark_missing(self.db, first))
        marked = self.row(first)["missing_since"]
        self.assertFalse(self.c.mark_missing(self.db, first))
        self.c.trash_copy(self.db, first)
        self.assertFalse(self.c.clear_missing(self.db, first))
        self.assertIsNotNone(self.c.restore_copy(self.db, first))
        restored = self.row(first)
        self.assertEqual((restored["copy_barcode"], restored["acquired_date"], restored["provenance"], restored["missing_since"]),
                         ("KEPT", "2026-09-03", "Signed", marked))
        self.assertTrue(self.c.clear_missing(self.db, first))
        self.assertIsNone(self.row(first)["missing_since"])

    def test_trashed_parent_and_hidden_parent_cannot_restore_copy(self):
        first = self.initial()
        self.c.add_copy(self.db, 10)
        self.c.trash_copy(self.db, first)
        self.db.execute("UPDATE items SET deleted_at=CURRENT_TIMESTAMP WHERE id=10")
        self.assertIsNone(self.c.restore_copy(self.db, first))
        self.db.execute("UPDATE items SET deleted_at=NULL,visible=0 WHERE id=10")
        with self.assertRaises(Refused) as error:
            self.c.restore_copy(self.db, first)
        self.assertEqual(error.exception.status_code, 404)
        self.assertIsNotNone(self.row(first)["deleted_at"])

    def test_structural_reparent_preserves_trashed_metadata_and_authorizes_both_parents(self):
        first = self.initial()
        self.c.add_copy(self.db, 10)
        self.c.update_copy(self.db, first, {"copy_barcode": "REPARENT", "provenance": "History"})
        self.c.trash_copy(self.db, first)
        deleted = self.row(first)["deleted_at"]
        self.h.item(11)
        self.c.update_copy(self.db, first, {"item_id": 11, "copy_number": 9, "is_primary": 0})
        self.assertEqual((self.row(first)["item_id"], self.row(first)["deleted_at"], self.row(first)["provenance"]),
                         (11, deleted, "History"))
        self.assertIn((10, "editor", True), self.h.authorization)
        self.assertIn((11, "editor", True), self.h.authorization)

    def test_plural_digital_ownership_survives_inline_projection_and_null(self):
        self.h.item(10, media_type="ebook", komga_id="inline-C")
        self.holdings.reconcile_digital_holdings(self.db, 10, "komga", [
            {"external_id": "plural-A", "source_url": "https://example.invalid/a"},
            {"external_id": "plural-B"}])
        self.holdings.sync_item(self.db, 10)
        self.db.execute("UPDATE items SET komga_id=NULL WHERE id=10")
        self.holdings.sync_item(self.db, 10)
        self.assertEqual({r[0] for r in self.db.execute("SELECT external_id FROM digital_holdings")},
                         {"plural-A", "plural-B", "inline-C"})

    def test_digital_projection_includes_every_sidecar_without_replacing_holding_ids(self):
        self.h.item(10, media_type="ebook", komga_id="inline-C", komga_library_id="remote-C")
        self.db.execute("INSERT INTO komga_records VALUES('sidecar-A',10,'remote-A'),('sidecar-B',10,'remote-B')")
        self.db.execute("INSERT INTO romm_records VALUES('romm-D',10,'platform-D')")
        self.holdings.reconcile_digital_holdings(self.db, 10, "komga", [{
            "external_id": "sidecar-A", "format": "cbz", "source_url": "https://example.invalid/kept"}])
        original = self.db.execute("SELECT id FROM digital_holdings WHERE external_id='sidecar-A'").fetchone()[0]
        self.holdings.sync_item(self.db, 10)
        self.holdings.sync_item(self.db, 10)
        rows = {r["external_id"]: r for r in self.db.execute("SELECT * FROM digital_holdings")}
        self.assertEqual(set(rows), {"inline-C", "sidecar-A", "sidecar-B", "romm-D"})
        self.assertEqual((rows["sidecar-A"]["id"], rows["sidecar-A"]["format"], rows["sidecar-A"]["source_url"]),
                         (original, "cbz", "https://example.invalid/kept"))
        self.assertEqual(rows["romm-D"]["library_id"], "platform-D")

    def test_ambiguous_projection_or_hidden_digital_collision_rolls_back_copy_creation(self):
        self.h.item(10, komga_id="clash", komga_library_id="remote-A")
        self.db.execute("INSERT INTO komga_records VALUES('clash',10,'remote-B')")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.holdings.sync_item(self.db, 10, create_copy=True)
        self.assertEqual(self.c.copies_for_item(self.db, 10), [])
        self.db.execute("DELETE FROM komga_records")
        self.h.item(11, visible=0)
        self.db.execute("INSERT INTO digital_holdings(item_id,provider,external_id) VALUES(11,'komga','clash')")
        with self.assertRaisesRegex(ValueError, "^Digital identity is unavailable$"):
            self.holdings.sync_item(self.db, 10, create_copy=True)
        self.assertEqual(self.c.copies_for_item(self.db, 10), [])

    def test_copy_and_legacy_seam_rollback_together_when_core_projection_refuses(self):
        first = self.initial()

        def fail_after_seam(db, item_id, fields):
            db.execute("UPDATE items SET location_id=? WHERE id=?", (fields["location_id"], item_id))
            raise ValueError("Core projection refused")

        self.h.modules["app.services.item_write"].update_item_fields = fail_after_seam
        with self.assertRaisesRegex(ValueError, "Core projection refused"):
            self.c.update_copy(self.db, first, {"location_id": 42, "provenance": "Not committed"})
        self.assertEqual(self.row(first)["location_id"], 40)
        self.assertIsNone(self.row(first)["provenance"])
        self.assertEqual(self.db.execute("SELECT location_id FROM items WHERE id=10").fetchone()[0], 1)

    def test_audit_copyless_legacy_fallback_cannot_match_equal_unrelated_node(self):
        self.h.item()
        audits = self.h.load("app.services.audits")
        self.assertEqual(audits.expected_rows(self.db, 1), [])
        rows = audits.expected_rows(self.db, 40)
        self.assertEqual([(r["item_id"], r["location_id"], r["copy_id"]) for r in rows], [(10, 40, None)])
        audit_id = audits.create_session(self.db, scope="location", location_id=1)
        self.assertEqual(audits.get_session(self.db, audit_id)["location_name"], "Legacy B")

    def test_audit_expected_sets_hide_trash_and_missing_marks_stay_copy_specific(self):
        first = self.initial()
        second = self.c.add_copy(self.db, 10, {"location_id": 41})
        audits = self.h.load("app.services.audits")
        audit_id = audits.create_session(self.db, scope="library", location_id=None)
        audits.add_entry(self.db, audit_id, item_id=10, copy_id=first, location_id=40, raw_code="x", result="confirmed")
        report = audits.build_report(self.db, audits.get_session(self.db, audit_id))
        self.assertEqual(report.missing_copy_ids(), {second})
        self.c.trash_copy(self.db, second)
        self.assertEqual({r["copy_id"] for r in audits.expected_rows(self.db, None)}, {first})
        self.db.execute("UPDATE items SET deleted_at=CURRENT_TIMESTAMP WHERE id=10")
        self.assertEqual(audits.expected_rows(self.db, None), [])

    def test_order_and_labels_are_node_scoped_and_leave_hidden_copies_untouched(self):
        first = self.initial()
        second = self.c.add_copy(self.db, 10, {"location_id": 40})
        self.h.item(11)
        hidden = self.c.add_copy(self.db, 11, {"location_id": 40, "position_order": 99})
        self.db.execute("UPDATE items SET visible=0 WHERE id=11")
        order = self.h.load("app.services.location_order")
        order.apply_copy_order(self.db, 40, [second, first])
        self.assertEqual(self.row(hidden)["position_order"], 99)
        with self.assertRaises(ValueError):
            order.apply_copy_order(self.db, 40, [first, second, hidden])
        labels = self.h.load("app.services.copy_labels")
        self.assertEqual(labels.count_scope(self.db, "location", 1), 0)
        self.assertEqual(labels.assign_codes(self.db, "SHF", "location", 40)[0], 2)
        self.assertIsNone(self.row(hidden)["copy_barcode"])

    def test_batch_authorization_precedes_first_order_or_label_write(self):
        first = self.initial()
        self.h.item(11, permission=3)
        other = self.c.add_copy(self.db, 11, {"location_id": 40})
        self.db.execute("UPDATE items SET permission=1 WHERE id=11")
        with self.assertRaises(Refused):
            self.h.load("app.services.location_order").apply_copy_order(self.db, 40, [other, first])
        self.assertIsNone(self.row(first)["position_order"])
        with self.assertRaises(Refused):
            self.h.load("app.services.copy_labels").assign_codes(self.db, "SHF", "all")
        self.assertIsNone(self.row(first)["copy_barcode"])

    def test_node_conflict_does_not_leave_a_legacy_location_inserted(self):
        locations = self.h.load("app.services.locations")
        locations.create_location(self.db, "Unrelated root")
        with self.assertRaises(locations.DuplicateLocation):
            locations.create_legacy_location(self.db, "Unrelated root")
        self.assertIsNone(locations.find_by_path(self.db, "Unrelated root"))

    def test_hidden_barcode_collision_is_a_safe_200_fragment_and_no_insert(self):
        self.initial()
        self.h.item(11, title="HIDDEN-LIBRARY-TITLE")
        hidden = self.c.add_copy(self.db, 11, {"copy_barcode": "PRIVATE-CODE"})
        self.db.execute("UPDATE items SET visible=0 WHERE id=11")
        self.db.commit()
        router = self.h.load("app.routers.item_copies")
        response = asyncio.run(router.add_copy(self.h.request(), 10, copy_barcode="PRIVATE-CODE"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["copy_error"], "That copy barcode is already in use")
        self.assertNotIn("HIDDEN-LIBRARY-TITLE", repr(response.context))
        self.assertEqual(len(self.c.copies_for_item(self.db, 10)), 1)
        self.assertEqual(self.row(hidden)["copy_barcode"], "PRIVATE-CODE")

    def test_url_parent_binding_and_omitted_metadata_are_preserved(self):
        first = self.initial()
        self.c.update_copy(self.db, first, {"provenance": "Old history", "notes": "Keep notes"})
        self.h.item(11)
        other = self.c.add_copy(self.db, 11)
        self.db.commit()
        router = self.h.load("app.routers.item_copies")
        before = list(self.db.iterdump())
        response = asyncio.run(router.update_copy(self.h.request({"condition": "good"}), 10, other, condition="good"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.template_name, "fragments/item_copies.html")
        self.assertEqual(response.context["copy_error"], "Copy not found")
        self.assertEqual(response.context["item_id"], 10)
        self.assertEqual([copy["id"] for copy in response.context["copies"]], [first])
        self.assertEqual(list(self.db.iterdump()), before)
        self.assertIsNone(self.row(other)["condition"])
        asyncio.run(router.update_copy(self.h.request({"condition": "good"}), 10, first, condition="good"))
        self.assertEqual((self.row(first)["condition"], self.row(first)["provenance"], self.row(first)["notes"]),
                         ("good", "Old history", "Keep notes"))
        self.assertTrue({"item", "item_id", "copies", "locations", "location_nodes", "conditions",
                         "missing", "is_physical_holding", "copy_error", "message", "error"}
                        <= router._copy_context(self.db, 10).keys())

    def test_audit_provenance_survives_purge_and_unknown_history_fails_closed(self):
        a = self.h.load("app.services.audits")
        self.h.item(10, legacy=None, _library_id=2)
        audit = a.create_session(self.db, scope="library", location_id=None)
        owner = {"id": 41, "role": "editor", "library_roles": {2: "editor"}}
        other = {"id": 42, "role": "editor", "library_roles": {1: "editor"}}
        self.h.actor = owner
        entry = a.add_entry(self.db, audit, item_id=10, copy_id=None, location_id=40,
                            raw_code="PRIVATE-PROVENANCE", result="elsewhere")
        a.add_entry(self.db, audit, item_id=None, copy_id=None, location_id=40,
                    raw_code="OWNER-UNMATCHED", result="not_owned")
        self.db.execute(
            "INSERT INTO audit_entries(session_id,item_id,raw_code,result,copy_specific) VALUES(?,NULL,?,'confirmed',0)",
            (audit, "UNKNOWN-LEGACY-ORPHAN"))
        snapshot = self.db.execute("SELECT security_library_id,security_user_id FROM audit_entries WHERE id=?", (entry,)).fetchone()
        self.assertEqual(tuple(snapshot), (2, 41))
        self.h.actor = other
        self.assertEqual(a.get_session(self.db, audit)["scans"], 0)
        self.assertEqual(a.build_report(self.db, a.get_session(self.db, audit)).scans, 0)
        # Current visibility alone cannot expose a scan from the old library.
        self.db.execute("UPDATE library_items SET library_id=1 WHERE item_id=10")
        self.assertEqual(a.get_session(self.db, audit)["scans"], 0)
        self.db.execute("DELETE FROM items WHERE id=10")
        self.assertEqual(a.get_session(self.db, audit)["scans"], 0)
        self.assertNotIn("PRIVATE-PROVENANCE", repr(a.build_report(self.db, a.get_session(self.db, audit))))
        self.h.actor = owner
        report = a.build_report(self.db, a.get_session(self.db, audit))
        self.assertEqual((report.scans, report.counts["elsewhere"], report.counts["unexpected"]), (2, 1, 1))
        self.assertEqual(report.elsewhere[0]["raw_code"], "PRIVATE-PROVENANCE")
        self.assertNotIn("UNKNOWN-LEGACY-ORPHAN", repr(report))
        owner["library_roles"] = {1: "editor"}
        self.assertEqual(a.get_session(self.db, audit)["scans"], 1)  # Own unmatched scan only.
        self.h.actor = {"id": 43, "role": "viewer", "account_role": "admin", "api_token_id": 99}
        report = a.build_report(self.db, a.get_session(self.db, audit))
        self.assertEqual(report.scans, 3)
        self.assertIn("UNKNOWN-LEGACY-ORPHAN", repr(report))

    def test_missing_selection_preflights_existing_hidden_and_readonly_targets(self):
        first = self.initial()
        self.h.item(11)
        other = self.c.add_copy(self.db, 11, {"location_id": 40})
        a = self.h.load("app.services.audits")
        audit = a.create_session(self.db, scope="library", location_id=None)
        self.h.actor = {"id": 50, "role": "editor", "library_roles": {1: "editor"}}
        # An unauthorized existing target must refuse even when it is NOT in
        # the missing set; the native nonmissing no-op is for authorized targets.
        a.add_entry(self.db, audit, item_id=11, copy_id=other, location_id=40,
                    raw_code="already-found", result="confirmed")
        session = a.get_session(self.db, audit)
        self.db.execute("UPDATE items SET permission=1 WHERE id=11")
        with self.assertRaisesRegex(a.AuditError, "^Those copies cannot be updated$"):
            a.mark_selected_missing(self.db, session, [first, other])
        self.assertIsNone(self.row(first)["missing_since"])
        self.assertIsNone(self.row(other)["missing_since"])
        self.db.execute("UPDATE items SET permission=3,visible=0 WHERE id=11")
        self.db.execute("UPDATE library_items SET library_id=2 WHERE item_id=11")
        with self.assertRaisesRegex(a.AuditError, "^Those copies cannot be updated$"):
            a.mark_selected_missing(self.db, session, [first, other, 999999])
        self.assertIsNone(self.row(first)["missing_since"])
        self.assertIsNone(self.row(other)["missing_since"])
        self.h.item(12)
        found = self.c.add_copy(self.db, 12, {"location_id": 40})
        a.add_entry(self.db, audit, item_id=12, copy_id=found, location_id=40, raw_code="found", result="confirmed")
        self.assertEqual(a.mark_selected_missing(self.db, session, [first, found, 999999]), 1)
        self.assertIsNone(self.row(found)["missing_since"])
        self.assertIsNotNone(self.row(first)["missing_since"])

    def test_stale_label_refusal_keeps_edit_context_and_unsaved_fields(self):
        first = self.initial()
        self.db.commit()
        labels = self.h.load("app.routers.labels")
        form = {"location_id": "42", "condition": "good", "notes": "Draft notes", "provenance": "Draft provenance"}
        assigned = asyncio.run(labels.assign_copy_code(self.h.request(form), 10, first))
        code = self.row(first)["copy_barcode"]
        self.assertEqual(assigned.template_name, "fragments/item_copy_edit.html")
        for _ in range(2):
            before = list(self.db.iterdump())
            stale = asyncio.run(labels.assign_copy_code(self.h.request(form), 10, first))
            self.assertEqual(stale.status_code, 200)
            self.assertEqual(stale.template_name, "fragments/item_copy_edit.html")
            self.assertEqual(stale.context["copy_error"], "This copy already has a code")
            self.assertEqual(stale.context["copy"]["notes"], "Draft notes")
            self.assertEqual(stale.context["copy"]["provenance"], "Draft provenance")
            self.assertEqual(stale.context["copy"]["location_id"], 42)
            self.assertEqual((stale.context["item_id"], stale.context["copy"]["id"]), (10, first))
            self.assertEqual(stale.context["copy"]["condition"], "good")
            self.assertEqual(self.row(first)["copy_barcode"], code)
            self.assertIsNone(self.row(first)["notes"])
            self.assertEqual(list(self.db.iterdump()), before)
        second = self.c.add_copy(self.db, 10)
        self.c.trash_copy(self.db, first)
        self.db.commit()
        before = list(self.db.iterdump())
        missing = asyncio.run(labels.assign_copy_code(self.h.request(form), 10, first))
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(missing.template_name, "fragments/item_copies.html")
        self.assertEqual(missing.context["copy_error"], "Copy not found")
        self.assertEqual([row["id"] for row in missing.context["copies"]], [second])
        self.assertEqual(self.row(first)["copy_barcode"], code)
        self.assertEqual(list(self.db.iterdump()), before)

    def test_arrange_ui_requires_every_visible_parent_and_preserves_token_cap(self):
        first = self.initial()
        self.db.commit()
        router = self.h.load("app.routers.location_order")
        allowed = {"id": 60, "role": "viewer", "library_roles": {1: "editor", 2: "viewer"}}
        response = asyncio.run(router.arrange_location(self.h.request(), 40, allowed))
        self.assertTrue(response.context["can_edit"])
        readonly = {"id": 61, "role": "editor", "library_roles": {1: "viewer"}}
        response = asyncio.run(router.arrange_location(self.h.request(), 40, readonly))
        self.assertFalse(response.context["can_edit"])
        self.h.item(11, _library_id=2)
        other = self.c.add_copy(self.db, 11, {"location_id": 40})
        self.db.commit()
        response = asyncio.run(router.arrange_location(self.h.request(), 40, allowed))
        self.assertFalse(response.context["can_edit"])
        token = {"id": 62, "role": "viewer", "account_role": "admin", "api_token_id": 1}
        self.assertFalse(asyncio.run(router.arrange_location(self.h.request(), 40, token)).context["can_edit"])
        self.h.actor = allowed
        with self.assertRaises(Refused):
            self.h.load("app.services.location_order").apply_copy_order(self.db, 40, [other, first])
        self.assertIsNone(self.row(first)["position_order"])
        self.assertIsNone(self.row(other)["position_order"])

    def test_no_action_alias_cleanup_preserves_live_trash_and_every_copy_placement(self):
        first, historical, trashed_parent_copy, root, unrelated = self._alias_fixture()
        self.assertEqual(next(row["on_delete"] for row in self.db.execute("PRAGMA foreign_key_list(items)")
                              if row["from"] == "location_id"), "NO ACTION")
        before = self._alias_snapshot()
        start = len(self.h.authorization)
        self.h.load("app.services.locations").delete_legacy_location(self.db, 1)
        self.assertEqual(self.h.authorization[start:], [(10, "editor", True), (11, "editor", True), (13, "editor", True)])
        after = self._alias_snapshot()
        self.assertEqual(after["items"], [
            {**row, "location_id": None} if row["location_id"] == 1 else row for row in before["items"]])
        self.assertEqual(after["item_copies"], before["item_copies"])
        self.assertEqual(after["location_nodes"], [
            {**row, "legacy_location_id": None} if row["legacy_location_id"] == 1 else row
            for row in before["location_nodes"]])
        self.assertEqual(after["locations"], [row for row in before["locations"] if row["id"] != 1])
        self.assertEqual(after["library_items"], before["library_items"])
        self.assertEqual(self.row(first)["location_id"], 41)
        self.assertIsNotNone(self.row(historical)["deleted_at"])
        self.assertEqual(self.row(trashed_parent_copy)["location_id"], 41)
        self.assertEqual(self.row(root)["location_id"], 40)
        self.assertEqual(self.row(unrelated)["location_id"], 1)  # Equal-but-unrelated NODE 1.
        self.holdings.sync_item(self.db, 10)
        self.holdings.sync_item(self.db, 13)
        self.assertEqual(self._alias_snapshot(), after)  # No later compatibility flatten/recreation.
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_alias_cleanup_preflights_hidden_readonly_targets_before_any_change(self):
        self._alias_fixture()
        self.db.execute("UPDATE library_items SET library_id=2 WHERE item_id=11")
        # The protected item's precise child copy is under the alias, while its
        # LEGACY seam names a different location. Mapping detachment still checks
        # this existing copy parent, not only rows reached by the NULL cleanup.
        self.db.execute("UPDATE items SET location_id=2 WHERE id=11")
        locations = self.h.load("app.services.locations")
        for mode, status in (("hidden", 404), ("readonly", 403)):
            with self.subTest(mode=mode):
                self.db.execute("UPDATE items SET visible=? WHERE id=11", (mode != "hidden",))
                self.h.actor = {"id": 70, "role": "editor", "library_roles": {1: "editor"}}
                if mode == "readonly":
                    self.h.actor["library_roles"][2] = "viewer"
                before = self._alias_snapshot()
                with self.assertRaises(Refused) as denied:
                    locations.delete_legacy_location(self.db, 1)
                self.assertEqual(denied.exception.status_code, status)
                self.assertEqual(self._alias_snapshot(), before)

    def test_alias_cleanup_rolls_back_detach_and_clear_if_delete_fails(self):
        self._alias_fixture()
        self.db.execute("INSERT INTO settings VALUES('caller_pending','preserve uncommitted work')")
        before = self._alias_snapshot()
        self.db.set_authorizer(lambda action, table, *args:
                               sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DELETE and table == "locations"
                               else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.h.load("app.services.locations").delete_legacy_location(self.db, 1)
        finally:
            self.db.set_authorizer(None)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self._alias_snapshot(), before)

    def test_alias_cleanup_has_only_the_exact_nontimestamp_relationship_write(self):
        source = ast.parse((ROOT / "app/services/locations.py").read_text())
        cleanup = next(node for node in source.body
                       if isinstance(node, ast.FunctionDef) and node.name == "delete_legacy_location")
        item_writes = []
        for function in (node for node in source.body if isinstance(node, ast.FunctionDef)):
            for node in ast.walk(function):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "execute" and node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                        and re.match(r"\s*(?:UPDATE\s+items\b|INSERT\s+INTO\s+items\b|DELETE\s+FROM\s+items\b)",
                                     node.args[0].value, re.I)):
                    item_writes.append((function.name, node.args[0].value))
        self.assertEqual(item_writes, [(cleanup.name, "UPDATE items SET location_id = NULL WHERE location_id = ?")])

    def test_copy_condition_is_bounded_trimmed_freetext_not_a_preset_enum(self):
        router = self.h.load("app.routers.item_copies")
        for grade in ("Good", "Fine", "Very Good", "Original Condition", "Ex-library — signed", "Collector A+ / 8.5"):
            self.assertEqual(router._clean_condition("  " + grade + "  "), grade)
        self.assertIsNone(router._clean_condition(" \t "))
        self.assertEqual(router._clean_condition("X" * 60), "X" * 60)
        with self.assertRaisesRegex(ValueError, "^Condition must be 60 characters or fewer$"):
            router._clean_condition("X" * 61)
        historical = "Legacy collector grade " + "x" * 60
        self.assertEqual(router._clean_condition(historical, existing=historical), historical)

    def test_custom_condition_context_omission_and_invalid_edits_preserve_history(self):
        first = self.initial()
        custom = "Collector A+ / signed"
        self.c.update_copy(self.db, first, {"condition": custom, "notes": "Original note", "copy_barcode": "CUSTOM-GRADE"})
        router = self.h.load("app.routers.item_copies")
        context = router._copy_context(self.db, 10)
        self.assertIn((custom, custom), context["conditions"])
        self.assertIn(("good", "Good"), context["conditions"])
        self.assertIn(("Fine", "Fine"), context["conditions"])
        self.assertEqual(context["condition_max_length"], 60)
        self.assertEqual(context["condition_presets"], router.CONDITIONS)
        panel = router._render_edit_panel(self.h.request(), self.db, 10, first, {"condition": "Unsaved personal grade"})
        self.assertIn((custom, custom), panel.context["conditions"])
        self.assertIn(("Unsaved personal grade", "Unsaved personal grade"), panel.context["conditions"])
        self.db.commit()
        response = asyncio.run(router.update_copy(self.h.request({"notes": "Changed only"}), 10, first, notes="Changed only"))
        self.assertFalse(response.context["copy_error"])
        self.assertEqual(self.row(first)["condition"], custom)
        before = dict(self.row(first))
        database_before = list(self.db.iterdump())
        refused = asyncio.run(router.update_copy(
            self.h.request({"condition": "Draft different grade", "acquisition_price": "not-money"}),
            10, first, condition="Draft different grade", acquisition_price="not-money"))
        self.assertEqual(refused.status_code, 200)
        self.assertEqual(refused.template_name, "fragments/item_copy_edit.html")
        self.assertEqual(refused.context["copy_error"], "Acquisition price must be a number")
        self.assertEqual((refused.context["item_id"], refused.context["copy"]["id"]), (10, first))
        self.assertEqual(refused.context["copy"]["condition"], "Draft different grade")
        self.assertEqual(refused.context["copy"]["acquisition_price"], "not-money")
        self.assertEqual(refused.context["copy"]["copy_barcode"], "CUSTOM-GRADE")
        self.assertEqual(dict(self.row(first)), before)
        self.assertEqual(list(self.db.iterdump()), database_before)
        self.assertIn((custom, custom), refused.context["conditions"])
        added = asyncio.run(router.add_copy(self.h.request(), 10, condition="  Fine  "))
        self.assertFalse(added.context["copy_error"])
        self.assertEqual(self.c.copies_for_item(self.db, 10)[1]["condition"], "Fine")

    def test_idle_edit_get_authorizes_parent_without_starting_a_write(self):
        first = self.initial()
        self.h.actor = {"id": 90, "role": "editor", "library_roles": {1: "editor"}}
        self.db.commit()
        before = list(self.db.iterdump())
        changes = self.db.total_changes
        self.h.events.clear()
        self.h.authorization.clear()
        router = self.h.load("app.routers.item_copies")
        response = asyncio.run(router.edit_panel(self.h.request(), 10, first))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.template_name, "fragments/item_copy_edit.html")
        self.assertEqual(response.context["copy"]["id"], first)
        self.assertTrue(response.context["can_edit_item"])
        self.assertIn((10, "editor", False), self.h.authorization)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.db.total_changes, changes)
        self.assertFalse(any(re.match(r"\s*(?:BEGIN|SAVEPOINT|INSERT|UPDATE|DELETE)\b", event[1], re.I)
                             for event in self.h.events if event[0] == "sql"))
        self.assertEqual(list(self.db.iterdump()), before)

    def test_mutation_lock_oracle_rejects_idle_and_deferred_read_transactions(self):
        self.initial()
        self.db.commit()
        for begin in (None, "BEGIN"):
            with self.subTest(begin=begin):
                if begin:
                    self.db.execute(begin)
                    self.db.execute("SELECT * FROM items").fetchall()
                    self.assertTrue(self.db.in_transaction)  # Still NOT a write lock.
                with self.assertRaisesRegex(AssertionError, "before a reserved write lock"):
                    with self.h.mutation_authorization():
                        self.h.require(self.db, 10, "editor")
                self.db.rollback()
        self.db.execute("BEGIN IMMEDIATE")
        with self.h.mutation_authorization():
            self.h.require(self.db, 10, "editor")
        self.db.rollback()
        self.db.execute("BEGIN")
        self.db.execute("SELECT * FROM items").fetchall()
        self.c.write_lock(self.db)  # A deferred read must be upgraded, not merely detected.
        with self.h.mutation_authorization():
            self.h.require(self.db, 10, "editor")
        self.db.rollback()

    def test_copy_mutations_authorize_under_reserved_lock_before_first_data_write(self):
        first = self.initial()
        self.c.add_copy(self.db, 10)
        self.c.mark_missing(self.db, first)
        self.db.commit()
        router = self.h.load("app.routers.item_copies")
        labels = self.h.load("app.routers.labels")
        operations = (
            lambda: router.update_copy(self.h.request({"notes": "Locked edit"}), 10, first, notes="Locked edit"),
            lambda: labels.assign_copy_code(self.h.request(), 10, first),
            lambda: router.add_copy(self.h.request(), 10, condition="Fine"),
            lambda: router.copy_found(self.h.request(), 10, first),
            lambda: router.remove_copy(self.h.request(), 10, first),
        )
        for operation in operations:
            self.h.events.clear()
            with self.h.mutation_authorization():
                response = asyncio.run(operation())
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.context["copy_error"])
            authorizations = [index for index, event in enumerate(self.h.events)
                              if event == ("authorize", 10, "editor", False)]
            writes = [index for index, event in enumerate(self.h.events)
                      if event[0] == "sql" and re.match(r"\s*(?:INSERT|UPDATE|DELETE)\b", event[1], re.I)
                      and event[1] != "UPDATE item_copies SET updated_at = updated_at WHERE 0"]
            self.assertTrue(authorizations)
            self.assertTrue(writes)
            self.assertLess(authorizations[0], writes[0])
        self.assertEqual(self.row(first)["notes"], "Locked edit")
        self.assertTrue(self.row(first)["copy_barcode"])
        self.assertIsNotNone(self.row(first)["deleted_at"])

    def test_parent_404_and_viewer_403_are_not_child_200_refusals(self):
        first = self.initial()
        self.db.commit()
        router = self.h.load("app.routers.item_copies")
        for visible, permission, status in ((0, 3, 404), (1, 1, 403)):
            with self.subTest(status=status):
                self.db.execute("UPDATE items SET visible=?,permission=? WHERE id=10", (visible, permission))
                self.db.commit()
                before = list(self.db.iterdump())
                with self.assertRaises(Refused) as denied:
                    with self.h.mutation_authorization():
                        asyncio.run(router.update_copy(self.h.request({"notes": "Not saved"}),
                                                       10, first, notes="Not saved"))
                self.assertEqual(denied.exception.status_code, status)
                self.assertEqual(denied.exception.detail, "Item not found" if status == 404 else "Forbidden")
                if status == 404:
                    self.assertEqual(json.loads(denied.exception.headers["HX-Trigger"]), {"error": "Item not found"})
                self.assertEqual(list(self.db.iterdump()), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
