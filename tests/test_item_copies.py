"""Product coverage for managing multiple physical copies of one catalogue item."""

from app.services import holdings
from app.services.item_write import insert_item


def _root_and_shelf(db, room_name="Living Room", shelf_name="Shelf 1"):
    legacy_id = db.execute(
        "INSERT INTO locations (name) VALUES (?)", (room_name,)
    ).lastrowid
    holdings.ensure_legacy_location_nodes(db)
    root_id = db.execute(
        "SELECT id FROM location_nodes WHERE legacy_location_id = ?", (legacy_id,)
    ).fetchone()["id"]
    shelf_id = db.execute(
        "INSERT INTO location_nodes (parent_id, name) VALUES (?, ?)",
        (root_id, shelf_name),
    ).lastrowid
    return legacy_id, root_id, shelf_id


def _book(db, title="Copy Test", location_id=None):
    return insert_item(
        db,
        title=title,
        media_type="book",
        source="test",
        owned=1,
        location_id=location_id,
    )


def test_two_copies_can_share_the_same_shelf(editor_client, db):
    legacy, _root, shelf = _root_and_shelf(db)
    item_id = _book(db, location_id=legacy)
    primary = db.execute(
        "SELECT id FROM item_copies WHERE item_id = ?", (item_id,)
    ).fetchone()["id"]
    db.execute("UPDATE item_copies SET location_id = ? WHERE id = ?", (shelf, primary))
    db.commit()

    response = editor_client.post(
        f"/api/items/{item_id}/copies",
        data={"location_id": shelf, "condition": "good", "copy_barcode": "COPY-002"},
    )
    assert response.status_code == 200
    rows = db.execute(
        "SELECT copy_number, location_id, is_primary FROM item_copies "
        "WHERE item_id = ? ORDER BY copy_number",
        (item_id,),
    ).fetchall()
    assert [(r["copy_number"], r["location_id"]) for r in rows] == [(1, shelf), (2, shelf)]
    assert [r["is_primary"] for r in rows] == [1, 0]


def test_copy_metadata_can_be_updated_and_primary_move_updates_legacy_room(editor_client, db):
    living_legacy, _living, _living_shelf = _root_and_shelf(db, "Living Room", "Shelf 1")
    bedroom_legacy, _bedroom, bedroom_shelf = _root_and_shelf(db, "Bedroom", "Shelf 1")
    item_id = _book(db, location_id=living_legacy)
    copy_id = db.execute(
        "SELECT id FROM item_copies WHERE item_id = ? AND is_primary = 1", (item_id,)
    ).fetchone()["id"]
    db.commit()

    response = editor_client.post(
        f"/api/items/{item_id}/copies/{copy_id}",
        data={
            "location_id": bedroom_shelf,
            "condition": "very_good",
            "notes": "Signed copy",
            "acquired_date": "2026-09-03",
            "acquisition_price": "12.50",
            "copy_barcode": "SIGNED-1",
        },
    )
    assert response.status_code == 200
    copy = db.execute("SELECT * FROM item_copies WHERE id = ?", (copy_id,)).fetchone()
    assert copy["location_id"] == bedroom_shelf
    assert copy["condition"] == "very_good"
    assert copy["notes"] == "Signed copy"
    assert copy["acquired_date"] == "2026-09-03"
    assert copy["acquisition_price"] == 12.5
    assert copy["copy_barcode"] == "SIGNED-1"
    assert db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()["location_id"] == bedroom_legacy


def test_copy_barcode_is_unique_across_physical_copies(editor_client, db):
    legacy, _root, shelf = _root_and_shelf(db)
    first = _book(db, "First", legacy)
    second = _book(db, "Second", legacy)
    db.commit()

    assert editor_client.post(
        f"/api/items/{first}/copies",
        data={"location_id": shelf, "copy_barcode": "ASSET-42"},
    ).status_code == 200
    response = editor_client.post(
        f"/api/items/{second}/copies",
        data={"location_id": shelf, "copy_barcode": "ASSET-42"},
    )
    assert response.status_code == 200
    assert "already in use" in response.text
    assert db.execute(
        "SELECT COUNT(*) AS c FROM item_copies WHERE copy_barcode = 'ASSET-42'"
    ).fetchone()["c"] == 1


def test_last_owned_physical_copy_cannot_be_removed(editor_client, db):
    legacy, _root, _shelf = _root_and_shelf(db)
    item_id = _book(db, location_id=legacy)
    copy_id = db.execute(
        "SELECT id FROM item_copies WHERE item_id = ?", (item_id,)
    ).fetchone()["id"]
    db.commit()

    response = editor_client.delete(f"/api/items/{item_id}/copies/{copy_id}")
    assert response.status_code == 200
    assert "must keep at least one copy" in response.text
    assert db.execute(
        "SELECT COUNT(*) AS c FROM item_copies WHERE item_id = ?", (item_id,)
    ).fetchone()["c"] == 1


def test_deleting_primary_copy_promotes_next_copy_and_updates_legacy_location(editor_client, db):
    living_legacy, _living, _ = _root_and_shelf(db, "Living Room", "Shelf 1")
    bedroom_legacy, _bedroom, bedroom_shelf = _root_and_shelf(db, "Bedroom", "Shelf 1")
    item_id = _book(db, location_id=living_legacy)
    primary = db.execute(
        "SELECT id FROM item_copies WHERE item_id = ? AND is_primary = 1", (item_id,)
    ).fetchone()["id"]
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
        "VALUES (?, 2, ?, 0)",
        (item_id, bedroom_shelf),
    )
    db.commit()

    response = editor_client.delete(f"/api/items/{item_id}/copies/{primary}")
    assert response.status_code == 200
    replacement = db.execute(
        "SELECT copy_number, is_primary, location_id FROM item_copies WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    assert replacement["copy_number"] == 2
    assert replacement["is_primary"] == 1
    assert replacement["location_id"] == bedroom_shelf
    assert db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()["location_id"] == bedroom_legacy


def test_digital_items_do_not_accept_physical_copies(editor_client, db):
    item_id = insert_item(
        db, title="Digital Only", media_type="ebook", source="test", owned=1
    )
    db.commit()

    fragment = editor_client.get(f"/api/items/{item_id}/copies")
    assert fragment.status_code == 200
    assert "does not have physical copies" in fragment.text

    response = editor_client.post(f"/api/items/{item_id}/copies", data={})
    assert response.status_code == 200
    assert "Only owned physical media can have copies" in response.text
    assert db.execute(
        "SELECT 1 FROM item_copies WHERE item_id = ?", (item_id,)
    ).fetchone() is None


def test_viewer_cannot_add_or_delete_copies(viewer_client, db):
    legacy, _root, _shelf = _root_and_shelf(db)
    item_id = _book(db, location_id=legacy)
    copy_id = db.execute(
        "SELECT id FROM item_copies WHERE item_id = ?", (item_id,)
    ).fetchone()["id"]
    db.commit()

    assert viewer_client.post(f"/api/items/{item_id}/copies", data={}).status_code == 403
    assert viewer_client.delete(f"/api/items/{item_id}/copies/{copy_id}").status_code == 403
