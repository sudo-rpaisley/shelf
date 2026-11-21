"""The Trash page: its route, its listing, its account-menu entry.

Rows are seeded and trashed through the funnel functions, then committed
before the first request (G48). Expiry is a raw backdate on this connection,
same as tests/test_trash_routes.py.
"""

import re

from app.services import item_copies, item_write
from app.services.item_write import insert_item
from tests.conftest import _insert_borrower, _insert_location


def _item(db, title="Trash Page", **fields):
    return insert_item(db, title=title, source="test", **fields)


def _item_with_copies(db, title, n=2):
    loc = _insert_location(db, f"{title} shelf")
    item_id = _item(db, title, location_id=loc)
    primary = db.execute(
        "SELECT id FROM copies_live WHERE item_id = ? AND is_primary = 1", (item_id,)
    ).fetchone()["id"]
    others = [item_copies.add_copy(db, item_id, {"location_id": loc}) for _ in range(n - 1)]
    return item_id, [primary, *others]


def _backdate_item(db, item_id, days):
    db.execute(
        "UPDATE items SET deleted_at = datetime('now', ?) WHERE id = ?",
        (f"-{days} days", item_id),
    )


def _loan(db, item_id, *, open_=True):
    bid = _insert_borrower(db, f"Borrower {item_id}")
    return db.execute(
        "INSERT INTO checkouts (item_id, borrower_id, checked_in) VALUES (?, ?, ?)",
        (item_id, bid, None if open_ else "2026-01-01"),
    ).lastrowid


def _count_wrapper(html: str) -> int:
    return len(re.findall(r'id="trash-list"', html))


# --- Reachability / roles ----------------------------------------------------

def test_route_is_registered_outside_the_api_prefix():
    import os
    os.environ.setdefault("SHELF_DISABLE_RATE_LIMIT", "1")
    from app.main import app
    assert "/trash" in [r.path for r in app.routes]


def test_editor_can_view_the_page(editor_client, db):
    resp = editor_client.get("/trash")
    assert resp.status_code == 200
    assert _count_wrapper(resp.text) == 1


def test_admin_can_view_the_page(admin_client, db):
    resp = admin_client.get("/trash")
    assert resp.status_code == 200
    assert _count_wrapper(resp.text) == 1


def test_viewer_is_redirected_to_browse(viewer_client, db):
    resp = viewer_client.get("/trash", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/browse"


def test_anonymous_is_redirected_to_login(client, admin_user, db):
    # admin_user seeds a user with no session on THIS client — otherwise an
    # empty user table sends every unauthenticated request to /setup instead.
    resp = client.get("/trash", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# --- Listing content -----------------------------------------------------

def test_a_trashed_item_lists_with_its_title(editor_client, db):
    item_id = _item(db, "Gone To Trash")
    item_write.trash_item(db, item_id)
    db.commit()

    html = editor_client.get("/trash").text
    assert "Gone To Trash" in html
    assert f'data-testid="trash-item-row-{item_id}"' in html


def test_copy_group_links_a_live_item_and_leaves_a_trashed_item_unlinked(editor_client, db):
    live_item, (_p1, live_copy) = _item_with_copies(db, "Still Live Holder")
    item_copies.trash_copy(db, live_copy)

    trashed_item, (_p2, trashed_copy) = _item_with_copies(db, "Gone Holder")
    item_copies.trash_copy(db, trashed_copy)
    item_write.trash_item(db, trashed_item)
    db.commit()

    html = editor_client.get("/trash").text
    assert f'<a href="/item/{live_item}"' in html
    assert f'data-testid="trash-copy-group-link-{live_item}"' in html
    assert f'data-testid="trash-copy-group-title-{trashed_item}"' in html
    assert f'data-testid="trash-copy-group-link-{trashed_item}"' not in html


def test_loaned_badge_only_on_the_item_with_an_open_loan(editor_client, db):
    open_item = _item(db, "Out On Loan")
    _loan(db, open_item, open_=True)
    item_write.trash_item(db, open_item)

    returned_item = _item(db, "Returned Already")
    _loan(db, returned_item, open_=False)
    item_write.trash_item(db, returned_item)
    db.commit()

    html = editor_client.get("/trash").text
    assert f'data-testid="trash-loaned-{open_item}"' in html
    assert f'data-testid="trash-loaned-{returned_item}"' not in html


# --- Role-gated controls ------------------------------------------------------

def test_admin_sees_both_delete_controls_and_empty_expired(admin_client, db):
    item_id = _item(db, "Admin Sees Me")
    item_write.trash_item(db, item_id)
    db.commit()
    db.execute(
        "UPDATE items SET deleted_at = datetime('now', '-200 days') WHERE id = ?", (item_id,)
    )
    db.commit()

    html = admin_client.get("/trash").text
    assert f'hx-delete="/api/trash/items/{item_id}"' in html
    assert "Delete permanently" in html
    assert 'data-testid="trash-empty-expired"' in html
    assert "Empty expired" in html


def test_editor_sees_neither_delete_controls_nor_empty_expired(editor_client, db):
    item_id = _item(db, "Editor Cannot See Me")
    item_write.trash_item(db, item_id)
    db.commit()
    db.execute(
        "UPDATE items SET deleted_at = datetime('now', '-200 days') WHERE id = ?", (item_id,)
    )
    db.commit()

    html = editor_client.get("/trash").text
    assert f'hx-delete="/api/trash/items/{item_id}"' not in html
    assert "Delete permanently" not in html
    assert 'data-testid="trash-empty-expired"' not in html


# --- The expired filter and the zero-retention arm ---------------------------

def test_expired_filter_hides_new_and_keeps_old(editor_client, db):
    old = _item(db, "Old Enough To Purge")
    new = _item(db, "Too New To Purge")
    item_write.trash_item(db, old)
    item_write.trash_item(db, new)
    _backdate_item(db, old, 200)
    _backdate_item(db, new, 10)
    db.commit()

    html = editor_client.get("/trash?expired=1").text
    assert "Old Enough To Purge" in html
    assert "Too New To Purge" not in html
    assert f'data-testid="trash-expired-{old}"' in html


def test_zero_retention_renders_no_empty_expired_and_no_expired_mark(admin_client, db):
    item_id = _item(db, "Never Expires")
    item_write.trash_item(db, item_id)
    _backdate_item(db, item_id, 200)
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('trash_retention_days', '0')"
    )
    db.commit()

    html = admin_client.get("/trash").text
    assert 'data-testid="trash-empty-expired"' not in html
    assert f'data-testid="trash-expired-{item_id}"' not in html


# --- hx-confirm strings, exact (G28's unit half) ------------------------------

def test_item_delete_confirm_is_the_exact_string(admin_client, db):
    item_id = _item(db, "Confirm Me")
    item_write.trash_item(db, item_id)
    db.commit()

    html = admin_client.get("/trash").text
    assert (
        "hx-confirm=\"Delete 'Confirm Me' permanently? This cannot be undone.\""
        in html
    )


def test_empty_expired_confirm_names_the_exact_count(admin_client, db):
    old = _item(db, "Old One")
    another = _item(db, "Old Two")
    item_write.trash_item(db, old)
    item_write.trash_item(db, another)
    _backdate_item(db, old, 200)
    _backdate_item(db, another, 200)
    db.commit()

    html = admin_client.get("/trash").text
    assert 'hx-confirm="Delete 2 expired items and copies permanently?"' in html


def test_copy_delete_confirm_names_the_copy_and_item(admin_client, db):
    item_id, (_primary, secondary) = _item_with_copies(db, "Copy Confirm Item")
    item_copies.trash_copy(db, secondary)
    db.commit()

    html = admin_client.get("/trash").text
    assert (
        "hx-confirm=\"Delete copy #2 of 'Copy Confirm Item' permanently? "
        "This cannot be undone.\"" in html
    )


# --- Notice arms --------------------------------------------------------------

def test_purged_notice_after_empty_expired(admin_client, db):
    old = _item(db, "Purge Notice")
    item_write.trash_item(db, old)
    _backdate_item(db, old, 200)
    db.commit()

    resp = admin_client.post("/api/trash/empty-expired")
    assert resp.status_code == 200
    assert 'data-testid="trash-purged-notice"' in resp.text
    assert "Permanently deleted 1 row." in resp.text
    assert _count_wrapper(resp.text) == 1


def test_not_found_notice_on_a_missing_row(editor_client):
    resp = editor_client.post("/api/trash/items/99999/restore")
    assert resp.status_code == 404
    assert 'data-testid="trash-notice-not-found"' in resp.text
    assert _count_wrapper(resp.text) == 1


def test_restore_item_first_notice_on_a_copy_of_a_trashed_item(editor_client, db):
    item_id, (_primary, secondary) = _item_with_copies(db, "Item Trashed First")
    item_copies.trash_copy(db, secondary)
    item_write.trash_item(db, item_id)
    db.commit()

    resp = editor_client.post(f"/api/trash/copies/{secondary}/restore")
    assert resp.status_code == 200
    assert 'data-testid="trash-notice-restore-item-first"' in resp.text
    assert _count_wrapper(resp.text) == 1


# --- Empty states --------------------------------------------------------------

def test_empty_trash_renders_the_empty_state(editor_client, db):
    html = editor_client.get("/trash").text
    assert 'data-testid="trash-empty"' in html
    assert "Trash is empty." in html


def test_filtering_to_expired_with_none_expired_renders_the_filtered_empty_state(editor_client, db):
    item_id = _item(db, "Too New")
    item_write.trash_item(db, item_id)
    db.commit()

    html = editor_client.get("/trash?expired=1").text
    assert 'data-testid="trash-empty"' in html
    assert "Nothing in Trash has passed the retention window yet." in html


# --- Account menu --------------------------------------------------------------

def _panel(html: str) -> str:
    return html.split('data-testid="account-menu-panel"', 1)[1].split("<!-- Account Modal -->", 1)[0]


def test_editor_account_menu_shows_library_trash_and_no_administration(editor_client):
    panel = _panel(editor_client.get("/browse").text)
    assert "Library" in panel
    assert 'data-testid="account-menu-trash"' in panel
    assert "Administration" not in panel


def test_admin_account_menu_shows_both_headings(admin_client):
    panel = _panel(admin_client.get("/browse").text)
    assert "Library" in panel
    assert "Administration" in panel
    assert 'data-testid="account-menu-trash"' in panel
    assert 'data-testid="account-menu-settings"' in panel


def test_viewer_account_menu_shows_neither_heading(viewer_client):
    panel = _panel(viewer_client.get("/browse").text)
    assert "Library" not in panel
    assert "Administration" not in panel
    assert 'data-testid="account-menu-trash"' not in panel


# --- The two-action pin (rev 1 R1) --------------------------------------------

def test_two_actions_in_a_row_each_get_a_working_trash_list(editor_client, db):
    """Take a control from an action route's OWN returned fragment (not the
    page) and act with it — proves the fragment is self-contained rather than
    only working when it is reached from a full page load."""
    item_id = _item(db, "First Action")
    item_write.trash_item(db, item_id)
    another = _item(db, "Second Action")
    item_write.trash_item(db, another)
    db.commit()

    first = editor_client.post(f"/api/trash/items/{item_id}/restore")
    assert first.status_code == 200
    assert _count_wrapper(first.text) == 1
    m = re.search(
        rf'hx-post="(/api/trash/items/{another}/restore)"[^>]*>', first.text
    )
    assert m, "the first response's own fragment does not carry the second control"

    second = editor_client.post(m.group(1))
    assert second.status_code == 200
    assert _count_wrapper(second.text) == 1
