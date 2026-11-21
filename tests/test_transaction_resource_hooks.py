"""Resource recovery belongs to the outer connection, not a released savepoint."""

import sqlite3

import pytest

from app.database import _Connection, after_commit, after_rollback


def _connection(path):
    db = sqlite3.connect(path, factory=_Connection)
    assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    db.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    return db


def test_rollback_unwinds_all_resources_in_reverse_and_drops_success_callbacks(tmp_path):
    calls = []
    with _connection(tmp_path / "ordinary.db") as db:
        db.execute("INSERT INTO probe VALUES (1)")
        after_commit(db, lambda: calls.append("committed"))
        after_rollback(db, lambda: calls.append("first"))
        after_rollback(db, lambda: calls.append("second"))
        db.rollback()
        assert calls == ["second", "first"]
        assert db.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
    assert calls == ["second", "first"]
    db.close()


def test_released_savepoint_keeps_recovery_until_outer_commit_or_close(tmp_path):
    calls = []
    db = _connection(tmp_path / "ordinary.db")
    db.execute("BEGIN IMMEDIATE")
    db.execute("SAVEPOINT imported")
    db.execute("INSERT INTO probe VALUES (1)")
    after_rollback(db, lambda: calls.append("recovered"))
    db.execute("RELEASE imported")
    assert calls == []
    db.close()
    db.close()  # Native sqlite close is idempotent; resource ownership is too.
    assert calls == ["recovered"]


def test_successful_commit_runs_success_once_and_disarms_recovery(tmp_path):
    calls = []
    db = _connection(tmp_path / "ordinary.db")
    db.execute("INSERT INTO probe VALUES (1)")
    after_commit(db, lambda: calls.append("success"))
    after_rollback(db, lambda: calls.append("recovered"))
    db.commit()
    db.rollback()
    db.close()
    assert calls == ["success"]


def test_native_connection_context_commit_failure_rolls_back_and_recovers(tmp_path):
    calls = []
    db = _connection(tmp_path / "ordinary.db")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE child (parent INTEGER REFERENCES probe(id) DEFERRABLE INITIALLY DEFERRED)")
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            db.execute("INSERT INTO child VALUES (99)")
            after_commit(db, lambda: calls.append("success"))
            after_rollback(db, lambda: calls.append("recovered"))
    assert calls == ["recovered"]
    assert db.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
    db.close()


def test_plain_connection_cannot_pretend_to_own_rollback_recovery():
    with sqlite3.connect(":memory:") as db:
        with pytest.raises(ValueError, match="Shelf-managed"):
            after_rollback(db, lambda: pytest.fail("must not run eagerly"))


def test_direct_commit_failure_also_rolls_back_and_recovers(tmp_path):
    db = _connection(tmp_path / "ordinary.db")
    calls = []
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE child (parent INTEGER REFERENCES probe(id) DEFERRABLE INITIALLY DEFERRED)")
    db.execute("INSERT INTO child VALUES (99)")
    after_rollback(db, lambda: calls.append("recovered"))
    with pytest.raises(sqlite3.IntegrityError):
        db.commit()
    assert not db.in_transaction
    assert calls == ["recovered"]
    db.close()


def test_failing_cache_callback_cannot_skip_successful_resource_finalization(tmp_path):
    db = _connection(tmp_path / "ordinary.db")
    calls = []
    db.execute("INSERT INTO probe VALUES (1)")

    def failing_cache():
        raise RuntimeError("cache probe")

    after_commit(db, failing_cache)
    after_commit(db, lambda: calls.append("resource committed"))
    after_rollback(db, lambda: pytest.fail("committed resources cannot be undone"))
    with pytest.raises(RuntimeError, match="cache probe"):
        db.commit()
    assert calls == ["resource committed"]
    assert db.execute("SELECT id FROM probe").fetchone()[0] == 1
    db.rollback()
    db.close()


def test_one_recovery_failure_does_not_suppress_other_resource_recoveries(tmp_path):
    db = _connection(tmp_path / "ordinary.db")
    calls = []
    after_rollback(db, lambda: calls.append("earlier resource restored"))

    def failing_recovery():
        raise OSError("recovery probe")

    after_rollback(db, failing_recovery)
    with pytest.raises(OSError, match="recovery probe"):
        db.rollback()
    assert calls == ["earlier resource restored"]
    db.close()
