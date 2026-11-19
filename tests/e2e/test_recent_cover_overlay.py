"""Future real-browser oracles for the requested cover revision; not yet executed."""

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean
from tests.e2e.test_recent_shelf import _open, _seed


pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("width", (375, 1366))
def test_recent_caption_is_hover_only_bottom_left_with_keyboard_equivalent(
    authed_page, live_server, request, width, tmp_path,
):
    _seed(live_server, f"hover-overlay-{width}", request)
    page = authed_page
    recent = _open(page, live_server, width)
    expect(recent.locator(".media-card")).to_have_count(24)
    expect(recent.locator("details, summary, .media-card__type")).to_have_count(0)
    card = recent.locator(".media-card").first
    link = card.locator(".media-card__detail")
    caption = card.locator(".media-card__caption")
    title = caption.locator("h3").text_content()
    creator = caption.locator(".media-card__creator").text_content()
    recent.locator("h2").hover()
    expect(caption).to_have_css("opacity", "0")
    expect(link).to_have_accessible_name(f"{title} {creator}")
    before = card.locator(".media-card__art-stage").bounding_box()
    link.hover()
    expect(caption).to_have_css("opacity", "1")
    expect(caption).to_have_css("text-align", "left")
    for sample in recent.locator(".media-card").all():
        geometry = sample.evaluate("""node => {
            const art = node.querySelector('.media-artwork').getBoundingClientRect();
            const label = node.querySelector('.media-card__caption').getBoundingClientRect();
            const image = node.querySelector('img');
            return {ax:art.x, ay:art.y, right:art.right, bottom:art.bottom,
                lx:label.x, ly:label.y, lr:label.right, lb:label.bottom,
                fit:image ? getComputedStyle(image).objectFit : null};
        }""")
        assert geometry["lx"] == pytest.approx(geometry["ax"], abs=1)
        assert geometry["lr"] == pytest.approx(geometry["right"], abs=1)
        assert geometry["lb"] == pytest.approx(geometry["bottom"], abs=1)
        assert geometry["ay"] <= geometry["ly"] < geometry["bottom"]
        assert geometry["fit"] in (None, "contain")
    assert card.locator(".media-card__art-stage").bounding_box() == before
    recent.screenshot(path=str(tmp_path / f"recent-cover-hover-{width}.png"))
    recent.locator("h2").hover()
    expect(caption).to_have_css("opacity", "0")
    link.focus()
    expect(caption).to_have_css("opacity", "1")
    link.press("Tab")
    expect(caption).to_have_css("opacity", "0")
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert_page_clean(page)


@pytest.mark.parametrize("width", (375, 1366))
def test_slider_only_size_control_centers_100_and_can_shrink_grow_and_return(
    authed_page, live_server, request, width,
):
    _seed(live_server, f"centered-size-{width}", request)
    page = authed_page
    recent = _open(page, live_server, width)
    slider = page.locator("[data-cover-size-input]")
    output = page.locator("[data-cover-size-output]")
    stage = recent.locator(".media-card__art-stage").first
    expect(page.locator("[data-cover-size-control] button")).to_have_count(0)
    expect(slider).to_be_enabled()
    expect(slider).to_have_value("5")
    expect(slider).to_have_attribute("min", "0")
    expect(slider).to_have_attribute("max", "10")
    expect(slider).to_have_attribute("aria-valuetext", "100%")
    expect(output).to_have_text("100%")
    normal_width = stage.bounding_box()["width"]
    slider.press("Home")
    expect(slider).to_have_value("0")
    expect(output).to_have_text("50%")
    assert stage.bounding_box()["width"] < normal_width
    for _ in range(5):
        slider.press("ArrowRight")
    expect(slider).to_have_value("5")
    expect(output).to_have_text("100%")
    assert stage.bounding_box()["width"] == pytest.approx(normal_width, abs=1)
    slider.press("End")
    expect(slider).to_have_value("10")
    expect(slider).to_have_attribute("aria-valuetext", "200%")
    expect(output).to_have_text("200%")
    assert stage.bounding_box()["width"] > normal_width
    for _ in range(5):
        slider.press("ArrowLeft")
    expect(slider).to_have_value("5")
    expect(output).to_have_text("100%")
    assert stage.bounding_box()["width"] == pytest.approx(normal_width, abs=1)
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert_page_clean(page)
