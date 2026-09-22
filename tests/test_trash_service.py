"""The Trash service: retention, the one expired predicate, the cached count.

Rows are trashed through the funnel functions directly and backdated with a raw
`UPDATE` on this connection — tests may spell the column; the `deleted_at`
source pin scans `app/` only.
"""

import time

import pytest

from app.database import get_setting, set_setting
from app.services import item_copies, item_write, trash
from app.services.item_write import insert_item


def _item(db, title="Trash Service", *, location_id=None):
    return insert_item(db, title=title, source="test", location_id=location_id)


def _location(db, name="Shelf A"):
    return db.execute("INSERT INTO locations (name) VALUES (?)", (name,)).lastrowid


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


def _set_retention(db, value):
    set_setting(db, "trash_retention_days", value)


def _two_copy_item(db, title):
    """An item with a primary and a secondary copy; returns (item, secondary)."""
    loc = _location(db, f"{title} shelf")
    item_id = _item(db, title, location_id=loc)
    secondary = item_copies.add_copy(db, item_id, {"location_id": loc})
    return item_id, secondary


class TestRetentionDays:
    def test_default_is_180(self, db):
        assert trash.get_retention_days(db) == 180

    def test_stored_value_is_read(self, db):
        _set_retention(db, "90")
        assert trash.get_retention_days(db) == 90

    def test_unparseable_value_falls_back_to_default(self, db):
        _set_retention(db, "abc")
        assert trash.get_retention_days(db) == 180

    def test_zero_is_kept_as_zero(self, db):
        _set_retention(db, "0")
        assert trash.get_retention_days(db) == 0


class TestExpiredClause:
    def test_matches_a_row_past_the_window_only(self, db):
        old = _item(db, "Old One")
        new = _item(db, "New One")
        item_write.trash_item(db, old)
        item_write.trash_item(db, new)
        _backdate_item(db, old, 200)
        _backdate_item(db, new, 10)
        sql, params = trash.expired_clause(180, "i")
        ids = {
            r["id"]
            for r in db.execute(f"SELECT id FROM items i WHERE {sql}", params)
        }
        assert ids == {old}

    def test_zero_days_matches_nothing(self):
        assert trash.expired_clause(0, "i") == ("0 = 1", [])

    def test_clause_is_parameterised_on_the_alias(self):
        sql, _ = trash.expired_clause(30, "c")
        assert "c.deleted_at IS NOT NULL" in sql
        assert "i.deleted_at" not in sql


class TestExpiredCount:
    def test_counts_items_and_copies_of_live_items(self, db):
        gone = _item(db, "Expired Item")
        item_write.trash_item(db, gone)
        _backdate_item(db, gone, 200)
        _live_item, secondary = _two_copy_item(db, "Live Holder")
        item_copies.trash_copy(db, secondary)
        _backdate_copy(db, secondary, 200)
        assert trash.expired_count(db) == 2

    def test_copy_of_a_trashed_item_is_counted_once_as_its_item(self, db):
        item_id, secondary = _two_copy_item(db, "Trashed Holder")
        item_copies.trash_copy(db, secondary)
        _backdate_copy(db, secondary, 200)
        item_write.trash_item(db, item_id)
        _backdate_item(db, item_id, 200)
        # The item counts; its individually trashed copy does not — it is
        # purged with the item, not on its own clock.
        assert trash.expired_count(db) == 1

    def test_recent_rows_are_not_counted(self, db):
        item_id = _item(db, "Recent")
        item_write.trash_item(db, item_id)
        _backdate_item(db, item_id, 10)
        assert trash.expired_count(db) == 0

    def test_zero_retention_issues_no_count_statement(self, db, monkeypatch):
        item_id = _item(db, "Would Expire")
        item_write.trash_item(db, item_id)
        _backdate_item(db, item_id, 200)
        _set_retention(db, "0")
        db.commit()

        statements = []
        real_get_db = trash.get_db

        from contextlib import contextmanager

        @contextmanager
        def traced_get_db():
            with real_get_db() as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        monkeypatch.setattr(trash, "get_db", traced_get_db)
        assert trash.expired_count() == 0
        assert not [s for s in statements if "item_copies" in s or "COUNT" in s]
        # And the zero is cached like any other answer.
        assert trash._cache is not None and trash._cache["count"] == 0

    def test_cached_for_an_hour(self, db, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])
        item_id = _item(db, "Cache Probe")
        item_write.trash_item(db, item_id)
        _backdate_item(db, item_id, 200)
        db.commit()
        assert trash.expired_count() == 1

        # A write no funnel announces (the backdate) is invisible until the
        # TTL lapses.
        other = _item(db, "Cache Probe Two")
        db.execute(
            "UPDATE items SET deleted_at = datetime('now', '-200 days') WHERE id = ?",
            (other,),
        )
        db.commit()
        clock[0] += trash.CACHE_TTL_SECONDS - 1
        assert trash.expired_count() == 1
        clock[0] += 2
        assert trash.expired_count() == 2

    def test_passing_a_connection_reads_fresh_and_leaves_the_cache(self, db):
        item_id = _item(db, "Fresh Read")
        item_write.trash_item(db, item_id)
        _backdate_item(db, item_id, 200)
        db.commit()
        assert trash.expired_count() == 1
        db.execute(
            "UPDATE items SET deleted_at = NULL WHERE id = ?", (item_id,)
        )
        assert trash.expired_count(db) == 0
        assert trash._cache["count"] == 1


class TestFunnelInvalidates:
    """Each of the four funnel functions drops the cached count."""

    def _prime(self):
        trash.expired_count()
        assert trash._cache is not None

    def test_trash_item_invalidates(self, db):
        item_id = _item(db, "Inv Trash")
        db.commit()
        self._prime()
        assert item_write.trash_item(db, item_id)
        assert trash._cache is None

    def test_restore_item_invalidates_and_the_count_moves_at_once(self, db):
        item_id = _item(db, "Inv Restore")
        item_write.trash_item(db, item_id)
        _backdate_item(db, item_id, 200)
        db.commit()
        assert trash.expired_count() == 1
        assert item_write.restore_item(db, item_id)
        db.commit()
        # No TTL wait: the restore dropped the cache.
        assert trash.expired_count() == 0

    def test_trash_copy_invalidates(self, db):
        _item_id, secondary = _two_copy_item(db, "Inv Copy")
        db.commit()
        self._prime()
        assert item_copies.trash_copy(db, secondary) is not None
        assert trash._cache is None

    def test_restore_copy_invalidates(self, db):
        _item_id, secondary = _two_copy_item(db, "Inv Copy Restore")
        item_copies.trash_copy(db, secondary)
        db.commit()
        self._prime()
        assert item_copies.restore_copy(db, secondary) is not None
        assert trash._cache is None

    def test_a_no_op_call_still_leaves_the_count_correct(self, db):
        item_id = _item(db, "Inv Noop")
        db.commit()
        self._prime()
        assert not item_write.restore_item(db, item_id)
        assert trash.expired_count() == 0


class TestSetSetting:
    def test_round_trips_and_overwrites(self, db):
        set_setting(db, "trash_retention_days", "30")
        assert get_setting(db, "trash_retention_days") == "30"
        set_setting(db, "trash_retention_days", "45")
        assert get_setting(db, "trash_retention_days") == "45"
        assert db.execute(
            "SELECT COUNT(*) FROM settings WHERE key = 'trash_retention_days'"
        ).fetchone()[0] == 1

    def test_upsert_setting_still_encrypts_a_sensitive_key(self, db):
        from app.routers.settings import _upsert_setting

        _upsert_setting(db, "hardcover_token", "plain-secret")
        stored = db.execute(
            "SELECT value FROM settings WHERE key = 'hardcover_token'"
        ).fetchone()["value"]
        assert stored and stored != "plain-secret"
        assert get_setting(db, "hardcover_token") == "plain-secret"


@pytest.fixture(autouse=True)
def _cache_starts_empty():
    # conftest's _isolated_db resets it; this makes the precondition explicit.
    assert trash._cache is None
    yield


class TestInvalidateAfterCommit:
    """A refill between a writer's invalidation and its commit must not stick.

    The writer invalidates inside its transaction; another request can refill
    the cache in that gap from a connection that still sees the old rows. The
    writer's connection invalidates again once it commits.
    """

    def _expired_item(self, db, title):
        item_id = _item(db, title)
        item_write.trash_item(db, item_id)
        _backdate_item(db, item_id, 200)
        db.commit()
        return item_id

    def test_a_refill_before_commit_is_dropped_by_the_commit(self, db):
        from app.database import get_db

        item_id = self._expired_item(db, "Race Restore")
        assert trash.expired_count() == 1
        with get_db() as writer:
            writer.execute("BEGIN IMMEDIATE")
            assert item_write.restore_item(writer, item_id)
            # Another request's render, before the restore commits.
            assert trash.expired_count() == 1
            writer.commit()
            assert trash.expired_count() == 0

    def test_a_rolled_back_write_runs_no_callback(self, db):
        from app.database import after_commit, get_db

        calls = []
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            after_commit(conn, lambda: calls.append("ran"))
            conn.rollback()
            conn.commit()
        assert calls == []

    def test_a_callback_runs_once_per_commit(self, db):
        from app.database import after_commit, get_db

        calls = []
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            after_commit(conn, lambda: calls.append("ran"))
            conn.commit()
            conn.commit()
        assert calls == ["ran"]

    def test_a_refill_that_straddles_an_invalidation_is_not_stored(self, db, monkeypatch):
        self._expired_item(db, "Straddle")
        real_count = trash._count

        def count_then_invalidate(conn, days):
            result = real_count(conn, days)
            trash.invalidate()  # a writer commits while this refill is reading
            return result

        monkeypatch.setattr(trash, "_count", count_then_invalidate)
        assert trash.expired_count() == 1
        assert trash._cache is None

    @pytest.mark.parametrize("route", ["item", "copy"])
    def test_a_purge_through_the_route_moves_the_cached_count(self, admin_client, db, route):
        if route == "item":
            target = self._expired_item(db, "Purge Item")
        else:
            _item_id, secondary = _two_copy_item(db, "Purge Copy")
            item_copies.trash_copy(db, secondary)
            _backdate_copy(db, secondary, 200)
            db.commit()
            target = secondary
        assert trash.expired_count() == 1
        path = f"/api/trash/{'items' if route == 'item' else 'copies'}/{target}"
        assert admin_client.delete(path).status_code == 200
        assert trash.expired_count() == 0
