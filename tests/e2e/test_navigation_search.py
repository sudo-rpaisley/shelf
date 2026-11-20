"""Future synthetic native nav search/form/layout journeys; source only here.

No execution is implied. Long-label/200% cases are layout stresses, not locale
or assistive-technology qualification. Existing tests/fixtures remain unchanged.
"""

from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean
from tests.e2e.test_connected_library import _browser_page, _db, _item, _login, connected_library  # noqa: F401


pytestmark = pytest.mark.e2e


def _snapshot(server):
    tables = ("items", "item_copies", "item_authors", "authors", "settings", "list_items",
              "source_instances", "source_records", "library_saved_views", "user_item_state", "user_item_events")
    with _db(server) as db:
        return {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")) for table in tables}


def _nav(page):
    search = page.locator("[data-navigation-search]")
    expect(search).to_have_count(1)
    expect(search).to_be_visible()
    return search, search.get_by_label("Search your library", exact=True)


def _submit(page, search, query, *, enter):
    query.focus()
    with page.expect_navigation(wait_until="domcontentloaded") as navigation:
        if enter:
            query.press("Enter")
        else:
            search.get_by_role("button", name="Search", exact=True).click()
    assert navigation.value.status == 200


@pytest.mark.parametrize("javascript,enter", [(True, True), (False, False)])
def test_home_nav_search_opens_full_matching_library_with_native_submission(connected_library, browser, javascript, enter):
    server = connected_library
    with _db(server) as db:
        first = _item(db, "Harbor 星 atlas in print")
        second = _item(db, "Harbor 星 atlas digital", "ebook")
        other = _item(db, "An unrelated field guide")
    domain = _snapshot(server)
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": 375, "height": 1000}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        search, query = _nav(page)
        expect(query).to_be_visible()
        assert query.get_attribute("maxlength") == "300"
        if javascript:
            expect(page.locator("[data-nav-shell]")).not_to_have_attribute("open", "")
            page.keyboard.press("/")
            expect(query).to_be_focused()
        query.fill("Harbor 星")
        _submit(page, search, query, enter=enter)
        params = parse_qs(urlsplit(page.url).query, keep_blank_values=True)
        assert urlsplit(page.url).path == "/library"
        assert params == {"reset": ["1"], "view": ["items"], "sort": ["recent"], "q": ["Harbor 星"]}
        results = page.get_by_test_id("library-results")
        expect(results.locator(f'[data-library-member="{first}"]')).to_have_count(1)
        expect(results.locator(f'[data-library-member="{second}"]')).to_have_count(1)
        expect(results.locator(f'[data-library-member="{other}"]')).to_have_count(0)
        expect(page.get_by_label("Search your library", exact=True)).to_have_value("Harbor 星")
        assert _snapshot(server) == domain
        assert_page_clean(page)


@pytest.mark.parametrize("javascript", [False, True])
def test_nav_query_enter_clear_and_apply_filters_share_one_native_saved_view_form(connected_library, browser, javascript):
    server, setup = connected_library, connected_library["page"]
    with _db(server) as db:
        _item(db, "Harbor 星 print")
        _item(db, "Harbor 星 digital", "ebook")
    saved = {"name": "Nav ownership", "q": "Harbor", "types": "book,ebook", "family": "book",
             "view": "items", "sort": "recent", "page_size": "48"}
    created = setup.request.post(server["url"] + "/api/library/views", form=saved,
                                 headers={"X-CSRF-Token": setup.evaluate("window.csrfToken()")}, max_redirects=0)
    assert created.status == 303
    with _db(server) as db:
        saved_id = db.execute("SELECT id FROM library_saved_views WHERE name = 'Nav ownership'").fetchone()[0]
    domain = _snapshot(server)
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": 1366, "height": 1000}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        page.goto(server["url"] + "/library?" + urlencode({"saved_view": saved_id, "page": 2}))
        search, query = _nav(page)
        expect(query).to_have_value("Harbor")
        expect(page.get_by_test_id("library-filters").locator('input[name="q"]')).to_have_count(0)
        expect(page.locator('main input[type="search"]')).to_have_count(0)
        expect(page.get_by_test_id("library-filter-options")).not_to_have_attribute("open", "")
        assert query.get_attribute("form") == "library-filters"
        if javascript:
            assert query.evaluate("node => node.form.id") == "library-filters"
            assert search.get_by_role("button", name="Search", exact=True).evaluate("node => node.form.id") == "library-filters"
            assert page.locator("#library-filters").evaluate("form => new FormData(form).getAll('q')") == ["Harbor"]
        query.fill("Harbor 星")
        _submit(page, search, query, enter=True)
        params = parse_qs(urlsplit(page.url).query, keep_blank_values=True)
        expected = {"saved_view": [str(saved_id)], "types": ["book,ebook"], "family": ["book"],
                    "view": ["items"], "sort": ["recent"], "page_size": ["48"], "q": ["Harbor 星"]}
        for key, value in expected.items():
            assert params[key] == value
        assert "page" not in params
        search, query = _nav(page)
        query.fill("")
        _submit(page, search, query, enter=False)
        params = parse_qs(urlsplit(page.url).query, keep_blank_values=True)
        assert params["q"] == [""] and params["saved_view"] == [str(saved_id)]
        search, query = _nav(page)
        expect(query).to_have_value("")
        page.get_by_test_id("library-filter-options").locator(":scope > summary").click()
        page.locator("#library-media-type").select_option("book")
        query.fill("Harbor")
        with page.expect_navigation(wait_until="domcontentloaded") as navigation:
            page.get_by_role("button", name="Apply filters", exact=True).click()
        assert navigation.value.status == 200
        params = parse_qs(urlsplit(page.url).query, keep_blank_values=True)
        assert params["q"] == ["Harbor"] and params["media_type"] == ["book"] and params["saved_view"] == [str(saved_id)]
        assert _snapshot(server) == domain
        assert_page_clean(page)


@pytest.mark.parametrize("width", [320, 1024, 1366])
@pytest.mark.parametrize("enlarged", [False, True])
def test_search_remains_in_nav_outside_closed_menu_with_wrapping_focus_and_touch_targets(connected_library, width, enlarged):
    server, page = connected_library, connected_library["page"]
    page.set_viewport_size({"width": width, "height": 1100})
    page.goto(server["url"] + "/library?view=items")
    search, query = _nav(page)
    if enlarged:
        page.evaluate("""() => {
            document.documentElement.style.fontSize='200%';
            const root=document.querySelector('[data-navigation-search]');
            root.querySelector('label').textContent='Search the local collection and every connected catalogue';
            root.querySelector('input[type="search"]').placeholder='Search titles, creators and identifiers in this library';
            root.querySelector('button').textContent='Search the complete local catalogue';
        }""")
        query = search.locator('input[type="search"]')
    assert query.evaluate("node => !node.closest('details') && !!node.closest('[data-media-navigation]')")
    query.focus()
    expect(query).to_be_focused()
    query.press("Tab")
    button = search.locator('button[type="submit"]')
    expect(button).to_be_focused()
    account = page.get_by_test_id("account-menu-button")
    for control in (query, button, account):
        box = control.bounding_box()
        assert box and box["height"] >= 44 and box["x"] >= -1 and box["x"] + box["width"] <= width + 1
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert_page_clean(page)


@pytest.mark.parametrize("javascript", [False, True])
def test_recent_subtitle_is_gone_but_native_eight_enhanced24_and_heading_destination_remain(connected_library, browser, javascript):
    server = connected_library
    with _db(server) as db:
        ids = [_item(db, f"Nav recent {number:02d}") for number in range(30)]
    domain = _snapshot(server)
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": 1366, "height": 1000}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        recent = page.locator('[data-recent-shelf="streaming-v1"]')
        heading = recent.locator(".recent-shelf__heading")
        expect(heading).to_have_text("Recently added")
        expect(heading.locator("p, [data-recent-count]")).to_have_count(0)
        cards = page.locator("#home-recent-row article[data-item-id]")
        expected = list(reversed(ids))[:24 if javascript else 8]
        expect(cards).to_have_count(len(expected))
        assert [int(node.get_attribute("data-item-id")) for node in cards.all()] == expected
        if javascript:
            expect(recent).to_have_attribute("data-recent-enhanced", "true")
        title = heading.get_by_role("link", name="Recently added", exact=True)
        expect(title).to_have_attribute("href", "/library?reset=1&view=items&sort=recent")
        assert _snapshot(server) == domain
        assert_page_clean(page)


def test_disabled_library_nav_search_uses_existing_browse_get_fallback(connected_library):
    server, page = connected_library, connected_library["page"]
    configured = page.request.post(server["url"] + "/api/settings/features/connected_library", form={"enabled": "0"},
                                    headers={"X-CSRF-Token": page.evaluate("window.csrfToken()")})
    assert configured.ok
    domain = _snapshot(server)
    page.goto(server["url"] + "/")
    search, query = _nav(page)
    expect(search).to_have_attribute("action", "/browse")
    expect(query).to_have_attribute("maxlength", "200")
    query.fill("A local title")
    _submit(page, search, query, enter=True)
    assert urlsplit(page.url).path == "/browse"
    assert parse_qs(urlsplit(page.url).query) == {"sort": ["newest"], "q": ["A local title"]}
    assert _snapshot(server) == domain
    assert_page_clean(page)
