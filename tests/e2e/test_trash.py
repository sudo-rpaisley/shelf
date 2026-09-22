"""E2E: Trash end to end — delete, restore, re-scan, purge, the admin nag.

Every test here boots its own `server_factory` server rather than using the
shared `live_server` (G34): the Trash page and the admin banner both read
*every* trashed row with no cap and no per-file scoping, so a server that
carries whatever earlier E2E files happened to trash would make the counts
and listings here unpredictable.

Every dialog handler records the confirm message and asserts on it (G28) —
a dead or missing `hx-confirm` must not pass silently. Every click whose
assertion reads a swapped HTMX fragment, a new document, or the DB behind
either is armed with the matching waiter *before* the click (G83), never a
`networkidle` wait sitting right after one. `attach_page_guard` wraps every
new page constructor call, and `assert_page_clean` runs on every page before
its context closes.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    _run_setup_wizard,
    assert_page_clean,
    attach_page_guard,
    insert_item,
)

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Helpers — local to this file, kept out of conftest.py on purpose (T8 is
# scoped to this one file).
# ---------------------------------------------------------------------------


def _backdate(data_dir, item_id, days=200):
    """Push a trashed item's `deleted_at` `days` into the past, directly on
    the sqlite file. Mirrors `expired_clause`'s own `datetime('now', ?)`
    modifier rather than computing a timestamp in Python, so this can never
    drift from what the app itself considers "expired"."""
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    try:
        conn.execute(
            "UPDATE items SET deleted_at = datetime('now', ?) WHERE id = ?",
            (f"-{days} days", item_id),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_tag(data_dir, item_id, name):
    """Attach one tag to an item directly on the sqlite file."""
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    try:
        tag_id = conn.execute(
            "INSERT INTO tags (name) VALUES (?)", (name,)
        ).lastrowid
        conn.execute(
            "INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)",
            (item_id, tag_id),
        )
        conn.commit()
    finally:
        conn.close()


def _item_exists(data_dir, item_id) -> bool:
    conn = sqlite3.connect(str(data_dir / "shelf.db"))
    try:
        return conn.execute(
            "SELECT 1 FROM items WHERE id = ?", (item_id,)
        ).fetchone() is not None
    finally:
        conn.close()


def _csrf_headers(page):
    return {"X-CSRF-Token": page.evaluate("() => window.csrfToken()")}


def _login(browser, base, credentials):
    """New context logged in as `credentials`; caller closes the context and
    calls `assert_page_clean` on the returned page before doing so."""
    ctx = browser.new_context()
    pg = attach_page_guard(ctx.new_page())
    pg.goto(f"{base}/login")
    pg.fill("input[name=username]", credentials["username"])
    pg.fill("input[name=password]", credentials["password"])
    pg.click("button[type=submit]")
    pg.wait_for_url(f"{base}/", timeout=10_000)
    return ctx, pg


def _open_scan_in_mode(pg, server, mode_label):
    """Load /scan and switch to a mode, waiting out the switch's own fetch —
    local copy of `test_scan.py`'s helper of the same name (kept in this file
    rather than imported cross-file, so T8 stays a single self-contained
    file)."""
    pg.goto(f"{server['url']}/scan")
    pg.wait_for_load_state("networkidle")
    button = pg.get_by_role("button", name=mode_label, exact=True)
    expect(button).to_be_visible(timeout=5_000)
    with pg.expect_response(lambda r: "/api/recent-scans" in r.url and r.ok):
        button.click()


def _recent_ish(hours=1) -> str:
    """A `deleted_at` value well inside the default 180-day retention window
    — trashed, but never expired, whatever `days` a caller elsewhere backdates
    an *expired* sibling row to."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 1. Delete from the item page -> Trash lists it -> Restore -> item page
#    renders again with its tag.
# ---------------------------------------------------------------------------


def test_delete_from_item_page_then_restore_from_trash_brings_it_back(
    server_factory, browser
):
    """A household's core loop: delete an item, find it in Trash, bring it
    back — tags and all."""
    server = server_factory()
    base = server["url"]
    data_dir = server["data_dir"]
    credentials = _run_setup_wizard(browser, base)

    ctx, pg = _login(browser, base, credentials)
    try:
        item_id = insert_item(
            data_dir, title="Trash CRUD Subject", media_type="book",
            isbn="9780000460019",
        )
        _seed_tag(data_dir, item_id, "trash-e2e-tag")

        pg.goto(f"{base}/item/{item_id}")
        pg.wait_for_load_state("networkidle")
        expect(pg.locator('a[href="/browse?tag=trash-e2e-tag"]')).to_be_visible()

        messages = []

        def accept(dialog):
            messages.append(dialog.message)
            dialog.accept()

        pg.once("dialog", accept)
        # Exact role name, not the broader "has hx-delete" locator
        # test_item_crud.py uses: this page now also carries the tag pill's
        # hx-delete "remove tag" button (added above via _seed_tag), which a
        # `[hx-delete]` selector's `.first` would resolve to instead, before
        # any dialog ever fires.
        delete_btn = pg.get_by_role("button", name="Delete", exact=True)
        # The DELETE answers 200 with a body; a second, JS-driven step
        # (data-after-request="goto-browse") navigates to /browse once the
        # swap lands. The assertion below reads page.url, so the waiter is
        # the navigation, not the response (G83 — same shape as
        # test_item_crud.py's test_item_delete).
        with pg.expect_navigation():
            delete_btn.click()

        assert messages == ["Move 'Trash CRUD Subject' to Trash?"]
        assert "/item/" not in pg.url

        pg.goto(f"{base}/trash")
        pg.wait_for_load_state("networkidle")
        row = pg.locator(f'[data-testid="trash-item-row-{item_id}"]')
        expect(row).to_be_visible()
        expect(row).to_contain_text("Trash CRUD Subject")

        with pg.expect_response(
            lambda r: r.url.split("?")[0].endswith(
                f"/api/trash/items/{item_id}/restore"
            )
        ):
            pg.locator(f'[data-testid="trash-restore-item-{item_id}"]').click()

        expect(pg.locator(f'[data-testid="trash-item-row-{item_id}"]')).to_have_count(0)

        pg.goto(f"{base}/item/{item_id}")
        pg.wait_for_load_state("networkidle")
        expect(pg.locator("body")).to_contain_text("Trash CRUD Subject")
        expect(pg.locator('a[href="/browse?tag=trash-e2e-tag"]')).to_be_visible()

        assert_page_clean(pg)
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# 2. Scan the deleted item in Lookup mode -> in-Trash card + warning toast ->
#    Restore in the card -> restored card; recent-scans strip shows both.
# ---------------------------------------------------------------------------


def test_scan_lookup_reports_in_trash_then_restore_updates_card_and_history(
    server_factory, browser
):
    """Lookup mode on a trashed item's barcode must report it and offer
    Restore rather than pretending the item is unknown (#`in_trash` status,
    `items_scan_modes._scan_mode_in_trash`)."""
    server = server_factory()
    base = server["url"]
    data_dir = server["data_dir"]
    credentials = _run_setup_wizard(browser, base)

    item_id = insert_item(
        data_dir, title="Scan Trash Lookup Subject", media_type="book",
        isbn="9780000460026", deleted_at=_recent_ish(),
    )

    ctx, pg = _login(browser, base, credentials)
    try:
        _open_scan_in_mode(pg, server, "Lookup")

        pg.fill("#isbn-input", "9780000460026")
        with pg.expect_response(
            lambda r: r.url.split("?")[0].endswith("/api/scan")
        ):
            pg.press("#isbn-input", "Enter")

        card = pg.locator(".scan-result").first
        expect(card).to_have_attribute("data-scan-status", "in_trash")
        expect(card.locator("[data-scan-badge]")).to_contain_text("in Trash")
        expect(card.locator("[data-scan-detail]")).to_contain_text("In Trash since")

        toast = pg.locator("#toast-container > div").first
        expect(toast).to_be_visible(timeout=5_000)
        assert "bg-shelf-warning" in (toast.get_attribute("class") or "")

        with pg.expect_response(
            lambda r: r.url.split("?")[0].endswith(
                f"/api/trash/items/{item_id}/restore"
            )
        ):
            card.locator('[data-testid="scan-restore-from-trash"]').click()

        card = pg.locator(".scan-result").first
        expect(card).to_have_attribute("data-scan-status", "restored")
        expect(card.locator("[data-scan-detail]")).to_contain_text("Restored from Trash")

        # Reload the recent-scans strip (re-clicking the current mode calls
        # setMode() -> loadRecentScans() unconditionally) so both scan_log
        # rows — in_trash, then restored — come back from the DB rather than
        # from the live card the swap already proved.
        with pg.expect_response(lambda r: "/api/recent-scans" in r.url and r.ok):
            pg.get_by_role("button", name="Lookup", exact=True).click()

        results = pg.locator("#scan-results")
        expect(results.locator(".scan-result")).to_have_count(2)
        expect(results).to_contain_text("in_trash")
        expect(results).to_contain_text("restored")

        assert_page_clean(pg)
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# 3. Admin purges one trashed item outright, then Empties Expired — only the
#    backdated row goes, the non-expired one stays.
# ---------------------------------------------------------------------------


def test_admin_purges_permanently_and_empties_expired_leaving_non_expired_rows(
    server_factory, browser
):
    server = server_factory()
    base = server["url"]
    data_dir = server["data_dir"]
    credentials = _run_setup_wizard(browser, base)

    item_now_id = insert_item(
        data_dir, title="Trash Purge Now Subject", media_type="book",
        isbn="9780000470010", deleted_at=_recent_ish(),
    )
    item_expired_id = insert_item(
        data_dir, title="Trash Purge Expired Subject", media_type="book",
        isbn="9780000470027",
    )
    _backdate(data_dir, item_expired_id, days=200)
    item_stays_id = insert_item(
        data_dir, title="Trash Purge Stays Subject", media_type="book",
        isbn="9780000470034", deleted_at=_recent_ish(),
    )

    ctx, pg = _login(browser, base, credentials)
    try:
        pg.goto(f"{base}/trash")
        pg.wait_for_load_state("networkidle")

        expect(pg.locator(f'[data-testid="trash-item-row-{item_now_id}"]')).to_be_visible()
        expect(pg.locator(f'[data-testid="trash-item-row-{item_expired_id}"]')).to_be_visible()
        expect(pg.locator(f'[data-testid="trash-item-row-{item_stays_id}"]')).to_be_visible()
        expect(pg.locator(f'[data-testid="trash-expired-{item_expired_id}"]')).to_be_visible()
        expect(pg.locator('[data-testid="trash-empty-expired"]')).to_contain_text(
            "Empty expired (1)"
        )

        messages = []

        def accept(dialog):
            messages.append(dialog.message)
            dialog.accept()

        pg.once("dialog", accept)
        with pg.expect_response(
            lambda r: r.url.split("?")[0].endswith(
                f"/api/trash/items/{item_now_id}"
            ) and r.request.method == "DELETE"
        ):
            pg.locator(f'[data-testid="trash-delete-item-{item_now_id}"]').click()

        assert messages == [
            "Delete 'Trash Purge Now Subject' permanently? This cannot be undone."
        ]
        expect(pg.locator(f'[data-testid="trash-item-row-{item_now_id}"]')).to_have_count(0)
        assert not _item_exists(data_dir, item_now_id)

        pg.once("dialog", accept)
        with pg.expect_response(
            lambda r: r.url.split("?")[0].endswith("/api/trash/empty-expired")
        ):
            pg.locator('[data-testid="trash-empty-expired"]').click()

        assert messages == [
            "Delete 'Trash Purge Now Subject' permanently? This cannot be undone.",
            "Delete 1 expired items and copies permanently?",
        ]
        expect(pg.locator('[data-testid="trash-purged-notice"]')).to_contain_text(
            "Permanently deleted 1 row."
        )
        expect(pg.locator(f'[data-testid="trash-item-row-{item_expired_id}"]')).to_have_count(0)
        assert not _item_exists(data_dir, item_expired_id)

        expect(pg.locator(f'[data-testid="trash-item-row-{item_stays_id}"]')).to_be_visible()
        assert _item_exists(data_dir, item_stays_id)

        assert_page_clean(pg)
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# 4. The admin banner: appears for a backdated row, Dismiss hides it (even
#    across a reload), and it comes back once a second row makes the expired
#    count exceed what was dismissed.
# ---------------------------------------------------------------------------


def test_admin_banner_appears_dismisses_and_reappears_on_a_new_expired_row(
    server_factory, browser
):
    """`app/services/trash.py`'s expired-count cache is a process-lived
    module global with an hour's TTL, refreshed only when something calls
    `trash.invalidate()` (every write path does; a raw sqlite write from a
    test does not). Two things follow for this test:

    - The FIRST expired row is seeded and backdated before
      `_run_setup_wizard` runs, so the cache's first-ever read — the admin's
      post-setup render of `/` — already sees it. Backdating afterwards would
      leave the process holding a stale (lower) count for up to an hour.
    - The SECOND expired row is seeded after the cache is already warm, so
      this test forces a recompute the way a real admin would: saving the
      Trash retention setting again (unchanged) through
      POST /api/settings/trash, which calls `trash.invalidate()`
      (`app/routers/settings.py::update_trash_settings`). That is the
      deviation from "just insert and reload" the task brief called out —
      recorded here rather than only in the report.
    """
    server = server_factory()
    base = server["url"]
    data_dir = server["data_dir"]

    item1_id = insert_item(
        data_dir, title="Nag Row One", media_type="book",
        isbn="9780000460033",
    )
    _backdate(data_dir, item1_id, days=200)

    # First admin-authenticated render of this fresh process — this is what
    # populates the trash.py module cache with today's expired count (1).
    credentials = _run_setup_wizard(browser, base)

    ctx, pg = _login(browser, base, credentials)
    try:
        pg.goto(f"{base}/browse")
        pg.wait_for_load_state("networkidle")
        banner = pg.locator('[data-testid="trash-nag"]')
        expect(banner).to_be_visible()
        expect(banner).to_contain_text(
            "1 item in Trash has been there longer than your retention window."
        )

        with pg.expect_response(
            lambda r: r.url.split("?")[0].endswith("/api/trash/nag/dismiss")
        ):
            pg.locator('[data-testid="trash-nag-dismiss"]').click()

        expect(pg.locator('[data-testid="trash-nag"]')).to_have_count(0)

        # Gone after a full reload too, not just the in-place htmx swap.
        pg.reload()
        pg.wait_for_load_state("networkidle")
        expect(pg.locator('[data-testid="trash-nag"]')).to_have_count(0)

        item2_id = insert_item(
            data_dir, title="Nag Row Two", media_type="book",
            isbn="9780000460040",
        )
        _backdate(data_dir, item2_id, days=200)

        resp = pg.request.post(
            f"{base}/api/settings/trash",
            form={"trash_retention_days": "180"},
            headers=_csrf_headers(pg),
        )
        assert resp.status in (200, 303)

        pg.goto(f"{base}/browse")
        pg.wait_for_load_state("networkidle")
        banner = pg.locator('[data-testid="trash-nag"]')
        expect(banner).to_be_visible()
        expect(banner).to_contain_text(
            "2 items in Trash have been there longer than your retention window."
        )

        assert_page_clean(pg)
    finally:
        ctx.close()
