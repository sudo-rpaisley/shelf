"""Bulk receipts name unique live targets, not the submitted ID count.

Physical rows, copies, membership and series metadata are checked together so
filtering only the receipt (after the physical UPDATE) cannot satisfy the pins.
The competing-writer probes use the isolated fixture database, never live data.
"""

import sqlite3
from contextlib import contextmanager

import pytest

from app import config
from app.services import item_copies, item_write, lists
from tests.conftest import _insert_item, _insert_location


URL = "/api/items/bulk-update"
OLD_STAMP = "2000-01-01 00:00:00"
MISSING_ID = 999999
INVALID_UPDATES = [
    ({"media_type": "widget"}, "Unknown media type: 'widget'"),
    ({"location_id": MISSING_ID}, f"Location {MISSING_ID} not found"),
    ({"reading_status": "finished"}, "Invalid reading status: 'finished'"),
    ({"owned": "2"}, "Owned must be 0 or 1"),
    ({"wishlisted": "true"}, "Wishlisted must be true or false"),
    ({"owned": "1", "wishlisted": True}, "An owned item cannot be on the wishlist"),
    ({"series_name": "   "}, "Series name cannot be empty"),
]


def _seed_selection(db):
    old_location = _insert_location(db, name="Original shelf")
    new_location = _insert_location(db, name="Target shelf")
    live = [
        _insert_item(
            db, title=f"Live {owned}", isbn=None, owned=owned,
            wishlisted=not owned, location_id=old_location,
            reading_status="want_to_read", series_name="Old live series", language="en",
        )
        for owned in (1, 0)
    ]
    trashed = _insert_item(
        db, title="Keep in Trash", isbn=None, owned=0, wishlisted=True,
        location_id=old_location, reading_status="want_to_read",
        series_name="Trash series", language="en",
    )
    other = _insert_item(
        db, title="Not selected", isbn=None, location_id=old_location,
        reading_status="want_to_read", series_name="Other series", language="en",
    )
    for item_id in [*live, trashed, other]:
        item_copies.sync_primary_location(db, item_id, old_location)
    item_write.trash_item(db, trashed, at="2001-01-01 00:00:00")
    db.execute("UPDATE items SET updated_at = ?", (OLD_STAMP,))
    for name in ("Old live series", "Trash series", "Other series"):
        db.execute(
            "INSERT INTO series_meta (name, description, source) VALUES (?, 'Keep metadata', 'manual')",
            (name,),
        )
    db.commit()
    return live, trashed, other, new_location


def _row(db, item_id):
    return dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())


def _snapshot(db):
    return {
        table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("items", "item_copies", "list_items", "series_meta")
    }


def _post(client, item_ids, updates):
    return client.post(URL, json={"item_ids": item_ids, "updates": updates})


def _probe_write_lock():
    rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
    try:
        try:
            rival.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc), str(exc)
            return "locked"
        return "acquired"
    finally:
        rival.rollback()
        rival.close()


def test_all_live_targets_keep_funnel_projections_and_series_gc(admin_client, db):
    live, trashed, other, new_location = _seed_selection(db)
    before = _snapshot(db)
    response = _post(admin_client, live, {
        "location_id": str(new_location), "reading_status": " read ",
        "owned": " 1 ", "series_name": "New series", "language": " fr ",
        "isbn": "not an allowed bulk field",
    })

    assert response.status_code == 200
    assert response.json() == {"ok": True, "updated": 2}
    for item_id in live:
        row = _row(db, item_id)
        assert (row["location_id"], row["reading_status"], row["owned"]) == (new_location, "read", 1)
        assert (row["series_name"], row["language"], row["isbn"]) == ("New series", "fr", None)
        assert row["updated_at"] != OLD_STAMP
        assert not lists.is_member(db, lists.WISHLIST, item_id)
        copies = db.execute("SELECT * FROM item_copies WHERE item_id = ?", (item_id,)).fetchall()
        assert len(copies) == 1
        assert copies[0]["location_id"] == new_location
    after = _snapshot(db)
    for table in ("items", "item_copies"):
        key = "id" if table == "items" else "item_id"
        assert [r for r in after[table] if r[key] in (trashed, other)] == [
            r for r in before[table] if r[key] in (trashed, other)
        ]
    assert {row["name"] for row in after["series_meta"]} == {"Trash series", "Other series"}
    assert lists.is_member(db, lists.WISHLIST, trashed)


def test_duplicates_and_missing_ids_count_canonical_live_targets_once(admin_client, db):
    live, trashed, other, _ = _seed_selection(db)
    untouched = [_row(db, item_id) for item_id in (trashed, other)]
    response = _post(
        admin_client,
        [str(live[0]), live[0], f"00{live[1]}", live[1], MISSING_ID, str(MISSING_ID)],
        {"reading_status": "read"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "updated": 2}
    assert [_row(db, item_id)["reading_status"] for item_id in live] == ["read", "read"]
    assert [_row(db, item_id) for item_id in (trashed, other)] == untouched


def test_live_missing_duplicate_and_trash_mix_never_mutates_trash(admin_client, db):
    live, trashed, other, new_location = _seed_selection(db)
    before = _snapshot(db)
    response = _post(admin_client, [live[0], trashed, MISSING_ID, live[0], str(live[1]), trashed], {
        "media_type": "audiobook", "location_id": new_location, "reading_status": "read",
        "owned": 1, "wishlisted": False, "series_name": "New series", "language": "ja",
    })

    assert response.status_code == 200
    after = _snapshot(db)
    for table in ("items", "item_copies", "list_items"):
        key = "id" if table == "items" else "item_id"
        assert [r for r in after[table] if r[key] in (trashed, other)] == [
            r for r in before[table] if r[key] in (trashed, other)
        ], f"{table} changed outside the live selection"
    assert response.json() == {"ok": True, "updated": 2}
    for item_id in live:
        row = _row(db, item_id)
        assert (row["media_type"], row["location_id"], row["reading_status"], row["owned"]) == (
            "audiobook", new_location, "read", 1,
        )
        assert (row["series_name"], row["language"]) == ("New series", "ja")
        assert not lists.is_member(db, lists.WISHLIST, item_id)
    assert {r["name"] for r in after["series_meta"]} == {"Trash series", "Other series"}


@pytest.mark.parametrize("selection", ["missing", "trashed", "both"])
def test_no_live_targets_succeed_with_zero_without_side_effects(admin_client, db, selection):
    _, trashed, _, new_location = _seed_selection(db)
    ids = {
        "missing": [MISSING_ID, str(MISSING_ID)],
        "trashed": [trashed, str(trashed)],
        "both": [trashed, MISSING_ID, trashed],
    }[selection]
    before = _snapshot(db)
    response = _post(admin_client, ids, {
        "location_id": str(new_location), "owned": "0", "wishlisted": True,
        "series_name": "New series", "language": "ja",
    })

    assert response.status_code == 200
    assert _snapshot(db) == before
    assert response.json() == {"ok": True, "updated": 0}


def test_no_value_change_still_counts_unique_live_targets_on_repeated_requests(admin_client, db):
    live, _, _, _ = _seed_selection(db)
    ids = [live[0], str(live[0]), live[1], MISSING_ID]
    for _ in range(2):
        response = _post(admin_client, ids, {"reading_status": "want_to_read"})
        assert response.status_code == 200
        assert response.json() == {"ok": True, "updated": 2}
        for item_id in live:
            row = _row(db, item_id)
            assert row["reading_status"] == "want_to_read"
            assert row["updated_at"] != OLD_STAMP


@pytest.mark.parametrize("invalid,message", INVALID_UPDATES)
def test_invalid_mixed_selection_updates_are_atomic(admin_client, db, invalid, message):
    live, trashed, _, new_location = _seed_selection(db)
    before = _snapshot(db)
    updates = {"location_id": new_location, "series_name": "New series", "language": "de", **invalid}
    response = _post(admin_client, [live[1], trashed, MISSING_ID, live[0], live[1]], updates)

    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": message}
    assert _snapshot(db) == before


def test_wishlist_over_mixed_owned_selection_refuses_every_target(admin_client, db):
    live, trashed, _, new_location = _seed_selection(db)
    before = _snapshot(db)
    response = _post(admin_client, [live[1], trashed, MISSING_ID, live[0], live[1]], {
        "wishlisted": True, "location_id": new_location, "series_name": "New series",
    })

    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "An owned item cannot be on the wishlist"}
    assert _snapshot(db) == before


@pytest.mark.parametrize("selection", ["missing", "trashed"])
@pytest.mark.parametrize("invalid,message", INVALID_UPDATES)
def test_empty_live_selection_preserves_validation_errors(admin_client, db, selection, invalid, message):
    _, trashed, _, _ = _seed_selection(db)
    ids = [MISSING_ID, str(MISSING_ID)] if selection == "missing" else [trashed, str(trashed)]
    before = _snapshot(db)
    response = _post(admin_client, ids, invalid)

    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": message}
    assert _snapshot(db) == before


@pytest.mark.parametrize("bad_id", ["not an ID", None, {}])
def test_invalid_ids_refuse_before_any_mutation(admin_client, db, bad_id):
    live, trashed, _, _ = _seed_selection(db)
    before = _snapshot(db)
    response = _post(admin_client, [live[0], bad_id, trashed], {"language": "fr"})

    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "Invalid item IDs"}
    assert _snapshot(db) == before


def test_csrf_is_still_required(admin_client, db):
    live, _, _, _ = _seed_selection(db)
    before = _snapshot(db)
    admin_client.headers.pop("X-CSRF-Token")
    response = _post(admin_client, live, {"language": "fr"})

    assert response.status_code == 403
    assert _snapshot(db) == before


def test_lock_probe_control_distinguishes_unlocked_and_locked_connections(db):
    _seed_selection(db)
    assert _probe_write_lock() == "acquired"
    db.execute("BEGIN IMMEDIATE")
    assert _probe_write_lock() == "locked"
    db.rollback()
    assert _probe_write_lock() == "acquired"


def test_live_selection_and_funnel_share_the_write_locked_connection(admin_client, db, monkeypatch):
    from app.routers import items as items_router

    live, trashed, _, new_location = _seed_selection(db)
    real_get_db = items_router.get_db
    real_update = items_router.update_items_fields
    connections, statements, read_probes, funnel_targets = [], [], [], []

    class ProbingConnection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, *args, **kwargs):
            normalized = " ".join(sql.upper().split())
            statements.append(normalized)
            if normalized.startswith("SELECT") and "FROM ITEMS_LIVE" in normalized:
                read_probes.append((self.conn.in_transaction, _probe_write_lock()))
            return self.conn.execute(sql, *args, **kwargs)

    @contextmanager
    def probing_get_db():
        with real_get_db() as conn:
            proxy = ProbingConnection(conn)
            connections.append(proxy)
            yield proxy

    def checked_update(conn, ids, fields):
        assert connections == [conn], "selection and funnel must use one connection"
        assert read_probes, "live targets must be selected before entering the write funnel"
        assert read_probes[0] == (True, "locked"), read_probes
        assert len(ids) == len(set(ids)) == 2
        assert set(ids) == set(live)
        funnel_targets.append(list(ids))
        return real_update(conn, ids, fields)

    monkeypatch.setattr(items_router, "get_db", probing_get_db)
    monkeypatch.setattr(items_router, "update_items_fields", checked_update)
    response = _post(admin_client, [live[0], trashed, MISSING_ID, live[1], live[0]], {
        "series_name": "New series", "location_id": new_location,
    })

    assert response.status_code == 200
    assert response.json() == {"ok": True, "updated": 2}
    assert funnel_targets and statements[0] == "BEGIN IMMEDIATE"
    assert all(probe == (True, "locked") for probe in read_probes)
    assert _probe_write_lock() == "acquired", "request must release the lock"


def test_targets_are_selected_after_a_competitor_commits_at_lock_entry(admin_client, db, monkeypatch):
    """Deterministic race seam: a row leaves and another enters the live set
    immediately before this route takes its lock. A pre-lock snapshot is stale.
    """
    from app.routers import items as items_router

    live, trashed, _, _ = _seed_selection(db)
    leaving = live[0]
    real_get_db = items_router.get_db
    competitor_rows = []

    class RacingConnection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, *args, **kwargs):
            if " ".join(sql.upper().split()) == "BEGIN IMMEDIATE":
                assert not competitor_rows, "the lock-entry seam must fire once"
                with real_get_db() as rival:
                    rival.execute("BEGIN IMMEDIATE")
                    assert item_write.trash_item(rival, leaving)
                    assert item_write.restore_item(rival, trashed)
                    competitor_rows.append(_row(rival, leaving))
            return self.conn.execute(sql, *args, **kwargs)

    @contextmanager
    def racing_get_db():
        with real_get_db() as conn:
            yield RacingConnection(conn)

    monkeypatch.setattr(items_router, "get_db", racing_get_db)
    response = _post(admin_client, [leaving, trashed, MISSING_ID, leaving], {"language": "fr"})

    assert competitor_rows, "the lock-entry race seam did not run"
    assert response.status_code == 200
    assert response.json() == {"ok": True, "updated": 1}
    assert _row(db, leaving) == competitor_rows[0]
    restored = _row(db, trashed)
    assert restored["deleted_at"] is None
    assert restored["language"] == "fr"
    assert _row(db, live[1])["language"] == "en"
