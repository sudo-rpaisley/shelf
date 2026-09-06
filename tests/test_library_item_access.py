"""Security regressions for direct and dashboard library access."""

from app.services import libraries, media_groups, user_state
from tests.conftest import _insert_item


def _private_library(db, name="Private"):
    return libraries.create_library(db, name)


def test_inaccessible_direct_item_and_personal_state_behave_as_missing(
    db, viewer_client
):
    private = _private_library(db)
    hidden = _insert_item(
        db,
        title="Hidden direct item",
        isbn="9780000011001",
        _library_id=private["id"],
    )
    db.commit()

    detail = viewer_client.get(f"/item/{hidden}", follow_redirects=False)
    assert detail.status_code == 303
    assert detail.headers["location"] == "/browse"

    personal = viewer_client.get(f"/api/items/{hidden}/personal-state")
    assert personal.status_code == 404
    assert "Hidden direct item" not in personal.text


def test_library_viewer_can_open_item_but_not_edit_it(db, viewer_client):
    item_id = _insert_item(db, title="Visible viewer item", isbn="9780000011002")
    db.commit()

    assert viewer_client.get(f"/item/{item_id}").status_code == 200
    edit = viewer_client.get(f"/item/{item_id}/edit", follow_redirects=False)
    assert edit.status_code == 303
    assert edit.headers["location"] == "/browse"


def test_global_viewer_with_library_editor_membership_can_open_edit_page(
    db, viewer_client, viewer_user
):
    library = _private_library(db, "Editable")
    libraries.set_membership(db, library["id"], viewer_user["id"], "editor")
    item_id = _insert_item(
        db,
        title="Library editable item",
        isbn="9780000011003",
        _library_id=library["id"],
    )
    db.commit()

    response = viewer_client.get(f"/item/{item_id}/edit", follow_redirects=False)
    assert response.status_code == 200
    assert "Library editable item" in response.text


def test_global_editor_without_library_membership_cannot_edit_item(
    db, editor_client
):
    private = _private_library(db, "No legacy ceiling")
    hidden = _insert_item(
        db,
        title="Not this editor's item",
        isbn="9780000011004",
        _library_id=private["id"],
    )
    db.commit()

    response = editor_client.get(f"/item/{hidden}/edit", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/browse"


def test_hidden_related_item_cannot_bridge_two_visible_items(
    db, viewer_client, viewer_user
):
    private = _private_library(db, "Hidden bridge")
    first = _insert_item(db, title="Visible Alpha", isbn="9780000011010")
    hidden = _insert_item(
        db,
        title="Secret Bridge",
        isbn="9780000011011",
        _library_id=private["id"],
    )
    third = _insert_item(db, title="Visible Omega", isbn="9780000011012")
    media_groups.link_items(db, first, hidden, "related")
    media_groups.link_items(db, hidden, third, "related")
    db.commit()

    condition, params = libraries.item_access_condition(viewer_user, item_alias="i")
    assert media_groups.related_ids(
        db,
        first,
        visibility_sql=condition,
        visibility_params=params,
    ) == []

    html = viewer_client.get(f"/item/{first}").text
    assert "Visible Alpha" in html
    assert "Secret Bridge" not in html
    assert "Visible Omega" not in html


def test_item_series_progress_counts_only_accessible_siblings(
    db, viewer_client
):
    private = _private_library(db, "Hidden series")
    first = _insert_item(
        db,
        title="Series One",
        isbn="9780000011020",
        series_name="Permission Saga",
        series_position=1,
    )
    _insert_item(
        db,
        title="Secret Series Two",
        isbn="9780000011021",
        series_name="Permission Saga",
        series_position=2,
        _library_id=private["id"],
    )
    _insert_item(
        db,
        title="Series Three",
        isbn="9780000011022",
        series_name="Permission Saga",
        series_position=3,
    )
    db.commit()

    html = viewer_client.get(f"/item/{first}").text
    assert "Secret Series Two" not in html
    assert "you own 2 of 1–3" in html
    assert "missing #2" in html


def test_revoked_hidden_state_does_not_appear_in_home_or_in_progress(
    db, viewer_client, viewer_user
):
    private = _private_library(db, "Revoked")
    visible = _insert_item(db, title="Recent Visible", isbn="9780000011030")
    hidden = _insert_item(
        db,
        title="Recent Hidden",
        isbn="9780000011031",
        _library_id=private["id"],
    )
    libraries.set_membership(db, private["id"], viewer_user["id"], "viewer")
    user_state.set_reading_status(db, viewer_user["id"], hidden, "reading")
    libraries.remove_membership(db, private["id"], viewer_user["id"])
    db.commit()

    home = viewer_client.get("/")
    assert home.status_code == 200
    assert "Recent Visible" in home.text
    assert "Recent Hidden" not in home.text

    progress = viewer_client.get("/api/home/personal-in-progress")
    assert progress.status_code == 200
    assert "Recent Hidden" not in progress.text


def test_stats_recent_and_personal_counts_ignore_inaccessible_library(
    db, viewer_client, viewer_user
):
    private = _private_library(db, "Stats hidden")
    visible = _insert_item(
        db,
        title="Stats Visible",
        isbn="9780000011040",
        manual_value=10.0,
    )
    hidden = _insert_item(
        db,
        title="Stats Hidden",
        isbn="9780000011041",
        manual_value=9999.0,
        _library_id=private["id"],
    )
    user_state.save_state(db, viewer_user["id"], visible, wishlist=1)
    user_state.save_state(db, viewer_user["id"], hidden, wishlist=1)
    db.execute(
        "INSERT INTO valuation_history (total_value, priced_count) VALUES (12345.0, 1)"
    )
    db.execute(
        "INSERT INTO valuation_history (total_value, priced_count) VALUES (23456.0, 1)"
    )
    db.commit()

    response = viewer_client.get("/stats")
    assert response.status_code == 200
    assert "Stats Visible" in response.text
    assert "Stats Hidden" not in response.text
    assert "9999" not in response.text
    assert "12345" not in response.text
    assert "23456" not in response.text