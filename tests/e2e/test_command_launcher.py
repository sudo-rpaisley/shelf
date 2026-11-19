"""Future fresh-synthetic native round-launcher journeys; authored, not executed."""

from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean
from tests.e2e.test_browse_button_removal import _open_media
from tests.e2e.test_connected_library import _browser_page, _login, connected_library
from tests.e2e.test_cover_size_integration import WIDTHS, _domain_snapshot


pytestmark = pytest.mark.e2e
_COLOURS = "node => {const s=getComputedStyle(node);return [s.backgroundColor,s.borderTopColor,s.color];}"


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("text_scale", (1, 2))
def test_round_commands_matches_help_above_it_with_a_larger_hit_area_and_native_focus(
    connected_library, browser, width, text_scale,
):
    server = connected_library
    with _browser_page(browser, viewport={"width": width, "height": 700}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        page.emulate_media(reduced_motion="reduce")
        page.evaluate("scale => {document.documentElement.style.fontSize = (100 * scale) + '%';}", text_scale)
        root = page.locator("[data-command-palette]")
        expect(root).to_have_attribute("data-command-palette-enhanced", "true")
        expect(root).to_have_css("position", "fixed")
        launcher = page.get_by_role("button", name="Commands", exact=True)
        expect(launcher).to_have_attribute("title", "Commands")
        circle = launcher.locator(".command-palette__launch-icon")
        help_button = page.locator("[data-shortcut-help]")
        hit, visual, help_box = launcher.bounding_box(), circle.bounding_box(), help_button.bounding_box()
        assert hit["width"] >= 44 and hit["height"] >= 44
        assert visual["width"] == pytest.approx(help_box["width"], abs=1)
        assert visual["height"] == pytest.approx(help_box["height"], abs=1)
        assert visual["x"] == pytest.approx(help_box["x"], abs=1)
        assert hit["x"] + hit["width"] / 2 == pytest.approx(visual["x"] + visual["width"] / 2, abs=1)
        assert hit["width"] > visual["width"] and hit["height"] > visual["height"]
        assert hit["y"] + hit["height"] < help_box["y"], "The help target must not be covered"
        assert 0 < help_box["y"] - visual["y"] - visual["height"] < help_box["height"]
        assert circle.evaluate("node => parseFloat(getComputedStyle(node).borderRadius)") >= visual["width"] / 2
        assert circle.evaluate(_COLOURS) == help_button.evaluate(_COLOURS)
        assert 0 <= hit["x"] < hit["x"] + hit["width"] <= width
        assert 0 <= hit["y"] < hit["y"] + hit["height"] <= 700
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        before = _domain_snapshot(server)
        storage = page.evaluate("() => [JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie]")
        requests = []

        def observed(request):
            if not urlsplit(request.url).path.startswith(("/static/", "/covers/")):
                requests.append((request.method, request.url))

        page.on("request", observed)
        try:
            launcher.focus()
            launcher.press("Enter")
            dialog = page.get_by_role("dialog", name="Commands", exact=True)
            expect(dialog).to_be_visible()
            expect(dialog.get_by_role("combobox")).to_be_focused()
            expect(launcher).to_have_attribute("aria-expanded", "true")
            page.keyboard.press("Escape")
            expect(dialog).to_be_hidden()
            expect(launcher).to_be_focused()
            expect(launcher).to_have_css("outline-style", "solid")
            help_button.click()
            expect(page.locator("#shortcut-modal")).to_be_visible()
            expect(dialog).to_be_hidden()
            page.keyboard.press("Escape")
            expect(page.locator("#shortcut-modal")).to_be_hidden()
            for shortcut in ("Control+k", "Meta+k"):
                page.locator("h1").click()
                page.keyboard.press(shortcut)
                expect(dialog).to_be_visible()
                page.keyboard.press("Escape")
                expect(dialog).to_be_hidden()
            assert launcher.bounding_box() == hit, "Commands must stay above help after scrolling/focus return"
            assert requests == [] and _domain_snapshot(server) == before
            assert page.evaluate("() => [JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie]") == storage
            page.emulate_media(media="print")
            expect(launcher).to_be_hidden()
            expect(help_button).to_be_hidden()
            assert_page_clean(page)
        finally:
            page.remove_listener("request", observed)


@pytest.mark.parametrize("javascript,native", ((False, True), (True, False)))
def test_unavailable_palette_keeps_its_inflow_explanation_and_real_native_library_link(
    connected_library, browser, javascript, native,
):
    server = connected_library
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": 320, "height": 700}) as page:
        if not native:
            page.add_init_script("delete HTMLDialogElement.prototype.showModal;")
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        root = page.locator("[data-command-palette]")
        expect(root).not_to_have_attribute("data-command-palette-enhanced", "true")
        expect(root).to_have_css("position", "static")
        expect(page.locator("[data-command-palette-launcher]")).to_be_hidden()
        fallback = page.locator("[data-command-palette-fallback]")
        fallback.locator("summary").focus()
        fallback.locator("summary").press("Enter")
        expect(fallback.locator("p")).to_be_visible()
        expect(page.locator("[data-command-palette-dialog]")).to_be_hidden()
        before = _domain_snapshot(server)
        _open_media(page)
        primary = page.locator('[data-nav-primary="browse"]')
        href = primary.get_attribute("href")
        assert href == "/library?reset=1&view=items&sort=recent"
        with page.expect_navigation(wait_until="load") as navigation:
            primary.click()
        assert navigation.value.status == 200
        expect(page).to_have_url(server["url"] + href)
        assert _domain_snapshot(server) == before
        assert_page_clean(page)
