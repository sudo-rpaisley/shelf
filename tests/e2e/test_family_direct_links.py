"""Future fresh-synthetic family link/arrow journeys; authored, not run here.

Real input/navigation are required. Long-label/RTL/200% probes are layout stresses,
not qualified translation or assistive-technology evidence. Existing fixtures and
protected assertions remain unchanged; separate runtime/resource approval needed.
"""

from urllib.parse import parse_qs, urljoin, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean, attach_page_guard
from tests.e2e.test_connected_library import _browser_page, _db, _item, _login, connected_library  # noqa: F401
from tests.e2e.test_navigation_search import _snapshot


pytestmark = pytest.mark.e2e


def _configure(server):
    page = server["page"]
    token = page.evaluate("window.csrfToken()")
    for feature in ("connected_library", "music", "periodicals"):
        result = page.request.post(server["url"] + "/api/settings/features/" + feature,
                                   form={"enabled": "1"}, headers={"X-CSRF-Token": token})
        assert result.ok


def _seed(server):
    from app import media_presentation

    result = {}
    with _db(server) as db:
        for family in media_presentation.family_nodes():
            result[family.key] = [_item(db, f"Direct family {family.key} {fmt.media_type}", fmt.media_type)
                                  for fmt in family.formats]
    return result


def _open(page):
    shell = page.locator("[data-nav-shell]")
    if shell.get_attribute("open") is None:
        page.get_by_test_id("nav-menu-button").click()
    library = page.locator("[data-nav-browse]")
    if library.get_attribute("open") is None:
        library.locator(":scope > summary").focus()
        library.locator(":scope > summary").press("Enter")
    expect(library).to_have_attribute("open", "")


@pytest.mark.parametrize("javascript,width,activation", [(True, 1366, "click"), (True, 375, "enter"), (False, 375, "click")])
def test_each_family_name_natively_opens_exact_all_items_destination_without_duplicate_entry(connected_library, browser, javascript, width, activation):
    server = connected_library
    _configure(server)
    expected = _seed(server)
    domain = _snapshot(server)
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": width, "height": 1000}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        for family, item_ids in expected.items():
            page.goto(server["url"] + "/")
            _open(page)
            branch = page.locator(f'[data-nav-family="{family}"]')
            label = branch.locator(f':scope > a[data-nav-family-link="{family}"]')
            expect(label).to_be_visible()
            expect(branch.get_by_role("link", name="All items", exact=True)).to_have_count(0)
            href = label.get_attribute("href")
            with page.expect_navigation(wait_until="domcontentloaded") as navigated:
                if activation == "click":
                    label.click()  # Desktop pointer entry may open formats first.
                else:
                    label.focus()
                    label.press("Enter")
            assert navigated.value.status == 200
            expect(page).to_have_url(urljoin(server["url"], href))
            params = parse_qs(urlsplit(page.url).query)
            assert params["reset"] == ["1"] and params["view"] == ["items"] and params["sort"] == ["recent"]
            members = page.get_by_test_id("library-results").locator("[data-library-member]")
            assert {int(node.get_attribute("data-library-member")) for node in members.all()} == set(item_ids)
            assert _snapshot(server) == domain
            assert_page_clean(page)


@pytest.mark.parametrize("javascript", [False, True])
def test_independent_native_arrow_keeps_format_selection_keyboard_and_focus_return(connected_library, browser, javascript):
    server = connected_library
    _configure(server)
    families = _seed(server)
    domain = _snapshot(server)
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": 375, "height": 1000}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        for family in families:
            page.goto(server["url"] + "/")
            _open(page)
            branch = page.locator(f'[data-nav-family="{family}"]')
            label = branch.locator(":scope > [data-nav-family-label]")
            arrow = branch.locator(":scope > details > summary")
            expect(arrow).to_have_accessible_name(label.inner_text().strip() + " Show formats")
            url = page.url
            label.focus()
            label.press("Tab")
            expect(arrow).to_be_focused()
            arrow.press("Space")
            expect(branch.locator(":scope > details")).to_have_attribute("open", "")
            assert page.url == url
            first_format = branch.locator("[data-nav-format]").first
            first_format.scroll_into_view_if_needed()
            expect(first_format).to_be_visible()
            if javascript:
                first_format.focus()
                first_format.press("Escape")
                expect(arrow).to_be_focused()
                expect(first_format).to_be_hidden()
                arrow.press("Enter")
                expect(first_format).to_be_visible()
            href = first_format.get_attribute("href")
            with page.expect_navigation(wait_until="domcontentloaded") as navigated:
                first_format.click()
            assert navigated.value.status == 200
            expect(page).to_have_url(urljoin(server["url"], href))
            assert len(parse_qs(urlsplit(page.url).query)["types"][0].split(",")) == 1
        assert _snapshot(server) == domain
        assert_page_clean(page)


def test_family_modified_click_keeps_native_new_tab_semantics(connected_library):
    server, page = connected_library, connected_library["page"]
    page.set_viewport_size({"width": 1366, "height": 1000})
    page.goto(server["url"] + "/")
    _open(page)
    label = page.locator('a[data-nav-family-link="books"]')
    href, original = label.get_attribute("href"), page.url
    domain = _snapshot(server)
    def guard(popup):
        attach_page_guard(popup)
    page.context.on("page", guard)
    popup = None
    try:
        with page.expect_popup() as opened:
            label.click(modifiers=["Control"])
        popup = opened.value
        popup.wait_for_load_state("load")
        expect(popup).to_have_url(urljoin(server["url"], href))
        assert page.url == original
        assert _snapshot(server) == domain
        assert_page_clean(popup)
        assert_page_clean(page)
    finally:
        page.context.remove_listener("page", guard)
        if popup is not None:
            popup.close()


@pytest.mark.parametrize("javascript,rtl", [(True, False), (False, False), (True, True)])
def test_wrapped_family_label_and_arrow_do_not_overlap_or_cover_inflow_phone_formats(connected_library, browser, javascript, rtl):
    server = connected_library
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": 320, "height": 1100}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        _open(page)
        page.evaluate("""rtl => {
            document.documentElement.style.fontSize='200%';
            document.documentElement.dir=rtl ? 'rtl' : 'ltr';
            document.querySelector('[data-nav-family="books"] > [data-nav-family-label]').textContent=
                rtl ? 'الكتب المطبوعة والكتب الإلكترونية والكتب الصوتية في المجموعة' :
                      'Printed books, electronic books and audiobooks across the collection';
        }""", rtl)
        branch = page.locator('[data-nav-family="books"]')
        label = branch.locator(":scope > [data-nav-family-label]")
        arrow = branch.locator(":scope > details > summary")
        arrow.click()
        formats = branch.locator(".media-navigation__formats")
        expect(formats).to_be_visible()
        label_box, arrow_box, formats_box = label.bounding_box(), arrow.bounding_box(), formats.bounding_box()
        assert label_box and arrow_box and formats_box
        assert label_box["height"] >= 44 and arrow_box["height"] >= 44 and arrow_box["width"] >= 44
        assert (label_box["x"] + label_box["width"] <= arrow_box["x"] + 1
                or arrow_box["x"] + arrow_box["width"] <= label_box["x"] + 1)
        assert formats_box["y"] >= max(label_box["y"] + label_box["height"], arrow_box["y"] + arrow_box["height"]) - 1
        for box in (label_box, arrow_box, formats_box):
            assert box["x"] >= -1 and box["x"] + box["width"] <= 321
        assert formats.evaluate("node => getComputedStyle(node).position") == "static"
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert_page_clean(page)


def test_family_arrow_context_does_not_pollute_command_palette_destinations(connected_library):
    server, page = connected_library, connected_library["page"]
    page.goto(server["url"] + "/")
    href = page.locator('a[data-nav-family-link="books"]').get_attribute("href")
    page.keyboard.press("Control+k")
    query = page.locator("[data-command-palette-query]")
    expect(query).to_be_focused()
    query.fill("Books")
    result = page.locator(f'[data-command-palette-result][href="{href}"]')
    expect(result).to_have_count(1)
    expect(result.locator("small")).to_have_text("Books")
    expect(result.locator("span")).to_have_text("Books")
    assert "Show formats" not in result.inner_text() and "All items" not in result.inner_text()
    assert_page_clean(page)
