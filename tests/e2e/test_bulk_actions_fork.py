"""E2E coverage for user-facing controls that previously had no browser test.

These tests drive the actual Alpine/HTMX/CSP UI rather than calling API
endpoints directly.  That distinction is important: an endpoint can be correct
while a client-side expression prevents the button from ever sending a request.
"""
import sqlite3

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_USERNAME, insert_item, insert_reading_log

pytestmark = pytest.mark.e2e


def _insert_location(data_dir, name: str) -> int:
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    try:
        cur = conn.execute("INSERT INTO locations (name) VALUES (?)", (name,))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _location_id(data_dir, item_id: int):
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    try:
        row = conn.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _bulk_item_state(data_dir, item_id: int):
    """Full fixture rows, including copy Trash and actor-owned history."""
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    conn.row_factory = sqlite3.Row
    try:
        state = {}
        for table, key, order in (
            ("items", "id", "id"),
            ("item_copies", "item_id", "id"),
            ("user_item_state", "item_id", "user_id"),
            ("user_reading_log", "item_id", "id"),
            ("library_items", "item_id", "library_id"),
        ):
            state[table] = [dict(row) for row in conn.execute(
                f"SELECT * FROM {table} WHERE {key} = ? ORDER BY {order}", (item_id,)
            )]
        return state
    finally:
        conn.close()


def test_bulk_move_apply_moves_selected_list_item(live_server, authed_page):
    """Selecting a list row, choosing a location and pressing Apply must move it.

    This is deliberately a browser test.  The bulk-update API already has unit
    coverage, but that did not catch a dead Alpine click expression in the UI.
    """
    location_id = _insert_location(live_server["data_dir"], "Bulk Move Target")
    item_id = insert_item(
        live_server["data_dir"],
        title="Bulk Move Browser Probe",
        isbn="9780000999401",
    )
    control_id = insert_item(
        live_server["data_dir"],
        title="Bulk Move Unselected Control",
        isbn="9780000999402",
        location_id=_insert_location(live_server["data_dir"], "Bulk Move Control Location"),
        personal_username=ADMIN_USERNAME,
        reading_status="read",
        date_started="2026-01-01",
        date_finished="2026-01-15",
    )
    insert_reading_log(live_server["data_dir"], control_id)
    selected_before = _bulk_item_state(live_server["data_dir"], item_id)
    control_before = _bulk_item_state(live_server["data_dir"], control_id)
    # Raw insert_item deliberately creates no copies: this picker edits FLAT
    # metadata, not a NODE placement, and must not manufacture a physical copy.
    assert selected_before["items"][0]["location_id"] is None
    assert selected_before["item_copies"] == control_before["item_copies"] == []
    assert len(control_before["user_item_state"]) == len(control_before["user_reading_log"]) == 1

    authed_page.goto(f"{live_server['url']}/browse")
    authed_page.wait_for_load_state("networkidle")
    authed_page.locator("[data-testid='view-list']").click()
    expect(authed_page.locator(f"tr[data-item-id='{item_id}']")).to_be_visible()
    expect(authed_page.locator(f"tr[data-item-id='{control_id}']")).to_be_visible()

    authed_page.get_by_role("button", name="Select", exact=True).click()
    authed_page.locator(f"tr[data-item-id='{item_id}']").click()
    expect(authed_page.get_by_text("1 selected", exact=True)).to_be_visible()

    authed_page.locator('select[x-model="bulkLocationVal"]').select_option(str(location_id))
    apply_button = authed_page.locator('button[x-show="bulkLocationVal"]')
    expect(apply_button).to_be_visible()

    bulk_url = f"{live_server['url']}/api/items/bulk-update"
    mutation_responses = []

    def observe_mutation_response(response):
        if response.url == bulk_url and response.request.method == "POST":
            mutation_responses.append(response)
            return True
        return False

    def is_browse_document(request):
        return (
            request.url.split("?", 1)[0] == f"{live_server['url']}/browse"
            and request.method == "GET"
            and request.is_navigation_request()
            and request.resource_type == "document"
            and request.frame == authed_page.main_frame
        )

    # The list-view HTMX request can push history after its rows are visible;
    # expect_navigation can mistake that same-document event for bulk success.
    # Require the real POST response, then a native document request/response
    # and load. bulkUpdate only reloads after parsing its genuine JSON data.ok.
    with (
        authed_page.expect_event("load"),
        authed_page.expect_response(lambda r: is_browse_document(r.request)) as reloaded,
        authed_page.expect_request(
            lambda r: bool(mutation_responses) and is_browse_document(r)
        ) as reload_request,
        authed_page.expect_response(observe_mutation_response) as mutation_response,
        authed_page.expect_request(
            lambda r: r.url == bulk_url and r.method == "POST"
        ) as mutation,
    ):
        apply_button.click()

    assert mutation.value.post_data_json == {
        "item_ids": [item_id], "updates": {"location_id": location_id},
    }
    assert mutation_response.value.request == mutation.value
    assert mutation_response.value.status == 200
    assert reloaded.value.request == reload_request.value
    assert reloaded.value.status == 200
    assert _location_id(live_server["data_dir"], item_id) == location_id
    selected_after = _bulk_item_state(live_server["data_dir"], item_id)
    assert selected_after["item_copies"] == []
    expected_item = {
        **selected_before["items"][0],
        "location_id": location_id,
        "updated_at": selected_after["items"][0]["updated_at"],
    }
    assert selected_after == {**selected_before, "items": [expected_item]}
    assert _bulk_item_state(live_server["data_dir"], control_id) == control_before


def test_shortcut_help_button_opens_and_modal_controls_close(live_server, authed_page):
    """The account shortcut action and modal close surfaces work under strict CSP."""
    authed_page.goto(f"{live_server['url']}/browse")
    authed_page.wait_for_load_state("networkidle")

    modal = authed_page.locator("#shortcut-modal")
    expect(modal).to_be_hidden()

    def open_shortcuts():
        authed_page.locator('[data-testid="account-menu-button"]').click()
        panel = authed_page.locator('[data-testid="account-menu-panel"]')
        expect(panel).to_be_visible()
        panel.locator('[data-testid="account-shortcuts-action"]').click()

    open_shortcuts()
    expect(modal).to_be_visible()

    authed_page.get_by_role("button", name="Close keyboard shortcuts").click()
    expect(modal).to_be_hidden()

    # Reopen and verify clicking the backdrop closes it too.
    open_shortcuts()
    expect(modal).to_be_visible()
    modal.click(position={"x": 5, "y": 5})
    expect(modal).to_be_hidden()
