"""Future fresh-synthetic caption navigation journeys; authored, never run here."""

import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from app.services import author_index
from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean
from tests.e2e.test_connected_library import _browser_page, _login, connected_library  # noqa: F401
from tests.e2e.test_cover_size_integration import _domain_snapshot
from tests.e2e.test_shared_cover_size_setting import _choose


pytestmark = pytest.mark.e2e


def _seed(server):
    with sqlite3.connect(server["data_dir"] / "shelf.db") as db:
        db.row_factory = sqlite3.Row
        ids = []
        for title, creator, series in (("Hits Out of Hell", "Meat Loaf", "Album Collection"),
                                        ("Bat Out of Hell", "Meat Loaf", "Album Collection"),
                                        ("A book about Meat Loaf", "Other Writer", "Other Collection")):
            item = db.execute("INSERT INTO items (title, authors, media_type, series_name) VALUES (?, ?, 'cd', ?)",
                              (title, creator, series)).lastrowid
            author_index.reindex_item(db, item, creator)
            ids.append(item)
        return ids


@pytest.mark.parametrize("width", [320, 1366])
@pytest.mark.parametrize("percentage", [50, 100, 200])
def test_caption_focus_artist_and_exact_series_navigation_preserve_frame_and_domain(connected_library, width, percentage):
    server, page = connected_library, connected_library["page"]
    first, second, other = _seed(server)
    page.set_viewport_size({"width": width, "height": 1100})
    page.goto(server["url"] + "/")
    _choose(page, percentage)
    domain = _domain_snapshot(server)
    stage = page.locator(f'#home-recent-row [data-item-id="{first}"] [data-caption-navigation]')
    cover = stage.locator("a[data-cover-link]")
    frame = stage.bounding_box()
    cover.focus()
    expect(stage.locator(".media-card__caption")).to_have_css("opacity", "1")
    cover.press("Tab")
    expect(stage.locator('[data-caption-link="item"]')).to_be_focused()
    page.keyboard.press("Tab")
    artist = stage.locator('[data-caption-link="creator"]')
    expect(artist).to_be_focused()
    expect(artist).to_have_text("Meat Loaf")
    assert stage.bounding_box()["width"] == pytest.approx(frame["width"], abs=1)
    assert stage.bounding_box()["height"] == pytest.approx(frame["height"], abs=1)
    with page.expect_navigation(wait_until="load"):
        artist.press("Enter")
    assert parse_qs(urlsplit(page.url).query)["author_filter"]
    grid = page.get_by_test_id("item-grid")
    expect(grid.locator(f'[data-item-id="{first}"]')).to_have_count(1)
    expect(grid.locator(f'[data-item-id="{second}"]')).to_have_count(1)
    expect(grid.locator(f'[data-item-id="{other}"]')).to_have_count(0)
    page.goto(server["url"] + "/")
    stage = page.locator(f'#home-recent-row [data-item-id="{first}"] [data-caption-navigation]')
    stage.hover()
    with page.expect_navigation(wait_until="load"):
        stage.locator('[data-caption-link="series"]').click()
    expect(page.get_by_test_id("series-card")).to_have_count(1)
    expect(page.get_by_test_id("series-card")).to_have_attribute("data-series-name", "Album Collection")
    assert _domain_snapshot(server) == domain
    assert_page_clean(page)


@pytest.mark.parametrize("javascript", [False, True])
def test_touch_names_are_visible_and_follow_native_links_on_the_first_tap(connected_library, browser, javascript):
    server = connected_library
    first, _, _ = _seed(server)
    with _browser_page(browser, has_touch=True, is_mobile=True, java_script_enabled=javascript,
                       viewport={"width": 375, "height": 1000}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        stage = page.locator(f'#home-recent-row [data-item-id="{first}"] [data-caption-navigation]')
        expect(stage.locator(".media-card__caption")).to_have_css("opacity", "1")
        creator = stage.locator('[data-caption-link="creator"]')
        assert creator.bounding_box()["height"] >= 44
        with page.expect_navigation(wait_until="load"):
            creator.tap()
        expect(page).to_have_url(re.compile(r"/browse\?author_filter=[1-9][0-9]*$"))


def test_browse_selection_keeps_metadata_clicks_local_then_allows_native_artist_navigation(connected_library):
    server, page = connected_library, connected_library["page"]
    first, _, _ = _seed(server)
    page.goto(server["url"] + "/browse")
    page.get_by_role("button", name="Select", exact=True).click()
    host = page.get_by_test_id("item-grid").locator(f'[data-item-id="{first}"]').locator('xpath=ancestor::*[@data-cover-size-card][1]')
    host.hover()
    url = page.url
    host.locator('[data-caption-link="creator"]').click()
    assert page.url == url
    expect(host.locator("[data-cover-select-action]")).to_have_attribute("aria-pressed", "true")
    host.locator("[data-cover-select-action]").click()
    expect(host.locator("[data-cover-select-action]")).to_have_attribute("aria-pressed", "false")
    assert_page_clean(page)
