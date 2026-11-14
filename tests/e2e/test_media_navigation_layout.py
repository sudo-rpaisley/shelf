"""Real-browser presentation checks using the shipped templates and assets.

The authorized fixture model is synthetic: these tests do not replace HTTP role,
feature or library-query acceptance. All browser requests are fulfilled locally.
"""

import hashlib
import mimetypes
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard


pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://shelf.test"


def _render(path="/"):
    from app.media_presentation import family_nodes
    from app.nav import NAV_TABS, media_navigation

    env = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)
    env.globals.update(
        _=lambda text, **kwargs: text % kwargs if kwargs else text,
        current_locale=lambda: "en", js_catalog=lambda: {},
        feature_on=lambda key: False, i18n_enabled=lambda: False,
    )
    tabs = [dict(tab) for tab in NAV_TABS if "admin" in tab.get("roles", ("admin",))]
    return env.from_string(
        '{% extends "base.html" %}{% block content %}'
        '<h1 class="text-2xl font-bold">Home</h1>{% endblock %}'
    ).render(
        request=SimpleNamespace(url=SimpleNamespace(path=path)),
        user={"id": 1, "username": "admin", "display_name": "Alex Morgan", "role": "admin"},
        nav_tabs=tabs, media_navigation=media_navigation(tabs, family_nodes()),
    )


@contextmanager
def _shell(browser, *, width=1440, height=900, stale=False, javascript=True):
    requests = []
    context = browser.new_context(
        viewport={"width": width, "height": height}, java_script_enabled=javascript,
        reduced_motion="reduce", service_workers="block",
    )

    def serve(route):
        parts = urlsplit(route.request.url)
        requests.append(parts.path)
        if parts.netloc != "shelf.test":
            return route.abort()
        if not parts.path.startswith("/static/"):
            return route.fulfill(status=200, content_type="text/html", body=_render(parts.path))
        path = ROOT / parts.path.lstrip("/")
        assert path.is_relative_to(ROOT / "static") and path.is_file(), parts.path
        body = path.read_bytes()
        if stale == "published" and parts.path == "/static/css/app.css":
            body = (ROOT / "tests/fixtures/navigation/67461926-app.css").read_bytes()
            assert hashlib.sha256(body).hexdigest() == "97574f6257d368d5f13977dcc91f8144cd857f9e32fd064fda2f99e701b746f2"
            assert b".media-navigation__browse-toggle>span{" in body
        elif stale and parts.path == "/static/css/app.css":
            # Controlled old-bundle model, not a claim about a deployed cache:
            # omit custom navigation rules but retain ordinary app utilities.
            body = re.sub(rb"[^{}]*\.media-navigation[^{}]*\{[^{}]*\}", b"", body)
            assert b".media-navigation__bar" not in body
        return route.fulfill(status=200, content_type=mimetypes.guess_type(path)[0], body=body)

    context.route("**/*", serve)
    page = attach_page_guard(context.new_page())
    try:
        page.goto(ORIGIN, wait_until="load")
        if javascript:
            expect(page.locator("[data-media-navigation]")).to_have_attribute("data-nav-enhanced", "true")
        yield page, requests
        assert_page_clean(page)
    finally:
        context.close()


def _fits(page):
    metrics = page.evaluate("""() => ({width: document.documentElement.clientWidth,
        scroll: document.documentElement.scrollWidth})""")
    assert metrics["scroll"] <= metrics["width"], metrics


@pytest.mark.parametrize("width", [1024, 1280, 1440, 2000])
@pytest.mark.parametrize("stale", [False, True, "published"], ids=["current-css", "old-bundle", "published-css"])
def test_desktop_header_is_one_compact_labelled_row_even_with_old_bundle(browser, width, stale):
    with _shell(browser, width=width, stale=stale) as (page, requests):
        nav = page.locator("[data-media-navigation]")
        assert nav.bounding_box()["height"] <= 72
        selectors = (
            ".media-navigation__brand", '[data-nav-primary="browse"]',
            "[data-nav-browse] > summary", '[data-nav-group="add"] > summary',
            '[data-nav-group="explore"] > summary', '[data-nav-group="manage"] > summary',
            '[data-testid="account-menu-button"]',
        )
        boxes = [page.locator(selector).bounding_box() for selector in selectors]
        assert all(box and box["height"] >= 28 and box["width"] >= 40 for box in boxes), boxes
        centers = [box["y"] + box["height"] / 2 for box in boxes]
        assert max(centers) - min(centers) <= 2, boxes
        assert all(left["x"] + left["width"] <= right["x"] + 1 for left, right in zip(boxes, boxes[1:])), boxes
        media_label = page.locator("[data-nav-browse] > summary > span")
        assert media_label.bounding_box()["width"] >= 35, "Media must be a visible label, not a bare chevron"
        style = media_label.evaluate("""e => {const s=getComputedStyle(e);
            return {position:s.position,clip:s.clipPath,overflow:s.overflow}}""")
        assert style == {"position": "static", "clip": "none", "overflow": "visible"}, style
        expect(page.get_by_test_id("nav-menu-button")).to_be_hidden()
        assert "/static/css/media-navigation.source.css" in requests
        _fits(page)


def test_desktop_media_menu_is_keyboard_operable_and_does_not_shift_content(browser):
    with _shell(browser) as (page, _):
        heading = page.get_by_role("heading", name="Home", exact=True)
        before = heading.bounding_box()
        media = page.locator("[data-nav-browse] > summary")
        media.focus()
        page.keyboard.press("Enter")
        expect(media).to_have_attribute("aria-expanded", "true")
        books = page.locator('[data-nav-family="books"] summary')
        books.focus()
        page.keyboard.press("Space")
        book = page.locator('[data-nav-format="book"]')
        expect(book).to_be_visible()
        expect(book).to_have_attribute("href", "/library?reset=1&view=items&sort=recent&types=book")
        box = book.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 1440
        assert heading.bounding_box() == before, "Desktop dropdowns must overlay, not push the page down"
        page.keyboard.press("Escape")
        expect(books).to_be_focused()
        expect(book).to_be_hidden()
        page.keyboard.press("Escape")
        expect(media).to_be_focused()
        expect(media).to_have_attribute("aria-expanded", "false")
        page.keyboard.press("Enter")
        heading.click()
        expect(media).to_have_attribute("aria-expanded", "false")


def test_desktop_first_mouse_click_does_not_close_a_menu_opened_by_pointer_entry(browser):
    with _shell(browser) as (page, _):
        media = page.locator("[data-nav-browse] > summary")
        media.click()
        expect(media).to_have_attribute("aria-expanded", "true")
        books = page.locator('[data-nav-family="books"] summary')
        books.click()
        expect(books).to_have_attribute("aria-expanded", "true")
        expect(page.locator('[data-nav-format="book"]')).to_be_visible()
        books.click()
        expect(books).to_have_attribute("aria-expanded", "false")
        media.click()
        expect(media).to_have_attribute("aria-expanded", "false")


@pytest.mark.parametrize("width", [320, 375, 768])
def test_mobile_menu_stays_in_flow_and_reaches_family_format_and_management_links(browser, width):
    with _shell(browser, width=width, height=568, stale=True) as (page, _):
        nav = page.locator("[data-media-navigation]")
        assert nav.bounding_box()["height"] <= 120
        toggle = page.get_by_test_id("nav-menu-button")
        expect(toggle).to_be_visible()
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        books = page.locator('[data-nav-family="books"] summary')
        books.click()
        book = page.locator('[data-nav-format="book"]')
        book.scroll_into_view_if_needed()
        expect(book).to_be_visible()
        assert book.evaluate("e => getComputedStyle(e.closest('ul')).position") == "static"
        _fits(page)
        page.locator('[data-nav-group="manage"] > summary').click()
        manager = page.locator("[data-nav-management]")
        manager.scroll_into_view_if_needed()
        expect(manager).to_be_visible()
        expect(manager).to_have_attribute("href", "/browse")
        _fits(page)
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        expect(toggle).to_be_focused()


def test_no_javascript_native_navigation_keeps_an_opener_after_resize(browser):
    with _shell(browser, width=375, javascript=False, stale=True) as (page, _):
        toggle = page.get_by_test_id("nav-menu-button")
        expect(toggle).to_be_visible()
        toggle.click()  # close the initially open native fallback
        page.set_viewport_size({"width": 1280, "height": 900})
        expect(toggle).to_be_visible()
        toggle.click()
        page.locator("[data-nav-browse] > summary").click()
        page.locator('[data-nav-family="books"] summary').click()
        expect(page.locator('[data-nav-format="book"]')).to_be_visible()
        expect(page.locator('[data-nav-primary="browse"]')).to_have_attribute(
            "href", "/library?reset=1&view=items&sort=recent",
        )


def test_authenticated_navigation_uses_real_library_management_and_account_routes(live_server, authed_page):
    """Separate HTTP/auth integration on the existing throwaway app fixture."""
    page, base = authed_page, live_server["url"]
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(base + "/")
    expect(page.locator("[data-media-navigation]")).to_have_attribute("data-nav-enhanced", "true")
    assert page.locator("[data-media-navigation]").bounding_box()["height"] <= 72
    stylesheet = page.request.get(base + "/static/css/media-navigation.source.css")
    assert stylesheet.status == 200
    assert stylesheet.headers["cache-control"] == "no-cache"

    page.locator('[data-nav-primary="browse"]').click()
    page.wait_for_url(base + "/library?reset=1&view=items&sort=recent")
    expect(page.get_by_test_id("library-page")).to_be_visible()
    expect(page.locator('[data-nav-primary="browse"]')).to_have_attribute("aria-current", "page")
    page.locator("[data-nav-browse] > summary").click()
    page.locator('[data-nav-family="books"] summary').click()
    page.locator('[data-nav-format="book"]').click()
    page.wait_for_url(base + "/library?reset=1&view=items&sort=recent&types=book")
    expect(page.get_by_test_id("library-page")).to_be_visible()

    page.locator('[data-nav-group="manage"] > summary').click()
    page.locator("[data-nav-management]").click()
    page.wait_for_url(base + "/browse")
    page.get_by_test_id("account-menu-button").click()
    page.get_by_test_id("account-menu-profile").click()
    close_account = page.get_by_role("button", name="Close account dialog", exact=True)
    expect(close_account).to_be_visible()
    close_account.click()
    expect(close_account).to_be_hidden()
    page.get_by_test_id("account-menu-button").click()
    page.get_by_test_id("account-menu-settings").click()
    page.wait_for_url(base + "/settings")
    _fits(page)
    assert_page_clean(page)
