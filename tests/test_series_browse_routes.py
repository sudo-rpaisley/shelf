import re
from urllib.parse import quote

from app import browse_columns as bc
from tests.conftest import _insert_item


def _seed_series(db, name="The Example Saga"):
    first = _insert_item(
        db,
        title="Example One",
        isbn="9780000070012",
        series_name=name,
        series_position=1,
    )
    second = _insert_item(
        db,
        title="Example Two",
        isbn="9780000070029",
        series_name=name.swapcase(),
        series_position=2,
    )
    standalone = _insert_item(
        db,
        title="Standalone Example",
        isbn="9780000070036",
    )
    db.commit()
    return first, second, standalone


def test_browse_and_live_search_share_grouped_result_set(admin_client, db):
    first, second, standalone = _seed_series(db)

    browse = admin_client.get("/browse?sort=title_asc")
    search = admin_client.get("/api/search?sort=title_asc")

    assert browse.status_code == search.status_code == 200
    for html in (browse.text, search.text):
        assert html.count('data-testid="series-card"') == 1
        assert "The Example Saga" in html
        assert f'data-item-id="{standalone}"' in html
        assert f'data-item-id="{first}"' not in html
        assert f'data-item-id="{second}"' not in html
        # One collapsed series plus one standalone item.
        assert "(2)" in html


def test_grouped_search_filter_uses_only_matching_members(admin_client, db):
    book = _insert_item(
        db,
        title="Book Member",
        isbn="9780000070043",
        media_type="book",
        series_name="Mixed Formats",
        series_position=1,
    )
    _insert_item(
        db,
        title="Comic Member",
        isbn="9780000070050",
        media_type="comic",
        series_name="Mixed Formats",
        series_position=2,
    )
    db.commit()

    html = admin_client.get("/api/search?media_type_filter=book").text

    assert html.count('data-testid="series-card"') == 1
    assert f'data-series-member-ids="{book}"' in html
    assert "1 item" in html


def test_series_detail_uses_nocase_identity_and_excludes_trash(admin_client, db):
    first, second, _ = _seed_series(db)
    db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (second,))
    db.commit()

    response = admin_client.get("/series/the%20example%20saga")

    assert response.status_code == 200
    assert 'data-testid="series-detail-items"' in response.text
    assert "Example One" in response.text
    assert "Example Two" not in response.text
    assert response.text.count('data-testid="series-detail-item"') == 1
    assert f'/item/{first}?from=series' in response.text


def test_series_detail_round_trips_name_containing_slash(admin_client, db):
    name = "Alpha / Beta"
    _insert_item(
        db,
        title="Slash Volume",
        isbn="9780000070067",
        series_name=name,
        series_position=1,
    )
    db.commit()

    # Preserve slashes as percent-encoded data; the {name:path} route must still
    # receive the decoded human series name and match it case-insensitively.
    encoded = quote(name, safe="")
    response = admin_client.get(f"/series/{encoded}")

    assert response.status_code == 200
    assert "Alpha / Beta" in response.text
    assert "Slash Volume" in response.text


def test_missing_series_redirects_to_series_index(admin_client):
    response = admin_client.get("/series/does-not-exist", follow_redirects=False)

    assert response.status_code in (302, 303, 307, 308)
    assert response.headers["location"] == "/series"

def test_grouped_series_row_matches_browse_column_registry(admin_client, db):
    _seed_series(db)

    response = admin_client.get("/browse?view=list")

    assert response.status_code == 200
    match = re.search(
        r'<tr[^>]*data-testid="series-row"[^>]*>(.*?)</tr>',
        response.text,
        re.S,
    )
    assert match, "grouped series row missing from list view"
    row_cols = re.findall(r'data-col="([a-z_]+)"', match.group(1))
    assert tuple(row_cols) == bc.COLUMN_NAMES
