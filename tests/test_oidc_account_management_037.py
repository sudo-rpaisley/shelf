"""Explicit OIDC account linking and managed-account protections."""

import time

from app import auth
from app.auth import hash_password
from app.database import get_setting
from app.oidc_policy import LOCAL_LOGIN_RECOVERY_ONLY, save_local_login_policy


def _set_oidc(db, *, sync_roles: bool = True):
    for key, value in (
        ("oidc_enabled", "1"),
        ("oidc_issuer", "https://id.example/application/o/shelf/"),
        ("oidc_client_id", "shelf-client"),
        ("oidc_sync_roles", "1" if sync_roles else "0"),
    ):
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    db.commit()


def _link_identity(db, user_id: int, subject: str = "subject-1"):
    db.execute(
        "INSERT INTO user_identities (user_id, provider, issuer, subject) "
        "VALUES (?, 'oidc', 'https://id.example/application/o/shelf/', ?)",
        (user_id, subject),
    )
    db.commit()


def _create_local_admin(db, username: str = "recovery2") -> int:
    cursor = db.execute(
        "INSERT INTO users (username, password, display_name, role) "
        "VALUES (?, ?, ?, 'admin')",
        (username, hash_password("local-password-123"), username),
    )
    db.commit()
    return int(cursor.lastrowid)


def _as_oidc_user(client, user):
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


def test_explicit_link_binds_stable_subject_and_invalidates_sessions(
    admin_client, db, viewer_user
):
    _set_oidc(db)
    before = db.execute(
        "SELECT token_version FROM users WHERE id = ?", (viewer_user["id"],)
    ).fetchone()["token_version"]

    response = admin_client.post(
        "/api/settings/oidc/accounts/link-existing",
        data={
            "shelf_username": viewer_user["username"],
            "oidc_subject": "stable-provider-subject",
            "oidc_email": "viewer@example.com",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?oidc_account_status=linked"

    identity = db.execute(
        "SELECT issuer, subject, email FROM user_identities WHERE user_id = ?",
        (viewer_user["id"],),
    ).fetchone()
    assert dict(identity) == {
        "issuer": "https://id.example/application/o/shelf/",
        "subject": "stable-provider-subject",
        "email": "viewer@example.com",
    }
    after = db.execute(
        "SELECT token_version FROM users WHERE id = ?", (viewer_user["id"],)
    ).fetchone()["token_version"]
    assert after == before + 1


def test_link_never_infers_identity_from_matching_email_or_username(
    admin_client, db, viewer_user
):
    _set_oidc(db)
    # Profile-like values alone do nothing: the route requires an explicit sub.
    response = admin_client.post(
        "/api/settings/oidc/accounts/link-existing",
        data={
            "shelf_username": viewer_user["username"],
            "oidc_subject": "",
            "oidc_email": f"{viewer_user['username']}@example.com",
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings?oidc_account_status=invalid"
    assert db.execute(
        "SELECT 1 FROM user_identities WHERE user_id = ?", (viewer_user["id"],)
    ).fetchone() is None


def test_duplicate_subject_and_already_linked_user_are_rejected(
    admin_client, db, viewer_user, editor_user
):
    _set_oidc(db)
    _link_identity(db, viewer_user["id"], "claimed-subject")

    same_user = admin_client.post(
        "/api/settings/oidc/accounts/link-existing",
        data={"shelf_username": viewer_user["username"], "oidc_subject": "other"},
        follow_redirects=False,
    )
    assert same_user.headers["location"] == "/settings?oidc_account_status=already_linked"

    same_subject = admin_client.post(
        "/api/settings/oidc/accounts/link-existing",
        data={"shelf_username": editor_user["username"], "oidc_subject": "claimed-subject"},
        follow_redirects=False,
    )
    assert same_subject.headers["location"] == "/settings?oidc_account_status=subject_used"


def test_last_local_admin_cannot_be_linked(admin_client, db, admin_user):
    _set_oidc(db)
    response = admin_client.post(
        "/api/settings/oidc/accounts/link-existing",
        data={"shelf_username": admin_user["username"], "oidc_subject": "admin-sub"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings?oidc_account_status=last_local_admin"


def test_break_glass_admin_cannot_be_linked(admin_client, db, admin_user):
    _set_oidc(db)
    _create_local_admin(db)
    save_local_login_policy(LOCAL_LOGIN_RECOVERY_ONLY, admin_user["username"])
    response = admin_client.post(
        "/api/settings/oidc/accounts/link-existing",
        data={"shelf_username": admin_user["username"], "oidc_subject": "admin-sub"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings?oidc_account_status=recovery_account"


def test_user_listing_marks_provider_role_management_and_recovery(
    admin_client, db, viewer_user, admin_user
):
    _set_oidc(db, sync_roles=True)
    _link_identity(db, viewer_user["id"])
    save_local_login_policy(LOCAL_LOGIN_RECOVERY_ONLY, admin_user["username"])

    response = admin_client.get("/api/users")
    assert response.status_code == 200
    users = {user["id"]: user for user in response.json()}
    assert users[viewer_user["id"]]["auth_provider"] == "OIDC"
    assert users[viewer_user["id"]]["role_managed"] is True
    assert users[admin_user["id"]]["auth_provider"] == "Local"
    assert users[admin_user["id"]]["break_glass"] is True


def test_synced_oidc_role_cannot_be_manually_changed(admin_client, db, viewer_user):
    _set_oidc(db, sync_roles=True)
    _link_identity(db, viewer_user["id"])
    response = admin_client.post(
        f"/api/users/{viewer_user['id']}/role",
        data={"role": "editor"},
    )
    assert response.json()["ok"] is False
    assert "managed by OIDC" in response.json()["message"]
    role = db.execute(
        "SELECT role FROM users WHERE id = ?", (viewer_user["id"],)
    ).fetchone()["role"]
    assert role == "viewer"


def test_oidc_role_can_be_local_when_sync_is_disabled(admin_client, db, viewer_user):
    _set_oidc(db, sync_roles=False)
    _link_identity(db, viewer_user["id"])
    response = admin_client.post(
        f"/api/users/{viewer_user['id']}/role",
        data={"role": "editor"},
    )
    assert response.json()["ok"] is True
    role = db.execute(
        "SELECT role FROM users WHERE id = ?", (viewer_user["id"],)
    ).fetchone()["role"]
    assert role == "editor"


def test_oidc_account_password_reset_is_blocked(admin_client, db, viewer_user):
    _set_oidc(db)
    _link_identity(db, viewer_user["id"])
    before = db.execute(
        "SELECT password FROM users WHERE id = ?", (viewer_user["id"],)
    ).fetchone()["password"]
    response = admin_client.post(
        f"/api/users/{viewer_user['id']}/password",
        data={"password": "replacement-password"},
    )
    assert response.json()["ok"] is False
    assert "do not use Shelf passwords" in response.json()["message"]
    after = db.execute(
        "SELECT password FROM users WHERE id = ?", (viewer_user["id"],)
    ).fetchone()["password"]
    assert after == before


def test_oidc_user_cannot_change_local_password_or_provider_display_name(
    client, db, viewer_user
):
    _set_oidc(db)
    _link_identity(db, viewer_user["id"])
    _as_oidc_user(client, viewer_user)

    password = client.post(
        "/api/account/password",
        data={"current_password": "password123", "new_password": "new-password-123"},
    )
    assert password.json()["ok"] is False
    assert "managed by OIDC" in password.json()["message"]

    display = client.post(
        "/api/account/display-name", data={"display_name": "Local override"}
    )
    assert display.json()["ok"] is False
    assert "identity provider" in display.json()["message"]


def test_break_glass_admin_cannot_be_demoted_or_deleted(
    admin_client, db, admin_user
):
    _set_oidc(db)
    _create_local_admin(db)
    save_local_login_policy(LOCAL_LOGIN_RECOVERY_ONLY, admin_user["username"])

    demote = admin_client.post(
        f"/api/users/{admin_user['id']}/role", data={"role": "viewer"}
    )
    assert demote.json()["ok"] is False
    assert "break-glass" in demote.json()["message"]

    # A second admin drives the request so the normal self-delete guard does
    # not mask the recovery-specific protection.
    other_id = db.execute(
        "SELECT id FROM users WHERE username = 'recovery2'"
    ).fetchone()["id"]
    other = db.execute(
        "SELECT id, username, display_name, role, token_version FROM users WHERE id = ?",
        (other_id,),
    ).fetchone()
    _as_oidc_user(admin_client, dict(other))
    delete = admin_client.delete(f"/api/users/{admin_user['id']}")
    assert delete.json()["ok"] is False
    assert "break-glass" in delete.json()["message"]


def test_account_link_status_uses_fixed_codes(admin_client):
    response = admin_client.get(
        "/settings?oidc_account_status=%3Cscript%3Ebad%3C/script%3E"
    )
    assert response.status_code == 200
    assert "<script>bad</script>" not in response.text
    assert "The account link request was invalid" not in response.text
