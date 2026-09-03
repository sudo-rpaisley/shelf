"""Browse naming, global nav search, and curated Collections."""


def _item(db, title, isbn):
    cur = db.execute(
        "INSERT INTO items (title, isbn, media_type, source) VALUES (?, ?, 'book', 'test')",
        (title, isbn),
    )
    return cur.lastrowid


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


def test_collections_is_a_primary_navigation_destination(admin_client):
    html = admin_client.get("/collections").text
    assert "<title>Collections — Shelf</title>" in html
    assert 'data-nav-tab="collections"' in html
    assert "Collections are separate from Series and Tags" in html


def test_navigation_search_redirects_to_browse(admin_client):
    response = admin_client.get(
        "/search", params={"query": "The Left Hand of Darkness"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/browse?q=The+Left+Hand+of+Darkness"


def test_navigation_search_bar_is_rendered_on_desktop_and_mobile(admin_client):
    html = admin_client.get("/collections").text
    assert 'data-testid="nav-search-form"' in html
    assert 'data-testid="nav-search-form-mobile"' in html
    assert html.count('name="query"') == 2
    # The global search deliberately does not use name=q, so Browse's HTMX
    # filter registry never double-sends its live search value.
    assert html.count('name="q"') == 0


def test_editor_can_create_and_update_a_collection(editor_client, db):
    response = editor_client.post(
        "/api/collections",
        data={"name": "  Favourite   Science Fiction  ", "description": "Top shelf picks"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = db.execute("SELECT * FROM collections").fetchone()
    assert row["name"] == "Favourite Science Fiction"
    assert row["description"] == "Top shelf picks"

    response = editor_client.post(
        f"/api/collections/{row['id']}",
        data={"name": "SF Favourites", "description": "Best of the best"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    updated = db.execute("SELECT * FROM collections WHERE id = ?", (row["id"],)).fetchone()
    assert updated["name"] == "SF Favourites"
    assert updated["description"] == "Best of the best"


def test_collection_names_are_case_insensitively_unique(editor_client):
    assert editor_client.post(
        "/api/collections", data={"name": "Favourites", "description": ""}
    ).status_code == 200
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

    html = admin_client.get("/browse", params={"collection": "Desert worlds"}).text
    assert "Dune" in html
    assert "Neuromancer" not in html
    assert 'name="collection" value="Desert worlds"' in html


def test_collection_cards_show_counts_and_link_into_browse(admin_client, db):
    item_id = _item(db, "Piranesi", "9781526622433")
    cid = _collection(db, "Short favourites", "Books I would reread")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (cid, item_id),
    )
    db.commit()

    html = admin_client.get("/collections").text
    assert "Short favourites" in html
    assert "1 item" in html
    assert "Books I would reread" in html
    assert "/browse?collection=Short%20favourites" in html


def test_deleting_collection_keeps_items_and_cascades_membership(editor_client, db):
    item_id = _item(db, "Solaris", "9780156027601")
    cid = _collection(db, "Temporary")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (cid, item_id),
    )
    db.commit()

    response = editor_client.delete(f"/api/collections/{cid}")
    assert response.status_code == 200
    assert db.execute("SELECT 1 FROM collections WHERE id = ?", (cid,)).fetchone() is None
    assert db.execute(
        "SELECT 1 FROM collection_items WHERE collection_id = ?", (cid,)
    ).fetchone() is None
    assert db.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()["title"] == "Solaris"
