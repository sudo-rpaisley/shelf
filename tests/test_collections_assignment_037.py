"""Collections assignment, Browse filtering and merge integrity on 0.37."""

from app.services import collections, item_merge, libraries
from tests.conftest import _insert_item


def _new_collection(db, library_id, name):
    return db.execute(
        "INSERT INTO collections (library_id, name) VALUES (?, ?)",
        (library_id, name),
    ).lastrowid


def _item(db, title, isbn, library_id=1):
    item_id = _insert_item(db, title=title, isbn=isbn, media_type="book")
    libraries.assign_item(db, item_id, library_id)
    return item_id


def test_item_options_are_same_library_only_and_mark_membership(admin_user, db):
    other_library = libraries.create_library(db, "Other Library")["id"]
    item_id = _item(db, "Main", "9780000081018")
    selected = _new_collection(db, 1, "Selected")
    _new_collection(db, 1, "Available")
    _new_collection(db, other_library, "Other library collection")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (selected, item_id),
    )
    options, can_edit = collections.item_options(db, admin_user, item_id)
    assert can_edit is True
    assert {row["name"] for row in options} == {"Selected", "Available"}
    assert [row["name"] for row in options if row["selected"]] == ["Selected"]


def test_editor_can_add_and_remove_from_item_detail(editor_client, editor_user, db):
    item_id = _item(db, "Assigned", "9780000081025")
    collection_id = _new_collection(db, 1, "Course List")
    db.commit()

    before = editor_client.get(f"/item/{item_id}")
    assert before.status_code == 200
    assert 'data-testid="add-to-collection-form"' in before.text
    assert "Course List" in before.text

    added = editor_client.post(
        f"/api/items/{item_id}/collections",
        data={"collection_id": collection_id},
        follow_redirects=False,
    )
    assert added.status_code == 303
    assert db.execute(
        "SELECT 1 FROM collection_items WHERE collection_id = ? AND item_id = ?",
        (collection_id, item_id),
    ).fetchone()
    detail = editor_client.get(f"/item/{item_id}").text
    assert f'href="/collections/{collection_id}"' in detail
    assert f'hx-delete="/api/items/{item_id}/collections/{collection_id}"' in detail

    removed = editor_client.delete(
        f"/api/items/{item_id}/collections/{collection_id}"
    )
    assert removed.status_code == 200
    assert db.execute(
        "SELECT 1 FROM collection_items WHERE collection_id = ? AND item_id = ?",
        (collection_id, item_id),
    ).fetchone() is None


def test_global_viewer_with_library_editor_membership_gets_assignment_ui(
    viewer_client, viewer_user, db
):
    item_id = _item(db, "Project Item", "9780000081032")
    _new_collection(db, 1, "Project")
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    db.commit()
    html = viewer_client.get(f"/item/{item_id}").text
    assert 'data-testid="add-to-collection-form"' in html


def test_library_viewer_cannot_change_collection_membership(viewer_client, db):
    item_id = _item(db, "Read Only", "9780000081049")
    collection_id = _new_collection(db, 1, "Read Only Collection")
    db.commit()
    response = viewer_client.post(
        f"/api/items/{item_id}/collections",
        data={"collection_id": collection_id},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_cross_library_assignment_is_rejected_even_for_admin(admin_client, db):
    other_library = libraries.create_library(db, "Other Library")["id"]
    item_id = _item(db, "Main", "9780000081056")
    collection_id = _new_collection(db, other_library, "Other")
    db.commit()
    response = admin_client.post(
        f"/api/items/{item_id}/collections",
        data={"collection_id": collection_id},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert db.execute(
        "SELECT 1 FROM collection_items WHERE collection_id = ? AND item_id = ?",
        (collection_id, item_id),
    ).fetchone() is None


def test_browse_collection_filter_and_htmx_keep_membership_scope(admin_client, db):
    picked = _item(db, "Picked Item", "9780000081063")
    other = _item(db, "Other Item", "9780000081070")
    collection_id = _new_collection(db, 1, "Shortlist")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (collection_id, picked),
    )
    db.commit()

    first = admin_client.get(f"/browse?collection={collection_id}").text
    assert "Picked Item" in first
    assert "Other Item" not in first
    assert f'<option value="{collection_id}" selected>Shortlist — Main Library</option>' in first

    fragment = admin_client.get(
        f"/api/search?collection={collection_id}&sort=title_asc"
    ).text
    assert "Picked Item" in fragment
    assert "Other Item" not in fragment


def test_inaccessible_collection_id_does_not_bypass_library_scope(
    viewer_client, viewer_user, db
):
    private_library = libraries.create_library(db, "Private Library")["id"]
    private_item = _item(db, "Private Pick", "9780000081087", private_library)
    collection_id = _new_collection(db, private_library, "Private Collection")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (collection_id, private_item),
    )
    db.commit()
    html = viewer_client.get(f"/browse?collection={collection_id}").text
    assert "Private Pick" not in html
    assert "Private Collection" not in html


def test_merge_reparents_collection_membership_and_collapses_duplicate(admin_user, db):
    keep_id = _item(db, "Keep", "9780000081094")
    other_id = _item(db, "Merge", "9780000081100")
    collection_id = _new_collection(db, 1, "Merged Picks")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (collection_id, keep_id),
    )
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (collection_id, other_id),
    )
    db.commit()

    item_merge.reparent_children(db, keep_id, other_id)
    rows = db.execute(
        "SELECT item_id FROM collection_items WHERE collection_id = ?",
        (collection_id,),
    ).fetchall()
    assert [row["item_id"] for row in rows] == [keep_id]
