"""Regression coverage for the rebuilt item-detail information hierarchy."""

from tests.conftest import _insert_item


def test_item_detail_renders_hero_about_details_and_copy_cards(editor_client, db):
    item_id = _insert_item(
        db,
        title="Card Layout Book",
        isbn="9789000090006",
        authors="Example Author",
        media_type="book",
        description="A useful synopsis.",
        location_name="Living Room / Bookcase 1 / Shelf 2",
        publisher="Example Press",
        publish_year=2026,
        page_count=320,
    )
    db.commit()

    html = editor_client.get(f"/item/{item_id}").text

    assert 'data-testid="item-detail-hero"' in html
    assert 'data-testid="item-about-card"' in html
    assert 'data-testid="item-details-card"' in html
    assert 'data-testid="item-copy-card"' in html
    assert 'data-testid="item-primary-actions"' in html
    assert "Card Layout Book" in html
    assert "A useful synopsis." in html
    assert "Living Room / Bookcase 1 / Shelf 2" in html
    assert "Example Press" in html


def test_layout_keeps_existing_record_and_admin_integration_contract(admin_client, db):
    item_id = _insert_item(
        db,
        title="Integration Layout Book",
        isbn="9789000090013",
        abs_id="li_layout",
        source="audiobookshelf",
    )
    db.commit()

    html = admin_client.get(f"/item/{item_id}").text

    assert 'data-testid="record-footer"' in html
    assert 'data-testid="integration-ids"' in html
    assert "li_layout" in html
    assert "via audiobookshelf" in html


def test_viewer_gets_information_hierarchy_without_edit_actions(viewer_client, db):
    item_id = _insert_item(
        db,
        title="Viewer Layout Book",
        isbn="9789000090020",
        media_type="book",
        description="Viewer-safe description.",
    )
    db.commit()

    html = viewer_client.get(f"/item/{item_id}").text

    assert 'data-testid="item-detail-hero"' in html
    assert 'data-testid="item-about-card"' in html
    assert 'data-testid="item-details-card"' in html
    assert 'data-testid="item-primary-actions"' not in html
    assert f'href="/item/{item_id}/edit' not in html
    assert "hx-delete=" not in html


def test_book_copy_card_preserves_personal_reading_status(viewer_client, db):
    item_id = _insert_item(
        db,
        title="Reading Layout Book",
        isbn="9789000090037",
        media_type="book",
    )
    db.commit()

    html = viewer_client.get(f"/item/{item_id}").text

    assert 'data-testid="item-copy-card"' in html
    assert ">Reading Status</p>" in html
    assert 'id="reading-status-section"' in html


def test_non_book_without_activity_omits_copy_card(viewer_client, db):
    item_id = _insert_item(
        db,
        title="Quiet Game",
        media_type="video_game",
        isbn=None,
    )
    db.commit()

    html = viewer_client.get(f"/item/{item_id}").text

    assert 'data-testid="item-detail-hero"' in html
    assert 'data-testid="item-copy-card"' not in html
    assert ">Reading Status</p>" not in html


def test_series_progress_remains_visible_in_hero_and_supporting_card(viewer_client, db):
    first = _insert_item(
        db,
        title="Layout Series One",
        isbn="9789000090044",
        series_name="Layout Saga",
        series_position=1,
        owned=1,
    )
    _insert_item(
        db,
        title="Layout Series Three",
        isbn="9789000090051",
        series_name="Layout Saga",
        series_position=3,
        owned=1,
    )
    db.commit()

    html = viewer_client.get(f"/item/{first}").text

    assert 'data-testid="series-progress"' in html
    assert 'data-testid="item-series-card"' in html
    assert "you own 2 of 1–3" in html
    assert "missing #2" in html
