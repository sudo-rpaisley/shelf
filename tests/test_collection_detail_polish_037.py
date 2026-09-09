"""Focused guards for the polished library-scoped Collection detail page."""

from app.services import libraries
from tests.conftest import _insert_item


def _collection(db, library_id, name, description=""):
    return db.execute(
        "INSERT INTO collections (library_id, name, description) VALUES (?, ?, ?)",
        (library_id, name, description or None),
    ).lastrowid


def _item(db, title, isbn, library_id=1):
    item_id = _insert_item(db, title=title, isbn=isbn, media_type="book")
    libraries.assign_item(db, item_id, library_id)
    return item_id


def test_detail_shows_count_and_direct_browse_action(admin_client, db):
    collection_id = _collection(db, 1, "Research", "Useful references")
    first = _item(db, "One", "9780000083012")
    second = _item(db, "Two", "9780000083029")
    db.executemany(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        [(collection_id, first), (collection_id, second)],
    )
    db.commit()

    html = admin_client.get(f"/collections/{collection_id}").text
    assert 'data-testid="collection-item-count">2 items<' in html
    assert f'href="/browse?collection={collection_id}"' in html
    assert 'data-testid="browse-collection"' in html
    assert "Useful references" in html


def test_detail_management_follows_library_editor_role(viewer_client, viewer_user, db):
    collection_id = _collection(db, 1, "Shared")
    db.commit()
    readonly = viewer_client.get(f"/collections/{collection_id}").text
    assert 'data-testid="collection-manage"' not in readonly

    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    db.commit()
    editable = viewer_client.get(f"/collections/{collection_id}").text
    assert 'data-testid="collection-manage"' in editable
    assert f'action="/api/collections/{collection_id}"' in editable


def test_empty_detail_points_editors_back_to_browse(editor_client, db):
    collection_id = _collection(db, 1, "Empty")
    db.commit()
    html = editor_client.get(f"/collections/{collection_id}").text
    assert 'data-testid="collection-empty"' in html
    assert "This Collection is empty" in html
    assert 'href="/browse"' in html
