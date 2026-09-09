from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1))


# Existing Shelf user management predates first-class library memberships.
# Keep that surface backwards-compatible: creating a viewer/editor grants the
# same role in Main Library, and changing the legacy global role keeps only
# that default membership aligned. Other library memberships are untouched.
replace_once(
    "app/routers/auth_routes.py",
    "from app.database import get_db\n",
    "from app.database import get_db\nfrom app.services import libraries\n",
)

replace_once(
    "app/routers/auth_routes.py",
    '''        with get_db() as db:\n            db.execute(\n                "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, ?)",\n                (username, hash_password(password), display_name, role),\n            )\n''',
    '''        with get_db() as db:\n            cursor = db.execute(\n                "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, ?)",\n                (username, hash_password(password), display_name, role),\n            )\n            if role in ("viewer", "editor"):\n                libraries.set_membership(\n                    db, libraries.DEFAULT_LIBRARY_ID, cursor.lastrowid, role\n                )\n''',
)

replace_once(
    "app/routers/auth_routes.py",
    '''        db.execute(\n            "UPDATE users SET role = ?, token_version = token_version + 1, updated_at = datetime('now') WHERE id = ?",\n            (role, user_id),\n        )\n''',
    '''        db.execute(\n            "UPDATE users SET role = ?, token_version = token_version + 1, updated_at = datetime('now') WHERE id = ?",\n            (role, user_id),\n        )\n        if role == "admin":\n            # Admin is the explicit global bypass; it needs no membership row.\n            libraries.remove_membership(db, libraries.DEFAULT_LIBRARY_ID, user_id)\n        else:\n            # Preserve the existing user-management contract for Main Library.\n            # Memberships in every other library remain independent.\n            libraries.set_membership(\n                db, libraries.DEFAULT_LIBRARY_ID, user_id, role\n            )\n''',
)

# Product-level regressions: these use the real admin API rather than fixture
# helpers so a future refactor cannot create unusable non-admin accounts again.
p = Path("tests/test_library_permissions.py")
test = p.read_text()
test += r'''


def test_new_viewer_and_editor_users_receive_main_library_membership(admin_client, db):
    for username, role in (("new-viewer", "viewer"), ("new-editor", "editor")):
        response = admin_client.post(
            "/api/users",
            data={
                "username": username,
                "display_name": username,
                "password": "password123",
                "role": role,
            },
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True
        user_id = db.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()["id"]
        assert libraries.membership_role(
            db,
            {"id": user_id, "role": role},
            libraries.DEFAULT_LIBRARY_ID,
        ) == role


def test_global_role_change_keeps_main_membership_compatible(admin_client, db):
    created = admin_client.post(
        "/api/users",
        data={
            "username": "role-change",
            "display_name": "Role Change",
            "password": "password123",
            "role": "viewer",
        },
    )
    assert created.json()["ok"] is True
    user_id = db.execute(
        "SELECT id FROM users WHERE username = 'role-change'"
    ).fetchone()["id"]

    promoted = admin_client.post(
        f"/api/users/{user_id}/role", data={"role": "editor"}
    )
    assert promoted.json()["ok"] is True
    assert libraries.membership_role(
        db, {"id": user_id, "role": "editor"}, libraries.DEFAULT_LIBRARY_ID
    ) == "editor"

    # A non-default membership remains independent of the legacy global role.
    other = libraries.create_library(db, "Role-independent Library")
    libraries.set_membership(db, other["id"], user_id, "viewer")
    db.commit()

    made_admin = admin_client.post(
        f"/api/users/{user_id}/role", data={"role": "admin"}
    )
    assert made_admin.json()["ok"] is True
    assert db.execute(
        "SELECT 1 FROM library_memberships WHERE library_id = ? AND user_id = ?",
        (libraries.DEFAULT_LIBRARY_ID, user_id),
    ).fetchone() is None
    assert db.execute(
        "SELECT role FROM library_memberships WHERE library_id = ? AND user_id = ?",
        (other["id"], user_id),
    ).fetchone()["role"] == "viewer"

    demoted = admin_client.post(
        f"/api/users/{user_id}/role", data={"role": "viewer"}
    )
    assert demoted.json()["ok"] is True
    assert libraries.membership_role(
        db, {"id": user_id, "role": "viewer"}, libraries.DEFAULT_LIBRARY_ID
    ) == "viewer"
    assert db.execute(
        "SELECT role FROM library_memberships WHERE library_id = ? AND user_id = ?",
        (other["id"], user_id),
    ).fetchone()["role"] == "viewer"
'''
p.write_text(test.rstrip() + "\n")
