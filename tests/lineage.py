"""Data-only parent-era fixtures and explicit fork/upstream operation mapping.

Never import or execute either historical application. Captures are literal SQL
and migration metadata, bound to the immutable parent blobs in their manifests.
The application under test is always the integrated runner, not a test runner
that quietly reproduces the desired result.
"""

import json
import re
import sqlite3
from pathlib import Path


_CAPTURES = Path(__file__).parent / "fixtures" / "lineage"
MAIN = json.loads((_CAPTURES / "main_database.json").read_text())
UPSTREAM = json.loads((_CAPTURES / "upstream_database.json").read_text())
MAIN_SCHEMA = MAIN["SCHEMA"]
UPSTREAM_SCHEMA = UPSTREAM["SCHEMA"]
MAIN_MIGRATION_TABLES = MAIN["MIGRATION_TABLES"]
UPSTREAM_MIGRATION_TABLES = UPSTREAM["MIGRATION_TABLES"]
MAIN_MIGRATIONS = tuple(tuple(m) for m in MAIN["MIGRATIONS"])
UPSTREAM_MIGRATIONS = tuple(tuple(m) for m in UPSTREAM["MIGRATIONS"])

# U24 is the exact SQL operation already published as M46. U25-31's physical
# foundation is owned by the historical fork node-domain foundation, not an
# integer alias of the upstream flat-location model. No tests renumber these
# operations as though upstream databases were admitted fork databases.
SUBSUMED_UPSTREAM_OPERATIONS = {
    24: "main43e244f5/46: confirmed legacy book mappings",
    25: "main43e244f5/holdings foundation: node-domain item_copies",
    26: "main43e244f5/holdings_foundation_backfill_v1: primary copies",
    27: "main43e244f5/holdings foundation: location_nodes.parent_id",
    28: "main43e244f5/holdings foundation: location_nodes.name",
    29: "main43e244f5/holdings foundation: explicit legacy_location_id linkage",
    30: "main43e244f5/holdings foundation: node parent index",
    31: "main43e244f5/holdings foundation: item_copies.position_order",
}


def upstream_operation(version):
    """Resolve an operation by immutable identity, never by numeric offset.

    The central registry publishes an exact origin-qualified description. SQL is
    checked too: only the two approved audit foreign-key domain substitutions
    differ from upstream SQL here. A missing, changed or duplicate operation
    fails before a fixture is constructed, rather than silently crediting it.
    """
    from app.database import MIGRATIONS

    original = next(m for m in UPSTREAM_MIGRATIONS if m[0] == version)
    description = (original[1] if version <= 24 else
                   f"Bridge upstream {version}: {original[1]}")
    matches = [m for m in MIGRATIONS if m[1] == description]
    assert len(matches) == 1, (version, original[1], matches)
    result = matches[0]
    if version <= 23:
        assert result[0] == version
    elif version == 24:
        assert result[0] == 46
    else:
        assert result[0] > 66
    expected_sql = original[2]
    if version in (45, 46):
        expected_sql = expected_sql.replace("REFERENCES locations(id)",
                                            "REFERENCES location_nodes(id)")
    def normalize(sql):
        return " ".join(sql.split())
    assert normalize(result[2]) == normalize(expected_sql), (version, result)
    return result


def upstream_version(version):
    return upstream_operation(version)[0]


def _execute_historical(db, sql, bootstrap):
    """Only parent-managed not-yet-created tables may be absent in old SQL."""
    try:
        db.execute(sql)
    except sqlite3.OperationalError as error:
        missing = re.fullmatch(r"no such table: (\w+)", str(error))
        if missing is None or not re.search(
            rf"CREATE TABLE IF NOT EXISTS\s+{re.escape(missing[1])}\s*\(", bootstrap
        ):
            raise


def fork_before_upstream_operation(path, version):
    """A genuine fork66 database before the atomic integration transaction.

    This is NOT an upstream-origin database relabeled to evade D1. Build the
    captured complete fork schema/history. The integrated bridge is atomic, so
    there is no supported partially applied bridge pretending to be "U version
    n-1". All new upstream effects are absent on this fixture, including the
    specifically requested one. It upgrades through the real integrated runner
    with its real provenance, never forged partial bridge history. Today's
    SCHEMA/MIGRATION_TABLES are never used to pre-create the requested effect.
    Original absent-before/positive/negative/no-reseed controls stay in callers.
    """
    assert version >= 32, "common-prefix upgrades use their own parent-era fixtures"
    upstream_operation(version)  # fail on a missing or unreviewed operation
    return captured_fork_database(path)


def captured_fork_database(path):
    """Construct only immutable fork-era state, with no current-app imports."""
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    bootstrap = MAIN_MIGRATION_TABLES + ";\n" + ";\n".join(MAIN["FOUNDATION_STATEMENTS"]) + ";"
    db.executescript(MAIN_SCHEMA)
    for number, description, sql in MAIN_MIGRATIONS:
        _execute_historical(db, sql, bootstrap)
        db.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                   (number, description))
    db.executescript(bootstrap)
    db.commit()
    return db


def captured_common_database(path, up_to, *, series_meta=False, users=False):
    """The admitted shared1–23 prefix, without later fork/upstream bootstrap.

    The original series_meta shape is explicit where a test exercises its
    ALTERs. `users` opts into the captured historical bootstrap (including its
    role CHECK), after the migration loop just as the parent runner did. No
    modern tables or future schema_version rows are manufactured.
    """
    assert 1 <= up_to <= 23
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.executescript(MAIN_SCHEMA)
    if series_meta:
        db.execute("CREATE TABLE series_meta (name TEXT PRIMARY KEY COLLATE NOCASE, "
                   "description TEXT, source TEXT, updated_at TEXT)")
    for number, description, sql in MAIN_MIGRATIONS:
        if number > up_to:
            continue
        _execute_historical(db, sql, MAIN_MIGRATION_TABLES)
        db.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                   (number, description))
    if users:
        ddl = re.search(r"CREATE TABLE IF NOT EXISTS users \(.*?\n\);",
                        MAIN_MIGRATION_TABLES, re.S)
        assert ddl is not None
        db.execute(ddl.group(0))
    db.commit()
    return db


def captured_fork_before_legacy_mapping(path):
    """Historical fork registrants present, published M46 not yet applied.

    Main's independent48+ registrants could precede46. This is not deletion of
    a bridge provenance row to replay a seed: only captured pre-bridge state is
    used, and the original mapping effect is genuinely absent.
    """
    db = captured_fork_database(path)
    db.execute("DROP TABLE legacy_book_mappings")
    db.execute("DELETE FROM schema_version WHERE version = 46")
    db.commit()
    return db


def captured_fork_before_personal_snapshot(path):
    """Captured fork1–46, BEFORE the personal domain48–56 ever existed."""
    return _captured_fork_before_registrant(path, 48)


def captured_fork_before_library_snapshot(path):
    """Captured fork1–46/48–56, BEFORE library registrants57–64."""
    return _captured_fork_before_registrant(path, 57)


def captured_fork_before_series_memberships(path):
    """Captured fork1–39: no item_series table, index or backfill40–42."""
    return _captured_fork_before_registrant(path, 40)


def _captured_statements(script):
    """Split literal SQL at complete statements, not semicolons in comments."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    assert not re.sub(r"--[^\n]*", "", pending).strip(), pending


def _captured_fork_before_registrant(path, first_pending):
    """Build a historical prefix directly; never delete completed history.

    Parent bootstrap bakes in some core CREATEs. At an earlier core cutoff,
    exclude exactly those later published tables and their indexes BEFORE
    executing the captured SQL. Independent captured foundations are retained.
    Private/library/provider registrants are not in the parent bootstrap and
    are executed only when they precede this explicit cutoff.
    """
    assert first_pending in (40, 48, 57)
    pending_tables = {
        match[1] for number, _description, sql in MAIN_MIGRATIONS
        if number >= first_pending
        and (match := re.match(r"CREATE TABLE IF NOT EXISTS (\w+)\b", sql))
    }
    bootstrap = []
    for sql in _captured_statements(MAIN_MIGRATION_TABLES):
        effect = re.sub(r"--[^\n]*", "", sql).strip()
        table = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)\b", effect)
        index = re.match(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS \w+\s+ON (\w+)\b", effect)
        if (table or index) and (table or index)[1] in pending_tables:
            continue
        if effect:
            bootstrap.append(sql)
    bootstrap += [sql + ";" for sql in MAIN["FOUNDATION_STATEMENTS"]]
    bootstrap_sql = "\n".join(bootstrap)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.executescript(MAIN_SCHEMA)
    for number, description, sql in MAIN_MIGRATIONS:
        if number >= first_pending:
            continue
        _execute_historical(db, sql, bootstrap_sql)
        db.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                   (number, description))
    db.executescript(bootstrap_sql)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not pending_tables & tables
    db.commit()
    return db


def captured_upstream_database(path):
    """Original upstream history is negative admission evidence, never upgraded."""
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.executescript(UPSTREAM_SCHEMA)
    for number, description, sql in UPSTREAM_MIGRATIONS:
        _execute_historical(db, sql, UPSTREAM_MIGRATION_TABLES)
        db.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                   (number, description))
    db.executescript(UPSTREAM_MIGRATION_TABLES)
    db.commit()
    return db
