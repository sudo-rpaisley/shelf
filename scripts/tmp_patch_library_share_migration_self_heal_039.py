from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


# Migration 45 is atomic, so it must not inherit the broad pre-atomic replay
# tolerance. However current MIGRATION_TABLES intentionally creates share_links
# with library_id already present. A database/test fixture can therefore have
# the exact target column while its schema_version row is absent. Recognise
# only this migration + this column as an already-converged schema.
replace_once(
    "app/database.py",
    '''    if "duplicate column name" in msg:\n        return version <= _PRE_ATOMIC_MAX_VERSION\n''',
    '''    if "duplicate column name" in msg:\n        if version <= _PRE_ATOMIC_MAX_VERSION:\n            return True\n        return version == 45 and "duplicate column name: library_id" in msg\n''',
)

p = Path("tests/test_share_links.py")
s = p.read_text().rstrip()
s += r'''


def test_share_library_migration_self_heals_exact_existing_column():
    """Migration 45 may converge with fresh-schema share_links without hiding
    unrelated post-atomic duplicate-column defects."""
    import sqlite3
    from app import database

    assert database._is_benign_migration_error(
        45, sqlite3.OperationalError("duplicate column name: library_id")
    ) is True
    assert database._is_benign_migration_error(
        45, sqlite3.OperationalError("duplicate column name: token")
    ) is False
    assert database._is_benign_migration_error(
        46, sqlite3.OperationalError("duplicate column name: library_id")
    ) is False
'''
p.write_text(s + "\n")
