from app.services import komga_records, komga_series_browse


def _candidate(komga_id: str, series_id: str, position: float, *, name="Shared Name", kind="manga"):
    return {
        "komga_id": komga_id,
        "komga_library_id": f"library-{kind}",
        "komga_series_id": series_id,
        "library_kind": kind,
        "title": f"{name} {position:g}",
        "authors": "Series Author",
        "isbn": None,
        "series_name": name,
        "series_position": position,
        "publish_year": 2020 + int(position),
        "description": None,
        "page_count": 180,
    }


def _add(db, komga_id: str, series_id: str, position: float, **kwargs):
    return komga_records.persist_candidate(
        db, _candidate(komga_id, series_id, position, **kwargs)
    )["item_id"]


def test_browse_groups_by_stable_komga_series_id_not_display_name(db):
    _add(db, "book-a1", "series-a", 1)
    _add(db, "book-a2", "series-a", 2)
    _add(db, "book-b1", "series-b", 1)

    items, raw_total, display_total = komga_series_browse.fetch_page(
        db, "", [], "i.title COLLATE NOCASE ASC",
        limit=20, offset=0, values={},
    )

    assert raw_total == 3
    assert display_total == 2
    groups = {item["browse_series_id"]: item for item in items}
    assert set(groups) == {"series-a", "series-b"}
    assert groups["series-a"]["browse_series_count"] == 2
    assert groups["series-b"]["browse_series_count"] == 1
    assert groups["series-a"]["browse_series_name"] == "Shared Name"


def test_explicit_series_filter_keeps_individual_item_browse(db):
    _add(db, "book-a1", "series-a", 1)
    _add(db, "book-a2", "series-a", 2)

    items, raw_total, display_total = komga_series_browse.fetch_page(
        db,
        "WHERE i.series_name = ? COLLATE NOCASE",
        ["Shared Name"],
        "i.series_position ASC",
        limit=20,
        offset=0,
        values={"series": "Shared Name"},
    )

    assert raw_total == display_total == 2
    assert len(items) == 2
    assert all(item["browse_series_group"] is False for item in items)


def test_conflicting_komga_series_links_are_not_guessed_into_a_group(db):
    first = komga_records.persist_candidate(
        db,
        {
            **_candidate("book-one", "series-one", 1),
            "isbn": "9781974700523",
        },
    )
    second = komga_records.persist_candidate(
        db,
        {
            **_candidate("book-two", "series-two", 1),
            "isbn": "9781974700523",
        },
    )
    assert first["item_id"] == second["item_id"]

    items, raw_total, display_total = komga_series_browse.fetch_page(
        db, "", [], "i.title COLLATE NOCASE ASC",
        limit=20, offset=0, values={},
    )

    assert raw_total == display_total == 1
    assert len(items) == 1
    assert items[0]["browse_series_group"] is False
    assert komga_series_browse.series_detail(db, "series-one") is None


def test_focused_series_orders_positions_and_reports_local_gaps(db):
    _add(db, "book-4", "series-gap", 4, name="Gap Series", kind="comic")
    _add(db, "book-1", "series-gap", 1, name="Gap Series", kind="comic")
    _add(db, "book-2", "series-gap", 2, name="Gap Series", kind="comic")

    series = komga_series_browse.series_detail(db, "series-gap")

    assert series is not None
    assert series["name"] == "Gap Series"
    assert series["kind"] == "comic"
    assert series["item_count"] == 3
    assert series["gaps"] == [3]
    assert [row["series_position"] for row in series["items"]] == [1.0, 2.0, 4.0]
