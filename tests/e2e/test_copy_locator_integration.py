"""Actual shipped locator wiring: no route interception or asset injection."""

import sqlite3

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard
from tests.e2e.test_copy_locator import (
    _assert_target_clear_and_reflowed, _url, _window_ids, locator_seed,
)


pytestmark = pytest.mark.e2e


def _snapshot(server):
    tables = ("items", "item_copies", "locations", "settings", "user_item_state", "user_item_events")
    with sqlite3.connect(server["data_dir"] / "shelf.db") as db:
        return {table: tuple(db.execute(f"SELECT * FROM {table} ORDER BY rowid")) for table in tables}


@pytest.mark.parametrize("width", (320, 375, 640, 768, 1024, 1280, 1366, 1920))
def test_real_shell_locator_follow_jump_return_and_back_without_injected_assets(
    live_server, authed_page, locator_seed, width,
):
    page, seed = authed_page, locator_seed
    page.set_viewport_size({"width": width, "height": 900})
    before = _snapshot(live_server)
    mutations, assets = [], []
    page.on("request", lambda request: mutations.append(request.url) if request.method != "GET" else None)
    page.on("request", lambda request: assets.append(request.url))
    page.goto(f'{live_server["url"]}/item/{seed["item"]}', wait_until="load")
    opener = page.locator(f'#copy-locator-open-{seed["copy"]}')
    with page.expect_navigation():
        opener.press("Enter")
    expect(page).to_have_url(_url(live_server["url"], seed))
    target = page.locator("[data-copy-locator-target]")
    expect(target).to_be_focused()
    _assert_target_clear_and_reflowed(page, seed)
    assert _window_ids(page) == seed["ordered"][6:13]
    for path in ("/static/js/copy-locator.js", "/static/css/copy-locator.source.css"):
        assert live_server["url"] + path in assets
        assert page.locator(f'[src="{path}"], [href="{path}"]').count() == 1
    expect(page.locator("[data-copy-locator] form, [data-copy-locator] button")).to_have_count(0)
    page.get_by_role("link", name="Jump to selected copy", exact=True).press("Enter")
    expect(target).to_be_focused()
    with page.expect_navigation():
        page.get_by_role("link", name="Back to item", exact=True).click()
    expect(opener).to_be_focused()
    page.go_back(wait_until="load")
    expect(target).to_be_focused()
    _assert_target_clear_and_reflowed(page, seed)
    assert mutations == [] and _snapshot(live_server) == before
    assert_page_clean(page)


def test_real_shipped_no_js_locator_has_native_target_and_clearance(
    live_server, browser, authed_page, locator_seed,
):
    context = browser.new_context(java_script_enabled=False, service_workers="block",
                                  storage_state=authed_page.context.storage_state(),
                                  viewport={"width": 320, "height": 900})
    page = attach_page_guard(context.new_page())
    try:
        page.goto(_url(live_server["url"], locator_seed), wait_until="load")
        expect(page.locator("[data-copy-locator-target]")).to_be_focused()
        _assert_target_clear_and_reflowed(page, locator_seed)
        assert page.evaluate("typeof window.__shelfCopyLocator") == "undefined"
        expect(page.get_by_role("link", name="Back to item", exact=True)).to_be_visible()
        assert_page_clean(page)
    finally:
        context.close()
