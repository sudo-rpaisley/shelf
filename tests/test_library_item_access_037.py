"""Direct item-read security regressions for first-class libraries."""

from app.services import libraries, media_groups, user_state
from tests.conftest import _insert_item


def _private_item(db, *, title: str, isbn: str, series_name=None, series_position=None):
    library = libraries.create_library(db, f"Private {title}")
    item_id = _insert_item(
        db,
        title=title,
        isbn=isbn,
        series_name=series_name,
        series_position=series_position,
        _library_id=library["id"],
    )
    return library, item_id


def test_guessed_hidden_item_and_personal_state_behave_as_missing(
    db, viewer_client, viewer_user
):
    _, hidden = _private_item(db, title="Hidden direct item", isbn="9780000011001")
    user_state.save_state(db, viewer_user["id"], hidden, personal_notes="must stay hidden")
    db.commit()

    detail = viewer_client.get(f"/item/{hidden}", follow_redirects=False)
    assert detail.status_code == 303
    assert detail.headers["location"] == "/browse"

    get_state = viewer_client.get(f"/api/items/{hidden}/personal-state")
    assert get_state.status_code == 404
    assert "must stay hidden" not in get_state.text

    post_state = viewer_client.post(
        f"/api/items/{hidden}/personal-state",
        data={"personal_notes": "write through guessed id"},
    )
    assert post_state.status_code == 404
    state = user_state.get_state(db, viewer_user["id"], hidden)
    assert state["personal_notes"] == "must stay hidden"


def test_visible_main_library_item_still_opens(db, viewer_client):
    item_id = _insert_item(db, title="Visible direct item", isbn="9780000011002")
    db.commit()
    response = viewer_client.get(f"/item/{item_id}")
    assert response.status_code == 200
    assert "Visible direct item" in response.text


def test_global_editor_without_library_membership_cannot_open_edit_page(
    db, editor_client
):
    _, hidden = _private_item(db, title="Not this editor's item", isbn="9780000011003")
    db.commit()
    response = editor_client.get(f"/item/{hidden}/edit", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/browse"


def test_hidden_format_link_is_not_rendered_on_visible_item(db, viewer_client):
    visible = _insert_item(db, title="Visible physical", isbn="9780000011010")
    _, hidden = _private_item(db, title="Hidden digital", isbn="9780000011011")
    media_groups.link_items(db, visible, hidden, link_type="format")
    db.commit()

    html = viewer_client.get(f"/item/{visible}").text
    assert "Visible physical" in html
    assert "Hidden digital" not in html


def test_hidden_related_item_cannot_bridge_two_visible_items(db, viewer_client):
    first = _insert_item(db, title="Visible Alpha", isbn="9780000011020")
    _, hidden = _private_item(db, title="Secret Bridge", isbn="9780000011021")
    third = _insert_item(db, title="Visible Omega", isbn="9780000011022")
    media_groups.link_items(db, first, hidden, link_type="related")
    media_groups.link_items(db, hidden, third, link_type="related")
    db.commit()

    panel = viewer_client.get(f"/api/related-media/items/{first}/panel")
    assert panel.status_code == 200
    assert "Secret Bridge" not in panel.text
    assert "Visible Omega" not in panel.text


def test_item_series_progress_counts_only_accessible_siblings(db, viewer_client):
    first = _insert_item(
        db,
        title="Series One",
        isbn="9780000011030",
        series_name="Permission Saga",
        series_position=1,
    )
    _private_item(
        db,
        title="Secret Series Two",
        isbn="9780000011031",
        series_name="Permission Saga",
        series_position=2,
    )
    _insert_item(
        db,
        title="Series Three",
        isbn="9780000011032",
        series_name="Permission Saga",
        series_position=3,
    )
    db.commit()

    html = viewer_client.get(f"/item/{first}").text
    assert "Secret Series Two" not in html
    assert "you own 2 of 1–3" in html
    assert "missing #2" in html


def test_item_tag_suggestions_do_not_reveal_hidden_library_tags(db, editor_client):
    visible = _insert_item(db, title="Tagged visible", isbn="9780000011040")
    _, hidden = _private_item(db, title="Tagged hidden", isbn="9780000011041")
    public_tag = db.execute("INSERT INTO tags (name) VALUES ('Public Tag')").lastrowid
    secret_tag = db.execute("INSERT INTO tags (name) VALUES ('Classified Tag')").lastrowid
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (visible, public_tag))
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (hidden, secret_tag))
    db.commit()

    html = editor_client.get(f"/item/{visible}").text
    assert "Public Tag" in html
    assert "Classified Tag" not in html
