"""The nightly price-alert task: `check_price_alerts()` gates and stamps, and
`_periodic_price_alerts()` stays offline under its test knob.

The pass itself (selection, drops, digest) is covered in
tests/test_price_alerts_service.py; this file pins the gate in `app/main.py`.
"""
import json
import logging
import time
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import _insert_item


def _set(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )


def _stamp(db):
    row = db.execute("SELECT value FROM settings WHERE key = 'price_alert_last_run'").fetchone()
    return row["value"] if row else None


def _rows(db):
    return db.execute("SELECT item_id, price FROM price_history ORDER BY id").fetchall()


def _seed_two(db, *, key=True, notify=True):
    a = _insert_item(db, title="Dune", isbn="9780441013593", wishlisted=True)
    b = _insert_item(db, title="Emma", isbn="9780141439587", wishlisted=True)
    if key:
        _set(db, "isbndb_api_key", "test-key")
    if notify:
        _set(db, "notify_url", "https://ntfy.example/shelf")
    return a, b


@pytest.fixture(autouse=True)
def _no_cache_file():
    # isbndb.CACHE_FILE is frozen at import time and conftest does not redirect
    # it, so an unpatched pass would write into the repo's data/ directory.
    with patch("app.services.isbndb._load_cache", return_value={}), \
         patch("app.services.isbndb._save_cache"):
        yield


def _lookup(price="12.99"):
    return patch("app.services.isbndb.lookup_price",
                 new=AsyncMock(return_value={"msrp": price}))


def _send(ok=True):
    return patch("app.services.notify.send_notification", new=AsyncMock(return_value=ok))


class TestGate:
    @pytest.mark.asyncio
    async def test_flag_off_is_a_no_op(self, db):
        from app.database import get_db
        from app.features import set_feature_enabled
        from app.main import check_price_alerts
        _seed_two(db)
        db.execute("COMMIT")
        with get_db() as conn:  # its own committed connection, so the flip is visible
            set_feature_enabled(conn, "price_alerts", False)

        with _lookup() as lookup, _send() as send:
            assert await check_price_alerts() is False
        lookup.assert_not_awaited()
        send.assert_not_awaited()
        assert _rows(db) == []
        assert _stamp(db) is None

    @pytest.mark.asyncio
    async def test_no_key_is_a_no_op(self, db, monkeypatch):
        from app.main import check_price_alerts
        monkeypatch.delenv("ISBNDB_API_KEY", raising=False)
        _seed_two(db, key=False)
        db.execute("COMMIT")

        with _lookup() as lookup:
            assert await check_price_alerts() is False
        lookup.assert_not_awaited()
        assert _rows(db) == []
        assert _stamp(db) is None

    @pytest.mark.asyncio
    async def test_recent_stamp_is_a_no_op(self, db):
        from app.main import check_price_alerts
        _seed_two(db)
        recent = str(time.time() - 3600)
        _set(db, "price_alert_last_run", recent)
        db.execute("COMMIT")

        with _lookup() as lookup:
            assert await check_price_alerts() is False
        lookup.assert_not_awaited()
        assert _rows(db) == []
        assert _stamp(db) == recent

    @pytest.mark.asyncio
    async def test_day_old_stamp_runs(self, db):
        from app.main import check_price_alerts
        _seed_two(db)
        _set(db, "price_alert_last_run", str(time.time() - 86400 - 60))
        db.execute("COMMIT")

        with _lookup() as lookup:
            assert await check_price_alerts() is True
        assert lookup.await_count == 2

    @pytest.mark.asyncio
    async def test_env_only_key_enables_the_pass(self, db, monkeypatch):
        """G15: the key is read through get_setting, so the env override counts."""
        from app.main import check_price_alerts
        monkeypatch.setenv("ISBNDB_API_KEY", "env-key")
        _seed_two(db, key=False)
        db.execute("COMMIT")

        with _lookup() as lookup:
            assert await check_price_alerts() is True
        assert lookup.await_count == 2
        assert lookup.await_args.args[1] == "env-key"


class TestPass:
    @pytest.mark.asyncio
    async def test_writes_one_row_per_item_and_stamps(self, db):
        from app.main import check_price_alerts
        a, b = _seed_two(db)
        db.execute("COMMIT")

        with _lookup("12.99"), _send() as send:
            assert await check_price_alerts() is True
        assert sorted(r["item_id"] for r in _rows(db)) == [a, b]
        assert all(r["price"] == 12.99 for r in _rows(db))
        # First observations are never drops, so nothing is sent.
        send.assert_not_awaited()
        assert _stamp(db) is not None

    @pytest.mark.asyncio
    async def test_qualifying_drop_sends_one_digest(self, db):
        from app.main import check_price_alerts
        a, b = _seed_two(db)
        for item_id in (a, b):
            db.execute("INSERT INTO price_history (item_id, price) VALUES (?, 20.0)", (item_id,))
        db.execute("COMMIT")

        with _lookup("10.00"), _send() as send:
            assert await check_price_alerts() is True
        send.assert_awaited_once()
        url, title, body, fmt = send.await_args.args
        assert url == "https://ntfy.example/shelf"
        assert fmt == "ntfy"
        assert title == "Shelf: 2 wishlist price drops"
        assert "Dune" in body and "Emma" in body
        assert "20.00" in body and "10.00" in body

    @pytest.mark.asyncio
    async def test_no_notify_url_writes_rows_and_stamps(self, db):
        from app.main import check_price_alerts
        a, _ = _seed_two(db, notify=False)
        db.execute("INSERT INTO price_history (item_id, price) VALUES (?, 20.0)", (a,))
        db.execute("COMMIT")

        with _lookup("10.00"), _send() as send:
            assert await check_price_alerts() is True
        send.assert_not_awaited()
        assert len(_rows(db)) == 3
        assert _stamp(db) is not None

    @pytest.mark.asyncio
    async def test_failed_send_still_stamps_and_is_not_retried(self, db):
        """Deliberately unlike the loan digest, which stamps only on a sent
        digest and retries five minutes later: a pass costs up to `cap` ISBNdb
        lookups, and re-running them is what the daily throttle prevents."""
        from app.main import check_price_alerts
        a, _ = _seed_two(db)
        db.execute("INSERT INTO price_history (item_id, price) VALUES (?, 20.0)", (a,))
        db.execute("COMMIT")

        with _lookup("10.00"), _send(ok=False) as send:
            assert await check_price_alerts() is True
        send.assert_awaited_once()
        assert _stamp(db) is not None

        with _lookup("5.00") as lookup, _send() as send:
            assert await check_price_alerts() is False
        lookup.assert_not_awaited()
        send.assert_not_awaited()


class TestPassSummary:
    """test-drive Obs 2: the pass leaves a summary Settings can show, and
    warns in the log when ISBNdb did not answer."""

    @pytest.mark.asyncio
    async def test_refused_key_is_summarised_and_warned(self, db, caplog):
        from app.main import check_price_alerts
        from app.services import isbndb
        _seed_two(db)
        db.execute("COMMIT")
        refused = patch("app.services.isbndb.lookup_price",
                        AsyncMock(side_effect=isbndb.LookupFailed(401)))
        with refused, _send(), caplog.at_level(logging.WARNING, logger="app.main"):
            assert await check_price_alerts() is True
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'price_alert_last_summary'").fetchone()
        assert json.loads(row["value"]) == {"looked_up": 1, "failed": 1, "status": 401}
        assert _stamp(db) is not None
        assert any("1 of 1 ISBNdb lookup(s) failed (last status: 401)" in r.getMessage()
                   for r in caplog.records)

    @pytest.mark.asyncio
    async def test_clean_pass_is_summarised_without_a_warning(self, db, caplog):
        from app.main import check_price_alerts
        _seed_two(db)
        db.execute("COMMIT")
        with _lookup("12.99"), _send(), caplog.at_level(logging.WARNING, logger="app.main"):
            assert await check_price_alerts() is True
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'price_alert_last_summary'").fetchone()
        assert json.loads(row["value"]) == {"looked_up": 2, "failed": 0, "status": None}
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestPeriodicLoop:
    @pytest.mark.asyncio
    async def test_returns_before_sleeping_under_the_test_knob(self, monkeypatch):
        from app.main import _periodic_price_alerts
        monkeypatch.setenv("SHELF_DISABLE_PRICE_ALERTS", "1")
        with patch("app.main.asyncio.sleep", new=AsyncMock()) as sleep, \
             patch("app.main.check_price_alerts", new=AsyncMock()) as check:
            await _periodic_price_alerts()
        sleep.assert_not_awaited()
        check.assert_not_awaited()
