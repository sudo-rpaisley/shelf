"""Administrative OIDC account migration and login-policy routes.

Identity linking is deliberately explicit. Shelf never treats a matching
username or email address as proof that a local account and an OIDC identity
belong to the same person; an administrator must bind the provider's stable
``sub`` value to the intended Shelf account.
"""

import logging
from urllib.parse import urlencode

from fastapi import Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.oidc import get_oidc_config
from app.oidc_policy import (
    OIDCPolicyError,
    get_local_login_policy,
    save_local_login_policy,
)
from app.routers.auth_routes import router

logger = logging.getLogger(__name__)

_MAX_SUBJECT_LENGTH = 512
_MAX_EMAIL_LENGTH = 320


def _settings_redirect(**params: str) -> RedirectResponse:
    return RedirectResponse(url="/settings?" + urlencode(params), status_code=303)


@router.post("/api/oidc/local-login-policy")
async def update_local_login_policy(
    request: Request,
    local_login_mode: str = Form("enabled"),
    break_glass_username: str = Form(""),
    _=Depends(require_role("admin")),
):
    """Choose normal local login or a single local recovery administrator."""
    try:
        policy = save_local_login_policy(local_login_mode, break_glass_username)
    except OIDCPolicyError as exc:
        logger.warning(
            "OIDC local-login policy rejected for admin '%s': %s",
            request.state.user["username"],
            exc,
        )
        return _settings_redirect(oidc_policy_error=str(exc))

    logger.info(
        "OIDC local-login policy changed to '%s' by admin '%s'",
        policy.mode,
        request.state.user["username"],
    )
    return _settings_redirect(oidc_policy_saved=policy.mode)


@router.post("/api/oidc/link-existing")
async def link_existing_oidc_account(
    request: Request,
    shelf_username: str = Form(...),
    oidc_subject: str = Form(...),
    oidc_email: str = Form(""),
    _=Depends(require_role("admin")),
):
    """Bind one existing local Shelf account to the configured OIDC issuer.

    The caller supplies the stable OIDC subject explicitly. We do not infer
    account ownership from username or email because either value can change
    or collide and neither is an authentication proof.
    """
    username = shelf_username.strip()
    subject = oidc_subject.strip()
    email = oidc_email.strip()
    config = get_oidc_config()

    if not config.configured:
        return _settings_redirect(oidc_link_error="Configure the OIDC issuer and Client ID before linking an account")
    if not username:
        return _settings_redirect(oidc_link_error="Enter the existing Shelf username to link")
    if not subject:
        return _settings_redirect(oidc_link_error="Enter the OIDC subject (sub) for this identity")
    if len(subject) > _MAX_SUBJECT_LENGTH or any(ord(ch) < 32 for ch in subject):
        return _settings_redirect(oidc_link_error="The OIDC subject is invalid")
    if email and len(email) > _MAX_EMAIL_LENGTH:
        return _settings_redirect(oidc_link_error="The OIDC email value is too long")

    with get_db() as db:
        user = db.execute(
            "SELECT id, username, role FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if not user:
            return _settings_redirect(oidc_link_error=f"Shelf user '{username}' was not found")

        policy = get_local_login_policy()
        if policy.recovery_only and user["id"] == policy.break_glass_user_id:
            return _settings_redirect(
                oidc_link_error="This account is the configured break-glass administrator; change the local-login policy before linking it"
            )

        existing_for_user = db.execute(
            "SELECT issuer, subject FROM user_identities WHERE user_id = ? LIMIT 1",
            (user["id"],),
        ).fetchone()
        if existing_for_user:
            return _settings_redirect(oidc_link_error=f"Shelf user '{user['username']}' already has an external identity")

        existing_subject = db.execute(
            "SELECT u.username FROM user_identities ui "
            "JOIN users u ON u.id = ui.user_id "
            "WHERE ui.issuer = ? AND ui.subject = ?",
            (config.issuer, subject),
        ).fetchone()
        if existing_subject:
            return _settings_redirect(
                oidc_link_error=f"That OIDC subject is already linked to '{existing_subject['username']}'"
            )

        # Keep at least one genuinely local administrator as a recovery path.
        # Linking immediately makes an account OIDC-only, so converting the
        # final local admin would defeat the branch's break-glass guarantee.
        if user["role"] == "admin":
            local_admin_count = db.execute(
                "SELECT COUNT(*) AS c FROM users u "
                "WHERE u.role = 'admin' AND NOT EXISTS ("
                "SELECT 1 FROM user_identities ui WHERE ui.user_id = u.id)"
            ).fetchone()["c"]
            if local_admin_count <= 1:
                return _settings_redirect(
                    oidc_link_error="Cannot link the last local administrator; keep a separate break-glass admin"
                )

        db.execute(
            "INSERT INTO user_identities "
            "(user_id, provider, issuer, subject, email) VALUES (?, 'oidc', ?, ?, ?)",
            (user["id"], config.issuer, subject, email or None),
        )
        # Attaching an OIDC identity disables password login for this account.
        # Invalidate any already-issued local JWTs at the same boundary so the
        # change takes effect immediately rather than at session expiry.
        db.execute(
            "UPDATE users SET token_version = token_version + 1, "
            "updated_at = datetime('now') WHERE id = ?",
            (user["id"],),
        )

    logger.info(
        "Existing Shelf user '%s' linked to OIDC issuer=%s by admin '%s'",
        user["username"],
        config.issuer,
        request.state.user["username"],
    )
    return _settings_redirect(oidc_linked=user["username"])
