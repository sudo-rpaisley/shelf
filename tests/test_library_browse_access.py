"""Security regressions for per-library Browse and HTMX search visibility."""

import pytest

from app.services import libraries
from tests.conftest import _insert_item


def _assigned_item(db, title: str, isbn: str, library_id: int, **fields) -> int:
    item_id = _insert_item(db, title=title, isbn=isbn, **fields)
    libraries.assign_item(db, item_id, library_id)
    return item_id


def test_browse_and_search_hide_inaccessible_library_items(viewer_client, viewer_user, db):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    visible_id = _assigned_item(db, "Visible Shelf Book", "9780000008805", 1)
    secret = libraries.create_library(db, "Private Library")
    secret_id = _assigned_item(db, "Hidden Shelf Book", "9780000008812", secret["id"])
    assert libraries.has_item_role(db, viewer_user, visible_id)
    assert not libraries.has_item_role(db, viewer_user, secret_id)
    db.commit()

    browse = viewer_client.get("/browse")
    assert browse.status_code == 200
    assert "Visible Shelf Book" in browse.text
    assert "Hidden Shelf Book" not in browse.text

    search = viewer_client.get("/api/search?q=Shelf+Book")
    assert search.status_code == 200
    assert "Visible Shelf Book" in search.text
    assert "Hidden Shelf Book" not in search.text


def test_filter_options_do_not_leak_hidden_catalogue_metadata(viewer_client, viewer_user, db):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    _assigned_item(
        db, "Public Series Book", "9780000008829", 1,
        series_name="Public Saga", language="en",
    )
    secret = libraries.create_library(db, "Secret")
    secret_id = _assigned_item(
        db, "Secret Series Book", "9780000008836", secret["id"],
        series_name="Secret Saga", language="zz-secret",
    )
    loc_id = db.execute("INSERT INTO locations (name) VALUES ('Secret Vault')").lastrowid
    db.execute("UPDATE items SET location_id = ? WHERE id = ?", (loc_id, secret_id))
    tag_id = db.execute("INSERT INTO tags (name) VALUES ('Classified Tag')").lastrowid
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (secret_id, tag_id))
    db.commit()

    html = viewer_client.get("/browse").text
    assert "Public Saga" in html
    assert "Secret Saga" not in html
    assert "Classified Tag" not in html
    assert "zz-secret" not in html
    assert "Secret Vault" not in html


def test_filter_counts_exclude_inaccessible_items(viewer_client, viewer_user, db):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    _assigned_item(db, "Visible Book", "9780000008843", 1, owned=1)
    secret = libraries.create_library(db, "Hidden Counts")
    _assigned_item(db, "Hidden Wishlist", "9780000008850", secret["id"], owned=0)
    db.commit()

    html = viewer_client.get("/browse").text
    assert "Hidden Wishlist" not in html
    assert "Wishlist (0)" in html or ">Wishlist<" not in html


def test_admin_retains_global_recovery_visibility(admin_client, db):
    secret = libraries.create_library(db, "Admin Recovery")
    _assigned_item(db, "Admin Can See This", "9780000008928", secret["id"])
    db.commit()
    response = admin_client.get("/browse")
    assert response.status_code == 200
    assert "Admin Can See This" in response.text


def test_access_predicate_is_bound_and_role_aware(viewer_user):
    sql, params = libraries.item_access_condition(viewer_user, item_alias="catalogue_item")
    assert "catalogue_item.id" in sql
    assert "lm.user_id = ?" in sql
    assert f"lm.user_id = {viewer_user['id']}" not in sql
    assert sql.count("?") == 1
    assert params == [viewer_user["id"]]

    editor_sql, editor_params = libraries.item_access_condition(
        viewer_user, item_alias="i", minimum_role="editor"
    )
    assert "('editor')" in editor_sql
    assert editor_sql.count("?") == 1
    assert editor_params == [viewer_user["id"]]


def test_access_predicate_rejects_unknown_roles_and_aliases(viewer_user):
    with pytest.raises(ValueError, match="Unknown library role"):
        libraries.item_access_condition(viewer_user, minimum_role="owner")
    with pytest.raises(ValueError, match="Invalid SQL item alias"):
        libraries.item_access_condition(viewer_user, item_alias="i; DROP TABLE items")
