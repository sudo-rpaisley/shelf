"""Administrative linking of existing Shelf users to stable OIDC identities.

Linking is deliberately explicit: username/email similarity is never identity
proof. An administrator supplies the provider's stable ``sub`` value and Shelf
binds it to the currently configured issuer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.oidc_policy import get_local_login_policy
from app.services import oidc_login

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/settings/oidc/accounts",
    dependencies=[Depends(require_role("admin"))],
)

_MAX_SUBJECT_LENGTH = 512
_MAX_EMAIL_LENGTH = 320


def _redirect(status: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/settings?oidc_account_status={status}", status_code=303
    )


def _valid_subject(value: str) -> bool:
    return bool(value) and len(value) <= _MAX_SUBJECT_LENGTH and not any(
        ord(ch) < 32 for ch in value
    )


@router.post("/link-existing")
async def link_existing_oidc_account(request: Request):
    form = await request.form()
    username = str(form.get("shelf_username") or "").strip()
    subject = str(form.get("oidc_subject") or "").strip()
    email = str(form.get("oidc_email") or "").strip()

    config = oidc_login.get_login_config().core
    if not config.configured:
        return _redirect("not_configured")
    if not username or not _valid_subject(subject) or len(email) > _MAX_EMAIL_LENGTH:
        return _redirect("invalid")

    with get_db() as db:
        user = db.execute(
            "SELECT id, username, role FROM users "
            "WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if not user:
            return _redirect("missing_user")

        policy = get_local_login_policy()
        if policy.recovery_only and int(user["id"]) == policy.break_glass_user_id:
            return _redirect("recovery_account")

        if db.execute(
            "SELECT 1 FROM user_identities WHERE user_id = ? LIMIT 1",
            (int(user["id"]),),
        ).fetchone():
            return _redirect("already_linked")

        if db.execute(
            "SELECT 1 FROM user_identities WHERE issuer = ? AND subject = ? LIMIT 1",
            (config.issuer, subject),
        ).fetchone():
            return _redirect("subject_used")

        # Linking immediately makes this account OIDC-only. Never convert the
        # final genuinely local administrator; keep an independent recovery
        # path even when normal local sign-in is currently enabled.
        if user["role"] == "admin":
            local_admins = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM users u "
                    "WHERE u.role = 'admin' AND NOT EXISTS ("
                    "SELECT 1 FROM user_identities ui WHERE ui.user_id = u.id)"
                ).fetchone()["c"]
            )
            if local_admins <= 1:
                return _redirect("last_local_admin")

        db.execute(
            "INSERT INTO user_identities "
            "(user_id, provider, issuer, subject, email) "
            "VALUES (?, 'oidc', ?, ?, ?)",
            (int(user["id"]), config.issuer, subject, email or None),
        )
        # Invalidate all already-issued local sessions at the same boundary at
        # which password login becomes unavailable for this account.
        db.execute(
            "UPDATE users SET token_version = token_version + 1, "
            "updated_at = datetime('now') WHERE id = ?",
            (int(user["id"]),),
        )

    logger.info(
        "Existing Shelf user '%s' linked to OIDC issuer=%s by admin '%s'",
        user["username"],
        config.issuer,
        request.state.user["username"],
    )
    return _redirect("linked")
