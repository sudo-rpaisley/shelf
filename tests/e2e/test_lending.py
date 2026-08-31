"""E2E product-smoke journeys for lending and viewer permissions."""
import sqlite3

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard, insert_item

pytestmark = pytest.mark.e2e


def _insert_borrower(data_dir, name: str) -> int:
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    try:
        cur = conn.execute("INSERT INTO borrowers (name) VALUES (?)", (name,))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_active_checkout(data_dir, item_id: int, borrower_id: int) -> None:
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    try:
        conn.execute(
            "INSERT INTO checkouts (item_id, borrower_id, due_date) "
            "VALUES (?, ?, date('now', '+14 days'))",
            (item_id, borrower_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_item_page_lend_then_return_full_journey(live_server, authed_page):
    """A normal household lend/return cycle works end to end from item detail."""
    borrower = "E2E Lending Friend"
    _insert_borrower(live_server["data_dir"], borrower)
    item_id = insert_item(
        live_server["data_dir"],
        title="E2E Lending Journey",
        isbn="9780907000017",
    )
    base = live_server["url"]

    authed_page.goto(f"{base}/item/{item_id}")
    authed_page.wait_for_load_state("networkidle")

    authed_page.get_by_role("button", name="Lend this item").click()
    form = authed_page.locator(f'form[action="/api/items/{item_id}/checkout"]')
    expect(form).to_be_visible()
    form.locator('select[name="borrower_id"]').select_option(label=borrower)
    form.locator('select[name="due_days"]').select_option("14")
    form.get_by_role("button", name="Check Out").click()

    authed_page.wait_for_url(f"{base}/item/{item_id}", timeout=10_000)
    authed_page.wait_for_load_state("networkidle")
    expect(authed_page.locator("body")).to_contain_text(f"Lent to {borrower}")
    expect(authed_page.get_by_role("button", name="Check In")).to_be_visible()

    authed_page.get_by_role("button", name="Check In").click()
    authed_page.wait_for_url(f"{base}/item/{item_id}", timeout=10_000)
    authed_page.wait_for_load_state("networkidle")

    expect(authed_page.get_by_role("button", name="Lend this item")).to_be_visible()
    history = authed_page.locator("details").filter(has_text="Checkout history")
    expect(history).to_be_visible()
    history.locator("summary").click()
    expect(history).to_contain_text(borrower)
    expect(history).to_contain_text("returned")
    assert_page_clean(authed_page)


def test_viewer_sees_loan_status_but_no_lending_or_editor_actions(
    live_server, browser, authed_page
):
    """Viewer UI mirrors backend permissions while retaining read-only loan context."""
    base = live_server["url"]
    username = "e2eviewer_lending"
    password = "viewer-password-123"

    # Create a real Viewer through the same admin UI a household would use.
    authed_page.goto(f"{base}/settings")
    authed_page.get_by_role("button", name="Users").click()
    authed_page.wait_for_load_state("networkidle")
    authed_page.fill('input[placeholder="Username"]', username)
    authed_page.fill('input[placeholder="Password (min 8 chars)"]', password)
    authed_page.locator('select[x-model="newRole"]').select_option("viewer")
    authed_page.get_by_role("button", name="Add User").click()
    expect(authed_page.locator("span.text-shelf-success")).to_contain_text("created")

    borrower_id = _insert_borrower(live_server["data_dir"], "E2E Viewer Borrower")
    lent_item_id = insert_item(
        live_server["data_dir"],
        title="E2E Viewer Lent Item",
        isbn="9780907000024",
    )
    available_item_id = insert_item(
        live_server["data_dir"],
        title="E2E Viewer Available Item",
        isbn="9780907000031",
    )
    _insert_active_checkout(live_server["data_dir"], lent_item_id, borrower_id)

    ctx = browser.new_context()
    try:
        page = attach_page_guard(ctx.new_page())
        page.goto(f"{base}/login")
        page.fill("input[name=username]", username)
        page.fill("input[name=password]", password)
        page.click("button[type=submit]")
        page.wait_for_url(f"{base}/browse", timeout=10_000)

        # Viewer navigation exposes read-only product surfaces, not editor/admin tools.
        expect(page.locator('[data-nav-tab="browse"]')).to_be_visible()
        expect(page.locator('[data-nav-tab="store"]')).to_be_visible()
        expect(page.locator('[data-nav-tab="series"]')).to_be_visible()
        expect(page.locator('[data-nav-tab="stats"]')).to_be_visible()
        for key in ("scan", "settings", "logs"):
            expect(page.locator(f'[data-nav-tab="{key}"]')).to_have_count(0)

        # A Viewer can understand that a book is out, but cannot return it.
        page.goto(f"{base}/item/{lent_item_id}")
        page.wait_for_load_state("networkidle")
        expect(page.locator("body")).to_contain_text("Lent to E2E Viewer Borrower")
        expect(page.get_by_role("button", name="Check In")).to_have_count(0)
        expect(page.locator('[data-testid="cover-controls"]')).to_have_count(0)
        expect(page.get_by_role("link", name="Edit", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Delete", exact=True)).to_have_count(0)
        expect(page.locator("body")).to_contain_text("Reading Status")

        # Nor should an available item advertise an action that the server rejects.
        page.goto(f"{base}/item/{available_item_id}")
        page.wait_for_load_state("networkidle")
        expect(page.get_by_role("button", name="Lend this item")).to_have_count(0)
        expect(page.locator(f'form[action="/api/items/{available_item_id}/checkout"]')).to_have_count(0)
        assert_page_clean(page)
    finally:
        ctx.close()
