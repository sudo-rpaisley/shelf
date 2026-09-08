"""OIDC fixed-session and break-glass recovery policy coverage."""

import time
from types import SimpleNamespace

import jwt
import pytest
from fastapi import Request, Response

from app import auth
from app.oidc_policy import (
    DEFAULT_OIDC_SESSION_HOURS,
    LOCAL_LOGIN_ENABLED,
    LOCAL_LOGIN_RECOVERY_ONLY,
    OIDCPolicyError,
    get_local_login_policy,
    get_oidc_session_hours,
    local_password_login_allowed,
    save_local_login_policy,
    save_oidc_session_hours,
)


def _request_with_token(token: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/browse",
        "headers": [(b"cookie", f"access_token={token}".encode())],
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "scheme": "http",
    }
    return Request(scope)


def test_oidc_token_uses_fixed_reauthentication_ceiling(monkeypatch, admin_user):
    now = int(time.time())
    reauth_at = now + 3600
    token = auth.create_token(
        admin_user["id"],
        admin_user["username"],
        admin_user["role"],
        admin_user["display_name"],
        auth_method="oidc",
        reauth_at=reauth_at,
    )
    payload = auth.decode_token(token)
    assert payload["authn"] == "oidc"
    assert payload["reauth"] == reauth_at
    assert payload["exp"] <= reauth_at

    midpoint = (payload["iat"] + payload["exp"]) / 2
    monkeypatch.setattr(auth.time, "time", lambda: midpoint + 1)
    assert auth.should_refresh_token(_request_with_token(token)) is None


def test_local_sessions_keep_half_life_refresh(monkeypatch, admin_user):
    token = auth.create_token(
        admin_user["id"],
        admin_user["username"],
        admin_user["role"],
        admin_user["display_name"],
    )
    payload = auth.decode_token(token)
    midpoint = (payload["iat"] + payload["exp"]) / 2
    monkeypatch.setattr(auth.time, "time", lambda: midpoint + 1)
    refreshed = auth.should_refresh_token(_request_with_token(token))
    assert refreshed is not None
    refreshed_payload = auth.decode_token(refreshed)
    assert refreshed_payload["authn"] == "local"
    assert "reauth" not in refreshed_payload


def test_current_user_exposes_auth_method_and_reauth(admin_client, admin_user):
    reauth_at = int(time.time()) + 7200
    token = auth.create_token(
        admin_user["id"],
        admin_user["username"],
        admin_user["role"],
        admin_user["display_name"],
        auth_method="oidc",
        reauth_at=reauth_at,
    )
    admin_client.cookies.set("access_token", token)
    response = admin_client.get("/browse")
    assert response.status_code == 200

    request = _request_with_token(token)
    user = auth.get_current_user(request)
    assert user["auth_method"] == "oidc"
    assert user["reauth_at"] == reauth_at


def test_cookie_max_age_can_match_shorter_oidc_session():
    response = Response()
    auth.set_auth_cookie(response, "token", csrf_token="csrf", max_age=3600)
    cookies = response.headers.getlist("set-cookie")
    assert len(cookies) == 2
    assert all("Max-Age=3600" in cookie for cookie in cookies)


def test_session_hours_default_validate_and_persist(db):
    assert get_oidc_session_hours() == DEFAULT_OIDC_SESSION_HOURS
    assert save_oidc_session_hours("12") == 12
    assert get_oidc_session_hours() == 12

    for bad in ("0", "169", "not-a-number"):
        with pytest.raises(OIDCPolicyError):
            save_oidc_session_hours(bad)

    db.execute(
        "INSERT INTO settings (key, value) VALUES ('oidc_session_hours', '999') "
        "ON CONFLICT(key) DO UPDATE SET value='999'"
    )
    db.commit()
    assert get_oidc_session_hours() == DEFAULT_OIDC_SESSION_HOURS


def test_recovery_only_requires_configured_oidc(monkeypatch, admin_user):
    import app.oidc as oidc

    monkeypatch.setattr(
        oidc,
        "get_oidc_config",
        lambda: SimpleNamespace(enabled=False, configured=False),
    )
    with pytest.raises(OIDCPolicyError, match="Enable and configure OIDC"):
        save_local_login_policy(LOCAL_LOGIN_RECOVERY_ONLY, admin_user["username"])


def test_recovery_only_allows_only_selected_local_admin(monkeypatch, db, admin_user, viewer_user):
    import app.oidc as oidc

    monkeypatch.setattr(
        oidc,
        "get_oidc_config",
        lambda: SimpleNamespace(enabled=True, configured=True),
    )
    policy = save_local_login_policy(
        LOCAL_LOGIN_RECOVERY_ONLY, admin_user["username"]
    )
    assert policy.recovery_only
    assert policy.break_glass_user_id == admin_user["id"]
    assert local_password_login_allowed(admin_user["id"])
    assert not local_password_login_allowed(viewer_user["id"])

    enabled = save_local_login_policy(LOCAL_LOGIN_ENABLED)
    assert not enabled.recovery_only
    assert local_password_login_allowed(viewer_user["id"])


def test_break_glass_must_remain_local_admin(monkeypatch, db, admin_user):
    import app.oidc as oidc

    monkeypatch.setattr(
        oidc,
        "get_oidc_config",
        lambda: SimpleNamespace(enabled=True, configured=True),
    )
    save_local_login_policy(LOCAL_LOGIN_RECOVERY_ONLY, admin_user["username"])

    db.execute(
        "INSERT INTO user_identities (user_id, provider, issuer, subject) "
        "VALUES (?, 'oidc', 'https://id.example/', 'admin-sub')",
        (admin_user["id"],),
    )
    db.commit()

    # Invalid persisted recovery configuration fails open so the operator can
    # still sign in locally and repair it.
    policy = get_local_login_policy()
    assert policy.mode == LOCAL_LOGIN_ENABLED
    assert local_password_login_allowed(admin_user["id"])
