from pathlib import Path


def patch_database() -> None:
    db = Path("app/database.py")
    text = db.read_text()
    if '(41, "Add Shelf libraries"' not in text:
        anchor = '         )"""),\n)\n\nMIGRATION_TABLES = """'
        if anchor not in text:
            raise SystemExit("migration tail anchor not found")
        migrations = r'''    (41, "Add Shelf libraries",
     """CREATE TABLE IF NOT EXISTS libraries (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
            description   TEXT,
            is_archived   INTEGER NOT NULL DEFAULT 0 CHECK(is_archived IN (0,1)),
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )"""),
    (42, "Add per-library user memberships",
     """CREATE TABLE IF NOT EXISTS library_memberships (
            library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role          TEXT NOT NULL CHECK(role IN ('viewer','editor')),
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (library_id, user_id)
        )"""),
    (43, "Add one-library-per-item mapping",
     """CREATE TABLE IF NOT EXISTS library_items (
            item_id       INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
            library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )"""),
    (44, "Create default Main Library",
     "INSERT OR IGNORE INTO libraries (id, name, description) "
     "VALUES (1, 'Main Library', 'Default library created during upgrade')"),
    (45, "Assign existing catalogue items to Main Library",
     "INSERT OR IGNORE INTO library_items (item_id, library_id) "
     "SELECT id, 1 FROM items"),
    (46, "Seed Main Library memberships from existing roles",
     """INSERT OR IGNORE INTO library_memberships (library_id, user_id, role)
        SELECT 1, id, role FROM users
         WHERE role IN ('viewer','editor')
           AND NOT EXISTS (
                 SELECT 1 FROM settings
                  WHERE key = 'sudo_fork_037_migration_ledger_v1'
                    AND value LIKE '%\"version\":62%'
           )"""),
    (47, "Index library memberships by user",
     "CREATE INDEX IF NOT EXISTS idx_library_memberships_user "
     "ON library_memberships(user_id)"),
    (48, "Index catalogue items by library",
     "CREATE INDEX IF NOT EXISTS idx_library_items_library "
     "ON library_items(library_id)"),
'''
        text = text.replace(
            anchor,
            '         )"""),\n' + migrations + ')\n\nMIGRATION_TABLES = """',
            1,
        )

    migration_tables = text.split('MIGRATION_TABLES = """', 1)[1]
    if "CREATE TABLE IF NOT EXISTS libraries (" not in migration_tables:
        table_anchor = "CREATE TABLE IF NOT EXISTS game_platforms ("
        if table_anchor not in text:
            raise SystemExit("MIGRATION_TABLES insertion anchor not found")
        tables = '''-- First-class Shelf libraries and per-library permissions. These tables are
-- also created by migrations 41-43 for upgrades; baking them into the central
-- table script keeps fresh databases on the same schema without request-path DDL.
CREATE TABLE IF NOT EXISTS libraries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description   TEXT,
    is_archived   INTEGER NOT NULL DEFAULT 0 CHECK(is_archived IN (0,1)),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS library_memberships (
    library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role          TEXT NOT NULL CHECK(role IN ('viewer','editor')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (library_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_library_memberships_user
    ON library_memberships(user_id);

CREATE TABLE IF NOT EXISTS library_items (
    item_id       INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_library_items_library
    ON library_items(library_id);

'''
        text = text.replace(table_anchor, tables + table_anchor, 1)
    db.write_text(text)


def patch_service() -> None:
    service = Path("app/services/libraries.py")
    original = service.read_text()
    marker = "def list_libraries"
    if marker not in original:
        raise SystemExit("libraries service function anchor not found")
    suffix = original[original.index(marker):]
    suffix = suffix.replace("    ensure_schema(db)\n", "")
    header = '''"""First-class Shelf libraries and per-library catalogue permissions.

Schema ownership lives in :mod:`app.database`; this service is deliberately
query/policy-only so permission checks never execute DDL on request paths.
"""

from __future__ import annotations


LIBRARY_ROLE_LEVELS = {"viewer": 1, "editor": 2, "admin": 3}
DEFAULT_LIBRARY_ID = 1
DEFAULT_LIBRARY_NAME = "Main Library"
LEGACY_FORK_LEDGER_KEY = "sudo_fork_037_migration_ledger_v1"


'''
    service.write_text(header + suffix)


def patch_tests_and_router_init() -> None:
    Path("app/routers/__init__.py").write_text("")
    tests = Path("tests/test_library_permissions_037.py")
    text = tests.read_text()
    if "from app import database\n" not in text:
        text = text.replace(
            "from app.auth import hash_password\n",
            "from app import database\nfrom app.auth import hash_password\n",
            1,
        )
    text = text.replace("libraries._LIBRARY_MIGRATIONS", "database.MIGRATIONS")
    old = '''    assert [version for version, _description, _sql in database.MIGRATIONS] == list(
        range(41, 49)
    )'''
    new = '''    assert [
        version for version, _description, _sql in database.MIGRATIONS
        if 41 <= version <= 48
    ] == list(range(41, 49))'''
    if old not in text:
        raise SystemExit("library migration-version test anchor not found")
    tests.write_text(text.replace(old, new, 1))


patch_database()
patch_service()
patch_tests_and_router_init()
