"""Cross-media My List behaviour and library-boundary regressions."""

from app.services import libraries, user_state
from tests.conftest import _insert_item


def test_my_list_is_personal_media_aware_and_library_scoped(
    db, viewer_client, viewer_user
):
    book = _insert_item(
        db,
        title="Read Next",
        isbn="9780000022001",
        media_type="book",
    )
    film = _insert_item(
        db,
        title="Watch Next",
        isbn="9780000022002",
        media_type="dvd",
    )
    audio = _insert_item(
        db,
        title="Listen Next",
        isbn="9780000022003",
        media_type="audiobook",
    )
    game = _insert_item(
        db,
        title="Play Next",
        isbn="9780000022004",
        media_type="video_game",
    )
    private = libraries.create_library(db, "Hidden My List")
    hidden = _insert_item(
        db,
        title="Secret Watchlist Title",
        isbn="9780000022005",
        media_type="dvd",
        _library_id=private["id"],
    )

    for item_id in (book, film, audio, game, hidden):
        user_state.set_reading_status(db, viewer_user["id"], item_id, "want_to_read")
    user_state.save_state(db, viewer_user["id"], book, wishlist=1)
    db.commit()

    page = viewer_client.get("/my-list")
    assert page.status_code == 200
    assert "Read Next" in page.text
    assert "Watch Next" in page.text
    assert "Listen Next" in page.text
    assert "Play Next" in page.text
    assert "Secret Watchlist Title" not in page.text
    assert "Want to Read" in page.text
    assert "Want to Watch" in page.text
    assert "Want to Listen" in page.text
    assert "Want to Play" in page.text
    assert 'href="/my-list"' in page.text

    watch = viewer_client.get("/my-list?kind=watch")
    assert watch.status_code == 200
    assert "Watch Next" in watch.text
    assert "Read Next" not in watch.text
    assert "Listen Next" not in watch.text
    assert "Play Next" not in watch.text
    assert "Secret Watchlist Title" not in watch.text

    removed = viewer_client.post(
        f"/my-list/{book}/remove",
        data={"kind": "read"},
        follow_redirects=False,
    )
    assert removed.status_code == 303
    assert removed.headers["location"] == "/my-list?kind=read"

    state = user_state.get_state(db, viewer_user["id"], book)
    assert state["reading_status"] is None
    assert state["wishlist"] == 1
