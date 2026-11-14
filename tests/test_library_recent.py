"""Bounded recent-item reads on the real, migrated catalogue fixture.

The bulk seed below measures fetched/materialized rows, not authorization,
source availability, general search latency or full-catalogue F1 acceptance.
"""

from dataclasses import FrozenInstanceError, asdict
import sqlite3

import pytest

from app import config
import app.services.library_recent as library_recent
from app.services import home_dashboard, item_copies, library_catalog, lists
from tests.conftest import _insert_item


def _item(db, title="A title", **fields):
    return _insert_item(db, title=title, isbn=None, **fields)


def _ids(result):
    return [item.id for item in result.items]


class _CursorProbe:
    def __init__(self, cursor, record):
        self.cursor = cursor
        self.record = record

    def fetchone(self):
        row = self.cursor.fetchone()
        self.record["fetched"] += row is not None
        return row

    def fetchall(self):
        rows = self.cursor.fetchall()
        self.record["fetched"] += len(rows)
        return rows

    def fetchmany(self, size=1):
        rows = self.cursor.fetchmany(size)
        self.record["fetched"] += len(rows)
        return rows

    def __iter__(self):
        for row in self.cursor:
            self.record["fetched"] += 1
            yield row

    def close(self):
        self.record["closed"] = True
        self.cursor.close()


class _ReadProbe:
    """Record actual SQL, binds and cursor consumption on the fixture's db."""

    def __init__(self, db):
        self.db = db
        self.queries = []
        self.materialized = []
        self.original_factory = db.row_factory

        def row_factory(cursor, values):
            self.materialized.append(tuple(column[0] for column in cursor.description))
            return self.original_factory(cursor, values)

        db.row_factory = row_factory

    def execute(self, sql, parameters=()):
        assert sql.lstrip().upper().startswith("SELECT "), sql
        record = {"sql": sql, "parameters": tuple(parameters), "fetched": 0, "closed": False}
        self.queries.append(record)
        return _CursorProbe(self.db.execute(sql, parameters), record)

    def restore(self):
        self.db.row_factory = self.original_factory


@pytest.fixture
def read_probe(db):
    probe = _ReadProbe(db)
    try:
        yield probe
    finally:
        probe.restore()


class _UnusedDB:
    def execute(self, *args, **kwargs):
        pytest.fail("invalid input must be refused before querying")


@pytest.mark.parametrize("count,has_more", [(0, False), (1, False), (8, False), (9, True)])
def test_preview_empty_one_limit_and_limit_plus_one(db, count, has_more):
    ids = [_item(db, str(n), created_at="2026-01-01 12:00:00") for n in range(count)]
    result = library_recent.recent_preview(db)
    assert isinstance(result, library_recent.ShelfPreview)
    assert _ids(result) == list(reversed(ids))[:8]
    assert result.has_more is has_more


@pytest.mark.parametrize("count", [0, 1, 3])
def test_zero_limit_still_reports_whether_a_matching_item_exists(db, count):
    for n in range(count):
        _item(db, str(n))
    result = library_recent.recent_preview(db, limit=0)
    assert result.items == ()
    assert result.has_more is bool(count)


def test_card_dto_uses_current_fields_and_real_wishlist_membership(db):
    owned = _item(db, "Owned <title>", authors="Creator & Co", cover_path="covers/owned.jpg")
    wish = _item(db, "Wish", media_type="ebook", owned=0, wishlisted=True)
    neither = _item(db, "Neither", media_type="dvd", owned=0)
    result = library_recent.recent_preview(db)
    cards = {item.id: item for item in result.items}
    assert asdict(cards[owned]) == {
        "id": owned, "title": "Owned <title>", "creator_text": "Creator & Co",
        "media_type": "book", "cover_path": "covers/owned.jpg", "owned": True, "wishlisted": False,
    }
    assert (cards[wish].owned, cards[wish].wishlisted) == (False, True)
    assert (cards[neither].owned, cards[neither].wishlisted) == (False, False)
    assert cards[wish].creator_text is None
    assert cards[wish].cover_path is None
    with pytest.raises(FrozenInstanceError):
        cards[owned].title = "Changed"
    with pytest.raises(FrozenInstanceError):
        result.has_more = True


@pytest.mark.parametrize("media_type", tuple(config.MEDIA_TYPES))
def test_each_canonical_type_is_selected_without_other_formats(db, media_type):
    wanted = _item(db, "Wanted", media_type=media_type)
    _item(db, "Legacy", media_type="unknown_legacy_format")
    other_type = next(key for key in config.MEDIA_TYPES if key != media_type)
    _item(db, "Other", media_type=other_type)
    preview = library_recent.recent_preview(db, (media_type,))
    page = library_recent.recent_items_page(db, (media_type,))
    assert _ids(preview) == _ids(page) == [wanted]
    assert page.matching_count == 1
    assert not preview.has_more and not page.has_more


def test_explicit_mixed_includes_unknown_legacy_types_and_all_ownership_states(db):
    ids = [
        _item(db, "Book"),
        _item(db, "Unowned", media_type="dvd", owned=0),
        _item(db, "Wanted", media_type="cd", owned=0, wishlisted=True),
        _item(db, "Legacy", media_type="unknown_legacy_format"),
    ]
    assert set(_ids(library_recent.recent_preview(db, ()))) == set(ids)
    page = library_recent.recent_items_page(db, ())
    assert set(_ids(page)) == set(ids)
    assert page.matching_count == 4


def test_two_type_union_is_deduplicated_in_config_order_with_bound_predicates(db, read_probe):
    book = _item(db, "Book", media_type="book")
    digital = _item(db, "Digital", media_type="ebook")
    _item(db, "Film", media_type="dvd")
    result = library_recent.recent_preview(read_probe, ("ebook", "book", "ebook"), limit=1)
    assert _ids(result) == [digital]
    assert result.has_more is True
    selection, hydration = read_probe.queries
    assert "i.media_type IN (?, ?)" in selection["sql"]
    assert "book" not in selection["sql"] and "ebook" not in selection["sql"]
    assert selection["parameters"] == ("book", "ebook", 2)
    assert hydration["parameters"] == (digital,)
    page = library_recent.recent_items_page(db, ("ebook", "book", "book"))
    assert set(_ids(page)) == {book, digital}
    assert page.matching_count == 2


@pytest.mark.parametrize("media_types", [
    None, False, True, "book", "", [], ["book"], {}, {"book": True}, {"book"},
    (None,), (False,), (1,), ({"type": "book"},), (["book"],),
    ("",), ("unknown",), ("book", "unknown"), ("kids_book",), ("music",),
    ("Book",), (" book",), ("book\x00",), ("x" * 65,),
    ("book') OR 1=1 --",), ("book", "dvd'); DROP TABLE items; --"),
])
@pytest.mark.parametrize("function", ["recent_preview", "recent_items_page"])
def test_noncanonical_or_nontuple_type_input_never_broadens(media_types, function):
    with pytest.raises(ValueError, match="^invalid_media_types$"):
        getattr(library_recent, function)(_UnusedDB(), media_types)


def test_type_input_count_is_bounded_before_deduplication(db):
    item = _item(db)
    maximum = library_recent.MAX_MEDIA_TYPE_INPUTS
    assert _ids(library_recent.recent_preview(db, ("book",) * maximum)) == [item]
    for function in (library_recent.recent_preview, library_recent.recent_items_page):
        with pytest.raises(ValueError, match="^invalid_media_types$"):
            function(_UnusedDB(), ("book",) * (maximum + 1))


@pytest.mark.parametrize("value", [None, True, False, "8", 1.0, {}, [], -1, 51, 10**100])
def test_preview_limit_is_a_strict_bounded_integer(value):
    with pytest.raises(ValueError, match="^invalid_limit$"):
        library_recent.recent_preview(_UnusedDB(), limit=value)


@pytest.mark.parametrize("field,value", [
    ("page", None), ("page", True), ("page", "2"), ("page", 1.0), ("page", {}),
    ("page", 0), ("page", -1), ("page", 1_000_001), ("page", 10**100),
    ("page_size", None), ("page_size", False), ("page_size", "24"), ("page_size", 1.0),
    ("page_size", {}), ("page_size", 0), ("page_size", -1), ("page_size", 101),
])
def test_page_counts_are_strict_bounded_integers(field, value):
    with pytest.raises(ValueError, match=f"^invalid_{field}$"):
        library_recent.recent_items_page(_UnusedDB(), **{field: value})


def test_page_dto_has_accurate_count_bounded_rows_and_current_page(db):
    ids = [_item(db, str(n), created_at="2026-01-01 12:00:00") for n in range(7)]
    expected = list(reversed(ids))
    seen = []
    for number in range(1, 5):
        result = library_recent.recent_items_page(db, page=number, page_size=2)
        assert isinstance(result, library_recent.PageDTO)
        assert result.matching_count == 7
        assert (result.page, result.pages, result.page_size) == (number, 4, 2)
        assert _ids(result) == expected[(number - 1) * 2:number * 2]
        assert result.has_more is (number < 4)
        seen.extend(_ids(result))
    assert seen == expected
    last = library_recent.recent_items_page(db, page=1_000_000, page_size=2)
    assert (last.page, last.pages, last.matching_count, last.has_more) == (4, 4, 7, False)
    assert _ids(last) == expected[-1:]
    with pytest.raises(FrozenInstanceError):
        last.page = 1


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_page_zero_one_exact_size_and_size_plus_one(db, count):
    for n in range(count):
        _item(db, str(n))
    result = library_recent.recent_items_page(db, page_size=2)
    assert result.matching_count == count
    assert len(result.items) == min(count, 2)
    assert result.page == 1
    assert result.pages == (2 if count == 3 else 1)
    assert result.has_more is (count > 2)


def test_empty_filtered_page_clamps_to_one_without_broadening(db):
    _item(db, media_type="dvd")
    result = library_recent.recent_items_page(db, ("book",), page=42)
    assert result.items == ()
    assert (result.page, result.pages, result.page_size, result.matching_count) == (1, 1, 24, 0)
    assert result.has_more is False


def test_recent_order_matches_existing_home_for_ties_empty_and_legacy_timestamp_text(db):
    for timestamp in ("", "2025-12-31", "2026-01-01 12:00:00", "2026-01-01 12:00:00", "legacy-text"):
        _item(db, timestamp or "Empty timestamp", created_at=timestamp)
    baseline = [row["id"] for row in home_dashboard.dashboard_summary(db)["recent_items"]]
    assert _ids(library_recent.recent_preview(db)) == baseline
    assert _ids(library_recent.recent_items_page(db)) == baseline


def test_current_migrated_schema_rejects_null_created_at(db):
    """No nullable-timestamp production fixture exists on this baseline."""
    column = next(row for row in db.execute("PRAGMA table_info(items)") if row["name"] == "created_at")
    assert column["notnull"] == 1
    item = _item(db)
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed: items.created_at"):
        db.execute("UPDATE items SET created_at = NULL WHERE id = ?", (item,))
    assert _ids(library_recent.recent_preview(db)) == [item]


def test_native_descending_null_order_is_the_existing_home_sql_baseline(db):
    """Engine semantics only; not a claim of nullable rows in the real schema."""
    rows = db.execute(
        "SELECT 1 AS id, NULL AS created_at UNION ALL SELECT 2, '' "
        "UNION ALL SELECT 3, '2026-01-01' UNION ALL SELECT 4, NULL "
        "ORDER BY created_at DESC, id DESC"
    ).fetchall()
    assert [row["id"] for row in rows] == [3, 2, 4, 1]


def test_trash_is_excluded_and_copies_membership_and_history_are_read_only(db):
    live = _item(db, "Live", owned=0, wishlisted=True)
    dead = _item(db, "Trash", owned=0, wishlisted=True, created_at="9999-01-01")
    item_copies.insert_copy(db, {"item_id": live, "copy_number": 1})
    removed = item_copies.insert_copy(db, {"item_id": live, "copy_number": 2})
    item_copies.insert_copy(db, {"item_id": dead, "copy_number": 1})
    db.execute("UPDATE item_copies SET deleted_at = '2026-01-01' WHERE id = ?", (removed,))
    db.execute("UPDATE items SET deleted_at = '2026-01-01' WHERE id = ?", (dead,))
    for item in (live, dead):
        db.execute("INSERT INTO reading_log (item_id, status, notes) VALUES (?, 'read', 'Keep history')", (item,))
    tables = ("items", "item_copies", "list_items", "reading_log")
    before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
              for table in tables}
    changes = db.total_changes
    assert _ids(library_recent.recent_preview(db)) == [live]
    page = library_recent.recent_items_page(db)
    assert _ids(page) == [live] and page.matching_count == 1
    after = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
             for table in tables}
    assert after == before
    assert db.total_changes == changes


def test_reads_work_in_query_only_mode_without_owning_the_connection_or_transaction(db):
    item = _item(db, owned=0, wishlisted=True)
    db.commit()
    db.execute("PRAGMA query_only=ON")
    try:
        assert not db.in_transaction
        assert _ids(library_recent.recent_preview(db)) == [item]
        assert _ids(library_recent.recent_items_page(db)) == [item]
        assert not db.in_transaction
        db.execute("BEGIN")
        assert library_recent.recent_items_page(db).items[0].wishlisted is True
        assert db.in_transaction
        db.rollback()
        assert db.execute("SELECT 1").fetchone()[0] == 1
    finally:
        db.execute("PRAGMA query_only=OFF")


def test_reads_leave_the_callers_pending_changes_and_rollback_alone(db):
    item = _item(db)
    assert db.in_transaction
    assert _ids(library_recent.recent_preview(db)) == [item]
    assert library_recent.recent_items_page(db).matching_count == 1
    assert db.in_transaction
    db.rollback()
    assert library_recent.recent_items_page(db).matching_count == 0


def test_cursors_close_and_database_errors_propagate_without_transaction_management():
    class FetchFailed(Exception):
        pass

    class FailingCursor:
        closed = False

        def fetchall(self):
            raise FetchFailed

        def fetchone(self):
            raise FetchFailed

        def close(self):
            self.closed = True

    class FailingDB:
        def execute(self, sql, parameters=()):
            self.cursor = FailingCursor()
            return self.cursor

    for function in (library_recent.recent_preview, library_recent.recent_items_page):
        failing = FailingDB()
        with pytest.raises(FetchFailed):
            function(failing)
        assert failing.cursor.closed


def test_missing_live_view_fails_loudly_without_physical_table_fallback():
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(sqlite3.OperationalError, match="no such table: items_live"):
            library_recent.recent_preview(connection)
    connection.close()


def test_empty_reads_do_not_hydrate_metadata(db, read_probe):
    assert library_recent.recent_preview(read_probe).items == ()
    assert len(read_probe.queries) == 1
    assert read_probe.queries[0]["fetched"] == 0
    read_probe.queries.clear()
    assert library_recent.recent_items_page(read_probe).items == ()
    assert len(read_probe.queries) <= 2
    assert sum(query["fetched"] for query in read_probe.queries) == 1
    assert all(query["closed"] for query in read_probe.queries)


def test_31000_item_seed_bounds_actual_fetch_and_metadata_before_any_hydration(db, monkeypatch):
    """Test-only bulk data in the real schema, not an application insert path."""
    db.executemany(
        "INSERT INTO items (title, media_type, created_at, owned) VALUES (?, ?, ?, ?)",
        ((f"Item {n}", "book" if n % 2 else "ebook", "2026-01-01 12:00:00", 0)
         for n in range(31_000)),
    )
    newest = db.execute("SELECT id FROM items_live ORDER BY id DESC LIMIT 1").fetchone()["id"]
    lists.add(db, lists.WISHLIST, newest)

    def forbidden(*args, **kwargs):
        pytest.fail("recent items must not hydrate the full catalogue, holdings or graph")

    monkeypatch.setattr(library_catalog, "search_catalog", forbidden)
    monkeypatch.setattr(library_catalog.availability, "availability_for_items", forbidden)
    monkeypatch.setattr(library_catalog.format_groups, "format_components", forbidden)

    probe = _ReadProbe(db)
    try:
        preview = library_recent.recent_preview(probe, limit=8)
        assert _ids(preview) == list(range(newest, newest - 8, -1))
        assert preview.items[0].wishlisted is True
        assert preview.items[1].wishlisted is False  # unowned is not a proxy
        assert preview.has_more is True
        selection, hydration = probe.queries
        assert "ORDER BY i.created_at DESC, i.id DESC LIMIT ?" in selection["sql"]
        assert selection["parameters"] == (9,)
        assert selection["fetched"] == 9
        assert "list_items" not in selection["sql"]
        assert "i.id IN (" in hydration["sql"]
        assert hydration["parameters"] == tuple(_ids(preview))
        assert hydration["fetched"] == 8
        assert len(probe.materialized) == 17
        assert sum("creator_text" in columns for columns in probe.materialized) == 8
        assert all(query["closed"] for query in probe.queries)

        probe.queries.clear()
        probe.materialized.clear()
        page = library_recent.recent_items_page(probe, ("ebook", "book", "ebook"), page=2, page_size=24)
        assert _ids(page) == list(range(newest - 24, newest - 48, -1))
        assert (page.matching_count, page.page, page.pages, page.has_more) == (31_000, 2, 1292, True)
        count, selection, hydration = probe.queries
        assert "COUNT(*)" in count["sql"] and count["fetched"] == 1
        assert count["parameters"] == ("book", "ebook")
        assert "LIMIT ? OFFSET ?" in selection["sql"]
        assert selection["parameters"] == ("book", "ebook", 24, 24)
        assert selection["fetched"] == 24
        assert "list_items" not in selection["sql"]
        assert hydration["parameters"] == tuple(_ids(page))
        assert hydration["fetched"] == 24
        assert len(probe.materialized) == 49
        assert sum("creator_text" in columns for columns in probe.materialized) == 24
        assert all(query["closed"] for query in probe.queries)

        probe.queries.clear()
        probe.materialized.clear()
        assert len(library_recent.recent_preview(probe, limit=50).items) == 50
        assert [query["fetched"] for query in probe.queries] == [51, 50]
        probe.queries.clear()
        probe.materialized.clear()
        assert len(library_recent.recent_items_page(probe, page_size=100).items) == 100
        assert [query["fetched"] for query in probe.queries] == [1, 100, 100]
    finally:
        probe.restore()
