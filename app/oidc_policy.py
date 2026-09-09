"""Local authentication and fixed-session policy for OIDC installations.

Shelf deliberately never has a true "no local recovery" mode. Operators may
leave normal password login enabled or restrict it to one explicitly selected
local administrator. Invalid recovery configuration fails open to normal local
login so a bad setting cannot brick the installation.
"""

from dataclasses import dataclass
import logging

from app.database import get_db, get_setting

logger = logging.getLogger(__name__)

LOCAL_LOGIN_ENABLED = "enabled"
LOCAL_LOGIN_RECOVERY_ONLY = "recovery_only"
_VALID_MODES = {LOCAL_LOGIN_ENABLED, LOCAL_LOGIN_RECOVERY_ONLY}

DEFAULT_OIDC_SESSION_HOURS = 24
MIN_OIDC_SESSION_HOURS = 1
MAX_OIDC_SESSION_HOURS = 168


class OIDCPolicyError(ValueError):
    """An administrator-submitted OIDC policy value is unsafe."""


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


def _setting_enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _oidc_recovery_mode_ready() -> bool:
    """Return whether OIDC is enabled and has the minimum sign-in settings."""
    with get_db() as db:
        enabled = _setting_enabled(get_setting(db, "oidc_enabled"))
        issuer = (get_setting(db, "oidc_issuer") or "").strip()
        client_id = (get_setting(db, "oidc_client_id") or "").strip()
    return enabled and bool(issuer and client_id)


def get_local_login_policy() -> LocalLoginPolicy:
    """Return validated local-login policy, failing open if recovery is unsafe."""
    with get_db() as db:
        mode = (get_setting(db, "oidc_local_login_mode") or LOCAL_LOGIN_ENABLED).strip()
        if mode not in _VALID_MODES or mode == LOCAL_LOGIN_ENABLED:
            return _enabled_policy()

        raw_id = (get_setting(db, "oidc_break_glass_user_id") or "").strip()
        try:
            user_id = int(raw_id)
        except (TypeError, ValueError):
            logger.error(
                "OIDC recovery-only policy has no valid break-glass user; enabling local login"
            )
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
            logger.error(
                "OIDC break-glass account is no longer a local administrator; enabling local login"
            )
            return _enabled_policy()

        return LocalLoginPolicy(
            mode=LOCAL_LOGIN_RECOVERY_ONLY,
            break_glass_user_id=int(user["id"]),
            break_glass_username=user["username"],
        )


def save_local_login_policy(mode: str, break_glass_username: str = "") -> LocalLoginPolicy:
    """Validate and save the local password-login recovery policy."""
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

    if not _oidc_recovery_mode_ready():
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
            raise OIDCPolicyError(
                "The break-glass account must remain a local account, not an OIDC identity"
            )

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
        break_glass_user_id=int(user["id"]),
        break_glass_username=user["username"],
    )


def local_password_login_allowed(user_id: int) -> bool:
    policy = get_local_login_policy()
    return not policy.recovery_only or int(user_id) == policy.break_glass_user_id


def get_oidc_session_hours() -> int:
    """Return the fixed OIDC reauthentication interval in hours."""
    with get_db() as db:
        raw = (
            get_setting(db, "oidc_session_hours") or str(DEFAULT_OIDC_SESSION_HOURS)
        ).strip()
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_OIDC_SESSION_HOURS
    if not MIN_OIDC_SESSION_HOURS <= hours <= MAX_OIDC_SESSION_HOURS:
        return DEFAULT_OIDC_SESSION_HOURS
    return hours


def get_oidc_session_ttl_seconds() -> int:
    return get_oidc_session_hours() * 3600


def save_oidc_session_hours(value: str | int) -> int:
    try:
        hours = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise OIDCPolicyError(
            "OIDC reauthentication interval must be a whole number of hours"
        ) from exc
    if not MIN_OIDC_SESSION_HOURS <= hours <= MAX_OIDC_SESSION_HOURS:
        raise OIDCPolicyError(
            f"OIDC reauthentication interval must be between "
            f"{MIN_OIDC_SESSION_HOURS} and {MAX_OIDC_SESSION_HOURS} hours"
        )
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('oidc_session_hours', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(hours),),
        )
    return hours
