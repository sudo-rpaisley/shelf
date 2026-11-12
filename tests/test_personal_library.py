"""Account isolation, explicit plans, strict forms and lossless merge history."""

import json
import sqlite3
from contextlib import contextmanager
from html.parser import HTMLParser

import pytest

from app.services import personal_library
from tests.conftest import _create_user, _insert_item, _TEST_CSRF_TOKEN


@pytest.fixture
def item_id(db):
    item_id = _insert_item(db, isbn=None)
    db.commit()
    return item_id


@pytest.fixture
def other_user():
    return _create_user("other-viewer", "password123", "Other Viewer", "viewer")


def _login(client, user):
    from app.auth import create_token

    client.cookies.set("access_token", create_token(
        user["id"], user["username"], user["role"], user["display_name"],
    ))


def _url(item_id):
    return f"/api/library/items/{item_id}/personal"


def _save(db, user_id, item_id, **fields):
    """Service tests obey the same lock/commit contract as a request."""
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    state = personal_library.save(db, user_id, item_id, personal_library.parse_form(fields))
    db.commit()
    return state


def _events(db, user_id, item_id):
    return db.execute(
        "SELECT * FROM user_item_events WHERE user_id = ? AND item_id = ? ORDER BY id",
        (user_id, item_id),
    ).fetchall()


def test_defaults_and_context_are_nonmutating(db, viewer_user, item_id):
    state = personal_library.state_for(db, viewer_user["id"], item_id)
    assert state == {
        "user_id": viewer_user["id"], "item_id": item_id, "status": None,
        "progress": None, "rating": None, "favourite": 0, "on_list": 0,
        "notes": "", "started_at": None, "finished_at": None, "updated_at": None,
        "last_event_id": 0,
    }
    context = personal_library.personal_context(db, viewer_user["id"], item_id)
    assert context["item"]["id"] == item_id
    assert context["personal"] == state
    assert context["personal_events"] == []
    assert personal_library.personal_context(db, viewer_user["id"], 999999) is None
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0


def test_two_viewers_have_separate_state_forms_and_history(viewer_client, viewer_user, other_user, db, item_id):
    response = viewer_client.post(_url(item_id), data={
        "status": "in_progress", "progress": "37", "rating": "4",
        "notes": "first-private-note", "favourite": "1",
    }, follow_redirects=False)
    assert response.status_code == 303

    _login(viewer_client, other_user)
    response = viewer_client.get(_url(item_id))
    assert response.status_code == 200
    assert "first-private-note" not in response.text
    assert 'value="37"' not in response.text
    response = viewer_client.post(_url(item_id), data={
        "status": "planned", "progress": "8", "notes": "second-private-note", "on_list": "1",
    }, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "second-private-note" in response.text and "first-private-note" not in response.text
    assert "first-private-note" not in viewer_client.get("/activity").text

    _login(viewer_client, viewer_user)
    html = viewer_client.get(_url(item_id)).text
    assert "first-private-note" in html and "second-private-note" not in html
    assert "second-private-note" not in viewer_client.get("/activity").text
    first = personal_library.state_for(db, viewer_user["id"], item_id)
    second = personal_library.state_for(db, other_user["id"], item_id)
    assert (first["progress"], second["progress"]) == (37, 8)
    assert first["favourite"] == 1 and second["favourite"] == 0
    assert first["on_list"] == 0 and second["on_list"] == 1
    assert len(_events(db, viewer_user["id"], item_id)) == 1
    assert len(_events(db, other_user["id"], item_id)) == 1


def test_viewer_updates_personal_state_without_changing_catalogue(viewer_client, viewer_user, db):
    item_id = _insert_item(db, isbn=None, reading_status="reading", owned=0, wishlisted=True)
    db.commit()
    before = dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
    memberships = [tuple(row) for row in db.execute("SELECT * FROM list_items").fetchall()]
    legacy_history = db.execute("SELECT COUNT(*) FROM reading_log").fetchone()[0]

    response = viewer_client.post(_url(item_id), data={
        "status": "finished", "rating": "5", "favourite": "on", "on_list": "1", "notes": "Local only",
        "owned": "1", "reading_status": "read", "wishlisted": "0",
    }, follow_redirects=False)

    assert response.status_code == 303
    assert dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()) == before
    assert [tuple(row) for row in db.execute("SELECT * FROM list_items").fetchall()] == memberships
    assert db.execute("SELECT COUNT(*) FROM reading_log").fetchone()[0] == legacy_history
    state = personal_library.state_for(db, viewer_user["id"], item_id)
    assert state["status"] == "finished" and state["progress"] == 100 and state["rating"] == 5


def test_form_query_and_reset_cannot_override_authenticated_identity(viewer_client, viewer_user, other_user, db, item_id):
    _save(db, other_user["id"], item_id, notes="untouched-other", status="planned")
    response = viewer_client.post(_url(item_id) + f"?user_id={other_user['id']}", data={
        "user_id": str(other_user["id"]), "username": other_user["username"],
        "status": "paused", "notes": "own-note", "started_at": "forged", "finished_at": "forged",
    }, follow_redirects=False)
    assert response.status_code == 303
    own = personal_library.state_for(db, viewer_user["id"], item_id)
    assert own["notes"] == "own-note" and own["started_at"] is None and own["finished_at"] is None
    assert "untouched-other" not in viewer_client.get(_url(item_id) + f"?user_id={other_user['id']}").text
    response = viewer_client.post(_url(item_id) + "/reset", data={"user_id": str(other_user["id"])}, follow_redirects=False)
    assert response.status_code == 303
    assert personal_library.state_for(db, viewer_user["id"], item_id)["updated_at"] is None
    assert personal_library.state_for(db, other_user["id"], item_id)["notes"] == "untouched-other"


@pytest.mark.parametrize("field,value", [
    ("progress", "-1"), ("progress", "101"), ("progress", "1.5"),
    ("progress", "NaN"), ("progress", "nan"), ("progress", "inf"),
    ("progress", "-Infinity"), ("progress", "1e2"), ("progress", "+1"),
    ("progress", " 1 "), ("progress", "١"), ("progress", "9" * 5000),
    ("rating", "0"), ("rating", "6"), ("rating", "2.5"),
    ("rating", "NaN"), ("rating", "inf"), ("rating", "true"),
    ("status", "reading"), ("status", "unknown"), ("status", "null"),
    ("favourite", "yes-please"), ("on_list", "-1"),
    ("notes", "n" * 5001), ("notes", "before\x00after"),
])
def test_invalid_form_is_rejected_without_state_or_history(viewer_client, db, item_id, field, value):
    response = viewer_client.post(_url(item_id), data={field: value}, headers={"HX-Request": "true"})
    assert response.status_code == 422
    toast = json.loads(response.headers["HX-Trigger"])["showToast"]
    assert toast["type"] == "error" and toast["message"]
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 0


@pytest.mark.parametrize("progress,rating", [("0", "1"), ("100", "5"), ("", "")])
def test_optional_integer_boundaries_round_trip(viewer_client, viewer_user, db, item_id, progress, rating):
    response = viewer_client.post(_url(item_id), data={
        "status": "planned", "progress": progress, "rating": rating, "notes": "n" * 5000,
    }, follow_redirects=False)
    assert response.status_code == 303
    state = personal_library.state_for(db, viewer_user["id"], item_id)
    assert state["progress"] == (int(progress) if progress else None)
    assert state["rating"] == (int(rating) if rating else None)
    assert len(state["notes"]) == 5000


@pytest.mark.parametrize("status", personal_library.STATUS_LABELS)
def test_each_status_is_accepted(viewer_client, viewer_user, db, item_id, status):
    response = viewer_client.post(_url(item_id), data={"status": status}, follow_redirects=False)
    assert response.status_code == 303
    state = personal_library.state_for(db, viewer_user["id"], item_id)
    assert state["status"] == status
    assert bool(state["started_at"]) == (status == "in_progress")
    assert bool(state["finished_at"]) == (status == "finished")


def test_dates_reopen_and_idempotent_history(db, viewer_user, item_id):
    uid = viewer_user["id"]
    started = _save(db, uid, item_id, status="in_progress", progress="10")
    finished = _save(db, uid, item_id, status="finished")
    assert finished["started_at"] == started["started_at"]
    assert finished["finished_at"] and finished["progress"] == 100
    _save(db, uid, item_id, status="finished")
    assert len(_events(db, uid, item_id)) == 2
    reopened = _save(db, uid, item_id, status="in_progress", progress="20")
    assert reopened["finished_at"] is None and reopened["started_at"] == started["started_at"]


def test_only_changed_saves_advance_state_owned_recency(db, viewer_user, item_id):
    uid = viewer_user["id"]
    empty = _save(db, uid, item_id)
    assert empty["last_event_id"] == 0
    assert _events(db, uid, item_id) == []
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0

    saved = _save(db, uid, item_id, status="in_progress", progress="10")
    events = _events(db, uid, item_id)
    assert saved["last_event_id"] == events[-1]["id"] > 0
    assert personal_library.state_for(db, uid, item_id) == saved
    db.create_function("datetime", 1, lambda _value: "2099-01-01 00:00:00")
    unchanged = _save(db, uid, item_id, status="in_progress", progress="10")
    assert unchanged == saved
    assert len(_events(db, uid, item_id)) == 1

    changed = _save(db, uid, item_id, status="in_progress", progress="11")
    assert changed["last_event_id"] == _events(db, uid, item_id)[-1]["id"]
    assert changed["last_event_id"] > saved["last_event_id"]
    assert personal_library.state_for(db, uid, item_id) == changed


def test_form_cannot_supply_or_promote_last_event_id(viewer_client, viewer_user, db, item_id):
    fields = {"notes": "own note", "last_event_id": str(2**63 - 1)}
    response = viewer_client.post(_url(item_id), data=fields, follow_redirects=False)
    assert response.status_code == 303
    state = personal_library.state_for(db, viewer_user["id"], item_id)
    assert state["last_event_id"] == _events(db, viewer_user["id"], item_id)[0]["id"]
    assert state["last_event_id"] != 2**63 - 1
    assert "last_event_id" not in personal_library.parse_form(fields)
    response = viewer_client.post(_url(item_id), data={**fields, "last_event_id": "0"}, follow_redirects=False)
    assert response.status_code == 303
    assert personal_library.state_for(db, viewer_user["id"], item_id) == state
    assert len(_events(db, viewer_user["id"], item_id)) == 1


def test_blank_fields_and_unchecked_boxes_clear_values(viewer_client, viewer_user, db, item_id):
    _save(db, viewer_user["id"], item_id, status="planned", progress="4", rating="5", favourite="1", on_list="1", notes="old")
    response = viewer_client.post(_url(item_id), data={"status": "", "progress": "", "rating": "", "notes": ""}, follow_redirects=False)
    assert response.status_code == 303
    state = personal_library.state_for(db, viewer_user["id"], item_id)
    assert state["status"] is None and state["progress"] is None and state["rating"] is None
    assert state["favourite"] == 0 and state["on_list"] == 0 and state["notes"] == ""


def test_repeated_fields_files_and_json_are_not_coerced(viewer_client, db, item_id):
    repeated = viewer_client.post(_url(item_id), content="progress=1&progress=2", headers={
        "Content-Type": "application/x-www-form-urlencoded",
    })
    assert repeated.status_code == 422
    uploaded = viewer_client.post(_url(item_id), files={"notes": ("note.txt", b"not a text field")})
    assert uploaded.status_code == 422
    assert viewer_client.post(_url(item_id), json={"progress": 1.1}).status_code == 415
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0


def test_htmx_fragment_and_normal_form_have_safe_contracts(viewer_client, db, item_id):
    fragment = viewer_client.get(_url(item_id))
    assert fragment.status_code == 200
    assert 'id="personal-library"' in fragment.text and 'data-testid="personal-library"' in fragment.text
    assert 'method="post"' in fragment.text and 'name="_csrf"' in fragment.text
    assert f'value="{_TEST_CSRF_TOKEN}"' in fragment.text
    assert fragment.headers["Cache-Control"] == "no-store"
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0

    response = viewer_client.post(_url(item_id), data={"notes": "saved", "next": "//evil.invalid"},
                                  headers={"Referer": "https://evil.invalid"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["Location"] == f"/item/{item_id}"
    response = viewer_client.post(_url(item_id), data={"notes": "swapped"}, headers={"HX-Request": "true"})
    assert response.status_code == 200 and 'id="personal-library"' in response.text
    assert "swapped" in response.text and "<html" not in response.text and "Location" not in response.headers


def test_save_and_reset_share_section_drop_and_busy_controls(viewer_client, viewer_user, db, item_id):
    _save(db, viewer_user["id"], item_id, notes="both forms are visible")

    class Forms(HTMLParser):
        def __init__(self):
            super().__init__()
            self.section_ids = []
            self.forms = []
            self.controls = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "section":
                self.section_ids.append(attrs.get("id"))
            if tag == "form":
                self.forms.append((attrs, self.section_ids[-1]))
            if tag in {"input", "select", "textarea", "button"}:
                self.controls.append(attrs)

        def handle_endtag(self, tag):
            if tag == "section":
                self.section_ids.pop()

    for method, suffix, fields, expected_status in (
        ("GET", "", None, 200),
        ("POST", "", {"notes": "swapped forms"}, 200),
        ("POST", "", {"progress": "101"}, 422),
        ("POST", "/reset", None, 200),
    ):
        response = viewer_client.request(method, _url(item_id) + suffix, data=fields,
                                         headers={"HX-Request": "true"})
        assert response.status_code == expected_status
        markup = Forms()
        markup.feed(response.text)
        expected_actions = {_url(item_id)}
        if suffix != "/reset":
            expected_actions.add(_url(item_id) + "/reset")
        assert {attrs["action"] for attrs, _ in markup.forms} == expected_actions
        for attrs, section_id in markup.forms:
            assert section_id == "personal-library"
            assert attrs["hx-sync"] == "closest section:drop"
            assert attrs["hx-disabled-elt"] == (
                "#personal-library input, #personal-library select, "
                "#personal-library textarea, #personal-library button"
            )
            assert attrs["hx-target"] == "#personal-library"
            assert attrs["hx-swap"] == "outerHTML"
        assert markup.controls and all("disabled" not in attrs for attrs in markup.controls)
        assert "last_event_id" not in response.text


def test_notes_are_escaped_in_form_and_history(viewer_client, item_id):
    note = '</textarea><script>alert("private")</script>'
    response = viewer_client.post(_url(item_id), data={"notes": note}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<script>" not in response.text and "&lt;script&gt;" in response.text
    response = viewer_client.get("/activity")
    assert response.status_code == 200
    assert note not in response.text and "&lt;script&gt;" in response.text


def test_reset_clears_only_own_state_and_preserves_snapshots(viewer_client, viewer_user, other_user, db, item_id):
    uid = viewer_user["id"]
    _save(db, uid, item_id, status="in_progress", progress="42", rating="4", favourite="1", on_list="1", notes="before-reset")
    _save(db, other_user["id"], item_id, notes="other-still-there")
    response = viewer_client.post(_url(item_id) + "/reset", headers={"HX-Request": "true"})
    assert response.status_code == 200 and 'id="personal-library"' in response.text
    assert "before-reset" in response.text
    assert personal_library.state_for(db, uid, item_id)["updated_at"] is None
    assert personal_library.state_for(db, other_user["id"], item_id)["notes"] == "other-still-there"
    events = _events(db, uid, item_id)
    assert [event["event_type"] for event in events] == ["updated", "reset"]
    assert json.loads(events[-1]["snapshot_json"])["progress"] == 42
    response = viewer_client.post(_url(item_id) + "/reset", follow_redirects=False)
    assert response.status_code == 303 and response.headers["Location"] == f"/item/{item_id}"
    assert len(_events(db, uid, item_id)) == 2


@pytest.mark.parametrize("action", ["save", "reset"])
def test_csrf_is_required_and_plain_form_token_works(viewer_client, viewer_user, db, item_id, action):
    _save(db, viewer_user["id"], item_id, notes="before-csrf")
    url = _url(item_id) + ("/reset" if action == "reset" else "")
    for token in (None, "wrong"):
        fields = {"notes": "should-not-land"}
        if token is not None:
            fields["_csrf"] = token
        response = viewer_client.post(url, data=fields, headers={"X-CSRF-Token": ""}, follow_redirects=False)
        assert response.status_code == 403
        assert personal_library.state_for(db, viewer_user["id"], item_id)["notes"] == "before-csrf"
    response = viewer_client.post(url, data={"_csrf": _TEST_CSRF_TOKEN, "notes": "valid-form"},
                                  headers={"X-CSRF-Token": ""}, follow_redirects=False)
    assert response.status_code == 303
    state = personal_library.state_for(db, viewer_user["id"], item_id)
    assert state["notes"] == ("" if action == "reset" else "valid-form")


@pytest.mark.parametrize("method,path", [
    ("GET", "/activity"), ("GET", "personal"), ("POST", "personal"), ("POST", "reset"),
])
def test_feature_off_refuses_all_routes_without_losing_data(viewer_client, viewer_user, db, item_id, method, path):
    from app.database import set_setting

    _save(db, viewer_user["id"], item_id, notes="saved-while-on")
    set_setting(db, "feature.connected_library", "0")
    db.commit()
    url = path if path.startswith("/") else _url(item_id) + ("/reset" if path == "reset" else "")
    response = viewer_client.request(method, url, data={"notes": "must-not-land"} if method == "POST" else None,
                                     follow_redirects=False)
    if url == "/activity":
        assert response.status_code == 200
        assert 'data-testid="feature-disabled"' in response.text
        assert 'data-testid="activity-page"' not in response.text
    else:
        assert response.status_code == 403
    assert personal_library.state_for(db, viewer_user["id"], item_id)["notes"] == "saved-while-on"
    assert len(_events(db, viewer_user["id"], item_id)) == 1


@pytest.mark.parametrize("path", ["/activity", "/api/library/items/1/personal"])
def test_anonymous_cannot_read_personal_surfaces(client, viewer_user, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303 and response.headers["Location"].startswith("/login")


@pytest.mark.parametrize("trashed", [False, True])
def test_missing_or_trashed_item_cannot_be_read_saved_or_reset(viewer_client, viewer_user, db, item_id, trashed):
    if trashed:
        _save(db, viewer_user["id"], item_id, status="in_progress", favourite="1", on_list="1", notes="hidden-in-trash")
        db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (item_id,))
        db.commit()
    else:
        item_id = 999999
    before = [tuple(row) for row in db.execute("SELECT * FROM user_item_state").fetchall()]
    before_events = [tuple(row) for row in db.execute("SELECT * FROM user_item_events").fetchall()]
    for method, url in (("GET", _url(item_id)), ("POST", _url(item_id)), ("POST", _url(item_id) + "/reset")):
        response = viewer_client.request(method, url, data={"notes": "refused"} if method == "POST" else None)
        assert response.status_code == 404
        assert json.loads(response.headers["HX-Trigger"])["showToast"] == {"message": "Item not found", "type": "error"}
    assert personal_library.personal_context(db, viewer_user["id"], item_id) is None
    assert personal_library.state_for(db, viewer_user["id"], item_id)["updated_at"] is None
    assert "hidden-in-trash" not in viewer_client.get("/activity").text
    assert [tuple(row) for row in db.execute("SELECT * FROM user_item_state").fetchall()] == before
    assert [tuple(row) for row in db.execute("SELECT * FROM user_item_events").fetchall()] == before_events


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("POST", ""), ("POST", "/reset")])
@pytest.mark.parametrize("item_id,expected", [
    (2**63 - 1, 404), (2**63, 422), (0, 422), (-1, 422), (-(2**63) - 1, 422),
])
def test_personal_path_ids_are_bounded_before_sqlite(viewer_client, db, method, suffix, item_id, expected):
    response = viewer_client.request(
        method, _url(item_id) + suffix,
        data={"notes": "not written"} if method == "POST" else None,
        follow_redirects=False,
    )
    assert response.status_code == expected
    if expected == 422:
        assert response.json()["detail"][0]["loc"] == ["path", "item_id"]
    else:
        assert json.loads(response.headers["HX-Trigger"])["showToast"]["type"] == "error"
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 0


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("POST", ""), ("POST", "/reset")])
def test_authentication_precedes_personal_path_validation(client, viewer_user, method, suffix):
    response = client.request(
        method, _url(2**63) + suffix,
        data={"notes": "not written"} if method == "POST" else None,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["Location"].startswith("/login")


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("POST", ""), ("POST", "/reset")])
def test_feature_gate_precedes_personal_path_validation(viewer_client, db, method, suffix):
    from app.database import set_setting

    set_setting(db, "feature.connected_library", "0")
    db.commit()
    response = viewer_client.request(
        method, _url(2**63) + suffix,
        data={"notes": "not written"} if method == "POST" else None,
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert response.json()["error"] == "feature_disabled"


@pytest.mark.parametrize("suffix", ["", "/reset"])
def test_csrf_precedes_personal_path_validation(viewer_client, suffix):
    response = viewer_client.post(_url(2**63) + suffix, data={"notes": "not written"},
                                  headers={"X-CSRF-Token": ""}, follow_redirects=False)
    assert response.status_code == 403
    assert "CSRF" in response.text


@pytest.mark.parametrize("action", ["save", "reset"])
def test_live_item_guard_runs_under_write_lock(viewer_client, monkeypatch, db, item_id, action):
    from app import config
    from app.database import get_db
    from app.routers import personal_library as routes

    probed = []
    statements = []

    class Probe:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, parameters=()):
            statements.append(sql)
            if sql == "SELECT id FROM items_live WHERE id = ?":
                rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
                try:
                    rival.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    assert "locked" in str(exc)
                    probed.append("locked")
                else:
                    probed.append("acquired")
                    rival.rollback()
                finally:
                    rival.close()
            return self.connection.execute(sql, parameters)

    @contextmanager
    def probed_db():
        with get_db() as connection:
            yield Probe(connection)

    monkeypatch.setattr(routes, "get_db", probed_db)
    response = viewer_client.post(_url(item_id) + ("/reset" if action == "reset" else ""),
                                  data={"notes": "locked-save"}, follow_redirects=False)
    assert response.status_code == 303
    assert statements[0] == "BEGIN IMMEDIATE"
    assert probed == ["locked"], "the live guard must hold the lock, not merely the later write"


def test_state_and_event_are_atomic_if_event_insert_fails(viewer_client, viewer_user, monkeypatch, db, item_id):
    def fail(*args):
        raise sqlite3.IntegrityError("forced event failure")

    monkeypatch.setattr(personal_library, "_record", fail)
    with pytest.raises(sqlite3.IntegrityError, match="forced event failure"):
        viewer_client.post(_url(item_id), data={"notes": "must-roll-back"})
    assert personal_library.state_for(db, viewer_user["id"], item_id)["updated_at"] is None
    assert _events(db, viewer_user["id"], item_id) == []


def test_dashboard_is_explicit_account_scoped_and_media_neutral(viewer_client, viewer_user, other_user, db):
    ids = {
        name: _insert_item(db, title=name, isbn=None, media_type=media_type)
        for name, media_type in (("Continue Book", "book"), ("Planned DVD", "dvd"),
                                 ("Listed Album", "cd"), ("Favourite Digital", "ebook"),
                                 ("Household Only", "book"), ("Other Only", "book"), ("Trashed", "book"))
    }
    uid = viewer_user["id"]
    _save(db, uid, ids["Continue Book"], status="in_progress", progress="25")
    _save(db, uid, ids["Planned DVD"], status="planned")
    _save(db, uid, ids["Listed Album"], status="paused", on_list="1")
    _save(db, uid, ids["Favourite Digital"], favourite="1")
    _save(db, other_user["id"], ids["Other Only"], status="in_progress", favourite="1", on_list="1", notes="other-secret")
    _save(db, uid, ids["Trashed"], status="in_progress", favourite="1", on_list="1", notes="trashed-secret")
    db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (ids["Trashed"],))
    db.execute("UPDATE items SET reading_status = 'reading' WHERE id = ?", (ids["Household Only"],))
    db.commit()

    context = personal_library.dashboard(db, uid)
    assert [row["id"] for row in context["continue_items"]] == [ids["Continue Book"]]
    assert {row["id"] for row in context["up_next_items"]} == {ids["Planned DVD"], ids["Listed Album"]}
    assert [row["id"] for row in context["favourite_items"]] == [ids["Favourite Digital"]]
    assert len(context["personal_events"]) == 4
    response = viewer_client.get("/activity")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    for testid in ("activity-page", "activity-continue", "activity-up-next", "activity-favourites", "activity-history"):
        assert f'data-testid="{testid}"' in response.text
    for name in ("Continue Book", "Planned DVD", "Listed Album", "Favourite Digital"):
        assert name in response.text
    for hidden in ("Household Only", "Other Only", "other-secret", "trashed-secret"):
        assert hidden not in response.text
    assert 'data-cover-shape="square"' in response.text


def test_empty_dashboard_and_bounded_deterministic_limits(db, viewer_user):
    uid = viewer_user["id"]
    assert all(personal_library.dashboard(db, uid)[key] == [] for key in
               ("continue_items", "up_next_items", "favourite_items", "personal_events"))
    ids = []
    for number in range(3):
        item_id = _insert_item(db, title=f"Plan {number}", isbn=None)
        ids.append(item_id)
        _save(db, uid, item_id, status="planned", favourite="1")
    db.execute("UPDATE user_item_state SET updated_at = '2026-01-01 00:00:00'")
    context = personal_library.dashboard(db, uid, limit=2)
    assert [row["id"] for row in context["up_next_items"]] == ids[::-1][:2]
    assert [row["id"] for row in context["favourite_items"]] == ids[::-1][:2]
    assert len(context["personal_events"]) == 2
    assert personal_library.dashboard(db, uid, limit=-1)["up_next_items"] == []
    assert len(personal_library.dashboard(db, uid, limit=1000)["up_next_items"]) == 3


@pytest.mark.parametrize("latest", ["keep", "other", "tie"])
def test_reparent_preserves_each_users_latest_state_notes_flags_and_all_events(db, viewer_user, other_user, latest):
    keep = _insert_item(db, title="Keep", isbn=None)
    other = _insert_item(db, title="Other", isbn=None)
    uid = viewer_user["id"]
    kept = _save(db, uid, keep, status="in_progress", progress="12", rating="5", favourite="1", notes="kept note")
    moved = _save(db, uid, other, status="paused", progress="67", rating="2", on_list="1", notes="other note")
    _save(db, other_user["id"], other, status="planned", notes="other user's plan")
    db.execute("UPDATE user_item_state SET updated_at = '2026-01-01 00:00:00'")
    if latest != "tie":
        db.execute("UPDATE user_item_state SET updated_at = '2026-02-01 00:00:00' WHERE item_id = ?", (keep if latest == "keep" else other,))
    originals = [dict(row) for row in db.execute("SELECT * FROM user_item_events").fetchall()]
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    personal_library.reparent(db, keep, other)
    db.execute("DELETE FROM items WHERE id = ?", (other,))
    db.commit()

    state = personal_library.state_for(db, uid, keep)
    # Same-second edits use the state's own event identity, not combined history.
    other_wins = latest in ("other", "tie")
    assert state["status"] == ("paused" if other_wins else "in_progress")
    assert state["progress"] == (67 if other_wins else 12)
    assert state["rating"] == (2 if other_wins else 5)
    assert state["last_event_id"] == (moved if other_wins else kept)["last_event_id"]
    assert state["favourite"] == 1 and state["on_list"] == 1
    assert "kept note" in state["notes"] and "other note" in state["notes"]
    assert personal_library.state_for(db, other_user["id"], keep)["notes"] == "other user's plan"
    assert db.execute("SELECT COUNT(*) FROM user_item_state WHERE item_id = ?", (other,)).fetchone()[0] == 0
    for original in originals:
        moved = dict(db.execute("SELECT * FROM user_item_events WHERE id = ?", (original["id"],)).fetchone())
        assert moved == {**original, "item_id": keep}


def test_merge_keeps_long_notes_in_accessible_private_history(viewer_client, viewer_user, db):
    keep = _insert_item(db, title="Keep", isbn=None)
    other = _insert_item(db, title="Other", isbn=None)
    _save(db, viewer_user["id"], keep, notes="a" * 5000)
    _save(db, viewer_user["id"], other, notes="b" * 5000)
    db.execute("UPDATE user_item_state SET updated_at = '2026-01-01' WHERE item_id = ?", (keep,))
    db.execute("UPDATE user_item_state SET updated_at = '2026-02-01' WHERE item_id = ?", (other,))
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    personal_library.reparent(db, keep, other)
    db.execute("DELETE FROM items WHERE id = ?", (other,))
    db.commit()
    assert personal_library.state_for(db, viewer_user["id"], keep)["notes"] == "b" * 5000
    context = personal_library.personal_context(db, viewer_user["id"], keep)
    merged = next(event for event in context["personal_events"] if event["event_type"] == "merged")
    assert [state["notes"] for state in merged["snapshot"]["merged_states"]] == ["a" * 5000, "b" * 5000]
    response = viewer_client.get(_url(keep))
    assert response.status_code == 200 and "a" * 5000 in response.text and "b" * 5000 in response.text


def test_reparent_does_not_make_an_older_state_win_a_later_merge(db, viewer_user):
    ids = [_insert_item(db, title=str(n), isbn=None) for n in range(3)]
    for number, item_id in enumerate(ids, 1):
        _save(db, viewer_user["id"], item_id, status="in_progress", progress=str(number * 10))
        db.execute("UPDATE user_item_state SET updated_at = ? WHERE item_id = ?", (f"2026-01-0{number}", item_id))
        db.commit()
    db.execute("BEGIN IMMEDIATE")
    personal_library.reparent(db, ids[0], ids[1])
    personal_library.reparent(db, ids[0], ids[2])
    assert personal_library.state_for(db, viewer_user["id"], ids[0])["progress"] == 30
    personal_library.reparent(db, ids[0], ids[0])
    assert personal_library.state_for(db, viewer_user["id"], ids[0])["progress"] == 30


def test_reset_losers_history_cannot_promote_older_state_in_chained_same_second_merges(db, viewer_user):
    uid = viewer_user["id"]
    a, b, c = [_insert_item(db, title=name, isbn=None) for name in ("A", "B", "C")]
    fixed_now = "2026-11-12 12:00:00"
    db.create_function("datetime", 1, lambda _value: fixed_now)
    # Actual save order differs from item-id order; every state has the same timestamp.
    first = _save(db, uid, a, status="planned", progress="10", favourite="1", notes="A note")
    winner = _save(db, uid, c, status="in_progress", progress="30", rating="5", on_list="1", notes="C note")
    reset_loser = _save(db, uid, b, status="paused", progress="20", notes="B reset note")
    assert first["updated_at"] == winner["updated_at"] == reset_loser["updated_at"] == fixed_now
    assert first["last_event_id"] < winner["last_event_id"] < reset_loser["last_event_id"]
    db.execute("BEGIN IMMEDIATE")
    assert personal_library.reset(db, uid, b)
    db.commit()
    reset_event = _events(db, uid, b)[-1]
    originals = [dict(row) for row in db.execute("SELECT * FROM user_item_events ORDER BY id").fetchall()]
    assert reset_event["event_type"] == "reset"
    assert personal_library.state_for(db, uid, b)["last_event_id"] == 0

    db.execute("BEGIN IMMEDIATE")
    personal_library.reparent(db, a, b)
    assert personal_library.state_for(db, uid, a)["last_event_id"] == first["last_event_id"]
    assert len(_events(db, uid, a)) == 3
    personal_library.reparent(db, a, c)
    db.execute("DELETE FROM items WHERE id IN (?, ?)", (b, c))
    db.commit()

    state = personal_library.state_for(db, uid, a)
    assert (state["status"], state["progress"], state["rating"]) == ("in_progress", 30, 5)
    assert state["started_at"] == winner["started_at"]
    assert state["updated_at"] == fixed_now and state["last_event_id"] == winner["last_event_id"]
    assert state["favourite"] == 1 and state["on_list"] == 1
    assert "A note" in state["notes"] and "C note" in state["notes"]
    for original in originals:
        preserved = dict(db.execute("SELECT * FROM user_item_events WHERE id = ?", (original["id"],)).fetchone())
        assert preserved == {**original, "item_id": a}
    history = _events(db, uid, a)
    assert [event["event_type"] for event in history] == ["updated", "updated", "updated", "reset", "merged"]
    assert state["last_event_id"] not in (reset_loser["last_event_id"], reset_event["id"], history[-1]["id"])
    assert json.loads(reset_event["snapshot_json"])["notes"] == "B reset note"


def test_unknown_legacy_recency_keeps_kept_state_on_exact_ties(db, viewer_user):
    uid = viewer_user["id"]
    keep = _insert_item(db, title="Keep", isbn=None)
    other = _insert_item(db, title="Other", isbn=None)
    _save(db, uid, keep, status="planned", progress="10", favourite="1", notes="keep note")
    _save(db, uid, other, status="paused", progress="20", on_list="1", notes="other note")
    db.execute("UPDATE user_item_state SET last_event_id = 0, updated_at = '2026-11-12 12:00:00'")
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    personal_library.reparent(db, keep, other)
    state = personal_library.state_for(db, uid, keep)
    assert (state["status"], state["progress"], state["last_event_id"]) == ("planned", 10, 0)
    assert state["favourite"] == 1 and state["on_list"] == 1
    assert state["notes"] == "keep note\n\nother note"
