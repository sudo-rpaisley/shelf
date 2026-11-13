"""Real middleware + server-rendered forms, not mocked reading-life services.

The coordinator owns main.py/features.py/nav.py registration. Until that lands,
the fixture installs only this router temporarily on the real application;
authentication, CSRF, features, templates, database and merge remain unchanged.
This is local HTTP integration evidence, not live/browser acceptance.
"""

import inspect
import json
import sqlite3
from contextlib import contextmanager
from html.parser import HTMLParser
from urllib.parse import urlencode

import pytest

from app.services import reading_life as life
from tests.conftest import _create_user, _insert_item, _TEST_CSRF_TOKEN
from tests.test_reading_life import (
    journal_fields, make_goal, make_journal, make_session, records, session_fields, table_rows,
)


@pytest.fixture(autouse=True)
def _reading_life_router(monkeypatch, _isolated_db):
    from app.main import app
    from app.routers import reading_life

    if not any(getattr(route, "path", "") == "/reading-life" for route in app.routes):
        monkeypatch.setattr(app.router, "routes", list(app.router.routes))
        app.include_router(reading_life.router)


class Forms(HTMLParser):
    """Read actual rendered form values, including selected options/textareas."""

    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.forms, self.current, self.select, self.textarea = [], None, None, None
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {**attrs, "fields": {}, "controls": []}
            self.forms.append(self.current)
        if self.current is None:
            return
        if tag in ("input", "button", "select", "textarea"):
            self.current["controls"].append(attrs)
        if tag == "input" and attrs.get("name"):
            self.current["fields"][attrs["name"]] = attrs.get("value", "")
        if tag == "select":
            self.select = attrs.get("name")
        if tag == "option" and self.select:
            if self.select not in self.current["fields"] or "selected" in attrs:
                self.current["fields"][self.select] = attrs.get("value", "")
        if tag == "textarea":
            self.textarea = attrs.get("name")
            self.current["fields"][self.textarea] = ""

    def handle_data(self, data):
        if self.current is not None and self.textarea:
            self.current["fields"][self.textarea] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None
        if tag == "select":
            self.select = None
        if tag == "textarea":
            self.textarea = None

    def for_action(self, action):
        return next(form for form in self.forms if form.get("action") == action)


def _login(client, db, user):
    from app.auth import create_token

    version = db.execute("SELECT token_version FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
    client.cookies.set("access_token", create_token(
        user["id"], user["username"], user["role"], user["display_name"], token_version=version,
    ))


def _snapshot(db):
    return {table: table_rows(db, table) for table in ("reading_sessions", "reading_journal", "reading_goals")}


def _actions(r):
    return [
        (f"/api/reading-life/items/{r['item_id']}/sessions", session_fields()),
        (f"/api/reading-life/sessions/{r['session_id']}", session_fields(status="paused", ended_on="", version="1")),
        (f"/api/reading-life/items/{r['item_id']}/journal", journal_fields(session_id=str(r["session_id"]))),
        (f"/api/reading-life/journal/{r['entry_id']}", journal_fields(body="updated note", version="1")),
        ("/api/reading-life/goals", {"year": "2025", "activity": "play", "count_rule": "distinct_items", "target": "3"}),
        (f"/api/reading-life/goals/{r['goal_id']}", {"version": "1", "target": "5"}),
        (f"/api/reading-life/goals/{r['goal_id']}/remove", {"version": "1"}),
    ]


def _pages(r):
    return ["/reading-life?year=2025", f"/reading-life/items/{r['item_id']}",
            f"/reading-life/sessions/{r['session_id']}", f"/reading-life/journal/{r['entry_id']}"]


def test_native_forms_support_complete_repeat_session_journal_and_goal_flow(viewer_client, viewer_user, db):
    item = _insert_item(db, isbn=None, title="A usable reading life", media_type="audiobook")
    db.commit()
    response = viewer_client.get(f"/reading-life/items/{item}")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    form = Forms(response.text).for_action(f"/api/reading-life/items/{item}/sessions")
    assert form["method"] == "post" and form["fields"]["activity"] == "listen"
    assert form["fields"]["_csrf"] == _TEST_CSRF_TOKEN
    assert len(form["fields"]["submission_key"]) == 32
    response = viewer_client.post(form["action"], data={**form["fields"], "started_on": "2025-01-01"},
                                  headers={"X-CSRF-Token": ""}, follow_redirects=False)
    assert response.status_code == 303
    session_url = response.headers["Location"]
    session_id = int(session_url.rsplit("/", 1)[1])
    assert response.headers["HX-Redirect"] == session_url
    page = viewer_client.get(session_url)
    assert page.status_code == 200 and 'data-testid="reading-life-session"' in page.text
    form = Forms(page.text).for_action(f"/api/reading-life/sessions/{session_id}")
    assert form["fields"]["version"] == "1" and form["fields"]["status"] == "in_progress"
    response = viewer_client.post(form["action"], data={**form["fields"], "status": "finished", "ended_on": "2025-01-31"}, follow_redirects=False)
    assert response.status_code == 303
    page = viewer_client.get(session_url)
    assert "2025-01-31" in page.text and "Revision 2" in page.text and "Revision 1" in page.text

    form = Forms(page.text).for_action(f"/api/reading-life/items/{item}/journal")
    assert form["fields"]["session_id"] == str(session_id)
    original = "Private session reflection\nSecond line"
    response = viewer_client.post(form["action"], data={**form["fields"], "body": original, "entry_on": "2025-02-01"}, follow_redirects=False)
    assert response.status_code == 303
    journal_url = response.headers["Location"]
    page = viewer_client.get(journal_url)
    assert page.status_code == 200 and original in page.text
    form = next(f for f in Forms(page.text).forms if f.get("method") == "post")
    assert form["fields"]["body"] == original
    assert viewer_client.post(form["action"], data={**form["fields"], "body": "Corrected reflection"}, follow_redirects=False).status_code == 303
    page = viewer_client.get(journal_url)
    assert original in page.text and "Corrected reflection" in page.text and "Revision 2" in page.text

    # A second cycle is a fresh form/row; the finished cycle remains untouched.
    previous = life.session_for(db, viewer_user["id"], session_id)
    page = viewer_client.get(f"/reading-life/items/{item}")
    form = Forms(page.text).for_action(f"/api/reading-life/items/{item}/sessions")
    second = viewer_client.post(form["action"], data=form["fields"], follow_redirects=False)
    assert second.status_code == 303 and second.headers["Location"] != session_url
    assert life.session_for(db, viewer_user["id"], session_id) == previous

    page = viewer_client.get("/reading-life?year=2025")
    form = Forms(page.text).for_action("/api/reading-life/goals")
    response = viewer_client.post(form["action"], data={**form["fields"], "target": "2"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["Location"] == "/reading-life?year=2025#reading-goals"
    page = viewer_client.get(response.headers["Location"])
    assert "1 / 2 completed" in page.text and "Completed sessions (repeats count)" in page.text
    assert "Distinct catalogue items" in page.text and "different editions remain separate items" in page.text
    goal = life.goals_for(db, viewer_user["id"], 2025)[0]
    form = Forms(page.text).for_action(f"/api/reading-life/goals/{goal['id']}")
    assert viewer_client.post(form["action"], data={**form["fields"], "target": "5"}, follow_redirects=False).status_code == 303
    page = viewer_client.get("/reading-life?year=2025")
    form = Forms(page.text).for_action(f"/api/reading-life/goals/{goal['id']}/remove")
    assert form["fields"]["version"] == "2"
    before = _snapshot(db)
    assert viewer_client.post(form["action"], data=form["fields"], follow_redirects=False).status_code == 303
    assert life.goals_for(db, viewer_user["id"], 2025) == []
    assert _snapshot(db)["reading_sessions"] == before["reading_sessions"]
    assert _snapshot(db)["reading_journal"] == before["reading_journal"]


@pytest.mark.parametrize("client_name,user_name", [("viewer_client", "viewer_user"), ("editor_client", "editor_user"), ("admin_client", "admin_user")])
def test_every_role_can_manage_only_its_own_reading_life(request, db, client_name, user_name):
    client, user = request.getfixturevalue(client_name), request.getfixturevalue(user_name)
    item = _insert_item(db, isbn=None)
    db.commit()
    response = client.post(f"/api/reading-life/items/{item}/sessions?user_id=999999", data={
        **session_fields(), "user_id": "999999", "item_id": "999999", "history_json": "[]", "version": "999",
    }, follow_redirects=False)
    assert response.status_code == 303
    sid = int(response.headers["Location"].rsplit("/", 1)[1])
    assert life.session_for(db, user["id"], sid)["version"] == 1
    assert client.get(response.headers["Location"]).status_code == 200
    note = client.post(f"/api/reading-life/items/{item}/journal", data={
        **journal_fields(session_id=str(sid)), "user_id": "999999",
    }, follow_redirects=False)
    assert note.status_code == 303 and table_rows(db, "reading_journal")[0]["user_id"] == user["id"]
    assert client.post("/api/reading-life/goals", data={"year": "2025", "activity": "all", "count_rule": "sessions", "target": "1", "user_id": "999999"}, follow_redirects=False).status_code == 303
    assert table_rows(db, "reading_goals")[0]["user_id"] == user["id"]


def test_all_route_dependencies_resolve_viewer_role_before_feature():
    from app.routers.reading_life import router

    assert len(router.routes) == 11
    for route in router.routes:
        calls = [dep.call for dep in route.dependant.dependencies]
        assert calls[0].__qualname__.startswith("require_role.")
        assert inspect.getclosurevars(calls[0]).nonlocals["min_level"] == 1
        assert calls[1].feature_key == "connected_library"


def test_other_accounts_and_admin_cannot_read_modify_or_attach_private_records(viewer_client, admin_user, viewer_user, db, records):
    other = _create_user("other", "password123", "Other", "viewer")
    own_item = _insert_item(db, title="Other item", isbn=None)
    own_sid = make_session(db, other["id"], own_item)
    make_journal(db, other["id"], own_item, body="other-account-secret")
    make_goal(db, other["id"])
    before = _snapshot(db)
    for user in (other, admin_user):
        _login(viewer_client, db, user)
        response = viewer_client.get("/reading-life?year=2025&user_id=" + str(viewer_user["id"]))
        assert response.status_code == 200 and "private reading-life note" not in response.text
        item = viewer_client.get(f"/reading-life/items/{records['item_id']}?user_id={viewer_user['id']}")
        assert item.status_code == 200 and "private reading-life note" not in item.text
        for path in _pages(records)[2:]:
            response = viewer_client.get(path)
            assert response.status_code == 404 and response.headers["Cache-Control"] == "no-store"
            assert json.loads(response.headers["HX-Trigger"])["showToast"]["message"] == "Reading life record not found"
        for index in (1, 3, 5, 6):
            path, fields = _actions(records)[index]
            response = viewer_client.post(path, data={**fields, "user_id": str(viewer_user["id"])}, follow_redirects=False)
            assert response.status_code == 404 and "private reading-life note" not in response.text
        response = viewer_client.post(f"/api/reading-life/items/{records['item_id']}/journal", data=journal_fields(session_id=str(records["session_id"])), follow_redirects=False)
        assert response.status_code == 404
    _login(viewer_client, db, viewer_user)
    # Even one's own session cannot be attached to the wrong item.
    assert viewer_client.post(f"/api/reading-life/items/{own_item}/journal", data=journal_fields(session_id=str(records["session_id"])), follow_redirects=False).status_code == 404
    assert viewer_client.post(f"/api/reading-life/items/{own_item}/journal", data=journal_fields(session_id=str(own_sid)), follow_redirects=False).status_code == 404
    assert "other-account-secret" not in viewer_client.get("/reading-life?year=2025").text
    assert _snapshot(db) == before


def test_anonymous_and_bearer_outside_v1_cannot_use_any_surface(client, db, records):
    before = _snapshot(db)
    for path in _pages(records):
        response = client.get(path, headers={"Authorization": "Bearer not-a-session"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["Location"].startswith("/login")
    for path, fields in _actions(records):
        response = client.post(path, data=fields, follow_redirects=False)
        assert response.status_code == 303 and response.headers["Location"].startswith("/login")
    assert _snapshot(db) == before


def test_feature_off_refuses_every_route_and_reenable_reveals_unchanged_data(viewer_client, viewer_user, db, records):
    from app.features import set_feature_enabled

    before = _snapshot(db)
    set_feature_enabled(db, "connected_library", False)
    db.commit()
    for path in _pages(records):
        response = viewer_client.get(path)
        assert response.status_code == 200 and 'data-testid="feature-disabled"' in response.text
        assert "private reading-life note" not in response.text
    for path, fields in _actions(records):
        for headers in ({}, {"HX-Request": "true"}):
            response = viewer_client.post(path, data=fields, headers=headers, follow_redirects=False)
            assert response.status_code == 403
            if headers:
                assert json.loads(response.headers["HX-Trigger"])["showToast"]["type"] == "error"
            else:
                assert response.json()["error"] == "feature_disabled"
    assert _snapshot(db) == before
    set_feature_enabled(db, "connected_library", True)
    db.commit()
    assert "private reading-life note" in viewer_client.get("/reading-life?year=2025").text
    assert _snapshot(db) == before


def test_auth_csrf_and_feature_refusals_precede_invalid_path_validation(client, viewer_user, db):
    from app.features import set_feature_enabled

    path = f"/api/reading-life/sessions/{2**63}"
    assert client.post(path, data=session_fields(version="1"), follow_redirects=False).status_code == 303
    _login(client, db, viewer_user)
    csrf = client.post(path, data=session_fields(version="1"), headers={"X-CSRF-Token": ""}, follow_redirects=False)
    assert csrf.status_code == 403 and "CSRF" in csrf.text
    set_feature_enabled(db, "connected_library", False)
    db.commit()
    refused = client.post(path, data=session_fields(version="1"), follow_redirects=False)
    assert refused.status_code == 403 and refused.json()["error"] == "feature_disabled"


@pytest.mark.parametrize("action", range(7))
def test_csrf_required_for_every_mutation_and_native_token_works(viewer_client, db, records, action):
    path, fields = _actions(records)[action]
    before = _snapshot(db)
    for token in (None, "wrong"):
        supplied = {**fields, **({"_csrf": token} if token else {})}
        response = viewer_client.post(path, data=supplied, headers={"X-CSRF-Token": ""}, follow_redirects=False)
        assert response.status_code == 403 and "CSRF" in response.text
        assert _snapshot(db) == before
    response = viewer_client.post(path, data={**fields, "_csrf": _TEST_CSRF_TOKEN}, headers={"X-CSRF-Token": ""}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["Cache-Control"] == "no-store"
    assert _snapshot(db) != before


def test_gets_are_nonmutating_forms_private_and_redirects_never_use_caller_url(viewer_client, db, records):
    before = _snapshot(db)
    for path in _pages(records):
        response = viewer_client.get(path)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        forms = [f for f in Forms(response.text).forms if f.get("method") == "post"]
        assert forms and all(f["fields"]["_csrf"] == _TEST_CSRF_TOKEN for f in forms)
        assert all(f["action"].startswith("/api/reading-life/") for f in forms)
        # The pre-existing base shortcut modal has inline handlers (outside this
        # task). None of the added forms/controls may rely on those CSP-dead hooks.
        assert all(not any(name.startswith("on") for name in control)
                   for form in forms for control in form["controls"])
        assert 'hx-boost="false"' in response.text
    assert _snapshot(db) == before
    path, fields = _actions(records)[0]
    response = viewer_client.post(path + "?next=//evil.invalid", data={**fields, "next": "https://evil.invalid"}, headers={"Referer": "https://evil.invalid"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["Location"].startswith("/reading-life/sessions/")


def test_corrections_have_stale_write_refusal_preserved_input_and_visible_toast(viewer_client, viewer_user, db, records):
    sid, jid, gid = records["session_id"], records["entry_id"], records["goal_id"]
    session_url = f"/api/reading-life/sessions/{sid}"
    fields = session_fields(version="1", ended_on="2025-02-01")
    assert viewer_client.post(session_url, data=fields, follow_redirects=False).status_code == 303
    current = life.session_for(db, viewer_user["id"], sid)
    stale = viewer_client.post(session_url, data={**fields, "status": "paused", "ended_on": ""}, headers={"HX-Request": "true"})
    assert stale.status_code == 409
    assert json.loads(stale.headers["HX-Trigger"])["showToast"]["type"] == "error"
    assert Forms(stale.text).for_action(session_url)["fields"]["version"] == "1"
    assert Forms(stale.text).for_action(session_url)["fields"]["status"] == "paused"
    assert life.session_for(db, viewer_user["id"], sid) == current
    journal_url = f"/api/reading-life/journal/{jid}"
    assert viewer_client.post(journal_url, data=journal_fields(version="1", body="newest-body"), follow_redirects=False).status_code == 303
    stale = viewer_client.post(journal_url, data=journal_fields(version="1", body="unsaved-body"), follow_redirects=False)
    assert stale.status_code == 409 and 'role="alert"' in stale.text
    assert Forms(stale.text).for_action(journal_url)["fields"]["body"] == "unsaved-body"
    assert Forms(stale.text).for_action(journal_url)["fields"]["version"] == "1"
    assert life.journal_for(db, viewer_user["id"], jid)["body"] == "newest-body"
    goal_url = f"/api/reading-life/goals/{gid}"
    assert viewer_client.post(goal_url, data={"version": "1", "target": "20"}, follow_redirects=False).status_code == 303
    for path, data in ((goal_url, {"version": "1", "target": "30"}), (goal_url + "/remove", {"version": "1"})):
        stale = viewer_client.post(path, data=data, follow_redirects=False)
        assert stale.status_code == 409 and 'role="alert"' in stale.text
        assert Forms(stale.text).for_action(goal_url)["fields"]["version"] == "1"
        assert life.goal_for(db, viewer_user["id"], gid)["target"] == 20


def test_invalid_forms_keep_user_text_escape_xss_and_never_write(viewer_client, viewer_user, db, records):
    before = _snapshot(db)
    item, sid, jid, gid = records["item_id"], records["session_id"], records["entry_id"], records["goal_id"]
    bad_note = '</textarea><script>alert("private")</script>'
    cases = [
        (f"/api/reading-life/items/{item}/sessions", session_fields(ended_on="2024-01-01")),
        (f"/api/reading-life/sessions/{sid}", session_fields(version="1", started_on="2025-02-30")),
        (f"/api/reading-life/items/{item}/journal", journal_fields(session_id=str(sid), entry_on="bad-date", body=bad_note)),
        (f"/api/reading-life/journal/{jid}", journal_fields(version="1", entry_on="bad-date", body=bad_note)),
        ("/api/reading-life/goals", {"year": "2025", "activity": "read", "count_rule": "work", "target": "10"}),
        (f"/api/reading-life/goals/{gid}", {"version": "1", "target": "NaN"}),
        (f"/api/reading-life/goals/{gid}/remove", {"version": "NaN"}),
    ]
    for path, fields in cases:
        response = viewer_client.post(path, data=fields, follow_redirects=False)
        assert response.status_code == 422 and 'role="alert"' in response.text
        assert json.loads(response.headers["HX-Trigger"])["showToast"]["message"]
        assert bad_note not in response.text
        if "body" in fields:
            assert "&lt;script&gt;" in response.text
            assert Forms(response.text).for_action(path)["fields"]["body"] == bad_note
        assert _snapshot(db) == before
    # Valid XSS-looking text is stored literally and escaped in current/history/list views.
    path = f"/api/reading-life/journal/{jid}"
    assert viewer_client.post(path, data=journal_fields(version="1", body=bad_note), follow_redirects=False).status_code == 303
    for page in _pages(records):
        response = viewer_client.get(page)
        assert bad_note not in response.text
        if "sessions/" in page or "journal/" in page or page.startswith("/reading-life?") or "items/" in page:
            assert "&lt;script&gt;" in response.text


def test_repeated_fields_uploads_huge_ids_and_json_refused_without_writes(viewer_client, db, records):
    before = _snapshot(db)
    for index in range(7):
        path, fields = _actions(records)[index]
        name = "body" if index in (2, 3) else "version" if index == 6 else "target" if index >= 4 else "status"
        duplicated = urlencode([*fields.items(), (name, fields[name])])
        response = viewer_client.post(path, content=duplicated, headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
        assert response.status_code == 422
        response = viewer_client.post(path, data={k: v for k, v in fields.items() if k != name}, files={name: ("field.txt", b"file not text")}, follow_redirects=False)
        assert response.status_code == 422
        assert viewer_client.post(path, json=fields, follow_redirects=False).status_code == 415
    response = viewer_client.post(f"/api/reading-life/items/{records['item_id']}/journal", data=journal_fields(session_id="9" * 10000), follow_redirects=False)
    assert response.status_code == 422  # no int-conversion overflow in error recovery
    assert _snapshot(db) == before


def test_repeated_create_posts_are_idempotent_and_goal_collisions_require_explicit_edit(viewer_client, db, records):
    for index in (0, 2):
        path, fields = _actions(records)[index]
        first = viewer_client.post(path, data=fields, follow_redirects=False)
        before = _snapshot(db)
        second = viewer_client.post(path, data=fields, follow_redirects=False)
        assert first.status_code == second.status_code == 303
        assert first.headers["Location"] == second.headers["Location"]
        assert _snapshot(db) == before
    before = _snapshot(db)
    duplicate = viewer_client.post("/api/reading-life/goals", data={
        "year": "2025", "activity": "all", "count_rule": "sessions", "target": "999",
    }, follow_redirects=False)
    assert duplicate.status_code == 409 and "Edit its target instead" in duplicate.text
    assert Forms(duplicate.text).for_action("/api/reading-life/goals")["fields"]["target"] == "999"
    assert _snapshot(db) == before


def test_hidden_items_are_excluded_and_untouchable_until_restore(viewer_client, viewer_user, db, records):
    item, uid = records["item_id"], viewer_user["id"]
    before = _snapshot(db)
    db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (item,))
    db.commit()
    assert life.goals_for(db, uid, 2025)[0]["completed"] == 0
    for path in _pages(records)[1:]:
        assert viewer_client.get(path).status_code == 404
    for index in (0, 1, 2, 3):
        path, fields = _actions(records)[index]
        assert viewer_client.post(path, data=fields, follow_redirects=False).status_code == 404
    assert "private reading-life note" not in viewer_client.get("/reading-life?year=2025").text
    assert life.sessions_page(db, uid)["rows"] == life.journal_page(db, uid)["rows"] == []
    assert _snapshot(db) == before
    db.execute("UPDATE items SET deleted_at = NULL WHERE id = ?", (item,))
    db.commit()
    assert life.goals_for(db, uid, 2025)[0]["completed"] == 1
    assert "private reading-life note" in viewer_client.get("/reading-life?year=2025").text
    assert _snapshot(db) == before


def test_all_path_ids_and_query_pages_are_bounded_before_sqlite(viewer_client, db, records):
    before = _snapshot(db)
    for value, expected in ((0, 422), (-1, 422), (2**63, 422), (2**63 - 1, 404)):
        for path in (f"/reading-life/items/{value}", f"/reading-life/sessions/{value}", f"/reading-life/journal/{value}"):
            assert viewer_client.get(path).status_code == expected
        paths = _actions({"item_id": value, "session_id": value, "entry_id": value, "goal_id": value})
        for index in (0, 1, 2, 3, 5, 6):
            path, fields = paths[index]
            assert viewer_client.post(path, data=fields, follow_redirects=False).status_code == expected
    for suffix in ("year=0", "year=10000", "session_page=0", "journal_page=100001", "year=NaN"):
        assert viewer_client.get("/reading-life?" + suffix).status_code == 422
    assert viewer_client.get(f"/reading-life/sessions/{records['session_id']}?history_page=0").status_code == 422
    assert _snapshot(db) == before


@pytest.mark.parametrize("action", range(7))
def test_mutating_guards_read_under_write_lock_and_real_operation_succeeds(viewer_client, monkeypatch, db, records, action):
    from app import config
    from app.database import get_db
    from app.routers import reading_life as routes

    statements, probed = [], []

    class Probe:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            statements.append(sql)
            if sql.startswith("SELECT") and not probed:
                rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
                try:
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        rival.execute("BEGIN IMMEDIATE")
                    probed.append("locked")
                finally:
                    rival.close()
            return self.connection.execute(sql, parameters)

    @contextmanager
    def probed_db():
        with get_db() as connection:
            yield Probe(connection)

    monkeypatch.setattr(routes, "get_db", probed_db)
    path, fields = _actions(records)[action]
    assert viewer_client.post(path, data=fields, follow_redirects=False).status_code == 303
    assert statements[0] == "BEGIN IMMEDIATE" and probed == ["locked"]


def test_manual_personal_status_history_shared_log_and_catalogue_are_never_rewritten(viewer_client, viewer_user, db, records):
    from app.services import personal_library
    from tests.test_reading_life import _write

    item, uid = records["item_id"], viewer_user["id"]
    _write(db, personal_library.save, uid, item, personal_library.parse_form({
        "status": "paused", "progress": "43", "rating": "5", "notes": "manual note", "on_list": "1",
    }))
    db.execute("UPDATE items SET reading_status = 'reading', date_started = '2020-01-01' WHERE id = ?", (item,))
    db.execute("INSERT INTO reading_log (item_id, status, notes) VALUES (?, 'reading', 'shared legacy note')", (item,))
    db.commit()
    tables = ("items", "reading_log", "user_item_state", "user_item_events", "list_items")
    before = {t: [tuple(r) for r in db.execute(f"SELECT * FROM {t}")] for t in tables}
    for index in (0, 1, 2, 3):
        path, fields = _actions(records)[index]
        assert viewer_client.post(path, data=fields, follow_redirects=False).status_code == 303
    for table in tables:
        assert [tuple(r) for r in db.execute(f"SELECT * FROM {table}")] == before[table]
    before = _snapshot(db)
    assert viewer_client.post(f"/api/library/items/{item}/personal/reset", follow_redirects=False).status_code == 303
    assert _snapshot(db) == before  # current-state reset never removes earlier cycles/journal


def test_failed_transaction_rolls_back_a_session_written_before_exception(viewer_client, monkeypatch, db, records):
    original = life.create_session
    before = _snapshot(db)

    def interrupted(*args):
        original(*args)
        raise sqlite3.IntegrityError("forced interruption after insert")

    monkeypatch.setattr(life, "create_session", interrupted)
    path, fields = _actions(records)[0]
    with pytest.raises(sqlite3.IntegrityError, match="forced interruption"):
        viewer_client.post(path, data=fields)
    assert _snapshot(db) == before


def test_actual_backup_restore_restores_cycles_journal_revisions_and_goals(admin_client, viewer_user, db, records):
    from app.database import get_db

    uid, sid = viewer_user["id"], records["session_id"]
    make_session(db, uid, records["item_id"], activity="listen")
    # Keep a real corrected revision, not just one fresh row, through restore.
    from tests.test_reading_life import _write
    _write(db, life.update_journal, uid, records["entry_id"], {
        "version": 1, "entry_on": "2025-01-02", "body": "corrected before backup",
    })
    before = _snapshot(db)
    backup = admin_client.get("/api/settings/backup")
    assert backup.status_code == 200 and backup.content.startswith(b"SQLite format 3")
    for table in ("reading_journal", "reading_sessions", "reading_goals"):
        db.execute(f"DELETE FROM {table}")
    db.commit()
    restored = admin_client.post("/api/settings/restore", files={
        "file": ("reading-life.db", backup.content, "application/octet-stream"),
    })
    assert restored.status_code == 200 and restored.json()["ok"] is True
    assert admin_client.get("/reading-life", follow_redirects=False).status_code == 303
    with get_db() as current:
        assert _snapshot(current) == before
        assert current.execute("PRAGMA foreign_key_check").fetchall() == []
        _login(admin_client, current, viewer_user)
    page = admin_client.get(f"/reading-life/sessions/{sid}")
    assert page.status_code == 200 and "corrected before backup" in page.text
    assert "private reading-life note" in admin_client.get(f"/reading-life/journal/{records['entry_id']}").text


def test_page_pagination_and_coordinator_include_hooks_use_real_flags(viewer_client, db, records):
    from app.features import set_feature_enabled
    from app.main import app

    for number in range(life.PAGE_SIZE):
        make_journal(db, table_rows(db, "reading_sessions")[0]["user_id"], records["item_id"], body=f"paging entry {number}")
    page = viewer_client.get("/reading-life?year=2025&session_page=2")
    assert page.status_code == 200
    assert "/reading-life?year=2025&amp;session_page=2&amp;journal_page=2" in page.text
    second = viewer_client.get("/reading-life?year=2025&journal_page=2")
    assert second.status_code == 200 and "private reading-life note" in second.text
    assert "paging entry 19" not in second.text
    env = app.state.templates.env
    for fragment, path in (("reading_life_link.html", f"/reading-life/items/{records['item_id']}"),
                           ("reading_life_activity.html", "/reading-life")):
        template = env.get_template("fragments/" + fragment)
        assert f'href="{path}"' in template.render(item={"id": records["item_id"]})
        set_feature_enabled(db, "connected_library", False)
        db.commit()
        assert path not in template.render(item={"id": records["item_id"]})
        set_feature_enabled(db, "connected_library", True)
        db.commit()
