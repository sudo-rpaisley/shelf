from datetime import date

import pytest

from app.services import user_state


def _item(db, title="Personal State Book"):
    return db.execute(
        "INSERT INTO items (title, media_type, source) VALUES (?, 'book', 'test')",
        (title,),
    ).lastrowid


def test_personal_state_is_isolated_between_users(db, admin_user, viewer_user):
    item_id = _item(db)

    saved = user_state.save_state(
        db,
        admin_user["id"],
        item_id,
        reading_status="reading",
        rating=5,
        wishlist=1,
        favourite=1,
        personal_notes="Mine",
        progress_value=40,
        progress_total=100,
        progress_unit="pages",
    )
    other = user_state.get_state(db, viewer_user["id"], item_id)

    assert saved["persisted"] is True
    assert saved["rating"] == 5
    assert saved["wishlist"] == 1
    assert saved["favourite"] == 1
    assert saved["personal_notes"] == "Mine"
    assert saved["progress_value"] == 40
    assert saved["progress_total"] == 100

    assert other is not None
    assert other["persisted"] is False
    assert other["reading_status"] is None
    assert other["rating"] is None
    assert other["wishlist"] == 0
    assert other["favourite"] == 0
    assert other["personal_notes"] is None


def test_new_account_does_not_inherit_another_users_state(db, admin_user):
    from app.auth import hash_password

    item_id = _item(db)
    user_state.save_state(
        db,
        admin_user["id"],
        item_id,
        reading_status="read",
        wishlist=1,
        personal_notes="private",
    )

    new_user_id = db.execute(
        "INSERT INTO users (username, password, role) VALUES (?, ?, 'viewer')",
        ("later-user", hash_password("password123")),
    ).lastrowid

    state = user_state.get_state(db, new_user_id, item_id)
    assert state is not None
    assert state["persisted"] is False
    assert state["reading_status"] is None
    assert state["wishlist"] == 0
    assert state["personal_notes"] is None


def test_reading_status_dates_and_history_belong_to_acting_user(
    db, admin_user, viewer_user
):
    item_id = _item(db)
    today = date.today().isoformat()

    reading = user_state.set_reading_status(db, admin_user["id"], item_id, "reading")
    assert reading["reading_status"] == "reading"
    assert reading["date_started"] == today
    assert reading["date_finished"] is None

    finished = user_state.set_reading_status(db, admin_user["id"], item_id, "read")
    assert finished["reading_status"] == "read"
    assert finished["date_started"] == today
    assert finished["date_finished"] == today

    admin_history = user_state.get_reading_history(db, admin_user["id"], item_id)
    viewer_history = user_state.get_reading_history(db, viewer_user["id"], item_id)
    assert len(admin_history) == 1
    assert admin_history[0]["status"] == "read"
    assert admin_history[0]["source"] == "personal"
    assert viewer_history == []


def test_state_validation_rejects_invalid_personal_values(db, admin_user):
    item_id = _item(db)

    with pytest.raises(ValueError, match="Rating"):
        user_state.save_state(db, admin_user["id"], item_id, rating=6)
    with pytest.raises(ValueError, match="Wishlist"):
        user_state.save_state(db, admin_user["id"], item_id, wishlist=2)
    with pytest.raises(ValueError, match="negative"):
        user_state.save_state(db, admin_user["id"], item_id, progress_value=-1)
    with pytest.raises(ValueError, match="exceed"):
        user_state.save_state(
            db,
            admin_user["id"],
            item_id,
            progress_value=101,
            progress_total=100,
        )
    with pytest.raises(ValueError, match="Unsupported"):
        user_state.save_state(db, admin_user["id"], item_id, title="not personal")

    assert db.execute(
        "SELECT COUNT(*) FROM user_item_state WHERE user_id = ? AND item_id = ?",
        (admin_user["id"], item_id),
    ).fetchone()[0] == 0


def test_single_user_legacy_history_is_visible_without_duplicate(db, admin_user):
    item_id = _item(db)
    shared = (item_id, "read", "2026-01-01", "2026-01-05", "legacy", "2026-01-05 12:00:00")
    db.execute(
        "INSERT INTO reading_log "
        "(item_id, status, date_started, date_finished, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        shared,
    )
    db.execute(
        "INSERT INTO user_reading_log "
        "(user_id, item_id, status, date_started, date_finished, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (admin_user["id"], *shared),
    )

    history = user_state.get_reading_history(db, admin_user["id"], item_id)
    assert len(history) == 1
    assert history[0]["source"] == "personal"

    db.execute(
        "INSERT INTO reading_log "
        "(item_id, status, date_started, date_finished, notes) "
        "VALUES (?, 'read', '2026-02-01', '2026-02-03', 'later legacy')",
        (item_id,),
    )
    history = user_state.get_reading_history(db, admin_user["id"], item_id)
    assert len(history) == 2
    assert {row["source"] for row in history} == {"personal", "legacy"}


def test_legacy_shared_history_is_hidden_once_second_user_exists(
    db, admin_user, viewer_user
):
    item_id = _item(db)
    db.execute(
        "INSERT INTO reading_log (item_id, status, date_finished) VALUES (?, 'read', '2026-02-03')",
        (item_id,),
    )

    assert user_state.get_reading_history(db, admin_user["id"], item_id) == []
    assert user_state.get_reading_history(db, viewer_user["id"], item_id) == []


def test_user_state_schema_and_migrations_are_centrally_installed(db):
    tables = {
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"user_item_state", "user_reading_log"} <= tables

    versions = {
        row["version"]
        for row in db.execute(
            "SELECT version FROM schema_version WHERE version BETWEEN 32 AND 40"
        ).fetchall()
    }
    assert versions == set(range(32, 41))


def test_status_labels_follow_media_family():
    assert user_state.status_labels("book")["read"] == "Read"
    assert user_state.status_labels("audiobook")["read"] == "Listened"
    assert user_state.status_labels("dvd")["read"] == "Watched"
    assert user_state.status_labels("video_game")["read"] == "Completed"
