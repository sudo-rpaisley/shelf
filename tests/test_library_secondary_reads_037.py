"""Current-0.37 library visibility regressions for Series and Music."""

from unittest.mock import AsyncMock, patch

from app.services import libraries, user_state
from tests.conftest import _insert_item


def _private_library(db, name="Private"):
    return libraries.create_library(db, name)


def test_series_page_hides_inaccessible_members_and_unassigned_rows(
    db, viewer_client
):
    private = _private_library(db, "Private series")
    _insert_item(
        db,
        title="Visible Volume",
        isbn="9780000014005",
        series_name="Mixed Saga",
        series_position=1,
    )
    _insert_item(
        db,
        title="Hidden Mixed Volume",
        isbn="9780000014012",
        series_name="Mixed Saga",
        series_position=2,
        _library_id=private["id"],
    )
    _insert_item(
        db,
        title="Hidden Only Volume",
        isbn="9780000014029",
        series_name="Secret Saga",
        series_position=1,
        _library_id=private["id"],
    )
    _insert_item(db, title="Visible Loose Book", isbn="9780000014036")
    _insert_item(
        db,
        title="Hidden Loose Book",
        isbn="9780000014043",
        _library_id=private["id"],
    )
    db.commit()

    html = viewer_client.get("/series").text

    assert "Mixed Saga" in html
    assert "Visible Volume" in html
    assert "Hidden Mixed Volume" not in html
    assert "Secret Saga" not in html
    assert "Hidden Only Volume" not in html
    assert "Visible Loose Book" in html
    assert "Hidden Loose Book" not in html
    assert "1 book with no series" in html


def test_series_page_wishlist_is_personal_not_shared_unowned(
    db, viewer_client, viewer_user
):
    item_id = _insert_item(
        db,
        title="Not My Wishlist Yet",
        isbn="9780000014050",
        series_name="Personal Saga",
        series_position=1,
        owned=0,
    )
    db.commit()

    html = viewer_client.get("/series").text
    assert "Personal Saga" in html
    assert "on your wishlist" not in html

    user_state.save_state(db, viewer_user["id"], item_id, wishlist=1)
    db.commit()
    html = viewer_client.get("/series").text
    assert "1 on your wishlist" in html


def test_series_check_uses_accessible_items_and_personal_wishlist(
    db, viewer_client, viewer_user
):
    private = _private_library(db, "Private checked series")
    _insert_item(
        db,
        title="Owned Visible",
        isbn="9780000014067",
        series_name="Checked Saga",
        series_position=1,
        hardcover_book_id=201,
        owned=1,
    )
    wishlist_id = _insert_item(
        db,
        title="Wish Visible",
        isbn="9780000014074",
        series_name="Checked Saga",
        series_position=2,
        hardcover_book_id=202,
        owned=0,
    )
    _insert_item(
        db,
        title="Owned Hidden",
        isbn="9780000014081",
        series_name="Checked Saga",
        series_position=3,
        hardcover_book_id=203,
        owned=1,
        _library_id=private["id"],
    )
    user_state.save_state(db, viewer_user["id"], wishlist_id, wishlist=1)
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('hardcover_token', 'tok')"
    )
    db.commit()

    books = [
        {
            "hardcover_book_id": 201,
            "title": "Owned Visible",
            "authors": "A",
            "cover_url": None,
            "year": 2001,
            "series_position": 1,
        },
        {
            "hardcover_book_id": 202,
            "title": "Wish Visible",
            "authors": "A",
            "cover_url": None,
            "year": 2002,
            "series_position": 2,
        },
        {
            "hardcover_book_id": 203,
            "title": "Owned Hidden",
            "authors": "A",
            "cover_url": None,
            "year": 2003,
            "series_position": 3,
        },
    ]
    with patch(
        "app.services.hardcover.get_series_books",
        new=AsyncMock(return_value=books),
    ):
        data = viewer_client.get(
            "/api/series/check", params={"name": "Checked Saga"}
        ).json()

    by_id = {book["hardcover_book_id"]: book["status"] for book in data["books"]}
    assert by_id == {201: "owned", 202: "wishlist", 203: "missing"}
    assert data["missing"] == 1


def test_series_check_does_not_cache_hidden_only_local_series(
    db, viewer_client
):
    private = _private_library(db, "Hidden check cache")
    _insert_item(
        db,
        title="Hidden Local",
        isbn="9780000014098",
        series_name="Hidden Checked Saga",
        series_position=1,
        hardcover_book_id=301,
        _library_id=private["id"],
    )
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('hardcover_token', 'tok')"
    )
    db.commit()
    books = [
        {
            "hardcover_book_id": 301,
            "title": "Hidden Local",
            "authors": "A",
            "cover_url": None,
            "year": 2001,
            "series_position": 1,
        }
    ]

    with patch(
        "app.services.hardcover.get_series_books",
        new=AsyncMock(return_value=books),
    ):
        data = viewer_client.get(
            "/api/series/check", params={"name": "Hidden Checked Saga"}
        ).json()

    assert data["books"][0]["status"] == "missing"
    assert db.execute(
        "SELECT 1 FROM series_meta WHERE name = 'Hidden Checked Saga' COLLATE NOCASE"
    ).fetchone() is None


def test_music_page_hides_items_outside_accessible_libraries(
    db, viewer_client
):
    private = _private_library(db, "Private music")
    _insert_item(
        db,
        title="Visible Album",
        isbn=None,
        media_type="cd",
    )
    _insert_item(
        db,
        title="Hidden Album",
        isbn=None,
        media_type="vinyl",
        _library_id=private["id"],
    )
    db.commit()

    html = viewer_client.get("/music").text
    assert "Visible Album" in html
    assert "Hidden Album" not in html


def test_hidden_music_item_page_is_not_found_for_viewer(
    db, viewer_client, admin_client
):
    # Both clients share one TestClient fixture, so query as viewer first.
    private = _private_library(db, "Private music detail")
    hidden = _insert_item(
        db,
        title="Hidden Pressing",
        isbn=None,
        media_type="vinyl",
        _library_id=private["id"],
    )
    db.commit()

    # admin_client creation has overwritten the shared cookie; restore viewer.
    from app.auth import create_token
    row = db.execute(
        "SELECT id, username, role, display_name FROM users WHERE username = 'viewer'"
    ).fetchone()
    viewer_client.cookies.set(
        "access_token",
        create_token(row["id"], row["username"], row["role"], row["display_name"]),
    )
    assert viewer_client.get(f"/music/item/{hidden}").status_code == 404

    admin = db.execute(
        "SELECT id, username, role, display_name FROM users WHERE username = 'admin'"
    ).fetchone()
    admin_client.cookies.set(
        "access_token",
        create_token(admin["id"], admin["username"], admin["role"], admin["display_name"]),
    )
    assert admin_client.get(f"/music/item/{hidden}").status_code == 200


def test_hidden_music_refresh_is_not_found_for_editor(
    db, editor_client
):
    private = _private_library(db, "Private music refresh")
    hidden = _insert_item(
        db,
        title="Hidden Refresh Pressing",
        isbn=None,
        media_type="cd",
        _library_id=private["id"],
    )
    db.commit()

    response = editor_client.post(f"/api/music/items/{hidden}/refresh")
    assert response.status_code == 404
