"""Actual keyboard activation of the account action and labelled shortcut dialog."""
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean
from tests.e2e.test_shortcuts import _VIOLATION_PROBE

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("activation", ["Enter", "Space"])
def test_account_shortcuts_keyboard_activation_and_all_close_paths(authed_page, live_server, activation):
    page = authed_page
    page.add_init_script(_VIOLATION_PROBE)
    page.goto(f"{live_server['url']}/browse")
    menu = page.get_by_test_id("account-menu-panel")
    action = menu.get_by_test_id("account-shortcuts-action")
    modal = page.locator("#shortcut-modal")
    expect(page.locator("#shortcut-help-trigger")).to_have_count(0)
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(modal).to_be_hidden()

    def open_with_keyboard():
        page.get_by_test_id("account-menu-button").click()
        expect(menu).to_be_visible()
        expect(action).to_have_attribute("role", "menuitem")
        expect(action.locator("span")).to_have_text("Keyboard shortcuts")
        expect(action.locator("kbd")).to_have_text("?")
        action.focus()
        expect(action).to_be_focused()
        page.keyboard.press(activation)
        expect(menu).to_be_hidden()
        expect(modal).to_be_visible()
        expect(modal).to_have_attribute("role", "dialog")
        expect(modal).to_have_attribute("aria-modal", "true")
        expect(modal).to_have_attribute("aria-labelledby", "shortcut-modal-title")
        expect(modal).to_have_accessible_name("Keyboard Shortcuts")

    open_with_keyboard()
    # A click INSIDE the card is not a backdrop dismissal.
    modal.get_by_role("heading", name="Keyboard Shortcuts", exact=True).click()
    expect(modal).to_be_visible()
    close = modal.get_by_role("button", name="Close keyboard shortcuts", exact=True)
    expect(close).to_have_attribute("id", "shortcut-modal-close")
    close.focus()
    page.keyboard.press(activation)
    expect(modal).to_be_hidden()

    open_with_keyboard()
    page.keyboard.press("Escape")
    expect(modal).to_be_hidden()

    open_with_keyboard()
    modal.click(position={"x": 5, "y": 5})
    expect(modal).to_be_hidden()
    assert page.evaluate("window.__cspViolations") == []
    assert_page_clean(page)
