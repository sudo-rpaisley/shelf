"""OIDC authentication and group-to-role mapping tests."""

import json
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.oidc import (
    OIDCAccessDenied,
    OIDCConfig,
    OIDCError,
    OIDCIdentity,
    _decode_flow,
    get_oidc_config,
    identity_from_claims,
    parse_groups,
    provision_or_sync,
    role_from_groups,
    save_oidc_config,
    validate_issuer_url,
)


def _config(**overrides):
    values = dict(
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
    values.update(overrides)
    return OIDCConfig(**values)


def test_parse_groups_deduplicates_without_changing_case():
    assert parse_groups("Shelf-Admins\nShelf-Users, Shelf-Admins") == (
        "Shelf-Admins",
        "Shelf-Users",
    )


def test_role_mapping_uses_highest_matching_role():
    config = _config()
    assert role_from_groups(("Shelf-Users",), config) == "viewer"
    assert role_from_groups(("Shelf-Users", "Shelf-Editors"), config) == "editor"
    assert role_from_groups(("Shelf-Users", "Shelf-Editors", "Shelf-Admins"), config) == "admin"


def test_unmapped_groups_default_to_deny():
    config = _config(required_group="", viewer_groups=())
    assert role_from_groups(("Other",), config) is None


def test_required_group_is_a_hard_gate():
    config = _config()
    claims = {
        "sub": "123",
        "preferred_username": "alice",
        "name": "Alice",
        "groups": ["Shelf-Editors"],
    }
    with pytest.raises(OIDCAccessDenied, match="required Shelf access group"):
        identity_from_claims(claims, config)


def test_issuer_requires_https_by_default(monkeypatch):
    monkeypatch.delenv("SHELF_OIDC_ALLOW_INSECURE_HTTP", raising=False)
    with pytest.raises(OIDCError, match="HTTPS"):
        validate_issuer_url("http://idp.example/issuer")
    validate_issuer_url("https://idp.example/issuer")


def test_oidc_client_secret_is_encrypted_at_rest(db):
    save_oidc_config(
        {
            "enabled": True,
            "provider_name": "Authentik",
            "issuer": "https://auth.example/application/o/shelf/",
            "client_id": "shelf",
            "client_secret": "super-secret-value",
            "scopes": "openid profile email",
            "group_claim": "groups",
            "required_group": "Shelf-Users",
            "admin_groups": "Shelf-Admins",
            "editor_groups": "Shelf-Editors",
            "viewer_groups": "Shelf-Users",
            "default_role": "deny",
            "auto_provision": True,
            "sync_roles": True,
        }
    )
    raw = db.execute("SELECT value FROM settings WHERE key = 'oidc_client_secret'").fetchone()["value"]
    assert raw != "super-secret-value"
    assert raw.startswith("gAAAAA")
    assert get_oidc_config().client_secret == "super-secret-value"


def test_oidc_username_collision_does_not_auto_link_local_account(admin_user, db):
    config = _config()
    identity = OIDCIdentity(
        issuer=config.issuer,
        subject="external-admin-subject",
        username="admin",
        display_name="External Admin",
        email="admin@example.com",
        groups=("Shelf-Users", "Shelf-Editors"),
        role="editor",
    )
    user = provision_or_sync(identity, config)

    assert user["id"] != admin_user["id"]
    assert user["username"] == "admin-oidc"
    assert user["role"] == "editor"
    local = db.execute("SELECT role FROM users WHERE id = ?", (admin_user["id"],)).fetchone()
    assert local["role"] == "admin"


def test_role_sync_updates_existing_oidc_user_and_invalidates_sessions(admin_user, db):
    config = _config()
    identity = OIDCIdentity(
        issuer=config.issuer,
        subject="subject-1",
        username="alice",
        display_name="Alice",
        email="alice@example.com",
        groups=("Shelf-Users",),
        role="viewer",
    )
    first = provision_or_sync(identity, config)
    assert first["role"] == "viewer"
    before_version = first["token_version"]

    promoted = OIDCIdentity(**{**identity.__dict__, "groups": ("Shelf-Users", "Shelf-Admins"), "role": "admin"})
    second = provision_or_sync(promoted, config)
    assert second["id"] == first["id"]
    assert second["role"] == "admin"
    assert second["token_version"] == before_version + 1


def test_managed_oidc_role_cannot_be_changed_locally(admin_client, admin_user):
    config = _config()
    identity = OIDCIdentity(
        issuer=config.issuer,
        subject="managed-user",
        username="managed",
        display_name="Managed User",
        email=None,
        groups=("Shelf-Users",),
        role="viewer",
    )
    user = provision_or_sync(identity, config)

    # Persist matching global settings so is_role_managed() sees sync_roles=true.
    save_oidc_config(
        {
            "enabled": True,
            "provider_name": config.provider_name,
            "issuer": config.issuer,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "scopes": config.scopes,
            "group_claim": config.group_claim,
            "required_group": config.required_group,
            "admin_groups": "Shelf-Admins",
            "editor_groups": "Shelf-Editors",
            "viewer_groups": "Shelf-Users",
            "default_role": "deny",
            "auto_provision": True,
            "sync_roles": True,
        }
    )

    response = admin_client.post(f"/api/users/{user['id']}/role", data={"role": "editor"})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "managed by OIDC" in response.json()["message"]


def _rsa_key_and_jwk():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk["kid"] = "test-key"
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return private, jwk


@pytest.mark.asyncio
async def test_full_oidc_callback_provisions_mapped_role(monkeypatch, client, admin_user, db):
    config_values = {
        "enabled": True,
        "provider_name": "Authentik",
        "issuer": "https://idp.example/application/o/shelf/",
        "client_id": "shelf-client",
        "client_secret": "client-secret",
        "scopes": "openid profile email groups",
        "group_claim": "groups",
        "required_group": "Shelf-Users",
        "admin_groups": "Shelf-Admins",
        "editor_groups": "Shelf-Editors",
        "viewer_groups": "Shelf-Users",
        "default_role": "deny",
        "auto_provision": True,
        "sync_roles": True,
    }
    save_oidc_config(config_values)

    metadata = {
        "issuer": config_values["issuer"],
        "authorization_endpoint": "https://idp.example/authorize",
        "token_endpoint": "https://idp.example/token",
        "jwks_uri": "https://idp.example/jwks",
        "userinfo_endpoint": "https://idp.example/userinfo",
        "response_types_supported": ["code"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    }

    async def fake_discover(_config):
        return metadata

    monkeypatch.setattr("app.oidc.discover", fake_discover)

    start = client.get("/login?oidc=1", follow_redirects=False)
    assert start.status_code == 302
    location = start.headers["location"]
    params = parse_qs(urlparse(location).query)
    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["nonce"]
    assert params["state"]
    assert "oidc_flow" in client.cookies

    flow = _decode_flow(client.cookies.get("oidc_flow"))
    private, jwk = _rsa_key_and_jwk()
    now = int(time.time())
    id_token = jwt.encode(
        {
            "iss": metadata["issuer"],
            "sub": "authentik-user-123",
            "aud": config_values["client_id"],
            "exp": now + 300,
            "iat": now,
            "nonce": flow["nonce"],
            "preferred_username": "admin",
            "name": "OIDC Admin Name",
        },
        private,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    async def fake_exchange(*_args, **_kwargs):
        return {"id_token": id_token, "access_token": "access-token"}

    async def fake_jwks(_metadata):
        return {"keys": [jwk]}

    async def fake_userinfo(_metadata, _token, subject):
        assert subject == "authentik-user-123"
        return {
            "sub": subject,
            "email": "admin@example.com",
            "groups": ["Shelf-Users", "Shelf-Editors"],
        }

    monkeypatch.setattr("app.oidc._exchange_code", fake_exchange)
    monkeypatch.setattr("app.oidc._fetch_jwks", fake_jwks)
    monkeypatch.setattr("app.oidc._userinfo", fake_userinfo)

    callback = client.get(
        "/login",
        params={"code": "authorization-code", "state": flow["state"]},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/browse"
    assert "access_token" in client.cookies
    assert "oidc_flow" not in client.cookies

    row = db.execute(
        "SELECT u.username, u.role, ui.issuer, ui.subject "
        "FROM user_identities ui JOIN users u ON u.id = ui.user_id "
        "WHERE ui.subject = ?",
        ("authentik-user-123",),
    ).fetchone()
    assert row is not None
    # Existing local "admin" is never auto-linked solely by username.
    assert row["username"] == "admin-oidc"
    assert row["role"] == "editor"
    assert row["issuer"] == metadata["issuer"]


def test_login_page_shows_oidc_only_when_enabled(client, admin_user):
    response = client.get("/login")
    assert response.status_code == 200
    assert 'data-testid="oidc-login"' not in response.text

    save_oidc_config(
        {
            "enabled": True,
            "provider_name": "Authentik",
            "issuer": "https://idp.example/application/o/shelf/",
            "client_id": "shelf",
            "client_secret": "secret",
            "scopes": "openid profile email",
            "group_claim": "groups",
            "required_group": "Shelf-Users",
            "admin_groups": "Shelf-Admins",
            "editor_groups": "Shelf-Editors",
            "viewer_groups": "Shelf-Users",
            "default_role": "deny",
            "auto_provision": True,
            "sync_roles": True,
        }
    )
    response = client.get("/login")
    assert response.status_code == 200
    assert 'data-testid="oidc-login"' in response.text
    assert "Sign in with Authentik" in response.text
