"""Library visibility regressions for Series, Collections, Locations and helpers."""

from app.services import holdings, libraries
from tests.conftest import _insert_item


def test_series_overview_and_detail_hide_inaccessible_members(db, viewer_client):
    private = libraries.create_library(db, "Private series")
    visible = _insert_item(
        db,
        title="Visible Volume",
        isbn="9780000012001",
        series_name="Mixed Saga",
        series_position=1,
    )
    _insert_item(
        db,
        title="Hidden Volume",
        isbn="9780000012002",
        series_name="Mixed Saga",
        series_position=2,
        _library_id=private["id"],
    )
    db.commit()

    overview = viewer_client.get("/series")
    assert overview.status_code == 200
    assert "Visible Volume" in overview.text
    assert "Hidden Volume" not in overview.text

    detail = viewer_client.get("/series/detail", params={"name": "Mixed Saga"})
    assert detail.status_code == 200
    assert "Visible Volume" in detail.text
    assert "Hidden Volume" not in detail.text
    assert str(visible) in detail.text


def test_hidden_only_series_behaves_as_not_found(db, viewer_client):
    private = libraries.create_library(db, "Hidden only series")
    _insert_item(
        db,
        title="Invisible Series Item",
        isbn="9780000012010",
        series_name="Secret Saga",
        series_position=1,
        _library_id=private["id"],
    )
    db.commit()

    response = viewer_client.get("/series/detail", params={"name": "Secret Saga"})
    assert response.status_code == 404
    assert "Invisible Series Item" not in response.text


def test_item_series_rows_do_not_reveal_hidden_siblings_or_series_names(
    db, viewer_client
):
    private = libraries.create_library(db, "Hidden series rows")
    visible = _insert_item(
        db,
        title="Visible Series Anchor",
        isbn="9780000012020",
        series_name="Public Sequence",
        series_position=1,
    )
    _insert_item(
        db,
        title="Hidden Public Sequence Item",
        isbn="9780000012021",
        series_name="Public Sequence",
        series_position=2,
        _library_id=private["id"],
    )
    _insert_item(
        db,
        title="Hidden Named Item",
        isbn="9780000012022",
        series_name="Secret Sequence Name",
        series_position=1,
        _library_id=private["id"],
    )
    db.commit()

    response = viewer_client.get(f"/api/items/{visible}/series-rows")
    assert response.status_code == 200
    assert "Hidden Public Sequence Item" not in response.text
    assert "Secret Sequence Name" not in response.text


def test_collection_card_counts_and_previews_are_accessible_only(db, viewer_client):
    private = libraries.create_library(db, "Hidden collection")
    visible = _insert_item(db, title="Visible Collection Item", isbn="9780000012030")
    hidden = _insert_item(
        db,
        title="Hidden Collection Item",
        isbn="9780000012031",
        _library_id=private["id"],
    )
    collection_id = db.execute(
        "INSERT INTO collections (name, description) VALUES ('Mixed Collection', 'Shared definition')"
    ).lastrowid
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (collection_id, visible),
    )
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (collection_id, hidden),
    )
    db.commit()

    response = viewer_client.get("/collections")
    assert response.status_code == 200
    assert "Mixed Collection" in response.text
    assert "Visible Collection Item" in response.text
    assert "Hidden Collection Item" not in response.text
    assert "1 item" in response.text


def test_location_detail_hides_inaccessible_physical_copies(
    db, viewer_client
):
    private = libraries.create_library(db, "Hidden shelf")
    holdings.ensure_foundation(db)
    root = db.execute(
        "SELECT id FROM location_nodes ORDER BY id LIMIT 1"
    ).fetchone()
    if root:
        location_id = root["id"]
    else:
        location_id = db.execute(
            "INSERT INTO location_nodes (name) VALUES ('Test Shelf')"
        ).lastrowid

    visible = _insert_item(db, title="Visible Shelf Copy", isbn="9780000012040")
    hidden = _insert_item(
        db,
        title="Hidden Shelf Copy",
        isbn="9780000012041",
        _library_id=private["id"],
    )
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
        "VALUES (?, 1, ?, 1)",
        (visible, location_id),
    )
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
        "VALUES (?, 1, ?, 1)",
        (hidden, location_id),
    )
    db.commit()

    response = viewer_client.get(f"/locations/{location_id}")
    assert response.status_code == 200
    assert "Visible Shelf Copy" in response.text
    assert "Hidden Shelf Copy" not in response.text


def test_attention_view_omits_hidden_incomplete_records(db, viewer_client):
    private = libraries.create_library(db, "Hidden attention")
    _insert_item(
        db,
        title="Visible Missing Cover",
        isbn="9780000012050",
        owned=1,
        cover_path=None,
    )
    _insert_item(
        db,
        title="Hidden Missing Cover",
        isbn="9780000012051",
        owned=1,
        cover_path=None,
        _library_id=private["id"],
    )
    db.commit()

    response = viewer_client.get("/attention?category=cover")
    assert response.status_code == 200
    assert "Visible Missing Cover" in response.text
    assert "Hidden Missing Cover" not in response.text


def test_hidden_music_item_helpers_do_not_expose_release(db, viewer_client):
    private = libraries.create_library(db, "Hidden music")
    item_id = _insert_item(
        db,
        title="Secret Pressing",
        isbn=None,
        media_type="vinyl",
        _library_id=private["id"],
    )
    db.commit()

    detail = viewer_client.get(f"/api/music/items/{item_id}/detail")
    assert detail.status_code == 200
    assert detail.text == ""

    edit = viewer_client.get(f"/music/item/{item_id}/edit", follow_redirects=False)
    assert edit.status_code == 303
    assert edit.headers["location"] == "/music"
