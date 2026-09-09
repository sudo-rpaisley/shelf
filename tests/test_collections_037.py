"""0.37 library-scoped Collections foundation."""

import sqlite3

from app.services import collections, libraries
from tests.conftest import _insert_item


def _new_library(db, name):
    return libraries.create_library(db, name)["id"]


def _collection(db, library_id, name):
    return db.execute(
        "INSERT INTO collections (library_id, name) VALUES (?, ?)", (library_id, name)
    ).lastrowid


def test_collection_schema_is_library_scoped(db):
    columns = {row["name"] for row in db.execute("PRAGMA table_info(collections)")}
    assert "library_id" in columns
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='collection_items'").fetchone()


def test_same_name_is_unique_per_library_not_globally(db):
    second = _new_library(db, "Second Library")
    db.execute("INSERT INTO collections (library_id, name) VALUES (1, 'Favourites')")
    db.execute("INSERT INTO collections (library_id, name) VALUES (?, 'Favourites')", (second,))
    with __import__('pytest').raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO collections (library_id, name) VALUES (1, 'favourites')")


def test_viewer_sees_only_collections_in_accessible_libraries(viewer_client, viewer_user, db):
    visible = _collection(db, 1, "Visible list")
    private_library = _new_library(db, "Private Library")
    hidden = _collection(db, private_library, "Hidden list")
    db.commit()
    page = viewer_client.get("/collections")
    assert page.status_code == 200
    assert "Visible list" in page.text
    assert "Hidden list" not in page.text
    assert viewer_client.get(f"/collections/{visible}").status_code == 200
    assert viewer_client.get(f"/collections/{hidden}", follow_redirects=False).status_code == 303


def test_editor_can_create_only_in_an_editable_library(editor_client, editor_user, db):
    private_library = _new_library(db, "Other Library")
    db.commit()
    ok = editor_client.post(
        "/api/collections", data={"library_id": 1, "name": "  Favourite   SF  ", "description": "Top picks"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    row = db.execute("SELECT * FROM collections WHERE library_id = 1").fetchone()
    assert row["name"] == "Favourite SF"
    denied = editor_client.post(
        "/api/collections", data={"library_id": private_library, "name": "Nope", "description": ""},
        follow_redirects=False,
    )
    assert denied.status_code == 403


def test_per_library_editor_membership_can_manage_even_with_global_viewer(viewer_client, viewer_user, db):
    library_id = _new_library(db, "Project Library")
    libraries.set_membership(db, library_id, viewer_user["id"], "editor")
    db.commit()
    response = viewer_client.post(
        "/api/collections", data={"library_id": library_id, "name": "Project", "description": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.execute("SELECT 1 FROM collections WHERE library_id = ?", (library_id,)).fetchone()


def test_collection_preview_and_detail_never_cross_library_boundary(admin_client, admin_user, db):
    second = _new_library(db, "Second Library")
    first_item = _insert_item(db, title="Main Item", isbn="9780000099105", media_type="book")
    second_item = _insert_item(db, title="Private Item", isbn="9780000099112", media_type="book")
    libraries.assign_item(db, first_item, 1)
    libraries.assign_item(db, second_item, second)
    cid = _collection(db, 1, "Main Picks")
    db.execute("INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)", (cid, first_item))
    # Corrupt/raw SQL can create a cross-library membership; read paths must
    # still refuse to expose it.
    db.execute("INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)", (cid, second_item))
    db.commit()
    detail = admin_client.get(f"/collections/{cid}").text
    assert "Main Item" in detail
    assert "Private Item" not in detail
    cards = admin_client.get("/collections").text
    assert "Main Picks" in cards


def test_service_rejects_cross_library_membership(admin_user, db):
    second = _new_library(db, "Second Library")
    item_id = _insert_item(db, title="Other", isbn="9780000099129", media_type="book")
    libraries.assign_item(db, item_id, second)
    cid = _collection(db, 1, "Main")
    db.commit()
    try:
        collections.add_item(db, admin_user, cid, item_id)
    except ValueError as exc:
        assert "same library" in str(exc)
    else:
        raise AssertionError("cross-library collection membership was accepted")


def test_delete_collection_cascades_membership_but_keeps_item(admin_client, admin_user, db):
    item_id = _insert_item(db, title="Keep Me", isbn="9780000099136", media_type="book")
    libraries.assign_item(db, item_id, 1)
    cid = _collection(db, 1, "Temporary")
    db.execute("INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)", (cid, item_id))
    db.commit()
    response = admin_client.delete(f"/api/collections/{cid}")
    assert response.status_code == 200
    assert db.execute("SELECT 1 FROM collection_items WHERE collection_id = ?", (cid,)).fetchone() is None
    assert db.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()["title"] == "Keep Me"
