from app.services import lists, series_browse


def _item(
    db,
    title,
    *,
    series_name=None,
    series_position=None,
    cover_path=None,
    source="test",
    owned=1,
):
    cur = db.execute(
        "INSERT INTO items "
        "(title, media_type, owned, source, series_name, series_position, cover_path) "
        "VALUES (?, 'book', ?, ?, ?, ?, ?)",
        (title, owned, source, series_name, series_position, cover_path),
    )
    return cur.lastrowid


def _units(db, *, where="", params=None, limit=60, offset=0, order=None):
    return series_browse.fetch_units(
        db,
        where,
        params or [],
        order or "i.title COLLATE NOCASE ASC",
        limit=limit,
        offset=offset,
    )


def test_series_collapses_before_pagination(db):
    for n in range(1, 8):
        _item(db, f"Alpha {n}", series_name="Alpha Saga", series_position=n)
    _item(db, "Beta")
    _item(db, "Gamma")

    first, total = _units(db, limit=2, offset=0)
    second, total_again = _units(db, limit=2, offset=2)

    assert total == total_again == 3
    assert len(first) == 2
    assert len(second) == 1
    alpha = next(unit for unit in first if unit["browse_series_group"])
    assert alpha["browse_series_name"] == "Alpha Saga"
    assert alpha["browse_series_count"] == 7
    assert len(alpha["browse_series_item_ids"]) == 7


def test_non_series_items_remain_independent_units(db):
    first_id = _item(db, "Same title")
    second_id = _item(db, "Same title")

    units, total = _units(db)

    assert total == 2
    assert {unit["id"] for unit in units} == {first_id, second_id}
    assert all(not unit["browse_series_group"] for unit in units)
    assert all(unit["browse_series_item_ids"] == [unit["id"]] for unit in units)


def test_series_identity_is_case_insensitive(db):
    first = _item(db, "Dune", series_name="Dune Saga", series_position=1)
    second = _item(db, "Dune Messiah", series_name="dune saga", series_position=2)

    units, total = _units(db)

    assert total == 1
    assert set(units[0]["browse_series_item_ids"]) == {first, second}
    assert units[0]["browse_series_count"] == 2


def test_representative_cover_prefers_earliest_numbered_member_with_artwork(db):
    _item(db, "Volume One", series_name="Art Saga", series_position=1)
    second = _item(
        db,
        "Volume Two",
        series_name="Art Saga",
        series_position=2,
        cover_path="covers/two.jpg",
    )
    _item(
        db,
        "Volume Three",
        series_name="Art Saga",
        series_position=3,
        cover_path="covers/three.jpg",
    )

    units, total = _units(db)

    assert total == 1
    assert second in units[0]["browse_series_item_ids"]
    assert units[0]["browse_series_cover_path"] == "covers/two.jpg"


def test_filters_apply_before_grouping_and_bulk_member_ids(db):
    komga = _item(
        db,
        "Filtered One",
        series_name="Mixed Source",
        series_position=1,
        source="komga",
    )
    _item(
        db,
        "Manual Two",
        series_name="Mixed Source",
        series_position=2,
        source="manual",
    )

    units, total = _units(
        db,
        where="WHERE i.source = ?",
        params=["komga"],
    )

    assert total == 1
    assert units[0]["browse_series_item_ids"] == [komga]
    assert units[0]["browse_series_count"] == 1


def test_blank_series_name_is_not_grouped(db):
    first_id = _item(db, "Blank A", series_name="")
    second_id = _item(db, "Blank B", series_name="   ")

    units, total = _units(db)

    assert total == 2
    assert {unit["id"] for unit in units} == {first_id, second_id}


def test_trashed_items_are_absent_from_group_and_detail(db):
    visible = _item(db, "Visible", series_name="Trash Saga", series_position=1)
    hidden = _item(db, "Hidden", series_name="Trash Saga", series_position=2)
    db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (hidden,))

    units, total = _units(db)
    series = series_browse.series_detail(db, "Trash Saga")

    assert total == 1
    assert units[0]["browse_series_item_ids"] == [visible]
    assert series is not None
    assert [item["id"] for item in series["items"]] == [visible]


def test_series_detail_uses_name_identity_and_reports_states_and_gaps(db):
    owned = _item(db, "One", series_name="State Saga", series_position=1)
    wished = _item(
        db, "Two", series_name="state saga", series_position=2, owned=0
    )
    _item(db, "Four", series_name="STATE SAGA", series_position=4, owned=0)
    lists.set_membership(db, lists.WISHLIST, [wished], True)

    series = series_browse.series_detail(db, "State Saga")

    assert series is not None
    assert series["item_count"] == 3
    assert series["owned_count"] == 1
    assert series["wishlist_count"] == 1
    assert series["neither_count"] == 1
    assert series["gaps"] == [3]
    assert {item["id"] for item in series["items"]} >= {owned, wished}


def test_merge_details_preserves_series_metadata_and_uses_group_cover():
    unit = {
        "id": 7,
        "browse_series_group": True,
        "browse_series_count": 3,
        "browse_series_item_ids": [7, 8, 9],
        "browse_series_cover_path": "covers/8.jpg",
        "browse_series_name": "Example",
        "browse_series_url": "/series/Example",
    }
    row = {
        "id": 7,
        "title": "Volume One",
        "cover_path": None,
        "authors": "Author",
    }

    merged = series_browse.merge_item_details([unit], [row])

    assert merged[0]["cover_path"] == "covers/8.jpg"
    assert merged[0]["browse_series_item_ids"] == [7, 8, 9]


def test_limit_and_offset_are_bounded(db):
    for n in range(3):
        _item(db, f"Item {n}")

    units, total = _units(db, limit=0, offset=-10)

    assert total == 3
    assert len(units) == 1
