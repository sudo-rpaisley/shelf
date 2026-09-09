"""Chromium coverage for per-library Browse selection permissions."""

import sqlite3

import pytest
from playwright.sync_api import expect

from app.auth import hash_password
from tests.e2e.conftest import attach_page_guard, assert_page_clean, insert_item

pytestmark = pytest.mark.e2e


USERNAME = "e2ecollectioneditor"
PASSWORD = "e2ecollectionpassword1"


def _set_test_user(data_dir, role: str) -> None:
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    try:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (USERNAME,)).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, 'viewer')",
                (USERNAME, hash_password(PASSWORD), "Collection Editor"),
            )
            user_id = int(cur.lastrowid)
        else:
            user_id = int(row[0])
        conn.execute(
            "INSERT INTO library_memberships (library_id, user_id, role) VALUES (1, ?, ?) "
            "ON CONFLICT(library_id, user_id) DO UPDATE SET role = excluded.role",
            (user_id, role),
        )
        conn.commit()
    finally:
        conn.close()


def _create_collection(data_dir) -> None:
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO collections (library_id, name) VALUES (1, 'Browser Picks')"
        )
        conn.commit()
    finally:
        conn.close()


def test_select_mode_follows_library_membership(live_server, browser, setup_admin):
    item_id = insert_item(
        live_server["data_dir"], title="Library-aware selectable item", media_type="book"
    )
    _create_collection(live_server["data_dir"])
    _set_test_user(live_server["data_dir"], "viewer")

    ctx = browser.new_context()
    page = attach_page_guard(ctx.new_page())
    page.goto(f"{live_server['url']}/login")
    page.fill("input[name=username]", USERNAME)
    page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{live_server['url']}/", timeout=10_000)

    page.goto(f"{live_server['url']}/browse")
    expect(page.get_by_test_id("select-mode-toggle")).to_have_count(0)

    # The account remains a global viewer. Only its Main Library membership
    # changes, which is the boundary this UI must follow.
    _set_test_user(live_server["data_dir"], "editor")
    page.reload()
    expect(page.get_by_test_id("select-mode-toggle")).to_be_visible()

    page.get_by_test_id("select-mode-toggle").click()
    item = page.locator(f'[data-item-id="{item_id}"]')
    expect(item).to_have_attribute("data-can-edit", "1")
    item.click()
    expect(page.get_by_text("1 selected", exact=True)).to_be_visible()
    expect(page.get_by_test_id("bulk-collection-select")).to_be_visible()
    expect(page.get_by_test_id("bulk-type-control")).to_have_count(0)

    assert_page_clean(page)
    ctx.close()
