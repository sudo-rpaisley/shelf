"""Real-browser controls for quick destinations and native disclosure repair.

The existing local shell renderer supplies authorized synthetic data, not HTML
fixtures. HTTP policy/query acceptance remains in the unchanged integration tests
and the companion unit matrix. No provider accounts or external requests are used.
"""

from contextlib import contextmanager
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard
from tests.e2e.test_media_navigation_layout import ORIGIN, _fits, _render, _shell


pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
WIDTHS = (320, 390, 640, 768, 1023, 1024, 1440, 2000)


def _header_metrics(nav):
    return json.dumps(nav.evaluate("""e => Array.from(e.querySelectorAll(
        '.media-navigation__bar,.media-navigation__main,.media-navigation__shell,' +
        '.media-navigation__tree,.media-navigation__activity,.media-navigation__quick,' +
        '.media-navigation__quick details,.media-navigation__quick summary,.media-navigation__quick ul,' +
        '.media-navigation__quick li,.media-navigation__account'
    )).map(e => {const s=getComputedStyle(e), r=e.getBoundingClientRect(); return {
        element:e.className || e.tagName, x:r.x, y:r.y, width:r.width, height:r.height,
        display:s.display, position:s.position, float:s.cssFloat, inline:s.inlineSize
    }})"""), indent=2)


@pytest.mark.parametrize("key", ["store", "series", "discover", "stats"])
def test_desktop_quick_destination_is_visible_actionable_without_opening_a_menu(browser, key):
    with _shell(browser) as (page, _):
        link = page.locator(f'[data-nav-tab="{key}"]')
        expect(link).to_have_count(1)
        expect(link).to_be_visible()
        with page.expect_navigation():
            link.click()
        expect(page).to_have_url(ORIGIN + "/" + key)
        expect(link).to_have_attribute("aria-current", "page")


def test_explore_really_collapses_and_stays_collapsed_through_other_navigation_operations(browser):
    with _shell(browser) as (page, requests):
        before = list(requests)
        explore = page.locator('[data-nav-group="explore"] > summary')
        series = page.locator('[data-nav-tab="series"]')
        expect(explore).to_have_attribute("aria-expanded", "true")
        assert series.evaluate("e => getComputedStyle(e.closest('ul')).position") == "static"
        explore.focus()
        page.keyboard.press("Enter")
        expect(explore).to_have_attribute("aria-expanded", "false")
        expect(series).to_be_hidden()
        page.locator('[data-nav-browse] > summary').click()
        page.get_by_role("heading", name="Home", exact=True).click()
        expect(explore).to_have_attribute("aria-expanded", "false")
        page.set_viewport_size({"width": 390, "height": 800})
        page.get_by_test_id("nav-menu-button").click()
        expect(series).to_be_hidden()
        explore.focus()
        page.keyboard.press("Space")
        expect(series).to_be_visible()
        page.set_viewport_size({"width": 1440, "height": 900})
        expect(series).to_be_visible()
        expect(explore).to_have_attribute("aria-expanded", "true")
        assert requests == before, "disclosure operations must not fetch navigation data"


def test_main_menu_exposes_stats_and_the_same_external_primary_browse_in_one_operation(browser):
    with _shell(browser, width=390, height=800) as (page, _):
        primary = page.locator('[data-nav-primary="browse"]')
        expect(primary).to_have_count(1)
        expect(primary).to_be_hidden()
        assert primary.evaluate("e => !e.closest('[data-testid=nav-menu-panel]')")
        assert primary.evaluate("e => e.nextElementSibling.matches('[data-nav-shell]')")
        toggle = page.get_by_test_id("nav-menu-button")
        toggle.focus()
        page.keyboard.press("Enter")
        expect(primary).to_be_visible()
        stats = page.locator('[data-nav-tab="stats"]')
        expect(stats).to_be_visible()
        with page.expect_navigation():
            stats.click()
        expect(page).to_have_url(ORIGIN + "/stats")


def test_focused_primary_stays_visible_when_resizing_or_dismissing_the_narrow_menu(browser):
    with _shell(browser, width=1024) as (page, _):
        primary = page.locator('[data-nav-primary="browse"]')
        toggle = page.get_by_test_id("nav-menu-button")
        primary.focus()
        page.set_viewport_size({"width": 1023, "height": 900})
        expect(primary).to_be_hidden()
        expect(toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(primary).to_be_visible()
        primary.focus()
        page.set_viewport_size({"width": 1024, "height": 900})
        expect(primary).to_be_focused()
        expect(primary).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 800})
        expect(toggle).to_be_focused()
        page.keyboard.press("Space")
        primary.focus()
        page.keyboard.press("Escape")
        expect(primary).to_be_hidden()
        expect(toggle).to_be_focused()
        page.keyboard.press("Enter")
        primary.focus()
        page.get_by_role("heading", name="Home", exact=True).click()
        expect(primary).to_be_hidden()
        expect(toggle).to_be_focused()


def test_native_tab_order_reaches_quick_links_and_leaves_the_navigation_without_a_trap(browser):
    with _shell(browser, width=390) as (page, _):
        toggle = page.get_by_test_id("nav-menu-button")
        toggle.focus()
        page.keyboard.press("Enter")
        expect(page.locator('[data-nav-primary="browse"]')).to_be_visible()
        page.keyboard.press("Shift+Tab")
        expect(page.locator('[data-nav-primary="browse"]')).to_be_focused()
        page.keyboard.press("Tab")
        expect(toggle).to_be_focused()
        page.keyboard.press("Tab")
        expect(page.locator('[data-nav-browse] > summary')).to_be_focused()
        page.locator('[data-nav-tab="series"]').focus()
        page.keyboard.press("Tab")
        expect(page.locator('[data-nav-tab="discover"]')).to_be_focused()
        page.keyboard.press("Tab")
        expect(page.locator('[data-nav-tab="stats"]')).to_be_focused()
        page.keyboard.press("Tab")
        expect(page.locator('[data-nav-group="manage"] > summary')).to_be_focused()
        page.keyboard.press("Tab")
        expect(page.get_by_role("button", name="Account menu", exact=True)).to_be_focused()
        expect(toggle).to_have_attribute("aria-expanded", "false")


def test_compact_account_retains_its_label_target_focus_and_working_profile_menu(browser):
    with _shell(browser, width=1024, stale="published") as (page, _):
        account = page.get_by_role("button", name="Account menu", exact=True)
        account.focus()
        assert account.evaluate("e => getComputedStyle(e).outlineStyle") != "none"
        page.keyboard.press("Enter")
        expect(page.get_by_test_id("account-menu-panel")).to_be_visible()
        expect(page.get_by_test_id("account-menu-settings")).to_have_attribute("href", "/settings")
        page.get_by_test_id("account-menu-profile").click()
        close = page.get_by_role("button", name="Close account dialog", exact=True)
        expect(close).to_be_visible()
        close.click()
        expect(close).to_be_hidden()
        toggle = page.get_by_test_id("nav-menu-button")
        page.set_viewport_size({"width": 390, "height": 800})
        toggle.click()
        page.locator('[data-nav-primary="browse"]').focus()
        account.click()
        expect(page.locator('[data-nav-primary="browse"]')).to_be_hidden()
        expect(account).to_be_focused()
        expect(page.get_by_test_id("account-menu-panel")).to_be_visible()


@pytest.mark.parametrize("width", [320, 1024])
def test_enlarged_text_reflows_without_clipping_quick_navigation(browser, width):
    with _shell(browser, width=width, height=900, stale="published") as (page, _):
        page.add_style_tag(content="html { font-size: 200%; }")
        if width < 1024:
            page.get_by_test_id("nav-menu-button").click()
        for key in ("store", "series", "discover", "stats"):
            link = page.locator(f'[data-nav-tab="{key}"]')
            link.scroll_into_view_if_needed()
            expect(link).to_be_visible()
            box = link.bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= width, (key, box, _header_metrics(page.locator("nav")))
            assert link.evaluate("e => parseFloat(getComputedStyle(e).fontSize)") >= 28
        _fits(page)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("stale", [False, True, "published"], ids=["current-css", "stripped-css", "published-css"])
def test_quick_navigation_has_readable_targets_and_no_overflow_at_all_eight_widths(browser, width, stale):
    with _shell(browser, width=width, stale=stale) as (page, _):
        nav = page.locator("[data-media-navigation]")
        initial_height = nav.bounding_box()["height"]
        boxes = []
        if width < 1024:
            assert nav.bounding_box()["height"] <= 120
            page.get_by_test_id("nav-menu-button").click()
        else:
            assert nav.bounding_box()["height"] <= 72, _header_metrics(nav)
        for selector in ('[data-nav-primary="browse"]', '[data-nav-browse] > summary',
                         '[data-nav-group="add"] > summary', '[data-nav-tab="store"]',
                         '[data-nav-group="explore"] > summary', '[data-nav-tab="series"]',
                         '[data-nav-tab="discover"]', '[data-nav-tab="stats"]',
                         '[data-nav-group="manage"] > summary'):
            control = page.locator(selector)
            control.scroll_into_view_if_needed()
            expect(control).to_be_visible()
            box = control.bounding_box()
            boxes.append(box)
            assert box["width"] >= 80 and box["height"] >= 44, (width, selector, box)
            assert box["x"] >= 0 and box["x"] + box["width"] <= width, (width, selector, box)
            style = control.evaluate("""e => {const s = getComputedStyle(e); return {
                opacity: s.opacity, clip: s.clipPath, font: parseFloat(s.fontSize),
                nowrap: s.whiteSpace, overflow: s.overflow, text: s.textOverflow
            }}""")
            assert style["opacity"] == "1" and style["clip"] == "none" and style["font"] >= 14, style
            assert style["overflow"] != "hidden" and style["text"] != "ellipsis", style
        label = page.locator('[data-nav-browse] > summary > span')
        assert label.bounding_box()["width"] >= 35
        assert label.evaluate("e => getComputedStyle(e).position") == "static"
        quick_panel = page.locator('[data-nav-group="explore"] > ul')
        assert quick_panel.evaluate("""e => {const s=getComputedStyle(e); return {
            position:s.position, start:s.insetInlineStart, end:s.insetInlineEnd,
            shadow:s.boxShadow, border:s.borderTopWidth
        }}""") == {"position": "static", "start": "auto", "end": "auto", "shadow": "none", "border": "0px"}
        account = page.get_by_role("button", name="Account menu", exact=True)
        expect(account).to_be_visible()
        box = account.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44
        _fits(page)
        print("NAVIGATION_GEOMETRY=" + json.dumps({
            "width": width, "css": stale, "initial_header_height": initial_height,
            "min_text_target_width": min(box["width"] for box in boxes),
            "min_text_target_height": min(box["height"] for box in boxes),
            "gutter": nav.locator(".app-shell").evaluate("e => getComputedStyle(e).paddingInlineStart"),
        }, sort_keys=True))


def test_short_desktop_keeps_media_formats_reachable_and_explore_in_flow(browser):
    with _shell(browser, width=1024, height=320, stale="published") as (page, _):
        expect(page.locator('[data-nav-tab="stats"]')).to_be_visible()
        assert page.locator('[data-nav-tab="stats"]').evaluate("e => getComputedStyle(e.closest('ul')).position") == "static"
        page.locator('[data-nav-browse] > summary').click()
        page.locator('[data-nav-family="books"] summary').click()
        ebook = page.locator('[data-nav-format="ebook"]')
        ebook.scroll_into_view_if_needed()
        expect(ebook).to_be_visible()
        box = ebook.bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= 320, box
        _fits(page)


def test_short_phone_can_scroll_to_stats_after_only_opening_main_menu(browser):
    with _shell(browser, width=390, height=320, stale="published") as (page, _):
        page.get_by_test_id("nav-menu-button").click()
        stats = page.locator('[data-nav-tab="stats"]')
        stats.scroll_into_view_if_needed()
        expect(stats).to_be_visible()
        box = stats.bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= 320, box
        with page.expect_navigation():
            stats.click()
        expect(page).to_have_url(ORIGIN + "/stats")


def test_no_js_quick_links_and_native_explore_collapse_survive_resize(browser):
    with _shell(browser, width=390, javascript=False, stale="published") as (page, _):
        explore = page.locator('[data-nav-group="explore"] > summary')
        stats = page.locator('[data-nav-tab="stats"]')
        expect(stats).to_be_visible()
        expect(page.locator('[data-nav-tab="store"]')).to_be_visible()
        explore.click()
        expect(stats).to_be_hidden()
        page.set_viewport_size({"width": 1024, "height": 900})
        expect(explore).to_be_visible()
        expect(stats).to_be_hidden()
        explore.click()
        expect(stats).to_be_visible()
        assert stats.evaluate("e => getComputedStyle(e.closest('ul')).position") == "static"
        _fits(page)


@contextmanager
def _touch_shell(browser):
    context = browser.new_context(viewport={"width": 390, "height": 568}, has_touch=True,
                                  is_mobile=True, service_workers="block", reduced_motion="reduce")

    def serve(route):
        parts = urlsplit(route.request.url)
        assert parts.netloc == "shelf.test", route.request.url
        if parts.path.startswith("/static/"):
            path = ROOT / parts.path.lstrip("/")
            assert path.is_relative_to(ROOT / "static") and path.is_file()
            return route.fulfill(path=path)
        return route.fulfill(status=200, content_type="text/html", body=_render(parts.path))

    context.route("**/*", serve)
    page = attach_page_guard(context.new_page())
    try:
        page.goto(ORIGIN, wait_until="load")
        expect(page.locator("[data-media-navigation]")).to_have_attribute("data-nav-enhanced", "true")
        yield page
        assert_page_clean(page)
    finally:
        context.close()


def test_real_touch_main_menu_and_native_explore_do_not_require_hover(browser):
    with _touch_shell(browser) as page:
        page.get_by_test_id("nav-menu-button").tap()
        stats = page.locator('[data-nav-tab="stats"]')
        expect(stats).to_be_visible()
        explore = page.locator('[data-nav-group="explore"] > summary')
        explore.tap()
        expect(stats).to_be_hidden()
        explore.tap()
        expect(stats).to_be_visible()
        with page.expect_navigation():
            stats.tap()
        expect(page).to_have_url(ORIGIN + "/stats")
