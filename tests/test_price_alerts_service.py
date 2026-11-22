"""The price-alert service: candidate selection, drop arithmetic, digest, pass."""

import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.services import isbndb, price_alerts
from tests.conftest import _insert_item

ISBN_A = "9780000000026"
ISBN_B = "9780000000019"


def _wish(db, n, **kw):
    return _insert_item(db, title=f"Book {n}", isbn=f"97800000{n:05d}"[:13], wishlisted=True, **kw)


def _obs(db, item_id, price, at):
    db.execute(
        "INSERT INTO price_history (item_id, price, observed_at) VALUES (?, ?, ?)",
        (item_id, price, at),
    )


def _ids(rows):
    return [r["id"] for r in rows]


class TestSettings:
    def test_defaults_when_missing(self, db):
        assert price_alerts.get_threshold_pct(db) == 15
        assert price_alerts.get_nightly_cap(db) == 100

    @pytest.mark.parametrize("raw,expected", [
        ("20", 20), ("1", 1), ("100", 100), ("0", 15), ("101", 15), ("-3", 15), ("abc", 15),
    ])
    def test_threshold_bounds(self, db, raw, expected):
        db.execute("INSERT INTO settings (key, value) VALUES ('price_alert_threshold_pct', ?)", (raw,))
        assert price_alerts.get_threshold_pct(db) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("250", 250), ("500", 500), ("501", 100), ("0", 100), ("x", 100),
    ])
    def test_cap_bounds(self, db, raw, expected):
        db.execute("INSERT INTO settings (key, value) VALUES ('price_alert_nightly_cap', ?)", (raw,))
        assert price_alerts.get_nightly_cap(db) == expected


class TestSelectCandidates:
    def test_only_live_wishlisted_isbn_items(self, db):
        live = _wish(db, 1)
        trashed = _wish(db, 2)
        db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (trashed,))
        _insert_item(db, title="Owned only", isbn="9780000000033")
        no_isbn = _insert_item(db, title="No ISBN", isbn=None, wishlisted=True)
        blank = _insert_item(db, title="Blank ISBN", isbn="", wishlisted=True)
        rows = price_alerts.select_candidates(db, 50)
        assert _ids(rows) == [live]
        assert no_isbn not in _ids(rows) and blank not in _ids(rows)

    def test_never_observed_first_then_oldest_then_id(self, db):
        a, b, c, d = (_wish(db, n) for n in range(1, 5))
        _obs(db, a, 10.0, "2026-09-03 00:00:00")
        _obs(db, c, 10.0, "2026-09-01 00:00:00")
        # b and d never observed; a observed later than c
        assert _ids(price_alerts.select_candidates(db, 10)) == [b, d, c, a]

    def test_ties_break_by_id(self, db):
        a, b = _wish(db, 1), _wish(db, 2)
        _obs(db, b, 1.0, "2026-09-01 00:00:00")
        _obs(db, a, 1.0, "2026-09-01 00:00:00")
        assert _ids(price_alerts.select_candidates(db, 10)) == [a, b]

    def test_null_observation_counts_as_observed(self, db):
        a, b = _wish(db, 1), _wish(db, 2)
        _obs(db, a, None, "2026-09-01 00:00:00")
        # a was looked at (a miss), b never was: b leads, a does not.
        assert _ids(price_alerts.select_candidates(db, 10)) == [b, a]
        _obs(db, b, 5.0, "2026-09-02 00:00:00")
        assert _ids(price_alerts.select_candidates(db, 10)) == [a, b]

    def test_cap_is_honoured(self, db):
        for n in range(1, 6):
            _wish(db, n)
        assert len(price_alerts.select_candidates(db, 3)) == 3

    def test_two_passes_cycle_through_all(self, db):
        ids = [_wish(db, n) for n in range(1, 6)]
        seen = []
        for step in range(2):
            batch = price_alerts.select_candidates(db, 3)
            seen.extend(_ids(batch))
            for r in batch:
                _obs(db, r["id"], 1.0, f"2026-09-0{step + 1} 00:00:00")
        assert set(seen[:3]) == set(ids[:3])
        assert set(seen) == set(ids)


class TestDrops:
    def test_drop_pct_arithmetic(self):
        assert price_alerts.drop_pct(100.0, 85.0) == 15.0
        assert price_alerts.drop_pct(100.0, 90.0) == 10.0
        assert price_alerts.drop_pct(100.0, 110.0) < 0
        assert price_alerts.drop_pct(None, 10.0) is None
        assert price_alerts.drop_pct(10.0, None) is None
        assert price_alerts.drop_pct(0, 5.0) is None

    def test_previous_price_skips_null_and_later_rows(self, db):
        a = _wish(db, 1)
        first = price_alerts.record_observation(db, a, 20.0)
        miss = price_alerts.record_observation(db, a, None)
        now = price_alerts.record_observation(db, a, 15.0)
        assert price_alerts.previous_price(db, a, first) is None
        assert price_alerts.previous_price(db, a, miss) == 20.0
        assert price_alerts.previous_price(db, a, now) == 20.0

    def test_previous_price_is_per_item(self, db):
        a, b = _wish(db, 1), _wish(db, 2)
        price_alerts.record_observation(db, a, 50.0)
        bid = price_alerts.record_observation(db, b, 10.0)
        assert price_alerts.previous_price(db, b, bid) is None


class TestDigest:
    def test_title_pluralisation_and_money(self, db):
        db.commit()
        one = price_alerts.build_digest([{"title": "Dune", "previous": 20.0, "price": 15.0}])
        assert one[0] == "Shelf: 1 wishlist price drop"
        assert one[1] == "Dune — $20.00 → $15.00 (−25%)"
        two = price_alerts.build_digest([
            {"title": "A", "previous": 10.0, "price": 8.0},
            {"title": "B", "previous": 1000.0, "price": 500.0},
        ])
        assert two[0] == "Shelf: 2 wishlist price drops"
        assert "$1,000.00 → $500.00 (−50%)" in two[1]
        assert two[1].count("\n") == 1

    def test_money_goes_through_format_money(self, db):
        db.commit()
        with patch("app.currency.format_money", side_effect=lambda v: f"<{v}>"):
            _, body = price_alerts.build_digest([{"title": "X", "previous": 4.0, "price": 2.0}])
        assert "<4.0> → <2.0>" in body


class TestPriceLine:
    def test_none_without_priced_rows(self, db):
        a = _wish(db, 1)
        assert price_alerts.price_line(db, a) is None
        _obs(db, a, None, "2026-09-01 00:00:00")
        assert price_alerts.price_line(db, a) is None

    def test_current_only(self, db):
        a = _wish(db, 1)
        _obs(db, a, 12.5, "2026-09-01 03:04:05")
        assert price_alerts.price_line(db, a) == {
            "current": 12.5, "current_on": "2026-09-01", "stale": False,
            "was": None, "was_on": None}

    def test_was_when_earlier_differs(self, db):
        a = _wish(db, 1)
        _obs(db, a, 20.0, "2026-08-01 00:00:00")
        _obs(db, a, 15.0, "2026-09-01 00:00:00")
        line = price_alerts.price_line(db, a)
        assert (line["current"], line["was"], line["was_on"]) == (15.0, 20.0, "2026-08-01")

    def test_no_was_when_earlier_equal(self, db):
        a = _wish(db, 1)
        _obs(db, a, 15.0, "2026-08-01 00:00:00")
        _obs(db, a, 15.0, "2026-09-01 00:00:00")
        line = price_alerts.price_line(db, a)
        assert line["was"] is None and line["was_on"] is None

    def test_was_skips_equal_run_and_nulls(self, db):
        a = _wish(db, 1)
        _obs(db, a, 20.0, "2026-07-01 00:00:00")
        _obs(db, a, 15.0, "2026-08-01 00:00:00")
        _obs(db, a, None, "2026-08-15 00:00:00")
        _obs(db, a, 15.0, "2026-09-01 00:00:00")
        _obs(db, a, None, "2026-09-02 00:00:00")
        line = price_alerts.price_line(db, a)
        assert line["current"] == 15.0 and line["current_on"] == "2026-09-01"
        assert line["was"] == 20.0 and line["was_on"] == "2026-07-01"
        assert line["stale"] is True  # the newest row is the 09-02 miss

    def test_not_stale_when_newest_row_is_priced(self, db):
        a = _wish(db, 1)
        _obs(db, a, 20.0, "2026-08-01 00:00:00")
        _obs(db, a, None, "2026-08-15 00:00:00")
        _obs(db, a, 15.0, "2026-09-01 00:00:00")
        assert price_alerts.price_line(db, a)["stale"] is False


class TestLookupPriceCacheBypass:
    def _client(self, calls):
        def responder(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"book": {"title": "Fresh", "authors": [], "msrp": "7.50"}})
        return responder

    @respx.mock
    async def test_use_cache_false_hits_client_and_rewrites_entry(self, monkeypatch):
        monkeypatch.setattr(isbndb.outbound, "acquire", AsyncMock())
        calls = []
        respx.get(f"https://api2.isbndb.com/book/{ISBN_B}").mock(side_effect=self._client(calls))
        cache = {ISBN_B: {"data": {"title": "Stale", "msrp": "1.00"}, "fetched_at": time.time()}}
        async with httpx.AsyncClient() as client:
            result = await isbndb.lookup_price(ISBN_B, "k", client, cache, use_cache=False)
        assert calls and result["msrp"] == "7.50"
        assert cache[ISBN_B]["data"]["msrp"] == "7.50"

    @respx.mock
    @pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
    async def test_failed_request_leaves_a_good_entry_alone(self, monkeypatch, status):
        """test-drive Obs 1: a refused or rate-limited watcher lookup must not
        overwrite the year-long entry valuation reads."""
        monkeypatch.setattr(isbndb.outbound, "acquire", AsyncMock())
        respx.get(f"https://api2.isbndb.com/book/{ISBN_B}").mock(return_value=httpx.Response(status))
        good = {"data": {"title": "Kept", "msrp": "20.00"}, "fetched_at": time.time()}
        cache = {ISBN_B: dict(good)}
        async with httpx.AsyncClient() as client:
            result = await isbndb.lookup_price(ISBN_B, "k", client, cache, use_cache=False)
            assert result is None
            # valuation's read still sees the good price, without a request
            again = await isbndb.lookup_price(ISBN_B, "k", client, cache)
        assert cache[ISBN_B] == good
        assert again["msrp"] == "20.00"

    @respx.mock
    async def test_timeout_leaves_cache_untouched(self, monkeypatch):
        monkeypatch.setattr(isbndb.outbound, "acquire", AsyncMock())
        respx.get(f"https://api2.isbndb.com/book/{ISBN_B}").mock(side_effect=httpx.ReadTimeout("slow"))
        cache = {}
        async with httpx.AsyncClient() as client:
            assert await isbndb.lookup_price(ISBN_B, "k", client, cache, use_cache=False) is None
        assert cache == {}

    @respx.mock
    async def test_not_found_is_cached_as_a_miss(self, monkeypatch):
        monkeypatch.setattr(isbndb.outbound, "acquire", AsyncMock())
        respx.get(f"https://api2.isbndb.com/book/{ISBN_B}").mock(return_value=httpx.Response(404))
        cache = {}
        async with httpx.AsyncClient() as client:
            assert await isbndb.lookup_price(ISBN_B, "k", client, cache) is None
        assert cache[ISBN_B]["data"] is None

    @respx.mock
    async def test_default_still_returns_cached(self, monkeypatch):
        monkeypatch.setattr(isbndb.outbound, "acquire", AsyncMock())
        calls = []
        respx.get(f"https://api2.isbndb.com/book/{ISBN_B}").mock(side_effect=self._client(calls))
        cache = {ISBN_B: {"data": {"title": "Stale", "msrp": "1.00"}, "fetched_at": time.time()}}
        async with httpx.AsyncClient() as client:
            result = await isbndb.lookup_price(ISBN_B, "k", client, cache)
        assert not calls and result["msrp"] == "1.00"


def _count(db):
    return db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]


@pytest.fixture
def cache_stubs():
    """G: isbndb.CACHE_FILE is frozen at import, so never touch the real one."""
    with patch("app.services.isbndb._load_cache", return_value={}) as load, \
         patch("app.services.isbndb._save_cache") as save:
        yield load, save


class TestRunPass:
    async def _run(self, **kw):
        args = {"threshold": 15, "cap": 100, "notify_url": "http://n.test/t", "notify_format": "ntfy"}
        args.update(kw)
        return await price_alerts.run_pass("key", **args)

    async def test_one_row_per_candidate_and_one_digest(self, db, cache_stubs):
        a, b, c = _wish(db, 1), _wish(db, 2), _wish(db, 3)
        _obs(db, a, 20.0, "2026-09-01 00:00:00")
        _obs(db, b, 20.0, "2026-09-01 00:00:00")
        db.commit()
        lookup = AsyncMock(side_effect=[{"msrp": "10.00"}, {"msrp": "19.00"}, {"msrp": "5.00"}])
        send = AsyncMock(return_value=True)
        with patch("app.services.isbndb.lookup_price", lookup), \
             patch("app.services.notify.send_notification", send):
            # order: c (never observed), a, b
            result = await self._run()
        assert result == {"looked_up": 3, "failed": 0, "failed_status": None, "drops": 1, "sent": True}
        assert lookup.await_count == 3
        assert all(call.kwargs["use_cache"] is False for call in lookup.await_args_list)
        from app.database import get_db
        with get_db() as d:
            assert _count(d) == 5
            rows = d.execute("SELECT item_id, price FROM price_history WHERE id > 2 ORDER BY id").fetchall()
        assert [(r["item_id"], r["price"]) for r in rows] == [(c, 10.0), (a, 19.0), (b, 5.0)]
        # c is a first observation, a fell 5%, b fell 75%: only b qualifies.
        assert send.await_count == 1
        url, title, body, fmt = send.await_args.args
        assert (url, fmt) == ("http://n.test/t", "ntfy")
        assert title == "Shelf: 1 wishlist price drop"
        assert "Book 2" in body and "$20.00" in body and "$5.00" in body
        assert "Book 1" not in body

    async def test_no_notify_url_still_writes_rows(self, db, cache_stubs):
        a = _wish(db, 1)
        _obs(db, a, 20.0, "2026-09-01 00:00:00")
        db.commit()
        send = AsyncMock(return_value=True)
        with patch("app.services.isbndb.lookup_price", AsyncMock(return_value={"msrp": "1.00"})), \
             patch("app.services.notify.send_notification", send):
            result = await self._run(notify_url="")
        assert result == {"looked_up": 1, "failed": 0, "failed_status": None, "drops": 1, "sent": False}
        send.assert_not_called()
        from app.database import get_db
        with get_db() as d:
            assert _count(d) == 2

    async def test_failed_send_is_not_retried(self, db, cache_stubs):
        a = _wish(db, 1)
        _obs(db, a, 20.0, "2026-09-01 00:00:00")
        db.commit()
        send = AsyncMock(return_value=False)
        with patch("app.services.isbndb.lookup_price", AsyncMock(return_value={"msrp": "1.00"})), \
             patch("app.services.notify.send_notification", send):
            result = await self._run()
        assert result["sent"] is False and result["drops"] == 1
        assert send.await_count == 1

    async def test_miss_records_null_and_never_drops(self, db, cache_stubs):
        a = _wish(db, 1)
        _obs(db, a, 20.0, "2026-09-01 00:00:00")
        db.commit()
        send = AsyncMock(return_value=True)
        with patch("app.services.isbndb.lookup_price", AsyncMock(return_value=None)), \
             patch("app.services.notify.send_notification", send):
            result = await self._run()
        assert result == {"looked_up": 1, "failed": 0, "failed_status": None, "drops": 0, "sent": False}
        send.assert_not_called()
        from app.database import get_db
        with get_db() as d:
            last = d.execute("SELECT price FROM price_history ORDER BY id DESC LIMIT 1").fetchone()
        assert last["price"] is None

    async def test_save_cache_once(self, db, cache_stubs):
        for n in range(1, 4):
            _wish(db, n)
        db.commit()
        _, save = cache_stubs
        with patch("app.services.isbndb.lookup_price", AsyncMock(return_value={"msrp": "1.00"})):
            await self._run(notify_url="")
        assert save.call_count == 1

    async def test_cap_limits_lookups(self, db, cache_stubs):
        for n in range(1, 6):
            _wish(db, n)
        db.commit()
        lookup = AsyncMock(return_value={"msrp": "1.00"})
        with patch("app.services.isbndb.lookup_price", lookup):
            result = await self._run(cap=2, notify_url="")
        assert result["looked_up"] == 2 and lookup.await_count == 2

    async def test_malformed_isbn_writes_null_without_lookup(self, db, cache_stubs):
        a = _insert_item(db, title="Junk", isbn="not-an-isbn", wishlisted=True)
        db.commit()
        lookup = AsyncMock(return_value={"msrp": "1.00"})
        with patch("app.services.isbndb.lookup_price", lookup):
            result = await self._run(notify_url="")
        lookup.assert_not_called()
        assert result["looked_up"] == 0
        from app.database import get_db
        with get_db() as d:
            rows = d.execute("SELECT item_id, price FROM price_history").fetchall()
        assert [(r["item_id"], r["price"]) for r in rows] == [(a, None)]


class TestRunPassFailures:
    """test-drive Obs 2: a lookup ISBNdb did not answer is counted, recorded
    nowhere, and a refused key ends the pass."""

    async def _run(self, **kw):
        args = {"threshold": 15, "cap": 100, "notify_url": "", "notify_format": "ntfy"}
        args.update(kw)
        return await price_alerts.run_pass("key", **args)

    async def test_failed_lookup_writes_no_row_and_keeps_its_place(self, db, cache_stubs):
        a, b = _wish(db, 1), _wish(db, 2)
        db.commit()
        lookup = AsyncMock(side_effect=[isbndb.LookupFailed(429), {"msrp": "9.00"}])
        with patch("app.services.isbndb.lookup_price", lookup):
            result = await self._run()
        assert result["looked_up"] == 2
        assert (result["failed"], result["failed_status"]) == (1, 429)
        assert all(call.kwargs["raise_on_failure"] is True for call in lookup.await_args_list)
        from app.database import get_db
        with get_db() as d:
            rows = d.execute("SELECT item_id, price FROM price_history").fetchall()
            assert [(r["item_id"], r["price"]) for r in rows] == [(b, 9.0)]
            # a was never observed, so it leads the next pass
            assert price_alerts.select_candidates(d, 1)[0]["id"] == a

    @pytest.mark.parametrize("status", [401, 403])
    async def test_refused_key_stops_the_pass(self, db, cache_stubs, status):
        for n in range(1, 4):
            _wish(db, n)
        db.commit()
        lookup = AsyncMock(side_effect=isbndb.LookupFailed(status))
        with patch("app.services.isbndb.lookup_price", lookup):
            result = await self._run()
        assert lookup.await_count == 1
        assert (result["looked_up"], result["failed"], result["failed_status"]) == (1, 1, status)
        from app.database import get_db
        with get_db() as d:
            assert _count(d) == 0

    async def test_other_failures_do_not_stop_the_pass(self, db, cache_stubs):
        for n in range(1, 4):
            _wish(db, n)
        db.commit()
        lookup = AsyncMock(side_effect=isbndb.LookupFailed(None))
        with patch("app.services.isbndb.lookup_price", lookup):
            result = await self._run()
        assert lookup.await_count == 3
        assert (result["failed"], result["failed_status"]) == (3, None)


class TestDescribeLastRun:
    STAMP = "1790000000"  # 2026-09-21 14:13:20 UTC

    def test_never(self):
        assert price_alerts.describe_last_run(None, None) == "Never"

    def test_time_alone_without_a_summary(self):
        assert price_alerts.describe_last_run(self.STAMP, None) == "2026-09-21 14:13 UTC"
        assert price_alerts.describe_last_run(self.STAMP, "not json") == "2026-09-21 14:13 UTC"

    def test_clean_pass(self):
        s = json.dumps({"looked_up": 40, "failed": 0, "status": None})
        assert price_alerts.describe_last_run(self.STAMP, s) == "2026-09-21 14:13 UTC — 40 checked"

    @pytest.mark.parametrize("status, reason", [
        (401, "ISBNdb refused the API key"),
        (403, "ISBNdb refused the API key"),
        (429, "ISBNdb rate limit"),
        (None, "no response from ISBNdb"),
        (502, "ISBNdb answered HTTP 502"),
    ])
    def test_failures_name_the_reason(self, status, reason):
        s = json.dumps({"looked_up": 5, "failed": 2, "status": status})
        assert price_alerts.describe_last_run(self.STAMP, s) == (
            f"2026-09-21 14:13 UTC — 5 checked, 2 failed ({reason})")
