"""Schema and compatibility coverage for copies, location trees and holdings."""

import sqlite3

import pytest

from app.media_types import is_digital_media, is_physical_media
from app.services import holdings, location_tree
from app.services.item_write import insert_item


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

    # Foundation ensure prepares schema, not a recurring seed for newly added
    # flat locations/items. Explicitly project this post-init fixture instead.
    holdings.ensure_foundation(db)
    assert db.execute("SELECT COUNT(*) FROM location_nodes").fetchone()[0] == 0
    db.execute("INSERT INTO location_nodes (id, name) VALUES (100, 'Unmapped ID control')")
    living_node = location_tree.legacy_location_to_node(db, living, create=True)
    bedroom_node = location_tree.legacy_location_to_node(db, bedroom, create=True)
    assert living_node != living and bedroom_node != bedroom
    # This is a NEW owned physical acquisition, so the normal insert funnel
    # deliberately creates its first copy. Reads/sync alone must not do so.
    item_id = insert_item(db, title="Physical Book", media_type="book", source="test",
                          owned=1, location_id=living)
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
    assert copy["location_id"] == living_node

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
    assert (moved["id"], moved["location_id"]) == (copy["id"], bedroom_node)

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
    assert db.execute("SELECT id FROM item_copies WHERE item_id = ?", (item_id,)).fetchall()[0][0] == copy["id"]
    assert db.execute("SELECT COUNT(*) FROM item_copies WHERE item_id = ?", (item_id,)).fetchone()[0] == 1


def test_digital_item_does_not_gain_physical_copy(db):
    item_id = insert_item(
        db, title="Digital Book", media_type="ebook", source="test", owned=1
    )

    assert db.execute(
        "SELECT 1 FROM item_copies WHERE item_id = ?", (item_id,)
    ).fetchone() is None
    assert is_digital_media("ebook") is True
    assert is_physical_media("ebook") is False
    assert is_physical_media("magazine") is True


def test_provider_identifiers_are_synced_into_digital_holdings(db):
    item_id = insert_item(
        db,
        title="Synced Audio",
        media_type="audiobook",
        source="test",
        owned=1,
        abs_id="abs-one",
        abs_library_id="library-a",
    )

    holding = db.execute(
        "SELECT id, provider, external_id, library_id FROM digital_holdings "
        "WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    assert {key: holding[key] for key in ("provider", "external_id", "library_id")} == {
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
        "SELECT id, external_id, library_id FROM digital_holdings "
        "WHERE item_id = ? AND provider = 'audiobookshelf' ORDER BY external_id",
        (item_id,),
    ).fetchall()
    assert [(row["external_id"], row["library_id"]) for row in rows] == [
        ("abs-one", "library-a"),
        ("abs-two", "library-b")
    ]
    assert rows[0]["id"] == holding["id"], "adding a projection must not replace the original identity"
    identities = [tuple(row) for row in rows]
    # Clearing/replacing generic inline metadata is NOT an exact connector
    # detach instruction. The connector owns removal of its one selected ID.
    db.execute("UPDATE items SET abs_id = NULL, abs_library_id = NULL WHERE id = ?", (item_id,))
    holdings.sync_item_holding(db, item_id)
    assert [tuple(row) for row in db.execute(
        "SELECT id, external_id, library_id FROM digital_holdings "
        "WHERE item_id = ? AND provider = 'audiobookshelf' ORDER BY external_id", (item_id,),
    )] == identities


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
