"""Security regressions for library-scoped Browse/search projections."""

from app import browse_filters
from app.services import browse_grouping, libraries, user_state, user_state_browse
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


def test_hidden_linked_item_cannot_bridge_two_visible_media_groups(db, viewer_user):
    visible_a = _insert_item(
        db,
        title="Visible Format A",
        isbn="9780000008843",
        media_type="book",
    )
    hidden_library, hidden = _library_with_item(
        db,
        "Hidden Links",
        "Hidden Bridge",
        "9780000008850",
        media_type="ebook",
    )
    visible_c = _insert_item(
        db,
        title="Visible Format C",
        isbn="9780000008867",
        media_type="audiobook",
    )
    db.execute(
        "INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'same_work')",
        (visible_a, hidden),
    )
    db.execute(
        "INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'same_work')",
        (hidden, visible_c),
    )

    values = browse_filters.values_from({})
    where, params = browse_filters.build_where(values, user_id=viewer_user["id"])
    where, params = libraries.scope_where(where, params, viewer_user)
    visibility_sql, visibility_params = libraries.item_access_condition(viewer_user)

    items, raw_total, display_total = browse_grouping.fetch_page(
        db,
        where,
        params,
        "i.id ASC",
        limit=20,
        offset=0,
        values=values,
        visibility_sql=visibility_sql,
        visibility_params=visibility_params,
    )

    assert hidden_library["id"] not in libraries.accessible_library_ids(db, viewer_user)
    assert raw_total == 2
    assert display_total == 2
    assert {item["id"] for item in items} == {visible_a, visible_c}
    assert all(item["browse_media_group"] is False for item in items)


def test_group_metadata_uses_only_accessible_linked_members(db, viewer_user):
    visible_a = _insert_item(
        db,
        title="Canonical Visible Title",
        isbn="9780000008874",
        media_type="book",
    )
    visible_c = _insert_item(
        db,
        title="Visible Audio",
        isbn="9780000008881",
        media_type="audiobook",
    )
    hidden_library, hidden = _library_with_item(
        db,
        "Hidden Metadata",
        "Secret Linked Title",
        "9780000008898",
        media_type="ebook",
    )
    for other in (visible_c, hidden):
        db.execute(
            "INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'same_work')",
            (visible_a, other),
        )

    values = browse_filters.values_from({})
    where, params = browse_filters.build_where(values, user_id=viewer_user["id"])
    where, params = libraries.scope_where(where, params, viewer_user)
    visibility_sql, visibility_params = libraries.item_access_condition(viewer_user)

    items, raw_total, display_total = browse_grouping.fetch_page(
        db,
        where,
        params,
        "i.id ASC",
        limit=20,
        offset=0,
        values=values,
        visibility_sql=visibility_sql,
        visibility_params=visibility_params,
    )

    assert raw_total == 2
    assert display_total == 1
    assert len(items) == 1
    group = items[0]
    assert group["browse_media_group"] is True
    assert group["browse_media_count"] == 2
    assert group["browse_media_title"] == "Canonical Visible Title"
    assert "Secret Linked Title" not in str(group)
    assert hidden_library["id"] not in libraries.accessible_library_ids(db, viewer_user)


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
