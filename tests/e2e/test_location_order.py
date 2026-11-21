"""Product smoke test for copy-specific physical shelf ordering."""

import sqlite3

import pytest
from playwright.sync_api import expect

from tests.conftest import _insert_node_location, _insert_physical_item
from tests.e2e.conftest import assert_page_clean

pytestmark = pytest.mark.e2e


def test_arrange_page_auto_orders_series(live_server, authed_page):
    db_path = live_server["data_dir"] / "shelf.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        location_id = _insert_node_location(conn, "E2E Ordered Shelf")
        second_item = _insert_physical_item(
            conn, title="Ordering Volume Two", isbn=None, owned=1,
            location_id=location_id, series_name="Ordering Series", series_position=2,
        )
        first_item = _insert_physical_item(
            conn, title="Ordering Volume One", isbn=None, owned=1,
            location_id=location_id, series_name="Ordering Series", series_position=1,
        )
        second_copy = conn.execute(
            "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
            "VALUES (?, 1, ?, 1)",
            (second_item, location_id),
        ).lastrowid
        first_copy = conn.execute(
            "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
            "VALUES (?, 1, ?, 1)",
            (first_item, location_id),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()

    authed_page.goto(f"{live_server['url']}/locations/{location_id}/arrange")
    authed_page.wait_for_load_state("networkidle")
    expect(authed_page.locator("body")).to_contain_text("E2E Ordered Shelf")
    expect(authed_page.locator("[data-copy-id]")).to_have_count(2)

    with authed_page.expect_response(
        lambda response: f"/api/locations/{location_id}/auto-order" in response.url
        and response.request.method == "POST"
    ):
        authed_page.locator("[data-auto-order='series']").click()

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id, position_order FROM item_copies WHERE id IN (?, ?) "
            "ORDER BY position_order",
            (first_copy, second_copy),
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(first_copy, 1), (second_copy, 2)]
    assert_page_clean(authed_page)
