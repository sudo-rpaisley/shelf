"""Tests for first-class Shelf libraries and their permission boundary."""

from app.auth import hash_password
from app.services import libraries
from tests.conftest import _insert_item


def _user(db, username: str, role: str = "viewer") -> dict:
    cursor = db.execute(
        "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, ?)",
        (username, hash_password("password123"), username.title(), role),
    )
    return {
        "id": cursor.lastrowid,
        "username": username,
        "display_name": username.title(),
        "role": role,
    }


def _run_upgrade_snapshot(db) -> None:
    for version in (60, 61, 62):
        sql = next(
            sql
            for number, _description, sql in libraries._LIBRARY_MIGRATIONS
            if number == version
        )
        db.execute(sql)


def test_init_creates_default_main_library(db):
    rows = libraries.list_libraries(db)

    main = next(row for row in rows if row["id"] == libraries.DEFAULT_LIBRARY_ID)
    assert main["name"] == "Main Library"
    assert main["is_archived"] == 0


def test_upgrade_snapshot_preserves_existing_single_library_access(db):
    viewer = _user(db, "legacy-viewer", "viewer")
    editor = _user(db, "legacy-editor", "editor")
    admin = _user(db, "legacy-admin", "admin")
    item_id = _insert_item(db, title="Legacy Catalogue Item")

    # Re-run only the idempotent data migrations as if these rows existed at
    # upgrade time. Existing installations should wake up as one Main Library.
    _run_upgrade_snapshot(db)

    assert libraries.item_library_id(db, item_id) == libraries.DEFAULT_LIBRARY_ID
    assert libraries.membership_role(db, viewer, libraries.DEFAULT_LIBRARY_ID) == "viewer"
    assert libraries.membership_role(db, editor, libraries.DEFAULT_LIBRARY_ID) == "editor"
    # Admins need no membership row; their site role is the explicit bypass.
    assert libraries.membership_role(db, admin, libraries.DEFAULT_LIBRARY_ID) == "admin"
    assert db.execute(
        "SELECT 1 FROM library_memberships WHERE user_id = ?", (admin["id"],)
    ).fetchone() is None


def test_library_role_is_independent_of_legacy_global_viewer_role(db):
    user = _user(db, "mixed-access", "viewer")
    books = libraries.create_library(db, "Books")
    private = libraries.create_library(db, "Private Archive")

    libraries.set_membership(db, books["id"], user["id"], "editor")
    libraries.set_membership(db, private["id"], user["id"], "viewer")

    assert libraries.membership_role(db, user, books["id"]) == "editor"
    assert libraries.has_library_role(db, user, books["id"], "editor") is True
    assert libraries.has_library_role(db, user, private["id"], "viewer") is True
    assert libraries.has_library_role(db, user, private["id"], "editor") is False


def test_no_membership_means_no_library_or_item_access(db):
    user = _user(db, "no-access", "editor")
    private = libraries.create_library(db, "Restricted")
    item_id = _insert_item(db, title="Restricted Item", isbn="9780000007716")
    libraries.assign_item(db, item_id, private["id"])

    # The old global editor role must not leak access once library permissions
    # are authoritative.
    assert libraries.membership_role(db, user, private["id"]) is None
    assert libraries.has_library_role(db, user, private["id"], "viewer") is False
    assert libraries.has_item_role(db, user, item_id, "viewer") is False
    assert private["id"] not in libraries.accessible_library_ids(db, user)


def test_global_admin_can_recover_every_library_and_mapped_or_unmapped_item(db):
    admin = _user(db, "site-admin", "admin")
    first = libraries.create_library(db, "First")
    second = libraries.create_library(db, "Second")
    mapped = _insert_item(db, title="Mapped", isbn="9780000007723")
    unmapped = _insert_item(db, title="Unmapped", isbn="9780000007730")
    libraries.assign_item(db, mapped, first["id"])

    assert libraries.has_library_role(db, admin, first["id"], "editor") is True
    assert libraries.has_library_role(db, admin, second["id"], "viewer") is True
    assert libraries.has_item_role(db, admin, mapped, "editor") is True
    assert libraries.has_item_role(db, admin, unmapped, "viewer") is True
    accessible = libraries.accessible_library_ids(db, admin)
    assert first["id"] in accessible
    assert second["id"] in accessible


def test_assigning_item_to_another_library_moves_instead_of_duplicates(db):
    first = libraries.create_library(db, "Library A")
    second = libraries.create_library(db, "Library B")
    item_id = _insert_item(db, title="Movable Item", isbn="9780000007747")

    libraries.assign_item(db, item_id, first["id"])
    assert libraries.item_library_id(db, item_id) == first["id"]

    libraries.assign_item(db, item_id, second["id"])
    assert libraries.item_library_id(db, item_id) == second["id"]
    assert db.execute(
        "SELECT COUNT(*) AS c FROM library_items WHERE item_id = ?", (item_id,)
    ).fetchone()["c"] == 1


def test_removing_membership_revokes_access_without_touching_catalogue(db):
    user = _user(db, "revoked", "viewer")
    library = libraries.create_library(db, "Shared")
    item_id = _insert_item(db, title="Still Here", isbn="9780000007754")
    libraries.assign_item(db, item_id, library["id"])
    libraries.set_membership(db, library["id"], user["id"], "viewer")

    assert libraries.has_item_role(db, user, item_id) is True
    libraries.remove_membership(db, library["id"], user["id"])

    assert libraries.has_item_role(db, user, item_id) is False
    assert db.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()["title"] == "Still Here"
