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
        expect(primary).to_be_focused()
        page.set_viewport_size({"width": 1023, "height": 900})
        expect(primary).to_be_hidden()
        expect(toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(primary).to_be_visible()
        primary.focus()
        expect(primary).to_be_focused()
        page.set_viewport_size({"width": 1024, "height": 900})
        expect(primary).to_be_focused()
        expect(primary).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 800})
        expect(toggle).to_be_focused()
        page.keyboard.press("Space")
        expect(primary).to_be_visible()
        primary.focus()
        expect(primary).to_be_focused()
        page.keyboard.press("Escape")
        expect(primary).to_be_hidden()
        expect(toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(primary).to_be_visible()
        primary.focus()
        expect(primary).to_be_focused()
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
        primary = page.locator('[data-nav-primary="browse"]')
        expect(primary).to_be_visible()
        primary.focus()
        expect(primary).to_be_focused()
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


# Phone follow-up controls are appended below the frozen390 definitions.
@pytest.mark.parametrize("stale", [False, True, "published"], ids=["current", "stripped", "published"])
def test_phone_ready_button_is_second_child_and_toggles_actual_details_natively(browser, stale):
    with _shell(browser, width=375, stale=stale) as (page, requests):
        shell = page.locator('[data-nav-shell]')
        native = shell.locator(':scope > summary')
        button = page.get_by_role("button", name="Main menu", exact=True)
        tree = page.get_by_test_id("nav-menu-panel")
        expect(shell).to_have_attribute("data-nav-shell-button", "ready")
        expect(button).to_have_count(1)
        expect(button).to_be_visible()
        assert button.evaluate("e => e.tagName === 'BUTTON' && e.type === 'button' && e.parentElement.children[1] === e")
        assert shell.evaluate("e => e.tagName === 'DETAILS' && e.firstElementChild.tagName === 'SUMMARY' && !e.open")
        assert page.locator('[data-nav-primary="browse"]').evaluate("e => e.nextElementSibling === document.querySelector('[data-nav-shell]')")
        assert native.get_attribute("role") is None
        expect(native).to_be_hidden()
        assert native.get_attribute("data-testid") is None
        expect(page.get_by_test_id("nav-menu-button")).to_have_count(1)
        expect(tree).to_be_hidden()
        expect(button).to_have_attribute("aria-expanded", "false")
        controlled = button.get_attribute("aria-controls")
        assert controlled and tree.get_attribute("id") == controlled
        assert page.locator('[id]').evaluate_all("(els, id) => els.filter(e => e.id === id).length", controlled) == 1
        assert shell.evaluate("e => {const s=getComputedStyle(e, '::details-content'); return s.contentVisibility === 'visible' && s.display === 'contents'}")
        before = list(requests)
        button.focus()
        page.keyboard.press("Enter")
        assert shell.evaluate("e => e.open")
        expect(button).to_have_attribute("aria-expanded", "true")
        expect(tree).to_be_visible()
        library = tree.get_by_role("link", name="Library", exact=True)
        expect(library).to_be_visible()
        expect(library).to_have_attribute("href", "/library?reset=1&view=items&sort=recent")
        expect(page.get_by_role("link", name="All media", exact=True)).to_have_count(0)
        expect(library.locator('..').locator('.media-navigation__description')).to_have_text("All media")
        page.keyboard.press("Shift+Tab")
        expect(page.locator('[data-nav-primary="browse"]')).to_be_focused()
        page.keyboard.press("Tab")
        expect(button).to_be_focused()
        page.keyboard.press("Tab")
        expect(page.locator('[data-nav-browse] > summary')).to_be_focused()
        button.focus()
        page.keyboard.press("Space")
        assert not shell.evaluate("e => e.open")
        expect(tree).to_be_hidden()
        expect(button).to_have_attribute("aria-expanded", "false")
        expect(button).to_be_visible()
        expect(button).to_be_focused()
        assert requests == before
        _fits(page)


def _phone_fault_script(fault, *, collision=False, focus_native=False):
    """Fault injection and observation in tests only; production detects capabilities."""
    return """(() => {
        const fault = %s;
        const trace = window.__phoneSetup = {created: [], verified: [], commits: [], injected: 0, writes: []};
        const create = document.createElement.bind(document);
        document.createElement = function(tag, ...args) {
            if (fault === 'create-throws' && tag === 'button') {
                trace.injected++;
                throw new Error('Injected button creation failure');
            }
            const element = create(tag, ...args);
            if (tag === 'button') {
                const record = {element, added: 0, removed: 0};
                trace.created.push(record);
                const add = element.addEventListener.bind(element), remove = element.removeEventListener.bind(element);
                element.addEventListener = function(type, ...args) {
                    if (type === 'click') record.added++;
                    return add(type, ...args);
                };
                element.removeEventListener = function(type, ...args) {
                    if (type === 'click') record.removed++;
                    return remove(type, ...args);
                };
            }
            return element;
        };
        const supports = CSS.supports.bind(CSS);
        CSS.supports = function(...args) {
            if ((fault === 'selector-unsupported' && args[0].includes('details-content')) ||
                (fault === 'property-unsupported' && args[0] === 'content-visibility') ||
                (fault === 'display-unsupported' && args[0] === 'display' && args[1] === 'contents')) {
                trace.injected++;
                return false;
            }
            return supports(...args);
        };
        const style = window.getComputedStyle.bind(window);
        window.getComputedStyle = function(element, pseudo) {
            if (element.matches('[data-nav-shell]') && pseudo === '::details-content') {
                if (fault === 'style-throws') { trace.injected++; throw new Error('Injected style failure'); }
                const result = style(element, pseudo);
                const button = element.querySelector(':scope > button'), tree = element.querySelector('[data-testid="nav-menu-panel"]');
                trace.verified.push({closed: !element.open, display: result.display,
                    visibility: result.contentVisibility, button: !!button && button.checkVisibility(),
                    tree: style(tree).display, summary: style(element.firstElementChild).display});
                return result;
            }
            return style(element, pseudo);
        };
        const attribute = Element.prototype.setAttribute;
        Element.prototype.setAttribute = function(name, value) {
            if (this.matches('[data-nav-shell]') && name === 'data-nav-shell-button' && value === 'ready') {
                trace.commits.push({verified: trace.verified.slice(), button: this.children[1].tagName,
                    nativeHook: this.firstElementChild.getAttribute('data-testid'),
                    hooks: this.querySelectorAll('[data-testid="nav-menu-button"]').length});
                if (fault === 'commit-throws') { trace.injected++; throw new Error('Injected readiness commit failure'); }
            }
            if (fault === 'hook-throws' && this.tagName === 'BUTTON' && name === 'data-testid' && value === 'nav-menu-button') {
                trace.injected++;
                throw new Error('Injected hook transfer failure');
            }
            return attribute.call(this, name, value);
        };
        const opened = Object.getOwnPropertyDescriptor(HTMLDetailsElement.prototype, 'open');
        Object.defineProperty(HTMLDetailsElement.prototype, 'open', {...opened, set(value) {
            if (this.matches('[data-nav-shell]') && this.open !== !!value) trace.writes.push(!!value);
            opened.set.call(this, value);
        }});
        document.addEventListener('DOMContentLoaded', () => {
            const shell = document.querySelector('[data-nav-shell]'), tree = shell.querySelector('[data-testid="nav-menu-panel"]');
            if (fault === 'insert-missing') { shell.insertBefore = undefined; trace.injected++; }
            if (fault === 'insert-throws') {
                const insert = shell.insertBefore.bind(shell);
                shell.insertBefore = function(...args) {
                    const result = insert(...args); trace.injected++;
                    throw new Error('Injected failure after actual insertion');
                };
            }
            if (fault === 'commit-throws' || fault === 'hook-throws') tree.id = 'existing-phone-tree';
            if (%s) {
                tree.id = 'media-navigation-tree-1';
                const collision = create('div'); collision.id = tree.id; document.body.appendChild(collision);
            }
            if (%s) shell.firstElementChild.focus();
        });
    })();""" % (json.dumps(fault), json.dumps(collision), json.dumps(focus_native))


@contextmanager
def _phone_capability_shell(browser, *, fault=None, collision=False, focus_native=False, empty=False, javascript=True):
    import mimetypes
    import re

    context = browser.new_context(viewport={"width": 375, "height": 812},
                                  java_script_enabled=javascript, service_workers="block", reduced_motion="reduce")
    context.add_init_script(_phone_fault_script(fault, collision=collision, focus_native=focus_native))
    requests = []

    def serve(route):
        parts = urlsplit(route.request.url)
        assert parts.netloc == "shelf.test", route.request.url
        requests.append(parts.path)
        if not parts.path.startswith("/static/"):
            return route.fulfill(status=200, content_type="text/html",
                                 body=_phone_empty_render(parts.path) if empty else _render(parts.path))
        path = ROOT / parts.path.lstrip("/")
        assert path.is_relative_to(ROOT / "static") and path.is_file()
        body = path.read_bytes()
        if fault == "missing-css" and parts.path == "/static/css/app.css":
            body = re.sub(rb"[^{}]*\.media-navigation[^{}]*\{[^{}]*\}", b"", body)
        if fault == "missing-css" and parts.path == "/static/css/media-navigation.source.css":
            body = b""
        if fault == "wrong-pseudo-css" and parts.path == "/static/css/media-navigation.source.css":
            body = body.replace(b"content-visibility: visible;", b"content-visibility: hidden;")
        return route.fulfill(status=200, content_type=mimetypes.guess_type(path)[0], body=body)

    context.route("**/*", serve)
    page = attach_page_guard(context.new_page())
    try:
        page.goto(ORIGIN, wait_until="load")
        if javascript:
            expect(page.locator('[data-media-navigation]')).to_have_attribute("data-nav-enhanced", "true")
        yield page, requests
        assert_page_clean(page)
    finally:
        context.close()


def _phone_empty_render(path):
    from types import SimpleNamespace
    from jinja2 import Environment, FileSystemLoader
    from app.media_presentation import family_nodes
    from app.nav import media_navigation

    env = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)
    env.globals.update(_=lambda text, **kwargs: text % kwargs if kwargs else text,
                       current_locale=lambda: "en", js_catalog=lambda: {},
                       feature_on=lambda key: False, i18n_enabled=lambda: False)
    return env.from_string('{% extends "base.html" %}{% block content %}<h1>Home</h1>{% endblock %}').render(
        request=SimpleNamespace(url=SimpleNamespace(path=path)), user=None, nav_tabs=[],
        media_navigation=media_navigation([], family_nodes()),
    )


@pytest.mark.parametrize("fault", [
    "selector-unsupported", "property-unsupported", "display-unsupported", "insert-missing",
    "create-throws", "insert-throws", "style-throws", "hook-throws", "commit-throws",
    "missing-css", "wrong-pseudo-css",
])
def test_phone_failed_capability_or_partial_setup_rolls_back_and_keeps_native_enhancement(browser, fault):
    with _phone_capability_shell(browser, fault=fault, focus_native=True) as (page, _):
        shell = page.locator('[data-nav-shell]')
        native = shell.locator(':scope > summary')
        tree = page.get_by_test_id("nav-menu-panel")
        assert shell.get_attribute("data-nav-shell-button") is None
        expect(shell.locator(':scope > button')).to_have_count(0)
        expect(page.get_by_role("button", name="Main menu", exact=True)).to_have_count(0)
        expect(page.get_by_test_id("nav-menu-button")).to_have_count(1)
        expect(native).to_have_attribute("data-testid", "nav-menu-button")
        assert native.get_attribute("role") is None
        assert tree.get_attribute("id") == ("existing-phone-tree" if fault in {"hook-throws", "commit-throws"} else None)
        assert not shell.evaluate("e => e.open"), "native enhancement must still select its narrow closed layout"
        expect(native).to_be_visible()
        expect(native).to_be_focused()
        trace = page.evaluate("() => ({injected:__phoneSetup.injected, records:__phoneSetup.created.map(r => ({added:r.added, removed:r.removed, connected:r.element.isConnected}))})")
        if fault not in {"missing-css", "wrong-pseudo-css"}:
            assert trace["injected"] >= 1, "the intended failure must actually be reached"
        assert all(not row["connected"] and row["added"] == row["removed"] for row in trace["records"]), trace
        page.evaluate("() => {for (const r of __phoneSetup.created) r.element.click()}")
        assert not shell.evaluate("e => e.open"), "detached partial controls must have no live listener"
        page.keyboard.press("Enter")
        expect(native).to_have_attribute("aria-expanded", "true")
        expect(tree.get_by_role("link", name="Library", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        expect(native).to_be_focused()
        expect(native).to_have_attribute("aria-expanded", "false")
        page.add_script_tag(url=ORIGIN + "/static/js/media-navigation.js")
        expect(page.get_by_test_id("nav-menu-button")).to_have_count(1)
        expect(shell.locator(':scope > button')).to_have_count(0)
        assert shell.get_attribute("data-nav-shell-button") is None


def test_phone_readiness_uses_loaded_closed_state_proof_collision_free_id_and_one_commit(browser):
    with _phone_capability_shell(browser, collision=True, focus_native=True) as (page, _):
        shell = page.locator('[data-nav-shell]')
        button = page.get_by_role("button", name="Main menu", exact=True)
        expect(shell).to_have_attribute("data-nav-shell-button", "ready")
        expect(button).to_be_focused()
        controlled = button.get_attribute("aria-controls")
        assert controlled and controlled != "media-navigation-tree-1"
        assert controlled == page.get_by_test_id("nav-menu-panel").get_attribute("id")
        assert page.locator('[id]').evaluate_all("(els, id) => els.filter(e => e.id === id).length", controlled) == 1
        trace = page.evaluate("() => ({commits:__phoneSetup.commits, verified:__phoneSetup.verified})")
        assert len(trace["commits"]) == 1
        assert trace["commits"][0]["button"] == "BUTTON"
        assert trace["commits"][0]["nativeHook"] is None and trace["commits"][0]["hooks"] == 1
        assert any(row == {"closed": True, "display": "contents", "visibility": "visible",
                           "button": True, "tree": "none", "summary": "flex"}
                   for row in trace["commits"][0]["verified"]), trace
        assert not page.locator('[data-nav-browse]').evaluate("e => e.open"), "queued setup toggles must not open Media"
        for _ in range(2):
            page.add_script_tag(url=ORIGIN + "/static/js/media-navigation.js")
        assert page.evaluate("() => __phoneSetup.commits.length") == 1
        expect(page.get_by_test_id("nav-menu-button")).to_have_count(1)
        expect(shell.locator(':scope > button')).to_have_count(1)
        writes = page.evaluate("() => __phoneSetup.writes.length")
        button.click()
        expect(button).to_have_attribute("aria-expanded", "true")
        assert page.evaluate("() => __phoneSetup.writes.length") == writes + 1
        button.click()
        expect(button).to_have_attribute("aria-expanded", "false")
        assert page.evaluate("() => __phoneSetup.writes.length") == writes + 2


def test_phone_actual_programmatic_toggles_and_focus_return_use_the_correct_opener(browser):
    with _shell(browser, width=375) as (page, _):
        shell = page.locator('[data-nav-shell]')
        button = shell.locator(':scope > button')
        expect(page.get_by_role("button", name="Main menu", exact=True)).to_have_count(1)
        tree = page.get_by_test_id("nav-menu-panel")
        shell.evaluate("e => {e.open = true}")
        expect(button).to_have_attribute("aria-expanded", "true")
        expect(tree).to_be_visible()
        primary = page.locator('[data-nav-primary="browse"]')
        primary.focus()
        shell.evaluate("e => {e.open = false}")
        expect(button).to_have_attribute("aria-expanded", "false")
        expect(primary).to_be_hidden()
        expect(button).to_be_focused()
        button.click()
        page.locator('[data-nav-family="books"] > details > summary').click()
        book = page.locator('[data-nav-format="book"]')
        book.focus()
        page.keyboard.press("Escape")
        expect(page.locator('[data-nav-family="books"] summary')).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator('[data-nav-browse] > summary')).to_be_focused()
        page.keyboard.press("Escape")
        expect(button).to_be_focused()
        expect(tree).to_be_hidden()
        button.click()
        tree.get_by_role("link", name="Library", exact=True).focus()
        shell.evaluate("e => {e.open = false}")
        expect(tree).to_be_hidden()
        expect(button).to_be_focused()
        page.set_viewport_size({"width": 1024, "height": 900})
        expect(page.locator('[data-nav-browse] > summary')).to_be_focused()
        expect(button).to_be_hidden()
        shell.evaluate("e => {e.open = false}")
        expect(button).to_have_attribute("aria-expanded", "false")
        expect(button).to_be_visible()
        assert not shell.evaluate("e => e.open"), "real mode must not force a fictitious permanently open shell"
        button.click()
        expect(button).to_have_attribute("aria-expanded", "true")
        expect(tree).to_be_visible()
        expect(page.get_by_role("button", name="Main menu", exact=True)).to_have_count(0)
        expect(page.locator('[data-nav-browse] > summary')).to_be_focused()
        primary.focus()
        page.set_viewport_size({"width": 1023, "height": 900})
        expect(button).to_be_focused()
        expect(button).to_be_visible()


def test_phone_ready_empty_context_and_no_js_native_fallback_do_not_invent_destinations(browser):
    with _phone_capability_shell(browser, empty=True) as (page, _):
        button = page.get_by_role("button", name="Main menu", exact=True)
        expect(button).to_be_visible()
        button.click()
        expect(page.get_by_test_id("nav-menu-panel")).to_have_text("No navigation available.")
        expect(page.locator('[data-nav-tab]')).to_have_count(0)
        assert page.locator('nav a').evaluate_all("els => els.map(e => e.getAttribute('href'))") == ["/"]
        page.set_viewport_size({"width": 1024, "height": 900})
        expect(page.locator('.media-navigation__brand')).to_be_focused()
    with _phone_capability_shell(browser, javascript=False) as (page, _):
        native = page.locator('[data-nav-shell] > summary')
        assert native.get_attribute("role") is None
        expect(page.locator('[data-nav-shell] > button')).to_have_count(0)
        native.click()
        page.set_viewport_size({"width": 1024, "height": 900})
        expect(native).to_be_visible()
        native.click()
        page.locator('[data-nav-browse] > summary').click()
        expect(page.get_by_role("link", name="Library", exact=True)).to_be_visible()


def test_phone_ready_button_works_with_real_touch_and_reduced_motion(browser):
    with _touch_shell(browser) as page:
        button = page.get_by_role("button", name="Main menu", exact=True)
        expect(button).to_be_visible()
        assert button.locator('svg').evaluate("e => getComputedStyle(e).transitionDuration") == "0s"
        button.tap()
        expect(button).to_have_attribute("aria-expanded", "true")
        expect(page.get_by_role("link", name="Library", exact=True)).to_be_visible()
        button.tap()
        expect(button).to_have_attribute("aria-expanded", "false")
        expect(page.get_by_test_id("nav-menu-panel")).to_be_hidden()
        expect(button).to_be_visible()


# Reuse the unchanged fresh Standard fixture; never patch the original journey.
from tests.e2e.test_connected_library import connected_library


def test_phone_later_library_search_counts_thresholds_and_account_journey(connected_library, browser):
    """Independent later-leg coverage, NOT a replacement/pass for the full original.

    The original still checks Home overflow and remains RED at its first newly
    reached failure. Start a separate Library journey to expose later failures
    without suppressing, catching or changing any of its original assertions.
    """
    from tests.e2e.conftest import ADMIN_USERNAME, ADMIN_PASSWORD
    from tests.e2e.test_connected_library import (
        _assert_phone_fits, _browser_page, _choice, _copy, _counts, _db, _item,
        _login, _navigate, _open_details,
    )

    server = connected_library
    long_location = (
        "Home / Upstairs landing / Reading room window shelf / Tall archive cabinet / "
        "Upper compartment / Box of illustrated journeys"
    )
    book_title = "An Atlas of Quiet Places and Long Autumn Journeys"
    video_title = "Evening Journeys Through the Northern Islands"
    with _db(server) as db:
        location = db.execute("INSERT INTO locations (name) VALUES (?)", (long_location,)).lastrowid
        book = _item(db, book_title, location_id=location)
        _copy(db, book, location)
        video = _item(db, video_title, "digital_video", authors="Robin Clearwater", duration_mins=142)
    with _browser_page(browser, viewport={"width": 375, "height": 812}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        menu = page.get_by_test_id("nav-menu-panel")
        page.get_by_role("button", name="Main menu", exact=True).click()
        expect(menu).to_be_visible()
        expect(menu.get_by_role("link", name="Library", exact=True)).to_be_visible()
        _navigate(page, menu.get_by_role("link", name="Library", exact=True))
        expect(page.get_by_test_id("library-page")).to_be_visible()
        _assert_phone_fits(page)
        filters = page.get_by_test_id("library-filters")
        options = page.get_by_test_id("library-filter-options")
        expect(options).not_to_have_attribute("open", "")
        expect(options.locator("summary")).to_have_text("Filters and display")
        search = filters.get_by_label("Search your library", exact=True)
        expect(search).to_be_visible()
        assert search.bounding_box()["width"] >= 120, "The phone search field collapsed"
        expect(page.get_by_role("navigation", name="Library presets", exact=True)).to_be_visible()
        results = page.get_by_test_id("library-results")
        _counts(page, 2, 2)
        page.evaluate("window.scrollTo(0, 0)")
        assert results.bounding_box()["y"] < 900, "Closed filters push results below a phone's first screen"
        card_heading = results.locator(f'[data-library-member="{book}"]').get_by_role(
            "heading", name=book_title, exact=True,
        )
        expect(card_heading).to_be_visible()
        assert card_heading.bounding_box()["y"] < 900, "The first matching card is buried below the filters"
        _assert_phone_fits(page)
        search.fill("Quiet Places")
        _open_details(options)
        _choice(filters, "Format").select_option("book")
        _choice(filters, "Availability").select_option("physical")
        _choice(filters, "Holding source").select_option("physical")
        _navigate(page, filters.get_by_role("button", name="Apply filters", exact=True))
        _counts(page, 1, 1)
        expect(options).not_to_have_attribute("open", "")
        expect(results).to_contain_text(long_location)
        _assert_phone_fits(page)
        saved = page.get_by_test_id("library-saved-views")
        saved.locator("summary").click()
        expect(saved.get_by_label("View name", exact=True)).to_be_visible()
        _assert_phone_fits(page)
        page.get_by_test_id("account-menu-button").click()
        account = page.get_by_test_id("account-menu-panel")
        expect(account).to_be_visible()
        for key in ("activity", "sources", "needs-attention"):
            expect(account.get_by_test_id(f"account-menu-{key}")).to_be_visible()
        _assert_phone_fits(page)
        _navigate(page, account.get_by_test_id("account-menu-sources"))
        expect(page.get_by_role("heading", name="Connected libraries", exact=True)).to_be_visible()
        _assert_phone_fits(page)
        for item, title in ((book, book_title), (video, video_title)):
            page.goto(f"{server['url']}/item/{item}")
            expect(page.get_by_role("heading", level=1)).to_have_text(title)
            expect(page.get_by_test_id("personal-library-editor")).not_to_have_attribute("open", "")
            expect(page.get_by_test_id("catalogue-status-heading")).to_have_text("Shared catalogue status")
            if item == book:
                expect(page.get_by_test_id("available-here")).to_contain_text(long_location)
            _assert_phone_fits(page)
            copy_location = page.get_by_role("combobox", name="Location for the new copy", exact=True)
            expect(copy_location).to_be_visible()
            copy_location.scroll_into_view_if_needed()
            box = copy_location.bounding_box()
            assert box["width"] > 0 and box["x"] >= 0 and box["x"] + box["width"] <= 375, (
                f"The new-copy location selector leaves the phone viewport on item {item}: {box}"
            )
            _assert_phone_fits(page)
        page.get_by_title("Keyboard shortcuts (?)", exact=True).click()
        shortcuts = page.locator("#shortcut-modal")
        expect(shortcuts).to_be_visible()
        close = shortcuts.get_by_role("button", name="Close keyboard shortcuts", exact=True)
        expect(close).to_be_visible()
        _assert_phone_fits(page)
        close.click()
        expect(shortcuts).to_be_hidden()
