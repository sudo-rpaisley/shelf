"""Product coverage for hierarchical locations and physical shelf ordering."""

from app.services import holdings
from app.services.item_write import insert_item


def _legacy_root(db, name):
    legacy_id = db.execute("INSERT INTO locations (name) VALUES (?)", (name,)).lastrowid
    holdings.ensure_legacy_location_nodes(db)
    node = db.execute(
        "SELECT id FROM location_nodes WHERE legacy_location_id = ?", (legacy_id,)
    ).fetchone()
    return legacy_id, node["id"]


def _child(db, parent_id, name):
    holdings.install_schema(db)
    return db.execute(
        "INSERT INTO location_nodes (parent_id, name) VALUES (?, ?)",
        (parent_id, name),
    ).lastrowid


def _physical_item(db, title, legacy_location_id, **kwargs):
    fields = {
        "title": title,
        "media_type": "book",
        "source": "test",
        "owned": 1,
        "location_id": legacy_location_id,
    }
    fields.update(kwargs)
    return insert_item(db, fields)


def test_location_tree_renders_same_shelf_name_under_different_rooms(viewer_client, db):
    _, living = _legacy_root(db, "Living Room")
    _, bedroom = _legacy_root(db, "Bedroom")
    living_shelf = _child(db, living, "Shelf 1")
    bedroom_shelf = _child(db, bedroom, "Shelf 1")
    db.commit()

    response = viewer_client.get("/locations")
    assert response.status_code == 200
    html = response.text
    assert f'data-location-node="{living_shelf}"' in html
    assert f'data-location-node="{bedroom_shelf}"' in html
    assert html.count("Shelf 1") >= 2


def test_admin_can_add_nested_location_but_duplicate_sibling_is_rejected(admin_client, db):
    _, living = _legacy_root(db, "Living Room")
    db.commit()

    response = admin_client.post(
        "/api/location-tree",
        data={"name": "Shelf 1", "parent_id": living, "sort_order": 0},
        follow_redirects=False,
    )
    assert response.status_code == 303
    shelf = db.execute(
        "SELECT id FROM location_nodes WHERE parent_id = ? AND name = 'Shelf 1'",
        (living,),
    ).fetchone()
    assert shelf is not None

    duplicate = admin_client.post(
        "/api/location-tree",
        data={"name": "shelf 1", "parent_id": living, "sort_order": 0},
        follow_redirects=False,
    )
    assert duplicate.status_code == 303
    assert "error=duplicate" in duplicate.headers["location"]


def test_location_cannot_be_reparented_into_its_descendant(admin_client, db):
    _, room = _legacy_root(db, "Room")
    bookcase = _child(db, room, "Bookcase")
    shelf = _child(db, bookcase, "Shelf")
    db.commit()

    response = admin_client.post(
        f"/api/location-tree/{room}/update",
        data={"name": "Room", "parent_id": shelf, "sort_order": 0},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=cycle" in response.headers["location"]
    assert db.execute(
        "SELECT parent_id FROM location_nodes WHERE id = ?", (room,)
    ).fetchone()["parent_id"] is None


def test_drag_order_endpoint_requires_every_copy_exactly_once(editor_client, db):
    legacy, room = _legacy_root(db, "Room")
    first_item = _physical_item(db, "First", legacy)
    second_item = _physical_item(db, "Second", legacy)
    copies = db.execute(
        "SELECT id, item_id FROM item_copies WHERE item_id IN (?, ?) ORDER BY item_id",
        (first_item, second_item),
    ).fetchall()
    first_copy, second_copy = copies[0]["id"], copies[1]["id"]
    db.commit()

    response = editor_client.post(
        f"/api/location-tree/{room}/order",
        data={"copy_ids": f"{second_copy},{first_copy}"},
    )
    assert response.status_code == 200
    ordered = db.execute(
        "SELECT id, position_order FROM item_copies WHERE location_id = ? "
        "ORDER BY position_order",
        (room,),
    ).fetchall()
    assert [row["id"] for row in ordered] == [second_copy, first_copy]

    incomplete = editor_client.post(
        f"/api/location-tree/{room}/order",
        data={"copy_ids": str(first_copy)},
    )
    assert incomplete.status_code == 409


def test_auto_arrange_supports_series_order(editor_client, db):
    legacy, room = _legacy_root(db, "Bookcase")
    second = _physical_item(
        db, "Volume Two", legacy, series_name="Example", series_position=2
    )
    first = _physical_item(
        db, "Volume One", legacy, series_name="Example", series_position=1
    )
    db.commit()

    response = editor_client.post(
        f"/api/location-tree/{room}/auto-order",
        data={"sort_key": "series"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    ordered_items = db.execute(
        "SELECT c.item_id FROM item_copies c WHERE c.location_id = ? "
        "ORDER BY c.position_order",
        (room,),
    ).fetchall()
    assert [row["item_id"] for row in ordered_items] == [first, second]


def test_moving_primary_copy_to_nested_shelf_keeps_correct_legacy_room(editor_client, db):
    living_legacy, living = _legacy_root(db, "Living Room")
    bedroom_legacy, bedroom = _legacy_root(db, "Bedroom")
    bedroom_shelf = _child(db, bedroom, "Shelf 1")
    item_id = _physical_item(db, "Move Me", living_legacy)
    copy_id = db.execute(
        "SELECT id FROM item_copies WHERE item_id = ? AND is_primary = 1", (item_id,)
    ).fetchone()["id"]
    db.commit()

    response = editor_client.post(
        f"/api/location-tree/copies/{copy_id}/move",
        data={"location_id": bedroom_shelf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.execute(
        "SELECT location_id FROM item_copies WHERE id = ?", (copy_id,)
    ).fetchone()["location_id"] == bedroom_shelf
    assert db.execute(
        "SELECT location_id FROM items WHERE id = ?", (item_id,)
    ).fetchone()["location_id"] == bedroom_legacy


def test_viewer_cannot_mutate_location_tree(viewer_client, db):
    _, room = _legacy_root(db, "Room")
    db.commit()

    response = viewer_client.post(
        "/api/location-tree",
        data={"name": "Shelf", "parent_id": room},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert db.execute(
        "SELECT 1 FROM location_nodes WHERE parent_id = ?", (room,)
    ).fetchone() is None
