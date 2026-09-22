"""The Trash API: restore, delete permanently, empty expired.

Rows are seeded and trashed through the funnel functions, then committed
before the first request (G48). Expiry is a raw backdate on this connection.
"""

import sqlite3
from contextlib import contextmanager

import pytest

from app.services import item_copies, item_write, trash
from app.services.item_write import insert_item
from tests.conftest import _insert_borrower, _insert_location


def _item(db, title="Trash Route", **fields):
    return insert_item(db, title=title, source="test", **fields)


def _item_with_copies(db, title, n=2):
    """An item with a primary copy and `n - 1` secondaries; returns (item, [copy ids])."""
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


def _backdate_copy(db, copy_id, days):
    db.execute(
        "UPDATE item_copies SET deleted_at = datetime('now', ?) WHERE id = ?",
        (f"-{days} days", copy_id),
    )


def _physical_item(db, item_id):
    return db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def _physical_copy(db, copy_id):
    return db.execute("SELECT * FROM item_copies WHERE id = ?", (copy_id,)).fetchone()


def _tag(db, item_id, name="keepme"):
    tag_id = db.execute("INSERT INTO tags (name) VALUES (?)", (name,)).lastrowid
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item_id, tag_id))
    return tag_id


def _loan(db, item_id, *, open_=True):
    bid = _insert_borrower(db, f"Borrower {item_id}")
    return db.execute(
        "INSERT INTO checkouts (item_id, borrower_id, checked_in) VALUES (?, ?, ?)",
        (item_id, bid, None if open_ else "2026-01-01"),
    ).lastrowid


def _install_lock_probe(monkeypatch, sql_prefix):
    """Record whether a rival writer can take the lock when `sql_prefix` runs.

    Wraps the Trash router's `get_db` so each statement starting with
    `sql_prefix` is followed by a zero-timeout rival `BEGIN IMMEDIATE`; the
    returned list gains "acquired" or "locked: …" per hit.
    """
    import app.config
    import app.routers.trash as trash_router

    probe_results = []
    real_get_db = trash_router.get_db

    class LockProbingConnection:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *args, **kwargs):
            result = self._conn.execute(sql, *args, **kwargs)
            if sql.startswith(sql_prefix):
                rival = sqlite3.connect(str(app.config.DATABASE_PATH), timeout=0)
                try:
                    rival.execute("BEGIN IMMEDIATE")
                    probe_results.append("acquired")
                    rival.rollback()
                except sqlite3.OperationalError as exc:
                    probe_results.append(f"locked: {exc}")
                finally:
                    rival.close()
            return result

    @contextmanager
    def probing_get_db():
        with real_get_db() as conn:
            yield LockProbingConnection(conn)

    monkeypatch.setattr(trash_router, "get_db", probing_get_db)
    return probe_results


class TestRestoreItem:
    def test_restores_with_loans_tags_and_copies_intact(self, editor_client, db):
        item_id, copies = _item_with_copies(db, "Round Trip")
        _tag(db, item_id)
        loan = _loan(db, item_id)
        item_write.trash_item(db, item_id)
        db.commit()

        resp = editor_client.post(f"/api/trash/items/{item_id}/restore")
        assert resp.status_code == 200
        assert db.execute("SELECT 1 FROM items_live WHERE id = ?", (item_id,)).fetchone()
        assert db.execute(
            "SELECT COUNT(*) FROM item_tags WHERE item_id = ?", (item_id,)
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT checked_in FROM checkouts WHERE id = ?", (loan,)
        ).fetchone()["checked_in"] is None
        assert db.execute(
            "SELECT COUNT(*) FROM copies_live WHERE item_id = ?", (item_id,)
        ).fetchone()[0] == len(copies)

    def test_restoring_a_live_item_is_idempotent(self, editor_client, db):
        item_id = _item(db, "Already Live")
        db.commit()
        assert editor_client.post(f"/api/trash/items/{item_id}/restore").status_code == 200

    def test_restoring_a_missing_item_is_404(self, editor_client):
        assert editor_client.post("/api/trash/items/99999/restore").status_code == 404


class TestRestoreCopy:
    def test_restores_a_trashed_copy_of_a_live_item_as_secondary(self, editor_client, db):
        item_id, (primary, secondary) = _item_with_copies(db, "Copy Back")
        item_copies.trash_copy(db, secondary)
        db.commit()

        resp = editor_client.post(f"/api/trash/copies/{secondary}/restore")
        assert resp.status_code == 200
        row = db.execute("SELECT * FROM copies_live WHERE id = ?", (secondary,)).fetchone()
        assert row is not None and row["is_primary"] == 0
        assert db.execute(
            "SELECT is_primary FROM copies_live WHERE id = ?", (primary,)
        ).fetchone()["is_primary"] == 1

    def test_restored_trashed_primary_returns_as_secondary(self, editor_client, db):
        item_id, (primary, secondary) = _item_with_copies(db, "Old Primary")
        item_copies.trash_copy(db, primary)  # promotes `secondary`
        db.commit()

        assert editor_client.post(f"/api/trash/copies/{primary}/restore").status_code == 200
        assert db.execute(
            "SELECT is_primary FROM copies_live WHERE id = ?", (primary,)
        ).fetchone()["is_primary"] == 0
        assert db.execute(
            "SELECT is_primary FROM copies_live WHERE id = ?", (secondary,)
        ).fetchone()["is_primary"] == 1

    def test_copy_of_a_trashed_item_is_refused_and_nothing_written(self, editor_client, db):
        item_id, (_primary, secondary) = _item_with_copies(db, "Item Gone")
        item_copies.trash_copy(db, secondary)
        item_write.trash_item(db, item_id)
        db.commit()
        before = _physical_copy(db, secondary)["deleted_at"]

        resp = editor_client.post(f"/api/trash/copies/{secondary}/restore")
        assert resp.status_code == 200
        assert _physical_copy(db, secondary)["deleted_at"] == before
        assert _physical_item(db, item_id)["deleted_at"] is not None

    def test_already_live_copy_answers_unchanged(self, editor_client, db):
        _item_id, (_primary, secondary) = _item_with_copies(db, "Live Copy")
        db.commit()
        before = dict(_physical_copy(db, secondary))

        assert editor_client.post(f"/api/trash/copies/{secondary}/restore").status_code == 200
        assert dict(_physical_copy(db, secondary)) == before

    def test_missing_copy_is_404(self, editor_client):
        assert editor_client.post("/api/trash/copies/99999/restore").status_code == 404

    def test_copy_state_tells_the_three_none_cases_apart(self, db):
        item_id, (_primary, live, trashed_copy) = _item_with_copies(db, "States", n=3)
        item_copies.trash_copy(db, trashed_copy)
        assert trash.copy_state(db, 99999) is None
        assert trash.copy_state(db, live) == "live"
        assert trash.copy_state(db, trashed_copy) == "trashed"
        item_write.trash_item(db, item_id)
        assert trash.copy_state(db, live) == "item_trashed"
        assert trash.copy_state(db, trashed_copy) == "item_trashed"


class TestPurgeItem:
    def test_purge_fires_the_cascades_and_nulls_scan_log(self, admin_client, db):
        import app.config

        item_id, copies = _item_with_copies(db, "Purge Me")
        _tag(db, item_id)
        _loan(db, item_id)
        cover = app.config.COVERS_DIR / "purge-me.jpg"
        cover.parent.mkdir(parents=True, exist_ok=True)
        cover.write_bytes(b"jpeg")
        db.execute("UPDATE items SET cover_path = ? WHERE id = ?", ("purge-me.jpg", item_id))
        log_id = db.execute(
            "INSERT INTO scan_log (isbn, media_type, result, item_id) VALUES (?, ?, ?, ?)",
            ("9780000000001", "book", "added", item_id),
        ).lastrowid
        item_write.trash_item(db, item_id)
        db.commit()

        resp = admin_client.delete(f"/api/trash/items/{item_id}")
        assert resp.status_code == 200
        assert _physical_item(db, item_id) is None
        for table in ("checkouts", "item_tags", "item_copies"):
            assert db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE item_id = ?", (item_id,)
            ).fetchone()[0] == 0, table
        assert db.execute(
            "SELECT item_id FROM scan_log WHERE id = ?", (log_id,)
        ).fetchone()["item_id"] is None
        # Out of scope, pinned so nobody adds an unlink by accident: no delete
        # path removes a cover file.
        assert cover.exists()

    def test_purging_a_live_item_is_refused(self, admin_client, db):
        item_id = _item(db, "Still Live")
        db.commit()
        assert admin_client.delete(f"/api/trash/items/{item_id}").status_code == 404
        assert _physical_item(db, item_id)["deleted_at"] is None


class TestPurgeCopy:
    def test_purge_removes_only_that_copy_and_mints_no_primary(self, admin_client, db):
        item_id, (primary, secondary, third) = _item_with_copies(db, "Copy Purge", n=3)
        item_copies.trash_copy(db, secondary)
        db.commit()
        before = {
            r["id"]: r["is_primary"]
            for r in db.execute("SELECT id, is_primary FROM item_copies WHERE item_id = ?", (item_id,))
            if r["id"] != secondary
        }

        assert admin_client.delete(f"/api/trash/copies/{secondary}").status_code == 200
        assert _physical_copy(db, secondary) is None
        after = {
            r["id"]: r["is_primary"]
            for r in db.execute("SELECT id, is_primary FROM item_copies WHERE item_id = ?", (item_id,))
        }
        assert after == before

    def test_purging_a_live_copy_is_refused(self, admin_client, db):
        _item_id, (_primary, secondary) = _item_with_copies(db, "Live Copy Purge")
        db.commit()
        assert admin_client.delete(f"/api/trash/copies/{secondary}").status_code == 404
        assert _physical_copy(db, secondary) is not None


class TestEmptyExpired:
    def _seed(self, db):
        old = _item(db, "Old Trash")
        new = _item(db, "New Trash")
        item_write.trash_item(db, old)
        item_write.trash_item(db, new)
        _backdate_item(db, old, 200)
        _backdate_item(db, new, 10)
        live_item, (_p1, live_copy) = _item_with_copies(db, "Live Holder")
        item_copies.trash_copy(db, live_copy)
        _backdate_copy(db, live_copy, 200)
        gone_item, (_p2, gone_copy) = _item_with_copies(db, "Trashed Holder")
        item_copies.trash_copy(db, gone_copy)
        _backdate_copy(db, gone_copy, 200)
        item_write.trash_item(db, gone_item)  # trashed now: not expired itself
        db.commit()
        return old, new, live_copy, gone_item, gone_copy

    def test_purges_only_what_is_past_the_window(self, admin_client, db):
        old, new, live_copy, gone_item, gone_copy = self._seed(db)

        assert admin_client.post("/api/trash/empty-expired").status_code == 200
        assert _physical_item(db, old) is None
        assert _physical_item(db, new) is not None
        assert _physical_copy(db, live_copy) is None
        # A copy of a trashed item is left to its item.
        assert _physical_copy(db, gone_copy) is not None
        assert _physical_item(db, gone_item) is not None

    def test_zero_retention_purges_nothing(self, admin_client, db):
        old, new, live_copy, _gone_item, gone_copy = self._seed(db)
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('trash_retention_days', '0')"
        )
        db.commit()

        assert admin_client.post("/api/trash/empty-expired").status_code == 200
        assert _physical_item(db, old) is not None
        assert _physical_copy(db, live_copy) is not None

    def test_the_set_is_chosen_under_the_write_lock(self, admin_client, db, monkeypatch):
        """G18: a rival writer must already be locked out when expired_ids reads."""
        self._seed(db)
        probe_results = _install_lock_probe(monkeypatch, "SELECT i.id FROM items i WHERE")
        assert admin_client.post("/api/trash/empty-expired").status_code == 200
        assert probe_results, "expired_ids never ran — the probe did not fire"
        assert probe_results[0].startswith("locked"), (
            f"got {probe_results[0]!r} — Empty expired chose its set outside "
            "the write lock (G18)"
        )


class TestGuardReadsAreUnderTheLock:
    """G18 for the single-row routes: the read that decides runs under the lock.

    Restore item is not here: its guard is the `UPDATE` itself, which takes
    the write lock on its own, so no probe can tell a missing `BEGIN
    IMMEDIATE` apart there.
    """

    def _purge_item_target(self, db):
        item_id = _item(db, "Probe Purge Item")
        item_write.trash_item(db, item_id)
        return "delete", f"/api/trash/items/{item_id}"

    def _copy_target(self, db, method, suffix):
        _item_id, (_primary, secondary) = _item_with_copies(db, f"Probe {suffix}")
        item_copies.trash_copy(db, secondary)
        return method, f"/api/trash/copies/{secondary}{suffix}"

    @pytest.mark.parametrize("route,guard_sql", [
        ("purge_item", "SELECT id FROM items WHERE id = ? AND deleted_at IS NOT NULL"),
        ("purge_copy", "SELECT id FROM item_copies WHERE id = ? AND deleted_at IS NOT NULL"),
        ("restore_copy", "SELECT c.deleted_at AS copy_deleted"),
    ])
    def test_the_guard_read_holds_the_write_lock(self, admin_client, db, monkeypatch, route, guard_sql):
        if route == "purge_item":
            method, path = self._purge_item_target(db)
        elif route == "purge_copy":
            method, path = self._copy_target(db, "delete", "")
        else:
            method, path = self._copy_target(db, "post", "/restore")
        db.commit()
        probe_results = _install_lock_probe(monkeypatch, guard_sql)
        assert getattr(admin_client, method)(path).status_code == 200
        assert probe_results, f"{route}'s guard read never ran — the probe did not fire"
        assert probe_results[0].startswith("locked"), (
            f"got {probe_results[0]!r} — {route} read its guard outside the write lock (G18)"
        )


class TestStaleWritesToATrashedItem:
    """A page opened before the item went to Trash can still post to it."""

    def test_checkout_refuses_a_trashed_item(self, editor_client, db):
        item_id = _item(db, "Stale Lend")
        borrower = _insert_borrower(db, "Stale Borrower")
        item_write.trash_item(db, item_id)
        db.commit()
        resp = editor_client.post(
            f"/api/items/{item_id}/checkout",
            data={"borrower_id": borrower}, follow_redirects=False,
        )
        assert resp.status_code == 404
        assert db.execute(
            "SELECT COUNT(*) AS n FROM checkouts WHERE item_id = ?", (item_id,)
        ).fetchone()["n"] == 0

    def test_edit_refuses_a_trashed_item(self, editor_client, db):
        item_id = _item(db, "Stale Edit")
        item_write.trash_item(db, item_id)
        db.commit()
        resp = editor_client.post(
            f"/api/items/{item_id}",
            data={"title": "Renamed In Trash"}, follow_redirects=False,
        )
        assert resp.status_code == 404
        assert _physical_item(db, item_id)["title"] == "Stale Edit"


class TestRoles:
    EDITOR_ROUTES = [
        ("post", "/api/trash/items/1/restore"),
        ("post", "/api/trash/copies/1/restore"),
    ]
    ADMIN_ROUTES = [
        ("delete", "/api/trash/items/1"),
        ("delete", "/api/trash/copies/1"),
        ("post", "/api/trash/empty-expired"),
    ]

    @pytest.mark.parametrize("method,path", EDITOR_ROUTES + ADMIN_ROUTES)
    def test_viewer_is_refused_everywhere(self, viewer_client, method, path):
        assert getattr(viewer_client, method)(path).status_code == 403

    @pytest.mark.parametrize("method,path", ADMIN_ROUTES)
    def test_editor_is_refused_the_admin_routes(self, editor_client, method, path):
        assert getattr(editor_client, method)(path).status_code == 403

    @pytest.mark.parametrize("method,path", EDITOR_ROUTES)
    def test_editor_reaches_the_restore_routes(self, editor_client, method, path):
        # 404: no such row — but past the role check.
        assert getattr(editor_client, method)(path).status_code == 404
