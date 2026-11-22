"""Request-local catalogue visibility and library authority.

Only canonical, server-resolved principals belong here. An absent actor means
trusted service work, not an alternate HTTP authentication mechanism: request
dispatch MUST install its principal and routers MUST require authentication.
Connections are short-lived; their TEMP views never enter backups.
"""

from contextvars import ContextVar, Token

from fastapi import HTTPException, Request

from app.services import libraries


_actor: ContextVar = ContextVar("catalogue_actor", default=None)
_EXPIRED = object()


def set_actor(user: dict) -> Token:
    if not user or not user.get("id"):
        raise ValueError("A resolved authenticated principal is required")
    # The mutable holder is invalidated on reset as well as removed from this
    # context: a copied task must not retain authority after request completion
    # or silently turn into an unscoped trusted service operation.
    return _actor.set({"user": dict(user)})


def reset_actor(token: Token) -> None:
    holder = _actor.get()
    if holder is not None:
        holder["user"] = _EXPIRED
    _actor.reset(token)


def current_actor() -> dict | None:
    holder = _actor.get()
    user = holder["user"] if holder else None
    if user is _EXPIRED:
        raise RuntimeError("Catalogue request scope has ended")
    return user


def _principal(user=None):
    actor = current_actor()
    # A service cannot swap a request's principal for a more privileged user.
    if actor is not None:
        if user is not None and int(user["id"]) != int(actor["id"]):
            raise HTTPException(403, "Forbidden")
        return actor
    return user


def require_item_role(db, item_id, role, user=None, include_deleted=False):
    """Authorize on the caller's connection, inside its write lock.

    Hidden/unmapped/missing items all answer 404; a visible but read-only item
    answers 403. Service calls without a principal remain legitimate. Returning
    the physical row lets a locked caller retain its native liveness handling.
    """
    if role not in libraries.LIBRARY_ROLE_LEVELS:
        raise ValueError("Unknown library role")
    principal = _principal(user)
    # FastAPI's Python integer is not SQLite's signed 64-bit binding. Treat an
    # unrepresentable parent exactly like any other missing ID, before SQL.
    oversized = isinstance(item_id, int) and not -(2**63) <= item_id < 2**63
    row = None if oversized else db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if principal is None:
        return row
    if (row is None or (not include_deleted and row["deleted_at"] is not None)
            or not libraries.has_item_role(db, principal, item_id, "viewer")):
        raise HTTPException(404, "Item not found")
    if not libraries.has_item_role(db, principal, item_id, role):
        raise HTTPException(403, "Forbidden")
    return row


def require_library_role(db, library_id, role, user=None) -> None:
    principal = _principal(user)
    if isinstance(library_id, int) and not -(2**63) <= library_id < 2**63:
        raise HTTPException(404, "Library not found")
    if principal is None:
        if not db.execute("SELECT 1 FROM libraries WHERE id = ?", (library_id,)).fetchone():
            raise LookupError("Library not found")
        return
    if not libraries.has_library_role(db, principal, library_id, "viewer"):
        raise HTTPException(404, "Library not found")
    if not libraries.has_library_role(db, principal, library_id, role):
        raise HTTPException(403, "Forbidden")


def require_catalogue_role(role):
    """Authentication plus *some* library authority, never item authorization.

    Route owners still check every target inside BEGIN IMMEDIATE. Global-admin
    operations keep auth.require_role('admin'); library editors are not admins.
    """
    if role not in libraries.LIBRARY_ROLE_LEVELS:
        raise ValueError("Unknown library role")

    async def dependency(request: Request):
        from app.auth import _raise_auth_required, _raise_insufficient_role
        from app.database import get_db

        user = getattr(request.state, "user", None)
        if not user:
            _raise_auth_required(request)
        if role == "admin":
            allowed = user.get("role") == "admin" and not user.get("api_token_id")
        else:
            with get_db() as db:
                allowed = any(
                    libraries.has_library_role(db, user, library_id, role)
                    for library_id in libraries.accessible_library_ids(db, user)
                )
        if not allowed:
            _raise_insufficient_role(request)
        return user

    return dependency


def access_condition(*, item_alias="i", role="viewer", user=None):
    """Bound predicate for deliberate physical reads (Trash/collisions)."""
    principal = _principal(user)
    if principal is None:
        return "1 = 1", []
    return libraries.item_access_condition(principal, item_alias=item_alias, minimum_role=role)


def item_projection(db, *, item_alias="i"):
    """Projection/join/parameters for deliberate physical reads, e.g. Trash."""
    if not item_alias or not item_alias.replace("_", "").isalnum():
        raise ValueError("Invalid SQL item alias")
    user = current_actor()
    if user is None:
        return f"{item_alias}.*", "", []
    columns = [r[1] for r in db.execute("PRAGMA main.table_info(items)")]
    private = {"reading_status", "date_started", "date_finished"}
    projection = ", ".join(
        f's."{name}" AS "{name}"' if name in private else f'{item_alias}."{name}"'
        for name in columns
    )
    return (
        projection,
        f" LEFT JOIN user_item_state s ON s.item_id = {item_alias}.id AND s.user_id = ? ",
        [int(user["id"])],
    )


def install_catalogue_views(db) -> None:
    """Intersect upstream live views with visibility and private reading state.

    No DDL/schema repair, seeding, persistent view or trigger occurs here.
    Missing initialized security data is a hard refusal, never a fallback to
    unscoped HTTP reads. With no HTTP actor, native service semantics apply.
    """
    user = current_actor()
    if user is None:
        db.execute("DROP VIEW IF EXISTS temp.copies_live")
        db.execute("DROP VIEW IF EXISTS temp.items_live")
        db.execute("CREATE TEMP VIEW items_live AS SELECT * FROM main.items WHERE deleted_at IS NULL")
        db.execute(
            "CREATE TEMP VIEW copies_live AS SELECT c.* FROM main.item_copies c "
            "JOIN items_live i ON i.id = c.item_id WHERE c.deleted_at IS NULL"
        )
        return

    # Poison the broad base views BEFORE validating security structure. Even a
    # caller which catches initialization failure must not regain broad reads.
    db.execute("DROP VIEW IF EXISTS temp.copies_live")
    db.execute("DROP VIEW IF EXISTS temp.items_live")
    db.execute("CREATE TEMP VIEW items_live AS SELECT * FROM main.items WHERE 0")
    db.execute(
        "CREATE TEMP VIEW copies_live AS SELECT c.* FROM main.item_copies c "
        "JOIN items_live i ON i.id = c.item_id WHERE 0"
    )
    db.execute("SELECT item_id, library_id FROM library_items LIMIT 0")
    db.execute("SELECT id FROM libraries LIMIT 0")
    db.execute("SELECT library_id, user_id, role FROM library_memberships LIMIT 0")
    db.execute(
        "SELECT user_id, item_id, reading_status, date_started, date_finished "
        "FROM user_item_state LIMIT 0"
    )
    columns = [r[1] for r in db.execute("PRAGMA main.table_info(items)")]
    if not columns or "deleted_at" not in columns:
        raise RuntimeError("Catalogue security requires an initialized items schema")
    condition, params = libraries.item_access_condition(user, item_alias="i")
    # SQLite prohibits parameters in view definitions. Only validated integer
    # principal ids are interpolated; no username/request text becomes SQL.
    if params:
        condition = condition.replace("?", str(int(user["id"])))
    private = {"reading_status", "date_started", "date_finished"}
    projection = ", ".join(
        f's."{name}" AS "{name}"' if name in private else f'i."{name}"'
        for name in columns
    )
    user_id = int(user["id"])
    db.execute("DROP VIEW IF EXISTS temp.copies_live")
    db.execute("DROP VIEW IF EXISTS temp.items_live")
    db.execute(
        f"CREATE TEMP VIEW items_live AS SELECT {projection} FROM main.items i "
        f"LEFT JOIN main.user_item_state s ON s.item_id = i.id AND s.user_id = {user_id} "
        f"WHERE i.deleted_at IS NULL AND ({condition})"
    )
    db.execute(
        "CREATE TEMP VIEW copies_live AS SELECT c.* FROM main.item_copies c "
        "JOIN items_live i ON i.id = c.item_id WHERE c.deleted_at IS NULL"
    )
