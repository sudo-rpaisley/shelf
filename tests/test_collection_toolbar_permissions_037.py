"""Library-aware Browse selection and bulk Collection membership."""

import pytest

from app.services import collections, libraries
from tests.conftest import _insert_item


def _item(db, title, isbn, library_id=1):
    item_id = _insert_item(db, title=title, isbn=isbn, media_type="book")
    libraries.assign_item(db, item_id, library_id)
    return item_id


def _collection(db, library_id, name):
    return db.execute(
        "INSERT INTO collections (library_id, name) VALUES (?, ?)",
        (library_id, name),
    ).lastrowid


def test_library_viewer_does_not_get_select_mode(viewer_client, db):
    _item(db, "Read only", "9780000082015")
    db.commit()
    html = viewer_client.get("/browse").text
    assert 'data-testid="select-mode-toggle"' not in html


def test_mixed_memberships_only_mark_editor_items_selectable(viewer_client, viewer_user, db):
    editable = _item(db, "Editable", "9780000082022")
    other_library = libraries.create_library(db, "Read-only Library")["id"]
    readonly = _item(db, "Read only elsewhere", "9780000082039", other_library)
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    libraries.set_membership(db, other_library, viewer_user["id"], "viewer")
    db.commit()
    html = viewer_client.get("/browse").text
    assert 'data-testid="select-mode-toggle"' in html
    assert f'data-item-id="{editable}" data-can-edit="1"' in html
    assert f'data-item-id="{readonly}" data-can-edit="0"' in html


def test_library_editor_gets_collection_actions_not_admin_bulk_metadata(editor_client, db):
    _item(db, "Editor item", "9780000082046")
    _collection(db, 1, "Editor Picks")
    db.commit()
    html = editor_client.get("/browse").text
    assert 'data-testid="bulk-collection-select"' in html
    assert 'data-testid="bulk-type-control"' not in html


def test_admin_gets_metadata_and_collection_bulk_controls(admin_client, db):
    _item(db, "Admin item", "9780000082053")
    _collection(db, 1, "Admin Picks")
    db.commit()
    html = admin_client.get("/browse").text
    assert 'data-testid="bulk-collection-select"' in html
    assert 'data-testid="bulk-type-control"' in html


def test_bulk_collection_change_is_atomic_across_libraries(admin_user, db):
    first = _item(db, "Main item", "9780000082060")
    other_library = libraries.create_library(db, "Other Library")["id"]
    second = _item(db, "Other item", "9780000082077", other_library)
    collection_id = _collection(db, 1, "Main Picks")
    with pytest.raises(ValueError):
        collections.bulk_change_items(db, admin_user, collection_id, [first, second], "add")
    assert db.execute(
        "SELECT COUNT(*) AS c FROM collection_items WHERE collection_id = ?",
        (collection_id,),
    ).fetchone()["c"] == 0


def test_editor_can_bulk_add_and_remove_same_library(editor_user, db):
    first = _item(db, "One", "9780000082084")
    second = _item(db, "Two", "9780000082091")
    collection_id = _collection(db, 1, "Pair")
    assert collections.bulk_change_items(db, editor_user, collection_id, [first, second], "add") == 2
    assert collections.bulk_change_items(db, editor_user, collection_id, [first, second], "remove") == 2


def test_bulk_endpoint_rejects_mixed_library_selection_without_partial_write(admin_client, db):
    first = _item(db, "Endpoint main", "9780000082107")
    other_library = libraries.create_library(db, "Endpoint Other")["id"]
    second = _item(db, "Endpoint other", "9780000082114", other_library)
    collection_id = _collection(db, 1, "Endpoint Picks")
    db.commit()
    response = admin_client.post(
        f"/api/collections/{collection_id}/items/bulk",
        json={"item_ids": [first, second], "action": "add"},
    )
    assert response.status_code == 400
    assert db.execute(
        "SELECT COUNT(*) AS c FROM collection_items WHERE collection_id = ?",
        (collection_id,),
    ).fetchone()["c"] == 0
