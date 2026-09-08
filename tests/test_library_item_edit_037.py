"""Item-local mutation security regressions for first-class libraries."""

from app.services import libraries
from tests.conftest import _insert_item


def _private_item(db, *, title: str, isbn: str, cover_path=None):
    library = libraries.create_library(db, f"Private {title}")
    item_id = _insert_item(
        db,
        title=title,
        isbn=isbn,
        cover_path=cover_path,
        _library_id=library["id"],
    )
    return library, item_id


def test_library_editor_membership_allows_global_viewer_to_edit_and_save(
    db, viewer_client, viewer_user
):
    library, item_id = _private_item(
        db, title="Viewer promoted in library", isbn="9780000012001"
    )
    libraries.set_membership(db, library["id"], viewer_user["id"], "editor")
    db.commit()

    edit = viewer_client.get(f"/item/{item_id}/edit")
    assert edit.status_code == 200

    saved = viewer_client.post(
        f"/api/items/{item_id}",
        data={"title": "Library editor saved this"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert db.execute(
        "SELECT title FROM items WHERE id = ?", (item_id,)
    ).fetchone()["title"] == "Library editor saved this"


def test_global_editor_without_membership_cannot_update_or_delete(
    db, editor_client
):
    _, item_id = _private_item(
        db, title="Hidden from global editor", isbn="9780000012002"
    )
    db.commit()

    update = editor_client.post(
        f"/api/items/{item_id}", data={"title": "Unauthorized rename"}
    )
    delete = editor_client.delete(f"/api/items/{item_id}")

    assert update.status_code == 404
    assert delete.status_code == 404
    assert db.execute(
        "SELECT title FROM items WHERE id = ?", (item_id,)
    ).fetchone()["title"] == "Hidden from global editor"


def test_admin_can_edit_item_without_library_membership(db, admin_client):
    _, item_id = _private_item(
        db, title="Admin-visible private item", isbn="9780000012003"
    )
    db.commit()

    response = admin_client.get(f"/item/{item_id}/edit")
    assert response.status_code == 200


def test_hidden_cover_mutation_is_denied(db, editor_client):
    _, item_id = _private_item(
        db,
        title="Private cover",
        isbn="9780000012004",
        cover_path="covers/private.jpg",
    )
    db.commit()

    response = editor_client.post(f"/api/items/{item_id}/cover-remove")
    assert response.status_code == 404
    assert db.execute(
        "SELECT cover_path FROM items WHERE id = ?", (item_id,)
    ).fetchone()["cover_path"] == "covers/private.jpg"


def test_hidden_tag_mutation_is_denied(db, editor_client):
    _, item_id = _private_item(
        db, title="Private tags", isbn="9780000012005"
    )
    db.commit()

    response = editor_client.post(
        f"/api/items/{item_id}/tags", data={"name": "Should not exist"}
    )
    assert response.status_code == 404
    assert not db.execute(
        "SELECT 1 FROM tags WHERE name = ?", ("Should not exist",)
    ).fetchone()


def test_related_media_search_and_link_do_not_cross_library_editor_boundary(
    db, editor_client
):
    visible = _insert_item(
        db, title="Editable visible item", isbn="9780000012006"
    )
    _, hidden = _private_item(
        db, title="Classified relation target", isbn="9780000012007"
    )
    db.commit()

    search = editor_client.get(
        f"/api/related-media/items/{visible}/search",
        params={"q": "Classified relation target"},
    )
    assert search.status_code == 200
    # The empty-state message echoes the query, so assert on the candidate's
    # hidden form value rather than its title text.
    assert f'name="other_item_id" value="{hidden}"' not in search.text

    link = editor_client.post(
        f"/api/related-media/items/{visible}/links",
        data={"other_item_id": hidden, "link_type": "related"},
    )
    assert link.status_code == 404
    assert not db.execute(
        "SELECT 1 FROM item_links WHERE item_a_id IN (?, ?) AND item_b_id IN (?, ?)",
        (visible, hidden, visible, hidden),
    ).fetchone()


def test_hidden_legacy_reading_status_cannot_be_changed_by_guessed_id(
    db, viewer_client
):
    _, item_id = _private_item(
        db, title="Private legacy status", isbn="9780000012008"
    )
    db.commit()

    response = viewer_client.post(
        f"/api/items/{item_id}/reading-status", data={"status": "read"}
    )
    assert response.status_code == 404
    assert db.execute(
        "SELECT reading_status FROM items WHERE id = ?", (item_id,)
    ).fetchone()["reading_status"] is None


def test_hidden_item_cannot_be_pushed_to_hardcover(db, editor_client):
    _, item_id = _private_item(
        db, title="Private Hardcover push", isbn="9780000012009"
    )
    db.commit()

    response = editor_client.post(f"/api/hardcover/push/{item_id}")
    assert response.status_code == 404
