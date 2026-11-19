"""Future real-dropdown journeys on fresh synthetic servers; not yet executed."""

from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean
from tests.e2e.test_connected_library import _add_user, _browser_page, _login, connected_library
from tests.e2e.test_cover_size_integration import _domain_snapshot


pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("width", (320, 1280))
@pytest.mark.parametrize("javascript", (False, True))
@pytest.mark.parametrize("role", ("admin", "editor"))
def test_native_dropdown_entry_is_unique_and_keyboard_reaches_the_same_add_page(
    connected_library, browser, width, javascript, role,
):
    server = connected_library
    username, password = ADMIN_USERNAME, ADMIN_PASSWORD
    if role == "editor":
        username, password = "synthetic-add-editor", "synthetic-add-password-2026"
        _add_user(server["page"], server, username, password, role)
    with _browser_page(browser, java_script_enabled=javascript,
                       viewport={"width": width, "height": 900}) as page:
        _login(page, server, username, password)
        before = _domain_snapshot(server)
        requests = []

        def observed(request):
            if not urlsplit(request.url).path.startswith(("/static/", "/covers/")):
                requests.append((request.method, request.url))

        page.on("request", observed)
        try:
            expect(page.get_by_test_id("add-media-entry")).to_have_count(1)
            shell = page.locator("details[data-nav-shell]")
            if shell.get_attribute("open") is None:
                page.get_by_test_id("nav-menu-button").filter(visible=True).click()
            group = page.locator('details[data-nav-group="add"]')
            opener = group.locator(":scope > summary")
            opener.focus()
            if group.get_attribute("open") is not None:
                opener.press("Enter")
                expect(group).not_to_have_attribute("open", "")
            opener.press("Enter")
            expect(group).to_have_attribute("open", "")
            entry = group.get_by_test_id("add-media-entry")
            expect(entry).to_be_visible()
            expect(entry).to_have_attribute("href", server["url"] + "/add")
            assert entry.get_attribute("data-nav-tab") is None
            assert entry.get_attribute("data-nav-menu-tab") is None
            assert requests == [], "Discovering/opening the menu must not submit or consult a provider"
            assert _domain_snapshot(server) == before
            entry.focus()
            with page.expect_navigation(wait_until="load") as reached:
                entry.press("Enter")
            assert reached.value.status == 200
            expect(page).to_have_url(server["url"] + "/add")
            expect(page.get_by_test_id("add-media-search")).to_be_visible()
            assert requests == [("GET", server["url"] + "/add")]
            assert _domain_snapshot(server) == before
            assert_page_clean(page)
        finally:
            page.remove_listener("request", observed)


def test_actual_viewer_session_has_no_add_entry(connected_library, browser):
    server = connected_library
    username, password = "synthetic-add-viewer", "synthetic-viewer-password-2026"
    _add_user(server["page"], server, username, password, "viewer")
    with _browser_page(browser, viewport={"width": 320, "height": 900}) as page:
        _login(page, server, username, password)
        expect(page.get_by_test_id("add-media-entry")).to_have_count(0)
        expect(page.locator('details[data-nav-group="add"]')).to_have_count(0)
        assert_page_clean(page)
