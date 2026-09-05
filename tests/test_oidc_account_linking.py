"""Regression coverage for explicitly linking existing Shelf accounts to OIDC."""

from urllib.parse import unquote_plus


def _configure_oidc(db):
    for key, value in (
        ("oidc_issuer", "https://idp.example/application/o/shelf/"),
        ("oidc_client_id", "shelf-client"),
        ("oidc_enabled", "1"),
        ("oidc_sync_roles", "1"),
    ):
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    db.commit()


def test_admin_can_explicitly_link_existing_user(admin_client, editor_user, db):
    _configure_oidc(db)
    before = db.execute(
        "SELECT token_version FROM users WHERE id = ?", (editor_user["id"],)
    ).fetchone()["token_version"]

    response = admin_client.post(
        "/api/oidc/link-existing",
        data={
            "shelf_username": editor_user["username"],
            "oidc_subject": "stable-subject-123",
            "oidc_email": "editor@example.com",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?oidc_linked=editor"
    identity = db.execute(
        "SELECT provider, issuer, subject, email FROM user_identities WHERE user_id = ?",
        (editor_user["id"],),
    ).fetchone()
    assert dict(identity) == {
        "provider": "oidc",
        "issuer": "https://idp.example/application/o/shelf/",
        "subject": "stable-subject-123",
        "email": "editor@example.com",
    }
    after = db.execute(
        "SELECT token_version FROM users WHERE id = ?", (editor_user["id"],)
    ).fetchone()["token_version"]
    assert after == before + 1


def test_linking_never_infers_identity_from_username_or_email(admin_client, editor_user, db):
    _configure_oidc(db)

    # Merely sharing profile-looking values creates no external identity.
    assert db.execute(
        "SELECT 1 FROM user_identities WHERE user_id = ?", (editor_user["id"],)
    ).fetchone() is None

    response = admin_client.post(
        "/api/oidc/link-existing",
        data={
            "shelf_username": editor_user["username"],
            "oidc_subject": "the-explicit-proof-key",
            "oidc_email": editor_user["username"] + "@example.com",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = db.execute(
        "SELECT subject FROM user_identities WHERE user_id = ?", (editor_user["id"],)
    ).fetchone()
    assert row["subject"] == "the-explicit-proof-key"


def test_oidc_subject_cannot_be_linked_to_two_users(admin_client, editor_user, viewer_user, db):
    _configure_oidc(db)

    first = admin_client.post(
        "/api/oidc/link-existing",
        data={"shelf_username": "editor", "oidc_subject": "same-subject"},
        follow_redirects=False,
    )
    assert first.status_code == 303

    second = admin_client.post(
        "/api/oidc/link-existing",
        data={"shelf_username": "viewer", "oidc_subject": "same-subject"},
        follow_redirects=False,
    )
    assert second.status_code == 303
    message = unquote_plus(second.headers["location"])
    assert "already linked to 'editor'" in message
    assert db.execute(
        "SELECT 1 FROM user_identities WHERE user_id = ?", (viewer_user["id"],)
    ).fetchone() is None


def test_last_local_admin_cannot_be_linked(admin_client, admin_user, db):
    _configure_oidc(db)

    response = admin_client.post(
        "/api/oidc/link-existing",
        data={"shelf_username": admin_user["username"], "oidc_subject": "admin-subject"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "last local administrator" in unquote_plus(response.headers["location"])
    assert db.execute(
        "SELECT 1 FROM user_identities WHERE user_id = ?", (admin_user["id"],)
    ).fetchone() is None


def test_admin_can_link_one_admin_when_another_local_admin_remains(admin_client, db):
    _configure_oidc(db)
    from app.auth import hash_password

    db.execute(
        "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, 'admin')",
        ("second-admin", hash_password("password123"), "Second Admin"),
    )
    db.commit()

    response = admin_client.post(
        "/api/oidc/link-existing",
        data={"shelf_username": "second-admin", "oidc_subject": "second-admin-sub"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?oidc_linked=second-admin"


def test_linking_requires_configured_oidc(admin_client, editor_user):
    response = admin_client.post(
        "/api/oidc/link-existing",
        data={"shelf_username": editor_user["username"], "oidc_subject": "subject"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Configure+the+OIDC+issuer" in response.headers["location"]


def test_settings_users_tab_exposes_explicit_linking_form(admin_client, db):
    _configure_oidc(db)
    response = admin_client.get("/settings")
    assert response.status_code == 200
    assert 'data-testid="oidc-account-linking"' in response.text
    assert 'action="/api/oidc/link-existing"' in response.text
    assert "never matches accounts automatically" in response.text
