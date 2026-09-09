from pathlib import Path
import subprocess


def git_show(spec: str) -> str:
    return subprocess.check_output(["git", "show", spec], text=True)


# Final query/policy-only service from the recovered implementation.
service = git_show("c8e3b5817339656cd8284c96e6ddae0f88946405:app/services/libraries.py")
service = service.replace('LEGACY_FORK_LEDGER_KEY = "sudo_fork_037_migration_ledger_v1"\n', '')
Path("app/services/libraries.py").write_text(service)

# Append clean upstream migrations after current migration 32.
db_path = Path("app/database.py")
db = db_path.read_text()
anchor = '''    (32, "Add durable cover-review dismissal",
     "ALTER TABLE items ADD COLUMN cover_review_dismissed INTEGER NOT NULL DEFAULT 0"),
)'''
replacement = '''    (32, "Add durable cover-review dismissal",
     "ALTER TABLE items ADD COLUMN cover_review_dismissed INTEGER NOT NULL DEFAULT 0"),
    (33, "Add Shelf libraries",
     """CREATE TABLE IF NOT EXISTS libraries (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
            description   TEXT,
            is_archived   INTEGER NOT NULL DEFAULT 0 CHECK(is_archived IN (0,1)),
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )"""),
    (34, "Add per-library user memberships",
     """CREATE TABLE IF NOT EXISTS library_memberships (
            library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role          TEXT NOT NULL CHECK(role IN ('viewer','editor')),
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (library_id, user_id)
        )"""),
    (35, "Add one-library-per-item mapping",
     """CREATE TABLE IF NOT EXISTS library_items (
            item_id       INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
            library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )"""),
    (36, "Create default Main Library",
     "INSERT OR IGNORE INTO libraries (id, name, description) "
     "VALUES (1, 'Main Library', 'Default library created during upgrade')"),
    (37, "Assign existing catalogue items to Main Library",
     "INSERT OR IGNORE INTO library_items (item_id, library_id) "
     "SELECT id, 1 FROM items"),
    (38, "Seed Main Library memberships from existing roles",
     """INSERT OR IGNORE INTO library_memberships (library_id, user_id, role)
        SELECT 1, id, role FROM users WHERE role IN ('viewer','editor')"""),
    (39, "Index library memberships by user",
     "CREATE INDEX IF NOT EXISTS idx_library_memberships_user ON library_memberships(user_id)"),
    (40, "Index catalogue items by library",
     "CREATE INDEX IF NOT EXISTS idx_library_items_library ON library_items(library_id)"),
)'''
if anchor not in db:
    raise SystemExit("migration anchor not found")
db = db.replace(anchor, replacement, 1)

fresh_anchor = '''CREATE TABLE IF NOT EXISTS game_platforms (
'''
fresh_tables = '''-- First-class Shelf libraries and per-library permissions. These definitions
-- mirror migrations 33-35 so fresh databases and upgraded databases converge.
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
if fresh_anchor not in db:
    raise SystemExit("fresh-schema anchor not found")
db = db.replace(fresh_anchor, fresh_tables + fresh_anchor, 1)
db_path.write_text(db)

# Adapt the final foundation tests to this upstream migration namespace and
# remove the fork-upgrade-only ledger regression.
test = git_show("c8e3b5817339656cd8284c96e6ddae0f88946405:tests/test_library_permissions_037.py")
test = test.replace("import json\n\n", "")
test = test.replace("for version in (44, 45, 46):", "for version in (36, 37, 38):")
test = test.replace("if 41 <= version <= 48", "if 33 <= version <= 40")
test = test.replace("list(range(41, 49))", "list(range(33, 41))")
test = test.replace("test_library_migrations_use_current_037_namespace", "test_library_migrations_follow_current_upstream_namespace")
start = test.index("\ndef test_legacy_fork_ledger_prevents_revoked_membership_from_being_regranted")
end = test.index("\ndef test_library_role_is_independent_of_legacy_global_viewer_role", start)
test = test[:start] + test[end:]
Path("tests/test_library_permissions.py").write_text(test)
