"""Digital Comic Browse grouping regressions.

Komga can add thousands of individual issues. Browse groups those issues by
series by default, but opening a series must reveal ordinary individual items.
"""

from app import browse_filters
from app.routers.items_common import SORT_OPTIONS
from app.services import browse_grouping
from tests.conftest import _insert_item


def _add_saga(db):
    issue3 = _insert_item(
        db,
        title="Saga Alpha #3",
        isbn="9780000000103",
        media_type="digital_comic",
        series_name="Saga Alpha",
        series_position=3.0,
        cover_path="covers/saga3.jpg",
        created_at="2026-01-03 00:00:00",
    )
    issue1 = _insert_item(
        db,
        title="Saga Alpha #1",
        isbn="9780000000101",
        media_type="digital_comic",
        series_name="Saga Alpha",
        series_position=1.0,
        cover_path="covers/saga1.jpg",
        created_at="2026-01-01 00:00:00",
    )
    issue2 = _insert_item(
        db,
        title="Saga Alpha #2",
        isbn="9780000000102",
        media_type="digital_comic",
        series_name="Saga Alpha",
        series_position=2.0,
        cover_path="covers/saga2.jpg",
        created_at="2026-01-02 00:00:00",
    )
    return issue1, issue2, issue3


def test_browse_groups_digital_comics_by_default_and_uses_earliest_cover(db, admin_client):
    issue1, issue2, issue3 = _add_saga(db)
    physical = _insert_item(
        db,
        title="Saga Alpha Physical",
        isbn="9780000000201",
        media_type="comic",
        series_name="Saga Alpha",
        series_position=1.0,
    )
    db.commit()

    html = admin_client.get("/browse").text

    # item_grid.html intentionally emits both the grid and list templates;
    # Alpine reveals one of them. The collapsed series must therefore appear
    # once as a card and once as a row, never as its three individual issues.
    assert html.count('data-series-group="Saga Alpha"') == 2
    assert "covers/saga1.jpg" in html
    assert "3 issues" in html
    assert f'data-item-id="{physical}"' in html
    assert f'data-item-id="{issue1}"' not in html
    assert f'data-item-id="{issue2}"' not in html
    assert f'data-item-id="{issue3}"' not in html


def test_series_drilldown_shows_individual_digital_issues(db, admin_client):
    issue1, issue2, issue3 = _add_saga(db)
    db.commit()

    html = admin_client.get(
        "/browse?media_type_filter=digital_comic&series=Saga%20Alpha"
    ).text

    assert 'data-series-group="Saga Alpha"' not in html
    for item_id in (issue1, issue2, issue3):
        assert f'data-item-id="{item_id}"' in html


def test_grouping_can_be_disabled_in_collection_settings(db, admin_client):
    issue1, issue2, issue3 = _add_saga(db)
    db.commit()

    response = admin_client.post(
        "/api/settings/display",
        data={
            "currency": "USD",
            "metadata_search_lang": "en",
            "browse_group_digital_comics_present": "1",
        },
    )
    assert response.status_code == 200

    html = admin_client.get("/browse").text
    assert 'data-series-group="Saga Alpha"' not in html
    for item_id in (issue1, issue2, issue3):
        assert f'data-item-id="{item_id}"' in html


def test_grouping_happens_before_pagination(db):
    for series_index, series_name in enumerate(("Alpha", "Beta", "Gamma"), start=1):
        for issue in (1, 2, 3):
            _insert_item(
                db,
                title=f"{series_name} #{issue}",
                isbn=f"97800000{series_index:02d}{issue:03d}",
                media_type="digital_comic",
                series_name=series_name,
                series_position=float(issue),
                cover_path=f"covers/{series_name.lower()}{issue}.jpg",
                created_at=f"2026-01-0{series_index} 00:00:0{issue}",
            )
    db.commit()

    values = browse_filters.values_from({})
    where, params = browse_filters.build_where(values)
    _, order_clause = SORT_OPTIONS["newest"]

    page1, raw_total, display_total = browse_grouping.fetch_page(
        db, where, params, order_clause, limit=2, offset=0, values=values
    )
    page2, raw_total2, display_total2 = browse_grouping.fetch_page(
        db, where, params, order_clause, limit=2, offset=2, values=values
    )

    assert raw_total == raw_total2 == 9
    assert display_total == display_total2 == 3
    assert len(page1) == 2
    assert len(page2) == 1
    names = [item["series_name"] for item in page1 + page2]
    assert sorted(names) == ["Alpha", "Beta", "Gamma"]
    assert len(set(names)) == 3


def test_api_search_uses_the_same_series_grouping(db, admin_client):
    _add_saga(db)
    db.commit()

    html = admin_client.get("/api/search?media_type_filter=digital_comic").text
    assert html.count('data-series-group="Saga Alpha"') == 2
    assert "covers/saga1.jpg" in html
