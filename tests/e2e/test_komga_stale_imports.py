"""Future fresh-synthetic browser visibility journeys, authored not run.

Cached verdicts are explicitly seeded fixtures: these tests do NOT prove remote
enumeration/completeness, which has separate unit/integration regression sources.
"""

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, assert_page_clean
from tests.e2e.test_connected_library import _browser_page, _db, _item, _login, connected_library  # noqa: F401
from tests.e2e.test_navigation_search import _snapshot


pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("javascript", [False, True])
def test_stale_import_is_hidden_from_default_views_but_retained_for_unavailable_inspection_and_activity(connected_library, browser, javascript):
    server, setup = connected_library, connected_library["page"]
    token = setup.evaluate("window.csrfToken()")
    response = setup.request.post(server["url"] + "/api/komga/settings", data={
        "url": "https://komga.example", "api_key": "synthetic-token",
    }, headers={"X-CSRF-Token": token})
    assert response.ok and response.json()["ok"]
    assert setup.request.post(server["url"] + "/api/settings/features/komga", form={"enabled": "1"},
                              headers={"X-CSRF-Token": token}).ok
    with _db(server) as db:
        old = _item(db, "Retained stale issue", "comic", source="komga", series_name="Retained series", series_position=1)
        live = _item(db, "Current issue", "comic", source="komga", series_name="Retained series", series_position=1)
        for identity, item, state in (("old-id", old, "unavailable"), ("current-id", live, "available")):
            db.execute("INSERT INTO komga_records (komga_id, item_id, library_id, kind) VALUES (?, ?, 'lib', 'comic')", (identity, item))
            db.execute("INSERT INTO komga_presence (komga_id, library_id, server_url, availability) "
                       "VALUES (?, 'lib', 'https://komga.example', ?)", (identity, state))
    response = setup.request.post(server["url"] + f"/api/library/items/{old}/personal",
                                  form={"status": "in_progress", "progress": "17"},
                                  headers={"X-CSRF-Token": token}, max_redirects=0)
    assert response.status == 303
    before = _snapshot(server)
    with _browser_page(browser, java_script_enabled=javascript, viewport={"width": 375, "height": 1000}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        for path in ("/browse", "/library?view=items", "/series"):
            page.goto(server["url"] + path, wait_until="load")
            if path == "/series":
                expect(page.locator(f'a[href="/item/{old}?from=series"]')).to_have_count(0)
                expect(page.locator(f'a[href="/item/{live}?from=series"]').first).to_be_visible()
                expect(page.get_by_test_id("series-card")).to_have_attribute("data-item-count", "1")
            else:
                marker = "data-library-member" if path.startswith("/library") else "data-item-id"
                expect(page.locator(f'[{marker}="{old}"]')).to_have_count(0)
                expect(page.locator(f'[{marker}="{live}"]').first).to_be_visible()
        page.goto(server["url"] + "/library?view=items&availability=unavailable")
        expect(page.locator(f'[data-library-member="{old}"]')).to_have_count(1)
        page.goto(server["url"] + f"/item/{old}")
        expect(page.locator("main")).to_contain_text("Retained stale issue")
        page.goto(server["url"] + "/activity")
        expect(page.locator("main")).to_contain_text("Retained stale issue")
        assert _snapshot(server) == before
        assert_page_clean(page)
