"""Future fresh-synthetic icon-control journeys; authored, not executed here.

The shared browser fixture uses Chromium. Native-clear coordinates below target
that engine, not every browser. Enlarged-text/RTL probes are layout stresses, not
locale/AT qualification. Real history navigation is not guaranteed BFCache use.
"""

from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean
from tests.e2e.test_connected_library import _browser_page, _db, _item, _login, connected_library  # noqa: F401
from tests.e2e.test_cover_size_history import _ready
from tests.e2e.test_cover_size_integration import _domain_snapshot
from tests.e2e.test_navigation_search import _nav, _snapshot, _submit
from tests.e2e.test_shared_cover_size_setting import _choose, _key, _seed_active


pytestmark = pytest.mark.e2e


def _clipped(locator):
    expect(locator).to_have_css("clip-path", "inset(50%)")
    expect(locator).to_have_css("position", "absolute")
    assert locator.get_attribute("aria-hidden") != "true"
    box = locator.bounding_box()
    assert box and box["width"] <= 1 and box["height"] <= 1


def _icon_submit(search):
    button = search.get_by_role("button", name="Search", exact=True)
    expect(button).to_have_attribute("type", "submit")
    expect(button).to_have_attribute("title", "Search")
    expect(button.locator("svg")).to_have_attribute("aria-hidden", "true")
    assert button.inner_text().strip() == ""
    assert button.evaluate("node => node.parentElement.classList.contains('media-navigation__search-field')")
    return button


@pytest.mark.parametrize("width", [320, 1366])
def test_decorative_cover_icons_leave_native_sizing_storage_and_real_history_usable(connected_library, width):
    server, page = connected_library, connected_library["page"]
    _seed_active(server, f"icon-size-{width}")
    page.set_viewport_size({"width": width, "height": 1100})
    page.goto(server["url"] + "/", wait_until="load")
    domain = _domain_snapshot(server)
    control = page.locator("[data-cover-size-control]")
    slider = control.get_by_role("slider", name="Item size", exact=True)
    expect(slider).to_be_enabled()
    _clipped(control.locator("label > .cover-size-control__text"))
    _clipped(control.locator("[data-cover-size-output]"))
    assert slider.evaluate("node => node.labels.length === 1 && node.labels[0].htmlFor === node.id")
    assert control.locator("output").get_attribute("for") == slider.get_attribute("id")
    before = page.evaluate("() => ({local:{...localStorage}, session:{...sessionStorage}, cookie:document.cookie})")
    requests = []
    def record(request):
        if not urlsplit(request.url).path.startswith(("/static/", "/covers/")):
            requests.append((request.method, request.url))
    page.on("request", record)
    try:
        for value in ("smaller", "larger"):
            icon = control.locator(f'[data-cover-size-icon="{value}"]')
            expect(icon).to_be_visible()
            assert icon.evaluate("node => !node.closest('label,button,a') && node.tabIndex < 0")
            icon.click()
            expect(slider).to_have_value("5")
        assert page.evaluate("() => ({local:{...localStorage}, session:{...sessionStorage}, cookie:document.cookie})") == before
        for percentage in (50, 100, 200):
            _choose(page, percentage)
            _clipped(control.locator("[data-cover-size-output]"))
            expect(control.locator("[data-cover-size-status]")).to_be_visible()
            assert page.evaluate("key => localStorage.getItem(key)", _key(page)) == str(percentage)
        assert requests == []
    finally:
        page.remove_listener("request", record)
    stored = page.evaluate("() => ({local:{...localStorage}, session:{...sessionStorage}, cookie:document.cookie})")
    assert stored == {**before, "local": {**before["local"], _key(page): "200"}}
    small = control.locator('[data-cover-size-icon="smaller"] rect').bounding_box()
    large = control.locator('[data-cover-size-icon="larger"] rect').bounding_box()
    assert small and large and small["width"] < large["width"] and small["height"] < large["height"]
    page.goto(server["url"] + "/library?view=items", wait_until="load")
    expect(page.locator("[data-cover-size-input]")).to_have_attribute("aria-valuetext", "200%")
    _choose(page, 140)
    page.go_back()
    _ready(page)
    expect(page.locator("[data-cover-size-output]")).to_have_text("140%")
    _clipped(page.locator("[data-cover-size-output]"))
    _choose(page, 60)
    page.go_forward()
    _ready(page)
    expect(page.locator("[data-cover-size-output]")).to_have_text("60%")
    page.reload(wait_until="load")
    expect(page.get_by_role("slider", name="Item size", exact=True)).to_have_attribute("aria-valuetext", "60%")
    assert _domain_snapshot(server) == domain
    assert_page_clean(page)


@pytest.mark.parametrize("javascript,enter", [(True, False), (False, False), (True, True), (False, True)])
def test_infield_magnifier_or_enter_natively_submits_the_home_get_search(connected_library, browser, javascript, enter):
    server = connected_library
    with _db(server) as db:
        first = _item(db, "Icon Harbor 星 print")
        second = _item(db, "Icon Harbor 星 digital", "ebook")
        other = _item(db, "Other catalogue entry")
    domain = _snapshot(server)
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": 375, "height": 1100}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        search, query = _nav(page)
        button = _icon_submit(search)
        assert query.evaluate("node => node.form === node.closest('[data-navigation-search]')")
        assert button.evaluate("node => node.form === node.closest('[data-navigation-search]')")
        query.fill("Icon Harbor 星")
        _submit(page, search, query, enter=enter)
        assert urlsplit(page.url).path == "/library"
        assert parse_qs(urlsplit(page.url).query) == {"reset": ["1"], "view": ["items"], "sort": ["recent"], "q": ["Icon Harbor 星"]}
        results = page.get_by_test_id("library-results")
        for item_id in (first, second):
            expect(results.locator(f'[data-library-member="{item_id}"]')).to_have_count(1)
        expect(results.locator(f'[data-library-member="{other}"]')).to_have_count(0)
        assert _snapshot(server) == domain
        assert_page_clean(page)


@pytest.mark.parametrize("javascript", [False, True])
def test_wrapped_magnifier_keeps_external_form_filters_saved_view_and_empty_override(connected_library, browser, javascript):
    server, setup = connected_library, connected_library["page"]
    saved = {"name": "Icon saved view", "q": "Icon Harbor", "types": "book,ebook", "family": "book",
             "view": "items", "sort": "recent", "page_size": "48"}
    response = setup.request.post(server["url"] + "/api/library/views", form=saved,
                                  headers={"X-CSRF-Token": setup.evaluate("window.csrfToken()")}, max_redirects=0)
    assert response.status == 303
    with _db(server) as db:
        view_id = db.execute("SELECT id FROM library_saved_views WHERE name = 'Icon saved view'").fetchone()[0]
    domain = _snapshot(server)
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": 375, "height": 1100}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        page.goto(server["url"] + "/library?" + urlencode({"saved_view": view_id, "page": 2}))
        for text, enter in (("", False), ("Icon Harbor 星", True)):
            search, query = _nav(page)
            button = _icon_submit(search)
            expect(query).to_have_attribute("form", "library-filters")
            expect(button).to_have_attribute("form", "library-filters")
            assert query.evaluate("node => node.form.id") == button.evaluate("node => node.form.id") == "library-filters"
            expect(page.locator('input[name="q"]')).to_have_count(1)
            expect(page.locator('#library-filters input[name="q"]')).to_have_count(0)
            query.fill(text)
            assert page.locator("#library-filters").evaluate("form => new FormData(form).getAll('q')") == [text]
            _submit(page, search, query, enter=enter)
            params = parse_qs(urlsplit(page.url).query, keep_blank_values=True)
            for key in ("types", "family", "view", "sort", "page_size"):
                assert params[key] == [saved[key]]
            assert params["saved_view"] == [str(view_id)] and params["q"] == [text] and "page" not in params
        search, query = _nav(page)
        query.fill("Icon Harbor")
        page.get_by_test_id("library-filter-options").locator(":scope > summary").click()
        with page.expect_navigation(wait_until="domcontentloaded") as navigation:
            page.get_by_role("button", name="Apply filters", exact=True).click()
        assert navigation.value.status == 200
        assert parse_qs(urlsplit(page.url).query)["q"] == ["Icon Harbor"]
        assert _snapshot(server) == domain
        assert_page_clean(page)


@pytest.mark.parametrize("width,enlarged,rtl", [(320, False, False), (320, True, False), (320, True, True), (1024, False, False), (1366, False, False)])
def test_icon_layout_reserves_text_space_targets_and_focus_at_narrow_large_text_and_rtl_sizes(connected_library, width, enlarged, rtl):
    server, page = connected_library, connected_library["page"]
    page.set_viewport_size({"width": width, "height": 1200})
    page.goto(server["url"] + "/", wait_until="load")
    page.evaluate("""({enlarged, rtl}) => {
        document.documentElement.dir = rtl ? 'rtl' : 'ltr';
        if (enlarged) document.documentElement.style.fontSize = '200%';
        const search = document.querySelector('[data-navigation-search]');
        search.querySelector('label').textContent = 'Search the complete local catalogue and connected library';
        search.querySelector('button').setAttribute('aria-label', 'Search the complete local catalogue');
        document.querySelector('.cover-size-control__slider > span').textContent = 'Size of covers throughout the catalogue';
    }""", {"enlarged": enlarged, "rtl": rtl})
    search = page.locator("[data-navigation-search]")
    query, button = search.locator('input[type="search"]'), search.locator('button[type="submit"]')
    query.fill("A long visible query 星 & letters that must not overlap the magnifier")
    query.focus()
    expect(query).to_be_focused()
    query.press("Tab")
    expect(button).to_be_focused()
    assert button.evaluate("node => getComputedStyle(node).outlineStyle") != "none"
    assert query.evaluate("node => !node.closest('details') && !!node.closest('[data-media-navigation]')")
    input_box, button_box = query.bounding_box(), button.bounding_box()
    assert input_box and button_box and button_box["width"] >= 44 and button_box["height"] >= 44
    assert input_box["x"] <= button_box["x"] and button_box["x"] + button_box["width"] <= input_box["x"] + input_box["width"]
    assert input_box["y"] <= button_box["y"] and button_box["y"] + button_box["height"] <= input_box["y"] + input_box["height"]
    padding = query.evaluate("node => parseFloat(getComputedStyle(node).paddingInlineStart)")
    assert padding >= button_box["width"] + 1
    if rtl:
        assert button_box["x"] + button_box["width"] == pytest.approx(input_box["x"] + input_box["width"] - 1, abs=1)
    else:
        assert button_box["x"] == pytest.approx(input_box["x"] + 1, abs=1)
    control = page.locator("[data-cover-size-control]")
    _clipped(control.locator("label > .cover-size-control__text"))
    _clipped(control.locator("output"))
    slider = control.locator('input[type="range"]')
    slider.focus()
    expect(slider).to_be_focused()
    for locator in (query, button, slider, control.locator('[data-cover-size-icon="smaller"]'), control.locator('[data-cover-size-icon="larger"]')):
        box = locator.bounding_box()
        assert box and box["x"] >= -1 and box["x"] + box["width"] <= width + 1
    assert slider.bounding_box()["height"] >= 44
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert_page_clean(page)


@pytest.mark.parametrize("rtl", [False, True])
def test_chromium_native_search_clear_remains_at_the_opposite_end_from_the_magnifier(connected_library, rtl):
    server, page = connected_library, connected_library["page"]
    page.set_viewport_size({"width": 375, "height": 1100})
    page.goto(server["url"] + "/", wait_until="load")
    page.evaluate("rtl => document.documentElement.dir = rtl ? 'rtl' : 'ltr'", rtl)
    search, query = _nav(page)
    _icon_submit(search)
    query.fill("Clear this native search field")
    query.focus()
    original, domain = page.url, _snapshot(server)
    position = query.evaluate("""node => {
        const style = getComputedStyle(node), box = node.getBoundingClientRect();
        return {x: style.direction === 'rtl' ? parseFloat(style.paddingLeft) + 9 :
                     box.width - parseFloat(style.paddingRight) - 9, y: box.height / 2};
    }""")
    query.click(position=position)  # Real pointer click on Chromium's native clear.
    expect(query).to_have_value("")
    assert page.url == original and _snapshot(server) == domain
    expect(query).to_be_focused()
    assert_page_clean(page)


@pytest.mark.parametrize("javascript", [False, True])
def test_both_home_hints_are_removed_but_rows_still_scroll_and_nojs_guidance_survives(connected_library, browser, javascript):
    server, setup = connected_library, connected_library["page"]
    with _db(server) as db:
        ids = [_item(db, f"Icon shelf {number:02d}") for number in range(30)]
    response = setup.request.post(server["url"] + f"/api/library/items/{ids[-1]}/personal",
                                  form={"status": "in_progress", "progress": "17"},
                                  headers={"X-CSRF-Token": setup.evaluate("window.csrfToken()")}, max_redirects=0)
    assert response.status == 303
    domain = _snapshot(server)
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": 320, "height": 1100}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        expect(page.get_by_text("Swipe or scroll to explore.", exact=True)).to_have_count(0)
        recent, continuing = page.locator('[data-recent-shelf="streaming-v1"]'), page.get_by_test_id("home-personal-library")
        recent_cards = page.locator("#home-recent-row article[data-item-id]")
        expect(recent_cards).to_have_count(24 if javascript else 8)
        assert [int(node.get_attribute("data-item-id")) for node in recent_cards.all()] == list(reversed(ids))[:24 if javascript else 8]
        expect(page.locator("#home-continue-row article[data-item-id]")).to_have_attribute("data-item-id", str(ids[-1]))
        expect(continuing.get_by_role("link", name="Connect or refresh digital Continue", exact=True)).to_be_visible()
        for shell, selector in ((recent, "#home-recent-row"), (continuing, "#home-continue-row")):
            row = page.locator(selector)
            assert row.evaluate("node => node.scrollWidth > node.clientWidth")
            if javascript:
                next_button = shell.locator('[data-recent-action="next"]')
                expect(next_button).to_be_enabled()
                next_button.click()
            else:
                expect(shell.locator('[data-recent-action="next"]')).to_be_disabled()
                row.hover()
                page.mouse.wheel(300, 0)
            page.wait_for_function("selector => Math.abs(document.querySelector(selector).scrollLeft) > 1", arg=selector)
        if not javascript:
            expect(recent.get_by_text("Select Recently added to open the complete collection without JavaScript.", exact=True)).to_be_visible()
            expect(page.get_by_role("slider", name="Item size", exact=True)).to_be_disabled()
            expect(page.get_by_text("Enable JavaScript to change item size.", exact=True)).to_be_visible()
        assert _snapshot(server) == domain
        assert_page_clean(page)
