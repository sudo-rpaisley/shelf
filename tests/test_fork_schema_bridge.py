"""Isolated SQL regressions for the fork-first, provenance-aware schema bridge.

Runnable with stdlib unittest, without importing routers, the app, providers or
the changing integration tree. Only app/database.py is executed. Its config
imports are supplied as explicit non-secret test configuration. Historical SQL
dependencies below isolate parent-era prerequisites, not modern bootstrap SQL.
The published manifests and base schema are pinned to immutable-parent hashes.
"""

import ast
import hashlib
from pathlib import Path
import re
import sqlite3
import tempfile
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
FORK_HASH = "f1f582c6137d1ff5c4d8c5414ab1ae910e7d4dc5a037a11db8d6aaf525439a92"
UPSTREAM_HASH = "8ef6191d0070e6265ca75a546fd8ac1d7d1d17e63dbb75f9163fc00c7d9d864f"
SCHEMA_HASH = "a5df263dffd8fd4223d1e791e7379e1566b0fbe93a99c6e99961c3670a3aead2"
PRE_AUDIT_MANIFEST_HASH = "850112e1585c94058409c3e452c12c8abd6aec5f75b6f14e2e8502a0d1431f57"
PRE_SCAN_MANIFEST_HASH = "6f472f7d8c311d4dc28aeace5da5d81d8d011921e10846bc000fd22cad3d096f"


def load_owned_schema():
    class ConfigOnly(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.module == "app.config":
                return ast.copy_location(ast.Pass(), node)
            return node

    path = ROOT / "app/database.py"
    tree = ast.fix_missing_locations(ConfigOnly().visit(ast.parse(path.read_text())))
    module = types.ModuleType("isolated_fork_schema")
    module.__dict__.update(
        DATABASE_PATH=Path(":memory:"), COVERS_DIR=Path("unused-schema-test-covers"),
        GAME_PLATFORMS={"pc": "PC"},
    )
    exec(compile(tree, str(path), "exec"), module.__dict__)
    return module


# Parent-era prerequisites independent of the integrated MIGRATION_TABLES.
# The music fixture omits the exact main-only fields the bridge must add; all
# published main ALTERs still execute. It is not claimed as an entire deployed
# database capture (the immutable-parent exact-SQL check is separate).
FORK_PREREQUISITES = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE, password TEXT NOT NULL,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'viewer' CHECK(role IN ('admin','editor','viewer')),
    token_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE reading_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    status TEXT NOT NULL, date_started TEXT, date_finished TEXT, notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE series_meta (
    name TEXT PRIMARY KEY COLLATE NOCASE, description TEXT, source TEXT, updated_at TEXT,
    complete INTEGER DEFAULT NULL, hc_total INTEGER DEFAULT NULL,
    hc_missing INTEGER DEFAULT NULL, hc_checked_at TEXT DEFAULT NULL
);
CREATE TABLE music_releases (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    artist_credit TEXT, musicbrainz_release_id TEXT UNIQUE,
    musicbrainz_release_group_id TEXT, release_type TEXT, release_status TEXT,
    release_date TEXT, first_release_date TEXT, country TEXT, label TEXT,
    catalog_number TEXT, packaging TEXT, media_count INTEGER, format_summary TEXT,
    metadata_source TEXT, metadata_updated_at TEXT
);
CREATE TABLE music_identifiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    identifier_type TEXT NOT NULL, value TEXT NOT NULL, description TEXT,
    UNIQUE(item_id, identifier_type, value)
);
"""

FORK_PHYSICAL = """
CREATE TABLE location_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES location_nodes(id) ON DELETE RESTRICT,
    name TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0,
    legacy_location_id INTEGER UNIQUE REFERENCES locations(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX idx_location_nodes_root_name
    ON location_nodes(name COLLATE NOCASE) WHERE parent_id IS NULL;
CREATE UNIQUE INDEX idx_location_nodes_child_name
    ON location_nodes(parent_id, name COLLATE NOCASE) WHERE parent_id IS NOT NULL;
CREATE TABLE item_copies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    copy_number INTEGER NOT NULL DEFAULT 1,
    location_id INTEGER REFERENCES location_nodes(id) ON DELETE SET NULL,
    position_order INTEGER, condition TEXT, notes TEXT, acquired_date TEXT,
    acquisition_price REAL, copy_barcode TEXT UNIQUE,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(item_id, copy_number)
);
CREATE TABLE digital_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    provider TEXT NOT NULL, external_id TEXT NOT NULL, library_id TEXT,
    format TEXT, source_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(item_id, provider, external_id)
);
"""


def connect(path=":memory:", **kwargs):
    db = sqlite3.connect(str(path), **kwargs)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def apply_published(db, migrations):
    for version, description, sql in migrations:
        alter = re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", sql)
        if not alter or alter[2] not in {r[1] for r in db.execute(f"PRAGMA table_info({alter[1]})")}:
            db.execute(sql)
        db.execute(
            "INSERT INTO schema_version (version, description, applied_at) VALUES (?, ?, '2025-02-03 04:05:06')",
            (version, description),
        )


def historical_fork(schema, *, path=":memory:", physical=False, seed=True):
    db = connect(path)
    db.executescript(schema.SCHEMA + FORK_PREREQUISITES)
    apply_published(db, schema._FORK_CORE_MIGRATIONS)
    if seed:
        db.execute("INSERT INTO users (id, username, password, role) VALUES (7, 'old', 'not-a-credential', 'viewer')")
        db.execute("INSERT INTO items (id, title, reading_status, date_started, date_finished) "
                   "VALUES (11, 'Existing', 'read', '2025-01-01', '2025-01-02')")
        db.execute("INSERT INTO items (id, title, owned, media_type) VALUES (22, 'Wanted', 0, 'ebook')")
        db.execute("INSERT INTO reading_log (id, item_id, status, date_finished, notes) "
                   "VALUES (31, 11, 'read', '2025-01-02', 'legacy history')")
    apply_published(db, schema._FORK_MIGRATIONS[len(schema._FORK_CORE_MIGRATIONS):])
    if physical:
        db.executescript(FORK_PHYSICAL)
    db.commit()
    return db


def historical_bridge_before_audit(schema):
    """Build the exact prior v115 ledger, NOT a current DB with history deleted.

    Its operation tuples are pinned to the reviewed pre-audit manifest hash;
    audit_entries is created by that manifest's original adapted operation89.
    All old effects/history/provenance run before any synthetic audit data.
    """
    assert hashlib.sha256(repr(schema._PRE_AUDIT_MIGRATIONS).encode()).hexdigest() == PRE_AUDIT_MANIFEST_HASH
    db = connect()
    statements = list(schema._statements(schema.SCHEMA + schema.MIGRATION_TABLES + schema._PROVENANCE_SCHEMA))
    old_audit_ddl = next(sql for version, _d, sql in schema._PRE_AUDIT_MIGRATIONS if version == 89)
    for sql in statements:
        create = schema._CREATE.match(schema._uncomment(sql))
        if create:
            db.execute(old_audit_ddl if create[1] == "audit_entries" else sql)
    for version, description, sql in schema._PRE_AUDIT_MIGRATIONS:
        disposition = schema._apply_migration(db, version, sql)
        db.execute(
            "INSERT INTO schema_version (version,description,applied_at) VALUES (?,?,'2026-11-20 10:00:00')",
            (version, description),
        )
        schema._record_provenance(db, version, sql, disposition, "Pre-audit bridge operation")
    for sql in statements:
        if schema._INDEX.match(schema._uncomment(sql)):
            db.execute(sql)
    schema._seed_game_platforms(db)
    db.execute(
        "INSERT INTO schema_lineage (id,lineage,initial_lineage,manifest_sha256,recorded_at) "
        "VALUES (1,?,'fresh',?,'2026-11-20 10:00:00')",
        (schema._LINEAGE, PRE_AUDIT_MANIFEST_HASH),
    )
    db.commit()
    assert "security_library_id" not in schema._columns(db, "audit_entries")
    assert "security_user_id" not in schema._columns(db, "audit_entries")
    assert schema.inspect_schema_lineage(db) == "integrated"
    return db


def historical_bridge_before_scan(schema):
    assert hashlib.sha256(repr(schema._PRE_SCAN_MIGRATIONS).encode()).hexdigest() == PRE_SCAN_MANIFEST_HASH
    db = historical_bridge_before_audit(schema)
    for version, description, sql in schema._AUDIT_SECURITY_MIGRATIONS:
        disposition = schema._apply_migration(db, version, sql)
        db.execute("INSERT INTO schema_version(version,description) VALUES (?,?)", (version, description))
        schema._record_provenance(db, version, sql, disposition, "Pre-scan bridge operation")
    db.execute("UPDATE schema_lineage SET manifest_sha256=?", (PRE_SCAN_MANIFEST_HASH,))
    db.commit()
    assert "security_library_id" not in schema._columns(db, "scan_log")
    assert schema.inspect_schema_lineage(db) == "integrated"
    return db


def replace_table_schema(db, table, transform):
    """Make a REAL malformed DB; never patch the canonical schema/reference."""
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    definition = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
    temporary = "malformed_" + table
    changed = transform(definition)
    assert changed != definition
    db.execute(changed.replace(table, temporary, 1))
    db.execute(f'INSERT INTO "{temporary}" SELECT * FROM "{table}"')
    db.execute(f'DROP TABLE "{table}"')
    db.execute(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
    for sql in indexes:
        db.execute(sql)
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")


def dump(db):
    return tuple(db.iterdump())


def shape(db):
    result = {}
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        # SQLite reports no DEFAULT as None and explicit DEFAULT NULL as 'NULL'.
        # They are identical insert semantics; main's baked CREATE vs published
        # ALTERs used both spellings. No other default/constraint is normalized.
        columns = {r[1]: (r[2], r[3], None if r[4] == "NULL" else r[4], r[5])
                   for r in db.execute(f'PRAGMA table_info("{table}")')}
        fks = sorted((r[2], r[3], r[4], r[6]) for r in db.execute(f'PRAGMA foreign_key_list("{table}")'))
        indexes = sorted((r[1], r[2], r[4]) for r in db.execute(f'PRAGMA index_list("{table}")'))
        result[table] = (columns, fks, indexes)
    return result


class ForkSchemaBridgeTests(unittest.TestCase):
    def setUp(self):
        self.schema = load_owned_schema()
        self.connections = []

    def tearDown(self):
        for db in self.connections:
            db.close()

    def keep(self, db):
        self.connections.append(db)
        return db

    def fresh(self):
        db = self.keep(connect())
        self.schema._run_migrations(db)
        return db

    def fork(self, **kwargs):
        return self.keep(historical_fork(self.schema, **kwargs))

    def pre_audit(self):
        db = self.keep(historical_bridge_before_audit(self.schema))
        db.execute("INSERT INTO users (id,username,password,role) VALUES (7,'audit-user','not-a-credential','viewer')")
        db.execute("INSERT INTO libraries (id,name) VALUES (2,'Private audit library')")
        db.execute("INSERT INTO items (id,title,reading_status,owned) VALUES (11,'Private audit item','read',0)")
        db.execute("INSERT INTO items (id,title) VALUES (22,'Unmapped audit item')")
        db.execute("INSERT INTO library_items (item_id,library_id) VALUES (11,2)")
        db.execute("INSERT INTO reading_log (item_id,status) VALUES (11,'read')")
        db.execute("INSERT INTO audit_sessions (id,scope,location_name) VALUES (31,'library','Historical')")
        db.execute("INSERT INTO audit_entries (id,session_id,item_id,raw_code,result) "
                   "VALUES (91,31,11,'private-code','confirmed')")
        db.execute("INSERT INTO audit_entries (id,session_id,item_id,raw_code,result) "
                   "VALUES (92,31,NULL,'private-code','unexpected')")
        db.execute("INSERT INTO audit_entries (id,session_id,item_id,raw_code,result) "
                   "VALUES (93,31,22,'unmapped-code','unexpected')")
        db.commit()
        return db

    def assert_read_only_refusal(self, db, message=None):
        before, statements = dump(db), []
        db.set_trace_callback(statements.append)
        with self.assertRaises(self.schema.SchemaLineageError) as raised:
            self.schema._run_migrations(db)
        if message:
            self.assertRegex(str(raised.exception), message)
        db.set_trace_callback(None)
        self.assertEqual(dump(db), before)
        writes = [s for s in statements if s.lstrip().upper().startswith(
            ("CREATE ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ", "DROP ", "REPLACE ",
             "BEGIN IMMEDIATE", "BEGIN EXCLUSIVE")
        )]
        self.assertEqual(writes, [])

    def test_published_manifests_are_exact_and_numbers_do_not_collide(self):
        s = self.schema
        self.assertEqual(hashlib.sha256(repr(s._FORK_MIGRATIONS).encode()).hexdigest(), FORK_HASH)
        self.assertEqual(hashlib.sha256(repr(s._UPSTREAM_MIGRATIONS).encode()).hexdigest(), UPSTREAM_HASH)
        self.assertEqual(hashlib.sha256(s.SCHEMA.encode()).hexdigest(), SCHEMA_HASH)
        versions = [v for v, _d, _q in s.MIGRATIONS]
        self.assertEqual(len(versions), len(set(versions)))
        self.assertNotIn(47, versions)
        db = self.fresh()
        self.assertEqual(db.execute("SELECT description FROM schema_version WHERE version=24").fetchone()[0],
                         "Add komga_id column")
        row = db.execute("SELECT source_lineage, original_version FROM schema_migration_provenance WHERE version=67").fetchone()
        self.assertEqual(tuple(row), ("upstream7e0369e7", 24))
        self.assertEqual(s.inspect_schema_lineage(db), "integrated")

    def test_upstream_unknown_reserved_and_mixed_histories_refuse_before_writes(self):
        cases = (
            "UPDATE schema_version SET description='Add confirmed legacy book barcode mappings' WHERE version=24",
            "INSERT INTO schema_version (version, description) VALUES (47, 'reserved unknown')",
            "INSERT INTO schema_version (version, description) VALUES (999, 'unknown')",
            "ALTER TABLE items ADD COLUMN deleted_at TEXT",
            "CREATE TABLE romm_records (romm_id TEXT PRIMARY KEY)",
            "INSERT INTO settings VALUES ('holdings_foundation_backfill_v1','unknown')",
            "DELETE FROM schema_version WHERE version=30",
        )
        for sql in cases:
            with self.subTest(sql=sql):
                db = self.fork()
                db.execute(sql)
                db.commit()
                self.assert_read_only_refusal(db)

    def test_populated_unversioned_database_is_not_treated_as_fresh(self):
        db = self.keep(connect())
        db.executescript(self.schema.SCHEMA)
        db.execute("INSERT INTO items (title) VALUES ('Unattributed')")
        db.commit()
        self.assert_read_only_refusal(db)

    def test_read_only_unsupported_file_and_init_do_not_change_bytes_or_wal(self):
        with tempfile.TemporaryDirectory(prefix="fork-schema-", dir=ROOT) as directory:
            path = Path(directory) / "unsupported.db"
            db = historical_fork(self.schema, path=path)
            db.execute("UPDATE schema_version SET description='upstream collision' WHERE version=24")
            db.commit()
            db.close()
            before = path.read_bytes()
            ro = connect(f"file:{path}?mode=ro", uri=True)
            try:
                with self.assertRaises(self.schema.SchemaLineageError):
                    self.schema._run_migrations(ro)
            finally:
                ro.close()
            self.schema.DATABASE_PATH = path
            self.schema.COVERS_DIR = Path(directory) / "covers"
            with self.assertRaises(self.schema.SchemaLineageError):
                self.schema.init_db()
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(Path(str(path) + "-wal").exists())
            self.assertFalse(self.schema.COVERS_DIR.exists())

    def test_fresh_and_fork_upgrade_have_identical_central_shapes(self):
        fresh, fork = self.fresh(), self.fork(physical=True)
        self.schema._run_migrations(fork)
        self.assertEqual(shape(fork), shape(fresh))
        for db in (fresh, fork):
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger','view')").fetchall(), [])
        expected = {v for v, _d, _q in self.schema.MIGRATIONS}
        self.assertEqual({r[0] for r in fork.execute("SELECT version FROM schema_version")}, expected)

    def test_known_common_prefix_upgrade_and_pre_atomic_verified_column(self):
        db = self.keep(connect())
        db.executescript(self.schema.SCHEMA + FORK_PREREQUISITES)
        apply_published(db, [m for m in self.schema._FORK_CORE_MIGRATIONS if m[0] <= 14])
        db.execute("ALTER TABLE items ADD COLUMN manual_value REAL DEFAULT NULL")
        db.commit()
        self.assertEqual(self.schema.inspect_schema_lineage(db), "common-prefix")
        self.schema._run_migrations(db)
        self.assertEqual(db.execute("SELECT disposition FROM schema_migration_provenance WHERE version=15").fetchone()[0],
                         "verified-existing")

    def test_history_timestamps_retained_and_adaptations_are_not_raw_execution_claims(self):
        db = self.fork()
        original = [tuple(r) for r in db.execute("SELECT * FROM schema_version ORDER BY version")]
        self.schema._run_migrations(db)
        retained = [tuple(r) for r in db.execute("SELECT * FROM schema_version WHERE version<=66 ORDER BY version")]
        self.assertEqual(retained, original)
        self.assertEqual({r[0] for r in db.execute("SELECT disposition FROM schema_migration_provenance WHERE version<=66")},
                         {"legacy-recorded"})
        row = db.execute("SELECT * FROM schema_migration_provenance WHERE version=68").fetchone()
        self.assertEqual(row["original_version"], 25)
        self.assertEqual(row["disposition"], "adapted")
        self.assertNotEqual(row["source_sql_sha256"], row["effect_sql_sha256"])

    def test_replay_and_ensure_helpers_do_not_reseed_private_history_or_grants(self):
        db = self.fork()
        # This user arrived after main's published snapshots but BEFORE bridge
        # upgrade. They must not receive somebody else's legacy private state.
        db.execute("INSERT INTO users (id, username, password, role) VALUES (8, 'late-main', 'not-a-credential', 'viewer')")
        db.execute("DELETE FROM library_memberships WHERE user_id=7")
        db.commit()
        self.schema._run_migrations(db)
        db.execute("INSERT INTO users (id, username, password, role) VALUES (9, 'late-bridge', 'not-a-credential', 'viewer')")
        db.execute("INSERT INTO items (id, title, owned) VALUES (44, 'Late raw item', 1)")
        db.commit()
        before = dump(db)
        self.assertEqual(self.schema._run_migrations(db), [])
        for name in ("ensure_library_schema", "ensure_user_state_schema", "ensure_provider_series_schema",
                     "ensure_holdings_schema", "ensure_series_memberships_schema", "ensure_periodical_schema"):
            getattr(self.schema, name)(db)
        self.assertEqual(dump(db), before)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM user_reading_log").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM user_item_state WHERE user_id=8").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM user_item_state WHERE user_id=9").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM library_memberships").fetchone()[0], 0)
        self.assertIsNone(db.execute("SELECT 1 FROM item_copies WHERE item_id=44").fetchone())

    def test_uninitialized_ensure_refuses_without_lazy_ddl_or_seeds(self):
        db = self.fork()
        before = dump(db)
        with self.assertRaises(self.schema.SchemaLineageError):
            self.schema.ensure_library_schema(db)
        self.assertEqual(dump(db), before)

    def test_node_fk_ids_copy_metadata_and_legacy_visibility_are_preserved(self):
        db = self.fork(physical=True)
        db.execute("INSERT INTO locations (id, name) VALUES (1, 'Legacy room')")
        db.execute("INSERT INTO location_nodes (id, name) VALUES (1, 'Unrelated node')")
        db.execute("INSERT INTO location_nodes (id, name, legacy_location_id) VALUES (77, 'Mapped room', 1)")
        db.execute("INSERT INTO location_nodes (id, parent_id, name) VALUES (78, 77, 'Precise shelf')")
        db.execute("UPDATE items SET location_id=1, media_type='kids_book' WHERE id=11")
        db.execute("INSERT INTO items (id, title, isbn, media_type) VALUES (33, 'Twin', '9781234567897', 'book')")
        db.execute("UPDATE items SET isbn='9781234567897' WHERE id=11")
        db.execute("INSERT INTO item_copies "
                   "(id,item_id,copy_number,location_id,position_order,condition,notes,acquired_date,acquisition_price,copy_barcode,is_primary) "
                   "VALUES (501,11,4,78,19,'good','kept','2020-01-01',12.5,'COPY-501',1)")
        db.commit()
        original = dict(db.execute("SELECT * FROM item_copies WHERE id=501").fetchone())
        self.schema._run_migrations(db)
        copy = dict(db.execute("SELECT * FROM item_copies WHERE id=501").fetchone())
        self.assertEqual({k: copy[k] for k in original}, original)
        fk = {r[3]: r[2] for r in db.execute("PRAGMA foreign_key_list(item_copies)")}
        self.assertEqual(fk["location_id"], "location_nodes")
        self.assertEqual(db.execute("SELECT media_type FROM items WHERE id=11").fetchone()[0], "kids_book")
        self.assertIsNotNone(db.execute("SELECT 1 FROM items WHERE id=33").fetchone())
        self.assertIsNotNone(db.execute("SELECT 1 FROM user_item_state WHERE user_id=7 AND item_id=11").fetchone())
        self.assertIsNotNone(db.execute("SELECT 1 FROM library_items WHERE item_id=11").fetchone())
        self.assertEqual({r[2] for r in db.execute("PRAGMA foreign_key_list(audit_sessions)")}, {"location_nodes"})

    def test_flat_copy_fk_and_duplicate_primaries_refuse_without_repair(self):
        db = self.fork(physical=True)
        db.execute("ALTER TABLE item_copies RENAME TO wrong_domain_old")
        copy_ddl = re.search(r"CREATE TABLE item_copies \(.*?\n\);", FORK_PHYSICAL, re.S).group()
        db.execute(copy_ddl.replace("REFERENCES location_nodes(id)", "REFERENCES locations(id)"))
        db.execute("DROP TABLE wrong_domain_old")
        db.commit()
        self.assert_read_only_refusal(db, "foreign-key domain")
        db2 = self.fork(physical=True)
        db2.execute("INSERT INTO item_copies (item_id, copy_number, is_primary) VALUES (11,1,1)")
        db2.execute("INSERT INTO item_copies (item_id, copy_number, is_primary) VALUES (11,2,1)")
        db2.commit()
        self.assert_read_only_refusal(db2, "duplicate primary")

    def test_existing_foundation_marker_prevents_a_copy_or_digital_seed_replay(self):
        db = self.fork(physical=True)
        db.execute("INSERT INTO settings VALUES ('holdings_foundation_backfill_v1','1')")
        db.execute("UPDATE items SET abs_id='abs-kept', abs_library_id='abs-library' WHERE id=11")
        db.commit()
        self.schema._run_migrations(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM digital_holdings").fetchone()[0], 0)

    def test_ambiguous_root_and_conflicting_managed_index_refuse_before_ddl(self):
        db = self.fork(physical=True)
        db.execute("INSERT INTO locations (id,name) VALUES (4,'Same name')")
        db.execute("INSERT INTO location_nodes (id,name) VALUES (81,'Same name')")
        db.commit()
        self.assert_read_only_refusal(db, "location-to-node")
        db2 = self.fork(physical=True)
        db2.execute("CREATE INDEX idx_item_copies_one_primary ON item_copies(item_id)")
        db2.commit()
        self.assert_read_only_refusal(db2, "Conflicting managed index")

    def test_plural_connector_ids_are_adopted_without_collapsing_or_replacing_rows(self):
        db = self.fork(physical=True)
        db.execute("UPDATE items SET romm_id='r1',romm_platform_id='p1' WHERE id=11")
        db.execute("UPDATE items SET media_type='digital_manga',komga_id='k1',komga_library_id='l1',komga_series_id='s1' WHERE id=22")
        db.execute("INSERT INTO digital_holdings (id,item_id,provider,external_id,library_id,format,source_url) "
                   "VALUES (601,11,'romm','r2','p2','zip','https://example.invalid/kept')")
        db.execute("INSERT INTO digital_holdings (id,item_id,provider,external_id,library_id) VALUES (602,22,'komga','k2','l2')")
        db.commit()
        retained = dict(db.execute("SELECT * FROM digital_holdings WHERE id=601").fetchone())
        self.schema._run_migrations(db)
        self.assertEqual(dict(db.execute("SELECT * FROM digital_holdings WHERE id=601").fetchone()), retained)
        self.assertEqual({r[0] for r in db.execute("SELECT romm_id FROM romm_records")}, {"r1", "r2"})
        self.assertEqual({r[0] for r in db.execute("SELECT komga_id FROM komga_records")}, {"k1", "k2"})
        self.assertEqual(db.execute("SELECT series_id FROM komga_records WHERE komga_id='k1'").fetchone()[0], "s1")
        self.assertIsNone(db.execute("SELECT series_id FROM komga_records WHERE komga_id='k2'").fetchone()[0])
        self.assertEqual(db.execute("SELECT romm_id FROM items WHERE id=11").fetchone()[0], "r1")
        self.assertIsNone(db.execute("SELECT 1 FROM item_copies WHERE item_id=22").fetchone())

    def test_incomplete_conflicting_connector_evidence_refuses_before_mutation(self):
        for second_item, library in ((11, None), (22, "p1"), (11, "p2")):
            with self.subTest(second_item=second_item, library=library):
                db = self.fork(physical=True)
                db.execute("UPDATE items SET romm_id='same',romm_platform_id=? WHERE id=11", (None if library is None else "p1",))
                if library is not None:
                    db.execute("INSERT INTO digital_holdings (item_id,provider,external_id,library_id) VALUES (?,'romm','same',?)",
                               (second_item, library))
                db.commit()
                self.assert_read_only_refusal(db)

    def test_complete_exact_evidence_can_fill_missing_inline_platform_without_guessing(self):
        db = self.fork(physical=True)
        db.execute("UPDATE items SET romm_id='r1', romm_platform_id=' ' WHERE id=11")
        db.execute("INSERT INTO digital_holdings (id,item_id,provider,external_id,library_id) "
                   "VALUES (601,11,'romm','r1','p1')")
        db.commit()
        self.schema._run_migrations(db)
        self.assertEqual(db.execute("SELECT platform_id FROM romm_records WHERE romm_id='r1'").fetchone()[0], "p1")
        # The adapter records the exact sidecar evidence; it does not rewrite
        # an unrelated inline field or destroy the existing digital row.
        self.assertEqual(db.execute("SELECT romm_platform_id FROM items WHERE id=11").fetchone()[0], " ")
        self.assertEqual(db.execute("SELECT library_id FROM digital_holdings WHERE id=601").fetchone()[0], "p1")

    def test_partial_seed_or_bridge_bookkeeping_refuses_replay(self):
        db = self.fork()
        db.execute("DELETE FROM schema_version WHERE version=56")
        db.commit()
        self.assert_read_only_refusal(db)
        integrated = self.fresh()
        integrated.execute("DELETE FROM schema_migration_provenance WHERE version=56")
        integrated.execute("DELETE FROM schema_version WHERE version=56")
        integrated.commit()
        self.assert_read_only_refusal(integrated)

    def test_effect_history_and_provenance_rollback_together_and_retry(self):
        class FailInsert:
            def __init__(self, db, target):
                self.db, self.target = db, target

            def execute(self, sql, params=()):
                if self.target in sql and params and params[0] == 99:
                    raise RuntimeError("simulated interruption at the durable history boundary")
                return self.db.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.db, name)

        for target in ("INSERT INTO schema_version", "INSERT INTO schema_migration_provenance"):
            with self.subTest(target=target):
                db = self.fork()
                before = dump(db)
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    self.schema._run_migrations(FailInsert(db, target))
                self.assertEqual(dump(db), before)
                self.assertNotIn("asin", {r[1] for r in db.execute("PRAGMA table_info(items)")})
                self.assertNotIn("schema_lineage", self.schema._tables(db))
                self.schema._run_migrations(db)
                self.assertEqual(self.schema.inspect_schema_lineage(db), "integrated")

    def test_fresh_interruption_rolls_back_all_bootstrap_tables(self):
        db = self.keep(connect())

        class FailFresh:
            def execute(self, sql, params=()):
                if "INSERT INTO schema_version" in sql and params and params[0] == 80:
                    raise RuntimeError("fresh migration interruption")
                return db.execute(sql, params)

            def __getattr__(self, name):
                return getattr(db, name)

        with self.assertRaisesRegex(RuntimeError, "fresh migration interruption"):
            self.schema._run_migrations(FailFresh())
        self.assertEqual(self.schema._tables(db), set())
        self.assertFalse(db.in_transaction)
        self.schema._run_migrations(db)
        self.assertEqual(self.schema.inspect_schema_lineage(db), "integrated")

    def test_nested_runner_does_not_commit_or_rollback_callers_work(self):
        db = self.fork()
        db.execute("INSERT INTO settings VALUES ('caller-uncommitted','keep')")
        self.schema._run_migrations(db)
        self.assertTrue(db.in_transaction)
        self.assertEqual(self.schema.inspect_schema_lineage(db), "integrated")
        db.rollback()
        self.assertEqual(self.schema.inspect_schema_lineage(db), "fork")
        self.assertIsNone(db.execute("SELECT 1 FROM settings WHERE key='caller-uncommitted'").fetchone())

    def test_upgrade_feature_defaults_and_shared_wishlist_are_one_time(self):
        db = self.fork()
        db.execute("INSERT INTO settings VALUES ('feature.valuation','0')")
        db.execute("INSERT INTO settings VALUES ('feature.hardcover','0')")
        db.commit()
        self.schema._run_migrations(db)
        defaults = dict(db.execute("SELECT key,value FROM settings WHERE key LIKE 'feature.%'"))
        for key in ("price_alerts", "collector", "buy_links", "release_radar", "api"):
            self.assertEqual(defaults[f"feature.{key}"], "0")
        self.assertEqual([r[0] for r in db.execute("SELECT item_id FROM list_items")], [22])
        db.execute("DELETE FROM list_items")
        db.execute("UPDATE settings SET value='1' WHERE key='feature.api'")
        db.commit()
        self.schema._run_migrations(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM list_items").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT value FROM settings WHERE key='feature.api'").fetchone()[0], "1")

    def test_deleting_a_managed_schema_column_or_index_is_not_silently_healed(self):
        db = self.fresh()
        db.execute("DROP INDEX idx_items_asin")
        db.commit()
        self.assert_read_only_refusal(db, "Missing initialized indexes")
        db2 = self.fresh()
        db2.execute("DROP INDEX idx_items_asin")
        db2.execute("ALTER TABLE items DROP COLUMN asin")
        db2.commit()
        self.assert_read_only_refusal(db2, "Incomplete initialized schema")

    def test_new_sql_defects_do_not_get_missing_table_or_duplicate_forgiveness(self):
        for sql in ("ALTER TABLE missing_table ADD COLUMN value TEXT", "ALTER TABLE items ADD COLUMN title TEXT"):
            with self.subTest(sql=sql):
                db = self.fresh()
                before = dump(db)
                original = self.schema.MIGRATIONS
                future_version = max(version for version, _d, _s in original) + 1
                self.schema.MIGRATIONS = tuple(original) + ((future_version, "Deliberate regression defect", sql),)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        self.schema._run_migrations(db)
                finally:
                    self.schema.MIGRATIONS = original
                self.assertEqual(dump(db), before)

    def test_initialized_provenance_tampering_is_read_only_refusal(self):
        db = self.fresh()
        db.execute("UPDATE schema_migration_provenance SET source_sql_sha256='not-the-original' WHERE version=68")
        db.commit()
        self.assert_read_only_refusal(db)

    def test_audit_snapshot_backfill_is_bound_only_and_survives_purge(self):
        db = self.pre_audit()
        logs = self.schema._run_migrations(db)
        self.assertEqual(len(logs), 6)
        snapshots = [tuple(r) for r in db.execute(
            "SELECT id,security_library_id,security_user_id FROM audit_entries ORDER BY id"
        )]
        self.assertEqual(snapshots, [(91, 2, None), (92, None, None), (93, None, None)])
        # The same raw code is NOT evidence for assigning the historical orphan.
        db.execute("DELETE FROM items WHERE id=11")
        db.execute("DELETE FROM libraries WHERE id=2")
        db.commit()
        purged = db.execute("SELECT item_id,security_library_id,security_user_id,raw_code "
                            "FROM audit_entries WHERE id=91").fetchone()
        self.assertEqual(tuple(purged), (None, 2, None, "private-code"))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_entries").fetchone()[0], 3)
        self.assertIsNone(db.execute("SELECT security_library_id FROM audit_entries WHERE id=92").fetchone()[0])
        self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_audit_actor_and_library_snapshots_are_plain_nullable_non_fk_values(self):
        db = self.fresh()
        for name in ("security_library_id", "security_user_id"):
            column = self.schema._columns(db, "audit_entries")[name]
            self.assertEqual((column["type"], column["notnull"], column["dflt_value"], column["pk"]),
                             ("INTEGER", 0, None, 0))
        self.assertFalse({"security_library_id", "security_user_id"} & {
            row["from"] for row in db.execute("PRAGMA foreign_key_list(audit_entries)")
        })
        db.execute("INSERT INTO users (id,username,password) VALUES (7,'actor','not-a-credential')")
        db.execute("INSERT INTO libraries (id,name) VALUES (2,'Snapshot library')")
        db.execute("INSERT INTO audit_sessions (id,scope,location_name) VALUES (31,'library','Current')")
        db.execute("INSERT INTO audit_entries "
                   "(id,session_id,raw_code,result,security_library_id,security_user_id) "
                   "VALUES (91,31,'retained-code','unexpected',2,7)")
        db.execute("DELETE FROM users WHERE id=7")
        db.execute("DELETE FROM libraries WHERE id=2")
        db.commit()
        self.assertEqual(tuple(db.execute("SELECT security_library_id,security_user_id "
                                          "FROM audit_entries WHERE id=91").fetchone()), (2, 7))
        self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall(), [])

    def test_prior_bridge_upgrade_preserves_every_old_history_and_provenance_row(self):
        db = self.pre_audit()
        history = [tuple(r) for r in db.execute("SELECT * FROM schema_version ORDER BY version")]
        ledger = [tuple(r) for r in db.execute("SELECT * FROM schema_migration_provenance ORDER BY version")]
        lineage = dict(db.execute("SELECT * FROM schema_lineage").fetchone())
        self.schema._run_migrations(db)
        self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM schema_version WHERE version<=115 ORDER BY version")], history)
        self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM schema_migration_provenance WHERE version<=115 ORDER BY version")], ledger)
        current_lineage = dict(db.execute("SELECT * FROM schema_lineage").fetchone())
        self.assertEqual({k: current_lineage[k] for k in lineage if k != "manifest_sha256"},
                         {k: v for k, v in lineage.items() if k != "manifest_sha256"})
        self.assertEqual(current_lineage["manifest_sha256"], self.schema._MANIFEST_HASH)
        self.assertNotEqual(current_lineage["manifest_sha256"], PRE_AUDIT_MANIFEST_HASH)
        for version, _description, sql in self.schema._AUDIT_SECURITY_MIGRATIONS:
            row = db.execute("SELECT * FROM schema_migration_provenance WHERE version=?", (version,)).fetchone()
            self.assertEqual((row["source_lineage"], row["original_version"], row["disposition"]),
                             ("fork-main-bridge-v1", version, "executed"))
            self.assertEqual(row["source_sql_sha256"], hashlib.sha256(sql.encode()).hexdigest())
            self.assertEqual(row["effect_sql_sha256"], row["source_sql_sha256"])
        fresh = self.fresh()
        self.assertEqual(shape(db), shape(fresh))

    def test_audit_extension_repeat_does_not_rebind_snapshots_or_replay_any_seeds(self):
        db = self.pre_audit()
        # Old one-time snapshots ran before these users/items existed. Even a
        # deliberately emptied non-security seed table must stay empty now.
        db.execute("DELETE FROM game_platforms")
        db.commit()
        self.schema._run_migrations(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM game_platforms").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM user_reading_log").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM library_memberships").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM list_items").fetchone()[0], 0)
        db.execute("INSERT INTO libraries (id,name) VALUES (3,'Moved later')")
        db.execute("UPDATE library_items SET library_id=3 WHERE item_id=11")
        db.commit()
        before = dump(db)
        self.assertEqual(self.schema._run_migrations(db), [])
        self.schema.ensure_audit_schema(db)
        self.assertEqual(dump(db), before)
        self.assertEqual(db.execute("SELECT security_library_id FROM audit_entries WHERE id=91").fetchone()[0], 2)
        self.assertIsNone(db.execute("SELECT security_library_id FROM audit_entries WHERE id=92").fetchone()[0])

    def test_audit_extension_failure_rolls_back_columns_snapshot_ledger_and_manifest(self):
        class FailAudit:
            def __init__(self, db, target, version):
                self.db, self.target, self.version = db, target, version

            def execute(self, sql, params=()):
                if self.target in sql and (self.version is None or (params and params[0] == self.version)):
                    raise RuntimeError("audit migration interruption")
                return self.db.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.db, name)

        for target, version in (("INSERT INTO schema_version", 116),
                                ("INSERT INTO schema_migration_provenance", 117),
                                ("INSERT INTO schema_migration_provenance", 118),
                                ("INSERT INTO schema_lineage", None)):
            with self.subTest(target=target, version=version):
                db = self.pre_audit()
                before = dump(db)
                with self.assertRaisesRegex(RuntimeError, "audit migration interruption"):
                    self.schema._run_migrations(FailAudit(db, target, version))
                self.assertEqual(dump(db), before)
                self.assertNotIn("security_library_id", self.schema._columns(db, "audit_entries"))
                self.assertEqual(self.schema.inspect_schema_lineage(db), "integrated")
                self.schema._run_migrations(db)
                self.assertEqual(db.execute("SELECT security_library_id FROM audit_entries WHERE id=91").fetchone()[0], 2)

    def test_prior_bridge_rejects_partial_unrecorded_audit_effects_and_missing_old_history(self):
        for sql in (
            "ALTER TABLE audit_entries ADD COLUMN security_library_id INTEGER",
            "UPDATE schema_lineage SET manifest_sha256='unknown-manifest'",
            "UPDATE schema_lineage SET manifest_sha256='" + self.schema._MANIFEST_HASH + "'",
        ):
            with self.subTest(sql=sql):
                db = self.pre_audit()
                db.execute(sql)
                db.commit()
                self.assert_read_only_refusal(db)
        db = self.pre_audit()
        db.execute("DELETE FROM schema_migration_provenance WHERE version=115")
        db.execute("DELETE FROM schema_version WHERE version=115")
        db.commit()
        self.assert_read_only_refusal(db)

    def test_audit_ensure_requires_current_initialized_security_columns(self):
        old = self.pre_audit()
        before = dump(old)
        with self.assertRaises(self.schema.SchemaLineageError):
            self.schema.ensure_audit_schema(old)
        self.assertEqual(dump(old), before)
        db = self.keep(sqlite3.connect(":memory:", factory=self.schema._Connection))
        db.row_factory = sqlite3.Row
        self.schema._run_migrations(db)
        db._schema_lineage_verified = True
        db.execute("ALTER TABLE audit_entries DROP COLUMN security_user_id")
        db.commit()
        before = dump(db)
        with self.assertRaisesRegex(self.schema.SchemaLineageError, "durable security snapshots"):
            self.schema.ensure_audit_schema(db)
        self.assertEqual(dump(db), before)

    def test_staged_connection_helper_is_explicit_schema_only_and_reports_lineage(self):
        db = self.keep(connect())
        globals_before = (self.schema.DATABASE_PATH, self.schema.COVERS_DIR)
        report = self.schema.validate_and_migrate_staged_connection(db)
        self.assertEqual(set(report), {"initial_lineage", "lineage", "manifest_sha256", "migration_logs"})
        self.assertEqual((report["initial_lineage"], report["lineage"]), ("fresh", "integrated"))
        self.assertEqual(report["manifest_sha256"], self.schema._MANIFEST_HASH)
        self.assertTrue(report["migration_logs"])
        self.assertFalse(db.in_transaction)
        self.assertEqual((self.schema.DATABASE_PATH, self.schema.COVERS_DIR), globals_before)
        self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type IN ('view','trigger')").fetchall(), [])
        second = self.schema.validate_and_migrate_staged_connection(db)
        self.assertEqual((second["initial_lineage"], second["lineage"], second["migration_logs"]),
                         ("integrated", "integrated", []))
        fork = self.fork()
        self.assertEqual(self.schema.validate_and_migrate_staged_connection(fork)["initial_lineage"], "fork")
        old_bridge = self.pre_audit()
        self.assertEqual(len(self.schema.validate_and_migrate_staged_connection(old_bridge)["migration_logs"]), 6)

    def test_staged_connection_helper_refuses_ambient_transactions_or_unsupported_history(self):
        db = self.fork()
        db.execute("INSERT INTO settings VALUES ('caller-pending','keep')")
        before = dump(db)
        with self.assertRaisesRegex(ValueError, "idle sqlite3.Row"):
            self.schema.validate_and_migrate_staged_connection(db)
        self.assertTrue(db.in_transaction)
        self.assertEqual(dump(db), before)
        db.rollback()
        db.execute("UPDATE schema_version SET description='upstream collision' WHERE version=24")
        db.commit()
        before = dump(db)
        with self.assertRaises(self.schema.SchemaLineageError):
            self.schema.validate_and_migrate_staged_connection(db)
        self.assertEqual(dump(db), before)
        bare = self.keep(sqlite3.connect(":memory:"))
        with self.assertRaisesRegex(ValueError, "idle sqlite3.Row"):
            self.schema.validate_and_migrate_staged_connection(bare)
        self.assertEqual(bare.execute("SELECT name FROM sqlite_master").fetchall(), [])

    def test_empty_seed_targets_and_erased_trailing_history_refuse_without_file_changes(self):
        cases = (("library_memberships", (62, 63, 64)),
                 ("user_reading_log", (56,)), ("user_item_state", (55, 56)))
        for target, versions in cases:
            with self.subTest(target=target), tempfile.TemporaryDirectory(prefix="seed-refusal-", dir=ROOT) as directory:
                path = Path(directory) / "fork.db"
                db = self.keep(historical_fork(self.schema, path=path))
                db.execute(f'DELETE FROM "{target}"')
                if target == "user_item_state":
                    db.execute("DELETE FROM user_reading_log")
                for version in versions:
                    db.execute("DELETE FROM schema_version WHERE version=?", (version,))
                db.execute("INSERT INTO users(id,username,password) VALUES(8,'later-user','synthetic')")
                db.commit()
                history = [tuple(r) for r in db.execute("SELECT * FROM schema_version ORDER BY version")]
                before = path.read_bytes()
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM "{target}"').fetchone()[0], 0)
                self.assert_read_only_refusal(db, "Ambiguous preexisting")
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM "{target}"').fetchone()[0], 0)
                self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM schema_version ORDER BY version")], history)
                self.assertIsNone(db.execute("SELECT 1 FROM user_reading_log WHERE user_id=8").fetchone())
                self.assertFalse(Path(str(path) + "-wal").exists())

    def test_tracked_common_prefix_without_domains_can_seed_once(self):
        db = self.keep(connect())
        db.executescript(self.schema.SCHEMA + FORK_PREREQUISITES)
        apply_published(db, [m for m in self.schema._FORK_CORE_MIGRATIONS if m[0] <= 23])
        db.execute("INSERT INTO users(id,username,password,role) VALUES(7,'pre-feature','synthetic','viewer')")
        db.execute("INSERT INTO items(id,title,reading_status) VALUES(11,'Pre-feature','read')")
        db.execute("INSERT INTO reading_log(item_id,status) VALUES(11,'read')")
        db.commit()
        self.assertFalse({"user_item_state", "user_reading_log", "libraries", "library_memberships"} & self.schema._tables(db))
        self.assertEqual(self.schema.inspect_schema_lineage(db), "common-prefix")
        self.schema._run_migrations(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM user_reading_log WHERE user_id=7").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM library_memberships WHERE user_id=7").fetchone()[0], 1)
        self.assertEqual(self.schema._run_migrations(db), [])

    def test_real_malformed_fork_and_integrated_constraints_refuse_without_reference_poisoning(self):
        cases = (
            ("users", lambda sql: sql.replace("UNIQUE COLLATE NOCASE", "COLLATE NOCASE")),
            ("users", lambda sql: sql.replace("COLLATE NOCASE", "COLLATE BINARY")),
            ("users", lambda sql: re.sub(r"(password\s+TEXT)\s+NOT NULL", r"\1", sql)),
            ("users", lambda sql: sql.replace("DEFAULT 'viewer'", "DEFAULT 'admin'")),
            ("users", lambda sql: re.sub(r"(display_name\s+TEXT)(,)", r'\1 DEFAULT "NULL"\2', sql)),
            ("users", lambda sql: re.sub(r"CHECK\s*\(role\s+IN\s*\('admin',\s*'editor',\s*'viewer'\)\)", "", sql)),
            ("users", lambda sql: sql.replace(" AUTOINCREMENT", "")),
            ("users", lambda sql: re.sub(r"(password\s+TEXT NOT NULL)", r"\1 CHECK(length(password)>0)", sql)),
            ("item_copies", lambda sql: re.sub(r"(copy_barcode\s+TEXT)\s+UNIQUE", r"\1", sql)),
        )
        for integrated in (False, True):
            for case_number, (table, transform) in enumerate(cases):
                with self.subTest(integrated=integrated, table=table, transform_case=case_number):
                    db = self.fresh() if integrated else self.fork(physical=True)
                    reference = self.schema._reference_schema()
                    try:
                        canonical = self.schema._table_contract(reference, table)
                        unique = self.schema._index_shape(reference, table)
                    finally:
                        reference.close()
                    replace_table_schema(db, table, transform)
                    self.assert_read_only_refusal(db)
                    # Neither the module manifest nor the trusted oracle was
                    # altered to make the malformed fixture appear canonical.
                    reference = self.schema._reference_schema()
                    try:
                        self.assertEqual(self.schema._table_contract(reference, table), canonical)
                        self.assertEqual(self.schema._index_shape(reference, table), unique)
                    finally:
                        reference.close()

    def test_semantically_equivalent_unique_index_and_sql_spellings_are_supported(self):
        db = self.fork()
        replace_table_schema(db, "users", lambda sql: sql.replace("UNIQUE COLLATE NOCASE", "COLLATE NOCASE"))
        db.execute("CREATE UNIQUE INDEX preserved_username_identity ON users(username COLLATE NOCASE DESC)")
        db.commit()
        self.schema._run_migrations(db)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO users(username,password) VALUES('OLD','synthetic')")
        db.rollback()
        self.assertEqual(self.schema._default_shape(None), self.schema._default_shape("((NULL))"))
        self.assertNotEqual(self.schema._default_shape(None), self.schema._default_shape('(\"NULL\")'))
        self.assertNotEqual(self.schema._default_shape("'manual'"), self.schema._default_shape("'Manual'"))

    def test_corrupt_integrated_foreign_keys_refuse_all_entrypoints_before_wal_or_writes(self):
        with tempfile.TemporaryDirectory(prefix="integrated-fk-", dir=ROOT) as directory:
            path = Path(directory) / "corrupt.db"
            db = self.keep(connect(path))
            self.schema._run_migrations(db)
            db.execute("INSERT INTO items(id,title) VALUES(11,'Dangling library')")
            db.commit()
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("INSERT INTO library_items(item_id,library_id) VALUES(11,999)")
            db.commit()
            db.execute("PRAGMA foreign_keys=ON")
            before, contents = path.read_bytes(), dump(db)
            self.assert_read_only_refusal(db, "foreign-key violations")
            for call in (self.schema.inspect_schema_lineage, self.schema.validate_and_migrate_staged_connection):
                with self.subTest(entrypoint=call.__name__):
                    with self.assertRaisesRegex(self.schema.SchemaLineageError, "foreign-key violations"):
                        call(db)
                    self.assertEqual(dump(db), contents)
                    self.assertEqual(path.read_bytes(), before)
            self.schema.DATABASE_PATH = path
            with self.assertRaisesRegex(self.schema.SchemaLineageError, "foreign-key violations"):
                with self.schema.get_db():
                    self.fail("Corrupt DB reached the request/view handoff")
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(Path(str(path) + "-wal").exists())

    def test_classifier_preserves_caller_transaction_and_idle_callback_hooks(self):
        db = self.fork()
        db.execute("INSERT INTO settings VALUES('caller-snapshot','uncommitted')")
        before = dump(db)
        self.assertEqual(self.schema.inspect_schema_lineage(db), "fork")
        self.assertTrue(db.in_transaction)
        self.assertEqual(dump(db), before)
        db.execute("DELETE FROM schema_version WHERE version=56")
        before = dump(db)
        with self.assertRaises(self.schema.SchemaLineageError):
            self.schema.inspect_schema_lineage(db)
        self.assertTrue(db.in_transaction)
        self.assertEqual(dump(db), before)
        db.rollback()
        hooked = self.keep(sqlite3.connect(":memory:", factory=self.schema._Connection))
        hooked.row_factory = sqlite3.Row
        self.schema._run_migrations(hooked)
        called = []
        callback = lambda: called.append("committed")
        hooked._after_commit.append(callback)
        self.assertEqual(self.schema.inspect_schema_lineage(hooked), "integrated")
        self.assertFalse(hooked.in_transaction)
        self.assertEqual(hooked._after_commit, [callback])
        self.assertEqual(called, [])

    def test_two_connection_wal_upgrade_uses_one_snapshot_then_rechecks_under_lock(self):
        with tempfile.TemporaryDirectory(prefix="schema-wal-", dir=ROOT) as directory:
            path = Path(directory) / "fork.db"
            first = historical_fork(self.schema, path=path)
            first.execute("PRAGMA journal_mode=WAL")
            other = connect(path)
            schema = self.schema

            class PausedInventory:
                fired = False

                def __init__(self):
                    self.history_snapshots, self.other_logs = [], None

                def execute(self, sql, params=()):
                    cursor = first.execute(sql, params)
                    if not self.fired and sql.startswith("SELECT name FROM sqlite_master WHERE type ="):
                        self.fired = True
                        rows = cursor.fetchall()
                        # A real read snapshot stays active while the other WAL
                        # writer commits; no threads/fake database are involved.
                        self.read_transaction_active = first.in_transaction
                        self.other_logs = schema._run_migrations(other)
                        return iter(rows)
                    if sql == "SELECT version, description FROM schema_version":
                        rows = cursor.fetchall()
                        self.history_snapshots.append({r["version"] for r in rows})
                        return iter(rows)
                    return cursor

                def __getattr__(self, name):
                    return getattr(first, name)

            paused = PausedInventory()
            try:
                self.assertEqual(schema._run_migrations(paused), [])
                self.assertTrue(paused.read_transaction_active)
                self.assertTrue(paused.other_logs)
                self.assertEqual(paused.history_snapshots[0], {v for v, _d, _s in schema._FORK_MIGRATIONS})
                self.assertEqual(paused.history_snapshots[1], {v for v, _d, _s in schema.MIGRATIONS})
                self.assertEqual(first.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0], len(schema.MIGRATIONS))
                self.assertEqual(first.execute("SELECT COUNT(*) FROM schema_migration_provenance").fetchone()[0], len(schema.MIGRATIONS))
                self.assertEqual(first.execute("SELECT COUNT(*) FROM user_reading_log").fetchone()[0], 1)
                self.assertEqual(first.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertFalse(first.in_transaction)
            finally:
                first.close()
                other.close()

    def test_individual_lineage_and_disposition_tampers_refuse_without_mutation(self):
        changes = (
            "UPDATE schema_lineage SET initial_lineage='unsupported'",
            "UPDATE schema_lineage SET initial_lineage='integrated'",
            "UPDATE schema_lineage SET initial_lineage='fork'",
            "UPDATE schema_lineage SET initial_lineage='common-prefix'",
            "UPDATE schema_lineage SET lineage='unknown-origin'",
            "UPDATE schema_lineage SET id=2",
            "UPDATE schema_migration_provenance SET disposition='executed' WHERE version=68",
            "UPDATE schema_migration_provenance SET disposition='legacy-recorded' WHERE version=68",
            "UPDATE schema_migration_provenance SET disposition='verified-existing' WHERE version=68",
            "UPDATE schema_migration_provenance SET disposition='verified-existing' WHERE version=118",
            "UPDATE schema_migration_provenance SET disposition='executed' WHERE version=46",
            "UPDATE schema_migration_provenance SET disposition='verified-existing' WHERE version=27",
            "UPDATE schema_migration_provenance SET disposition='executed' WHERE version=31",
            "UPDATE schema_migration_provenance SET disposition='verified-existing' WHERE version=1",
            "UPDATE schema_migration_provenance SET disposition='verified-existing' WHERE version=119",
            "UPDATE schema_migration_provenance SET disposition='adapted' WHERE version=55",
            "UPDATE schema_migration_provenance SET source_lineage='fork-main43e244f5' WHERE version=68",
            "UPDATE schema_migration_provenance SET original_version=26 WHERE version=68",
            "UPDATE schema_migration_provenance SET effect_sql_sha256='forged' WHERE version=68",
        )
        for sql in changes:
            with self.subTest(sql=sql):
                db = self.fresh()
                db.execute("PRAGMA ignore_check_constraints=ON")
                db.execute(sql)
                db.commit()
                db.execute("PRAGMA ignore_check_constraints=OFF")
                self.assert_read_only_refusal(db)
        fork = self.fork()
        self.schema._run_migrations(fork)
        fork.execute("UPDATE schema_lineage SET initial_lineage='fresh'")
        fork.commit()
        self.assert_read_only_refusal(fork)

    def test_scan_snapshots_upgrade_both_old_manifests_and_survive_item_purge(self):
        for previous, expected_operations in ((historical_bridge_before_audit, 6), (historical_bridge_before_scan, 3)):
            with self.subTest(previous=previous.__name__):
                db = self.keep(previous(self.schema))
                db.execute("INSERT INTO users(id,username,password) VALUES(7,'scan-actor','synthetic')")
                db.execute("INSERT INTO libraries(id,name) VALUES(2,'Private scan library')")
                db.execute("INSERT INTO items(id,title,reading_status,owned) VALUES(11,'Private scanned','read',0)")
                db.execute("INSERT INTO items(id,title) VALUES(22,'Unmapped scanned')")
                db.execute("INSERT INTO library_items(item_id,library_id) VALUES(11,2)")
                db.execute("INSERT INTO scan_log(id,isbn,result,item_id) VALUES(101,'same-private-code','found',11)")
                db.execute("INSERT INTO scan_log(id,isbn,result,item_id) VALUES(102,'same-private-code','unknown',NULL)")
                db.execute("INSERT INTO scan_log(id,isbn,result,item_id) VALUES(103,'unmapped-code','found',22)")
                db.execute("DELETE FROM game_platforms")
                db.commit()
                history = [tuple(r) for r in db.execute("SELECT * FROM schema_version ORDER BY version")]
                ledger = [tuple(r) for r in db.execute("SELECT * FROM schema_migration_provenance ORDER BY version")]
                logs = self.schema._run_migrations(db)
                self.assertEqual(len(logs), expected_operations)
                snapshots = [tuple(r) for r in db.execute("SELECT id,security_library_id,security_user_id FROM scan_log ORDER BY id")]
                self.assertEqual(snapshots, [(101, 2, None), (102, None, None), (103, None, None)])
                cutoff = 115 if previous is historical_bridge_before_audit else 118
                self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM schema_version WHERE version<=? ORDER BY version", (cutoff,))], history)
                self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM schema_migration_provenance WHERE version<=? ORDER BY version", (cutoff,))], ledger)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM game_platforms").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM library_memberships").fetchone()[0], 0)
                # Published scan_log has NO ACTION, so the purge writer clears
                # this FK explicitly. That must not erase durable snapshots.
                db.execute("UPDATE scan_log SET item_id=NULL WHERE item_id=11")
                db.execute("DELETE FROM items WHERE id=11")
                db.execute("DELETE FROM libraries WHERE id=2")
                db.commit()
                self.assertEqual(tuple(db.execute("SELECT item_id,security_library_id,security_user_id,isbn FROM scan_log WHERE id=101").fetchone()),
                                 (None, 2, None, "same-private-code"))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM scan_log").fetchone()[0], 3)
                before = dump(db)
                self.assertEqual(self.schema._run_migrations(db), [])
                self.schema.ensure_scan_history_schema(db)
                self.assertEqual(dump(db), before)
                self.assertEqual(shape(db), shape(self.fresh()))

    def test_scan_snapshot_values_have_no_fks_and_required_columns_fail_closed(self):
        db = self.fresh()
        for name in ("security_library_id", "security_user_id"):
            column = self.schema._columns(db, "scan_log")[name]
            self.assertEqual((column["type"], column["notnull"], column["dflt_value"], column["pk"]), ("INTEGER", 0, None, 0))
        self.assertFalse({"security_library_id", "security_user_id"} & {
            r["from"] for r in db.execute("PRAGMA foreign_key_list(scan_log)")
        })
        db.execute("INSERT INTO users(id,username,password) VALUES(7,'scan-user','synthetic')")
        db.execute("INSERT INTO libraries(id,name) VALUES(2,'Scan snapshot')")
        db.execute("INSERT INTO scan_log(isbn,result,security_library_id,security_user_id) VALUES('retained','unknown',2,7)")
        db.execute("DELETE FROM users WHERE id=7")
        db.execute("DELETE FROM libraries WHERE id=2")
        db.commit()
        self.assertEqual(tuple(db.execute("SELECT security_library_id,security_user_id FROM scan_log").fetchone()), (2, 7))
        db.execute("ALTER TABLE scan_log DROP COLUMN security_user_id")
        db.commit()
        before = dump(db)
        with self.assertRaises(self.schema.SchemaLineageError):
            self.schema.ensure_scan_history_schema(db)
        self.assertEqual(dump(db), before)

    def test_scan_migration_failure_rolls_back_columns_data_ledger_and_manifest(self):
        class FailScan:
            def __init__(self, db, version):
                self.db, self.version = db, version

            def execute(self, sql, params=()):
                if "INSERT INTO schema_migration_provenance" in sql and params and params[0] == self.version:
                    raise RuntimeError("scan migration interruption")
                return self.db.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.db, name)

        for version in (119, 120, 121):
            with self.subTest(version=version):
                db = self.keep(historical_bridge_before_scan(self.schema))
                db.execute("INSERT INTO items(id,title) VALUES(11,'Bound scan')")
                db.execute("INSERT INTO library_items(item_id,library_id) VALUES(11,1)")
                db.execute("INSERT INTO scan_log(isbn,result,item_id) VALUES('bound','found',11)")
                db.commit()
                before = dump(db)
                with self.assertRaisesRegex(RuntimeError, "scan migration interruption"):
                    self.schema._run_migrations(FailScan(db, version))
                self.assertEqual(dump(db), before)
                self.assertNotIn("security_library_id", self.schema._columns(db, "scan_log"))
                self.assertEqual(db.execute("SELECT manifest_sha256 FROM schema_lineage").fetchone()[0], PRE_SCAN_MANIFEST_HASH)
                self.schema._run_migrations(db)
                self.assertEqual(db.execute("SELECT security_library_id FROM scan_log").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
