"""Tests for app/services/wrapups.py — period parsing and period summaries."""
from datetime import date

from app.services.wrapups import Period, latest_period, parse_period, summarize
from tests.conftest import _insert_item


class TestParsePeriod:
    def test_missing_falls_back_to_current_month(self):
        today = date(2026, 9, 15)
        p = parse_period(None, today)
        assert p == Period(kind="month", key="2026-09", label="September 2026")

    def test_empty_string_falls_back_to_current_month(self):
        today = date(2026, 9, 15)
        p = parse_period("", today)
        assert p.kind == "month" and p.key == "2026-09"

    def test_malformed_falls_back_to_current_month(self):
        today = date(2026, 9, 15)
        for raw in ("banana", "2026-13", "2026-00", "26-09", "2026/09", "2026-9"):
            p = parse_period(raw, today)
            assert p.kind == "month" and p.key == "2026-09", raw

    def test_valid_month(self):
        today = date(2026, 9, 15)
        p = parse_period("2026-03", today)
        assert p == Period(kind="month", key="2026-03", label="March 2026")

    def test_valid_year(self):
        today = date(2026, 9, 15)
        p = parse_period("2025", today)
        assert p == Period(kind="year", key="2025", label="2025")

    def test_current_month_explicit(self):
        today = date(2026, 9, 15)
        p = parse_period("2026-09", today)
        assert p == Period(kind="month", key="2026-09", label="September 2026")

    def test_current_year_explicit(self):
        today = date(2026, 9, 15)
        p = parse_period("2026", today)
        assert p == Period(kind="year", key="2026", label="2026")

    def test_future_month_falls_back(self):
        today = date(2026, 9, 15)
        p = parse_period("2026-10", today)
        assert p.kind == "month" and p.key == "2026-09"

    def test_future_year_falls_back(self):
        today = date(2026, 9, 15)
        p = parse_period("2027", today)
        assert p.kind == "month" and p.key == "2026-09"

    def test_past_year_is_valid(self):
        today = date(2026, 1, 1)
        p = parse_period("2025-12", today)
        assert p == Period(kind="month", key="2025-12", label="December 2025")


class TestSummarizeMonthBoundaries:
    def test_membership_excludes_adjacent_days(self, db):
        _insert_item(db, title="Too Early", isbn="9780000000001",
                      reading_status="read", date_finished="2026-08-31")
        _insert_item(db, title="In Bounds Start", isbn="9780000000002",
                      reading_status="read", date_finished="2026-09-01")
        _insert_item(db, title="In Bounds End", isbn="9780000000003",
                      reading_status="read", date_finished="2026-09-30")
        _insert_item(db, title="Too Late", isbn="9780000000004",
                      reading_status="read", date_finished="2026-10-01")

        period = Period(kind="month", key="2026-09", label="September 2026")
        summary = summarize(db, period)

        assert summary.finished_total == 2
        titles = {t["title"] for t in summary.tiles}
        assert titles == {"In Bounds Start", "In Bounds End"}

    def test_year_membership_excludes_adjacent_years(self, db):
        _insert_item(db, title="Last Year", isbn="9780000000005",
                      reading_status="read", date_finished="2025-12-31")
        _insert_item(db, title="This Year Start", isbn="9780000000006",
                      reading_status="read", date_finished="2026-01-01")
        _insert_item(db, title="This Year End", isbn="9780000000007",
                      reading_status="read", date_finished="2026-12-31")
        _insert_item(db, title="Next Year", isbn="9780000000008",
                      reading_status="read", date_finished="2027-01-01")

        period = Period(kind="year", key="2026", label="2026")
        summary = summarize(db, period)

        assert summary.finished_total == 2
        titles = {t["title"] for t in summary.tiles}
        assert titles == {"This Year Start", "This Year End"}


class TestSummarizeTrash:
    def test_trashed_item_never_appears(self, db):
        _insert_item(db, title="Trashed", isbn="9780000000009",
                      reading_status="read", date_finished="2026-09-05",
                      deleted_at="2026-09-06 00:00:00")
        _insert_item(db, title="Live", isbn="9780000000010",
                      reading_status="read", date_finished="2026-09-05")

        period = Period(kind="month", key="2026-09", label="September 2026")
        summary = summarize(db, period)

        assert summary.finished_total == 1
        titles = [t["title"] for t in summary.tiles]
        assert titles == ["Live"]

    def test_trashed_item_excluded_from_additions_fallback(self, db):
        _insert_item(db, title="Trashed Add", isbn="9780000000011",
                      created_at="2026-09-05 00:00:00",
                      deleted_at="2026-09-06 00:00:00")
        _insert_item(db, title="Live Add", isbn="9780000000012",
                      created_at="2026-09-05 00:00:00")

        period = Period(kind="month", key="2026-09", label="September 2026")
        summary = summarize(db, period)

        assert summary.added_total == 1
        assert summary.showing == "added"
        titles = [t["title"] for t in summary.tiles]
        assert titles == ["Live Add"]


class TestSummarizeFallbacks:
    def test_nothing_finished_falls_back_to_additions(self, db):
        _insert_item(db, title="Added One", isbn="9780000000013",
                      created_at="2026-09-01 00:00:00")
        _insert_item(db, title="Added Two", isbn="9780000000014",
                      created_at="2026-09-02 00:00:00")

        period = Period(kind="month", key="2026-09", label="September 2026")
        summary = summarize(db, period)

        assert summary.finished_total == 0
        assert summary.added_total == 2
        assert summary.showing == "added"
        assert len(summary.tiles) == 2
        assert summary.finished == []

    def test_neither_finished_nor_added_is_empty(self, db):
        _insert_item(db, title="Elsewhere", isbn="9780000000015",
                      created_at="2020-01-01 00:00:00")

        period = Period(kind="month", key="2026-09", label="September 2026")
        summary = summarize(db, period)

        assert summary.finished_total == 0
        assert summary.added_total == 0
        assert summary.showing is None
        assert summary.tiles == []
        assert summary.top_author is None


class TestSummarizeCap:
    def test_more_than_cap_caps_tiles_but_not_totals(self, db):
        for i in range(5):
            _insert_item(db, title=f"Book {i}", isbn=f"978000000030{i}",
                         reading_status="read", date_finished=f"2026-09-{i + 1:02d}")

        period = Period(kind="month", key="2026-09", label="September 2026")
        summary = summarize(db, period, cap=3)

        assert summary.finished_total == 5
        assert len(summary.tiles) == 3

    def test_tile_order_is_finished_desc_then_id_desc(self, db):
        id1 = _insert_item(db, title="First", isbn="9780000000020",
                            reading_status="read", date_finished="2026-09-01")
        id2 = _insert_item(db, title="Second", isbn="9780000000021",
                            reading_status="read", date_finished="2026-09-15")
        id3 = _insert_item(db, title="Third Same Day", isbn="9780000000022",
                            reading_status="read", date_finished="2026-09-15")

        period = Period(kind="month", key="2026-09", label="September 2026")
        summary = summarize(db, period)

        ids = [t["id"] for t in summary.tiles]
        assert ids == [id3, id2, id1]


class TestSummarizeVerbs:
    def test_watched_and_played_count_under_their_own_verb(self, db):
        _insert_item(db, title="A Book", isbn="9780000000023", media_type="book",
                      reading_status="read", date_finished="2026-09-01")
        _insert_item(db, title="A DVD", isbn="9780000000024", media_type="dvd",
                      reading_status="read", date_finished="2026-09-02")
        _insert_item(db, title="A Game", isbn="9780000000025", media_type="video_game",
                      reading_status="read", date_finished="2026-09-03")

        period = Period(kind="month", key="2026-09", label="September 2026")
        summary = summarize(db, period)

        assert summary.finished_total == 3
        by_word = dict((word, count) for count, word in summary.finished)
        assert by_word == {"read": 1, "watched": 1, "played": 1}
        # read/watch/play order
        assert [word for _, word in summary.finished] == ["read", "watched", "played"]

    def test_zero_verbs_are_dropped(self, db):
        _insert_item(db, title="Only A Book", isbn="9780000000026", media_type="book",
                      reading_status="read", date_finished="2026-09-01")

        period = Period(kind="month", key="2026-09", label="September 2026")
        summary = summarize(db, period)

        assert summary.finished == [(1, "read")]


class TestSummarizeTopAuthor:
    def test_top_author_over_finished_items(self, db):
        _insert_item(db, title="Book A", isbn="9780000000027",
                      authors="Ann Leckie", reading_status="read", date_finished="2026-09-01")
        _insert_item(db, title="Book B", isbn="9780000000028",
                      authors="Ann Leckie, Someone Else", reading_status="read",
                      date_finished="2026-09-02")
        _insert_item(db, title="Book C", isbn="9780000000029",
                      authors="N. K. Jemisin", reading_status="read", date_finished="2026-09-03")

        period = Period(kind="month", key="2026-09", label="September 2026")
        summary = summarize(db, period)

        assert summary.top_author == ("Ann Leckie", 2)

    def test_top_author_falls_back_to_additions_when_nothing_finished(self, db):
        _insert_item(db, title="Book A", isbn="9780000000031",
                      authors="Becky Chambers", created_at="2026-09-01 00:00:00")
        _insert_item(db, title="Book B", isbn="9780000000032",
                      authors="Becky Chambers", created_at="2026-09-02 00:00:00")

        period = Period(kind="month", key="2026-09", label="September 2026")
        summary = summarize(db, period)

        assert summary.top_author == ("Becky Chambers", 2)

    def test_an_author_nobody_repeats_is_not_top(self, db):
        """qa-wrapups Observation 2: with every count at 1, the alphabetical
        tie-break would crown an arbitrary name."""
        _insert_item(db, title="Book A", isbn="9780000000033",
                      authors="Zadie Smith", reading_status="read", date_finished="2026-09-01")
        _insert_item(db, title="Book B", isbn="9780000000034",
                      authors="Ann Leckie", reading_status="read", date_finished="2026-09-02")

        period = Period(kind="month", key="2026-09", label="September 2026")
        summary = summarize(db, period)

        assert summary.top_author is None


class TestLatestPeriod:
    TODAY = date(2026, 9, 15)

    def test_empty_library_is_the_current_month(self, db):
        assert latest_period(db, self.TODAY).key == "2026-09"

    def test_newest_month_with_a_finish_or_an_addition(self, db):
        _insert_item(db, title="Added", isbn="9780000000035", created_at="2026-05-02 00:00:00")
        _insert_item(db, title="Finished", isbn="9780000000036", created_at="2026-03-01 00:00:00",
                      reading_status="read", date_finished="2026-08-20")

        assert latest_period(db, self.TODAY) == Period(kind="month", key="2026-08", label="August 2026")

    def test_future_dates_and_undated_reads_are_ignored(self, db):
        _insert_item(db, title="Past", isbn="9780000000037", created_at="2026-04-02 00:00:00")
        _insert_item(db, title="Future", isbn="9780000000038", created_at="2026-04-03 00:00:00",
                      reading_status="read", date_finished="2026-11-01")
        _insert_item(db, title="Want", isbn="9780000000039", created_at="2026-04-04 00:00:00",
                      reading_status="want_to_read", date_finished="2026-07-01")

        assert latest_period(db, self.TODAY).key == "2026-04"
