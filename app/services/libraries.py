"""First-class Shelf libraries and per-library catalogue permissions.

A Shelf library is a logical security boundary. It is not a Collection and it
is not an external provider's native library identifier.

The first rollout keeps the existing global ``users.role`` column intact for
compatibility. Global admins bypass library checks; for every other account the
membership role is authoritative and may differ from library to library.
"""

from __future__ import annotations


LIBRARY_ROLE_LEVELS = {"viewer": 1, "editor": 2, "admin": 3}
DEFAULT_LIBRARY_ID = 1
DEFAULT_LIBRARY_NAME = "Main Library"

_CREATE_LIBRARIES = """CREATE TABLE IF NOT EXISTS libraries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description   TEXT,
    is_archived   INTEGER NOT NULL DEFAULT 0 CHECK(is_archived IN (0,1)),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
)"""

_CREATE_LIBRARY_MEMBERSHIPS = """CREATE TABLE IF NOT EXISTS library_memberships (
    library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role          TEXT NOT NULL CHECK(role IN ('viewer','editor')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (library_id, user_id)
)"""

# A one-to-one mapping table is intentional for the first implementation.
# ``item_id`` is the primary key, so an item can belong to exactly one Shelf
# library. This gives us real foreign keys without a risky rebuild of Shelf's
# large historical ``items`` table merely to add a NOT NULL library_id column.
_CREATE_LIBRARY_ITEMS = """CREATE TABLE IF NOT EXISTS library_items (
    item_id       INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    library_id    INTEGER NOT NULL REFERENCES libraries(id) ON DELETE RESTRICT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
)"""

_SCHEMA_STATEMENTS = (
    _CREATE_LIBRARIES,
    _CREATE_LIBRARY_MEMBERSHIPS,
    _CREATE_LIBRARY_ITEMS,
    "CREATE INDEX IF NOT EXISTS idx_library_memberships_user ON library_memberships(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_library_items_library ON library_items(library_id)",
)

_LIBRARY_MIGRATIONS = (
    (57, "Add Shelf libraries", _CREATE_LIBRARIES),
    (58, "Add per-library user memberships", _CREATE_LIBRARY_MEMBERSHIPS),
    (59, "Add one-library-per-item mapping", _CREATE_LIBRARY_ITEMS),
    (
        60,
        "Create default Main Library",
        "INSERT OR IGNORE INTO libraries (id, name, description) "
        "VALUES (1, 'Main Library', 'Default library created during upgrade')",
    ),
    (
        61,
        "Assign existing catalogue items to Main Library",
        "INSERT OR IGNORE INTO library_items (item_id, library_id) "
        "SELECT id, 1 FROM items",
    ),
    (
        62,
        "Seed Main Library memberships from existing roles",
        "INSERT OR IGNORE INTO library_memberships (library_id, user_id, role) "
        "SELECT 1, id, role FROM users WHERE role IN ('viewer','editor')",
    ),
    (
        63,
        "Index library memberships by user",
        "CREATE INDEX IF NOT EXISTS idx_library_memberships_user "
        "ON library_memberships(user_id)",
    ),
    (
        64,
        "Index catalogue items by library",
        "CREATE INDEX IF NOT EXISTS idx_library_items_library "
        "ON library_items(library_id)",
    ),
)


def _register_migrations() -> None:
    """Register library migrations with Shelf's central atomic runner."""
    from app import database

    existing = {version for version, _description, _sql in database.MIGRATIONS}
    pending = tuple(m for m in _LIBRARY_MIGRATIONS if m[0] not in existing)
    if pending:
        database.MIGRATIONS = tuple(database.MIGRATIONS) + pending


_register_migrations()


def ensure_schema(db) -> None:
    """Create the library tables/indexes without granting any new access.

    Upgrade data backfills deliberately live in migrations 60-62. Re-running
    this helper must never silently restore a membership an administrator has
    intentionally removed.
    """
    for statement in _SCHEMA_STATEMENTS:
        db.execute(statement)


def list_libraries(db, *, include_archived: bool = False) -> list[dict]:
    ensure_schema(db)
    where = "" if include_archived else "WHERE is_archived = 0"
    rows = db.execute(
        f"SELECT id, name, description, is_archived, created_at, updated_at "
        f"FROM libraries {where} ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return [dict(row) for row in rows]


def create_library(db, name: str, description: str | None = None) -> dict:
    ensure_schema(db)
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Library name is required")
    if len(clean_name) > 200:
        raise ValueError("Library name is too long")
    clean_description = str(description).strip() if description is not None else None
    if clean_description == "":
        clean_description = None
    if clean_description and len(clean_description) > 2000:
        raise ValueError("Library description is too long")

    cursor = db.execute(
        "INSERT INTO libraries (name, description) VALUES (?, ?)",
        (clean_name, clean_description),
    )
    row = db.execute(
        "SELECT id, name, description, is_archived, created_at, updated_at "
        "FROM libraries WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    return dict(row)


def set_membership(db, library_id: int, user_id: int, role: str) -> dict:
    """Grant or change a non-admin library membership."""
    ensure_schema(db)
    if role not in ("viewer", "editor"):
        raise ValueError("Library role must be viewer or editor")
    if not db.execute("SELECT 1 FROM libraries WHERE id = ?", (library_id,)).fetchone():
        raise LookupError("Library not found")
    if not db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
        raise LookupError("User not found")

    db.execute(
        """INSERT INTO library_memberships (library_id, user_id, role)
           VALUES (?, ?, ?)
           ON CONFLICT(library_id, user_id) DO UPDATE SET
               role = excluded.role,
               updated_at = datetime('now')""",
        (library_id, user_id, role),
    )
    row = db.execute(
        "SELECT library_id, user_id, role, created_at, updated_at "
        "FROM library_memberships WHERE library_id = ? AND user_id = ?",
        (library_id, user_id),
    ).fetchone()
    return dict(row)


def remove_membership(db, library_id: int, user_id: int) -> None:
    ensure_schema(db)
    db.execute(
        "DELETE FROM library_memberships WHERE library_id = ? AND user_id = ?",
        (library_id, user_id),
    )


def membership_role(db, user: dict, library_id: int) -> str | None:
    """Return the effective role for one library.

    ``admin`` is the only global catalogue privilege. Non-admin global roles
    are intentionally ignored here once memberships exist; they are migration
    input, not a permanent permission ceiling.
    """
    ensure_schema(db)
    if user.get("role") == "admin":
        return "admin"
    row = db.execute(
        "SELECT role FROM library_memberships WHERE library_id = ? AND user_id = ?",
        (library_id, int(user["id"])),
    ).fetchone()
    return row["role"] if row else None


def has_library_role(db, user: dict, library_id: int, minimum_role: str = "viewer") -> bool:
    if minimum_role not in LIBRARY_ROLE_LEVELS:
        raise ValueError("Unknown library role")
    effective = membership_role(db, user, library_id)
    return LIBRARY_ROLE_LEVELS.get(effective, 0) >= LIBRARY_ROLE_LEVELS[minimum_role]


def accessible_library_ids(db, user: dict, *, include_archived: bool = False) -> list[int]:
    """Return libraries visible to a user in stable name order."""
    ensure_schema(db)
    archived_clause = "" if include_archived else "AND l.is_archived = 0"
    if user.get("role") == "admin":
        rows = db.execute(
            "SELECT l.id FROM libraries l WHERE 1=1 "
            f"{archived_clause} ORDER BY l.name COLLATE NOCASE"
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT l.id FROM libraries l "
            "JOIN library_memberships lm ON lm.library_id = l.id "
            "WHERE lm.user_id = ? "
            f"{archived_clause} ORDER BY l.name COLLATE NOCASE",
            (int(user["id"]),),
        ).fetchall()
    return [int(row["id"]) for row in rows]


def assign_item(db, item_id: int, library_id: int) -> None:
    """Assign or move one catalogue item to exactly one Shelf library."""
    ensure_schema(db)
    if not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
        raise LookupError("Item not found")
    if not db.execute("SELECT 1 FROM libraries WHERE id = ?", (library_id,)).fetchone():
        raise LookupError("Library not found")
    db.execute(
        """INSERT INTO library_items (item_id, library_id) VALUES (?, ?)
           ON CONFLICT(item_id) DO UPDATE SET library_id = excluded.library_id""",
        (item_id, library_id),
    )


def item_library_id(db, item_id: int) -> int | None:
    ensure_schema(db)
    row = db.execute(
        "SELECT library_id FROM library_items WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    return int(row["library_id"]) if row else None


def has_item_role(db, user: dict, item_id: int, minimum_role: str = "viewer") -> bool:
    """Check item access, denying unmapped items to non-admins by default."""
    if user.get("role") == "admin":
        return bool(db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone())
    library_id = item_library_id(db, item_id)
    if library_id is None:
        return False
    return has_library_role(db, user, library_id, minimum_role)


def item_access_condition(
    user: dict,
    *,
    item_alias: str = "i",
    minimum_role: str = "viewer",
) -> tuple[str, list]:
    """Return a bound SQL predicate for the user's accessible item set.

    The alias is supplied only by Shelf code, never request data. Keeping the
    predicate in one service makes Browse, search, Stats and grouped projections
    agree on the same deny-by-default rule.
    """
    if minimum_role not in LIBRARY_ROLE_LEVELS:
        raise ValueError("Unknown library role")
    if not item_alias or not item_alias.replace("_", "").isalnum():
        raise ValueError("Invalid SQL item alias")
    if user.get("role") == "admin":
        return "1 = 1", []

    if minimum_role == "viewer":
        membership_roles = "('viewer','editor')"
    elif minimum_role == "editor":
        membership_roles = "('editor')"
    else:
        # Non-admin users can never satisfy a global-admin requirement.
        return "1 = 0", []

    return (
        "EXISTS ("
        "SELECT 1 FROM library_items li "
        "JOIN library_memberships lm ON lm.library_id = li.library_id "
        f"WHERE li.item_id = {item_alias}.id AND lm.user_id = ? "
        f"AND lm.role IN {membership_roles}"
        ")",
        [int(user["id"])],
    )


def scope_where(
    where: str,
    params: list,
    user: dict,
    *,
    item_alias: str = "i",
    minimum_role: str = "viewer",
) -> tuple[str, list]:
    """AND a library-access predicate onto an existing optional WHERE clause."""
    condition, access_params = item_access_condition(
        user,
        item_alias=item_alias,
        minimum_role=minimum_role,
    )
    joiner = " AND " if where else "WHERE "
    return f"{where}{joiner}{condition}", list(params) + access_params
