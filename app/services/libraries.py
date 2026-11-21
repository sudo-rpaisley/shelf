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

from app.database import ensure_library_schema as ensure_schema


def __getattr__(name):
    if name == "_LIBRARY_MIGRATIONS":
        # Read-only legacy introspection API. SQL/history has exactly one real
        # owner; importing this service never mutates the migration registry.
        from app.database import MIGRATIONS

        return tuple(m for m in MIGRATIONS if 57 <= m[0] <= 64)
    raise AttributeError(name)


def _account_admin(user: dict) -> bool:
    return user.get("account_role", user.get("role")) == "admin"


def _cap_allows(user: dict, role: str) -> bool:
    # Bearer tokens retain their account's read visibility only. Neither a
    # library editor membership nor an admin account lifts the viewer cap.
    return role == "viewer" or not user.get("api_token_id")


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
    if _account_admin(user):
        if not db.execute("SELECT 1 FROM libraries WHERE id = ?", (library_id,)).fetchone():
            return None
        if user.get("api_token_id"):
            return "viewer"
        return "admin"
    row = db.execute(
        "SELECT role FROM library_memberships WHERE library_id = ? AND user_id = ?",
        (library_id, int(user["id"])),
    ).fetchone()
    if row is None:
        return None
    return "viewer" if user.get("api_token_id") else row["role"]


def has_library_role(db, user: dict, library_id: int, minimum_role: str = "viewer") -> bool:
    if minimum_role not in LIBRARY_ROLE_LEVELS:
        raise ValueError("Unknown library role")
    if not _cap_allows(user, minimum_role):
        return False
    effective = membership_role(db, user, library_id)
    return LIBRARY_ROLE_LEVELS.get(effective, 0) >= LIBRARY_ROLE_LEVELS[minimum_role]


def accessible_library_ids(db, user: dict, *, include_archived: bool = False) -> list[int]:
    """Return libraries visible to a user in stable name order."""
    ensure_schema(db)
    archived_clause = "" if include_archived else "AND l.is_archived = 0"
    if _account_admin(user):
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


def assign_item(db, item_id: int, library_id: int, *, new_item: bool = False) -> None:
    """Assign or move one catalogue item to exactly one Shelf library."""
    ensure_schema(db)
    if not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
        raise LookupError("Item not found")
    if not db.execute("SELECT 1 FROM libraries WHERE id = ?", (library_id,)).fetchone():
        raise LookupError("Library not found")
    from app.services.catalogue_security import current_actor, require_item_role, require_library_role

    if current_actor() is not None:
        existing = item_library_id(db, item_id)
        # New items are not mapped until this call; the insert funnel has
        # already authorized the destination. A move checks BOTH boundaries.
        if existing is not None or not new_item:
            require_item_role(db, item_id, "editor", include_deleted=True)
        require_library_role(db, library_id, "editor")
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
    """Check item access; a missing security mapping always denies."""
    library_id = item_library_id(db, item_id)
    if library_id is None:
        return False
    return has_library_role(db, user, library_id, minimum_role)


def require_recovery_admin(db, user: dict) -> dict:
    """Explicit recovery boundary; never extend ordinary orphan visibility.

    A copied role claim, deleted account or bearer viewer capability is not a
    native administrator. Retain the original principal/caps and validate it
    against the current account on the caller's connection.
    """
    from fastapi import HTTPException
    from app.services.catalogue_security import current_actor

    actor = current_actor()
    if (not user or user.get("api_token_id") or user.get("role") != "admin"
            or user.get("account_role", "admin") != "admin"
            or (actor is not None and (actor.get("api_token_id")
                or actor.get("role") != "admin" or actor["id"] != user["id"]))):
        raise HTTPException(403, "Forbidden")
    account = db.execute(
        "SELECT username, role, token_version FROM users WHERE id = ?", (user["id"],)
    ).fetchone()
    if (account is None or account["role"] != "admin"
            or ("username" in user and user["username"].casefold() != account["username"].casefold())
            or ("token_version" in user and user["token_version"] != account["token_version"])
            or (actor is not None and "token_version" in actor
                and actor["token_version"] != account["token_version"])):
        raise HTTPException(403, "Forbidden")
    return user


def can_recover_item(db, user: dict, item_id: int) -> bool:
    """Native-admin capability probe for an existing, genuinely unmapped row."""
    from fastapi import HTTPException

    try:
        require_recovery_admin(db, user)
    except HTTPException:
        return False
    if isinstance(item_id, int) and not -(2**63) <= item_id < 2**63:
        return False
    return db.execute(
        "SELECT 1 FROM items i WHERE i.id = ? "
        "AND NOT EXISTS (SELECT 1 FROM library_items li WHERE li.item_id = i.id)",
        (item_id,),
    ).fetchone() is not None


def recovery_items(db, user: dict) -> list[dict]:
    """Deliberate physical read for the authenticated admin recovery screen."""
    require_recovery_admin(db, user)
    return [dict(row) for row in db.execute(
        "SELECT i.id, i.title, i.media_type, i.deleted_at FROM items i "
        "WHERE NOT EXISTS (SELECT 1 FROM library_items li WHERE li.item_id = i.id) "
        "ORDER BY i.title COLLATE NOCASE, i.id"
    ).fetchall()]


def recover_item(db, user: dict, item_id: int, library_id: int) -> None:
    """Map an orphan only; retain item, Trash, copy and private-history identity."""
    from fastapi import HTTPException
    from app.services.item_write import _write_scope

    with _write_scope(db):
        require_recovery_admin(db, user)
        if isinstance(item_id, int) and not -(2**63) <= item_id < 2**63:
            raise HTTPException(404, "Item not found")
        if isinstance(library_id, int) and not -(2**63) <= library_id < 2**63:
            raise HTTPException(404, "Library not found")
        if not db.execute("SELECT i.id FROM items i WHERE i.id = ?", (item_id,)).fetchone():
            raise HTTPException(404, "Item not found")
        if item_library_id(db, item_id) is not None:
            raise HTTPException(409, "Item already belongs to a library")
        if not db.execute(
            "SELECT 1 FROM libraries WHERE id = ? AND is_archived = 0", (library_id,)
        ).fetchone():
            raise HTTPException(404, "Library not found")
        # assign_item deliberately denies unmapped HTTP targets. This is its
        # single native-admin recovery exception, not a more permissive move.
        db.execute("INSERT INTO library_items (item_id, library_id) VALUES (?, ?)",
                   (item_id, library_id))


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
    if not _cap_allows(user, minimum_role):
        return "1 = 0", []
    if _account_admin(user):
        return (
            "EXISTS (SELECT 1 FROM library_items li JOIN libraries l ON l.id = li.library_id "
            f"WHERE li.item_id = {item_alias}.id)", []
        )

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
        "JOIN libraries l ON l.id = li.library_id "
        "JOIN library_memberships lm ON lm.library_id = li.library_id "
        f"WHERE li.item_id = {item_alias}.id AND lm.user_id = ? "
        f"AND lm.role IN {membership_roles}"
        ")",
        [int(user["id"])],
    )


def accessible_item_ids(
    db,
    user: dict,
    item_ids,
    *,
    minimum_role: str = "viewer",
) -> list[int]:
    """Filter an arbitrary set of item IDs through the library boundary.

    This is used for secondary projections such as related media: checking the
    anchor item is not enough if the page then renders titles/covers from other
    libraries. The returned order follows the caller's input order.
    """
    ordered = list(dict.fromkeys(int(item_id) for item_id in item_ids))
    if not ordered:
        return []
    condition, access_params = item_access_condition(
        user,
        item_alias="i",
        minimum_role=minimum_role,
    )
    placeholders = ",".join("?" for _ in ordered)
    rows = db.execute(
        f"SELECT i.id FROM items_live i WHERE i.id IN ({placeholders}) AND ({condition})",
        ordered + access_params,
    ).fetchall()
    allowed = {int(row["id"]) for row in rows}
    return [item_id for item_id in ordered if item_id in allowed]


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
