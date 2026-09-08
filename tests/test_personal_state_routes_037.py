from app.services import user_state


def _item(db, title="Shared Catalogue Book", media_type="book"):
    item_id = db.execute(
        "INSERT INTO items (title, media_type, source) VALUES (?, ?, 'test')",
        (title, media_type),
    ).lastrowid
    # Client requests use their own SQLite connection. Commit fixture writes
    # before crossing that connection boundary so the route can see the item.
    db.commit()
    return item_id


def test_personal_state_routes_are_registered_through_main():
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/api/items/{item_id}/personal-state" in paths


def test_viewer_can_render_and_update_own_personal_state(
    db, viewer_client, viewer_user
):
    item_id = _item(db)

    response = viewer_client.get(f"/api/items/{item_id}/personal-state")
    assert response.status_code == 200
    assert 'data-testid="personal-state"' in response.text
    assert "Private to your Shelf account" in response.text

    response = viewer_client.post(
        f"/api/items/{item_id}/personal-state",
        data={
            "reading_status": "reading",
            "rating": "4",
            "favourite": "1",
            "wishlist": "1",
            "personal_notes": "Mine only",
            "progress_value": "23",
            "progress_total": "100",
            "progress_unit": "pages",
        },
    )
    assert response.status_code == 200
    assert "Mine only" in response.text
    assert "4 / 5" in response.text

    state = user_state.get_state(db, viewer_user["id"], item_id)
    assert state is not None
    assert state["reading_status"] == "reading"
    assert state["rating"] == 4
    assert state["favourite"] == 1
    assert state["wishlist"] == 1
    assert state["personal_notes"] == "Mine only"
    assert state["progress_value"] == 23
    assert state["progress_total"] == 100
    assert state["progress_unit"] == "pages"

    # Personal interaction must not rewrite the shared catalogue compatibility
    # fields. Provider sync and the old shared status surface can be retired in
    # a later layer without user A changing user B's state in the meantime.
    shared = db.execute(
        "SELECT reading_status, date_started, date_finished FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    assert tuple(shared) == (None, None, None)


def test_two_users_receive_different_item_state_fragments(
    db, admin_client, admin_user, viewer_client, viewer_user
):
    item_id = _item(db)
    user_state.save_state(
        db,
        admin_user["id"],
        item_id,
        rating=5,
        personal_notes="Admin private note",
    )
    user_state.save_state(
        db,
        viewer_user["id"],
        item_id,
        rating=2,
        personal_notes="Viewer private note",
    )
    db.commit()

    admin = admin_client.get(f"/api/items/{item_id}/personal-state")
    viewer = viewer_client.get(f"/api/items/{item_id}/personal-state")

    assert admin.status_code == 200
    assert viewer.status_code == 200
    assert "Admin private note" in admin.text
    assert "Viewer private note" not in admin.text
    assert "Viewer private note" in viewer.text
    assert "Admin private note" not in viewer.text


def test_personal_state_rejects_bad_values_without_overwriting_previous_state(
    db, viewer_client, viewer_user
):
    item_id = _item(db)
    user_state.save_state(db, viewer_user["id"], item_id, rating=3)
    db.commit()

    response = viewer_client.post(
        f"/api/items/{item_id}/personal-state",
        data={"rating": "6"},
    )
    assert response.status_code == 400

    state = user_state.get_state(db, viewer_user["id"], item_id)
    assert state is not None
    assert state["rating"] == 3


def test_personal_state_missing_item_is_404(viewer_client):
    response = viewer_client.get("/api/items/999999/personal-state")
    assert response.status_code == 404

    response = viewer_client.post(
        "/api/items/999999/personal-state",
        data={"wishlist": "1"},
    )
    assert response.status_code == 404


def test_item_detail_contains_lazy_personal_state_loader(db, viewer_client):
    item_id = _item(db)
    response = viewer_client.get(f"/item/{item_id}")

    assert response.status_code == 200
    assert f'hx-get="/api/items/{item_id}/personal-state"' in response.text
    assert "Loading my activity" in response.text
