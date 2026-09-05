from app.auth import hash_password
from app.services import user_state
from tests.conftest import _insert_item


def _second_user(db, username="other"):
    cur = db.execute(
        "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, 'viewer')",
        (username, hash_password("password123"), username.title()),
    )
    return cur.lastrowid


def test_legacy_state_is_inherited_until_first_personal_change(db, viewer_user):
    item_id = _insert_item(
        db,
        reading_status="reading",
        date_started="2026-08-01",
        owned=0,
    )

    state = user_state.get_state(db, viewer_user["id"], item_id)

    assert state["persisted"] is False
    assert state["reading_status"] == "reading"
    assert state["date_started"] == "2026-08-01"
    assert state["wishlist"] == 1


def test_personal_state_isolated_between_users_and_catalogue(db, viewer_user):
    item_id = _insert_item(db, reading_status=None, owned=1)
    other_id = _second_user(db)

    first = user_state.save_state(
        db,
        viewer_user["id"],
        item_id,
        rating=5,
        favourite=1,
        wishlist=1,
        personal_notes="Mine",
    )
    first = user_state.set_reading_status(db, viewer_user["id"], item_id, "read")
    second = user_state.save_state(
        db,
        other_id,
        item_id,
        rating=2,
        favourite=0,
        personal_notes="Theirs",
    )
    second = user_state.set_reading_status(db, other_id, item_id, "reading")

    assert first["reading_status"] == "read"
    assert second["reading_status"] == "reading"
    assert user_state.get_state(db, viewer_user["id"], item_id)["rating"] == 5
    assert user_state.get_state(db, other_id, item_id)["rating"] == 2
    assert user_state.get_state(db, viewer_user["id"], item_id)["personal_notes"] == "Mine"
    assert user_state.get_state(db, other_id, item_id)["personal_notes"] == "Theirs"

    shared = db.execute(
        "SELECT reading_status, date_started, date_finished, owned FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    assert shared["reading_status"] is None
    assert shared["date_started"] is None
    assert shared["date_finished"] is None
    assert shared["owned"] == 1


def test_personal_reading_history_does_not_leak_to_other_users(db, viewer_user):
    item_id = _insert_item(db, reading_status="read", date_started="2025-01-01", date_finished="2025-01-10")
    db.execute(
        "INSERT INTO reading_log (item_id, status, date_started, date_finished) VALUES (?, 'read', ?, ?)",
        (item_id, "2025-01-01", "2025-01-10"),
    )
    other_id = _second_user(db)

    user_state.set_reading_status(db, viewer_user["id"], item_id, "reading")
    user_state.set_reading_status(db, viewer_user["id"], item_id, "read")

    first_history = user_state.get_reading_history(db, viewer_user["id"], item_id)
    other_history = user_state.get_reading_history(db, other_id, item_id)

    assert [row["source"] for row in first_history].count("personal") == 1
    assert [row["source"] for row in first_history].count("legacy") == 1
    assert [row["source"] for row in other_history] == ["legacy"]


def test_progress_validation(db, viewer_user):
    item_id = _insert_item(db)

    saved = user_state.save_state(
        db,
        viewer_user["id"],
        item_id,
        progress_value=120,
        progress_total=300,
        progress_unit="pages",
    )
    assert saved["progress_value"] == 120
    assert saved["progress_total"] == 300
    assert saved["progress_unit"] == "pages"

    try:
        user_state.save_state(db, viewer_user["id"], item_id, progress_value=301)
    except ValueError as exc:
        assert "exceed" in str(exc)
    else:
        raise AssertionError("progress above total should be rejected")


def test_personal_state_fragment_uses_media_specific_labels(viewer_client, db):
    item_id = _insert_item(db, title="Film", isbn=None, media_type="dvd")

    response = viewer_client.get(f"/api/items/{item_id}/personal-state")

    assert response.status_code == 200
    assert "Watching status" in response.text
    assert "Want to Watch" in response.text
    assert "Add to my wishlist" in response.text


def test_viewer_can_update_personal_state(viewer_client, viewer_user, db):
    item_id = _insert_item(db)

    response = viewer_client.post(
        f"/api/items/{item_id}/personal-state",
        data={"rating": "4", "favourite": "1", "personal_notes": "Personal note"},
    )

    assert response.status_code == 200
    state = user_state.get_state(db, viewer_user["id"], item_id)
    assert state["rating"] == 4
    assert state["favourite"] == 1
    assert state["personal_notes"] == "Personal note"
