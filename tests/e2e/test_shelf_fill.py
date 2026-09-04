"""E2E coverage for the context-aware Shelf Fill scan mode."""
import sqlite3

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import insert_item

pytestmark = pytest.mark.e2e


def _insert_location(data_dir, name):
    db_path = data_dir / "shelf.db"
    conn = sqlite3.connect(str(db_path))
    try:
        location_id = conn.execute(
            "INSERT INTO locations (name, sort_order) VALUES (?, 1)", (name,)
        ).lastrowid
        conn.commit()
        return location_id
    finally:
        conn.close()


def test_shelf_fill_typed_scan_uses_sticky_precise_location(live_server, authed_page):
    """The browser routes typed scans through Shelf Fill, not ordinary Add."""
    data_dir = live_server["data_dir"]
    legacy_id = _insert_location(data_dir, "E2E Shelf")
    insert_item(
        data_dir,
        title="Shelf Fill E2E Book",
        media_type="book",
        isbn="9780000000170",
        location_id=legacy_id,
    )

    authed_page.goto(f"{live_server['url']}/scan")
    authed_page.wait_for_load_state("networkidle")

    shelf_fill = authed_page.locator("button", has_text="Shelf Fill")
    expect(shelf_fill).to_be_visible(timeout=5_000)
    shelf_fill.click()
    expect(authed_page.locator("h1")).to_have_text("Shelf Filling")

    picker = authed_page.locator("#shelf-fill-location")
    expect(picker).to_be_visible(timeout=5_000)
    picker.select_option(label="E2E Shelf · 1 copy")
    selected_location = picker.input_value()
    assert selected_location

    authed_page.fill("#isbn-input", "9780000000170")
    authed_page.press("#isbn-input", "Enter")

    result = authed_page.locator("#scan-results .scan-result").first
    expect(result).to_contain_text("Shelf Fill E2E Book", timeout=10_000)
    expect(result).to_contain_text("shelved")
    expect(result).to_contain_text("E2E Shelf")
    expect(result).to_contain_text("position 1")

    # The exact shelf choice survives a page reload, so a user can keep
    # filling the same physical shelf without re-selecting it each time.
    authed_page.reload()
    authed_page.wait_for_load_state("networkidle")
    reloaded_picker = authed_page.locator("#shelf-fill-location")
    expect(reloaded_picker).to_be_visible(timeout=5_000)
    expect(reloaded_picker).to_have_value(selected_location)
    expect(reloaded_picker.locator("option:checked")).to_contain_text("E2E Shelf")
