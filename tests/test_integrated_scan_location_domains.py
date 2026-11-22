"""Move selects flat locations; Inventory selects nodes, even for equal integers."""

from tests.conftest import _insert_item


def _domains(db):
    db.execute("INSERT INTO locations (id, name) VALUES (1, 'Legacy A'), (2, 'Legacy B')")
    db.execute("INSERT INTO location_nodes (id, name, legacy_location_id) "
               "VALUES (10, 'Node A', 1), (1, 'Node B', 2)")
    db.execute("INSERT INTO location_nodes (id, parent_id, name) VALUES (11, 10, 'Precise shelf')")


def test_inventory_node_equal_to_unrelated_flat_id_moves_the_actual_copy(db, editor_client):
    from app.services import item_copies

    _domains(db)
    item_id = _insert_item(db, title="Node inventory", location_id=1)
    copy_id = item_copies.add_copy(db, item_id, {"location_id": 10})
    db.commit()
    response = editor_client.post("/api/scan", data={
        "isbn": "9780000000026", "mode": "inventory", "location_id": "1",
    })
    assert response.status_code == 200
    assert response.context["status"] == "relocated"
    assert db.execute("SELECT location_id FROM item_copies WHERE id = ?", (copy_id,)).fetchone()[0] == 1
    assert db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()[0] == 2
    assert db.execute("SELECT legacy_location_id FROM location_nodes WHERE id = 1").fetchone()[0] == 2


def test_copy_label_move_converts_flat_selection_to_its_explicit_node(db, editor_client):
    from app.services import item_copies

    _domains(db)
    item_id = _insert_item(db, title="Label move", location_id=2)
    copy_id = item_copies.add_copy(db, item_id, {"location_id": 1, "copy_barcode": "SHF-DOMAIN-TEST"})
    db.commit()
    response = editor_client.post("/api/scan", data={
        "isbn": "SHF-DOMAIN-TEST", "mode": "move", "location_id": "1",
    })
    assert response.status_code == 200
    assert response.context["status"] == "moved"
    assert db.execute("SELECT location_id FROM item_copies WHERE id = ?", (copy_id,)).fetchone()[0] == 10
    assert db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()[0] == 1


def test_inventory_explicit_first_placement_preserves_precise_child_node(db, editor_client):
    _domains(db)
    item_id = _insert_item(db, title="First placement", location_id=None)
    assert db.execute("SELECT COUNT(*) FROM item_copies WHERE item_id = ?", (item_id,)).fetchone()[0] == 0
    db.commit()
    response = editor_client.post("/api/scan", data={
        "isbn": "9780000000026", "mode": "inventory", "location_id": "11",
    })
    assert response.status_code == 200
    assert response.context["status"] == "relocated"
    rows = db.execute("SELECT location_id, is_primary FROM item_copies WHERE item_id = ?", (item_id,)).fetchall()
    assert [tuple(row) for row in rows] == [(11, 1)]
    assert db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()[0] == 1


def test_inventory_never_recreates_a_trashed_copy_from_an_isbn(db, editor_client):
    from app.services import item_copies

    _domains(db)
    item_id = _insert_item(db, title="Historical copy", location_id=1)
    copy_id = item_copies.add_copy(db, item_id, {"location_id": 10})
    item_copies.trash_copy(db, copy_id, at="2026-01-01 00:00:00", replay=True)
    db.commit()
    before = [tuple(row) for row in db.execute("SELECT * FROM item_copies WHERE item_id = ?", (item_id,))]
    legacy_before = db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    response = editor_client.post("/api/scan", data={
        "isbn": "9780000000026", "mode": "inventory", "location_id": "11",
    })
    assert response.status_code == 200
    assert response.context["status"] == "error"
    assert [tuple(row) for row in db.execute("SELECT * FROM item_copies WHERE item_id = ?", (item_id,))] == before
    assert db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()[0] == legacy_before
