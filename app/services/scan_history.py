"""Scan-history provenance survives item purge without widening visibility."""

from fastapi import HTTPException

from app.services import catalogue_security, libraries
from app.services.item_write import _write_scope


def record_scan(db, isbn, media_type, result, item_id=None, mode="add"):
    """Record a real attempt with its canonical actor and proven library binding.

    A target removed/revoked after the operation is logged only as that actor's
    unanchored attempt. Its previous private library is never guessed or exposed.
    This helper owns no DDL and commits none of the caller's transaction.
    """
    with _write_scope(db):
        actor = catalogue_security.current_actor()
        library_id = None
        if item_id is not None:
            try:
                item = catalogue_security.require_item_role(
                    db, item_id, "viewer", include_deleted=True
                )
            except HTTPException as exc:
                if exc.status_code not in (403, 404):
                    raise
                item = None
            if item is None:
                item_id = None
            else:
                library_id = libraries.item_library_id(db, item_id)
        db.execute(
            "INSERT INTO scan_log (isbn, media_type, result, item_id, mode, "
            "security_library_id, security_user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (isbn, media_type, result, item_id, mode, library_id,
             int(actor["id"]) if actor is not None else None),
        )


def visibility_condition(user, *, scan_alias="sl"):
    """Bound SQL for retained history, including safely attributed orphan rows."""
    if not scan_alias or not scan_alias.replace("_", "").isalnum():
        raise ValueError("Invalid scan alias")
    actor = catalogue_security.current_actor()
    if actor is not None:
        if user is not None and int(user["id"]) != int(actor["id"]):
            raise HTTPException(403, "Forbidden")
        user = actor
    if not user:
        return "1 = 0", []
    if user.get("account_role", user.get("role")) == "admin":
        return "1 = 1", []
    current_scope, current_params = libraries.item_access_condition(
        user, item_alias="physical", minimum_role="viewer"
    )
    return (
        "((EXISTS (SELECT 1 FROM libraries history_library "
        "JOIN library_memberships history_member ON history_member.library_id = history_library.id "
        f"WHERE history_library.id = {scan_alias}.security_library_id "
        "AND history_member.user_id = ? AND history_member.role IN ('viewer', 'editor'))) "
        f"OR ({scan_alias}.security_library_id IS NULL AND {scan_alias}.security_user_id = ?)) "
        f"AND ({scan_alias}.item_id IS NULL OR EXISTS (SELECT 1 FROM items physical "
        f"WHERE physical.id = {scan_alias}.item_id AND ({current_scope})))",
        [int(user["id"]), int(user["id"]), *current_params],
    )
