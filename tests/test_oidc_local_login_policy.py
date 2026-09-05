"""Security and recovery coverage for OIDC local-login policy."""

from urllib.parse import unquote_plus

from app.auth import hash_password
from app.oidc_policy import get_local_login_policy


def _configure_oidc(db):
    for key, value in (
        ("oidc_enabled", "1"),
        ("oidc_issuer", "https://idp.example/application/o/shelf/"),
        ("oidc_client_id", "shelf-client"),
    ):
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    db.commit()


def _add_admin(db, username="recovery-admin"):
    db.execute(
        "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, 'admin')",
        (username, hash_password("recovery-password"), "Recovery Admin"),
    )
    db.commit()
    return db.execute(
        "SELECT id, username FROM users WHERE username = ?", (username,)
    ).fetchone()


def _enable_recovery_only(admin_client, db, username="recovery-admin"):
    _configure_oidc(db)
    recovery = _add_admin(db, username)
    response = admin_client.post(
        "/api/oidc/local-login-policy",
        data={
            "local_login_mode": "recovery_only",
            "break_glass_username": username,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?oidc_policy_saved=recovery_only"
    return recovery


def test_recovery_only_requires_enabled_configured_oidc(admin_client):
    response = admin_client.post(
        "/api/oidc/local-login-policy",
        data={"local_login_mode": "recovery_only", "break_glass_username": "admin"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Enable+and+configure+OIDC" in response.headers["location"]
    assert get_local_login_policy().recovery_only is False


def test_recovery_only_requires_local_admin(admin_client, editor_user, db):
    _configure_oidc(db)
    response = admin_client.post(
        "/api/oidc/local-login-policy",
        data={
            "local_login_mode": "recovery_only",
            "break_glass_username": editor_user["username"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "must+be+a+Shelf+administrator" in response.headers["location"]


def test_normal_login_page_hides_password_form_in_recovery_only_mode(admin_client, db):
    _enable_recovery_only(admin_client, db)
    admin_client.cookies.delete("access_token")

    response = admin_client.get("/login")
    assert response.status_code == 200
    assert 'data-testid="oidc-login"' in response.text
    assert 'data-testid="local-login-form"' not in response.text
    assert 'data-testid="recovery-login-link"' in response.text


def test_recovery_link_reveals_local_form_without_weakening_server_policy(admin_client, db):
    _enable_recovery_only(admin_client, db)
    admin_client.cookies.delete("access_token")

    response = admin_client.get("/login?local=1")
    assert response.status_code == 200
    assert 'data-testid="local-login-form"' in response.text
    assert "Recovery sign-in is restricted" in response.text


def test_only_selected_recovery_admin_can_use_local_password_login(admin_client, editor_user, db):
    _enable_recovery_only(admin_client, db)
    admin_client.cookies.delete("access_token")

    blocked = admin_client.post(
        "/login",
        data={"username": editor_user["username"], "password": "password123"},
        follow_redirects=False,
    )
    assert blocked.status_code == 401
    assert "Invalid username or password" in blocked.text
    assert "access_token" not in blocked.cookies

    allowed = admin_client.post(
        "/login",
        data={"username": "recovery-admin", "password": "recovery-password"},
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    assert allowed.headers["location"] == "/browse"
    assert "access_token" in allowed.cookies


def test_invalid_stored_recovery_account_fails_open_to_local_login(admin_client, db):
    _configure_oidc(db)
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('oidc_local_login_mode', 'recovery_only') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('oidc_break_glass_user_id', '999999') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    db.commit()

    policy = get_local_login_policy()
    assert policy.recovery_only is False

    admin_client.cookies.delete("access_token")
    page = admin_client.get("/login")
    assert 'data-testid="local-login-form"' in page.text


def test_break_glass_admin_cannot_be_demoted_or_deleted(admin_client, db):
    recovery = _enable_recovery_only(admin_client, db)

    demote = admin_client.post(
        f"/api/users/{recovery['id']}/role",
        data={"role": "viewer"},
    )
    assert demote.status_code == 200
    assert demote.json()["ok"] is False
    assert "break-glass" in demote.json()["message"]

    delete = admin_client.delete(f"/api/users/{recovery['id']}")
    assert delete.status_code == 200
    assert delete.json()["ok"] is False
    assert "break-glass" in delete.json()["message"]


def test_break_glass_admin_cannot_be_linked_to_oidc(admin_client, db):
    _enable_recovery_only(admin_client, db)
    response = admin_client.post(
        "/api/oidc/link-existing",
        data={"shelf_username": "recovery-admin", "oidc_subject": "recovery-sub"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "configured break-glass administrator" in unquote_plus(response.headers["location"])


def test_disabling_oidc_restores_normal_local_login(admin_client, db):
    _enable_recovery_only(admin_client, db)
    assert get_local_login_policy().recovery_only is True

    response = admin_client.post(
        "/api/oidc/settings",
        data={
            "oidc_provider_name": "OpenID Connect",
            "oidc_issuer": "https://idp.example/application/o/shelf/",
            "oidc_client_id": "shelf-client",
            "oidc_scopes": "openid profile email",
            "oidc_group_claim": "groups",
            "oidc_default_role": "deny",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert get_local_login_policy().recovery_only is False


def test_settings_exposes_recovery_policy_controls(admin_client, db):
    _configure_oidc(db)
    response = admin_client.get("/settings")
    assert response.status_code == 200
    assert 'data-testid="oidc-local-login-policy"' in response.text
    assert 'action="/api/oidc/local-login-policy"' in response.text
    assert "Shelf does not offer a mode with no local recovery path" in response.text
