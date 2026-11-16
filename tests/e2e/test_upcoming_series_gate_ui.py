"""Mixed-feature Upcoming UI against fresh, offline servers.

No provider token, ISBN, or cover URL is seeded. The existing server factory
disables radar/cover workers, and all actions hit the real local routes.
"""

import sqlite3
from contextlib import closing

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    _run_setup_wizard,
    assert_page_clean,
    attach_page_guard,
    insert_item,
)

pytestmark = pytest.mark.e2e

SERIES = "UI Gate & Radar"
TITLES = ["UI Gate Wishlist", "UI Gate Dismiss", "UI Gate Undated"]
PASSWORD = "gate-test-password1"


def _query(server, sql, params=()):
    with closing(sqlite3.connect(str(server["data_dir"] / "shelf.db"))) as conn:
        return conn.execute(sql, params).fetchall()


def _watch_state(server):
    return _query(server, "SELECT * FROM release_watches ORDER BY id")


def _release_state(server):
    return _query(server, "SELECT * FROM upcoming_releases ORDER BY id")


def _login(page, base, username, password):
    page.goto(f"{base}/login")
    page.fill("input[name=username]", username)
    page.fill("input[name=password]", password)
    with page.expect_navigation() as navigation:
        page.click("button[type=submit]")
    assert navigation.value.status == 200
    assert page.url == f"{base}/"


def _admin_post(server, path, form):
    admin = server["admin"]
    return admin.request.post(
        f"{server['url']}{path}", form=form, max_redirects=0,
        headers={"X-CSRF-Token": admin.evaluate("() => window.csrfToken()")},
    )


def _feature(server, key, enabled):
    response = _admin_post(server, f"/api/settings/features/{key}", {"enabled": "1" if enabled else "0"})
    assert response.status == 303
    assert response.headers["location"] == "/settings"


@pytest.fixture
def gate_server(server_factory, browser):
    server = server_factory()
    credentials = _run_setup_wizard(browser, server["url"])
    ctx = browser.new_context()
    admin = attach_page_guard(ctx.new_page())
    try:
        _login(admin, server["url"], credentials["username"], credentials["password"])
        server["admin"] = admin
        for role in ("editor", "viewer"):
            response = _admin_post(server, "/api/users", {
                "username": f"gate-{role}", "display_name": f"Gate {role}",
                "password": PASSWORD, "role": role,
            })
            assert response.status == 200 and response.json()["ok"] is True

        insert_item(server["data_dir"], title="UI Gate First", series_name=SERIES, owned=1)
        with closing(sqlite3.connect(str(server["data_dir"] / "shelf.db"))) as conn:
            watch_id = conn.execute(
                "INSERT INTO release_watches (series_name, checked_at, check_state) "
                "VALUES (?, '2026-11-01 12:00:00', 'ok')", (SERIES,),
            ).lastrowid
            ids = []
            for hc_id, title, position, date in (
                (92001, TITLES[0], 2, "2098-01-01"),
                (92002, TITLES[1], 3, "2099-01-01"),
                (92003, TITLES[2], 4, None),
            ):
                ids.append(conn.execute(
                    "INSERT INTO upcoming_releases (watch_id, hc_book_id, title, authors, "
                    "series_position, release_date) VALUES (?, ?, ?, 'UI Gate Writer', ?, ?)",
                    (watch_id, hc_id, title, position, date),
                ).lastrowid)
            conn.commit()
        server["release_ids"] = ids
        yield server
        assert_page_clean(admin)
    finally:
        ctx.close()


@pytest.mark.parametrize("size", [(1280, 800), (390, 844)], ids=["desktop", "phone"])
def test_series_off_editor_actions_work_and_stop_returns_when_reenabled(gate_server, browser, size):
    server = gate_server
    _feature(server, "series", False)
    watches = _watch_state(server)
    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = attach_page_guard(ctx.new_page())
    try:
        _login(page, server["url"], "gate-editor", PASSWORD)
        response = page.goto(f"{server['url']}/upcoming")
        assert response.status == 200
        expect(page.get_by_test_id("upcoming-row-title")).to_have_text(TITLES)
        expect(page.locator('main a[href="/series"]')).to_have_count(0)
        expect(page.locator('main [hx-post^="/api/series/"]')).to_have_count(0)
        expect(page.get_by_test_id("upcoming-row-stop")).to_have_count(0)
        expect(page.get_by_test_id("upcoming-row-wishlist")).to_have_count(3)
        expect(page.get_by_test_id("upcoming-row-dismiss")).to_have_count(3)
        expect(page.get_by_test_id("upcoming-attribution")).to_have_text("Powered by Hardcover")
        assert _watch_state(server) == watches

        rows = page.get_by_test_id("upcoming-row")
        with page.expect_navigation() as navigation:
            rows.filter(has_text=TITLES[0]).get_by_test_id("upcoming-row-wishlist").click()
        assert navigation.value.status == 200
        expect(page.get_by_test_id("upcoming-row-title")).to_have_text(TITLES[1:])
        assert _query(
            server, "SELECT i.title, i.owned, i.series_name, i.series_position, l.slug "
            "FROM items i JOIN list_items li ON li.item_id = i.id "
            "JOIN lists l ON l.id = li.list_id WHERE i.hardcover_book_id = 92001",
        ) == [(TITLES[0], 0, SERIES, 2.0, "wishlist")]

        page.evaluate("() => { window.__gateDocument = 'same-document'; }")
        path = f"/api/upcoming/{server['release_ids'][1]}/dismiss"
        with page.expect_response(lambda r: r.url.endswith(path) and r.request.method == "POST") as dismissed:
            rows.filter(has_text=TITLES[1]).get_by_test_id("upcoming-row-dismiss").click()
        assert dismissed.value.status == 200
        expect(page.get_by_test_id("upcoming-row-title")).to_have_text([TITLES[2]])
        assert page.evaluate("() => window.__gateDocument") == "same-document"
        assert _query(server, "SELECT dismissed FROM upcoming_releases WHERE id = ?",
                      (server["release_ids"][1],)) == [(1,)]
        assert _watch_state(server) == watches
        assert _query(server, "SELECT value FROM settings WHERE key = 'feature.series'") == [("0",)]

        # Enabling Series reveals only its controls; announcements and watches survive.
        _feature(server, "series", True)
        page.reload()
        expect(page.get_by_test_id("upcoming-row-title")).to_have_text([TITLES[2]])
        expect(page.locator('main a[href="/series"]')).to_be_visible()
        expect(page.get_by_test_id("upcoming-row-stop")).to_have_count(1)
        assert _watch_state(server) == watches
        with page.expect_navigation() as navigation:
            page.get_by_test_id("upcoming-row-stop").click()
        assert navigation.value.status == 200
        expect(page.get_by_test_id("upcoming-empty")).to_be_visible()
        expect(page.get_by_test_id("upcoming-row")).to_have_count(0)
        assert _watch_state(server) == [] and _release_state(server) == []
        assert _query(server, "SELECT i.owned FROM items i JOIN list_items li ON li.item_id = i.id "
                      "JOIN lists l ON l.id = li.list_id "
                      "WHERE i.hardcover_book_id = 92001 AND l.slug = 'wishlist'") == [(0,)]
        assert_page_clean(page)
    finally:
        ctx.close()


def test_viewer_mixed_feature_pages_remain_read_only_and_required_gates_stay(gate_server, browser):
    server = gate_server
    before = (_watch_state(server), _release_state(server))
    ctx = browser.new_context()
    page = attach_page_guard(ctx.new_page())
    try:
        _login(page, server["url"], "gate-viewer", PASSWORD)
        for enabled in (False, True):
            _feature(server, "series", enabled)
            page.goto(f"{server['url']}/upcoming")
            expect(page.get_by_test_id("upcoming-row-title")).to_have_text(TITLES)
            expect(page.locator('main a[href="/series"]')).to_have_count(1 if enabled else 0)
            for testid in ("upcoming-row-wishlist", "upcoming-row-dismiss", "upcoming-row-stop"):
                expect(page.get_by_test_id(testid)).to_have_count(0)
            expect(page.locator('main [x-data="upcomingRow"]')).to_have_count(0)
            assert (_watch_state(server), _release_state(server)) == before

        for key, label in (("hardcover", "Hardcover"), ("release_radar", "Release radar")):
            _feature(server, key, False)
            page.goto(f"{server['url']}/upcoming")
            expect(page.get_by_test_id("feature-disabled")).to_be_visible()
            expect(page.locator("main")).to_contain_text(f"{label} is turned off on this Shelf.")
            expect(page.get_by_test_id("upcoming-row")).to_have_count(0)
            expect(page.get_by_test_id("feature-enable-form")).to_have_count(0)
            assert (_watch_state(server), _release_state(server)) == before
            _feature(server, key, True)

        page.goto(f"{server['url']}/upcoming")
        expect(page.get_by_test_id("upcoming-row-title")).to_have_text(TITLES)
        assert (_watch_state(server), _release_state(server)) == before
        assert_page_clean(page)
    finally:
        ctx.close()
