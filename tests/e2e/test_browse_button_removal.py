"""Future native menu/shortcut journeys on fresh synthetic servers; not executed."""

from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean
from tests.e2e.test_connected_library import _browser_page, _login, connected_library
from tests.e2e.test_cover_size_integration import _domain_snapshot


pytestmark = pytest.mark.e2e
LIBRARY = "/library?reset=1&view=items&sort=recent"


def _open_media(page):
    shell = page.locator("details[data-nav-shell]")
    if shell.get_attribute("open") is None:
        page.get_by_test_id("nav-menu-button").filter(visible=True).click()
    media = page.locator("details[data-nav-browse]")
    summary = media.locator(":scope > summary")
    summary.focus()
    if media.get_attribute("open") is None:
        summary.press("Enter")
    expect(media).to_have_attribute("open", "")
    return media, summary


@pytest.mark.parametrize("width", (320, 1280))
@pytest.mark.parametrize("javascript", (False, True))
def test_no_duplicate_button_and_native_library_navigation_with_or_without_js(connected_library, browser, width, javascript):
    server = connected_library
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": width, "height": 900}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        before = _domain_snapshot(server)
        requests = []

        def observed(request):
            if not urlsplit(request.url).path.startswith(("/static/", "/covers/")):
                requests.append((request.method, request.url))

        page.on("request", observed)
        try:
            nav = page.locator("[data-media-navigation]")
            expect(nav.locator('[data-nav-group="manage"],[data-nav-management]')).to_have_count(0)
            expect(nav.locator('a[data-nav-tab="browse"],a[href="/browse"]')).to_have_count(0)
            expect(nav.locator("a.media-navigation__primary")).to_have_count(0)
            expect(nav.locator('[data-nav-primary="browse"]')).to_have_count(1)
            media, summary = _open_media(page)
            library = media.locator('a[data-nav-tab="library"]')
            expect(library).to_have_attribute("data-nav-primary", "browse")
            expect(library).to_have_attribute("href", LIBRARY)
            summary.press("Tab")
            expect(library).to_be_focused()
            expect(library).to_be_visible()
            main = nav.locator(".media-navigation__main").bounding_box()
            tree = page.get_by_test_id("nav-menu-panel").bounding_box()
            assert main["x"] <= tree["x"] + 1 and tree["x"] + tree["width"] <= main["x"] + main["width"] + 1
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
            assert requests == [] and _domain_snapshot(server) == before
            with page.expect_navigation(wait_until="load") as navigation:
                library.press("Enter")
            assert navigation.value.status == 200
            expect(page).to_have_url(server["url"] + LIBRARY)
            assert requests == [("GET", server["url"] + LIBRARY)]
            assert _domain_snapshot(server) == before
            assert_page_clean(page)
        finally:
            page.remove_listener("request", observed)


def test_library_primary_survives_keyboard_command_search_and_native_resize_focus(connected_library):
    server, page = connected_library, connected_library["page"]
    page.set_viewport_size({"width": 320, "height": 900})
    page.goto(server["url"] + "/", wait_until="load")
    before = _domain_snapshot(server)
    media, _ = _open_media(page)
    library = media.locator('a[data-nav-primary="browse"]')
    library.focus()
    page.set_viewport_size({"width": 1280, "height": 900})
    expect(media.locator(":scope > summary")).to_be_focused()
    expect(media).not_to_have_attribute("open", "")
    page.set_viewport_size({"width": 320, "height": 900})
    expect(page.get_by_test_id("nav-menu-button").filter(visible=True)).to_be_focused()
    page.locator("h1").click()
    with page.expect_navigation(wait_until="load") as navigation:
        page.keyboard.press("b")
    assert navigation.value.status == 200
    expect(page).to_have_url(server["url"] + LIBRARY)
    page.locator("[data-command-palette-launcher]").click()
    expect(page.locator('[data-command-palette-result][href="/browse"]')).to_have_count(0)
    query = "Synthetic search & catalogue text"
    page.locator("[data-command-palette-query]").fill(query)
    result = page.locator("[data-command-palette-result]").last
    parts = urlsplit(result.get_attribute("href"))
    assert parts.path == "/library" and parse_qs(parts.query) == {
        "reset": ["1"], "view": ["items"], "sort": ["recent"], "q": [query]}
    assert _domain_snapshot(server) == before
    assert_page_clean(page)


def test_missing_native_dialog_support_does_not_recreate_browse_or_disable_library_menu(connected_library, browser):
    server = connected_library
    with _browser_page(browser, viewport={"width": 320, "height": 900}) as page:
        page.add_init_script("delete HTMLDialogElement.prototype.showModal;")
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        nav = page.locator("[data-media-navigation]")
        expect(nav).to_have_attribute("data-nav-palette-fallback", "unavailable")
        expect(nav.locator("a.media-navigation__primary")).to_have_count(0)
        media, _ = _open_media(page)
        library = media.locator('a[data-nav-primary="browse"]')
        expect(library).to_be_visible()
        with page.expect_navigation(wait_until="load") as navigation:
            library.click()
        assert navigation.value.status == 200
        expect(page).to_have_url(server["url"] + LIBRARY)
        assert_page_clean(page)
