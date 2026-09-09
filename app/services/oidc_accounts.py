"""Bind validated OIDC identities to Shelf users and library permissions.

Protocol validation stays in :mod:`app.oidc`. This service owns only the local
account boundary: stable issuer+subject identity keys, auto-provisioning and
synchronisation of the default Main Library membership. It deliberately never
grants access to custom libraries.
"""

from __future__ import annotations

import secrets
from typing import Any

from app.auth import hash_password
from app.database import get_db
from app.oidc import OIDCAccessDenied, OIDCConfig, OIDCError, OIDCIdentity
from app.services import libraries


def _unique_username(db, base: str) -> str:
    candidate = (base or "oidc-user")[:64]
    if not db.execute(
        "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (candidate,)
    ).fetchone():
        return candidate

    stem = (base or "oidc-user")[:55]
    for number in range(1, 10000):
        suffix = "-oidc" if number == 1 else f"-oidc{number}"
        candidate = (stem[: 64 - len(suffix)] + suffix)[:64]
        if not db.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (candidate,)
        ).fetchone():
            return candidate
    raise OIDCError("Could not allocate a unique Shelf username")


def _sync_main_library_role(db, user_id: int, role: str) -> None:
    """Mirror an OIDC-managed non-admin role into Main Library only.

    Custom-library memberships remain explicit local policy. Admins bypass
    per-library memberships, so no extra grant is necessary for them.
    """
    if role in ("viewer", "editor"):
        libraries.set_membership(
            db, libraries.DEFAULT_LIBRARY_ID, int(user_id), role
        )


def _fresh_user(db, user_id: int) -> dict[str, Any]:
    row = db.execute(
        "SELECT id, username, display_name, role, token_version "
        "FROM users WHERE id = ?",
        (int(user_id),),
    ).fetchone()
    if not row:
        raise OIDCError("OIDC account no longer exists in Shelf")
    return dict(row)


def provision_or_sync(identity: OIDCIdentity, config: OIDCConfig) -> dict[str, Any]:
    """Resolve one validated OIDC identity to a Shelf account.

    Existing accounts are located *only* by stable issuer+subject. Username and
    email are profile attributes, never identity proof. When role sync is on,
    the mapped global role and Main Library membership move together while all
    other library memberships are preserved.
    """
    with get_db() as db:
        row = db.execute(
            "SELECT u.id, u.username, u.display_name, u.role, u.token_version "
            "FROM user_identities ui "
            "JOIN users u ON u.id = ui.user_id "
            "WHERE ui.issuer = ? AND ui.subject = ?",
            (identity.issuer, identity.subject),
        ).fetchone()

        if not row:
            if not config.auto_provision:
                raise OIDCAccessDenied(
                    "Your OIDC identity is valid but is not provisioned in Shelf"
                )

            username = _unique_username(db, identity.username)
            disabled_local_password = hash_password(secrets.token_urlsafe(48))
            cursor = db.execute(
                "INSERT INTO users (username, password, display_name, role) "
                "VALUES (?, ?, ?, ?)",
                (
                    username,
                    disabled_local_password,
                    identity.display_name,
                    identity.role,
                ),
            )
            user_id = int(cursor.lastrowid)
            db.execute(
                "INSERT INTO user_identities "
                "(user_id, provider, issuer, subject, email, last_login_at) "
                "VALUES (?, 'oidc', ?, ?, ?, datetime('now'))",
                (user_id, identity.issuer, identity.subject, identity.email),
            )
            _sync_main_library_role(db, user_id, identity.role)
            return _fresh_user(db, user_id)

        user_id = int(row["id"])
        role_changed = bool(config.sync_roles and row["role"] != identity.role)
        if role_changed and row["role"] == "admin" and identity.role != "admin":
            admin_count = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE role = 'admin'"
                ).fetchone()["c"]
            )
            if admin_count <= 1:
                raise OIDCError(
                    "OIDC role synchronisation would demote the last Shelf "
                    "administrator; create or restore a local break-glass admin first"
                )

        if config.sync_roles:
            db.execute(
                "UPDATE users SET role = ?, display_name = ?, "
                "token_version = token_version + ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (
                    identity.role,
                    identity.display_name,
                    1 if role_changed else 0,
                    user_id,
                ),
            )
            _sync_main_library_role(db, user_id, identity.role)
        else:
            db.execute(
                "UPDATE users SET display_name = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (identity.display_name, user_id),
            )

        db.execute(
            "UPDATE user_identities SET email = ?, last_login_at = datetime('now'), "
            "updated_at = datetime('now') WHERE issuer = ? AND subject = ?",
            (identity.email, identity.issuer, identity.subject),
        )
        return _fresh_user(db, user_id)


def managed_user_ids() -> set[int]:
    """Users whose global role is currently managed by OIDC group mapping."""
    with get_db() as db:
        return {
            int(row["user_id"])
            for row in db.execute(
                "SELECT DISTINCT user_id FROM user_identities"
            ).fetchall()
        }


def is_role_managed(user_id: int, config: OIDCConfig) -> bool:
    if not config.sync_roles:
        return False
    with get_db() as db:
        return bool(
            db.execute(
                "SELECT 1 FROM user_identities WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
        )
