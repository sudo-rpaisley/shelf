from pathlib import Path

path = Path("app/database.py")
text = path.read_text()

migration_anchor = '''    (48, "Index catalogue items by library",
     "CREATE INDEX IF NOT EXISTS idx_library_items_library "
     "ON library_items(library_id)"),
)'''
replacement = '''    (48, "Index catalogue items by library",
     "CREATE INDEX IF NOT EXISTS idx_library_items_library "
     "ON library_items(library_id)"),
    (49, "Add external user identities",
     """CREATE TABLE IF NOT EXISTS user_identities (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider      TEXT NOT NULL DEFAULT 'oidc',
            issuer        TEXT NOT NULL,
            subject       TEXT NOT NULL,
            email         TEXT,
            last_login_at TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(issuer, subject),
            UNIQUE(user_id, provider, issuer)
        )"""),
    (50, "Index external user identities by user",
     "CREATE INDEX IF NOT EXISTS idx_user_identities_user "
     "ON user_identities(user_id)"),
)'''
if migration_anchor not in text:
    raise SystemExit("migration 48 anchor not found")
text = text.replace(migration_anchor, replacement, 1)

users_anchor = '''CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password       TEXT NOT NULL,
    display_name   TEXT,
    role           TEXT NOT NULL DEFAULT 'viewer' CHECK(role IN ('admin','editor','viewer')),
    token_version  INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
'''
identity_tables = '''
CREATE TABLE IF NOT EXISTS user_identities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL DEFAULT 'oidc',
    issuer        TEXT NOT NULL,
    subject       TEXT NOT NULL,
    email         TEXT,
    last_login_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(issuer, subject),
    UNIQUE(user_id, provider, issuer)
);
CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities(user_id);
'''
if users_anchor not in text:
    raise SystemExit("fresh users-table anchor not found")
if "CREATE TABLE IF NOT EXISTS user_identities" not in text.split("MIGRATION_TABLES =", 1)[1]:
    text = text.replace(users_anchor, users_anchor + identity_tables, 1)

path.write_text(text)
print("OIDC account schema migrations 49-50 applied")
