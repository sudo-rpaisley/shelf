"""Schema and compatibility coverage for copies, location trees and holdings."""

import sqlite3

import pytest

from app.media_types import is_digital_media, is_physical_media
from app.services import holdings


def test_location_tree_allows_same_child_name_under_different_parents(db):
    holdings.install_schema(db)
    living = db.execute(
        "INSERT INTO location_nodes (name, sort_order) VALUES ('Living Room', 0)"
    ).lastrowid
    bedroom = db.execute(
        "INSERT INTO location_nodes (name, sort_order) VALUES ('Bedroom', 1)"
    ).lastrowid

    db.execute(
        "INSERT INTO location_nodes (parent_id, name) VALUES (?, 'Shelf 1')",
        (living,),
    )
    db.execute(
        "INSERT INTO location_nodes (parent_id, name) VALUES (?, 'Shelf 1')",
        (bedroom,),
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO location_nodes (parent_id, name) VALUES (?, 'shelf 1')",
            (living,),
        )


def test_legacy_location_and_owned_item_sync_into_primary_copy(db):
    living = db.execute(
        "INSERT INTO locations (name, sort_order) VALUES ('Living Room', 0)"
    ).lastrowid
    bedroom = db.execute(
        "INSERT INTO locations (name, sort_order) VALUES ('Bedroom', 1)"
    ).lastrowid

    item_id = db.execute(
        "INSERT INTO items (title, media_type, source, owned, location_id) "
        "VALUES ('Physical Book', 'book', 'test', 1, ?)",
        (living,),
    ).lastrowid
    holdings.sync_item_holding(db, item_id)

    copy = db.execute(
        "SELECT c.*, n.legacy_location_id FROM item_copies c "
        "LEFT JOIN location_nodes n ON n.id = c.location_id "
        "WHERE c.item_id = ? AND c.is_primary = 1",
        (item_id,),
    ).fetchone()
    assert copy is not None
    assert copy["copy_number"] == 1
    assert copy["legacy_location_id"] == living

    # Old flat move routes still move a copy while it remains on a legacy root.
    db.execute("UPDATE items SET location_id = ? WHERE id = ?", (bedroom, item_id))
    holdings.sync_item_holding(db, item_id)
    moved = db.execute(
        "SELECT c.id, c.location_id, n.legacy_location_id FROM item_copies c "
        "LEFT JOIN location_nodes n ON n.id = c.location_id "
        "WHERE c.item_id = ? AND c.is_primary = 1",
        (item_id,),
    ).fetchone()
    assert moved["legacy_location_id"] == bedroom

    # Once the copy has a precise child location, legacy compatibility updates
    # must not flatten it back to a room/root.
    shelf = db.execute(
        "INSERT INTO location_nodes (parent_id, name) VALUES (?, 'Shelf 1')",
        (moved["location_id"],),
    ).lastrowid
    db.execute("UPDATE item_copies SET location_id = ? WHERE id = ?", (shelf, moved["id"]))
    db.execute("UPDATE items SET location_id = ? WHERE id = ?", (living, item_id))
    holdings.sync_item_holding(db, item_id)
    precise = db.execute(
        "SELECT location_id FROM item_copies WHERE id = ?", (moved["id"],)
    ).fetchone()
    assert precise["location_id"] == shelf


def test_digital_item_does_not_gain_physical_copy(db):
    item_id = db.execute(
        "INSERT INTO items (title, media_type, source, owned) "
        "VALUES ('Digital Book', 'ebook', 'test', 1)"
    ).lastrowid
    holdings.sync_item_holding(db, item_id)

    assert db.execute(
        "SELECT 1 FROM item_copies WHERE item_id = ?", (item_id,)
    ).fetchone() is None
    assert is_digital_media("ebook") is True
    assert is_physical_media("ebook") is False
    assert is_physical_media("magazine") is True


def test_provider_identifiers_are_synced_into_digital_holdings(db):
    item_id = db.execute(
        "INSERT INTO items (title, media_type, source, owned, abs_id, abs_library_id) "
        "VALUES ('Synced Audio', 'audiobook', 'test', 1, 'abs-one', 'library-a')"
    ).lastrowid
    holdings.sync_item_holding(db, item_id)

    holding = db.execute(
        "SELECT provider, external_id, library_id FROM digital_holdings "
        "WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    assert dict(holding) == {
        "provider": "audiobookshelf",
        "external_id": "abs-one",
        "library_id": "library-a",
    }

    db.execute(
        "UPDATE items SET abs_id = 'abs-two', abs_library_id = 'library-b' WHERE id = ?",
        (item_id,),
    )
    holdings.sync_item_holding(db, item_id)
    rows = db.execute(
        "SELECT external_id, library_id FROM digital_holdings "
        "WHERE item_id = ? AND provider = 'audiobookshelf'",
        (item_id,),
    ).fetchall()
    assert [(row["external_id"], row["library_id"]) for row in rows] == [
        ("abs-two", "library-b")
    ]


def test_periodical_publication_can_own_multiple_issue_items(db):
    holdings.install_schema(db)
    publication_id = db.execute(
        "INSERT INTO periodical_publications (title, issn, publisher) "
        "VALUES ('Example Monthly', '1234-567X', 'Example Press')"
    ).lastrowid
    jan = db.execute(
        "INSERT INTO items (title, media_type, source, owned) "
        "VALUES ('Example Monthly — January 2026', 'magazine', 'test', 1)"
    ).lastrowid
    feb = db.execute(
        "INSERT INTO items (title, media_type, source, owned) "
        "VALUES ('Example Monthly — February 2026', 'magazine', 'test', 1)"
    ).lastrowid

    db.execute(
        "INSERT INTO periodical_issues "
        "(item_id, publication_id, issue_number, issue_date, barcode_supplement) "
        "VALUES (?, ?, '1', '2026-01-01', '01')",
        (jan, publication_id),
    )
    db.execute(
        "INSERT INTO periodical_issues "
        "(item_id, publication_id, issue_number, issue_date, barcode_supplement) "
        "VALUES (?, ?, '2', '2026-02-01', '02')",
        (feb, publication_id),
    )

    issues = db.execute(
        "SELECT item_id, issue_number, issue_date FROM periodical_issues "
        "WHERE publication_id = ? ORDER BY issue_date",
        (publication_id,),
    ).fetchall()
    assert [(row["item_id"], row["issue_number"]) for row in issues] == [
        (jan, "1"),
        (feb, "2"),
    ]
