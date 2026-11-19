"""Future native Home header alignment on fresh synthetic servers; unexecuted."""

from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean
from tests.e2e.test_connected_library import _browser_page, _login, connected_library
from tests.e2e.test_cover_size_integration import WIDTHS, _domain_snapshot
from tests.e2e.test_shared_cover_size_setting import _choose, _key


pytestmark = pytest.mark.e2e


def _assert_header(page, width):
    header = page.get_by_test_id("home-header")
    row = header.locator("[data-home-header-row]")
    title = row.locator("h1")
    tools = row.locator("[data-home-header-tools]")
    control = tools.locator("[data-cover-size-control]")
    expect(page.locator("[data-cover-size-control]")).to_have_count(1)
    expect(control).to_have_count(1)
    expect(page.locator("[data-media-display-tools] [data-cover-size-control]")).to_have_count(0)
    expect(title).to_have_text("Home")
    heading, first_line, tool_box = title.bounding_box(), control.locator("label").bounding_box(), tools.bounding_box()
    wrapped = tool_box["y"] >= heading["y"] + heading["height"] - 1
    if width >= 1024:
        assert not wrapped, "Home and Item size must share the heading line when space is available"
    if not wrapped:
        assert tool_box["x"] >= heading["x"] + heading["width"]
        assert first_line["y"] + first_line["height"] / 2 == pytest.approx(heading["y"] + heading["height"] / 2, abs=1)
    else:
        assert tool_box["y"] >= heading["y"] + heading["height"]
    row_box = row.bounding_box()
    description = header.locator(":scope > p").bounding_box()
    assert description["y"] >= row_box["y"] + row_box["height"]
    assert tool_box["x"] >= row_box["x"] - 1
    assert tool_box["x"] + tool_box["width"] == pytest.approx(row_box["x"] + row_box["width"], abs=1)
    slider = control.locator("input")
    assert slider.bounding_box()["width"] >= 80 and slider.bounding_box()["height"] >= 44
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert control.evaluate("node => !node.closest('form,nav')")


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("text_scale", (1, 2))
def test_heading_and_slider_share_the_first_line_or_wrap_without_offsets_or_overflow(
    connected_library, browser, width, text_scale,
):
    server = connected_library
    with _browser_page(browser, viewport={"width": width, "height": 900}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        page.evaluate("scale => {document.documentElement.style.fontSize = (100 * scale) + '%';}", text_scale)
        before = _domain_snapshot(server)
        stored = page.evaluate("() => [JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie]")
        requests = []

        def observed(request):
            if not urlsplit(request.url).path.startswith(("/static/", "/covers/")):
                requests.append((request.method, request.url))

        page.on("request", observed)
        try:
            _assert_header(page, width)
            expect(page.locator("[data-cover-size-input]")).to_have_value("5")
            expect(page.locator("[data-cover-size-output]")).to_have_text("100%")
            assert requests == [] and _domain_snapshot(server) == before
            assert page.evaluate("() => [JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie]") == stored
            page.emulate_media(media="print")
            expect(page.locator("[data-home-header-tools]")).to_be_hidden()
            expect(page.get_by_test_id("home-header").locator("h1")).to_be_visible()
            expect(page.locator("[data-home-header-row]")).to_have_css("display", "block")
            assert_page_clean(page)
        finally:
            page.remove_listener("request", observed)


@pytest.mark.parametrize("javascript,blocked", ((False, False), (True, True)))
def test_no_js_or_blocked_storage_keeps_first_line_alignment_with_notes_below(
    connected_library, browser, javascript, blocked,
):
    server = connected_library
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": 1280, "height": 900}) as page:
        if blocked:
            page.add_init_script("""{
                const get = Storage.prototype.getItem, set = Storage.prototype.setItem;
                Storage.prototype.getItem = function(key) {
                    if (key.startsWith('shelf.coverSize.v2:')) throw new Error('synthetic sizing read refusal');
                    return get.call(this, key);
                };
                Storage.prototype.setItem = function(key, value) {
                    if (key.startsWith('shelf.coverSize.v2:')) throw new Error('synthetic sizing write refusal');
                    return set.call(this, key, value);
                };
            }""")
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        _assert_header(page, 1280)
        slider = page.locator("[data-cover-size-input]")
        if blocked:
            expect(slider).to_be_enabled()
            expect(page.locator("[data-cover-size-status]")).to_contain_text("storage is unavailable")
        else:
            expect(slider).to_be_disabled()
            expect(page.locator("[data-cover-size-control] noscript p")).to_be_visible()
        assert_page_clean(page)


def test_moved_control_keeps_native_keyboard_sizing_and_the_same_saved_owner_across_pages(connected_library):
    server, page = connected_library, connected_library["page"]
    page.set_viewport_size({"width": 1280, "height": 900})
    before = _domain_snapshot(server)
    old_entries = page.evaluate("() => ({...localStorage})")
    requests = []

    def observed(request):
        if not urlsplit(request.url).path.startswith(("/static/", "/covers/")):
            requests.append((request.method, request.url))

    page.on("request", observed)
    try:
        _choose(page, 200)
        _assert_header(page, 1280)
        assert requests == [] and _domain_snapshot(server) == before
        assert page.evaluate("() => ({...localStorage})") == {**old_entries, _key(page): "200"}
    finally:
        page.remove_listener("request", observed)
    page.goto(server["url"] + "/library", wait_until="load")
    expect(page.locator("[data-cover-size-control]")).to_have_count(1)
    expect(page.locator("[data-media-display-tools] [data-cover-size-control]")).to_have_count(1)
    expect(page.locator("[data-cover-size-output]")).to_have_text("200%")
    page.goto(server["url"] + "/", wait_until="load")
    _assert_header(page, 1280)
    expect(page.locator("[data-cover-size-output]")).to_have_text("200%")
    assert _domain_snapshot(server) == before
    assert_page_clean(page)
