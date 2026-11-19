"""Future native-browser checks on fresh synthetic servers; not yet executed."""

import sqlite3
from urllib.parse import quote, urlencode, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean, attach_page_guard
from tests.e2e.test_connected_library import _add_user, _login, connected_library
from tests.e2e.test_cover_size_integration import _domain_snapshot, _seed


pytestmark = pytest.mark.e2e
PERCENTAGES = (50, 60, 70, 80, 90, 100, 120, 140, 160, 180, 200)


def _choose(page, percentage):
    slider = page.locator("[data-cover-size-input]")
    expect(slider).to_have_count(1)
    expect(slider).to_be_enabled()
    slider.press("Home")
    for _ in range(PERCENTAGES.index(percentage)):
        slider.press("ArrowRight")
    expect(slider).to_have_value(str(PERCENTAGES.index(percentage)))
    expect(slider).to_have_attribute("aria-valuetext", f"{percentage}%")
    expect(page.locator("[data-cover-size-output]")).to_have_text(f"{percentage}%")


def _key(page):
    shell = page.locator("html")
    mount = quote(shell.get_attribute("data-cover-size-mount"), safe="~()*!.'-")
    return f"shelf.coverSize.v2:{mount}:u:{shell.get_attribute('data-cover-size-account')}"


def _logout(page, server):
    page.get_by_test_id("account-menu-button").click()
    page.get_by_test_id("account-menu-logout").click()
    expect(page).to_have_url(server["url"] + "/login")


def _seed_active(server, suffix):
    prefix, ids = _seed(server, suffix)
    # Explicit synthetic fixture state only; ordinary GET/size changes are
    # compared with a snapshot taken AFTER this setup. No production resources.
    with sqlite3.connect(server["data_dir"] / "shelf.db") as db:
        uid = db.execute("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)).fetchone()[0]
        db.execute("INSERT INTO user_item_state (user_id, item_id, status, progress, on_list) "
                   "VALUES (?, ?, 'in_progress', 0, 1)", (uid, ids["book", True]))
    return prefix, ids


@pytest.mark.parametrize("width", (320, 375, 1366))
@pytest.mark.parametrize("percentage", (50, 100, 200))
def test_native_choice_persists_across_real_pages_with_the_same_cover_geometry(
    connected_library, width, percentage,
):
    server, page = connected_library, connected_library["page"]
    prefix, ids = _seed_active(server, f"shared-size-{width}-{percentage}")
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(server["url"] + "/", wait_until="load")
    domain = _domain_snapshot(server)
    storage_before = page.evaluate("() => ({local:{...localStorage}, session:{...sessionStorage}, cookie:document.cookie})")
    requests = []

    def unexpected_request(request):
        if not urlsplit(request.url).path.startswith(("/static/", "/covers/")):
            requests.append((request.method, request.url))

    page.on("request", unexpected_request)
    try:
        _choose(page, percentage)
        assert requests == []
        stored = page.evaluate("() => ({local:{...localStorage}, session:{...sessionStorage}, cookie:document.cookie})")
        assert stored == {**storage_before, "local": {**storage_before["local"], _key(page): str(percentage)}}
    finally:
        page.remove_listener("request", unexpected_request)

    paths = (
        ("/", f'#home-recent-row .media-card[data-item-id="{ids["book", True]}"] .media-card__art-stage'),
        ("/", "#home-continue-row .media-card__art-stage"),
        ("/library?" + urlencode({"q": prefix, "view": "items"}), f'.media-card[data-item-id="{ids["book", True]}"] .media-card__art-stage'),
        ("/library?" + urlencode({"q": prefix, "view": "groups"}), f'.media-card[data-item-id="{ids["book", True]}"] .media-card__art-stage'),
        ("/library?" + urlencode({"q": prefix, "view": "series"}), '[data-library-series] [data-cover-size-stage="catalogue-v2"]'),
        ("/music", f'.media-card[data-item-id="{ids["cd", True]}"] .media-card__art-stage'),
        ("/browse", f'a[data-item-id="{ids["book", True]}"] .media-card__art-stage'),
        ("/activity", '[data-testid="activity-continue"] [data-cover-size-stage="catalogue-v2"]'),
        ("/series", f'[href="/item/{ids["book", True]}?from=series"] [data-cover-size-stage="catalogue-v2"]'),
    )
    frames = []
    for path, selector in paths:
        page.goto(server["url"] + path, wait_until="load")
        expect(page.locator("[data-cover-size-output]")).to_have_text(f"{percentage}%")
        toolbar = page.locator("[data-media-display-tools]")
        expect(toolbar.locator("[data-cover-size-control]")).to_have_count(1)
        assert toolbar.bounding_box()["y"] < page.locator("main").bounding_box()["y"]
        stage = page.locator(selector)
        expect(stage).to_have_count(1)
        frame = stage.bounding_box()
        rem = page.locator("html").evaluate("node => parseFloat(getComputedStyle(node).fontSize)")
        expected = max(80, min(10 * rem * percentage / 100, width - 6 * rem))
        assert frame["width"] == pytest.approx(expected, abs=1), (path, frame)
        frames.append(frame["width"])
        artwork = stage.locator("[data-cover-shape]").first
        shape = artwork.get_attribute("data-cover-shape")
        box = artwork.bounding_box()
        assert box["width"] / box["height"] == pytest.approx(
            {"portrait": 2 / 3, "square": 1, "cassette": 5 / 8, "game": 3 / 4}[shape], abs=.01,
        )
        for image in artwork.locator("img").all():
            expect(image).to_have_css("object-fit", "contain")
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert_page_clean(page)
    assert max(frames) - min(frames) <= 1
    page.reload(wait_until="load")
    expect(page.locator("[data-cover-size-output]")).to_have_text(f"{percentage}%")
    assert _domain_snapshot(server) == domain


def test_real_same_account_tabs_update_without_navigation(connected_library):
    server, first = connected_library, connected_library["page"]
    second = attach_page_guard(first.context.new_page())
    try:
        second.goto(server["url"] + "/library", wait_until="load")
        _choose(first, 200)
        expect(second.locator("[data-cover-size-output]")).to_have_text("200%")
        _choose(second, 140)
        expect(first.locator("[data-cover-size-output]")).to_have_text("140%")
        assert_page_clean(second)
    finally:
        second.close()


@pytest.mark.parametrize("percentage", (50, 100, 200))
def test_browse_list_covered_and_placeholder_artwork_match_home_width(connected_library, percentage):
    server, page = connected_library, connected_library["page"]
    _, ids = _seed_active(server, f"shared-list-{percentage}")
    page.set_viewport_size({"width": 375, "height": 1000})
    page.goto(server["url"] + "/", wait_until="load")
    _choose(page, percentage)
    home = page.locator(f'#home-recent-row .media-card[data-item-id="{ids["book", True]}"] .media-artwork')
    expected = home.bounding_box()["width"]
    domain = _domain_snapshot(server)
    page.goto(server["url"] + "/browse", wait_until="load")
    page.get_by_test_id("view-list").click()
    expect(page.locator("[data-cover-size-output]")).to_have_text(f"{percentage}%")
    for covered in (False, True):
        artwork = page.locator(f'tr[data-item-id="{ids["book", covered]}"] [data-cover-size-artwork="catalogue-v2"]')
        expect(artwork).to_be_visible()
        assert artwork.bounding_box()["width"] == pytest.approx(expected, abs=1)
    assert _domain_snapshot(server) == domain
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert_page_clean(page)


def test_native_account_switching_keeps_distinct_nondefault_values(connected_library):
    server, page = connected_library, connected_library["page"]
    username, password = "synthetic-cover-viewer", "synthetic-cover-password-2026"
    _add_user(page, server, username, password)
    page.goto(server["url"] + "/", wait_until="load")
    _choose(page, 200)
    first_key = _key(page)
    _logout(page, server)
    _login(page, server, username, password)
    expect(page.locator("[data-cover-size-output]")).to_have_text("100%")
    second_key = _key(page)
    assert first_key != second_key
    _choose(page, 50)
    _logout(page, server)
    _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
    expect(page.locator("[data-cover-size-output]")).to_have_text("200%")
    assert page.evaluate("keys => keys.map(key => localStorage.getItem(key))", [first_key, second_key]) == ["200", "50"]


def test_denied_size_storage_still_allows_native_current_page_changes(connected_library):
    server, page = connected_library, connected_library["page"]
    page.add_init_script("""(() => {
        const get = Storage.prototype.getItem, set = Storage.prototype.setItem;
        Storage.prototype.getItem = function (key) {
            if (String(key).startsWith('shelf.coverSize.v2:')) throw new DOMException('synthetic refusal', 'SecurityError');
            return get.call(this, key);
        };
        Storage.prototype.setItem = function (key, value) {
            if (String(key).startsWith('shelf.coverSize.v2:')) throw new DOMException('synthetic refusal', 'QuotaExceededError');
            return set.call(this, key, value);
        };
    })();""")
    page.goto(server["url"] + "/", wait_until="load")
    _choose(page, 50)
    expect(page.locator("[data-cover-size-status]")).to_contain_text("this page only")
    page.goto(server["url"] + "/library", wait_until="load")
    expect(page.locator("[data-cover-size-output]")).to_have_text("100%")
    assert_page_clean(page)


def test_print_geometry_does_not_consume_the_saved_screen_scale(connected_library):
    server, page = connected_library, connected_library["page"]
    _seed_active(server, "shared-print")
    page.goto(server["url"] + "/", wait_until="load")
    _choose(page, 100)
    page.emulate_media(media="print")
    stage = page.locator("#home-recent-row .media-card__art-stage").first
    original = stage.bounding_box()
    page.emulate_media(media="screen")
    _choose(page, 200)
    page.emulate_media(media="print")
    assert stage.bounding_box() == original
    expect(page.locator("[data-media-display-tools]")).to_be_hidden()
    page.emulate_media(media="screen")
