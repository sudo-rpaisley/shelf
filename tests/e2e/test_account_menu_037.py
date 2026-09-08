"""Browser coverage for the richer signed-in account dropdown."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def _open(page, base):
    page.goto(f"{base}/browse")
    page.wait_for_load_state("networkidle")
    panel = page.locator('[data-testid="account-menu-panel"]')
    expect(panel).to_be_hidden()
    page.locator('[data-testid="account-menu-button"]').click()
    expect(panel).to_be_visible()
    return panel


def test_account_menu_settings_and_profile_actions(live_server, authed_page):
    base = live_server["url"]
    panel = _open(authed_page, base)

    expect(panel.locator('[data-testid="account-menu-settings"]')).to_be_visible()
    expect(panel.locator('[data-testid="account-menu-logs"]')).to_be_visible()

    panel.locator('[data-testid="account-menu-profile"]').click()
    expect(authed_page.locator("h3", has_text="Account")).to_be_visible()
    expect(panel).to_be_hidden()


def test_account_menu_opens_shortcut_help(live_server, authed_page):
    panel = _open(authed_page, live_server["url"])

    panel.locator('[data-testid="account-menu-shortcuts"]').click()
    expect(panel).to_be_hidden()
    expect(authed_page.locator("#shortcut-modal")).to_be_visible()
