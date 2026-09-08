"""Bind Shelf's 0.37 login surface to the provider-neutral OIDC core.

This module owns runtime sign-in orchestration only: loading the persisted
configuration, the short-lived encrypted flow cookie, protocol completion and
account provisioning. Settings persistence/UI and provider logout deliberately
live in later slices.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from fastapi import Request, Response

from app.database import get_db, get_setting
from app.oidc import (
    FLOW_COOKIE,
    FLOW_TTL_SECONDS,
    OIDCAccessDenied,
    OIDCConfig,
    OIDCError,
    _decode_flow,
    _encode_flow,
    build_authorization_redirect,
    discover,
    identity_from_claims,
    parse_groups,
    validate_callback_state,
)
from app.oidc_tokens import exchange_code, fetch_jwks, userinfo_claims, validate_id_token
from app.services.oidc_accounts import provision_or_sync


@dataclass(frozen=True)
class OIDCLoginConfig:
    enabled: bool
    provider_name: str
    core: OIDCConfig

    @property
    def available(self) -> bool:
        return self.enabled and self.core.configured


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_login_config() -> OIDCLoginConfig:
    """Read the current OIDC login configuration through Shelf settings."""
    with get_db() as db:
        enabled = _as_bool(get_setting(db, "oidc_enabled"), False)
        provider_name = (
            get_setting(db, "oidc_provider_name") or "OpenID Connect"
        ).strip() or "OpenID Connect"
        issuer = (get_setting(db, "oidc_issuer") or "").strip()
        client_id = (get_setting(db, "oidc_client_id") or "").strip()
        client_secret = get_setting(db, "oidc_client_secret") or ""
        scopes = (get_setting(db, "oidc_scopes") or "openid profile email").strip()
        group_claim = (get_setting(db, "oidc_group_claim") or "groups").strip() or "groups"
        required_group = (get_setting(db, "oidc_required_group") or "").strip()
        admin_groups = parse_groups(get_setting(db, "oidc_admin_groups"))
        editor_groups = parse_groups(get_setting(db, "oidc_editor_groups"))
        viewer_groups = parse_groups(get_setting(db, "oidc_viewer_groups"))
        default_role = (get_setting(db, "oidc_default_role") or "deny").strip().lower()
        auto_provision = _as_bool(get_setting(db, "oidc_auto_provision"), True)
        sync_roles = _as_bool(get_setting(db, "oidc_sync_roles"), True)

    # Persisted values may pre-date current validation. Fail closed for an
    # invalid default role instead of turning a bad setting into admin access
    # or making the public login page crash.
    if default_role not in {"deny", "viewer", "editor"}:
        default_role = "deny"

    return OIDCLoginConfig(
        enabled=enabled,
        provider_name=provider_name,
        core=OIDCConfig(
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
            group_claim=group_claim,
            required_group=required_group,
            admin_groups=admin_groups,
            editor_groups=editor_groups,
            viewer_groups=viewer_groups,
            default_role=default_role,
            auto_provision=auto_provision,
            sync_roles=sync_roles,
        ),
    )


def set_flow_cookie(response: Response, flow: dict[str, Any]) -> None:
    secure = not bool(os.environ.get("SHELF_DEV_INSECURE_COOKIES"))
    response.set_cookie(
        FLOW_COOKIE,
        _encode_flow(flow),
        max_age=FLOW_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/login",
    )


def clear_flow_cookie(response: Response) -> None:
    response.delete_cookie(FLOW_COOKIE, path="/login")


async def begin_login(request: Request, config: OIDCLoginConfig) -> tuple[str, dict[str, Any]]:
    if not config.available:
        raise OIDCError("OIDC sign-in is not enabled")
    metadata = await discover(config.core)
    redirect_uri = str(request.url_for("login_page"))
    return build_authorization_redirect(
        config.core,
        metadata,
        redirect_uri=redirect_uri,
    )


async def complete_login(request: Request, config: OIDCLoginConfig) -> dict[str, Any]:
    """Validate one callback and return the provisioned/synchronised Shelf user."""
    if not config.available:
        raise OIDCError("OIDC sign-in is not enabled")

    provider_error = request.query_params.get("error")
    if provider_error:
        if provider_error == "access_denied":
            raise OIDCAccessDenied("Sign-in was cancelled or denied by the identity provider")
        raise OIDCError("The identity provider could not complete sign-in")

    flow = _decode_flow(request.cookies.get(FLOW_COOKIE))
    validate_callback_state(flow, request.query_params.get("state"))

    if flow.get("issuer") != config.core.issuer:
        raise OIDCError("OIDC sign-in session does not match the configured issuer")

    code = request.query_params.get("code")
    verifier = flow.get("verifier")
    redirect_uri = flow.get("redirect_uri")
    nonce = flow.get("nonce")
    if not all(isinstance(value, str) and value for value in (code, verifier, redirect_uri, nonce)):
        raise OIDCError("OIDC sign-in response is incomplete")

    metadata = await discover(config.core)
    token = await exchange_code(
        metadata,
        config.core,
        code=code,
        redirect_uri=redirect_uri,
        verifier=verifier,
    )
    jwks = await fetch_jwks(metadata)
    claims = validate_id_token(
        token["id_token"],
        metadata,
        config.core,
        expected_nonce=nonce,
        jwks=jwks,
    )

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise OIDCError("OIDC subject claim is missing")
    extra_claims = await userinfo_claims(metadata, token, subject=subject)
    if extra_claims:
        # userinfo_claims already proves the same subject. Keep protocol claims
        # (including issuer/audience/lifetime/nonce) authoritative while letting
        # UserInfo enrich profile/group values.
        merged = dict(claims)
        for key, value in extra_claims.items():
            if key not in {"iss", "aud", "azp", "exp", "iat", "nbf", "nonce"}:
                merged[key] = value
        merged["sub"] = subject
        claims = merged

    identity = identity_from_claims(claims, config.core)
    return provision_or_sync(identity, config.core)
