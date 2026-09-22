from app.services import series_browse


def _item(
    db,
    title,
    *,
    series_name=None,
    series_position=None,
    cover_path=None,
    media_type="book",
    authors=None,
    publish_year=None,
    created_at=None,
):
    cur = db.execute(
        "INSERT INTO items "
        "(title, media_type, owned, source, series_name, series_position, cover_path, "
        " authors, publish_year, created_at) "
        "VALUES (?, ?, 1, 'test', ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
        (
            title,
            media_type,
            series_name,
            series_position,
            cover_path,
            authors,
            publish_year,
            created_at,
        ),
    )
    return cur.lastrowid


def test_series_collapses_before_pagination(db):
    alpha_ids = [
        _item(db, f"Alpha {n}", series_name="Alpha Saga", series_position=n)
        for n in range(1, 8)
    ]
    _item(db, "Beta")
    _item(db, "Gamma")

    first, total = series_browse.fetch_units(db, limit=2, offset=0)
    second, total_again = series_browse.fetch_units(db, limit=2, offset=2)

    assert total == total_again == 3
    assert len(first) == 2
    assert len(second) == 1
    alpha = next(unit for unit in first if unit["is_series"])
    assert alpha["unit_label"] == "Alpha Saga"
    assert alpha["member_count"] == 7
    assert alpha["member_ids"] == alpha_ids


def test_non_series_items_remain_independent_units(db):
    first_id = _item(db, "Same title")
    second_id = _item(db, "Same title")

    units, total = series_browse.fetch_units(db)

    assert total == 2
    assert {unit["id"] for unit in units} == {first_id, second_id}
    assert all(not unit["is_series"] for unit in units)
    assert all(unit["member_count"] == 1 for unit in units)
    assert {tuple(unit["member_ids"]) for unit in units} == {(first_id,), (second_id,)}


def test_series_identity_is_case_insensitive(db):
    first = _item(db, "Dune", series_name="Dune Saga", series_position=1)
    second = _item(db, "Dune Messiah", series_name="dune saga", series_position=2)

    units, total = series_browse.fetch_units(db)

    assert total == 1
    assert len(units) == 1
    assert units[0]["member_count"] == 2
    assert units[0]["member_ids"] == [first, second]
    assert units[0]["unit_key"] == "series:dune saga"


def test_series_namespace_cannot_collide_with_standalone_item_key(db):
    standalone_id = _item(db, "Standalone")
    series_id = _item(
        db,
        "Oddly Named Series Member",
        series_name=f"item:{standalone_id}",
        series_position=1,
    )

    units, total = series_browse.fetch_units(db)

    assert total == 2
    assert {unit["id"] for unit in units} == {standalone_id, series_id}
    assert {unit["unit_key"] for unit in units} == {
        f"item:{standalone_id}",
        f"series:item:{standalone_id}",
    }


def test_representative_prefers_earliest_numbered_member_with_artwork(db):
    volume_one = _item(
        db,
        "Volume One",
        series_name="Art Saga",
        series_position=1,
        cover_path=None,
    )
    volume_two = _item(
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

    units, total = series_browse.fetch_units(db)

    assert total == 1
    assert units[0]["id"] == volume_two
    assert units[0]["id"] != volume_one
    assert units[0]["cover_path"] == "covers/two.jpg"


def test_blank_series_name_is_not_grouped(db):
    first_id = _item(db, "Blank A", series_name="")
    second_id = _item(db, "Blank B", series_name="   ")

    units, total = series_browse.fetch_units(db)

    assert total == 2
    assert {unit["id"] for unit in units} == {first_id, second_id}


def test_filter_applies_before_grouping_and_member_ids_follow_visible_result(db):
    book_id = _item(
        db,
        "Book edition",
        series_name="Mixed Series",
        series_position=1,
        media_type="book",
    )
    _item(
        db,
        "Comic edition",
        series_name="Mixed Series",
        series_position=2,
        media_type="comic",
    )

    units, total = series_browse.fetch_units(
        db,
        where="WHERE i.media_type = ?",
        params=["book"],
    )

    assert total == 1
    assert units[0]["member_count"] == 1
    assert units[0]["member_ids"] == [book_id]


def test_newest_sort_uses_newest_member_not_representative(db):
    _item(
        db,
        "Volume One",
        series_name="Old Cover Series",
        series_position=1,
        cover_path="covers/one.jpg",
        created_at="2025-01-01 00:00:00",
    )
    _item(
        db,
        "Volume Two",
        series_name="Old Cover Series",
        series_position=2,
        created_at="2026-08-01 00:00:00",
    )
    _item(db, "Middle", created_at="2026-02-01 00:00:00")

    units, _ = series_browse.fetch_units(db, sort="newest")

    assert [unit["unit_label"] for unit in units] == ["Old Cover Series", "Middle"]
    assert units[0]["title"] == "Volume One"  # representative remains cover-aware


def test_title_sort_uses_series_name(db):
    _item(db, "Zebra Volume", series_name="Alpha Series", series_position=1)
    _item(db, "Beta standalone")

    units, _ = series_browse.fetch_units(db, sort="title_asc")

    assert [unit["unit_label"] for unit in units] == ["Alpha Series", "Beta standalone"]


def test_trashed_members_do_not_participate(db):
    live_id = _item(db, "Live", series_name="Trash Test", series_position=1)
    trashed_id = _item(db, "Trashed", series_name="Trash Test", series_position=2)
    db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (trashed_id,))

    units, total = series_browse.fetch_units(db)

    assert total == 1
    assert units[0]["member_count"] == 1
    assert units[0]["member_ids"] == [live_id]


def test_limit_and_offset_are_bounded(db):
    for n in range(3):
        _item(db, f"Item {n}")

    units, total = series_browse.fetch_units(db, limit=0, offset=-10)

    assert total == 3
    assert len(units) == 1
