"""Design-spike coverage for upstream issue #97.

These tests intentionally exercise the copy model in isolation.  They do not
wire it into the current item routes yet; the purpose of the branch is to prove
migration/backfill and compatibility semantics before that architecture is
committed upstream.
"""

import sqlite3

import pytest

from app.services import item_copies


def _location(db, name="Living Room"):
    return db.execute("INSERT INTO locations (name) VALUES (?)", (name,)).lastrowid


def _item(db, title="Copy Test", *, owned=1, location_id=None, media_type="book"):
    return db.execute(
        "INSERT INTO items (title, media_type, source, owned, location_id) "
        "VALUES (?, ?, 'test', ?, ?)",
        (title, media_type, owned, location_id),
    ).lastrowid


def test_backfill_projects_only_owned_items_with_explicit_locations(db):
    living = _location(db)
    placed = _item(db, "Placed", owned=1, location_id=living)
    unplaced = _item(db, "Unplaced", owned=1, location_id=None)
    wishlist = _item(db, "Wishlist", owned=0, location_id=living)
    digital_style = _item(
        db, "Digital audiobook", owned=1, location_id=None, media_type="audiobook"
    )

    assert item_copies.backfill_legacy_locations(db) == 1
    assert item_copies.backfill_legacy_locations(db) == 0

    rows = db.execute(
        "SELECT item_id, copy_number, location_id, is_primary FROM item_copies "
        "ORDER BY item_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(placed, 1, living, 1)]
    assert {unplaced, wishlist, digital_style}.isdisjoint({row["item_id"] for row in rows})


def test_copy_model_allows_zero_one_or_many_rows_for_an_item(db):
    living = _location(db)
    item_id = _item(db, location_id=None)
    item_copies.install_schema(db)

    assert item_copies.copies_for_item(db, item_id) == []

    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
        "VALUES (?, 1, ?, 1)",
        (item_id, living),
    )
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, location_id, condition) "
        "VALUES (?, 2, ?, 'good')",
        (item_id, living),
    )

    copies = item_copies.copies_for_item(db, item_id)
    assert [(row["copy_number"], row["location_id"]) for row in copies] == [
        (1, living),
        (2, living),
    ]

    db.execute("DELETE FROM item_copies WHERE item_id = ?", (item_id,))
    assert item_copies.copies_for_item(db, item_id) == []


def test_primary_copy_is_unique_per_item(db):
    item_copies.install_schema(db)
    item_id = _item(db)
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, is_primary) VALUES (?, 1, 1)",
        (item_id,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO item_copies (item_id, copy_number, is_primary) VALUES (?, 2, 1)",
            (item_id,),
        )


def test_copy_numbers_are_unique_within_item_but_can_repeat_across_items(db):
    item_copies.install_schema(db)
    first = _item(db, "First")
    second = _item(db, "Second")
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, is_primary) VALUES (?, 1, 1)",
        (first,),
    )
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, is_primary) VALUES (?, 1, 1)",
        (second,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO item_copies (item_id, copy_number) VALUES (?, 1)",
            (first,),
        )


def test_copy_barcode_is_unique_across_collection(db):
    item_copies.install_schema(db)
    first = _item(db, "First")
    second = _item(db, "Second")
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, copy_barcode, is_primary) "
        "VALUES (?, 1, 'ASSET-42', 1)",
        (first,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO item_copies (item_id, copy_number, copy_barcode, is_primary) "
            "VALUES (?, 1, 'ASSET-42', 1)",
            (second,),
        )


def test_copy_cascade_does_not_change_existing_item_contract(db):
    item_copies.install_schema(db)
    item_id = _item(db)
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, condition, is_primary) "
        "VALUES (?, 1, 'very_good', 1)",
        (item_id,),
    )

    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    assert db.execute(
        "SELECT 1 FROM item_copies WHERE item_id = ?", (item_id,)
    ).fetchone() is None


def test_sync_primary_location_creates_first_copy_only_for_non_null_location(db):
    living = _location(db)
    bedroom = _location(db, "Bedroom")
    item_id = _item(db)

    assert item_copies.sync_primary_location(db, item_id, None) is None
    assert item_copies.copies_for_item(db, item_id) == []

    copy_id = item_copies.sync_primary_location(db, item_id, living)
    copy = db.execute("SELECT * FROM item_copies WHERE id = ?", (copy_id,)).fetchone()
    assert copy["copy_number"] == 1
    assert copy["location_id"] == living
    assert copy["is_primary"] == 1

    assert item_copies.sync_primary_location(db, item_id, bedroom) == copy_id
    moved = db.execute("SELECT location_id FROM item_copies WHERE id = ?", (copy_id,)).fetchone()
    assert moved["location_id"] == bedroom

    assert item_copies.sync_primary_location(db, item_id, None) == copy_id
    cleared = db.execute("SELECT location_id FROM item_copies WHERE id = ?", (copy_id,)).fetchone()
    assert cleared["location_id"] is None


def test_sync_primary_location_does_not_move_secondary_copies(db):
    living = _location(db)
    bedroom = _location(db, "Bedroom")
    item_id = _item(db)
    primary_id = item_copies.sync_primary_location(db, item_id, living)
    secondary_id = db.execute(
        "INSERT INTO item_copies (item_id, copy_number, location_id) VALUES (?, 2, ?)",
        (item_id, living),
    ).lastrowid

    item_copies.sync_primary_location(db, item_id, bedroom)

    rows = db.execute(
        "SELECT id, location_id FROM item_copies WHERE item_id = ? ORDER BY copy_number",
        (item_id,),
    ).fetchall()
    assert [(row["id"], row["location_id"]) for row in rows] == [
        (primary_id, bedroom),
        (secondary_id, living),
    ]


def test_sync_rejects_unknown_flat_location(db):
    item_id = _item(db)
    with pytest.raises(ValueError, match="Location not found"):
        item_copies.sync_primary_location(db, item_id, 99999)


def test_copy_metadata_fields_are_independent_between_copies(db):
    item_copies.install_schema(db)
    item_id = _item(db)
    first = db.execute(
        "INSERT INTO item_copies "
        "(item_id, copy_number, condition, acquired_date, acquisition_source, "
        "acquisition_price, provenance, notes, is_primary) "
        "VALUES (?, 1, 'good', '2026-01-02', 'Bookshop', 12.50, 'Signed by author', "
        "'Reading copy', 1)",
        (item_id,),
    ).lastrowid
    second = db.execute(
        "INSERT INTO item_copies "
        "(item_id, copy_number, condition, acquisition_source, notes) "
        "VALUES (?, 2, 'poor', 'Charity shop', 'Spare copy')",
        (item_id,),
    ).lastrowid

    one = db.execute("SELECT * FROM item_copies WHERE id = ?", (first,)).fetchone()
    two = db.execute("SELECT * FROM item_copies WHERE id = ?", (second,)).fetchone()
    assert one["condition"] == "good"
    assert one["acquisition_price"] == 12.5
    assert one["provenance"] == "Signed by author"
    assert two["condition"] == "poor"
    assert two["acquisition_price"] is None
    assert two["notes"] == "Spare copy"
