"""Browser coverage for the rebuilt Settings workspace."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_settings_sidebar_switches_current_sections(live_server, authed_page):
    authed_page.goto(f"{live_server['url']}/settings")
    authed_page.wait_for_load_state("networkidle")

    nav = authed_page.get_by_test_id("settings-section-nav")
    expect(nav).to_be_visible()
    expect(authed_page.get_by_test_id("tab-library")).to_be_visible()

    authed_page.get_by_test_id("tab-integrations").click()
    expect(authed_page.locator("#romm-panel")).to_be_visible()
    expect(authed_page.locator("#komga-panel")).to_be_visible()

    authed_page.get_by_test_id("tab-users").click()
    expect(authed_page.get_by_text("Add User", exact=True)).to_be_visible()


def test_settings_remains_reachable_from_account_menu(live_server, authed_page):
    authed_page.goto(f"{live_server['url']}/browse")
    authed_page.wait_for_load_state("networkidle")
    authed_page.get_by_test_id("account-menu-button").click()
    authed_page.get_by_test_id("account-menu-settings").click()
    authed_page.wait_for_url(f"{live_server['url']}/settings")
    expect(authed_page.get_by_role("heading", name="Settings")).to_be_visible()
