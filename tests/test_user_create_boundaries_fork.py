"""Regression coverage for user-creation request boundaries."""

from app.services import libraries


def test_create_user_rejects_invalid_role_without_creating_account(admin_client, db):
    response = admin_client.post(
        "/api/users",
        data={
            "username": "invalid-role-user",
            "display_name": "Invalid Role",
            "password": "correct-horse-battery-staple",
            "role": "owner",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "Invalid role"}
    assert db.execute(
        "SELECT id FROM users WHERE username = ?", ("invalid-role-user",)
    ).fetchone() is None


def test_create_user_seeds_matching_main_library_membership(admin_client, db):
    response = admin_client.post(
        "/api/users",
        data={
            "username": "new-main-viewer",
            "display_name": "New Main Viewer",
            "password": "correct-horse-battery-staple",
            "role": "viewer",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    user = db.execute(
        "SELECT id, role FROM users WHERE username = ?",
        ("new-main-viewer",),
    ).fetchone()
    assert user is not None
    assert user["role"] == "viewer"

    membership = db.execute(
        "SELECT role FROM library_memberships WHERE library_id = ? AND user_id = ?",
        (libraries.DEFAULT_LIBRARY_ID, user["id"]),
    ).fetchone()
    assert membership is not None
    assert membership["role"] == "viewer"
