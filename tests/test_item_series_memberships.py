from tests.conftest import _insert_item


def test_series_rows_backfill_existing_items_and_stay_in_media_family(viewer_client, db):
    first = _insert_item(
        db,
        title="Saga One",
        isbn="9780000000101",
        media_type="book",
        series_name="The Saga",
        series_position=1,
    )
    second = _insert_item(
        db,
        title="Saga Two",
        isbn="9780000000102",
        media_type="ebook",
        series_name="The Saga",
        series_position=2,
    )
    _insert_item(
        db,
        title="Saga Audio",
        isbn="9780000000103",
        media_type="audiobook",
        series_name="The Saga",
        series_position=3,
    )
    db.commit()

    response = viewer_client.get(f"/api/items/{second}/series-rows")

    assert response.status_code == 200
    assert 'data-testid="item-series-row"' in response.text
    assert "The Saga" in response.text
    assert "Saga One" in response.text
    assert "Saga Two" in response.text
    assert "Saga Audio" not in response.text
    assert response.text.index("Saga One") < response.text.index("Saga Two")
    assert 'aria-current="page"' in response.text

    rows = db.execute(
        "SELECT item_id, series_name, position, is_primary FROM item_series "
        "WHERE item_id IN (?, ?) ORDER BY item_id",
        (first, second),
    ).fetchall()
    assert len(rows) == 2
    assert all(row["series_name"] == "The Saga" for row in rows)
    assert all(row["is_primary"] == 1 for row in rows)


def test_single_item_series_has_no_navigation_row_but_can_be_managed(editor_client, db):
    item_id = _insert_item(
        db,
        title="Only Book",
        isbn="9780000000201",
        media_type="book",
        series_name="One Book Series",
        series_position=1,
    )
    db.commit()

    response = editor_client.get(f"/api/items/{item_id}/series-rows")

    assert response.status_code == 200
    assert 'data-testid="item-series-row"' not in response.text
    assert "Manage series" in response.text
    assert "One Book Series" in response.text


def test_item_can_belong_to_multiple_series_and_primary_is_promoted(editor_client, db):
    current = _insert_item(
        db,
        title="Crossing Book",
        isbn="9780000000301",
        media_type="book",
        series_name="Main Sequence",
        series_position=2,
    )
    _insert_item(
        db,
        title="Main One",
        isbn="9780000000302",
        media_type="book",
        series_name="Main Sequence",
        series_position=1,
    )
    _insert_item(
        db,
        title="World One",
        isbn="9780000000303",
        media_type="book",
        series_name="Shared World",
        series_position=1,
    )
    db.commit()

    # First GET creates/backfills the compatibility membership table.
    assert editor_client.get(f"/api/items/{current}/series-rows").status_code == 200

    add = editor_client.post(
        f"/api/items/{current}/series-memberships",
        data={"series_name": "Shared World", "position": "4"},
    )
    assert add.status_code == 200
    assert "Main Sequence" in add.text
    assert "Shared World" in add.text
    assert add.text.count('data-testid="item-series-row"') == 2

    memberships = db.execute(
        "SELECT series_name, position, is_primary FROM item_series "
        "WHERE item_id = ? ORDER BY series_name COLLATE NOCASE",
        (current,),
    ).fetchall()
    assert len(memberships) == 2
    assert {row["series_name"] for row in memberships} == {"Main Sequence", "Shared World"}
    assert sum(row["is_primary"] for row in memberships) == 1

    legacy = db.execute(
        "SELECT series_name, series_position FROM items WHERE id = ?", (current,)
    ).fetchone()
    assert legacy["series_name"] == "Main Sequence"
    assert legacy["series_position"] == 2

    remove = editor_client.post(
        f"/api/items/{current}/series-memberships/remove",
        data={"series_name": "Main Sequence"},
    )
    assert remove.status_code == 200

    legacy = db.execute(
        "SELECT series_name, series_position FROM items WHERE id = ?", (current,)
    ).fetchone()
    assert legacy["series_name"] == "Shared World"
    assert legacy["series_position"] == 4

    promoted = db.execute(
        "SELECT is_primary FROM item_series "
        "WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
        (current, "Shared World"),
    ).fetchone()
    assert promoted["is_primary"] == 1


def test_updating_primary_membership_keeps_legacy_position_in_sync(editor_client, db):
    item_id = _insert_item(
        db,
        title="Positioned Book",
        isbn="9780000000401",
        media_type="book",
        series_name="Ordered Series",
        series_position=1,
    )
    db.commit()

    assert editor_client.get(f"/api/items/{item_id}/series-rows").status_code == 200
    response = editor_client.post(
        f"/api/items/{item_id}/series-memberships",
        data={"series_name": "Ordered Series", "position": "2.5"},
    )

    assert response.status_code == 200
    legacy = db.execute(
        "SELECT series_name, series_position FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    membership = db.execute(
        "SELECT position, is_primary FROM item_series "
        "WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
        (item_id, "Ordered Series"),
    ).fetchone()
    assert legacy["series_name"] == "Ordered Series"
    assert legacy["series_position"] == 2.5
    assert membership["position"] == 2.5
    assert membership["is_primary"] == 1


def test_viewer_cannot_change_series_memberships(viewer_client, db):
    item_id = _insert_item(db, title="Viewer Book", isbn="9780000000501", media_type="book")
    db.commit()

    response = viewer_client.post(
        f"/api/items/{item_id}/series-memberships",
        data={"series_name": "Forbidden Series", "position": "1"},
    )

    assert response.status_code == 403



def test_global_series_page_includes_secondary_memberships(admin_client, db):
    book = _insert_item(
        db, title="Half-Blood Prince", isbn="9780907001106", media_type="book",
        series_name="Harry Potter", series_position=6,
    )
    _insert_item(
        db, title="Fantastic Beasts", isbn="9780907001113", media_type="book",
        series_name="Wizarding World", series_position=1,
    )
    admin_client.get("/series")
    db.execute(
        "INSERT INTO item_series (item_id, series_name, position, is_primary) "
        "VALUES (?, 'Wizarding World', 8, 0)", (book,),
    )
    db.execute("COMMIT")

    html = admin_client.get("/series").text
    assert "Harry Potter" in html
    assert "Wizarding World" in html
    assert "Half-Blood Prince" in html
    assert "Fantastic Beasts" in html


def test_global_series_rename_updates_secondary_and_primary_memberships(admin_client, db):
    primary = _insert_item(
        db, title="Primary", isbn="9780907001120", media_type="book",
        series_name="Old Name", series_position=1,
    )
    secondary = _insert_item(
        db, title="Secondary", isbn="9780907001137", media_type="book",
        series_name="Other", series_position=1,
    )
    admin_client.get("/series")
    db.execute(
        "INSERT INTO item_series (item_id, series_name, position, is_primary) "
        "VALUES (?, 'Old Name', 2, 0)", (secondary,),
    )
    db.execute("COMMIT")

    response = admin_client.post("/api/series/Old%20Name/rename", data={"new_name": "New Name"})
    assert response.status_code == 200
    rows = db.execute(
        "SELECT item_id, series_name, position, is_primary FROM item_series "
        "WHERE series_name = 'New Name' COLLATE NOCASE ORDER BY item_id"
    ).fetchall()
    assert {row["item_id"] for row in rows} == {primary, secondary}
    assert not db.execute(
        "SELECT 1 FROM item_series WHERE series_name = 'Old Name' COLLATE NOCASE"
    ).fetchone()
    legacy = db.execute(
        "SELECT series_name, series_position FROM items WHERE id = ?", (primary,)
    ).fetchone()
    assert legacy["series_name"] == "New Name"
    assert legacy["series_position"] == 1


def test_remove_all_promotes_remaining_secondary_membership(admin_client, db):
    item = _insert_item(
        db, title="Multi Series", isbn="9780907001144", media_type="book",
        series_name="Primary Series", series_position=3,
    )
    admin_client.get("/series")
    db.execute(
        "INSERT INTO item_series (item_id, series_name, position, is_primary) "
        "VALUES (?, 'Secondary Series', 7, 0)", (item,),
    )
    db.execute("COMMIT")

    response = admin_client.post("/api/series/Primary%20Series/remove-all")
    assert response.status_code == 200
    remaining = db.execute(
        "SELECT series_name, position, is_primary FROM item_series WHERE item_id = ?", (item,)
    ).fetchone()
    assert remaining["series_name"] == "Secondary Series"
    assert remaining["position"] == 7
    assert remaining["is_primary"] == 1
    legacy = db.execute(
        "SELECT series_name, series_position FROM items WHERE id = ?", (item,)
    ).fetchone()
    assert legacy["series_name"] == "Secondary Series"
    assert legacy["series_position"] == 7
