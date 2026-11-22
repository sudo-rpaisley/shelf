"""D6: strict refusal must not lose the queued scan it did not save."""

import sqlite3

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import insert_item


pytestmark = pytest.mark.e2e


def test_invalid_flush_keeps_the_scan_without_a_placeholder(live_server, authed_page):
    bad_code = "9780441172710"
    insert_item(live_server["data_dir"], title="Invalid flush control", isbn="9780306406157")
    authed_page.goto(f"{live_server['url']}/store")
    expect(authed_page.get_by_test_id("status-line")).to_contain_text("titles cached")
    authed_page.context.set_offline(True)
    authed_page.get_by_test_id("isbn-input").fill(bad_code)
    authed_page.get_by_test_id("check-button").click()
    expect(authed_page.get_by_test_id("queue-count")).to_have_text("1")

    with authed_page.expect_response("**/api/store/queue") as response:
        authed_page.context.set_offline(False)
    result = response.value.json()["results"][0]
    assert result["status"] == "invalid"
    assert result["code"] == bad_code
    assert result["owned"] is False
    assert result.get("item_id") is None
    expect(authed_page.get_by_test_id("queue-count")).to_have_text("1")
    assert authed_page.evaluate(
        "() => JSON.parse(localStorage.getItem('shelf-store-queue')).map(e => e.isbn)"
    ) == [bad_code]
    with sqlite3.connect(str(live_server["data_dir"] / "shelf.db")) as conn:
        assert conn.execute("SELECT title, isbn FROM items ORDER BY id").fetchall() == [
            ("Invalid flush control", "9780306406157")
        ]
