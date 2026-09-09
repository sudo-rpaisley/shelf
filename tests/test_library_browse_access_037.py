"""Security regressions for library-scoped personal Browse/search projections."""

import pytest

from app import browse_filters
from app.services import libraries, user_state, user_state_browse
from tests.conftest import _insert_item


def _library_with_item(db, name: str, title: str, isbn: str, **item_fields):
    library = libraries.create_library(db, name)
    item_id = _insert_item(db, title=title, isbn=isbn, **item_fields)
    libraries.assign_item(db, item_id, library["id"])
    return library, item_id


def test_browse_and_htmx_search_hide_inaccessible_library_items(
    viewer_client, viewer_user, db
):
    visible_id = _insert_item(
        db,
        title="Visible Shelf Book",
        isbn="9780000008805",
    )
    secret_library, secret_id = _library_with_item(
        db,
        "Private Library",
        "Hidden Shelf Book",
        "9780000008812",
    )
    assert libraries.has_item_role(db, viewer_user, visible_id) is True
    assert libraries.has_item_role(db, viewer_user, secret_id) is False
    db.commit()

    browse = viewer_client.get("/browse")
    assert browse.status_code == 200
    assert "Visible Shelf Book" in browse.text
    assert "Hidden Shelf Book" not in browse.text
    assert secret_library["name"] not in browse.text

    search = viewer_client.get("/api/search?q=Shelf+Book")
    assert search.status_code == 200
    assert "Visible Shelf Book" in search.text
    assert "Hidden Shelf Book" not in search.text


def test_browse_filter_options_do_not_leak_hidden_series_tags_or_languages(
    viewer_client, db
):
    _insert_item(
        db,
        title="Public Series Book",
        isbn="9780000008829",
        series_name="Public Saga",
        language="en",
    )
    secret_library, secret_id = _library_with_item(
        db,
        "Secret",
        "Secret Series Book",
        "9780000008836",
        series_name="Secret Saga",
        language="zz-secret",
    )
    tag_id = db.execute("INSERT INTO tags (name) VALUES ('Classified Tag')").lastrowid
    db.execute(
        "INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)",
        (secret_id, tag_id),
    )
    db.commit()

    response = viewer_client.get("/browse")

    assert response.status_code == 200
    assert "Public Saga" in response.text
    assert "Secret Saga" not in response.text
    assert "Classified Tag" not in response.text
    assert "zz-secret" not in response.text
    assert secret_library["name"] not in response.text


def test_personal_filter_counts_exclude_state_on_revoked_library(db, viewer_user):
    visible_id = _insert_item(
        db,
        title="Visible Wanted",
        isbn="9780000008904",
    )
    secret_library, hidden_id = _library_with_item(
        db,
        "Former Access",
        "Hidden Wanted",
        "9780000008911",
    )
    libraries.set_membership(db, secret_library["id"], viewer_user["id"], "viewer")
    user_state.save_state(db, viewer_user["id"], visible_id, wishlist=1)
    user_state.save_state(db, viewer_user["id"], hidden_id, wishlist=1)
    libraries.remove_membership(db, secret_library["id"], viewer_user["id"])

    values = browse_filters.values_from({})
    where, params = browse_filters.build_where(values, user_id=viewer_user["id"])
    where, params = libraries.scope_where(where, params, viewer_user)
    total = db.execute(
        f"SELECT COUNT(*) AS c FROM items i {where}", params
    ).fetchone()["c"]
    counts = user_state_browse.filter_counts(
        db,
        values,
        total,
        viewer_user["id"],
        user=viewer_user,
    )

    assert total == 1
    assert counts["wishlist_count"] == 1


def test_admin_browse_keeps_global_recovery_access(admin_client, admin_user, db):
    secret_library, secret_id = _library_with_item(
        db,
        "Admin Recovery",
        "Admin Can See This",
        "9780000008928",
    )
    db.commit()

    assert libraries.has_item_role(db, admin_user, secret_id) is True
    assert secret_library["id"] in libraries.accessible_library_ids(db, admin_user)

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
        viewer_user,
        item_alias="i",
        minimum_role="editor",
    )
    assert "('editor')" in editor_sql
    assert editor_sql.count("?") == 1
    assert editor_params == [viewer_user["id"]]


def test_access_predicate_rejects_unknown_roles_and_aliases(viewer_user):
    with pytest.raises(ValueError, match="Unknown library role"):
        libraries.item_access_condition(viewer_user, minimum_role="owner")
    with pytest.raises(ValueError, match="Invalid SQL item alias"):
        libraries.item_access_condition(viewer_user, item_alias="i; DROP TABLE items")
