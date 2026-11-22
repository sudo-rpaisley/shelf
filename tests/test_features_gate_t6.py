"""T6: gate lending (checkouts.py's five routes, plus the lend/return scan
modes' inline refusal — G62/G105 apply to neither, since the scan refusal
reuses the existing "error" status rather than adding one).

Follows the pattern established by tests/test_features_gate.py. Reuses its
helpers rather than duplicating them. See GOTCHAS G117 (anonymous /api/
requests never reach the gate; a session fixture is required).
"""

from app.database import get_db
from tests.conftest import _insert_borrower, _insert_item, _insert_location
from tests.test_features_gate import (
    HX, NAV, assert_disabled_page, assert_json_403, assert_toast, off,
)


def _scan(client, mode, isbn="9780000000125", **extra):
    return client.post("/api/scan", data={"isbn": isbn, "mode": mode, **extra})


# --- Scan modes: lend / return ---------------------------------------------


def test_lend_scan_with_lending_off_reports_error_and_writes_nothing(admin_client, db):
    _insert_item(db, title="Lendable Book", isbn="9780000000125")
    bid = _insert_borrower(db, "Alice")
    db.commit()

    off("lending")
    resp = _scan(admin_client, "lend", borrower_id=str(bid))

    assert resp.status_code == 200
    assert 'data-scan-error' in resp.text
    assert "Lending is turned off" in resp.text
    assert db.execute("SELECT COUNT(*) FROM checkouts").fetchone()[0] == 0


def test_return_scan_with_lending_off_leaves_checked_in_null(admin_client, db):
    item_id = _insert_item(db, title="Return Me", isbn="9780000000217")
    bid = _insert_borrower(db, "Carol")
    loan_id = db.execute(
        "INSERT INTO checkouts (item_id, borrower_id, checked_out) VALUES (?, ?, datetime('now'))",
        (item_id, bid),
    ).lastrowid
    db.commit()

    off("lending")
    resp = _scan(admin_client, "return", isbn="9780000000217")

    assert resp.status_code == 200
    assert 'data-scan-error' in resp.text
    assert "Lending is turned off" in resp.text
    assert db.execute(
        "SELECT checked_in FROM checkouts WHERE id = ?", (loan_id,)
    ).fetchone()["checked_in"] is None


def test_lend_scan_with_lending_on_still_works(admin_client, db):
    _insert_item(db, title="Lendable Book", isbn="9780000000125")
    bid = _insert_borrower(db, "Alice")
    db.commit()

    resp = _scan(admin_client, "lend", borrower_id=str(bid))

    assert resp.status_code == 200
    assert b"checked_out" in resp.content or b"Lent to" in resp.content
    assert db.execute("SELECT COUNT(*) FROM checkouts").fetchone()[0] == 1


def test_other_scan_modes_unaffected_with_lending_off(admin_client, db):
    loc_id = _insert_location(db, "Garage")
    move_item = _insert_item(db, title="Moving Book", isbn="9780000000309")
    _insert_item(db, title="Found Book", isbn="9780000000606")
    db.commit()

    off("lending")
    move_resp = _scan(admin_client, "move", isbn="9780000000309", location_id=str(loc_id))
    lookup_resp = _scan(admin_client, "lookup", isbn="9780000000606")

    assert move_resp.status_code == 200
    assert b"moved" in move_resp.content
    assert lookup_resp.status_code == 200
    assert b"found" in lookup_resp.content
    with get_db() as check_db:
        row = check_db.execute(
            "SELECT location_id FROM items WHERE id = ?", (move_item,)
        ).fetchone()
    assert row["location_id"] == loc_id


# --- checkouts.py routes -----------------------------------------------


def test_checkout_htmx_gets_toast(admin_client, db):
    item_id = _insert_item(db, title="Checkoutable", isbn="9780000000415")
    bid = _insert_borrower(db, "Dana")
    db.commit()

    off("lending")
    resp = admin_client.post(
        f"/api/items/{item_id}/checkout", data={"borrower_id": str(bid)}, headers=HX
    )
    assert_toast(resp, "Lending")


def test_create_borrower_gets_json_403(admin_client):
    off("lending")
    resp = admin_client.post("/api/borrowers", data={"name": "Erin"})
    assert_json_403(resp, "lending", "Lending")


def test_checkout_nav_headers_get_disabled_page(admin_client, db):
    """R4 pin: the item page's native (non-htmx) lending form posts here
    with Sec-Fetch-Mode: navigate, so a disabled feature must land on the
    disabled page rather than raw JSON or a bare 403."""
    item_id = _insert_item(db, title="Checkoutable", isbn="9780000000422")
    bid = _insert_borrower(db, "Frank")
    db.commit()

    off("lending")
    resp = admin_client.post(
        f"/api/items/{item_id}/checkout", data={"borrower_id": str(bid)}, headers=NAV
    )
    assert_disabled_page(resp, "Lending", enable_form=True)
