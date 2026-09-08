"""Administrative OIDC settings on Shelf 0.37."""

from app.crypto import encrypt_value, get_encryption_key
from app.database import get_setting
from app.oidc import OIDCError
from app.oidc_policy import (
    LOCAL_LOGIN_RECOVERY_ONLY,
    get_local_login_policy,
    get_oidc_session_hours,
    save_local_login_policy,
)


def _valid_form(**overrides):
    values = {
        "oidc_enabled": "1",
        "oidc_provider_name": "Authentik",
        "oidc_issuer": "https://id.example/application/o/shelf/",
        "oidc_client_id": "shelf-client",
        "oidc_client_secret": "client-secret",
        "oidc_scopes": "profile email profile",
        "oidc_group_claim": "groups",
        "oidc_required_group": "Shelf-Users",
        "oidc_admin_groups": "Shelf-Admins, Shelf-Admins",
        "oidc_editor_groups": "Shelf-Editors",
        "oidc_viewer_groups": "Shelf-Users\nShelf-Readers",
        "oidc_default_role": "deny",
        "oidc_auto_provision": "1",
        "oidc_sync_roles": "1",
    }
    values.update(overrides)
    return values


def _set_ready_oidc(db):
    for key, value in (
        ("oidc_enabled", "1"),
        ("oidc_issuer", "https://id.example/application/o/shelf/"),
        ("oidc_client_id", "shelf-client"),
    ):
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    db.commit()


def test_save_oidc_settings_encrypts_secret_and_normalises_policy(admin_client, db):
    response = admin_client.post(
        "/api/settings/oidc", data=_valid_form(), follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?oidc_status=saved"

    raw = db.execute(
        "SELECT value FROM settings WHERE key = 'oidc_client_secret'"
    ).fetchone()["value"]
    assert raw != "client-secret"
    assert "client-secret" not in raw
    assert get_setting(db, "oidc_client_secret") == "client-secret"
    assert get_setting(db, "oidc_scopes") == "openid profile email"
    assert get_setting(db, "oidc_admin_groups") == "Shelf-Admins"
    assert get_setting(db, "oidc_viewer_groups") == "Shelf-Users\nShelf-Readers"
    assert get_setting(db, "oidc_auto_provision") == "1"
    assert get_setting(db, "oidc_sync_roles") == "1"


def test_blank_secret_preserves_and_explicit_clear_removes(admin_client, db):
    first = _valid_form()
    admin_client.post("/api/settings/oidc", data=first, follow_redirects=False)
    original_raw = db.execute(
        "SELECT value FROM settings WHERE key = 'oidc_client_secret'"
    ).fetchone()["value"]

    blank = _valid_form(oidc_client_secret="", oidc_provider_name="Updated")
    admin_client.post("/api/settings/oidc", data=blank, follow_redirects=False)
    kept_raw = db.execute(
        "SELECT value FROM settings WHERE key = 'oidc_client_secret'"
    ).fetchone()["value"]
    assert kept_raw == original_raw
    assert get_setting(db, "oidc_client_secret") == "client-secret"

    clear = _valid_form(oidc_client_secret="", oidc_clear_client_secret="1")
    admin_client.post("/api/settings/oidc", data=clear, follow_redirects=False)
    assert get_setting(db, "oidc_client_secret") == ""


def test_invalid_configuration_is_rejected_before_any_write(admin_client, db, monkeypatch):
    monkeypatch.delenv("SHELF_OIDC_ALLOW_INSECURE_HTTP", raising=False)
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('oidc_provider_name', 'Original')"
    )
    db.commit()

    response = admin_client.post(
        "/api/settings/oidc",
        data=_valid_form(
            oidc_provider_name="Should not save",
            oidc_issuer="http://insecure.example/",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?oidc_status=invalid"
    assert get_setting(db, "oidc_provider_name") == "Original"
    assert db.execute(
        "SELECT 1 FROM settings WHERE key = 'oidc_enabled'"
    ).fetchone() is None


def test_enabled_configuration_requires_issuer_and_client_id(admin_client, db):
    response = admin_client.post(
        "/api/settings/oidc",
        data=_valid_form(oidc_issuer="", oidc_client_id=""),
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings?oidc_status=invalid"
    assert db.execute(
        "SELECT 1 FROM settings WHERE key = 'oidc_enabled'"
    ).fetchone() is None


def test_save_and_test_uses_strict_discovery(admin_client, monkeypatch):
    called = {}

    async def fake_discover(config):
        called["issuer"] = config.issuer
        called["client_id"] = config.client_id
        return {"issuer": config.issuer}

    monkeypatch.setattr("app.routers.oidc_settings.discover", fake_discover)
    form = _valid_form(oidc_action="test")
    response = admin_client.post(
        "/api/settings/oidc", data=form, follow_redirects=False
    )
    assert response.headers["location"] == "/settings?oidc_status=tested"
    assert called == {
        "issuer": "https://id.example/application/o/shelf/",
        "client_id": "shelf-client",
    }


def test_discovery_failure_uses_fixed_status_not_provider_text(admin_client, monkeypatch):
    async def fail_discover(config):
        raise OIDCError("provider said token=secret-value")

    monkeypatch.setattr("app.routers.oidc_settings.discover", fail_discover)
    response = admin_client.post(
        "/api/settings/oidc",
        data=_valid_form(oidc_action="test"),
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings?oidc_status=test_failed"
    assert "secret-value" not in response.headers["location"]


def test_disabling_oidc_restores_normal_local_login(admin_client, db, admin_user):
    _set_ready_oidc(db)
    save_local_login_policy(LOCAL_LOGIN_RECOVERY_ONLY, admin_user["username"])
    assert get_local_login_policy().recovery_only

    form = _valid_form()
    form.pop("oidc_enabled")
    response = admin_client.post(
        "/api/settings/oidc", data=form, follow_redirects=False
    )
    assert response.headers["location"] == "/settings?oidc_status=saved"
    assert get_setting(db, "oidc_enabled") == "0"
    assert not get_local_login_policy().recovery_only
    assert db.execute(
        "SELECT 1 FROM settings WHERE key = 'oidc_break_glass_user_id'"
    ).fetchone() is None


def test_recovery_policy_control_uses_local_admin(admin_client, db, admin_user, viewer_user):
    _set_ready_oidc(db)
    ok = admin_client.post(
        "/api/settings/oidc/local-login",
        data={
            "oidc_local_login_mode": "recovery_only",
            "oidc_break_glass_username": admin_user["username"],
        },
        follow_redirects=False,
    )
    assert ok.headers["location"] == "/settings?oidc_status=policy_saved"
    assert get_local_login_policy().break_glass_user_id == admin_user["id"]

    bad = admin_client.post(
        "/api/settings/oidc/local-login",
        data={
            "oidc_local_login_mode": "recovery_only",
            "oidc_break_glass_username": viewer_user["username"],
        },
        follow_redirects=False,
    )
    assert bad.headers["location"] == "/settings?oidc_status=policy_invalid"
    assert get_local_login_policy().break_glass_user_id == admin_user["id"]


def test_session_policy_control_validates_range(admin_client):
    good = admin_client.post(
        "/api/settings/oidc/session",
        data={"oidc_session_hours": "12"},
        follow_redirects=False,
    )
    assert good.headers["location"] == "/settings?oidc_status=session_saved"
    assert get_oidc_session_hours() == 12

    bad = admin_client.post(
        "/api/settings/oidc/session",
        data={"oidc_session_hours": "169"},
        follow_redirects=False,
    )
    assert bad.headers["location"] == "/settings?oidc_status=session_invalid"
    assert get_oidc_session_hours() == 12


def test_oidc_settings_page_is_write_only_for_client_secret(admin_client, db):
    encrypted = encrypt_value("never-render-this", get_encryption_key())
    for key, value in (
        ("oidc_client_secret", encrypted),
        ("oidc_provider_name", "Authentik"),
        ("oidc_issuer", "https://id.example/application/o/shelf/"),
        ("oidc_client_id", "shelf-client"),
    ):
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    db.commit()

    response = admin_client.get("/settings")
    assert response.status_code == 200
    assert 'data-testid="oidc-settings"' in response.text
    assert "OpenID Connect (OIDC)" in response.text
    assert "never-render-this" not in response.text
    assert "Saved — leave blank to keep" in response.text
    assert "login" in response.text  # callback URI is rendered by url_for


def test_oidc_status_banner_never_reflects_unknown_query_text(admin_client):
    response = admin_client.get(
        "/settings?oidc_status=%3Cscript%3Ebad%3C/script%3E"
    )
    assert response.status_code == 200
    assert "<script>bad</script>" not in response.text
    assert "OIDC settings were not saved" not in response.text


def test_non_admin_cannot_change_oidc_settings(editor_client, viewer_client):
    for client in (editor_client, viewer_client):
        response = client.post(
            "/api/settings/oidc", data=_valid_form(), follow_redirects=False
        )
        assert response.status_code == 403
