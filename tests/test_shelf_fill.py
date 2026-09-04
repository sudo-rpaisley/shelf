from pathlib import Path

from app.database import get_db
from app.services import holdings
from app.services.item_write import insert_item


def _nested_location():
    with get_db() as db:
        legacy_id = db.execute(
            "INSERT INTO locations (name, sort_order) VALUES ('Living Room', 1)"
        ).lastrowid
        holdings.ensure_foundation(db)
        root_id = db.execute(
            "SELECT id FROM location_nodes WHERE legacy_location_id = ?", (legacy_id,)
        ).fetchone()["id"]
        bookcase_id = db.execute(
            "INSERT INTO location_nodes (parent_id, name, sort_order) VALUES (?, 'Bookcase', 1)",
            (root_id,),
        ).lastrowid
        shelf_id = db.execute(
            "INSERT INTO location_nodes (parent_id, name, sort_order) VALUES (?, 'Shelf 1', 1)",
            (bookcase_id,),
        ).lastrowid
    return legacy_id, root_id, bookcase_id, shelf_id


def _physical_item(isbn, legacy_id, *, title="Test Book", owned=1):
    with get_db() as db:
        return insert_item(
            db,
            title=title,
            isbn=isbn,
            media_type="book",
            source="test",
            owned=owned,
            location_id=legacy_id,
        )


def test_shelf_fill_picker_uses_hierarchical_paths(editor_client):
    _nested_location()

    response = editor_client.get("/api/shelf-fill/location-picker")

    assert response.status_code == 200
    assert 'id="shelf-fill-location"' in response.text
    assert "Living Room › Bookcase › Shelf 1" in response.text


def test_shelf_fill_existing_item_moves_primary_copy_and_sets_position(editor_client):
    legacy_id, _, _, shelf_id = _nested_location()
    item_id = _physical_item("9780000000100", legacy_id)

    response = editor_client.post(
        "/api/shelf-fill/scan",
        data={"isbn": "9780000000100", "location_node_id": shelf_id, "media_type": "auto"},
    )

    assert response.status_code == 200
    assert "shelved" in response.text
    assert "Living Room › Bookcase › Shelf 1" in response.text
    assert "position 1" in response.text
    with get_db() as db:
        copy = db.execute(
            "SELECT location_id, position_order FROM item_copies "
            "WHERE item_id = ? AND is_primary = 1",
            (item_id,),
        ).fetchone()
        item = db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()
    assert copy["location_id"] == shelf_id
    assert copy["position_order"] == 1
    assert item["location_id"] == legacy_id


def test_shelf_fill_scan_order_appends_positions(editor_client):
    legacy_id, _, _, shelf_id = _nested_location()
    first_id = _physical_item("9780000000101", legacy_id, title="First")
    second_id = _physical_item("9780000000102", legacy_id, title="Second")

    for code in ("9780000000101", "9780000000102"):
        response = editor_client.post(
            "/api/shelf-fill/scan",
            data={"isbn": code, "location_node_id": shelf_id, "media_type": "auto"},
        )
        assert response.status_code == 200

    with get_db() as db:
        rows = db.execute(
            "SELECT item_id, position_order FROM item_copies "
            "WHERE location_id = ? ORDER BY position_order",
            (shelf_id,),
        ).fetchall()
    assert [(row["item_id"], row["position_order"]) for row in rows] == [
        (first_id, 1),
        (second_id, 2),
    ]


def test_shelf_fill_wishlist_item_becomes_owned(editor_client):
    legacy_id, _, _, shelf_id = _nested_location()
    item_id = _physical_item("9780000000103", legacy_id, owned=0)

    response = editor_client.post(
        "/api/shelf-fill/scan",
        data={"isbn": "9780000000103", "location_node_id": shelf_id, "media_type": "auto"},
    )

    assert response.status_code == 200
    assert "Moved from wishlist and shelved" in response.text
    with get_db() as db:
        item = db.execute("SELECT owned FROM items WHERE id = ?", (item_id,)).fetchone()
        copy = db.execute(
            "SELECT location_id FROM item_copies WHERE item_id = ? AND is_primary = 1",
            (item_id,),
        ).fetchone()
    assert item["owned"] == 1
    assert copy["location_id"] == shelf_id


def test_shelf_fill_copy_barcode_moves_exact_copy(editor_client):
    legacy_id, root_id, _, shelf_id = _nested_location()
    item_id = _physical_item("9780000000104", legacy_id)
    with get_db() as db:
        second_copy_id = db.execute(
            "INSERT INTO item_copies "
            "(item_id, copy_number, location_id, copy_barcode, is_primary) "
            "VALUES (?, 2, ?, 'COPY-0002', 0)",
            (item_id, root_id),
        ).lastrowid
        primary_id = db.execute(
            "SELECT id FROM item_copies WHERE item_id = ? AND is_primary = 1",
            (item_id,),
        ).fetchone()["id"]

    response = editor_client.post(
        "/api/shelf-fill/scan",
        data={"isbn": "COPY-0002", "location_node_id": shelf_id, "media_type": "auto"},
    )

    assert response.status_code == 200
    assert "copy 2" in response.text
    with get_db() as db:
        second = db.execute(
            "SELECT location_id, position_order FROM item_copies WHERE id = ?",
            (second_copy_id,),
        ).fetchone()
        primary = db.execute(
            "SELECT location_id FROM item_copies WHERE id = ?", (primary_id,)
        ).fetchone()
    assert second["location_id"] == shelf_id
    assert second["position_order"] == 1
    assert primary["location_id"] == root_id


def test_shelf_fill_rejects_digital_item(editor_client):
    _, _, _, shelf_id = _nested_location()
    with get_db() as db:
        insert_item(
            db,
            title="Digital Test",
            isbn="9780000000105",
            media_type="ebook",
            source="test",
            owned=1,
        )

    response = editor_client.post(
        "/api/shelf-fill/scan",
        data={"isbn": "9780000000105", "location_node_id": shelf_id, "media_type": "auto"},
    )

    assert response.status_code == 200
    assert "Shelf Fill only works with physical media" in response.text


def test_shelf_fill_unknown_scan_delegates_to_add_then_places(editor_client, monkeypatch):
    legacy_id, _, _, shelf_id = _nested_location()
    from app.routers import items

    async def fake_add(
        request,
        isbn,
        media_type="book",
        location_id=None,
        platform="",
        mode="add",
        borrower_id=None,
        _=None,
    ):
        assert isbn == "9780000000106"
        assert location_id == legacy_id
        assert mode == "add"
        with get_db() as db:
            item_id = insert_item(
                db,
                title="New Scan",
                isbn=isbn,
                media_type="book",
                source="test",
                location_id=location_id,
            )
        return request.app.state.templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {
                "status": "added",
                "isbn": isbn,
                "title": "New Scan",
                "authors": None,
                "cover_path": None,
                "item_id": item_id,
                "source": "test",
                "media_type_label": "Book",
            },
        )

    monkeypatch.setattr(items, "scan_isbn", fake_add)

    response = editor_client.post(
        "/api/shelf-fill/scan",
        data={"isbn": "9780000000106", "location_node_id": shelf_id, "media_type": "auto"},
    )

    assert response.status_code == 200
    assert "Added and shelved" in response.text
    with get_db() as db:
        copy = db.execute(
            "SELECT c.location_id, c.position_order FROM item_copies c "
            "JOIN items i ON i.id = c.item_id WHERE i.isbn = ? AND c.is_primary = 1",
            ("9780000000106",),
        ).fetchone()
    assert copy["location_id"] == shelf_id
    assert copy["position_order"] == 1


def test_scan_script_exposes_sticky_shelf_fill_mode():
    script = Path("static/js/scan.js").read_text()

    assert "{id: 'shelf_fill', label: 'Shelf Fill'}" in script
    assert "shelf_fill_location" in script
    assert "/api/shelf-fill/scan" in script
    assert "/api/shelf-fill/place" in script
