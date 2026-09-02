from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text()
    if old not in text:
        raise SystemExit(f"patch anchor missing in {path}: {old[:140]!r}")
    target.write_text(text.replace(old, new, 1))


replace_once(
    "app/database.py",
    '''def _is_benign_migration_error(version: int, exc: sqlite3.OperationalError) -> bool:
    """True for the two ways a migration can fail harmlessly and still count
    as applied. Everything else is a defect in the migration SQL and must
    reach the caller instead of being silently recorded.

    Matching on the message alone is not enough: a typo'd table name produces
    the same "no such table" as a table MIGRATION_TABLES has not created yet,
    and a migration that re-adds an existing base column produces the same
    "duplicate column name" as an interrupted replay. Both are bound here to
    the invariant that actually makes them benign.
    """
    msg = str(exc)
    if "duplicate column name" in msg:
        return version <= _PRE_ATOMIC_MAX_VERSION
''',
    '''def _is_benign_migration_error(
    version: int,
    exc: sqlite3.OperationalError,
    *,
    db: sqlite3.Connection | None = None,
    sql: str = "",
) -> bool:
    """True only when a failed migration is already present by construction.

    Pre-atomic migrations retain their historical duplicate-column recovery.
    For newer migrations we do *not* trust the exception text alone: the SQL
    must be a simple ``ALTER TABLE ... ADD COLUMN``, the duplicate named by
    SQLite must be that same column, and PRAGMA must confirm it already exists
    on that exact table. This lets a baked-in MIGRATION_TABLES schema self-heal
    a missing version row without turning arbitrary post-atomic SQL errors into
    recorded successes.
    """
    msg = str(exc)
    if "duplicate column name" in msg:
        if version <= _PRE_ATOMIC_MAX_VERSION:
            return True
        if db is None or not sql:
            return False
        duplicate = re.search(r"duplicate column name:\\s*([A-Za-z_][A-Za-z0-9_]*)", msg, re.I)
        alter = re.match(
            r"\\s*ALTER\\s+TABLE\\s+([A-Za-z_][A-Za-z0-9_]*)"
            r"\\s+ADD\\s+COLUMN\\s+([A-Za-z_][A-Za-z0-9_]*)\\b",
            sql,
            re.I,
        )
        if not duplicate or not alter:
            return False
        table_name, column_name = alter.groups()
        if duplicate.group(1).casefold() != column_name.casefold():
            return False
        # table_name is restricted to an SQL identifier by the regex above,
        # so quoting it here is sufficient and no user-controlled SQL enters
        # this path.
        columns = db.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        return any(row["name"].casefold() == column_name.casefold() for row in columns)
''',
)

replace_once(
    "app/database.py",
    '''            if not _is_benign_migration_error(version, e):
                raise
''',
    '''            if not _is_benign_migration_error(version, e, db=db, sql=sql):
                raise
''',
)
replace_once(
    "app/database.py",
    '''                if not _is_benign_migration_error(version, e):
                    raise
''',
    '''                if not _is_benign_migration_error(version, e, db=db, sql=sql):
                    raise
''',
)

# Pin the post-atomic self-heal invariant directly against the Discogs schema.
path = ROOT / "tests/test_discogs.py"
text = path.read_text()
text = text.replace(
    "from app.database import get_setting\n",
    "from app.database import _run_migrations, get_setting\n",
    1,
)
anchor = '''class TestDiscogsNormalisation:\n'''
if anchor not in text:
    raise SystemExit("test insertion anchor missing")
test = '''class TestDiscogsMigrationRecovery:\n    def test_baked_discogs_column_self_heals_missing_version_row(self, db):\n        # Reproduce a restored/constructed DB where the complete table schema\n        # is present but migration 31's bookkeeping row is missing.\n        db.execute("DELETE FROM schema_version WHERE version = 31")\n        db.commit()\n\n        _run_migrations(db)\n\n        assert db.execute(\n            "SELECT 1 FROM schema_version WHERE version = 31"\n        ).fetchone() is not None\n\n\n'''
path.write_text(text.replace(anchor, test + anchor, 1))

print("Discogs migration self-heal patch applied")
