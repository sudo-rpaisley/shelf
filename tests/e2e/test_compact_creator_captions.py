"""Future native compact-caption journeys; source only, never executed here."""

import pytest
from playwright.sync_api import expect

from app.services import author_index
from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean
from tests.e2e.test_connected_library import _browser_page, _db, _item, _login, connected_library  # noqa: F401
from tests.e2e.test_cover_size_integration import _domain_snapshot
from tests.e2e.test_shared_cover_size_setting import _choose


pytestmark = pytest.mark.e2e
CREDIT = "Lara Elena Donnelly, French Carlomagno, Pius Bak, Samantha Dodge, Fifth Creator, Sixth Creator"


@pytest.mark.parametrize("javascript,width,percentage", [(True, 320, 100), (True, 1366, 200), (False, 375, 100)])
def test_long_author_list_is_one_line_with_ellipsis_and_no_caption_scroll_or_hidden_tab_stops(connected_library, browser, javascript, width, percentage):
    server = connected_library
    with _db(server) as db:
        item = _item(db, "Jim Henson's Labyrinth — long credits", "comic", authors=CREDIT)
        author_index.reindex_item(db, item, CREDIT)
    domain = _domain_snapshot(server)
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": width, "height": 1100}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        if javascript:
            _choose(page, percentage)
        stage = page.locator(f'#home-recent-row article[data-item-id="{item}"] [data-caption-navigation]')
        frame = stage.bounding_box()
        stage.locator("[data-cover-link]").focus()
        caption = stage.locator(".media-card__caption")
        expect(caption).to_have_css("opacity", "1")
        expect(caption).to_have_css("overflow-y", "hidden")
        assert caption.evaluate("node => node.scrollHeight <= node.clientHeight + 1")
        credit = stage.locator(".media-card__creator")
        expect(credit).to_have_attribute("title", CREDIT)
        expect(credit.locator("[data-caption-more]")).to_have_text("…")
        creator = credit.locator('[data-caption-link="creator"]')
        expect(creator).to_have_count(1)
        expect(creator).to_have_text("Lara Elena Donnelly")
        expect(creator).to_have_css("white-space", "nowrap")
        expect(creator).to_have_css("text-overflow", "ellipsis")
        stage.locator('[data-caption-link="item"]').focus()
        page.keyboard.press("Tab")
        expect(creator).to_be_focused()
        assert creator.bounding_box()["height"] >= 44
        assert stage.bounding_box() == frame
        with page.expect_navigation(wait_until="domcontentloaded") as navigation:
            stage.locator('[data-caption-link="item"]').click()
        assert navigation.value.status == 200
        for name in ("Lara Elena Donnelly", "French Carlomagno", "Pius Bak", "Samantha Dodge"):
            expect(page.locator("main")).to_contain_text(name)
        assert _domain_snapshot(server) == domain
        assert_page_clean(page)
