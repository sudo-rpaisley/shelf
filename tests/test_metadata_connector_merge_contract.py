"""Offline extracted-input checks for the concurrent metadata merge lane.

Run directly with Python while the application is unfrozen. These tests execute
owned AST inputs plus the frozen core transaction/authorization functions against
disposable SQLite schemas. FastAPI supplies its real HTTPException, but no app
module, conftest or provider is imported. Full schema, projection implementation,
HTTP composition and application qualification remain parent-freeze checks.
"""

from __future__ import annotations

import ast
import asyncio
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from itertools import count
from pathlib import Path
import re
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from urllib.parse import quote, urljoin, urlsplit

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "app/services"


def extracted(name, *, names=None, path=None, **namespace):
    """Load function/class syntax only, never module imports or side effects."""
    source = path or SERVICES / f"{name}.py"
    tree = ast.parse(source.read_text())
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    body += [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
             and (names is None or node.name in names)]
    scope = {"__name__": __name__, **namespace}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(source), "exec"), scope)
    return scope


class ConnectorMergeInputs(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY, title TEXT, authors TEXT, isbn TEXT, upc TEXT,
                media_type TEXT, source TEXT DEFAULT 'manual', deleted_at TEXT,
                library_id INTEGER DEFAULT 1, owned INTEGER DEFAULT 1, cover_path TEXT,
                series_name TEXT, series_position REAL, publisher TEXT, publish_year INTEGER,
                description TEXT, page_count INTEGER, platform TEXT, narrator TEXT,
                duration_mins INTEGER, asin TEXT, location_id INTEGER,
                abs_id TEXT, abs_library_id TEXT, komga_id TEXT, komga_library_id TEXT,
                komga_series_id TEXT, romm_id TEXT, romm_platform_id TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TEMP VIEW items_live AS SELECT * FROM items WHERE deleted_at IS NULL;
            CREATE TABLE libraries (id INTEGER PRIMARY KEY);
            INSERT INTO libraries VALUES (1), (2);
            CREATE TABLE library_items (item_id INTEGER PRIMARY KEY, library_id INTEGER NOT NULL);
            CREATE TABLE library_memberships (library_id INTEGER, user_id INTEGER, role TEXT);
            INSERT INTO library_memberships VALUES (1, 1, 'editor');
            CREATE TABLE komga_records (
                komga_id TEXT PRIMARY KEY, item_id INTEGER, library_id TEXT, series_id TEXT,
                kind TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE romm_records (
                romm_id TEXT PRIMARY KEY, item_id INTEGER, platform_id TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE digital_holdings (
                id INTEGER PRIMARY KEY, item_id INTEGER, provider TEXT, external_id TEXT,
                library_id TEXT, format TEXT, source_url TEXT,
                UNIQUE(item_id, provider, external_id)
            );
            CREATE TABLE game_platforms (slug TEXT PRIMARY KEY, name TEXT);
            INSERT INTO game_platforms VALUES ('snes', 'Super Nintendo');
        """)
        self.project = Mock()
        self.inserts = []
        # Execute the real frozen lock/savepoint and authorization functions,
        # with real membership queries against this disposable fixture. These
        # are extracted inputs, not an app import or permissive guard doubles.
        write_scope = extracted("item_write", names={"_write_scope"},
                                contextmanager=contextmanager, _savepoints=count())["_write_scope"]

        def initialized_security_schema(db):
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not {"libraries", "library_items", "library_memberships"} <= tables:
                raise RuntimeError("Security fixture is not initialized")

        roles = {"viewer": 1, "editor": 2, "admin": 3}
        library_scope = extracted("libraries", names={
            "_account_admin", "_cap_allows", "membership_role", "has_library_role", "item_library_id", "has_item_role",
        }, ensure_schema=initialized_security_schema, LIBRARY_ROLE_LEVELS=roles)
        self.security = extracted("catalogue_security", names={
            "set_actor", "reset_actor", "current_actor", "_principal", "require_item_role",
        }, libraries=SimpleNamespace(**library_scope),
            HTTPException=HTTPException, _actor=ContextVar("metadata_test_actor", default=None), _EXPIRED=object())

        def insert(db, fields, *, restore_trashed=True):
            self.inserts.append(restore_trashed)
            fields = {key: value for key, value in fields.items() if key != "series_memberships"}
            item_id = db.execute(f"INSERT INTO items ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                                 tuple(fields.values())).lastrowid
            db.execute("INSERT INTO library_items VALUES (?, ?)", (item_id, fields.get("library_id", 1)))
            return item_id

        def update(db, item_id, fields):
            db.execute(f"UPDATE items SET {','.join(key + ' = ?' for key in fields)} WHERE id = ?",
                       (*fields.values(), item_id))

        class IdentifierInTrash(ValueError):
            pass

        series = SimpleNamespace(
            add_metadata_memberships=Mock(), metadata_memberships=lambda data: [],
        )
        common = dict(holdings=SimpleNamespace(sync_item=self.project), require_item_role=self.security["require_item_role"],
                      _write_scope=write_scope,
                      insert_item=insert, update_item_fields=update, IdentifierInTrash=IdentifierInTrash)
        self.boundaries = common
        self.komga = extracted("komga_records", **common, series_memberships=series,
                               isbn_svc=SimpleNamespace(canonical_isbn_pair=lambda value: (value, None)),
                               KOMGA_KINDS=frozenset({"comic", "manga"}))
        self.romm = extracted("romm_records", **common, _series_enricher=None)

    def item(self, **fields):
        fields = {"title": "Local title", "media_type": "digital_comic", **fields}
        item_id = self.db.execute(f"INSERT INTO items ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                                  tuple(fields.values())).lastrowid
        self.db.execute("INSERT INTO library_items VALUES (?, ?)", (item_id, fields.get("library_id", 1)))
        return item_id

    def actor(self, user=None):
        token = self.security["set_actor"](user or {"id": 1, "role": "viewer"})
        self.addCleanup(self.security["reset_actor"], token)

    def music_fixture(self, *, library_id=1):
        self.db.executescript("""
            CREATE TABLE music_releases (
                item_id INTEGER PRIMARY KEY, artist_credit TEXT, musicbrainz_release_id TEXT UNIQUE,
                musicbrainz_release_group_id TEXT, release_type TEXT, release_status TEXT,
                release_date TEXT, first_release_date TEXT, country TEXT, label TEXT, catalog_number TEXT,
                packaging TEXT, media_count INTEGER, format_summary TEXT, metadata_source TEXT, metadata_updated_at TEXT,
                edition_notes TEXT, media_condition TEXT, packaging_condition TEXT, condition_notes TEXT,
                discogs_release_id INTEGER, discogs_master_id INTEGER, discogs_label TEXT,
                discogs_catalog_number TEXT, discogs_format_summary TEXT, discogs_genres TEXT,
                discogs_styles TEXT, discogs_notes TEXT, discogs_updated_at TEXT
            );
            CREATE TABLE music_identifiers (
                id INTEGER PRIMARY KEY, item_id INTEGER, identifier_type TEXT, value TEXT,
                description TEXT, source TEXT DEFAULT 'manual', UNIQUE(item_id, identifier_type, value)
            );
            CREATE TABLE music_media (id INTEGER PRIMARY KEY, item_id INTEGER, position INTEGER,
                format TEXT, title TEXT, track_count INTEGER);
            CREATE TABLE music_tracks (id INTEGER PRIMARY KEY, medium_id INTEGER, position INTEGER,
                number TEXT, title TEXT, artist_credit TEXT, duration_ms INTEGER, musicbrainz_recording_id TEXT);
            CREATE TABLE item_links (item_a_id INTEGER, item_b_id INTEGER, link_type TEXT);
        """)
        item_id = self.item(media_type="vinyl", library_id=library_id)
        self.db.execute("INSERT INTO music_releases (item_id, musicbrainz_release_id, discogs_release_id, "
                        "discogs_label, edition_notes) VALUES (?, 'original-release', 111, 'Old cache', 'Collector notes')", (item_id,))
        self.db.execute("INSERT INTO music_identifiers VALUES (77, ?, 'discogs_release_id', '111', 'Selected Discogs release', 'manual')", (item_id,))
        self.db.execute("INSERT INTO music_identifiers VALUES (78, ?, 'matrix_runout', 'MANUAL-1', 'Personal entry', 'manual')", (item_id,))
        self.catalogue = extracted("music_catalog", names={
            "_catalogue_write", "_now", "_discogs_is_fresh", "_link_release_group_siblings",
            "save_release", "add_identifier", "selected_discogs_release_id",
        }, wraps=wraps, datetime=datetime, timezone=timezone, timedelta=timedelta,
            _write_scope=self.boundaries["_write_scope"], require_item_role=self.boundaries["require_item_role"])
        self.selection = extracted("discogs_selection", music_catalog=SimpleNamespace(**self.catalogue),
                                   _write_scope=self.boundaries["_write_scope"],
                                   require_item_role=self.boundaries["require_item_role"], IDENTIFIER_TYPE="discogs_release_id")
        return item_id

    def music_snapshot(self):
        return {table: [tuple(row) for row in self.db.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("music_releases", "music_identifiers", "music_media", "music_tracks")}

    def book(self, **fields):
        return {"komga_id": "book-1", "komga_library_id": "library-1", "library_kind": "comic",
                "title": "Provider title", **fields}

    def rom(self, **fields):
        return {"romm_id": "rom-1", "romm_platform_id": "platform-1", "platform": "snes",
                "platform_name": "Super Nintendo", "title": "Provider game", **fields}

    def snapshot(self):
        return {name: [tuple(row) for row in self.db.execute(f"SELECT * FROM {name}").fetchall()]
                for name in ("items", "komga_records", "romm_records", "digital_holdings", "game_platforms")}

    def test_legacy_inline_identity_is_reconciled_not_duplicated(self):
        item_id = self.item(source="komga", komga_id="book-1", komga_library_id="library-1")
        # Reproduce the integration fault: a native sidecar-only matcher misses
        # a populated main inline identity, before the reconciled adapter runs.
        self.assertIsNone(self.komga["_existing_record"](self.db, "book-1"))
        result = self.komga["persist_candidate"](self.db, self.book())
        self.assertEqual(result["item_id"], item_id)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT item_id FROM komga_records").fetchone()[0], item_id)
        self.project.assert_called_once_with(self.db, item_id)

    def test_plural_exact_records_keep_representative_and_holding_ids(self):
        item_id = self.item(komga_id="book-1", komga_library_id="library-1", media_type="comic")
        self.db.executemany("INSERT INTO komga_records VALUES (?, ?, ?, NULL, 'comic', NULL)",
                            [("book-1", item_id, "library-1"), ("book-2", item_id, "library-2")])
        self.db.execute("INSERT INTO digital_holdings VALUES (77, ?, 'komga', 'book-2', 'library-2', 'CBZ', 'saved')", (item_id,))
        result = self.komga["persist_candidate"](self.db, self.book(komga_id="book-2", komga_library_id="library-2"))
        item = self.db.execute("SELECT * FROM items").fetchone()
        self.assertEqual((result["item_id"], item["komga_id"], item["title"], item["media_type"]),
                         (item_id, "book-1", "Local title", "comic"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM komga_records").fetchone()[0], 2)
        self.assertEqual(tuple(self.db.execute("SELECT id, format, source_url FROM digital_holdings").fetchone()), (77, "CBZ", "saved"))

    def test_conflicting_attachment_rolls_back_without_selecting_a_winner(self):
        first = self.item(komga_id="book-1")
        second = self.item()
        self.db.execute("INSERT INTO komga_records VALUES ('book-1', ?, 'library-1', NULL, 'comic', NULL)", (second,))
        before = self.snapshot()
        with self.assertRaises(self.komga["KomgaPersistenceError"]):
            self.komga["persist_candidate"](self.db, self.book())
        self.assertNotEqual(first, second)
        self.assertEqual(self.snapshot(), before)
        self.project.assert_not_called()

    def test_duplicate_inline_identity_is_ambiguous_even_with_one_live_row(self):
        self.item(komga_id="book-1")
        self.item(komga_id="book-1", deleted_at="2026-01-01")
        with self.assertRaises(self.komga["KomgaPersistenceError"]):
            self.komga["persist_candidate"](self.db, self.book())
        self.project.assert_not_called()

    def test_trash_exact_attachment_precedes_all_writes(self):
        item_id = self.item(romm_id="rom-1", deleted_at="2026-01-01", media_type="digital_game")
        before = self.snapshot()
        result = self.romm["persist_candidate"](self.db, self.rom(platform="new-platform", platform_name="New"))
        self.assertEqual(result, {"item_id": item_id, "action": "in_trash"})
        self.assertEqual(self.snapshot(), before)
        self.project.assert_not_called()

    def test_hidden_attachment_cannot_be_reclaimed_or_disclosed(self):
        self.item(komga_id="book-1", library_id=2)
        self.actor()
        self.db.executescript("DROP VIEW items_live; CREATE TEMP VIEW items_live AS SELECT * FROM items WHERE deleted_at IS NULL AND library_id = 1;")
        before = self.snapshot()
        with self.assertRaises(HTTPException) as refusal:
            self.komga["persist_candidate"](self.db, self.book())
        self.assertEqual(refusal.exception.status_code, 404)
        self.assertEqual(self.snapshot(), before)

    def test_incompatible_plural_classification_is_refused(self):
        item_id = self.item(source="komga", komga_id="book-1")
        self.db.execute("INSERT INTO komga_records VALUES ('book-2', ?, 'library-2', NULL, 'manga', NULL)", (item_id,))
        before = self.snapshot()
        with self.assertRaises(self.komga["KomgaPersistenceError"]):
            self.komga["persist_candidate"](self.db, self.book())
        self.assertEqual(self.snapshot(), before)

    def test_unclassified_new_komga_library_is_refused(self):
        with self.assertRaises(self.komga["KomgaPersistenceError"]):
            self.komga["persist_candidate"](self.db, self.book(library_kind=""))
        self.assertFalse(self.inserts)

    def test_new_komga_item_is_digital_and_never_machine_restored(self):
        self.item(isbn="9781974700523", media_type="manga")
        result = self.komga["persist_candidate"](self.db, self.book(library_kind="manga", isbn="9781974700523"))
        self.assertEqual(self.inserts, [False])
        row = self.db.execute("SELECT media_type, location_id FROM items WHERE id = ?", (result["item_id"],)).fetchone()
        self.assertEqual(tuple(row), ("digital_manga", None))

    def test_existing_romm_physical_attachment_is_not_taken_over(self):
        item_id = self.item(romm_id="rom-1", media_type="video_game", platform="nes")
        result = self.romm["persist_candidate"](self.db, self.rom())
        item = self.db.execute("SELECT title, media_type, platform, source FROM items").fetchone()
        self.assertEqual(result["item_id"], item_id)
        self.assertEqual(tuple(item), ("Local title", "video_game", "nes", "manual"))

    def test_romm_never_reassigns_an_unclaimed_title_match(self):
        existing = self.item(title="Provider game", media_type="digital_game", platform="snes")
        result = self.romm["persist_candidate"](self.db, self.rom())
        self.assertNotEqual(result["item_id"], existing)
        self.assertEqual(self.inserts, [False])
        row = self.db.execute("SELECT media_type, romm_id FROM items WHERE id = ?", (result["item_id"],)).fetchone()
        self.assertEqual(tuple(row), ("digital_game", "rom-1"))

    def test_romm_missing_platform_is_refused_without_registry_writes(self):
        before = self.snapshot()
        with self.assertRaises(self.romm["RomMPersistenceError"]):
            self.romm["persist_candidate"](self.db, self.rom(platform=""))
        self.assertEqual(self.snapshot(), before)

    def test_cover_token_target_requires_exact_origin(self):
        scope = extracted("romm_sync", urljoin=urljoin, urlsplit=urlsplit)
        target = scope["_cover_target"]
        self.assertEqual(target("https://romm.example", "/cover.jpg"), "https://romm.example/cover.jpg")
        for value in ("https://evil.example/x", "https://romm.example:444/x", "http://romm.example/x", "https://user@romm.example/x"):
            with self.subTest(value=value):
                self.assertIsNone(target("https://romm.example", value))

    def test_komga_series_names_are_not_guessed_from_volume_suffixes(self):
        scope = extracted("komga_books", PAGE_SIZE=200)
        self.assertEqual(scope["_canonical_series_name"]("One Piece (1)"), "One Piece (1)")
        self.assertEqual(scope["_canonical_series_name"]("One Piece (2)"), "One Piece (2)")

    def test_disabled_native_sync_never_loads_credentials_or_provider(self):
        for name in ("komga_sync", "romm_sync", "audiobookshelf"):
            config, run = Mock(side_effect=AssertionError("credentials read")), AsyncMock()
            scope = extracted(name, feature_enabled=lambda key: False, configuration=config,
                              _sync_lock=threading.Lock(), _series_enricher=None)
            scope["_sync"] = run
            args = ("https://abs.example", "test-token") if name == "audiobookshelf" else ()
            result = asyncio.run(scope["sync"](*args))
            self.assertIn("disabled", result["error"])
            config.assert_not_called()
            run.assert_not_awaited()

    def test_hidden_node_is_not_a_related_media_bridge(self):
        first, hidden, last = self.item(), self.item(library_id=2), self.item()
        self.db.executescript("CREATE TABLE item_links (item_a_id INTEGER, item_b_id INTEGER, link_type TEXT);")
        self.db.executemany("INSERT INTO item_links VALUES (?, ?, 'related')", [(first, hidden), (hidden, last)])
        self.db.executescript("DROP VIEW items_live; CREATE TEMP VIEW items_live AS SELECT * FROM items WHERE deleted_at IS NULL AND library_id = 1;")
        scope = extracted("media_groups", re=re, defaultdict=defaultdict)
        self.assertEqual(scope["related_ids"](self.db, first), [])

    def test_periodical_identity_keeps_distinct_issns_and_refuses_ambiguity(self):
        barcodes = extracted("periodicals", re=re, dataclass=dataclass, normalize_barcode=lambda value: re.sub(r"\D", "", value))
        scope = extracted("periodical_records", ensure_schema=lambda db: None,
                          normalise_issn=barcodes["normalise_issn"])
        self.db.executescript("CREATE TABLE periodical_publications (id INTEGER PRIMARY KEY, title TEXT, issn TEXT, publisher TEXT, language TEXT, updated_at TEXT);")
        first = scope["upsert_publication"](self.db, title="Wired", issn="1059-1028")
        second = scope["upsert_publication"](self.db, title="Wired", issn="1357-0978")
        self.assertNotEqual(first, second)
        self.assertEqual(scope["find_publication"](self.db, title="Wired", issn="10591028"), first)
        with self.assertRaises(ValueError):
            scope["find_publication"](self.db, title="Wired")
        with self.assertRaises(ValueError):
            barcodes["normalise_issn"]("1059-1020")

    def test_main_retail_addon_and_upstream_977_barcode_forms_survive(self):
        scope = extracted("periodicals", re=re, dataclass=dataclass, normalize_barcode=lambda value: re.sub(r"\D", "", value))
        serial = scope["parse_barcode"]("9770161737008")
        self.assertIsNotNone(serial)
        self.assertEqual(serial.issn, "0161-7370")
        retail = scope["parse_barcode"]("01234567890512")
        self.assertIsNotNone(retail)
        self.assertEqual((retail.issn, retail.supplement), (None, "12"))
        self.assertIsNone(scope["parse_barcode"]("012345678905"))

    def test_schema_is_central_and_enrichment_is_explicit_composition(self):
        aliases = {"provider_series": "ensure_provider_series_schema", "series_memberships": "ensure_series_memberships_schema",
                   "periodical_records": "ensure_periodical_schema"}
        for name, target in aliases.items():
            source = (SERVICES / f"{name}.py").read_text()
            self.assertIn(target, source)
            self.assertNotRegex(source, r"CREATE\s+(?:TABLE|INDEX)|ALTER\s+TABLE|_register_migrations")
        tree = ast.parse((SERVICES / "provider_series_sync.py").read_text())
        self.assertFalse(any(isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) for node in tree.body))
        source = ast.unparse(tree)
        self.assertNotIn("audiobookshelf.sync =", source)
        self.assertNotIn("komga.sync =", source)
        self.assertNotIn("romm._import_rom_batch =", source)

    def test_legacy_connector_modules_are_adapters_not_second_writers(self):
        for name in ("komga", "romm"):
            source = (SERVICES / f"{name}.py").read_text()
            self.assertIn(f"await {name}_sync.sync", source)
            self.assertNotIn("insert_item", source)
            self.assertNotRegex(source, r"INSERT\s+INTO\s+(?:items|komga_records|romm_records)")

    def test_abs_refuses_new_items_without_audio_or_ebook_evidence(self):
        scope = extracted("audiobookshelf", **self.boundaries, ItemValueError=ValueError)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            scope["persist_candidate"](self.db, {"abs_id": "abs-1", "abs_library_id": "lib-1", "title": "Unknown format"})
        self.assertEqual(self.snapshot(), before)

    def test_music_format_inference_refuses_unknown_or_conflicting_media(self):
        scope = extracted("music", path=ROOT / "app/routers/music.py", names={"_infer_media_type"})
        infer = scope["_infer_media_type"]
        self.assertIsNone(infer({"media": [{"format": "Unknown"}]}))
        self.assertIsNone(infer({"media": [{"format": "Vinyl"}, {"format": "CD"}]}))
        self.assertEqual(infer({"media": [{"format": '12" Vinyl'}]}), "vinyl")

    def test_music_catalog_cannot_replace_an_existing_exact_release_identity(self):
        item_id = self.item(media_type="vinyl")
        self.db.executescript("CREATE TABLE music_releases (item_id INTEGER PRIMARY KEY, musicbrainz_release_id TEXT);")
        self.db.execute("INSERT INTO music_releases VALUES (?, 'original-release')", (item_id,))
        scope = extracted("music_catalog", names={"save_release", "_catalogue_write"}, wraps=wraps,
                          _write_scope=self.boundaries["_write_scope"], require_item_role=self.boundaries["require_item_role"])
        with self.assertRaises(ValueError):
            scope["save_release"](self.db, item_id, {"musicbrainz_release_id": "different-release"})
        self.assertEqual(self.db.execute("SELECT musicbrainz_release_id FROM music_releases").fetchone()[0], "original-release")

    def test_provider_lookup_requires_the_requested_exact_release_id(self):
        missing = object()
        for name, requested, returned in (("musicbrainz", "release/with/path", "other"), ("discogs", "123", 456)):
            no_match = Mock(return_value=missing)
            scope = extracted(name, quote=quote, provider_result=SimpleNamespace(no_match=no_match),
                              MUSICBRAINZ_BASE="https://musicbrainz.example", DISCOGS_BASE="https://discogs.example",
                              _RELEASE_INCLUDES="recordings")
            fetch = AsyncMock(return_value=SimpleNamespace(found=True, payload={"id": returned}))
            scope["_request_json"] = fetch
            kwargs = {"token": "offline-test-token"} if name == "discogs" else {}
            result = asyncio.run(scope["lookup_release"](requested, object(), **kwargs))
            self.assertIs(result, missing)
            no_match.assert_called_once_with(name)
            self.assertEqual(fetch.await_count, 1)
            if name == "musicbrainz":
                self.assertIn("release%2Fwith%2Fpath", fetch.call_args.args[1])

    def test_direct_discogs_selection_authorizes_before_identifier_mutation(self):
        item_id = self.music_fixture()
        self.db.execute("UPDATE library_memberships SET role = 'viewer' WHERE user_id = 1")
        self.db.commit()
        self.actor({"id": 1, "role": "editor"})
        before, trace = self.music_snapshot(), []
        self.db.set_trace_callback(trace.append)
        with self.assertRaises(HTTPException) as refusal:
            self.selection["set_selected_release_id"](self.db, item_id, 222)
        self.assertEqual(refusal.exception.status_code, 403)
        self.db.commit()  # A caller catching the refusal must not commit a deletion.
        self.assertEqual(self.music_snapshot(), before)
        self.assertFalse(any(re.match(r"(?:DELETE FROM|UPDATE|INSERT INTO) music_", sql) for sql in trace))
        self.assertLess(trace.index("BEGIN IMMEDIATE"), next(index for index, sql in enumerate(trace)
                        if sql.startswith("SELECT * FROM items WHERE id =")))

    def test_direct_discogs_clear_authorizes_before_any_mutation(self):
        item_id = self.music_fixture()
        self.db.execute("UPDATE library_memberships SET role = 'viewer' WHERE user_id = 1")
        self.db.commit()
        self.actor()
        before = self.music_snapshot()
        with self.assertRaises(HTTPException) as refusal:
            self.selection["clear_selected_release_id"](self.db, item_id)
        self.assertEqual(refusal.exception.status_code, 403)
        self.db.commit()
        self.assertEqual(self.music_snapshot(), before)

    def test_direct_discogs_getter_does_not_disclose_hidden_identifier(self):
        item_id = self.music_fixture(library_id=2)
        self.actor()
        with self.assertRaises(HTTPException) as refusal:
            self.selection["get_selected_release_id"](self.db, item_id)
        self.assertEqual((refusal.exception.status_code, refusal.exception.detail), (404, "Item not found"))

    def test_discogs_later_cache_refusal_rolls_back_selection_and_preserves_ids(self):
        item_id = self.music_fixture()
        self.db.executescript("""CREATE TRIGGER refuse_cache_update BEFORE UPDATE ON music_releases
            BEGIN SELECT RAISE(ABORT, 'later cache refusal'); END;""")
        self.actor()
        before, trace = self.music_snapshot(), []
        self.db.set_trace_callback(trace.append)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "later cache refusal"):
            self.selection["set_selected_release_id"](self.db, item_id, 222)
        self.db.commit()
        self.assertEqual(self.music_snapshot(), before)
        self.assertTrue(any(sql.startswith("UPDATE music_identifiers") for sql in trace))
        self.assertTrue(any(sql.startswith("ROLLBACK TO") for sql in trace))

    def test_discogs_clear_later_refusal_does_not_leave_partial_deletion(self):
        item_id = self.music_fixture()
        self.db.executescript("""CREATE TRIGGER refuse_cache_clear BEFORE UPDATE ON music_releases
            BEGIN SELECT RAISE(ABORT, 'later clear refusal'); END;""")
        self.actor()
        before = self.music_snapshot()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "later clear refusal"):
            self.selection["clear_selected_release_id"](self.db, item_id)
        self.db.commit()
        self.assertEqual(self.music_snapshot(), before)

    def test_discogs_reselection_retains_marker_id_and_manual_release_data(self):
        item_id = self.music_fixture()
        self.actor()
        self.assertEqual(self.selection["set_selected_release_id"](self.db, item_id, 222), 222)
        self.assertEqual(self.selection["get_selected_release_id"](self.db, item_id), 222)
        marker = self.db.execute("SELECT id, value FROM music_identifiers WHERE identifier_type = 'discogs_release_id'").fetchone()
        self.assertEqual(tuple(marker), (77, "222"))
        release = self.db.execute("SELECT musicbrainz_release_id, edition_notes, discogs_release_id, discogs_label FROM music_releases").fetchone()
        self.assertEqual(tuple(release), ("original-release", "Collector notes", 222, None))
        self.assertEqual(tuple(self.db.execute("SELECT id, value FROM music_identifiers WHERE identifier_type = 'matrix_runout'").fetchone()), (78, "MANUAL-1"))
        self.assertTrue(self.selection["clear_selected_release_id"](self.db, item_id))
        self.assertFalse(self.selection["clear_selected_release_id"](self.db, item_id))
        self.assertIsNone(self.selection["get_selected_release_id"](self.db, item_id))
        self.assertEqual(self.db.execute("SELECT value FROM music_identifiers").fetchone()[0], "MANUAL-1")

    def test_discogs_direct_service_never_lifts_a_bearer_cap(self):
        item_id = self.music_fixture()
        self.actor({"id": 1, "role": "viewer", "account_role": "admin", "api_token_id": 9})
        before = self.music_snapshot()
        with self.assertRaises(HTTPException) as refusal:
            self.selection["set_selected_release_id"](self.db, item_id, 222)
        self.assertEqual(refusal.exception.status_code, 403)
        self.assertEqual(self.music_snapshot(), before)

    def test_modern_save_release_refresh_preserves_exact_pressing_and_collector_data(self):
        item_id = self.music_fixture()
        self.actor()
        self.catalogue["save_release"](self.db, item_id, {
            "musicbrainz_release_id": "original-release", "artist_credit": "Updated artist",
            "media": [{"position": 1, "format": '12" Vinyl', "tracks": [{"number": "A1", "title": "Track"}]}],
        })
        release = self.db.execute("SELECT musicbrainz_release_id, artist_credit, discogs_release_id, edition_notes FROM music_releases").fetchone()
        self.assertEqual(tuple(release), ("original-release", "Updated artist", 111, "Collector notes"))
        self.assertEqual(self.db.execute("SELECT number FROM music_tracks").fetchone()[0], "A1")
        self.assertEqual(self.db.execute("SELECT id FROM music_identifiers WHERE identifier_type = 'discogs_release_id'").fetchone()[0], 77)
        before = self.music_snapshot()
        with self.assertRaisesRegex(ValueError, "different exact MusicBrainz release identity"):
            self.catalogue["save_release"](self.db, item_id, {"musicbrainz_release_id": "other-release", "media": []})
        self.db.commit()
        self.assertEqual(self.music_snapshot(), before)

    def scope_transition(self, provider, *, refuse_core=False):
        """Exercise actual sidecar SQL, guarded transactions and a strict projection boundary.

        The update boundary checks the staged sidecar BEFORE its item update,
        then projects using real SQLite upsert SQL. This is not a claim about
        the independently owned full physical/copy projection implementation.
        """
        is_komga = provider == "komga"
        table = f"{provider}_records"
        external_id = "book-1" if is_komga else "rom-1"
        scope_key = "library_id" if is_komga else "platform_id"
        inline_scope = "komga_library_id" if is_komga else "romm_platform_id"
        fields = {f"{provider}_id": external_id, inline_scope: "old-scope", "source": provider,
                  "media_type": "digital_comic" if is_komga else "digital_game"}
        item_id = self.item(**fields)
        if is_komga:
            self.db.execute("INSERT INTO komga_records VALUES (?, ?, 'old-scope', NULL, 'comic', 'retained')", (external_id, item_id))
        else:
            self.db.execute("INSERT INTO romm_records VALUES (?, ?, 'old-scope', 'retained')", (external_id, item_id))
        self.db.execute("INSERT INTO digital_holdings VALUES (77, ?, ?, ?, 'old-scope', 'kept-format', 'kept-url')", (item_id, provider, external_id))
        original_rowid = self.db.execute(f"SELECT rowid FROM {table}").fetchone()[0]
        self.db.commit()
        if refuse_core:
            self.db.executescript("""CREATE TRIGGER refuse_core_update BEFORE UPDATE ON items
                WHEN NEW.title IN ('Provider title', 'Provider game')
                BEGIN SELECT RAISE(ABORT, 'later core refusal'); END;""")
        before, trace = self.snapshot(), []
        self.actor()
        self.db.set_trace_callback(trace.append)
        native = self.komga if is_komga else self.romm
        update = self.boundaries["update_item_fields"]

        def strict_projection(db, current_id):
            row = db.execute(f"SELECT i.{inline_scope} AS inline_scope, r.{scope_key} AS sidecar_scope "
                             f"FROM items i JOIN {table} r ON r.item_id = i.id WHERE i.id = ?", (current_id,)).fetchone()
            self.assertEqual((row["inline_scope"], row["sidecar_scope"]), ("new-scope", "new-scope"))
            db.execute("INSERT INTO digital_holdings (item_id, provider, external_id, library_id) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT(item_id, provider, external_id) DO UPDATE SET library_id = excluded.library_id",
                       (current_id, provider, external_id, row["sidecar_scope"]))

        def projected_update(db, current_id, desired):
            self.assertEqual(db.execute(f"SELECT {scope_key} FROM {table} WHERE item_id = ?", (current_id,)).fetchone()[0], "new-scope")
            update(db, current_id, desired)
            self.project(db, current_id)

        self.project.side_effect = strict_projection
        native["update_item_fields"] = projected_update
        candidate = self.book(komga_library_id="new-scope") if is_komga else self.rom(
            romm_platform_id="new-scope", platform="explicit-new-platform", platform_name="Provider's explicit platform")
        if refuse_core:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "later core refusal"):
                native["persist_candidate"](self.db, candidate)
            self.db.commit()
            self.assertEqual(self.snapshot(), before)
            self.assertTrue(any(sql.startswith("ROLLBACK TO") for sql in trace))
        else:
            decision = native["persist_candidate"](self.db, candidate)
            self.assertEqual(decision["item_id"], item_id)
            self.assertEqual(self.db.execute(f"SELECT {inline_scope} FROM items WHERE id = ?", (item_id,)).fetchone()[0], "new-scope")
            self.assertEqual(tuple(self.db.execute("SELECT id, library_id, format, source_url FROM digital_holdings").fetchone()),
                             (77, "new-scope", "kept-format", "kept-url"))
            self.assertGreaterEqual(self.project.call_count, 2)
        self.assertEqual(self.db.execute(f"SELECT rowid FROM {table}").fetchone()[0], original_rowid)
        staged = next(index for index, sql in enumerate(trace) if sql.startswith(f"INSERT INTO {table}"))
        core_update = next(index for index, sql in enumerate(trace) if sql.startswith("UPDATE items SET title"))
        self.assertLess(staged, core_update)

    def test_komga_scope_transition_stages_sidecar_before_core_and_keeps_ids(self):
        self.scope_transition("komga")

    def test_komga_scope_transition_later_refusal_rolls_back_sidecar(self):
        self.scope_transition("komga", refuse_core=True)

    def test_romm_platform_transition_stages_sidecar_before_core_and_keeps_ids(self):
        self.scope_transition("romm")

    def test_romm_platform_transition_later_refusal_rolls_back_registry_and_sidecar(self):
        self.scope_transition("romm", refuse_core=True)

    def test_plural_actions_refuse_without_an_explicit_representative(self):
        for provider, native, record_fields in (("komga", self.komga, "NULL, 'comic', NULL"),
                                                ("romm", self.romm, "NULL")):
            with self.subTest(provider=provider):
                item_id = self.item(media_type="comic" if provider == "komga" else "video_game")
                for external_id in ("first", "second"):
                    self.db.execute(f"INSERT INTO {provider}_records VALUES (?, ?, ?, {record_fields})", (external_id, item_id, "scope"))
                browser = Mock(return_value="https://provider.example/exact-second")
                scope = extracted(f"{provider}_sync", names={"item_action"}, feature_enabled=lambda key: True,
                                  configuration=lambda: {"url": "https://provider.example", "public_url": ""}, get_db=lambda: self.db,
                                  **{f"{provider}_records": SimpleNamespace(records_for_item=native["records_for_item"]),
                                     "komga_libraries" if provider == "komga" else "romm_client": SimpleNamespace(**{
                                         "browser_book_url" if provider == "komga" else "browser_rom_url": browser})})
                self.assertIsNone(scope["item_action"](item_id))
                browser.assert_not_called()
                candidate = self.book(komga_id="first", komga_library_id="scope") if provider == "komga" else self.rom(
                    romm_id="first", romm_platform_id="scope")
                native["persist_candidate"](self.db, candidate)
                self.assertIsNone(self.db.execute(f"SELECT {provider}_id FROM items WHERE id = ?", (item_id,)).fetchone()[0])
                self.assertIsNone(scope["item_action"](item_id))
                browser.assert_not_called()
                self.db.execute(f"UPDATE items SET {provider}_id = 'second' WHERE id = ?", (item_id,))
                self.assertEqual(scope["item_action"](item_id), "https://provider.example/exact-second")
                browser.assert_called_once_with("https://provider.example", "second", public_url=None)

    def test_abs_plural_holdings_do_not_invent_an_inline_representative(self):
        item_id = self.item(media_type="audiobook")
        self.db.executemany("INSERT INTO digital_holdings (id, item_id, provider, external_id, library_id) "
                            "VALUES (?, ?, 'audiobookshelf', ?, ?)", [(71, item_id, "abs-a", "lib-a"), (72, item_id, "abs-b", "lib-b")])
        scope = extracted("audiobookshelf", **self.boundaries, ItemValueError=ValueError,
                          series_memberships_svc=SimpleNamespace(add_metadata_memberships=Mock()))
        result = scope["persist_candidate"](self.db, {"abs_id": "abs-a", "abs_library_id": "lib-a", "title": "Provider title"})
        self.assertEqual(result["item_id"], item_id)
        self.assertIsNone(self.db.execute("SELECT abs_id FROM items WHERE id = ?", (item_id,)).fetchone()[0])
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT id, external_id FROM digital_holdings ORDER BY id")],
                         [(71, "abs-a"), (72, "abs-b")])

    def test_discogs_ambiguous_markers_never_choose_a_first_or_latest_identity(self):
        item_id = self.music_fixture()
        self.db.execute("INSERT INTO music_identifiers VALUES (79, ?, 'discogs_release_id', '222', 'Conflicting identity', 'manual')", (item_id,))
        self.actor()
        before = self.music_snapshot()
        self.assertIsNone(self.selection["get_selected_release_id"](self.db, item_id))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.selection["set_selected_release_id"](self.db, item_id, 333)
        self.db.commit()
        self.assertEqual(self.music_snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
