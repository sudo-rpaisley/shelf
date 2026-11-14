"""Shipped caption/status layout and native fallbacks on real Library pages.

Legacy markup/style cases explicitly substitute cached document/style fixtures;
long-label stress is not a claim that a complete locale/AT engine was qualified.
"""

from pathlib import Path
import re
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard


pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
CANONICAL = "/library?reset=1&view=items&sort=recent"
WIDTHS = (320, 375, 640, 768, 1024, 1280, 1366, 1920)


def _values(form):
    return form.evaluate("node => Object.fromEntries(new FormData(node))")


def _parts(page, count):
    details = page.get_by_test_id("library-filter-options")
    summary = details.locator(":scope > summary")
    body = details.locator(":scope > .library-filter-options__body")
    expect(summary).to_have_text("Filters and display")
    expect(details).not_to_have_attribute("open", "")
    expect(body).to_be_hidden()
    badge = page.get_by_test_id("library-active-filter-count")
    expect(badge).to_have_count(int(count > 0))
    if count:
        expect(badge).to_have_attribute("data-count", str(count))
        expect(badge).to_be_visible()
        expect(summary).to_have_attribute("aria-describedby", badge.get_attribute("id"))
        assert summary.evaluate("(node,id) => !node.contains(document.getElementById(id))", badge.get_attribute("id"))
    else:
        assert summary.get_attribute("aria-describedby") is None
    return details, summary, body, badge


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("stress", (False, True), ids=("normal", "long-labels-200percent"))
def test_caption_and_real_status_wrap_without_exposing_closed_controls(authed_page, live_server, width, stress):
    page = authed_page
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(live_server["url"] + CANONICAL, wait_until="load")
    details, summary, body, badge = _parts(page, 2)
    before = _values(page.get_by_test_id("library-filters"))
    if stress:
        page.evaluate("document.documentElement.style.fontSize = '200%'")
        summary.evaluate("node => node.textContent = 'Filtri e opzioni di visualizzazione e ordinamento della raccolta'")
        badge.evaluate("node => node.textContent = '· 2 opzioni attive'")
    expect(summary).to_be_visible()
    box = details.bounding_box()
    following = page.get_by_test_id("library-saved-views").bounding_box()
    assert box["height"] > 0 and box["x"] >= 0 and box["x"] + box["width"] <= width + .5
    assert following["y"] >= box["y"] + box["height"]
    for control in (summary, badge):
        rect = control.bounding_box()
        assert rect["width"] > 0 and rect["x"] >= box["x"] - .5
        assert rect["x"] + rect["width"] <= box["x"] + box["width"] + .5
    assert body.locator("select,button,a").evaluate_all("nodes => nodes.every(node => node.getClientRects().length === 0)")
    summary.focus()
    page.keyboard.press("Tab")
    expect(page.get_by_test_id("library-saved-views").locator(":scope > summary")).to_be_focused()
    summary.press("Enter")
    expect(body.locator("select").first).to_be_visible()
    opened = body.bounding_box()
    assert abs(opened["width"] - details.bounding_box()["width"]) < 1
    assert opened["y"] >= summary.bounding_box()["y"] + summary.bounding_box()["height"]
    assert _values(page.get_by_test_id("library-filters")) == before
    summary.press("Space")
    expect(body).to_be_hidden()
    expect(badge).to_be_visible()
    assert _values(page.get_by_test_id("library-filters")) == before
    assert_page_clean(page)


@pytest.mark.parametrize("params,count", (
    ({}, 0), ({"family": "book"}, 1), ({"view": "items", "sort": "recent"}, 2),
    ({"family": "book", "sort": "recent", "page_size": "48"}, 3),
    ({"media_type": "ebook", "family": "book", "series": "Saga #1/2", "source": "source:999",
      "ownership": "neither", "wishlist": "no", "view": "items", "sort": "recent", "page_size": "48"}, 9),
))
def test_real_status_and_successful_values_survive_closed_native_submission(authed_page, live_server, params, count):
    page = authed_page
    page.goto(live_server["url"] + "/library?" + urlencode({"reset": "1", **params}), wait_until="load")
    _parts(page, count)
    before = _values(page.get_by_test_id("library-filters"))
    with page.expect_navigation():
        page.get_by_test_id("library-filters").get_by_role("button", name="Search", exact=True).click()
    _parts(page, count)
    assert _values(page.get_by_test_id("library-filters")) == before
    assert_page_clean(page)


@pytest.mark.parametrize("mode", ("published", "unsupported"))
def test_new_html_with_old_or_unavailable_enhancement_css_keeps_native_disclosure(authed_page, live_server, mode):
    page = authed_page

    def stylesheet(route):
        if mode == "published":
            data = (ROOT / "tests/fixtures/navigation/67461926-app.css").read_bytes()
        else:
            data = (ROOT / "static/css/app.css").read_bytes()
            condition = b"selector(details::details-content)"
            assert data.count(condition) == 1
            data = data.replace(condition, b"selector(details::shelf-unsupported-details-content)")
        route.fulfill(status=200, content_type="text/css", body=data)

    page.route("**/static/css/app.css", stylesheet)
    page.goto(live_server["url"] + CANONICAL, wait_until="load")
    details = page.get_by_test_id("library-filter-options")
    summary = details.locator(":scope > summary")
    badge = page.get_by_test_id("library-active-filter-count")
    body = details.locator(":scope > .library-filter-options__body")
    expect(summary).to_have_text("Filters and display")
    expect(body).to_be_hidden()
    expect(badge).to_be_hidden()
    expect(summary).to_have_attribute("aria-describedby", badge.get_attribute("id"))
    before = _values(page.get_by_test_id("library-filters"))
    summary.press("Enter")
    expect(body.locator("select").first).to_be_visible()
    expect(badge).to_be_visible()
    expect(badge).to_have_attribute("data-count", "2")
    assert _values(page.get_by_test_id("library-filters")) == before
    summary.press("Space")
    expect(body).to_be_hidden()
    assert_page_clean(page)


def _legacy_document(html):
    match = re.search(r'<details data-testid="library-filter-options"[^>]*>(.*?)</details>', html, re.S)
    assert match
    body = match.group(1)
    assert body.count('<div class="library-filter-options__body">') == 1
    body = body.replace('<div class="library-filter-options__body">', '', 1)
    before_close, after_close = body.rsplit('</div>', 1)
    body = before_close + after_close
    badge = re.search(r'<span[^>]*data-testid="library-active-filter-count"[^>]*>.*?</span>', body, re.S)
    assert badge
    body = body.replace(badge.group(), '', 1).replace('</summary>', ' ' + badge.group() + '</summary>', 1)
    body = re.sub(r' aria-describedby="library-filter-active-count"', '', body)
    return html[:match.start()] + '<details data-testid="library-filter-options">' + body + '</details>' + html[match.end():]


def test_cached_old_html_without_body_marker_is_not_enhanced_by_new_css(authed_page, live_server):
    page = authed_page
    response = page.request.get(live_server["url"] + CANONICAL)
    assert response.status == 200
    html = _legacy_document(response.text())
    page.route("**/library?legacy-caption-control=1", lambda route: route.fulfill(
        status=200, content_type="text/html", body=html,
        headers={"Content-Security-Policy": response.headers["content-security-policy"]},
    ))
    page.goto(live_server["url"] + "/library?legacy-caption-control=1", wait_until="load")
    details = page.get_by_test_id("library-filter-options")
    assert details.get_attribute("data-library-filter-layout") is None
    expect(details.locator(".library-filter-options__body")).to_have_count(0)
    expect(details.locator("select").first).to_be_hidden()
    expect(details.locator("summary")).to_have_text("Filters and display · 2 active options")
    details.locator("summary").press("Enter")
    expect(details.locator("select").first).to_be_visible()
    details.locator("summary").press("Space")
    expect(details.locator("select").first).to_be_hidden()
    assert_page_clean(page)


def test_no_js_native_keyboard_keeps_real_status_and_form_values(browser, authed_page, live_server):
    context = browser.new_context(java_script_enabled=False, service_workers="block",
                                  storage_state=authed_page.context.storage_state(),
                                  viewport={"width": 375, "height": 900})
    page = attach_page_guard(context.new_page())
    try:
        page.goto(live_server["url"] + CANONICAL, wait_until="load")
        details, summary, body, badge = _parts(page, 2)
        summary.press("Enter")
        expect(details).to_have_attribute("open", "")
        expect(body.locator("select").first).to_be_visible()
        expect(badge).to_be_visible()
        summary.press("Space")
        expect(body).to_be_hidden()
        assert_page_clean(page)
    finally:
        context.close()
