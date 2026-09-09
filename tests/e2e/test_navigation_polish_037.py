"""Browser coverage for grouped desktop navigation."""

import pytest
from playwright.sync_api import expect

from tests.e2e.test_nav import _reset_nav_baseline

pytestmark = pytest.mark.e2e


def test_add_and_more_menus_expose_current_destinations(live_server, authed_page):
    _reset_nav_baseline(live_server, authed_page)
    authed_page.goto(f"{live_server['url']}/browse")
    authed_page.wait_for_load_state("networkidle")

    add = authed_page.get_by_test_id("nav-add-panel")
    expect(add).to_be_hidden()
    authed_page.get_by_test_id("nav-add-button").click()
    expect(add).to_be_visible()
    expect(add.locator('[data-nav-tab="scan"]')).to_be_visible()
    expect(add.locator('[data-nav-tab="shelf-fill"]')).to_be_visible()

    authed_page.keyboard.press("Escape")
    expect(add).to_be_hidden()

    more = authed_page.get_by_test_id("nav-more-panel")
    authed_page.get_by_test_id("nav-more-button").click()
    expect(more).to_be_visible()
    expect(more.locator('[data-nav-tab="music"]')).to_be_visible()
    expect(more.locator('[data-nav-tab="periodicals"]')).to_be_visible()
    expect(more.locator('[data-nav-tab="stats"]')).to_be_visible()
    expect(more.locator('[data-nav-tab="attention"]')).to_be_visible()


def test_shortcut_help_has_no_floating_button(live_server, authed_page):
    authed_page.goto(f"{live_server['url']}/browse")
    authed_page.wait_for_load_state("networkidle")
    expect(authed_page.locator('button[title="Keyboard shortcuts (?)"]')).to_have_count(0)
    authed_page.get_by_test_id("account-menu-button").click()
    authed_page.get_by_test_id("account-menu-shortcuts").click()
    expect(authed_page.locator("#shortcut-modal")).to_be_visible()
