"""E2E: Browse's Status filter labels follow the Type filter.

Choosing Type = DVD swaps in the Watch verb set on the OOB-refreshed Status
select; picking the finished option then shows the "Status: Watched" chip,
built by browse.js's syncFilters() from the selected option's text. G127: the
Type select is folded away below `sm`, so this drives it only at desktop
width. G83: each select change is awaited by its /api/search response, armed
before the click, never a networkidle wait after one.
"""
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import insert_item

pytestmark = pytest.mark.e2e


def _reset_browse_storage(page, base_url):
    page.goto(f"{base_url}/browse")
    page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")


def test_status_filter_words_follow_type_and_chip_reads_watched(live_server, authed_page):
    insert_item(live_server["data_dir"], title="Status Label Disc", media_type="dvd", isbn=None)
    page = authed_page
    _reset_browse_storage(page, live_server["url"])
    page.goto(f"{live_server['url']}/browse")
    page.wait_for_load_state("networkidle")

    status_select = page.locator("select#reading-status-filter")
    page.get_by_test_id("filters-toggle").click()
    expect(page.get_by_test_id("filters-panel")).to_be_visible()

    with page.expect_response(lambda r: "/api/search" in r.url):  # G83
        page.locator("select#type-filter").select_option("dvd")

    want_option = status_select.locator("option[value='want_to_read']")
    doing_option = status_select.locator("option[value='reading']")
    done_option = status_select.locator("option[value='read']")
    expect(want_option).to_have_text("Want to Watch")
    expect(doing_option).to_have_text("Watching")
    expect(done_option).to_have_text("Watched")

    with page.expect_response(lambda r: "/api/search" in r.url):  # G83
        status_select.select_option("read")

    expect(page.get_by_text("Status: Watched", exact=True)).to_be_visible()
