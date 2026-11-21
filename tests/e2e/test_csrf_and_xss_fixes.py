"""E2E regression tests for the 2026-07-03 review fixes.

Covers flows that unit tests cannot see because the bugs lived in template JS:
- Raw fetch() calls previously missing the X-CSRF-Token header (403 in prod)
- Plain POST forms inside an HTMX-swapped fragment missing the _csrf field
- Stored XSS via borrower name in the Loaned badge (Alpine x-text JS context)
"""
import re
import sqlite3

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import insert_item

pytestmark = pytest.mark.e2e


def _lend_item_to(data_dir, item_id: int, borrower_name: str) -> None:
    """Create a borrower and an open checkout for the item, directly in the DB."""
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    try:
        cur = conn.execute("INSERT INTO borrowers (name) VALUES (?)", (borrower_name,))
        conn.execute(
            "INSERT INTO checkouts (item_id, borrower_id) VALUES (?, ?)",
            (item_id, cur.lastrowid),
        )
        conn.commit()
    finally:
        conn.close()


def test_display_name_change_succeeds(live_server, authed_page):
    """Account modal display-name save — raw fetch() with FormData needs the CSRF header."""
    authed_page.goto(f"{live_server['url']}/browse")
    authed_page.wait_for_load_state("networkidle")
    authed_page.get_by_test_id("account-menu-button").click()
    authed_page.get_by_test_id("account-menu-profile").click()
    name_input = authed_page.locator("input[x-model='displayName']")
    expect(name_input).to_be_visible()
    name_input.fill("E2E Admin")  # same value back — exercises the endpoint
    with authed_page.expect_response("**/api/account/display-name") as resp_info:
        authed_page.click("button:has-text('Save')")
    assert resp_info.value.status == 200, f"display-name save returned {resp_info.value.status}"


def test_bulk_delete_succeeds(live_server, authed_page):
    """Browse bulk delete — raw fetch() DELETE needs the CSRF header."""
    insert_item(live_server["data_dir"], title="Bulk Target", isbn="9780000002013")
    authed_page.goto(f"{live_server['url']}/browse")
    authed_page.wait_for_load_state("networkidle")

    # Record the message and assert on it, never a bare accepting handler
    # (G28): the confirm() lives in an Alpine method, and if that handler were
    # dead — the exact shape `script-src 'self'` produces for an inline
    # onclick — the delete would still fire, the response would still be 200,
    # and an accept-and-assume test would pass over a missing confirmation.
    messages = []

    def _accept(dialog):
        messages.append(dialog.message)
        dialog.accept()

    authed_page.once("dialog", _accept)
    authed_page.get_by_text("Select", exact=True).click()
    authed_page.get_by_text("Select All", exact=True).click()
    with authed_page.expect_response(
        lambda r: "/api/items/" in r.url and r.request.method == "DELETE"
    ) as resp_info:
        authed_page.click("button:has-text('Delete Selected')")
    assert resp_info.value.status == 200, f"bulk delete returned {resp_info.value.status}"

    # The count is whatever Select All caught in this session's shared DB, so
    # pin the shape and a non-zero count rather than a brittle exact number.
    assert len(messages) == 1, f"expected exactly one confirm(), got {messages}"
    m = re.fullmatch(r"Move (\d+) items to Trash\?", messages[0])
    assert m, f"unexpected confirm message: {messages[0]!r}"
    assert int(m.group(1)) >= 1, f"confirm named {m.group(1)} items"


def test_loaned_badge_borrower_name_is_not_executed(live_server, authed_page):
    """A hostile borrower name must render as text, never execute as JS."""
    payload = "'+alert(document.domain)+'"
    item_id = insert_item(live_server["data_dir"], title="Lent Book", isbn="9780000002020")
    _lend_item_to(live_server["data_dir"], item_id, payload)

    dialogs = []
    authed_page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))

    authed_page.goto(f"{live_server['url']}/browse")
    authed_page.wait_for_load_state("networkidle")

    badge = authed_page.locator("text=Loaned").first
    expect(badge).to_be_visible()
    badge.click()
    # The borrower name must now be shown verbatim as text
    expect(authed_page.locator(f"text=To: {payload}")).to_be_visible()
    assert dialogs == [], f"XSS executed: alert fired with {dialogs}"


def test_plain_form_in_lazy_loaded_fragment_carries_csrf(live_server, authed_page):
    """A plain POST form swapped in by HTMX must still get the _csrf field.

    The Discogs panel on a Music item arrives via hx-trigger="load", after
    DOMContentLoaded, so csrf.js has to inject on htmx:load too — without it
    "Clear selection" is a 403 that no TestClient test can see.
    """
    data_dir = live_server["data_dir"]
    item_id = insert_item(data_dir, title="CSRF Pressing", media_type="vinyl", source="musicbrainz")
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    try:
        conn.execute(
            "INSERT INTO music_releases (item_id, musicbrainz_release_id) VALUES (?, ?)",
            (item_id, f"mb-csrf-{item_id}"),
        )
        conn.execute(
            "INSERT INTO music_identifiers (item_id, identifier_type, value) "
            "VALUES (?, 'discogs_release_id', '123456')",
            (item_id,),
        )
        conn.commit()
    finally:
        conn.close()

    authed_page.goto(f"{live_server['url']}/music/item/{item_id}")
    authed_page.wait_for_load_state("networkidle")
    clear = authed_page.locator("#discogs-panel button:has-text('Clear selection')")
    expect(clear).to_be_visible()
    expect(authed_page.locator("#discogs-panel input[name='_csrf']")).to_have_count(1)

    with authed_page.expect_response(lambda r: "/discogs/clear" in r.url) as resp_info:
        clear.click()
    assert resp_info.value.status == 303, f"clear returned {resp_info.value.status}"

    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM music_identifiers WHERE item_id = ?", (item_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert left == 0
