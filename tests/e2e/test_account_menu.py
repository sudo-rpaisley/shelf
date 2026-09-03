"""E2E coverage for the signed-in account and navigation dropdowns."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_account_menu_opens_shortcuts_and_settings_navigates(live_server, authed_page):
    base = live_server["url"]
    authed_page.goto(f"{base}/browse")
    authed_page.wait_for_load_state("networkidle")

    panel = authed_page.locator('[data-testid="account-menu-panel"]')
    expect(panel).to_be_hidden()

    authed_page.locator('[data-testid="account-menu-button"]').click()
    expect(panel).to_be_visible()
    expect(panel.locator('[data-nav-tab="settings"]')).to_be_visible()

    panel.locator('[data-testid="account-shortcuts-action"]').click()
    expect(panel).to_be_hidden()
    shortcut_modal = authed_page.locator('#shortcut-modal')
    expect(shortcut_modal).to_be_visible()
    expect(shortcut_modal).to_have_attribute("role", "dialog")

    authed_page.get_by_role("button", name="Close keyboard shortcuts").click()
    expect(shortcut_modal).to_be_hidden()

    authed_page.locator('[data-testid="account-menu-button"]').click()
    panel.locator('[data-nav-tab="settings"]').click()
    authed_page.wait_for_url(f"{base}/settings")


def test_desktop_dropdowns_and_account_profile_modal(live_server, authed_page):
    authed_page.goto(f"{live_server['url']}/browse")
    authed_page.wait_for_load_state("networkidle")

    add_panel = authed_page.locator('[data-testid="nav-add-panel"]')
    more_panel = authed_page.locator('[data-testid="nav-more-panel"]')
    expect(add_panel).to_be_hidden()
    expect(more_panel).to_be_hidden()

    authed_page.locator('[data-testid="nav-add-button"]').click()
    expect(add_panel).to_be_visible()
    authed_page.keyboard.press("Escape")
    expect(add_panel).to_be_hidden()

    authed_page.locator('[data-testid="nav-more-button"]').click()
    expect(more_panel).to_be_visible()
    authed_page.keyboard.press("Escape")
    expect(more_panel).to_be_hidden()

    authed_page.locator('[data-testid="account-menu-button"]').click()
    authed_page.locator('[data-testid="account-profile-action"]').click()

    dialog_heading = authed_page.locator("h3", has_text="Account")
    expect(dialog_heading).to_be_visible()
    expect(authed_page.locator('[data-testid="account-menu-panel"]')).to_be_hidden()
