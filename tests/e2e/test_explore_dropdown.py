"""Future native downward-menu journeys using fresh synthetic servers; unrun."""

from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean
from tests.e2e.test_connected_library import _browser_page, _login, connected_library
from tests.e2e.test_cover_size_integration import _domain_snapshot


pytestmark = pytest.mark.e2e


def _shell_open(page):
    shell = page.locator("details[data-nav-shell]")
    if shell.get_attribute("open") is None:
        page.get_by_test_id("nav-menu-button").filter(visible=True).click()


@pytest.mark.parametrize("width", (320, 1024, 1280))
@pytest.mark.parametrize("javascript", (False, True))
def test_explore_opens_below_its_trigger_and_stacks_links_not_sideways(connected_library, browser, width, javascript):
    server = connected_library
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": width, "height": 900}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        page.locator("h1").hover()
        _shell_open(page)
        explore = page.locator('details[data-nav-group="explore"]')
        summary = explore.locator(":scope > summary")
        stats = explore.locator('[data-nav-tab="stats"]')
        expect(explore).not_to_have_attribute("open", "")
        expect(stats).to_be_hidden()
        before = _domain_snapshot(server)
        requests = []

        def observed(request):
            if not urlsplit(request.url).path.startswith(("/static/", "/covers/")):
                requests.append((request.method, request.url))

        page.on("request", observed)
        try:
            header_height = page.locator("[data-media-navigation]").bounding_box()["height"]
            summary.focus()
            summary.press("Enter")
            expect(explore).to_have_attribute("open", "")
            panel = explore.locator(":scope > ul")
            trigger, box = summary.bounding_box(), panel.bounding_box()
            assert box["y"] >= trigger["y"] + trigger["height"] - 1
            assert box["x"] >= -1 and box["x"] + box["width"] <= width + 1
            links = panel.locator("a").all()
            assert len(links) >= 2
            boxes = [link.bounding_box() for link in links]
            for current, following in zip(boxes, boxes[1:]):
                assert following["y"] >= current["y"] + current["height"] - 1
                assert following["x"] == pytest.approx(current["x"], abs=1)
            assert all(box["width"] >= 80 and box["height"] >= 44 for box in boxes)
            if width >= 1024:
                expect(panel).to_have_css("position", "absolute")
                assert page.locator("[data-media-navigation]").bounding_box()["height"] == header_height
            else:
                expect(panel).to_have_css("position", "static")
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
            assert requests == [] and _domain_snapshot(server) == before
            expect(stats).to_have_attribute("href", "/stats")
            stats.focus()
            with page.expect_navigation(wait_until="load") as navigation:
                stats.press("Enter")
            assert navigation.value.status == 200
            expect(page).to_have_url(server["url"] + "/stats")
            assert requests == [("GET", server["url"] + "/stats")]
            assert _domain_snapshot(server) == before
            assert_page_clean(page)
        finally:
            page.remove_listener("request", observed)


def test_explore_uses_the_common_keyboard_peer_and_outside_dismissal_rules(connected_library):
    server, page = connected_library, connected_library["page"]
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(server["url"] + "/", wait_until="load")
    page.locator("h1").hover()
    explore = page.locator('details[data-nav-group="explore"]')
    summary = explore.locator(":scope > summary")
    summary.focus()
    summary.press("Space")
    expect(explore).to_have_attribute("open", "")
    series = explore.locator('[data-nav-tab="series"]')
    series.focus()
    series.press("Escape")
    expect(explore).not_to_have_attribute("open", "")
    expect(summary).to_be_focused()
    summary.press("Enter")
    peer = page.locator("details[data-nav-browse]")
    peer.locator(":scope > summary").focus()
    peer.locator(":scope > summary").press("Enter")
    expect(peer).to_have_attribute("open", "")
    expect(explore).not_to_have_attribute("open", "")
    summary.focus()
    summary.press("Enter")
    expect(explore).to_have_attribute("open", "")
    expect(peer).not_to_have_attribute("open", "")
    page.locator("h1").click()
    expect(explore).not_to_have_attribute("open", "")
    assert_page_clean(page)


def test_desktop_hover_reaches_the_downward_panel_and_leaving_closes_it(connected_library):
    server, page = connected_library, connected_library["page"]
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(server["url"] + "/", wait_until="load")
    page.locator("h1").hover()
    explore = page.locator('details[data-nav-group="explore"]')
    explore.locator(":scope > summary").hover()
    expect(explore).to_have_attribute("open", "")
    explore.locator('[data-nav-tab="series"]').hover()
    expect(explore).to_have_attribute("open", "")
    page.locator("h1").hover()
    expect(explore).not_to_have_attribute("open", "")
    assert_page_clean(page)
