"""Registered HTTP/browser journeys with whole-page CSP and page-error guards."""

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard, insert_item


pytestmark = pytest.mark.e2e


def _collect_csp(page):
    # Install before the first navigation. The Python list survives documents;
    # collect every directive/source, not just shortcut-related violations.
    violations = []
    page.expose_function("__recordShortcutCsp", lambda violation: violations.append(violation))
    page.add_init_script("""
        document.addEventListener('securitypolicyviolation', function(event) {
            window.__recordShortcutCsp({
                url: document.URL, directive: event.violatedDirective,
                blockedURI: event.blockedURI, sourceFile: event.sourceFile,
                lineNumber: event.lineNumber
            });
        });
    """)
    return violations


def _login(page, live_server, credentials):
    page.goto(f"{live_server['url']}/login")
    page.fill("input[name=username]", credentials["username"])
    page.fill("input[name=password]", credentials["password"])
    with page.expect_navigation():
        page.click("button[type=submit]")
    expect(page).to_have_url(f"{live_server['url']}/")


def _click_backdrop(modal):
    box = modal.bounding_box()
    assert box is not None
    # The lower-right corner belongs to the full-screen backdrop, not its card.
    modal.click(position={"x": box["width"] - 5, "y": box["height"] - 5})


@pytest.mark.parametrize("locale,width,title,heading", (
    ("en-US", 1280, "Keyboard shortcuts (?)", "Keyboard Shortcuts"),
    ("it-IT", 375, "Scorciatoie da tastiera (?)", "Scorciatoie da tastiera"),
))
def test_real_help_controls_keyboard_typing_and_account_interactions(
    live_server, browser, setup_admin, locale, width, title, heading,
):
    item_id = insert_item(live_server["data_dir"], title=f"Shortcut help {locale}", isbn=None)
    context = browser.new_context(locale=locale, viewport={"width": width, "height": 900})
    try:
        page = attach_page_guard(context.new_page())
        violations = _collect_csp(page)
        _login(page, live_server, setup_admin)
        expect(page.locator("html")).to_have_attribute("lang", locale.split("-")[0])
        trigger = page.locator("[data-shortcut-help]")
        modal = page.locator("#shortcut-modal")
        close = modal.locator("[data-shortcut-help-close]")
        expect(trigger).to_be_visible()
        expect(trigger).to_have_attribute("title", title)
        expect(trigger).to_have_attribute("aria-controls", "shortcut-modal")
        expect(modal).to_be_hidden()

        trigger.click()
        expect(modal).to_be_visible()
        expect(modal.locator("h3")).to_have_text(heading)
        modal.locator("h3").click()
        expect(modal).to_be_visible()  # A bubbled inner click must not dismiss it.
        close.locator("svg").click()  # Include a click on the close button's child.
        expect(modal).to_be_hidden()
        trigger.click()
        expect(modal).to_be_visible()
        _click_backdrop(modal)
        expect(modal).to_be_hidden()

        trigger.focus()
        page.keyboard.press("?")
        expect(modal).to_be_visible()
        page.keyboard.press("?")
        expect(modal).to_be_hidden()
        page.keyboard.press("?")
        expect(modal).to_be_visible()
        page.keyboard.press("Escape")
        expect(modal).to_be_hidden()

        # This base has Account/Profile, not an Account shortcut-help entry.
        # Preserve its real Alpine actions and outside-click/Escape ownership.
        account = page.get_by_test_id("account-menu-panel")
        page.get_by_test_id("account-menu-button").click()
        expect(account).to_be_visible()
        trigger.click()
        expect(modal).to_be_visible()
        expect(account).to_be_hidden()
        page.keyboard.press("Escape")
        expect(modal).to_be_hidden()
        page.get_by_test_id("account-menu-button").click()
        page.get_by_test_id("account-menu-profile").click()
        account_dialog = page.locator('[x-show="showAccount"]')
        expect(account_dialog).to_be_visible()
        page.get_by_test_id("account-language").focus()
        page.keyboard.press("?")
        expect(modal).to_be_hidden()
        page.keyboard.press("Escape")
        expect(account_dialog).to_be_hidden()

        page.goto(f"{live_server['url']}/item/{item_id}")
        expect(page.get_by_role("heading", level=1)).to_have_text(f"Shortcut help {locale}")
        page.get_by_test_id("personal-library-editor").locator("summary").click()
        for selector in ("#personal-progress", "#personal-notes", "#personal-status"):
            control = page.locator(selector)
            control.focus()
            expect(control).to_be_focused()
            page.keyboard.press("?")
            expect(modal).to_be_hidden()
        trigger.click()
        expect(modal).to_be_visible()
        # Escape must still close help even when a typing control has focus.
        page.locator("#personal-notes").focus()
        page.keyboard.press("Escape")
        expect(modal).to_be_hidden()

        assert_page_clean(page)
        assert violations == [], f"Whole-page CSP violations: {violations}"
    finally:
        context.close()


def test_served_script_consumes_the_actual_aria_target_relationship(
    live_server, browser, setup_admin,
):
    context = browser.new_context()
    try:
        page = attach_page_guard(context.new_page())
        violations = _collect_csp(page)
        _login(page, live_server, setup_admin)
        # Take real authenticated HTTP markup and change only the existing
        # relationship before parsing. app.js is still served byte-for-byte.
        response = context.request.get(f"{live_server['url']}/")
        assert response.status == 200
        html = response.text()
        assert html.count('aria-controls="shortcut-modal"') == 1
        assert html.count('id="shortcut-modal"') == 1
        html = html.replace('aria-controls="shortcut-modal"', 'aria-controls="retargeted-help"')
        html = html.replace('id="shortcut-modal"', 'id="retargeted-help"')
        context.route(f"{live_server['url']}/", lambda route: route.fulfill(response=response, body=html))
        page.goto(f"{live_server['url']}/")
        expect(page.locator("#shortcut-modal")).to_have_count(0)
        trigger = page.locator("[data-shortcut-help]")
        modal = page.locator("#retargeted-help")
        expect(trigger).to_have_attribute("aria-controls", "retargeted-help")
        expect(modal).to_be_hidden()
        trigger.click()
        expect(modal).to_be_visible()
        modal.locator("[data-shortcut-help-close]").click()
        expect(modal).to_be_hidden()
        trigger.click()
        expect(modal).to_be_visible()
        modal.locator("h3").click()
        expect(modal).to_be_visible()
        _click_backdrop(modal)
        expect(modal).to_be_hidden()
        print("TARGET_RELATIONSHIP: real HTTP markup retargeted; actual served app.js opened/closed the new target")
        assert_page_clean(page)
        assert violations == [], f"Whole-page CSP violations: {violations}"
    finally:
        context.close()
