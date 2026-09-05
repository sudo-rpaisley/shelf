"""Coverage for configurable OIDC reauthentication and provider logout."""

import time
from urllib.parse import unquote

from app.auth import create_token, decode_token
from app.oidc import OIDCError
from app.oidc_policy import get_oidc_session_hours


def _set_setting(db, key: str, value: str):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _configure_oidc(db):
    for key, value in (
        ("oidc_enabled", "1"),
        ("oidc_provider_name", "Test IdP"),
        ("oidc_issuer", "https://idp.example/application/o/shelf/"),
        ("oidc_client_id", "shelf-client"),
    ):
        _set_setting(db, key, value)
    db.commit()


def test_oidc_session_policy_defaults_to_24_hours():
    assert get_oidc_session_hours() == 24


def test_admin_can_change_oidc_reauthentication_interval(admin_client, db):
    response = admin_client.post(
        "/api/oidc/session-policy",
        data={"oidc_session_hours": "8"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?oidc_session_saved=8"
    assert get_oidc_session_hours() == 8


def test_invalid_oidc_reauthentication_interval_is_rejected(admin_client):
    for value in ("0", "169", "not-a-number"):
        response = admin_client.post(
            "/api/oidc/session-policy",
            data={"oidc_session_hours": value},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "oidc_session_error=" in response.headers["location"]
    assert get_oidc_session_hours() == 24


def test_oidc_callback_uses_configured_fixed_session_ceiling(admin_client, admin_user, db, monkeypatch):
    _set_setting(db, "oidc_session_hours", "1")
    db.commit()
    token_version = db.execute(
        "SELECT token_version FROM users WHERE id = ?", (admin_user["id"],)
    ).fetchone()["token_version"]

    async def fake_complete_login(_request, _config):
        return {
            "id": admin_user["id"],
            "username": admin_user["username"],
            "role": admin_user["role"],
            "display_name": admin_user["display_name"],
            "token_version": token_version,
        }

    monkeypatch.setattr("app.routers.auth_routes.complete_login", fake_complete_login)
    admin_client.cookies.delete("access_token")

    response = admin_client.get(
        "/login?code=fake-code&state=fake-state",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/browse"
    payload = decode_token(response.cookies.get("access_token"))
    assert payload is not None
    assert payload["authn"] == "oidc"
    assert 3595 <= payload["reauth"] - payload["iat"] <= 3605
    assert payload["exp"] <= payload["reauth"]


def test_provider_logout_setting_requires_oidc_configuration(admin_client):
    response = admin_client.post(
        "/api/oidc/logout-policy",
        data={"oidc_provider_logout": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "oidc_logout_error=" in response.headers["location"]


def test_oidc_logout_redirects_to_provider_after_clearing_shelf_session(
    client, admin_user, db, monkeypatch
):
    _configure_oidc(db)
    _set_setting(db, "oidc_provider_logout", "1")
    db.commit()
    token_version = db.execute(
        "SELECT token_version FROM users WHERE id = ?", (admin_user["id"],)
    ).fetchone()["token_version"]
    token = create_token(
        admin_user["id"],
        admin_user["username"],
        admin_user["role"],
        admin_user["display_name"],
        token_version,
        auth_method="oidc",
        reauth_at=int(time.time()) + 3600,
    )
    client.cookies.set("access_token", token)

    async def fake_provider_logout_url(_request):
        return "https://idp.example/logout?client_id=shelf-client"

    monkeypatch.setattr("app.routers.auth_routes.provider_logout_url", fake_provider_logout_url)
    response = client.post("/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "https://idp.example/logout?client_id=shelf-client"
    set_cookie = response.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie
    assert "csrf_token=" in set_cookie


def test_local_session_never_redirects_to_oidc_provider(admin_client, db, monkeypatch):
    _configure_oidc(db)
    _set_setting(db, "oidc_provider_logout", "1")
    db.commit()
    called = False

    async def should_not_be_called(_request):
        nonlocal called
        called = True
        return "https://idp.example/logout"

    monkeypatch.setattr("app.routers.auth_routes.provider_logout_url", should_not_be_called)
    response = admin_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert called is False


def test_provider_logout_failure_cannot_block_local_shelf_logout(client, admin_user, db, monkeypatch):
    _configure_oidc(db)
    _set_setting(db, "oidc_provider_logout", "1")
    db.commit()
    token_version = db.execute(
        "SELECT token_version FROM users WHERE id = ?", (admin_user["id"],)
    ).fetchone()["token_version"]
    client.cookies.set(
        "access_token",
        create_token(
            admin_user["id"],
            admin_user["username"],
            admin_user["role"],
            admin_user["display_name"],
            token_version,
            auth_method="oidc",
            reauth_at=int(time.time()) + 3600,
        ),
    )

    async def failed_provider_logout(_request):
        raise OIDCError("provider unavailable")

    monkeypatch.setattr("app.routers.auth_routes.provider_logout_url", failed_provider_logout)
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "access_token=" in response.headers.get("set-cookie", "")


def test_settings_exposes_session_and_logout_policy_controls(admin_client, db):
    _configure_oidc(db)
    response = admin_client.get("/settings")
    assert response.status_code == 200
    assert 'data-testid="oidc-session-policy"' in response.text
    assert 'action="/api/oidc/session-policy"' in response.text
    assert 'data-testid="oidc-logout-policy"' in response.text
    assert 'action="/api/oidc/logout-policy"' in response.text
