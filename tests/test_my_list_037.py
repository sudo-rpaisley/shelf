from app.services import user_state


def _item(db, title: str, media_type: str = "book") -> int:
    item_id = db.execute(
        "INSERT INTO items (title, media_type, source) VALUES (?, ?, 'test')",
        (title, media_type),
    ).lastrowid
    db.commit()
    return item_id


def test_my_list_routes_are_registered_through_main():
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/my-list" in paths
    assert "/my-list/{item_id}/remove" in paths


def test_my_list_is_personal_and_uses_media_aware_labels(
    db, client, admin_user, viewer_user
):
    from app.auth import create_token

    admin_book = _item(db, "Admin Book")
    admin_film = _item(db, "Admin Film", "dvd")
    viewer_game = _item(db, "Viewer Game", "video_game")

    user_state.set_reading_status(db, admin_user["id"], admin_book, "want_to_read")
    user_state.set_reading_status(db, admin_user["id"], admin_film, "want_to_read")
    user_state.set_reading_status(db, viewer_user["id"], viewer_game, "want_to_read")
    db.commit()

    admin_token = create_token(
        admin_user["id"], admin_user["username"], admin_user["role"], admin_user["display_name"]
    )
    client.cookies.set("access_token", admin_token)
    response = client.get("/my-list")

    assert response.status_code == 200
    assert "Admin Book" in response.text
    assert "Want to Read" in response.text
    assert "Admin Film" in response.text
    assert "Want to Watch" in response.text
    assert "Viewer Game" not in response.text

    watch = client.get("/my-list?kind=watch")
    assert "Admin Film" in watch.text
    assert "Admin Book" not in watch.text


def test_wishlist_and_my_list_are_independent(db, viewer_client, viewer_user):
    wanted_to_own = _item(db, "Wishlist Only")
    wanted_to_read = _item(db, "My List and Wishlist")

    user_state.save_state(db, viewer_user["id"], wanted_to_own, wishlist=1)
    user_state.save_state(
        db,
        viewer_user["id"],
        wanted_to_read,
        wishlist=1,
        reading_status="want_to_read",
    )
    db.commit()

    response = viewer_client.get("/my-list")
    assert response.status_code == 200
    assert "Wishlist Only" not in response.text
    assert "My List and Wishlist" in response.text

    removed = viewer_client.post(f"/my-list/{wanted_to_read}/remove", data={"kind": "all"})
    assert removed.status_code == 200
    state = user_state.get_state(db, viewer_user["id"], wanted_to_read)
    assert state is not None
    assert state["reading_status"] is None
    assert state["wishlist"] == 1


def test_invalid_kind_falls_back_to_all(db, viewer_client, viewer_user):
    item_id = _item(db, "Fallback Book")
    user_state.set_reading_status(db, viewer_user["id"], item_id, "want_to_read")
    db.commit()

    response = viewer_client.get("/my-list?kind=not-a-kind")
    assert response.status_code == 200
    assert "Fallback Book" in response.text


def test_remove_missing_item_is_404(viewer_client):
    response = viewer_client.post("/my-list/999999/remove", data={"kind": "all"})
    assert response.status_code == 404
