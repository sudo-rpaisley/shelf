"""Authenticated throwaway-app palette journeys, without asset-tag injection.

The component's own intercepted-shell tests remain separate. These checks use
actual routes/CSP/assets and the initialized Browse controller, not pinned CI.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard, insert_item
from tests.e2e.test_responsive import _COLLAPSE


pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("width", (320, 768, 1366))
def test_authenticated_palette_search_uses_rendered_mixed_browse_without_typing_requests(
    authed_page, live_server, width,
):
    page = authed_page
    prefix = f"Palette integration {width}"
    item_id = insert_item(live_server["data_dir"], title=prefix, media_type="book")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(live_server["url"] + "/", wait_until="load")
    primary = urlsplit(page.locator('[data-nav-primary="browse"]').get_attribute("href"))
    page.locator("[data-command-palette-launcher]").click()
    dialog = page.locator("[data-command-palette-dialog]")
    expect(dialog).to_be_visible()
    query = dialog.get_by_role("combobox")
    requests = []

    def catalogue_request(request):
        path = urlsplit(request.url).path
        if path.startswith(("/api/", "/library", "/browse")):
            requests.append((request.method, path))

    page.on("request", catalogue_request)
    try:
        query.fill(prefix)
        result = dialog.locator('a[href*="q="]')
        expect(result).to_have_count(1)
        target = urlsplit(result.get_attribute("href"))
        assert target.path == primary.path
        assert parse_qs(target.query) == {**parse_qs(primary.query), "q": [prefix]}
        assert requests == [], "Typing must not query the catalogue or providers"
        assert page.evaluate(_COLLAPSE) == []
        result.click()
        page.wait_for_url(lambda url: urlsplit(str(url)).path == target.path
                          and parse_qs(urlsplit(str(url)).query).get("q") == [prefix])
        expect(page.get_by_test_id("library-page")).to_be_visible()
        expect(page.locator(f'.media-card[data-item-id="{item_id}"]')).to_have_count(1)
    finally:
        page.remove_listener("request", catalogue_request)
    assert_page_clean(page)


def test_palette_escape_precedes_the_real_initialized_browse_controller(authed_page, live_server):
    page = authed_page
    page.goto(live_server["url"] + "/browse", wait_until="load")
    root = page.locator('[x-data="browsePage"]')
    expect(root).to_have_count(1)
    page.keyboard.press("Escape")
    page.keyboard.press("e")
    assert root.evaluate("element => Alpine.$data(element).selectMode") is True
    launcher = page.locator("[data-command-palette-launcher]")
    launcher.click()
    dialog = page.locator("[data-command-palette-dialog]")
    expect(dialog).to_be_visible()
    dialog.get_by_role("combobox").fill("bs?")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(launcher).to_be_focused()
    assert root.evaluate("element => Alpine.$data(element).selectMode") is True
    page.keyboard.press("Escape")
    assert root.evaluate("element => Alpine.$data(element).selectMode") is False
    assert_page_clean(page)


def test_same_real_authenticated_session_keeps_native_navigation_with_javascript_disabled(
    authed_page, live_server, browser,
):
    # Copy the disposable session's real browser-login cookies in memory only.
    # This tests no-JS navigation, not a claim about the login form's fallback.
    context = browser.new_context(java_script_enabled=False, service_workers="block")
    context.add_cookies(authed_page.context.cookies())
    page = attach_page_guard(context.new_page())
    try:
        page.goto(live_server["url"] + "/", wait_until="load")
        expect(page.locator("[data-command-palette-dialog]")).to_be_hidden()
        expect(page.locator("[data-command-palette-launcher]")).to_be_hidden()
        fallback = page.locator("[data-command-palette-fallback]")
        fallback.locator("summary").click()
        expect(fallback.locator("p")).to_be_visible()
        primary = page.locator('[data-nav-primary="browse"]')
        href = primary.get_attribute("href")
        primary.click()
        page.wait_for_url(live_server["url"] + href)
        expect(page.get_by_test_id("library-page")).to_be_visible()
        assert_page_clean(page)
    finally:
        context.close()
