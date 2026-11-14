"""Minimum-size producer geometry before Reset, including the actual Library grid."""

from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean
from tests.e2e.test_cover_size_integration import _RATIOS, _assert_width, _seed
from tests.e2e.test_responsive import _COLLAPSE


pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("width,columns", ((320, 1), (768, 2), (1366, 3)))
@pytest.mark.parametrize("view", ("items", "groups"))
def test_actual_library_grid_and_artwork_retain_geometry_at_fifty_percent_before_reset(
    authed_page, live_server, width, columns, view,
):
    page = authed_page
    prefix, ids = _seed(live_server, f"minimum-{width}-{view}")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(live_server["url"] + "/library?" + urlencode({"reset": "1", "view": view, "q": prefix}), wait_until="load")
    card = page.locator(f'.media-card[data-item-id="{ids["book", True]}"]')
    grid = card.locator("xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' grid ')][1]")
    expect(grid).to_have_count(1)
    measure = """e => {const style=getComputedStyle(e); return {
        display:style.display, columns:style.gridTemplateColumns, width:e.getBoundingClientRect().width
    }}"""
    before = grid.evaluate(measure)
    assert before["display"] == "grid" and len(before["columns"].split()) == columns, before
    host = card.evaluate("e => e.parentElement.getBoundingClientRect().width")
    title = card.locator(".media-card__title")
    title_text = title.text_content()
    title_font = title.evaluate("e => getComputedStyle(e).fontSize")
    slider = page.locator("[data-cover-size-input]")
    slider.press("Home")
    expect(slider).to_have_value("50")
    _assert_width(card, host, 50)
    assert grid.evaluate(measure) == before
    assert title.text_content() == title_text and title.evaluate("e => getComputedStyle(e).fontSize") == title_font
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert page.evaluate(_COLLAPSE) == []
    for media_type in ("book", "cd", "cassette", "video_game"):
        for covered in (False, True):
            artwork = page.locator(f'.media-card[data-item-id="{ids[media_type, covered]}"] .media-artwork')
            box = artwork.bounding_box()
            assert abs(box["width"] / box["height"] - _RATIOS[artwork.get_attribute("data-cover-shape")]) < .01
            if covered:
                expect(artwork.locator("img")).to_be_visible()
                assert artwork.locator("img").evaluate("e => getComputedStyle(e).objectFit") == "contain"
            else:
                expect(artwork.locator(".media-artwork__placeholder")).to_be_visible()
    # Nothing above can be satisfied by the reset/default geometry.
    expect(slider).to_have_value("50")
    assert_page_clean(page)
