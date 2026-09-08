"""Administrative OIDC configuration for Shelf 0.37.

This router owns persisted OIDC settings and the already-implemented local
recovery/session policies. Provider logout is intentionally a later slice.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.auth import require_role
from app.crypto import encrypt_value, get_encryption_key
from app.database import get_db
from app.oidc import OIDCError, discover, parse_groups, validate_issuer_url
from app.oidc_policy import (
    LOCAL_LOGIN_ENABLED,
    OIDCPolicyError,
    save_local_login_policy,
    save_oidc_session_hours,
)
from app.services import oidc_login
from app.services.oidc_logout import validate_provider_logout_url, OIDCLogoutError

router = APIRouter(
    prefix="/api/settings/oidc",
    dependencies=[Depends(require_role("admin"))],
)

_ALLOWED_DEFAULT_ROLES = {"deny", "viewer", "editor"}


class OIDCSettingsError(ValueError):
    pass


def _redirect(status: str) -> RedirectResponse:
    # `status` is always one of our own fixed literals. Never put provider or
    # validation exception text into the URL where the Settings page might
    # reflect it.
    return RedirectResponse(url=f"/settings?oidc_status={status}", status_code=303)


def _text(form, key: str, default: str = "", *, limit: int = 2048) -> str:
    raw = form.get(key, default)
    if raw is None:
        return default
    value = str(raw).strip()
    if len(value) > limit:
        raise OIDCSettingsError(f"{key} is too long")
    return value


def _normalise_scopes(value: str) -> str:
    seen: set[str] = set()
    scopes: list[str] = []
    for raw in value.split():
        scope = raw.strip()
        if scope and scope not in seen:
            seen.add(scope)
            scopes.append(scope)
    if "openid" not in seen:
        scopes.insert(0, "openid")
    return " ".join(scopes) or "openid"


def _normalise_groups(value: str) -> str:
    return "\n".join(parse_groups(value))


def _write_setting(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _write_secret(db, value: str, *, clear: bool) -> None:
    if clear:
        stored = ""
    elif not value:
        # Password fields are write-only; blank means preserve what is saved.
        return
    else:
        stored = encrypt_value(value, get_encryption_key())
    _write_setting(db, "oidc_client_secret", stored)


@router.post("")
async def update_oidc_settings(request: Request):
    form = await request.form()
    try:
        enabled = form.get("oidc_enabled") in {"1", "true", "on", "yes"}
        provider_name = _text(form, "oidc_provider_name", "OpenID Connect", limit=80)
        issuer = _text(form, "oidc_issuer", limit=1024)
        client_id = _text(form, "oidc_client_id", limit=512)
        client_secret = _text(form, "oidc_client_secret", limit=4096)
        provider_logout_url = _text(form, "oidc_provider_logout_url", limit=2048)
        if provider_logout_url:
            validate_provider_logout_url(provider_logout_url)
        scopes = _normalise_scopes(_text(form, "oidc_scopes", "openid profile email", limit=1024))
        group_claim = _text(form, "oidc_group_claim", "groups", limit=256) or "groups"
        required_group = _text(form, "oidc_required_group", limit=512)
        admin_groups = _normalise_groups(_text(form, "oidc_admin_groups", limit=4096))
        editor_groups = _normalise_groups(_text(form, "oidc_editor_groups", limit=4096))
        viewer_groups = _normalise_groups(_text(form, "oidc_viewer_groups", limit=4096))
        default_role = _text(form, "oidc_default_role", "deny", limit=16).lower()
        if default_role not in _ALLOWED_DEFAULT_ROLES:
            raise OIDCSettingsError("invalid default role")
        if issuer:
            validate_issuer_url(issuer)
        if enabled and not (issuer and client_id):
            raise OIDCSettingsError("enabled OIDC requires issuer and client ID")
    except (OIDCSettingsError, OIDCError, OIDCLogoutError):
        return _redirect("invalid")

    with get_db() as db:
        values = {
            "oidc_enabled": "1" if enabled else "0",
            "oidc_provider_name": provider_name or "OpenID Connect",
            "oidc_issuer": issuer,
            "oidc_client_id": client_id,
            "oidc_provider_logout_url": provider_logout_url,
            "oidc_scopes": scopes,
            "oidc_group_claim": group_claim,
            "oidc_required_group": required_group,
            "oidc_admin_groups": admin_groups,
            "oidc_editor_groups": editor_groups,
            "oidc_viewer_groups": viewer_groups,
            "oidc_default_role": default_role,
            "oidc_auto_provision": "1" if form.get("oidc_auto_provision") in {"1", "true", "on", "yes"} else "0",
            "oidc_sync_roles": "1" if form.get("oidc_sync_roles") in {"1", "true", "on", "yes"} else "0",
        }
        for key, value in values.items():
            _write_setting(db, key, value)
        _write_secret(
            db,
            client_secret,
            clear=form.get("oidc_clear_client_secret") in {"1", "true", "on", "yes"},
        )

    if not enabled:
        # Disabling SSO must always restore ordinary local password login. This
        # also clears the stored break-glass selection through the policy API.
        save_local_login_policy(LOCAL_LOGIN_ENABLED)

    if form.get("oidc_action") == "test":
        config = oidc_login.get_login_config()
        if not config.available:
            return _redirect("invalid")
        try:
            await discover(config.core)
        except OIDCError:
            return _redirect("test_failed")
        return _redirect("tested")

    return _redirect("saved")


@router.post("/local-login")
async def update_local_login_policy(request: Request):
    form = await request.form()
    try:
        mode = _text(form, "oidc_local_login_mode", LOCAL_LOGIN_ENABLED, limit=32)
        username = _text(form, "oidc_break_glass_username", limit=128)
        save_local_login_policy(mode, username)
    except (OIDCPolicyError, OIDCSettingsError):
        return _redirect("policy_invalid")
    return _redirect("policy_saved")


@router.post("/session")
async def update_session_policy(request: Request):
    form = await request.form()
    try:
        hours = _text(form, "oidc_session_hours", limit=8)
        save_oidc_session_hours(hours)
    except (OIDCPolicyError, OIDCSettingsError):
        return _redirect("session_invalid")
    return _redirect("session_saved")
