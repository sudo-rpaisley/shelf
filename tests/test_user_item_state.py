from app.auth import hash_password
from app.services import user_state
from tests.conftest import _insert_item


def _second_user(db, username="other"):
    cur = db.execute(
        "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, 'viewer')",
        (username, hash_password("password123"), username.title()),
    )
    return cur.lastrowid


def _raw_item(db, **values):
    fields = {
        "title": "Legacy Book",
        "isbn": "9780000000999",
        "media_type": "book",
        "source": "test",
    }
    fields.update(values)
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    return db.execute(
        f"INSERT INTO items ({columns}) VALUES ({placeholders})",
        list(fields.values()),
    ).lastrowid


def _run_snapshot(db):
    # The general test harness mirrors legacy inserts into personal tables so
    # old tests model an upgraded database. These tests exercise migrations
    # 55-56 themselves, so clear that harness-only mirror first.
    db.execute("DELETE FROM user_item_state")
    db.execute("DELETE FROM user_reading_log")
    for version in (55, 56):
        sql = next(sql for number, _description, sql in user_state._USER_STATE_MIGRATIONS if number == version)
        db.execute(sql)


def test_users_created_after_upgrade_do_not_inherit_shared_state(db, viewer_user):
    """A post-upgrade account starts clean even if legacy columns later change."""
    item_id = _raw_item(
        db,
        reading_status="reading",
        date_started="2026-08-01",
        owned=0,
    )

    state = user_state.get_state(db, viewer_user["id"], item_id)

    assert state["persisted"] is False
    assert state["reading_status"] is None
    assert state["date_started"] is None
    assert state["wishlist"] == 0


def test_snapshot_copies_legacy_state_for_users_present_at_upgrade(db, viewer_user):
    item_id = _raw_item(
        db,
        reading_status="read",
        date_started="2025-01-01",
        date_finished="2025-01-10",
        owned=0,
    )
    db.execute(
        "INSERT INTO reading_log (item_id, status, date_started, date_finished) "
        "VALUES (?, 'read', ?, ?)",
        (item_id, "2025-01-01", "2025-01-10"),
    )

    _run_snapshot(db)

    state = user_state.get_state(db, viewer_user["id"], item_id)
    history = user_state.get_reading_history(db, viewer_user["id"], item_id)
    assert state["persisted"] is True
    assert state["reading_status"] == "read"
    assert state["date_started"] == "2025-01-01"
    assert state["date_finished"] == "2025-01-10"
    assert state["wishlist"] == 1
    assert len(history) == 1
    assert history[0]["date_finished"] == "2025-01-10"


def test_user_created_after_snapshot_gets_no_copied_history(db, viewer_user):
    item_id = _raw_item(
        db,
        reading_status="read",
        date_started="2025-01-01",
        date_finished="2025-01-10",
    )
    db.execute(
        "INSERT INTO reading_log (item_id, status, date_started, date_finished) "
        "VALUES (?, 'read', ?, ?)",
        (item_id, "2025-01-01", "2025-01-10"),
    )
    _run_snapshot(db)

    later_user_id = _second_user(db, "later")

    assert user_state.get_state(db, later_user_id, item_id)["persisted"] is False
    assert user_state.get_state(db, later_user_id, item_id)["reading_status"] is None
    assert user_state.get_reading_history(db, later_user_id, item_id) == []


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
    item_id = _insert_item(db)
    other_id = _second_user(db)

    user_state.set_reading_status(db, viewer_user["id"], item_id, "reading")
    user_state.set_reading_status(db, viewer_user["id"], item_id, "read")

    first_history = user_state.get_reading_history(db, viewer_user["id"], item_id)
    other_history = user_state.get_reading_history(db, other_id, item_id)

    assert len(first_history) == 1
    assert first_history[0]["source"] == "personal"
    assert other_history == []


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
    # The HTTP request uses a separate SQLite connection, so publish the
    # fixture row before crossing that boundary.
    db.commit()

    response = viewer_client.get(f"/api/items/{item_id}/personal-state")

    assert response.status_code == 200
    assert "Watching Status" in response.text
    assert "Want to Watch" in response.text
    assert "Add to my wishlist" in response.text


def test_viewer_can_update_personal_state(viewer_client, viewer_user, db):
    item_id = _insert_item(db)
    # The route opens its own connection; commit the fixture first so it can
    # see the catalogue item and persist the user's state against it.
    db.commit()

    response = viewer_client.post(
        f"/api/items/{item_id}/personal-state",
        data={"rating": "4", "favourite": "1", "personal_notes": "Personal note"},
    )

    assert response.status_code == 200
    state = user_state.get_state(db, viewer_user["id"], item_id)
    assert state["rating"] == 4
    assert state["favourite"] == 1
    assert state["personal_notes"] == "Personal note"
