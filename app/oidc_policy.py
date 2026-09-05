"""Local-login policy for installations using OIDC.

Shelf never has a true "no local recovery" mode. Administrators may leave
normal local login enabled or restrict it to one explicitly selected local
administrator. If the stored recovery policy becomes invalid, Shelf fails
open to normal local login so a configuration mistake cannot brick access.
"""

from dataclasses import dataclass
import logging

from app.database import get_db, get_setting

logger = logging.getLogger(__name__)

LOCAL_LOGIN_ENABLED = "enabled"
LOCAL_LOGIN_RECOVERY_ONLY = "recovery_only"
_VALID_MODES = {LOCAL_LOGIN_ENABLED, LOCAL_LOGIN_RECOVERY_ONLY}


class OIDCPolicyError(ValueError):
    """An administrator-submitted OIDC login-policy value is unsafe."""


@dataclass(frozen=True)
class LocalLoginPolicy:
    mode: str
    break_glass_user_id: int | None = None
    break_glass_username: str | None = None

    @property
    def recovery_only(self) -> bool:
        return self.mode == LOCAL_LOGIN_RECOVERY_ONLY


def _enabled_policy() -> LocalLoginPolicy:
    return LocalLoginPolicy(mode=LOCAL_LOGIN_ENABLED)


def get_local_login_policy() -> LocalLoginPolicy:
    """Read and validate the stored local-login policy.

    An invalid recovery configuration deliberately falls back to normal local
    login. Availability wins here: an administrator can repair the setting
    after signing in instead of being permanently locked out.
    """
    with get_db() as db:
        mode = (get_setting(db, "oidc_local_login_mode") or LOCAL_LOGIN_ENABLED).strip()
        if mode not in _VALID_MODES or mode == LOCAL_LOGIN_ENABLED:
            return _enabled_policy()

        raw_id = (get_setting(db, "oidc_break_glass_user_id") or "").strip()
        try:
            user_id = int(raw_id)
        except (TypeError, ValueError):
            logger.error("OIDC recovery-only login policy has no valid break-glass user; enabling local login")
            return _enabled_policy()

        user = db.execute(
            "SELECT id, username, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        has_external_identity = bool(
            db.execute(
                "SELECT 1 FROM user_identities WHERE user_id = ? LIMIT 1", (user_id,)
            ).fetchone()
        )
        if not user or user["role"] != "admin" or has_external_identity:
            logger.error("OIDC break-glass account is no longer a local administrator; enabling local login")
            return _enabled_policy()

        return LocalLoginPolicy(
            mode=LOCAL_LOGIN_RECOVERY_ONLY,
            break_glass_user_id=user["id"],
            break_glass_username=user["username"],
        )


def save_local_login_policy(mode: str, break_glass_username: str = "") -> LocalLoginPolicy:
    """Validate and save the local-login policy selected by an administrator."""
    requested = (mode or LOCAL_LOGIN_ENABLED).strip()
    if requested not in _VALID_MODES:
        raise OIDCPolicyError("Unknown local login mode")

    if requested == LOCAL_LOGIN_ENABLED:
        with get_db() as db:
            db.execute(
                "INSERT INTO settings (key, value) VALUES ('oidc_local_login_mode', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (LOCAL_LOGIN_ENABLED,),
            )
            db.execute("DELETE FROM settings WHERE key = 'oidc_break_glass_user_id'")
        return _enabled_policy()

    # Do not hide normal local login until OIDC itself is configured and
    # enabled. The selected recovery administrator still protects against an
    # IdP outage after that point.
    from app.oidc import get_oidc_config

    oidc = get_oidc_config()
    if not oidc.enabled or not oidc.configured:
        raise OIDCPolicyError("Enable and configure OIDC before restricting local login")

    username = break_glass_username.strip()
    if not username:
        raise OIDCPolicyError("Choose a local administrator for break-glass recovery")

    with get_db() as db:
        user = db.execute(
            "SELECT id, username, role FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if not user:
            raise OIDCPolicyError(f"Shelf user '{username}' was not found")
        if user["role"] != "admin":
            raise OIDCPolicyError("The break-glass account must be a Shelf administrator")
        if db.execute(
            "SELECT 1 FROM user_identities WHERE user_id = ? LIMIT 1", (user["id"],)
        ).fetchone():
            raise OIDCPolicyError("The break-glass account must remain a local account, not an OIDC identity")

        for key, value in (
            ("oidc_local_login_mode", LOCAL_LOGIN_RECOVERY_ONLY),
            ("oidc_break_glass_user_id", str(user["id"])),
        ):
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    return LocalLoginPolicy(
        mode=LOCAL_LOGIN_RECOVERY_ONLY,
        break_glass_user_id=user["id"],
        break_glass_username=user["username"],
    )


def local_password_login_allowed(user_id: int) -> bool:
    """Return whether this user may authenticate through Shelf's password form."""
    policy = get_local_login_policy()
    if not policy.recovery_only:
        return True
    return user_id == policy.break_glass_user_id
