"""Merge promotion is not acquisition; private provider links are account-owned."""

import pytest

from tests.conftest import _insert_item


def _links(db):
    return [tuple(row) for row in db.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'hardcover_user_book:%' ORDER BY key"
    )]


def test_http_owned_promotion_preserves_intentional_zero_copy_archive(db, admin_client):
    keep = _insert_item(db, title="Unowned keeper", isbn=None, owned=0)
    source = _insert_item(db, title="Owned archive", isbn=None, owned=1)
    assert db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 0
    db.commit()
    result = admin_client.post("/api/items/merge", json={"keep_id": keep, "merge_ids": [source]})
    assert result.status_code == 200
    assert result.json() == {"ok": True, "merged": 1}
    assert db.execute("SELECT owned FROM items WHERE id = ?", (keep,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 0
    assert db.execute("SELECT id FROM items WHERE id = ?", (source,)).fetchone() is None


@pytest.mark.parametrize("shared_link", [False, True])
def test_http_merge_preserves_each_accounts_private_hardcover_link(db, admin_client, shared_link):
    keep = _insert_item(db, title="Link keeper", isbn=None)
    source = _insert_item(db, title="Link source", isbn=None)
    db.execute("INSERT INTO settings (key, value) VALUES (?, '7001')", (f"hardcover_user_book:101:{source}",))
    db.execute("INSERT INTO settings (key, value) VALUES (?, '9001')", (f"hardcover_user_book:202:{keep}",))
    if shared_link:
        db.execute("INSERT INTO settings (key, value) VALUES (?, '7001')", (f"hardcover_user_book:101:{keep}",))
    db.commit()
    response = admin_client.post("/api/items/merge", json={"keep_id": keep, "merge_ids": [source]})
    assert response.json() == {"ok": True, "merged": 1}
    assert _links(db) == sorted([
        (f"hardcover_user_book:101:{keep}", "7001"),
        (f"hardcover_user_book:202:{keep}", "9001"),
    ])
    assert "7001" not in response.text and "9001" not in response.text


def test_late_private_provider_conflict_rolls_back_the_entire_http_merge(db, admin_client):
    keep = _insert_item(db, title="Merge keeper", isbn=None)
    first = _insert_item(db, title="First source", isbn=None)
    last = _insert_item(db, title="Final source", isbn=None)
    for item, value in ((first, "PRIVATE-FIRST"), (last, "PRIVATE-LAST")):
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)",
                   (f"hardcover_user_book:101:{item}", value))
    db.commit()
    before = _links(db)
    rows = [tuple(row) for row in db.execute("SELECT * FROM items ORDER BY id")]
    response = admin_client.post("/api/items/merge", json={"keep_id": keep, "merge_ids": [first, last]})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["merged"] == 0
    assert "PRIVATE-FIRST" not in response.text and "PRIVATE-LAST" not in response.text
    assert _links(db) == before
    assert [tuple(row) for row in db.execute("SELECT * FROM items ORDER BY id")] == rows


def test_http_merge_promotion_keeps_retained_primary_node_and_order_through_metadata_edit(db, admin_client):
    from app.services import item_copies, item_write
    from tests.conftest import _insert_location, _insert_node_location, _legacy_location

    old_flat = _insert_location(db, "Unowned keeper's old legacy location")
    source_node = _insert_node_location(db, "Retained source NODE")
    source_flat = _legacy_location(db, source_node)
    assert source_node != source_flat and source_flat != old_flat
    keep = item_write.insert_item(db, title="Unowned placed keeper", isbn=None,
                                  owned=0, location_id=old_flat)
    source = item_write.insert_item(db, title="Owned located source", isbn=None,
                                    owned=1, location_id=source_flat)
    copy_id = db.execute("SELECT id FROM copies_live WHERE item_id=?", (source,)).fetchone()[0]
    item_copies.update_copy(db, copy_id, {"location_id": source_node, "position_order": 17,
                                         "provenance": "Original source copy"})
    db.commit()
    result = admin_client.post("/api/items/merge", json={"keep_id": keep, "merge_ids": [source]})
    assert result.status_code == 200 and result.json() == {"ok": True, "merged": 1}
    assert tuple(db.execute("SELECT id,item_id,location_id,position_order,provenance FROM copies_live").fetchone()) == (
        copy_id, keep, source_node, 17, "Original source copy",
    )
    assert db.execute("SELECT location_id FROM items WHERE id=?", (keep,)).fetchone()[0] == source_flat
    edited = admin_client.post(f"/api/items/{keep}", data={"publisher": "Metadata only"}, follow_redirects=False)
    assert edited.status_code == 303
    assert tuple(db.execute("SELECT id,item_id,location_id,position_order,provenance FROM copies_live").fetchone()) == (
        copy_id, keep, source_node, 17, "Original source copy",
    )
    assert db.execute("SELECT location_id FROM items WHERE id=?", (keep,)).fetchone()[0] == source_flat
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
