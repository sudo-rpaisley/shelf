"""OIDC login/callback integration on Shelf 0.37."""

import time

from app import auth
from app.crypto import encrypt_value, get_encryption_key
from app.oidc_policy import LOCAL_LOGIN_RECOVERY_ONLY, save_local_login_policy
from app.services import oidc_login


def _set_oidc(db, **overrides):
    values = {
        "oidc_enabled": "1",
        "oidc_provider_name": "Authentik",
        "oidc_issuer": "https://id.example/application/o/shelf/",
        "oidc_client_id": "shelf-client",
        "oidc_scopes": "openid profile email",
        "oidc_group_claim": "groups",
        "oidc_required_group": "",
        "oidc_admin_groups": "Shelf-Admins",
        "oidc_editor_groups": "Shelf-Editors",
        "oidc_viewer_groups": "Shelf-Users",
        "oidc_default_role": "deny",
        "oidc_auto_provision": "1",
        "oidc_sync_roles": "1",
    }
    values.update(overrides)
    for key, value in values.items():
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    db.commit()


def test_login_config_reads_encrypted_client_secret(db):
    _set_oidc(db)
    encrypted = encrypt_value("super-secret", get_encryption_key())
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('oidc_client_secret', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (encrypted,),
    )
    db.commit()

    config = oidc_login.get_login_config()
    assert config.available
    assert config.provider_name == "Authentik"
    assert config.core.client_secret == "super-secret"


def test_oidc_client_secret_can_be_overridden_by_environment(monkeypatch, db):
    _set_oidc(db)
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "from-environment")
    assert oidc_login.get_login_config().core.client_secret == "from-environment"


def test_login_page_shows_provider_when_enabled(client, db, admin_user):
    _set_oidc(db)
    response = client.get("/login")
    assert response.status_code == 200
    assert 'data-testid="oidc-login"' in response.text
    assert "Sign in with Authentik" in response.text
    assert 'data-testid="local-login-form"' in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


def test_login_page_keeps_local_form_when_oidc_disabled(client, admin_user):
    response = client.get("/login")
    assert response.status_code == 200
    assert 'data-testid="oidc-login"' not in response.text
    assert 'data-testid="local-login-form"' in response.text


def test_oidc_initiation_reuses_login_route_and_sets_short_lived_cookie(
    monkeypatch, client, db, admin_user
):
    _set_oidc(db)

    async def fake_begin(request, config):
        assert request.url.path == "/login"
        assert config.available
        return "https://id.example/authorize?state=abc", {
            "state": "abc",
            "nonce": "nonce",
            "verifier": "verifier",
            "redirect_uri": "https://testserver/login",
            "issuer": config.core.issuer,
            "exp": time.time() + 600,
        }

    monkeypatch.setattr(oidc_login, "begin_login", fake_begin)
    response = client.get("/login?oidc=1", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://id.example/authorize")
    cookie = response.headers.get("set-cookie", "")
    assert "oidc_flow=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/login" in cookie
    assert response.headers["cache-control"] == "no-store"


def test_oidc_callback_issues_fixed_shelf_session(monkeypatch, client, db, admin_user):
    _set_oidc(db)

    async def fake_complete(request, config):
        assert request.query_params["code"] == "code-1"
        return {
            "id": admin_user["id"],
            "username": admin_user["username"],
            "role": admin_user["role"],
            "display_name": admin_user["display_name"],
            "token_version": 1,
        }

    monkeypatch.setattr(oidc_login, "complete_login", fake_complete)
    response = client.get(
        "/login?code=code-1&state=state-1",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    payload = auth.decode_token(response.cookies["access_token"])
    assert payload["authn"] == "oidc"
    assert payload["reauth"] <= payload["exp"]
    assert payload["exp"] == payload["reauth"]
    assert response.headers["cache-control"] == "no-store"


def test_provider_access_denial_is_safe_and_clears_flow_cookie(client, db, admin_user):
    _set_oidc(db)
    response = client.get("/login?error=access_denied", follow_redirects=False)
    assert response.status_code == 403
    assert "cancelled or denied" in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "oidc_flow=" in response.headers.get("set-cookie", "")


def test_recovery_only_hides_local_form_until_explicitly_requested(
    client, db, admin_user
):
    _set_oidc(db)
    save_local_login_policy(LOCAL_LOGIN_RECOVERY_ONLY, admin_user["username"])

    normal = client.get("/login")
    assert 'data-testid="local-login-form"' not in normal.text
    assert 'data-testid="recovery-login-link"' in normal.text

    recovery = client.get("/login?local=1")
    assert 'data-testid="local-login-form"' in recovery.text
    assert "Recovery sign in" in recovery.text


def test_recovery_only_blocks_other_local_accounts_after_password_check(
    client, db, admin_user, viewer_user
):
    _set_oidc(db)
    save_local_login_policy(LOCAL_LOGIN_RECOVERY_ONLY, admin_user["username"])

    blocked = client.post(
        "/login",
        data={"username": viewer_user["username"], "password": "password123"},
        follow_redirects=False,
    )
    assert blocked.status_code == 401
    assert "Invalid username or password" in blocked.text

    allowed = client.post(
        "/login",
        data={"username": admin_user["username"], "password": "password123"},
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    assert allowed.headers["location"] == "/"


def test_oidc_linked_account_cannot_use_local_password(client, db, viewer_user):
    _set_oidc(db)
    db.execute(
        "INSERT INTO user_identities (user_id, provider, issuer, subject) "
        "VALUES (?, 'oidc', ?, 'viewer-subject')",
        (viewer_user["id"], "https://id.example/application/o/shelf/"),
    )
    db.commit()

    response = client.post(
        "/login",
        data={"username": viewer_user["username"], "password": "password123"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "configured identity provider" in response.text


def test_wrong_password_does_not_reveal_oidc_account_linkage(client, db, viewer_user):
    _set_oidc(db)
    db.execute(
        "INSERT INTO user_identities (user_id, provider, issuer, subject) "
        "VALUES (?, 'oidc', ?, 'viewer-subject')",
        (viewer_user["id"], "https://id.example/application/o/shelf/"),
    )
    db.commit()

    response = client.post(
        "/login",
        data={"username": viewer_user["username"], "password": "wrong-password"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "Invalid username or password" in response.text
    assert "identity provider" not in response.text
