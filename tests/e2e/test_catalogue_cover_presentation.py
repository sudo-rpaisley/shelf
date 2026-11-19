"""Future shared-cover journeys on fresh synthetic servers; never run by authoring."""

import re
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean
from tests.e2e.test_connected_library import _browser_page, _login, connected_library
from tests.e2e.test_cover_size_integration import _domain_snapshot
from tests.e2e.test_shared_cover_size_setting import _choose, _seed_active


pytestmark = pytest.mark.e2e


_STYLE = """stage => {
    const style = getComputedStyle(stage), preview = stage.querySelector('.media-card__cover-preview');
    const caption = stage.querySelector('.media-card__caption'), title = caption.querySelector('h3');
    const ps = getComputedStyle(preview), cs = getComputedStyle(caption), ts = getComputedStyle(title);
    const box = stage.getBoundingClientRect(), art = stage.querySelector('.media-artwork').getBoundingClientRect();
    return {width:box.width, height:box.height, artWidth:art.width, artHeight:art.height,
        padding:style.padding, borderWidth:style.borderWidth, borderStyle:style.borderStyle,
        radius:style.borderRadius, background:style.backgroundImage,
        previewRadius:ps.borderRadius, shadow:ps.boxShadow, captionBackground:cs.backgroundImage,
        captionPadding:cs.padding, captionAlign:cs.textAlign, titleFont:ts.fontSize, titleWeight:ts.fontWeight};
}"""


@pytest.mark.parametrize("width", (320, 1366))
@pytest.mark.parametrize("percentage", (50, 100, 200))
def test_catalogue_frames_and_caption_styling_match_recently_added_with_native_focus(
    connected_library, width, percentage,
):
    server, page = connected_library, connected_library["page"]
    prefix, ids = _seed_active(server, f"cover-presentation-{width}-{percentage}")
    item_id = ids["book", True]
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(server["url"] + "/", wait_until="load")
    _choose(page, percentage)
    domain = _domain_snapshot(server)
    paths = (
        ("/", f'#home-recent-row [data-item-id="{item_id}"] [data-cover-presentation]'),
        ("/", '#home-continue-row [data-cover-presentation]'),
        ("/library?" + urlencode({"q": prefix, "view": "items"}), f'.media-card[data-item-id="{item_id}"] [data-cover-presentation]'),
        ("/library?" + urlencode({"q": prefix, "view": "groups"}), f'.media-card[data-item-id="{item_id}"] [data-cover-presentation]'),
        ("/browse", f'a[data-item-id="{item_id}"] [data-cover-presentation]'),
        ("/activity", '[data-testid="activity-continue"] [data-cover-presentation]'),
        ("/series", f'[href="/item/{item_id}?from=series"] [data-cover-presentation]'),
    )
    baseline = None
    for path, selector in paths:
        page.goto(server["url"] + path, wait_until="load")
        page.locator("h1").first.hover()
        stage = page.locator(selector)
        expect(stage).to_have_count(1)
        caption = stage.locator(".media-card__caption")
        expect(caption).to_have_css("opacity", "0")
        style = stage.evaluate(_STYLE)
        if baseline is None:
            baseline = style
        else:
            for name, value in baseline.items():
                if isinstance(value, (float, int)):
                    assert style[name] == pytest.approx(value, abs=1), (path, name, style, baseline)
                else:
                    assert style[name] == value, (path, name, style, baseline)
        link = stage.locator("xpath=ancestor::a[1]")
        title = caption.locator("h3").text_content()
        expect(link).to_have_accessible_name(re.compile(re.escape(title)))
        link.hover()
        expect(caption).to_have_css("opacity", "1")
        page.locator("h1").first.hover()
        link.focus()
        link.press("Tab")
        page.keyboard.press("Shift+Tab")
        expect(link).to_be_focused()
        expect(caption).to_have_css("opacity", "1")
        expect(link).to_have_css("outline-style", "solid")
        assert stage.evaluate(_STYLE) == style, "Caption reveal must not resize the cover or change text size"
        expect(stage.locator("img")).to_have_css("object-fit", "contain")
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert_page_clean(page)
    assert _domain_snapshot(server) == domain


@pytest.mark.parametrize("javascript", (False, True))
def test_recent_heading_keeps_typography_underlines_and_natively_opens_the_existing_collection(
    connected_library, browser, javascript,
):
    server = connected_library
    _seed_active(server, f"heading-link-{javascript}")
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": 375, "height": 1000}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        domain = _domain_snapshot(server)
        heading = page.locator("#home-recent-heading")
        link = heading.get_by_role("link", name="Recently added", exact=True)
        expect(link).to_have_attribute("href", "/library?reset=1&view=items&sort=recent")
        expect(page.get_by_test_id("home-recent-items").locator(".recent-shelf__all")).to_have_count(0)
        page.locator("h1").hover()
        expect(link).to_have_css("text-decoration-line", "none")
        assert link.evaluate("n => [getComputedStyle(n).fontSize, getComputedStyle(n).fontWeight, getComputedStyle(n).color]") == heading.evaluate(
            "n => [getComputedStyle(n).fontSize, getComputedStyle(n).fontWeight, getComputedStyle(n).color]",
        )
        link.hover()
        expect(link).to_have_css("text-decoration-line", "underline")
        link.focus()
        link.press("Tab")
        page.keyboard.press("Shift+Tab")
        expect(link).to_be_focused()
        expect(link).to_have_css("outline-style", "solid")
        with page.expect_navigation(wait_until="load") as navigation:
            link.press("Enter")
        assert navigation.value.status == 200
        expect(page).to_have_url(server["url"] + "/library?reset=1&view=items&sort=recent")
        expect(page.get_by_test_id("library-results")).to_be_visible()
        assert _domain_snapshot(server) == domain
        assert_page_clean(page)
