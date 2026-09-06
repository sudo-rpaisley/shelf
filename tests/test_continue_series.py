"""Personal Continue / automatic series progression regressions."""

from app.services import libraries, user_state
from tests.conftest import _insert_item


def test_continue_shows_current_activity_then_promotes_next_series_item(
    db, viewer_client, viewer_user
):
    first = _insert_item(
        db,
        title="Saga One",
        isbn="9780000023001",
        media_type="book",
        series_name="Continue Saga",
        series_position=1,
    )
    second = _insert_item(
        db,
        title="Saga Two",
        isbn="9780000023002",
        media_type="book",
        series_name="Continue Saga",
        series_position=2,
    )
    audio = _insert_item(
        db,
        title="Current Audio",
        isbn="9780000023003",
        media_type="audiobook",
    )
    user_state.set_reading_status(db, viewer_user["id"], first, "read")
    user_state.set_reading_status(db, viewer_user["id"], audio, "reading")
    user_state.save_state(
        db,
        viewer_user["id"],
        audio,
        progress_value=4,
        progress_total=10,
        progress_unit="hours",
    )
    db.commit()

    home = viewer_client.get("/")
    assert 'hx-get="/api/home/continue"' in home.text
    assert 'data-testid="continue-loading"' in home.text

    rail = viewer_client.get("/api/home/continue")
    assert rail.status_code == 200
    assert "Continue Listening" in rail.text
    assert "Current Audio" in rail.text
    assert "4 / 10 hours" in rail.text
    assert "Up Next" in rail.text
    assert "Saga Two" in rail.text
    assert "Continue Saga" in rail.text
    assert "Start Reading" in rail.text

    started = viewer_client.post(f"/api/items/{second}/continue/start")
    assert started.status_code == 200
    assert "Continue Reading" in started.text
    assert "Saga Two" in started.text
    assert user_state.get_state(db, viewer_user["id"], second)["reading_status"] == "reading"

    detail = viewer_client.get(f"/item/{second}?from=home")
    assert detail.status_code == 200
    assert "Back to Home" in detail.text


def test_continue_never_skips_hidden_or_obviously_missing_series_members(
    db, viewer_client, viewer_user
):
    hidden_library = libraries.create_library(db, "Hidden continuation")

    visible_one = _insert_item(
        db,
        title="Visible Start",
        isbn="9780000023010",
        media_type="comic",
        series_name="Protected Run",
        series_position=1,
    )
    hidden_two = _insert_item(
        db,
        title="Secret Issue Two",
        isbn="9780000023011",
        media_type="comic",
        series_name="Protected Run",
        series_position=2,
        _library_id=hidden_library["id"],
    )
    visible_three = _insert_item(
        db,
        title="Visible Issue Three",
        isbn="9780000023012",
        media_type="comic",
        series_name="Protected Run",
        series_position=3,
    )

    gap_one = _insert_item(
        db,
        title="Gap Start",
        isbn="9780000023020",
        media_type="video_game",
        series_name="Gap Games",
        series_position=1,
    )
    gap_three = _insert_item(
        db,
        title="Gap Game Three",
        isbn="9780000023021",
        media_type="video_game",
        series_name="Gap Games",
        series_position=3,
    )

    user_state.set_reading_status(db, viewer_user["id"], visible_one, "read")
    user_state.set_reading_status(db, viewer_user["id"], gap_one, "read")
    db.commit()

    rail = viewer_client.get("/api/home/continue")
    assert rail.status_code == 200
    assert "Secret Issue Two" not in rail.text
    assert "Visible Issue Three" not in rail.text
    assert "Gap Game Three" not in rail.text

    denied = viewer_client.post(f"/api/items/{hidden_two}/continue/start")
    assert denied.status_code == 404
    assert user_state.get_state(db, viewer_user["id"], hidden_two)["reading_status"] is None

    # The visible titles themselves remain ordinary accessible catalogue items;
    # they are omitted only because Continue refuses to leap a protected/gap boundary.
    assert viewer_client.get(f"/item/{visible_three}").status_code == 200
    assert viewer_client.get(f"/item/{gap_three}").status_code == 200
