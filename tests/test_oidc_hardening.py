"""Security regression coverage for OIDC-managed Shelf accounts."""

import time
from unittest.mock import MagicMock

from app.auth import create_token, decode_token, hash_password, should_refresh_token
from app.oidc import OIDCConfig, OIDCError, OIDCIdentity, provision_or_sync


def _config() -> OIDCConfig:
    return OIDCConfig(
        enabled=True,
        provider_name="Test IdP",
        issuer="https://idp.example/application/o/shelf/",
        client_id="shelf-client",
        client_secret="secret",
        scopes="openid profile email groups",
        group_claim="groups",
        required_group="Shelf-Users",
        admin_groups=("Shelf-Admins",),
        editor_groups=("Shelf-Editors",),
        viewer_groups=("Shelf-Users",),
        default_role="deny",
        auto_provision=True,
        sync_roles=True,
    )


def _provision_oidc_user(username: str = "oidc-user") -> dict:
    config = _config()
    identity = OIDCIdentity(
        issuer=config.issuer,
        subject=f"subject-{username}",
        username=username,
        display_name="OIDC User",
        email=f"{username}@example.com",
        groups=("Shelf-Users",),
        role="viewer",
    )
    return provision_or_sync(identity, config)


def _settings_form(**overrides) -> dict[str, str]:
    values = {
        "oidc_enabled": "true",
        "oidc_provider_name": "Test IdP",
        "oidc_issuer": "https://idp.example/application/o/shelf/",
        "oidc_client_id": "shelf-client",
        "oidc_client_secret": "secret",
        "oidc_scopes": "openid profile email groups",
        "oidc_group_claim": "groups",
        "oidc_required_group": "Shelf-Users",
        "oidc_admin_groups": "Shelf-Admins",
        "oidc_editor_groups": "Shelf-Editors",
        "oidc_viewer_groups": "Shelf-Users",
        "oidc_default_role": "deny",
        "oidc_auto_provision": "true",
        "oidc_sync_roles": "true",
    }
    values.update(overrides)
    return values


def test_oidc_session_has_fixed_reauthentication_ceiling():
    reauth_at = int(time.time()) + 3600
    token = create_token(
        42,
        "oidc-user",
        "viewer",
        "OIDC User",
        auth_method="oidc",
        reauth_at=reauth_at,
    )
    payload = decode_token(token)
    assert payload is not None
    assert payload["authn"] == "oidc"
    assert payload["reauth"] == reauth_at
    assert payload["exp"] <= reauth_at

    request = MagicMock()
    request.cookies = {"access_token": token}
    assert should_refresh_token(request) is None


def test_local_login_is_rejected_for_oidc_identity_even_with_known_password(client, admin_user, db):
    user = _provision_oidc_user("managed-login")

    # Defence in depth: even if the random local password were replaced in the
    # database, the presence of an external identity must make /login refuse it.
    db.execute(
        "UPDATE users SET password = ? WHERE id = ?",
        (hash_password("KnownPassword123!"), user["id"]),
    )
    db.commit()

    response = client.post(
        "/login",
        data={"username": user["username"], "password": "KnownPassword123!"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "signs in through the configured identity provider" in response.text
    assert "access_token" not in response.cookies


def test_admin_cannot_set_password_for_oidc_identity(admin_client):
    user = _provision_oidc_user("managed-reset")
    response = admin_client.post(
        f"/api/users/{user['id']}/password",
        data={"password": "NewPassword123!"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "do not use Shelf passwords" in response.json()["message"]


def test_oidc_identity_cannot_change_password_or_display_name(client, admin_user):
    user = _provision_oidc_user("managed-self-service")
    reauth_at = int(time.time()) + 3600
    token = create_token(
        user["id"],
        user["username"],
        user["role"],
        user["display_name"],
        user["token_version"],
        auth_method="oidc",
        reauth_at=reauth_at,
    )
    client.cookies.set("access_token", token)

    password_response = client.post(
        "/api/account/password",
        data={"current_password": "irrelevant", "new_password": "NewPassword123!"},
    )
    assert password_response.status_code == 200
    assert password_response.json()["ok"] is False
    assert "managed by OIDC" in password_response.json()["message"]

    name_response = client.post(
        "/api/account/display-name",
        data={"display_name": "Locally Overridden"},
    )
    assert name_response.status_code == 200
    assert name_response.json()["ok"] is False
    assert "managed by your OIDC identity provider" in name_response.json()["message"]


def test_save_and_test_oidc_configuration_reports_success(admin_client, monkeypatch):
    async def fake_discover(config):
        assert config.issuer == "https://idp.example/application/o/shelf/"
        assert config.client_id == "shelf-client"
        return {
            "issuer": config.issuer,
            "authorization_endpoint": "https://idp.example/authorize",
            "token_endpoint": "https://idp.example/token",
            "jwks_uri": "https://idp.example/jwks",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
        }

    monkeypatch.setattr("app.routers.auth_routes.discover", fake_discover)
    response = admin_client.post(
        "/api/oidc/settings",
        data=_settings_form(oidc_action="test"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?oidc_test=1"


def test_save_and_test_oidc_configuration_reports_discovery_failure(admin_client, monkeypatch):
    async def fake_discover(_config):
        raise OIDCError("Could not retrieve provider configuration")

    monkeypatch.setattr("app.routers.auth_routes.discover", fake_discover)
    response = admin_client.post(
        "/api/oidc/settings",
        data=_settings_form(oidc_action="test"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/settings?oidc_test_error=")
    assert "Could+not+retrieve+provider+configuration" in response.headers["location"]
