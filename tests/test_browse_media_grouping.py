"""Browse regressions for linked-media stacks.

Related media stays as separate catalogue records in ``items`` but Browse
collapses each transitive ``item_links`` component into one display entry.
"""

from app import browse_filters
from app.routers.items_common import SORT_OPTIONS
from app.services import browse_grouping, media_groups
from tests.conftest import _insert_item


def _add_dune_stack(db):
    book = _insert_item(
        db,
        title="Dune",
        isbn="9780441172719",
        media_type="book",
        authors="Frank Herbert",
        cover_path="covers/dune-book.jpg",
        created_at="2026-01-01 00:00:00",
    )
    ebook = _insert_item(
        db,
        title="Dune",
        isbn="9780593099322",
        media_type="ebook",
        authors="Frank Herbert",
        created_at="2026-01-02 00:00:00",
    )
    film = _insert_item(
        db,
        title="Dune",
        isbn=None,
        media_type="dvd",
        created_at="2026-01-03 00:00:00",
    )
    game = _insert_item(
        db,
        title="Dune",
        isbn=None,
        media_type="video_game",
        platform="pc",
        created_at="2026-01-04 00:00:00",
    )

    # Same-work formats can be automatic; adaptations remain deliberate. The
    # connected component is transitive, so one chain is one Browse stack.
    media_groups.link_items(db, book, ebook, "format")
    media_groups.link_items(db, ebook, film, "related")
    media_groups.link_items(db, film, game, "related")
    return book, ebook, film, game


def test_fetch_page_collapses_transitive_cross_media_component(db):
    book, ebook, film, game = _add_dune_stack(db)
    db.commit()

    values = browse_filters.values_from({})
    where, params = browse_filters.build_where(values)
    _, order_clause = SORT_OPTIONS["newest"]

    items, raw_total, display_total = browse_grouping.fetch_page(
        db, where, params, order_clause, limit=60, offset=0, values=values
    )

    assert raw_total == 4
    assert display_total == 1
    assert len(items) == 1
    stack = items[0]
    # Sorting may choose a newer member as the query representative, but the
    # connected-component root provides stable display/navigation metadata.
    assert stack["id"] == game
    assert stack["browse_media_group"] is True
    assert stack["browse_media_root"] == book
    assert stack["browse_media_url"] == f"/item/{book}"
    assert stack["browse_media_title"] == "Dune"
    assert stack["browse_media_cover_path"] == "covers/dune-book.jpg"
    assert stack["browse_media_count"] == 4
    assert stack["browse_media_labels"] == [
        "Book",
        "eBook",
        "DVD / Blu-ray",
        "Video Game",
    ]


def test_browse_renders_one_stack_card_and_row_not_four_items(db, admin_client):
    book, ebook, film, game = _add_dune_stack(db)
    db.commit()

    html = admin_client.get("/browse").text

    # item_grid.html contains both Alpine grid and list templates, hence two
    # group markers: one card and one row.
    assert html.count(f'data-media-group="{book}"') == 2
    assert "4 linked" in html
    assert "Book · eBook · DVD / Blu-ray · Video Game" in html
    assert f'href="/item/{book}"' in html
    for item_id in (book, ebook, film, game):
        assert f'data-item-id="{item_id}"' not in html


def test_media_filter_keeps_stack_identity_and_shows_all_linked_formats(db):
    book, _, _, _ = _add_dune_stack(db)
    db.commit()

    values = browse_filters.values_from({"media_type_filter": "dvd"})
    where, params = browse_filters.build_where(values)
    _, order_clause = SORT_OPTIONS["newest"]
    items, raw_total, display_total = browse_grouping.fetch_page(
        db, where, params, order_clause, limit=60, offset=0, values=values
    )

    assert raw_total == 1
    assert display_total == 1
    assert items[0]["browse_media_root"] == book
    assert items[0]["browse_media_count"] == 4
    assert "DVD / Blu-ray" in items[0]["browse_media_labels"]
    assert "Video Game" in items[0]["browse_media_labels"]


def test_linked_media_grouping_can_be_disabled_without_unlinking_items(db, admin_client):
    book, ebook, film, game = _add_dune_stack(db)
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('browse_group_related_media', '0')"
    )
    db.commit()

    html = admin_client.get("/browse").text

    assert 'data-media-group=' not in html
    for item_id in (book, ebook, film, game):
        assert f'data-item-id="{item_id}"' in html
    assert set(media_groups.related_ids(db, book)) == {ebook, film, game}


def test_linked_media_grouping_happens_before_pagination(db):
    first = _add_dune_stack(db)[0]
    second_a = _insert_item(
        db, title="Example Work", isbn=None, media_type="book",
        created_at="2026-02-01 00:00:00",
    )
    second_b = _insert_item(
        db, title="Example Work", isbn=None, media_type="dvd",
        created_at="2026-02-02 00:00:00",
    )
    media_groups.link_items(db, second_a, second_b, "related")
    standalone = _insert_item(
        db, title="Standalone", isbn=None, media_type="book",
        created_at="2026-03-01 00:00:00",
    )
    db.commit()

    values = browse_filters.values_from({})
    where, params = browse_filters.build_where(values)
    _, order_clause = SORT_OPTIONS["newest"]

    page1, raw1, display1 = browse_grouping.fetch_page(
        db, where, params, order_clause, limit=2, offset=0, values=values
    )
    page2, raw2, display2 = browse_grouping.fetch_page(
        db, where, params, order_clause, limit=2, offset=2, values=values
    )

    assert raw1 == raw2 == 7
    assert display1 == display2 == 3
    assert len(page1) == 2
    assert len(page2) == 1
    keys = [
        ("media", item["browse_media_root"])
        if item["browse_media_group"] else ("item", item["id"])
        for item in page1 + page2
    ]
    assert len(set(keys)) == 3
    assert ("media", first) in keys
    assert ("media", second_a) in keys
    assert ("item", standalone) in keys


def test_api_pagination_keeps_media_stack_rendering(db, admin_client):
    book, _, _, _ = _add_dune_stack(db)
    _insert_item(
        db, title="Newer Standalone", isbn=None, media_type="book",
        created_at="2026-12-01 00:00:00",
    )
    db.commit()

    grid_page = admin_client.get("/api/search?page=2&per_page=1").text
    assert f'data-media-group="{book}"' in grid_page
    assert 'data-item-id=' not in grid_page

    list_page = admin_client.get("/api/search?page=2&per_page=1&view=list").text
    assert f'data-media-group="{book}"' in list_page
    assert 'data-item-id=' not in list_page
