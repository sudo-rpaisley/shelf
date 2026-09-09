"""Browser coverage for family scope persistence in the compact Collection toolbar."""

import re

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import insert_item

pytestmark = pytest.mark.e2e


def test_family_scope_survives_later_toolbar_requests(live_server, authed_page):
    manga_title = "Toolbar Family Manga 037"
    book_title = "Toolbar Family Book 037"
    insert_item(live_server["data_dir"], title=manga_title, media_type="manga")
    insert_item(live_server["data_dir"], title=book_title, media_type="book")

    authed_page.goto(f"{live_server['url']}/browse?media_family_filter=comics")
    authed_page.wait_for_load_state("networkidle")
    grid = authed_page.locator("#item-grid")
    expect(grid.get_by_role("link", name=re.compile(manga_title)).first).to_be_visible()
    expect(grid.get_by_text(book_title, exact=True)).to_have_count(0)

    # The family selector can live in the collapsed specialist panel and still
    # participates in the sort request through registry-derived hx-include.
    expect(authed_page.get_by_test_id("family-filter")).to_have_value("comics")
    authed_page.get_by_test_id("sort-control").select_option("title_asc")
    expect(grid.get_by_role("link", name=re.compile(manga_title)).first).to_be_visible()
    expect(grid.get_by_text(book_title, exact=True)).to_have_count(0)
    expect(authed_page.get_by_test_id("family-filter")).to_have_value("comics")


def test_family_filter_can_switch_collection_scope(live_server, authed_page):
    music_title = "Toolbar Family Vinyl 037"
    manga_title = "Toolbar Family Comic 037"
    insert_item(live_server["data_dir"], title=music_title, media_type="vinyl")
    insert_item(live_server["data_dir"], title=manga_title, media_type="comic")

    authed_page.goto(f"{live_server['url']}/browse")
    authed_page.wait_for_load_state("networkidle")
    authed_page.get_by_test_id("filters-toggle").click()
    family = authed_page.get_by_test_id("family-filter")
    expect(family).to_be_visible()
    family.select_option("music")

    grid = authed_page.locator("#item-grid")
    expect(grid.get_by_role("link", name=re.compile(music_title)).first).to_be_visible()
    expect(grid.get_by_text(manga_title, exact=True)).to_have_count(0)
