"""Real rendered component/capability controls, not authenticated HTTP acceptance.

The initializer is deliberately deferred by this local shell to observe actual
disclosure state before/after it. It then loads the shipped script unchanged.
Existing tests separately exercise the ordinary production load order.
"""

from contextlib import contextmanager
import mimetypes
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard
from tests.e2e.test_command_palette import ORIGIN, ROOT, _configure, _render


pytestmark = pytest.mark.e2e
MARKER = "data-nav-palette-fallback"


@contextmanager
def _deferred(browser, *, native=False, javascript=True, role="admin", empty=False, stale=False):
    context = browser.new_context(viewport={"width": 375, "height": 900},
                                  java_script_enabled=javascript, service_workers="block")
    if not native:
        context.add_init_script("""window.fallbackOriginalShowModal = HTMLDialogElement.prototype.showModal;
            HTMLDialogElement.prototype.showModal = undefined;""")
    state = {"deferred": True}
    requests = []

    def serve(route):
        parts = urlsplit(route.request.url)
        assert parts.netloc == "shelf.test", route.request.url
        requests.append((route.request.method, route.request.url))
        if parts.path == "/static/js/command-palette.js" and state["deferred"]:
            return route.fulfill(status=200, content_type="text/javascript", body="/* Deferred initializer control. */")
        if parts.path == "/__rf2/host.js":
            from tests.e2e.test_command_palette import HOST_JS

            return route.fulfill(status=200, content_type="text/javascript", body=HOST_JS)
        if parts.path.startswith("/static/"):
            path = ROOT / parts.path.lstrip("/")
            assert path.is_relative_to(ROOT / "static") and path.is_file()
            body = path.read_bytes()
            if parts.path == "/static/css/app.css":
                if stale == "published":
                    body = (ROOT / "tests/fixtures/navigation/67461926-app.css").read_bytes()
                elif stale:
                    body = re.sub(rb"[^{}]*\.media-navigation[^{}]*\{[^{}]*\}", b"", body)
            return route.fulfill(status=200, content_type=mimetypes.guess_type(path)[0], body=body)
        return route.fulfill(
            status=200, content_type="text/html", body=_render(role, parts.path, empty=empty),
            headers={"Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'"},
        )

    context.route("**/*", serve)
    page = attach_page_guard(context.new_page())

    def initialize():
        state["deferred"] = False
        page.add_script_tag(url=ORIGIN + "/static/js/command-palette.js")

    try:
        page.goto(ORIGIN, wait_until="load")
        if javascript:
            expect(page.locator("[data-media-navigation]")).to_have_attribute("data-nav-enhanced", "true")
        yield page, initialize, requests
        assert_page_clean(page)
    finally:
        context.close()


def _details(page):
    return page.locator("[data-media-navigation] details").evaluate_all(
        "nodes => nodes.map(node => [node.getAttribute('data-nav-group'), node.open])",
    )


@pytest.mark.parametrize("stale", (False, True, "published"), ids=("current", "stripped", "published"))
def test_unavailable_palette_exposes_only_the_same_primary_without_opening_any_disclosure(browser, stale):
    with _deferred(browser, stale=stale) as (page, initialize, requests):
        nav = page.locator("[data-media-navigation]")
        primary = nav.locator('[data-nav-primary="browse"]')
        expect(primary).to_be_hidden()
        before = _details(page)
        links = nav.locator("a").evaluate_all("nodes => nodes.map(node => [node.textContent, node.getAttribute('href')])")
        initialize()
        expect(nav).to_have_attribute(MARKER, "unavailable")
        expect(primary).to_be_visible()
        assert _details(page) == before
        assert nav.locator("a").evaluate_all("nodes => nodes.map(node => [node.textContent, node.getAttribute('href')])") == links
        expect(nav.locator("[data-nav-shell]")).not_to_have_attribute("open", "")
        assert primary.count() == 1
        href = primary.get_attribute("href")
        expect(page.locator("[data-command-palette-dialog]")).to_be_hidden()
        expect(page.locator("[data-command-palette-launcher]")).to_be_hidden()
        with page.expect_navigation():
            primary.press("Enter")
        expect(page).to_have_url(ORIGIN + href)
        assert all(method == "GET" for method, _ in requests)


def test_successful_reinitialization_clears_owned_fallback_and_does_not_duplicate_handlers(browser):
    with _deferred(browser) as (page, initialize, requests):
        nav = page.locator("[data-media-navigation]")
        before = _details(page)
        for _ in range(3):
            initialize()
        expect(nav).to_have_attribute(MARKER, "unavailable")
        assert _details(page) == before
        page.evaluate("""() => {
            HTMLDialogElement.prototype.showModal = window.fallbackOriginalShowModal;
            window.fallbackWindowBindings = [];
            const original = window.addEventListener;
            window.addEventListener = function(type, ...args) {
                fallbackWindowBindings.push(type); return original.call(this, type, ...args);
            };
        }""")
        initialize()
        expect(nav).not_to_have_attribute(MARKER, "unavailable")
        expect(page.locator("[data-command-palette]")).to_have_attribute("data-command-palette-enhanced", "true")
        expect(nav.locator('[data-nav-primary="browse"]')).to_be_hidden()
        for _ in range(3):
            initialize()
        assert page.evaluate("fallbackWindowBindings.sort()") == ["compositionend", "compositionstart", "keydown"]
        assert _details(page) == before
        page.locator("[data-command-palette-launcher]").click()
        expect(page.get_by_role("dialog", name="Commands", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("[data-command-palette-launcher]")).to_be_focused()
        assert all(method == "GET" for method, _ in requests)


@pytest.mark.parametrize("part", ("palette", "dialog", "query"))
def test_absent_or_incomplete_markup_does_not_claim_unavailable_native_support(browser, part):
    with _deferred(browser) as (page, initialize, _):
        selector = "[data-command-palette]" if part == "palette" else f"[data-command-palette-{part}]"
        page.locator(selector).evaluate("node => node.remove()")
        before = _details(page)
        initialize()
        expect(page.locator("[data-media-navigation]")).not_to_have_attribute(MARKER, "unavailable")
        expect(page.locator('[data-nav-primary="browse"]')).to_be_hidden()
        assert _details(page) == before


@pytest.mark.parametrize("native", (True, False))
@pytest.mark.parametrize("constraint", ("hidden", "inert", "aria-hidden"))
def test_explicit_primary_hidden_inert_and_accessibility_constraints_are_not_overridden(browser, native, constraint):
    with _deferred(browser, native=native) as (page, initialize, _):
        primary = page.locator('[data-nav-primary="browse"]')
        value = "true" if constraint == "aria-hidden" else ""
        primary.evaluate("(node,pair) => node.setAttribute(pair[0],pair[1])", [constraint, value])
        initialize()
        expect(primary).to_be_hidden()
        expect(primary).to_have_attribute(constraint, value)


@pytest.mark.parametrize("role,case", (("admin", "manual"), ("editor", "feature"), (None, "default")))
def test_unavailable_fallback_keeps_the_server_derived_core_browse_destination(browser, db, role, case):
    _configure(db, case)
    with _deferred(browser, role=role) as (page, initialize, _):
        before = _details(page)
        initialize()
        primary = page.locator('[data-nav-primary="browse"]')
        expect(primary).to_be_visible()
        expect(primary).to_have_attribute("href", "/browse?sort=newest")
        assert primary.count() == 1 and _details(page) == before


def test_empty_unavailable_navigation_never_creates_a_primary(browser):
    with _deferred(browser, empty=True) as (page, initialize, _):
        before = _details(page)
        anchors = page.locator("[data-media-navigation] a").count()
        initialize()
        expect(page.locator("[data-media-navigation]")).to_have_attribute(MARKER, "unavailable")
        expect(page.locator('[data-nav-primary="browse"]')).to_have_count(0)
        assert page.locator("[data-media-navigation] a").count() == anchors and _details(page) == before


def test_no_js_keeps_native_primary_visible_without_a_fallback_marker(browser):
    with _deferred(browser, javascript=False) as (page, _, _):
        expect(page.locator('[data-nav-primary="browse"]')).to_be_visible()
        expect(page.locator("[data-media-navigation]")).not_to_have_attribute(MARKER, "unavailable")
