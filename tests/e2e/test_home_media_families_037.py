"""Browser coverage for Home media-family entry points."""

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import insert_item

pytestmark = pytest.mark.e2e


def test_home_family_card_opens_filtered_collection(live_server, authed_page):
    manga_title = "Family Filter Manga 037"
    book_title = "Family Filter Book 037"
    music_title = "Family Filter Record 037"
    insert_item(live_server["data_dir"], title=manga_title, media_type="manga")
    insert_item(live_server["data_dir"], title=book_title, media_type="book")
    insert_item(live_server["data_dir"], title=music_title, media_type="vinyl")

    authed_page.goto(f"{live_server['url']}/")
    authed_page.wait_for_load_state("networkidle")

    families = authed_page.get_by_test_id("home-media-families")
    expect(families.locator('[data-media-family="comics"]')).to_be_visible()
    expect(families.locator('[data-media-family="periodicals"]')).to_be_visible()

    families.locator('[data-media-family="comics"]').click()
    authed_page.wait_for_url("**/browse?media_family_filter=comics")
    expect(authed_page.get_by_text(manga_title, exact=True)).to_be_visible()
    expect(authed_page.get_by_text(book_title, exact=True)).to_have_count(0)
    expect(authed_page.get_by_text(music_title, exact=True)).to_have_count(0)
