"""Browse naming, global nav search, and curated Collections."""

from tests.conftest import _insert_item


def _item(db, title, isbn):
    return _insert_item(db, title=title, isbn=isbn, media_type="book")


def _collection(db, name, description=None):
    cur = db.execute(
        "INSERT INTO collections (name, description) VALUES (?, ?)",
        (name, description),
    )
    return cur.lastrowid


def test_collection_tables_exist_on_fresh_database(db):
    names = {
        row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "collections" in names
    assert "collection_items" in names


def test_collection_page_was_renamed_to_browse(admin_client):
    html = admin_client.get("/browse").text
    assert "<title>Browse — Shelf</title>" in html
    assert ">Browse <span id=\"collection-count\"" in html
    assert 'data-nav-tab="browse"' in html
    assert "Browse" in html


def test_global_search_redirects_to_browse(viewer_client):
    response = viewer_client.get("/search?query=Discworld", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/browse?q=Discworld"


def test_collections_nav_and_page_exist(viewer_client, db):
    _collection(db, "Favourites", "Things worth revisiting")
    db.commit()
    html = viewer_client.get("/collections").text
    assert "<title>Collections — Shelf</title>" in html
    assert "Favourites" in html
    assert "Things worth revisiting" in html
    assert 'data-nav-tab="collections"' in html


def test_editor_can_create_update_and_delete_collection(editor_client, db):
    response = editor_client.post(
        "/api/collections",
        data={"name": "Favourites", "description": "Initial"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = db.execute("SELECT * FROM collections WHERE name = 'Favourites'").fetchone()
    assert row and row["description"] == "Initial"

    response = editor_client.post(
        f"/api/collections/{row['id']}",
        data={"name": "Keepers", "description": "Updated"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = db.execute("SELECT * FROM collections WHERE id = ?", (row["id"],)).fetchone()
    assert row["name"] == "Keepers"
    assert row["description"] == "Updated"

    response = editor_client.delete(f"/api/collections/{row['id']}")
    assert response.status_code == 200
    assert db.execute("SELECT 1 FROM collections WHERE id = ?", (row["id"],)).fetchone() is None


def test_duplicate_collection_name_is_rejected(editor_client, db):
    _collection(db, "Favourites")
    db.commit()
    response = editor_client.post(
        "/api/collections", data={"name": "favourites", "description": ""},
        follow_redirects=False,
    )
    assert response.status_code == 409


def test_viewer_can_browse_but_cannot_change_collections(viewer_client, db):
    cid = _collection(db, "Shared list")
    db.commit()
    assert viewer_client.get("/collections").status_code == 200
    assert viewer_client.post(
        "/api/collections", data={"name": "Nope", "description": ""}
    ).status_code == 403
    assert viewer_client.delete(f"/api/collections/{cid}").status_code == 403


def test_item_membership_round_trip_and_detail_fragment(editor_client, db):
    item_id = _item(db, "Dune", "9780441172719")
    collection_id = _collection(db, "Desert worlds")
    db.commit()

    response = editor_client.post(
        f"/api/items/{item_id}/collections",
        data={"collection_id": collection_id},
    )
    assert response.status_code == 200
    assert "Desert worlds" in response.text
    membership = db.execute(
        "SELECT 1 FROM collection_items WHERE collection_id = ? AND item_id = ?",
        (collection_id, item_id),
    ).fetchone()
    assert membership

    detail = editor_client.get(f"/item/{item_id}").text
    assert 'data-testid="item-collections"' in detail
    assert "Desert worlds" in detail
    assert "/browse?collection=Desert%20worlds" in detail

    response = editor_client.delete(
        f"/api/items/{item_id}/collections/{collection_id}"
    )
    assert response.status_code == 200
    assert "This item is not in a collection" in response.text
    assert db.execute(
        "SELECT 1 FROM collection_items WHERE collection_id = ? AND item_id = ?",
        (collection_id, item_id),
    ).fetchone() is None


def test_browse_collection_filter_only_returns_members(admin_client, db):
    dune = _item(db, "Dune", "9780441172719")
    _item(db, "Neuromancer", "9780441569595")
    cid = _collection(db, "Desert worlds")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (cid, dune),
    )
    db.commit()

    html = admin_client.get("/browse?collection=Desert%20worlds").text
    assert "Dune" in html
    assert "Neuromancer" not in html


def test_collection_cards_show_member_count_and_preview(viewer_client, db):
    dune = _item(db, "Dune", "9780441172719")
    neuromancer = _item(db, "Neuromancer", "9780441569595")
    cid = _collection(db, "Classics")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (cid, dune),
    )
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (cid, neuromancer),
    )
    db.commit()

    html = viewer_client.get("/collections").text
    assert "2 items" in html
    assert "Dune" in html
    assert "Neuromancer" in html


def test_item_collection_fragment_lists_available_collections(editor_client, db):
    item_id = _item(db, "Dune", "9780441172719")
    _collection(db, "Desert worlds")
    _collection(db, "Classics")
    db.commit()

    html = editor_client.get(f"/item/{item_id}").text
    assert "Desert worlds" in html
    assert "Classics" in html


def test_collection_delete_keeps_items(editor_client, db):
    item_id = _item(db, "Dune", "9780441172719")
    cid = _collection(db, "Temporary")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (cid, item_id),
    )
    db.commit()

    response = editor_client.delete(f"/api/collections/{cid}")
    assert response.status_code == 200
    assert db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()


def test_collection_name_validation(editor_client):
    response = editor_client.post(
        "/api/collections", data={"name": "   ", "description": ""},
        follow_redirects=False,
    )
    assert response.status_code == 400
