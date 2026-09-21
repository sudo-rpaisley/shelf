from app.services import series_browse


def _item(
    db,
    title,
    *,
    series_name=None,
    series_position=None,
    cover_path=None,
):
    cur = db.execute(
        "INSERT INTO items "
        "(title, media_type, owned, source, series_name, series_position, cover_path) "
        "VALUES (?, 'book', 1, 'test', ?, ?, ?)",
        (title, series_name, series_position, cover_path),
    )
    return cur.lastrowid


def test_series_collapses_before_pagination(db):
    for n in range(1, 8):
        _item(db, f"Alpha {n}", series_name="Alpha Saga", series_position=n)
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


def test_non_series_items_remain_independent_units(db):
    first_id = _item(db, "Same title")
    second_id = _item(db, "Same title")

    units, total = series_browse.fetch_units(db)

    assert total == 2
    assert {unit["id"] for unit in units} == {first_id, second_id}
    assert all(not unit["is_series"] for unit in units)
    assert all(unit["member_count"] == 1 for unit in units)


def test_series_identity_is_case_insensitive(db):
    _item(db, "Dune", series_name="Dune Saga", series_position=1)
    _item(db, "Dune Messiah", series_name="dune saga", series_position=2)

    units, total = series_browse.fetch_units(db)

    assert total == 1
    assert len(units) == 1
    assert units[0]["member_count"] == 2
    assert units[0]["unit_key"] == "series:dune saga"


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


def test_limit_and_offset_are_bounded(db):
    for n in range(3):
        _item(db, f"Item {n}")

    units, total = series_browse.fetch_units(db, limit=0, offset=-10)

    assert total == 3
    assert len(units) == 1
