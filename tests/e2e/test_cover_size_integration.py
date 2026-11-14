"""Authenticated producer sizing with real assets, not injected component HTML.

The separate component tests cover repeated HTMX/OOB insertion. These producer
pages navigate normally; no new HTMX endpoint or account preference is implied.
"""

import base64
import sqlite3
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard, insert_item
from tests.e2e.test_page_layout import _GEOMETRY
from tests.e2e.test_responsive import _COLLAPSE


pytestmark = pytest.mark.e2e
WIDTHS = (320, 375, 640, 768, 1024, 1280, 1366, 1920)
_GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
_RATIOS = {"portrait": 2 / 3, "square": 1, "cassette": 5 / 8, "game": 3 / 4}


def _seed(server, suffix):
    prefix, ids = f"Cover integration {suffix}", {}
    for media_type in ("book", "cd", "cassette", "video_game"):
        for covered in (False, True):
            cover = f"covers/cover-integration-{suffix}-{media_type}.gif" if covered else None
            if cover:
                (server["data_dir"] / cover).write_bytes(_GIF)
            item_id = insert_item(
                server["data_dir"], media_type=media_type,
                title=f"{prefix} {media_type} {'covered' if covered else 'placeholder'} with readable long metadata",
                authors="A creator with a deliberately substantial name", cover_path=cover,
                series_name=prefix,
            )
            ids[media_type, covered] = item_id
            if media_type in {"cd", "cassette"}:
                with sqlite3.connect(server["data_dir"] / "shelf.db") as db:
                    db.execute(
                        "INSERT INTO music_releases (item_id, artist_credit, release_type) VALUES (?, ?, 'album')",
                        (item_id, "A local recording artist"),
                    )
    return prefix, ids


def _domain_snapshot(server):
    tables = ("items", "item_copies", "locations", "settings", "list_items", "reading_log",
              "user_item_state", "user_item_events")
    with sqlite3.connect(server["data_dir"] / "shelf.db") as db:
        return {table: tuple(db.execute(f"SELECT * FROM {table} ORDER BY rowid")) for table in tables}


def _measure(card):
    return card.evaluate("""element => ({
        card:element.getBoundingClientRect().width,
        host:element.parentElement.getBoundingClientRect().width,
        titleFont:getComputedStyle(element.querySelector('.media-card__title')).fontSize,
        text:element.textContent,
        columns:getComputedStyle(element.parentElement.parentElement).gridTemplateColumns
    })""")


def _assert_width(card, host, percentage):
    expected = min(host, max(80, host * percentage / 100))
    assert abs(card.bounding_box()["width"] - expected) <= 1, (host, percentage, expected)


def _resize_and_reset(page, card):
    control = page.locator("[data-cover-size-control]")
    expect(control).to_have_count(1)
    panel = page.locator("[data-cover-size-panel]")
    description = page.locator("#" + panel.get_attribute("aria-describedby"))
    expect(description).to_contain_text("not more cards per row")
    slider = control.locator("[data-cover-size-input]")
    expect(slider).to_have_value("100")
    before = _measure(card)
    _assert_width(card, before["host"], 100)
    control.locator('[data-cover-size-action="smaller"]').click()
    expect(slider).to_have_value("90")
    _assert_width(card, before["host"], 90)
    slider.focus()
    slider.press("Home")
    expect(slider).to_have_value("50")
    _assert_width(card, before["host"], 50)
    slider.press("ArrowRight")
    expect(slider).to_have_value("60")
    _assert_width(card, before["host"], 60)
    after = _measure(card)
    assert after["host"] == before["host"] and after["columns"] == before["columns"]
    assert after["text"] == before["text"] and after["titleFont"] == before["titleFont"]
    control.locator('[data-cover-size-action="reset"]').click()
    expect(slider).to_have_value("100")
    _assert_width(card, before["host"], 100)
    slider.focus()
    slider.press("End")
    expect(slider).to_have_value("100")


@pytest.mark.parametrize("width", WIDTHS)
def test_actual_home_library_items_groups_and_music_use_the_floor_limited_curve(
    authed_page, live_server, width,
):
    page = authed_page
    prefix, ids = _seed(live_server, width)
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    paths = (
        ("/", ids["video_game", True]),
        ("/library?" + urlencode({"reset": "1", "view": "items", "q": prefix}), ids["book", True]),
        ("/library?" + urlencode({"reset": "1", "view": "groups", "q": prefix}), ids["book", True]),
        ("/music", ids["cd", True]),
    )
    for path, item_id in paths:
        page.goto(live_server["url"] + path, wait_until="load")
        card = page.locator(f'.media-card[data-item-id="{item_id}"]')
        expect(card).to_have_count(1)
        url, domain = page.url, _domain_snapshot(live_server)
        storage = page.evaluate("() => [JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie]")
        requests = []

        def non_artwork_request(request):
            if not request.url.startswith((live_server["url"] + "/static/", live_server["url"] + "/covers/")):
                requests.append((request.method, request.url))

        page.on("request", non_artwork_request)
        try:
            _resize_and_reset(page, card)
            assert page.url == url and requests == []
            assert _domain_snapshot(live_server) == domain
            assert page.evaluate("() => [JSON.stringify(localStorage), JSON.stringify(sessionStorage), document.cookie]") == storage
        finally:
            page.remove_listener("request", non_artwork_request)
        geometry = page.evaluate(_GEOMETRY)
        assert abs(geometry["page"] - geometry["content"]) <= 1, geometry
        assert abs(geometry["pageLeft"] - geometry["contentLeft"]) <= 1, geometry
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert page.evaluate(_COLLAPSE) == [], "Retain the unchanged 80px text-control content floor"
        for covered in (False, True):
            sample_id = ids["cd" if path == "/music" else "book", covered]
            artwork = page.locator(f'.media-card[data-item-id="{sample_id}"] .media-artwork')
            box = artwork.bounding_box()
            assert abs(box["width"] / box["height"] - _RATIOS[artwork.get_attribute("data-cover-shape")]) < .01
            if covered:
                assert artwork.locator("img").evaluate("element => getComputedStyle(element).objectFit") == "contain"
            else:
                expect(artwork.locator(".media-artwork__placeholder")).to_have_count(1)
        assert_page_clean(page)


def test_real_shelf_stays_ten_rem_while_a_narrow_grid_card_reaches_the_readable_floor(authed_page, live_server):
    page = authed_page
    prefix, ids = _seed(live_server, "floor")
    page.set_viewport_size({"width": 320, "height": 900})
    page.goto(live_server["url"] + "/", wait_until="load")
    card = page.locator(f'.media-card[data-item-id="{ids["book", True]}"]')
    slot = card.locator("..")
    rem = page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
    assert abs(slot.bounding_box()["width"] - 10 * rem) <= 1
    page.locator("[data-cover-size-input]").press("Home")
    assert abs(slot.bounding_box()["width"] - 10 * rem) <= 1
    _assert_width(card, 10 * rem, 50)
    # Library deliberately has one comfortable column on a phone. Music's real
    # two-column grid supplies the sub-160px host needed for this floor control.
    page.goto(live_server["url"] + "/music", wait_until="load")
    card = page.locator(f'.media-card[data-item-id="{ids["cd", True]}"]')
    host = _measure(card)["host"]
    assert 80 < host < 160, host
    page.locator("[data-cover-size-input]").press("Home")
    assert abs(card.bounding_box()["width"] - 80) <= 1
    assert_page_clean(page)


def test_document_size_resets_on_real_reload_and_series_hides_the_inapplicable_control(authed_page, live_server):
    page = authed_page
    _seed(live_server, "reload")
    page.goto(live_server["url"] + "/", wait_until="load")
    page.locator("[data-cover-size-input]").press("Home")
    expect(page.locator("[data-cover-size-input]")).to_have_value("50")
    page.reload(wait_until="load")
    expect(page.locator("[data-cover-size-input]")).to_have_value("100")
    page.goto(live_server["url"] + "/library?reset=1&view=series", wait_until="load")
    expect(page.locator("[data-cover-size-control]")).to_have_count(0)
    expect(page.locator("[data-cover-size-panel]")).to_have_count(0)
    assert_page_clean(page)


def test_real_local_account_switch_starts_at_default_without_copying_the_prior_document_choice(authed_page, live_server):
    page = authed_page
    csrf = next(cookie["value"] for cookie in page.context.cookies() if cookie["name"] == "csrf_token")
    response = page.request.post(
        live_server["url"] + "/api/users",
        form={"username": "cover-size-viewer", "password": "cover-size-fixture-password", "role": "viewer"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status == 200 and response.json()["ok"] is True
    page.goto(live_server["url"] + "/", wait_until="load")
    page.locator("[data-cover-size-input]").press("Home")
    expect(page.locator("[data-cover-size-input]")).to_have_value("50")
    response = page.request.post(live_server["url"] + "/logout", headers={"X-CSRF-Token": csrf}, max_redirects=0)
    assert response.status == 303
    page.goto(live_server["url"] + "/login", wait_until="load")
    page.fill("input[name=username]", "cover-size-viewer")
    page.fill("input[name=password]", "cover-size-fixture-password")
    page.click("button[type=submit]")
    page.wait_for_url(live_server["url"] + "/")
    expect(page.locator("[data-cover-size-input]")).to_have_value("100")
    assert page.evaluate("document.documentElement.style.getPropertyValue('--media-cover-scale')") == "1"
    assert_page_clean(page)


def test_real_no_js_session_has_labelled_disabled_controls_and_unchanged_native_cards(
    authed_page, live_server, browser,
):
    _, ids = _seed(live_server, "no-js")
    context = browser.new_context(java_script_enabled=False, service_workers="block",
                                  viewport={"width": 375, "height": 900})
    context.add_cookies(authed_page.context.cookies())
    page = attach_page_guard(context.new_page())
    try:
        page.goto(live_server["url"] + "/", wait_until="load")
        control = page.locator("[data-cover-size-control]")
        expect(control.get_by_role("slider", name="Cover size")).to_be_disabled()
        # Playwright intentionally excludes NOSCRIPT itself from text matching;
        # verify its actual rendered fallback paragraph, not the skipped wrapper.
        expect(control.locator("noscript p")).to_have_count(1)
        expect(control.get_by_text("Enable JavaScript to change cover size.", exact=True)).to_be_visible()
        card = page.locator(f'.media-card[data-item-id="{ids["book", True]}"]')
        _assert_width(card, _measure(card)["host"], 100)
        link = card.locator(".media-card__detail")
        link.focus()
        expect(link).to_be_focused()
        expect(link).to_have_attribute("href", f'/item/{ids["book", True]}')
        assert_page_clean(page)
    finally:
        context.close()
