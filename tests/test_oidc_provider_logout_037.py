"""Explicit provider logout behaviour for OIDC sessions."""

import time

from app import auth
from app.database import get_setting
from app.services.oidc_logout import (
    OIDCLogoutError,
    get_provider_logout_url,
    validate_provider_logout_url,
)


def _save_logout_url(db, value: str):
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('oidc_provider_logout_url', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (value,),
    )
    db.commit()


def _oidc_cookie(client, user):
    now = int(time.time())
    token = auth.create_token(
        user["id"],
        user["username"],
        user["role"],
        user["display_name"],
        user.get("token_version", 1),
        auth_method="oidc",
        reauth_at=now + 3600,
    )
    client.cookies.set("access_token", token)


def test_provider_logout_url_requires_explicit_https(monkeypatch):
    monkeypatch.delenv("SHELF_OIDC_ALLOW_INSECURE_HTTP", raising=False)
    assert validate_provider_logout_url("") == ""
    assert (
        validate_provider_logout_url("https://id.example/logout?client_id=shelf")
        == "https://id.example/logout?client_id=shelf"
    )
    for invalid in (
        "http://id.example/logout",
        "javascript:alert(1)",
        "https://user:pass@id.example/logout",
        "https://id.example/logout#fragment",
    ):
        try:
            validate_provider_logout_url(invalid)
        except OIDCLogoutError:
            pass
        else:
            raise AssertionError(f"accepted unsafe provider logout URL: {invalid}")


def test_invalid_persisted_logout_url_falls_back_to_empty(db, monkeypatch):
    monkeypatch.delenv("SHELF_OIDC_ALLOW_INSECURE_HTTP", raising=False)
    _save_logout_url(db, "http://id.example/logout")
    assert get_provider_logout_url() == ""


def test_local_session_never_redirects_to_provider(admin_client, db):
    _save_logout_url(db, "https://id.example/logout")
    response = admin_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert response.headers["cache-control"] == "no-store"


def test_oidc_session_uses_only_explicit_provider_logout(admin_client, db, admin_user):
    _save_logout_url(db, "https://id.example/logout?client_id=shelf")
    _oidc_cookie(admin_client, admin_user)
    response = admin_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "https://id.example/logout?client_id=shelf"
    assert response.headers["cache-control"] == "no-store"
    assert response.cookies.get("access_token") in (None, "")


def test_oidc_session_without_explicit_url_stays_local(admin_client, admin_user):
    _oidc_cookie(admin_client, admin_user)
    response = admin_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_settings_save_and_clear_provider_logout_url(admin_client, db):
    form = {
        "oidc_enabled": "1",
        "oidc_provider_name": "Authentik",
        "oidc_issuer": "https://id.example/application/o/shelf/",
        "oidc_client_id": "shelf-client",
        "oidc_scopes": "openid profile email",
        "oidc_group_claim": "groups",
        "oidc_default_role": "deny",
        "oidc_auto_provision": "1",
        "oidc_sync_roles": "1",
        "oidc_provider_logout_url": "https://id.example/logout?client_id=shelf",
    }
    response = admin_client.post(
        "/api/settings/oidc", data=form, follow_redirects=False
    )
    assert response.headers["location"] == "/settings?oidc_status=saved"
    assert get_setting(db, "oidc_provider_logout_url") == "https://id.example/logout?client_id=shelf"

    form["oidc_provider_logout_url"] = ""
    admin_client.post("/api/settings/oidc", data=form, follow_redirects=False)
    assert get_setting(db, "oidc_provider_logout_url") == ""


def test_settings_rejects_unsafe_logout_url_before_write(admin_client, db, monkeypatch):
    monkeypatch.delenv("SHELF_OIDC_ALLOW_INSECURE_HTTP", raising=False)
    _save_logout_url(db, "https://id.example/original")
    form = {
        "oidc_enabled": "1",
        "oidc_provider_name": "Authentik",
        "oidc_issuer": "https://id.example/application/o/shelf/",
        "oidc_client_id": "shelf-client",
        "oidc_scopes": "openid profile email",
        "oidc_group_claim": "groups",
        "oidc_default_role": "deny",
        "oidc_provider_logout_url": "http://evil.example/logout",
    }
    response = admin_client.post(
        "/api/settings/oidc", data=form, follow_redirects=False
    )
    assert response.headers["location"] == "/settings?oidc_status=invalid"
    assert get_setting(db, "oidc_provider_logout_url") == "https://id.example/original"


def test_settings_page_explains_logout_is_not_discovered(admin_client):
    response = admin_client.get("/settings")
    assert response.status_code == 200
    assert "Provider logout URL" in response.text
    assert "never guesses this endpoint" in response.text
