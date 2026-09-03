"""Browser coverage for the Settings information-architecture refresh."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_settings_desktop_sidebar_and_integration_directory(live_server, authed_page):
    authed_page.goto(f"{live_server['url']}/settings")
    authed_page.wait_for_load_state("networkidle")

    expect(authed_page.get_by_test_id("tab-library")).to_be_visible()
    expect(authed_page.get_by_test_id("tab-integrations")).to_be_visible()
    expect(authed_page.get_by_test_id("tab-data")).to_be_visible()
    expect(authed_page.get_by_test_id("tab-users")).to_be_visible()

    authed_page.get_by_test_id("tab-integrations").click()
    expect(authed_page.get_by_test_id("integration-directory")).to_be_visible()
    expect(authed_page.locator('#google_books_api_key')).to_be_visible()

    directory = authed_page.get_by_test_id("integration-directory")
    expect(directory.locator('a[href="#abs_url"]')).to_be_visible()
    expect(directory.locator('a[href="#komga_url"]')).to_be_visible()
    expect(directory.locator('a[href="#romm_url"]')).to_be_visible()
    expect(directory.locator('a[href="#igdb_client_id"]')).to_be_visible()


def test_settings_mobile_switcher_keeps_sections_reachable(live_server, authed_page):
    authed_page.set_viewport_size({"width": 390, "height": 844})
    authed_page.goto(f"{live_server['url']}/settings")
    authed_page.wait_for_load_state("networkidle")

    # The same section controls become a compact horizontal switcher on mobile.
    expect(authed_page.get_by_test_id("tab-library")).to_be_visible()
    expect(authed_page.get_by_test_id("tab-integrations")).to_be_visible()
    authed_page.get_by_test_id("tab-integrations").click()
    expect(authed_page.get_by_test_id("integration-directory")).to_be_visible()

    # The settings shell itself must not introduce horizontal page overflow.
    has_overflow = authed_page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
    assert not has_overflow
