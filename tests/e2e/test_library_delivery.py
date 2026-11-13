"""Integrated delivery acceptance: real routes, native forms and loopback feeds.

Each journey owns a fresh Standard install. Database helpers only seed or inspect
disposable data; no router, service or response is replaced. Layout audits reuse
the synthetic seeds, while source acceptance creates its bindings by real sync.
"""

import http.server
import json
import re
import threading
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from tests.e2e.test_connected_library import (
    _add_user,
    _browser_page,
    _choice,
    _db,
    _item,
    _login,
    _navigate,
    connected_library,
)


pytestmark = pytest.mark.e2e

_PROTECTED = (
    "items", "item_copies", "item_tags", "list_items", "item_links",
    "user_item_state", "user_item_events", "reading_log", "checkouts",
)
_READING = ("reading_sessions", "reading_journal", "reading_goals")
_DECISIONS = {
    "apply": "Apply reviewed bindings",
    "retire": "Retire old source",
    "rollback": "Roll back bindings",
}


def _snapshot(server, tables=_PROTECTED):
    with _db(server) as db:
        return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
                for table in tables}


def _csrf(page):
    return next(cookie["value"] for cookie in page.context.cookies() if cookie["name"] == "csrf_token")


def _fields(form):
    return form.evaluate("form => Object.fromEntries(new FormData(form))")


def _submit(page, form, button, *, status=200):
    """Prove a browser document POST uses its native token, not a fetch header."""
    action = form.get_attribute("action")
    expect(form).to_have_attribute("method", "post")
    expect(form.locator('input[name="_csrf"]')).to_have_value(_csrf(page))
    with page.expect_response(
        lambda response: response.request.method == "POST" and urlsplit(response.url).path == action
    ) as posted, page.expect_navigation(wait_until="domcontentloaded") as navigation:
        form.get_by_role("button", name=button, exact=True).click()
    assert posted.value.status == (303 if status == 200 else status)
    assert navigation.value.status == status
    request = posted.value.request
    assert request.is_navigation_request()
    assert "x-csrf-token" not in request.headers
    assert parse_qs(request.post_data)["_csrf"] == [_csrf(page)]
    assert navigation.value.headers["cache-control"] == "no-store"


def _account_destination(page, key):
    page.get_by_test_id("account-menu-button").click()
    destination = page.get_by_test_id(f"account-menu-{key}")
    expect(destination).to_be_visible()
    _navigate(page, destination)


def _session(page, *, started="2025-01-01", ended="", status="in_progress"):
    form = page.get_by_test_id("reading-session-form")
    form.get_by_label("Session status", exact=True).select_option(status)
    form.get_by_label("Started on", exact=True).fill(started)
    form.get_by_label("Ended on", exact=True).fill(ended)
    _submit(page, form, "Start or log a new session")
    expect(page.get_by_test_id("reading-life-session")).to_be_visible()
    return int(urlsplit(page.url).path.rsplit("/", 1)[1])


def _journal(page, body, *, entry_on="2025-02-01"):
    form = page.get_by_test_id("reading-journal-form")
    form.get_by_label("Entry date", exact=True).fill(entry_on)
    form.get_by_label("Private journal entry", exact=True).fill(body)
    _submit(page, form, "Add journal entry")
    expect(page.get_by_test_id("reading-life-journal-entry")).to_be_visible()
    return int(urlsplit(page.url).path.rsplit("/", 1)[1])


def _goal(page, *, rule="sessions", target="3"):
    form = page.get_by_test_id("reading-goal-form")
    form.get_by_label("Year", exact=True).fill("2025")
    form.get_by_label("Activity", exact=True).select_option("all")
    form.get_by_label("Counting rule", exact=True).select_option(rule)
    form.get_by_label("Target", exact=True).fill(target)
    _submit(page, form, "Add goal")
    return page.get_by_test_id("reading-goal").filter(
        has=page.get_by_role("heading", name=re.compile(
            "Completed sessions" if rule == "sessions" else "Distinct catalogue items"
        ))
    )


def _replacement_preview(page, server, ids, *, manual=True):
    page.goto(f"{server['url']}/source-migrations")
    compare = page.locator('form:has(select[name="old_source_id"])')
    compare.locator('[name="old_source_id"]').select_option(str(ids["old_source"]))
    compare.locator('[name="new_source_id"]').select_option(str(ids["new_source"]))
    _navigate(page, compare.get_by_role("button", name="Compare local snapshots", exact=True))
    return _submit_replacement_preview(page, ids, manual=manual)


def _submit_replacement_preview(page, ids, *, manual=True):
    """Submit the comparison already reached, including a fresh-inventory link."""
    form = page.locator('form[action="/api/source-migrations/replacement/preview"]')
    expect(form.locator('[name="old_source_id"]')).to_have_value(str(ids["old_source"]))
    expect(form.locator('[name="new_source_id"]')).to_have_value(str(ids["new_source"]))
    form.locator('[name="mappings"]').fill(f"{ids['old_record']},{ids['new_record']}\n")
    if manual:
        form.locator('[name="manual_reviewed"]').check()
    _submit(page, form, "Create dry-run preview")
    expect(page.get_by_test_id("source-migration-review")).to_be_visible()
    return urlsplit(page.url).path.rsplit("/", 1)[1]


def _repair_preview(page, server, ids):
    page.goto(f"{server['url']}/source-migrations")
    lookup = page.locator('form:has(select[name="repair_source_id"])')
    lookup.locator('[name="repair_source_id"]').select_option(str(ids["new_source"]))
    _navigate(page, lookup.get_by_role("button", name="Show saved records", exact=True))
    _navigate(page, page.get_by_role("link", name="Review this attachment", exact=True))
    form = page.locator('form[action="/api/source-migrations/repair/preview"]')
    expect(form.locator('[name="source_id"]')).to_have_value(str(ids["new_source"]))
    expect(form.locator('[name="record_id"]')).to_have_value(str(ids["new_record"]))
    form.locator('[name="item_id"]').fill(str(ids["keep"]))
    form.locator('[name="manual_reviewed"]').check()
    _submit(page, form, "Preview binding repair")
    expect(page.get_by_test_id("source-migration-review")).to_contain_text(
        "Explicit administrator review of record/item IDs"
    )
    return urlsplit(page.url).path.rsplit("/", 1)[1]


def _decision(page, action, *, status=200):
    form = page.locator(f'form[action$="/{action}"]')
    form.locator('[name="reviewed"]').check()
    _submit(page, form, _DECISIONS[action], status=status)


def _bindings(server):
    with _db(server) as db:
        return [tuple(row) for row in db.execute("SELECT id, source_id, item_id, external_id FROM source_records ORDER BY id")]


def _enabled(server, source_id):
    with _db(server) as db:
        return db.execute("SELECT enabled FROM source_instances WHERE id = ?", (source_id,)).fetchone()[0]


def _plan_states(server):
    with _db(server) as db:
        return dict(db.execute("SELECT id, state FROM source_migration_plans"))


def _seed_machine_records(db, *, keep, previous):
    """Rich cached machine evidence for geometry, without a network or app import."""
    ids = {"keep": keep, "previous": previous}
    for key, item in (("old", keep), ("new", previous)):
        source = db.execute(
            "INSERT INTO source_instances (provider, name, base_url, last_status) VALUES ('opds', ?, 'http://127.0.0.1:1/opds', 'ok')",
            (f"{key.title()} reading room — digital books and special archive editions",),
        ).lastrowid
        record = db.execute(
            "INSERT INTO source_records (source_id, item_id, external_id, title, media_type, metadata_json, open_url) "
            "VALUES (?, ?, ?, ?, 'ebook', ?, 'http://127.0.0.1:1/archive.epub')",
            (source, item, f"urn:shelf:layout:{key}:" + "edition-identity-" * 8,
             "The Long Way Around — remote archive observation and illustrated digital edition",
             json.dumps({"provider_ids": {"Isbn13": "9780306406157"}})),
        ).lastrowid
        ids.update({f"{key}_source": source, f"{key}_record": record})
    return ids


def _seed_reading_life(db, user, item):
    """Nonempty forms, repeated cycles, multiline text, revisions and goal controls."""
    revisions = [
        {"activity": "read", "status": "in_progress", "started_on": "2025-01-01", "ended_on": None,
         "version": 1, "recorded_at": "2025-01-01 10:00:00"},
        {"activity": "read", "status": "finished", "started_on": "2025-01-01", "ended_on": "2025-01-31",
         "version": 2, "recorded_at": "2025-02-01 10:00:00"},
    ]
    session = db.execute(
        "INSERT INTO reading_sessions (user_id, item_id, activity, status, started_on, ended_on, version, submission_key, history_json) "
        "VALUES (?, ?, 'read', 'finished', '2025-01-01', '2025-01-31', 2, ?, ?)",
        (user, item, "a" * 32, json.dumps(revisions)),
    ).lastrowid
    db.execute(
        "INSERT INTO reading_sessions (user_id, item_id, activity, status, started_on, submission_key, history_json) "
        "VALUES (?, ?, 'read', 'in_progress', '2025-02-01', ?, ?)",
        (user, item, "b" * 32, json.dumps([{**revisions[0], "started_on": "2025-02-01"}])),
    )
    body = "Private field notes about the first completed cycle.\nA second line with details about editions and the journey. " * 3
    history = [
        {"entry_on": "2025-02-01", "body": "Earlier private reflection, retained in full.",
         "version": 1, "recorded_at": "2025-02-01 10:00:00"},
        {"entry_on": "2025-02-02", "body": body, "version": 2, "recorded_at": "2025-02-02 10:00:00"},
    ]
    entry = db.execute(
        "INSERT INTO reading_journal (user_id, item_id, session_id, entry_on, body, version, submission_key, history_json) "
        "VALUES (?, ?, ?, '2025-02-02', ?, 2, ?, ?)",
        (user, item, session, body, "c" * 32, json.dumps(history)),
    ).lastrowid
    for rule in ("sessions", "distinct_items"):
        db.execute(
            "INSERT INTO reading_goals (user_id, year, activity, count_rule, target) VALUES (?, 2025, 'all', ?, 12)",
            (user, rule),
        )
    return {"reading_session": session, "reading_entry": entry}


@pytest.fixture
def machine_catalogues():
    """Two replacement servers; only catalogue reads are permitted or expected."""
    state = {"old": "Old remote archive edition", "new": "Replacement remote archive edition", "requests": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib handler contract
            state["requests"].append(self.path)
            key = self.path.split("/")[1]
            status, body = 404, b"Not a catalogue"
            if key in ("old", "new") and self.path == f"/{key}/catalog.xml":
                status = 200
                body = (
                    '<feed xmlns="http://www.w3.org/2005/Atom">'
                    f'<id>urn:shelf:delivery:{key}</id><title>Local archive</title>'
                    f'<entry><id>urn:shelf:delivery:{key}:edition</id><title>{escape(state[key])}</title>'
                    '<author><name>Rowan Avery</name></author><summary>Saved remote description.</summary>'
                    '<link rel="http://opds-spec.org/acquisition/open-access" '
                    f'href="/{key}/edition.epub" type="application/epub+zip"/>'
                    '</entry></feed>'
                ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/atom+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=stub.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"root": f"http://127.0.0.1:{stub.server_address[1]}", "state": state}
    finally:
        stub.shutdown()
        stub.server_close()
        thread.join(timeout=5)


@pytest.fixture
def machine_delivery(connected_library, machine_catalogues):
    server, page = connected_library, connected_library["page"]
    ids = {}
    for key in ("old", "new"):
        page.goto(f"{server['url']}/sources")
        form = page.locator('form[action="/api/sources"]')
        _choice(form, "Provider").select_option("opds")
        form.get_by_label("Name", exact=True).fill(f"{key.title()} archive")
        form.get_by_label("Server or OPDS feed URL", exact=True).fill(f"{machine_catalogues['root']}/{key}/catalog.xml")
        _navigate(page, form.get_by_role("button", name="Add source", exact=True))
        source = int(urlsplit(page.url).path.rsplit("/", 1)[1])
        _navigate(page, page.get_by_role("button", name="Sync now", exact=True))
        expect(page.get_by_role("status")).to_have_text("Sync complete.")
        with _db(server) as db:
            row = db.execute("SELECT id, item_id FROM source_records WHERE source_id = ?", (source,)).fetchone()
            ids.update({f"{key}_source": source, f"{key}_record": row["id"],
                        "keep" if key == "old" else "previous": row["item_id"]})
    assert ids["keep"] != ids["previous"], "Server-local IDs and titles must not automatically merge catalogue entries"
    yield {**server, "ids": ids, "catalogues": machine_catalogues}
    assert all(path in ("/old/catalog.xml", "/new/catalog.xml") for path in machine_catalogues["state"]["requests"])


def test_repeat_sessions_journal_and_goals_are_private_and_survive_reload(connected_library, browser):
    server, admin = connected_library, connected_library["page"]
    username, password = "delivery-reader", "delivery-reader-password"
    _add_user(admin, server, username, password)
    with _db(server) as db:
        item = _item(db, "The Returning Listener", "audiobook", reading_status="read", notes="Shared catalogue note.")
        user = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
        db.execute("INSERT INTO user_item_state (user_id, item_id, status, progress, notes) VALUES (?, ?, 'paused', 35, 'Manual personal note.')", (user, item))
        db.execute("INSERT INTO reading_log (item_id, status, notes) VALUES (?, 'read', 'Legacy shared log.')", (item,))
    protected = _snapshot(server)

    with _browser_page(browser) as page:
        _login(page, server, username, password)
        page.goto(f"{server['url']}/browse")
        _navigate(page, page.locator(f'a[data-item-id="{item}"][href="/item/{item}"]'))
        _navigate(page, page.get_by_test_id("reading-life-item-link").get_by_role("link"))
        expect(page.get_by_test_id("reading-session-form").get_by_label("Activity", exact=True)).to_have_value("listen")
        first = _session(page)
        first_url = page.url
        form = page.get_by_test_id("reading-session-form")
        form.get_by_label("Session status", exact=True).select_option("finished")
        form.get_by_label("Ended on", exact=True).fill("2025-01-31")
        _submit(page, form, "Save session correction")
        page.reload()
        expect(form.locator('[name="version"]')).to_have_value("2")
        history = page.get_by_test_id("reading-revision-list")
        expect(history.locator("li")).to_have_count(2)
        expect(history).to_contain_text("Revision 1")
        expect(history).to_contain_text("In progress")
        expect(history).to_contain_text("Revision 2")
        expect(history).to_contain_text("Finished")

        original = "Private first listening cycle.\nThe last chapter changes the journey."
        entry = _journal(page, original)
        journal_url = page.url
        expect(page.get_by_role("link", name="Linked session", exact=True)).to_have_attribute("href", f"/reading-life/sessions/{first}")
        revised = 'Corrected private reflection.\nLiteral <script>not executable</script> text.'
        journal_form = page.get_by_test_id("reading-journal-form")
        expect(journal_form.get_by_label("Private journal entry", exact=True)).to_have_value(original)
        journal_form.get_by_label("Private journal entry", exact=True).fill(revised)
        _submit(page, journal_form, "Save journal correction")
        page.reload()
        expect(journal_form.get_by_label("Private journal entry", exact=True)).to_have_value(revised)
        expect(page.get_by_test_id("reading-revision-list")).to_contain_text(original)
        expect(page.get_by_test_id("reading-revision-list")).to_contain_text(revised)
        expect(page.get_by_test_id("reading-revision-list").locator("li")).to_have_count(2)

        _navigate(page, page.get_by_role("link", name="Sessions and journal for this item", exact=True))
        second = _session(page, started="2025-03-01", ended="2025-03-31", status="finished")
        assert second != first
        _navigate(page, page.get_by_role("link", name="All sessions for this item / start another cycle", exact=True))
        expect(page.get_by_test_id("reading-session-list").locator("li")).to_have_count(2)
        _navigate(page, page.get_by_role("link", name="All my sessions, journal and goals", exact=True))
        year = page.locator('form[method="get"][action="/reading-life"]')
        year.get_by_label("Year", exact=True).fill("2025")
        _navigate(page, year.get_by_role("button", name="Show year", exact=True))
        sessions_goal = _goal(page)
        expect(sessions_goal).to_contain_text("2 / 3 completed")
        distinct_goal = _goal(page, rule="distinct_items", target="2")
        expect(distinct_goal).to_contain_text("1 / 2 completed")
        page.reload()
        expect(sessions_goal).to_contain_text("2 / 3 completed")
        edit = sessions_goal.get_by_test_id("reading-goal-edit")
        edit.get_by_label("Target", exact=True).fill("5")
        _submit(page, edit, "Update target")
        expect(sessions_goal).to_contain_text("2 / 5 completed")

        # An administrator is another account, not a privileged private reader.
        admin.goto(f"{server['url']}/reading-life?year=2025&user_id={user}")
        expect(admin.get_by_test_id("reading-session-list")).to_have_count(0)
        expect(admin.get_by_test_id("reading-journal-list")).to_have_count(0)
        expect(admin.get_by_test_id("reading-goal")).to_have_count(0)
        for url in (first_url, journal_url):
            response = admin.goto(url)
            assert response.status == 404
            assert "Private first listening" not in admin.content()
        for path, fields in (
            (f"/api/reading-life/sessions/{first}", {"activity": "listen", "status": "paused", "started_on": "2025-01-01", "version": "2"}),
            (f"/api/reading-life/journal/{entry}", {"entry_on": "2025-02-01", "body": "Foreign overwrite", "version": "2"}),
        ):
            response = admin.request.post(server["url"] + path, form=fields, headers={"X-CSRF-Token": _csrf(admin)})
            assert response.status == 404

        admin.goto(f"{server['url']}/reading-life/items/{item}")
        own_session = _session(admin, started="2025-04-01")
        assert own_session not in (first, second)
        _journal(admin, "A different account's private entry.")
        page.goto(f"{server['url']}/reading-life?year=2025")
        expect(page.get_by_test_id("reading-session-list").locator("li")).to_have_count(2)
        expect(page.get_by_test_id("reading-journal-list")).not_to_contain_text("different account")
        expect(sessions_goal).to_contain_text("2 / 5 completed")
        before_remove = _snapshot(server, _READING[:2])
        remove = sessions_goal.locator('form[action$="/remove"]')
        _submit(page, remove, "Remove goal (keeps sessions and journal)")
        expect(page.get_by_test_id("reading-goal")).to_have_count(1)
        assert _snapshot(server, _READING[:2]) == before_remove
        page.goto(first_url)
        expect(page.get_by_test_id("reading-session-form").locator('[name="version"]')).to_have_value("2")
        _account_destination(page, "activity")
        _navigate(page, page.get_by_test_id("reading-life-activity-link").get_by_role("link"))
        expect(page.get_by_test_id("reading-life-page")).to_be_visible()
        _account_destination(page, "reading-life")
        expect(page.get_by_test_id("reading-life-page")).to_be_visible()
    assert _snapshot(server) == protected


def test_source_replacement_preview_apply_resync_retire_and_rollback(machine_delivery):
    server, page, ids = machine_delivery, machine_delivery["page"], machine_delivery["ids"]
    # Nonempty copy and private activity fixtures make preservation substantive.
    with _db(server) as db:
        user = db.execute("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)).fetchone()[0]
        db.execute("INSERT INTO item_copies (item_id, copy_number, is_primary) VALUES (?, 1, 1)", (ids["keep"],))
        _seed_reading_life(db, user, ids["keep"])
        db.execute("INSERT INTO user_item_state (user_id, item_id, status, notes) VALUES (?, ?, 'paused', 'Keep my manual activity.')", (user, ids["keep"]))
        db.execute("INSERT INTO user_item_events (user_id, item_id, event_type, snapshot_json) VALUES (?, ?, 'updated', ?)",
                   (user, ids["keep"], json.dumps({"status": "paused", "notes": "Keep my manual activity."})))
    before, bindings = _snapshot(server, _PROTECTED + _READING), _bindings(server)
    page.goto(f"{server['url']}/library")
    _account_destination(page, "source-migrations")
    expect(page.get_by_test_id("source-migration-workspace")).to_be_visible()
    plan = _replacement_preview(page, server, ids)
    receipt_url = page.url
    review = page.get_by_test_id("source-migration-review")
    expect(review.get_by_role("status")).to_contain_text("Dry run only")
    expect(review).to_contain_text("Explicit administrator review of old/new record IDs")
    expect(review).to_contain_text("Unmapped old records: 0")
    assert _plan_states(server) == {plan: "preview"}
    assert _snapshot(server, _PROTECTED + _READING) == before
    assert _bindings(server) == bindings
    _decision(page, "apply")
    page.reload()
    expect(review.get_by_role("status")).to_contain_text("Reviewed bindings applied")
    assert _bindings(server)[1][2] == ids["keep"]
    assert _enabled(server, ids["old_source"]) == 1
    assert _plan_states(server) == {plan: "applied"}
    applied_bindings = _bindings(server)

    # The imported identity must keep following the corrected binding on sync.
    server["catalogues"]["state"]["new"] = "A later remote observation, not a local title replacement"
    page.goto(f"{server['url']}/sources/{ids['new_source']}")
    _navigate(page, page.get_by_role("button", name="Sync now", exact=True))
    expect(page.get_by_role("status")).to_have_text("Sync complete.")
    assert _bindings(server)[1][2] == ids["keep"]
    assert _snapshot(server, _PROTECTED + _READING) == before
    page.goto(f"{server['url']}/item/{ids['keep']}")
    new_holding = page.get_by_test_id("available-here").locator(f'[data-availability-source="source:{ids["new_source"]}"]')
    expected_url = f"{server['catalogues']['root']}/new/edition.epub"
    expect(new_holding.locator('a[target="_blank"]')).to_have_attribute("href", expected_url)
    page.goto(receipt_url)
    expect(review.get_by_role("button", name="Retire old source", exact=True)).to_have_count(0)
    expect(review.get_by_role("alert")).to_contain_text("fresh complete replacement preview")
    fresh_inventory = review.get_by_role("link", name="Review the complete current source inventories", exact=True)
    expect(fresh_inventory).to_have_attribute("href", f"/source-migrations?old_source_id={ids['old_source']}&new_source_id={ids['new_source']}")
    assert _plan_states(server) == {plan: "applied"}
    assert _snapshot(server, _PROTECTED + _READING) == before
    _navigate(page, fresh_inventory)
    workspace = page.get_by_test_id("source-migration-workspace")
    for label in ("Old source records (1)", "Replacement source records (1)"):
        details = workspace.locator("details").filter(has=page.locator("summary").filter(has_text=label))
        details.locator("summary").click()
        expect(details).to_have_attribute("open", "")
    expect(workspace).to_contain_text(server["catalogues"]["state"]["new"])
    fresh_plan = _submit_replacement_preview(page, ids)
    fresh_receipt_url = page.url
    assert fresh_plan != plan
    expect(review.get_by_role("status")).to_contain_text("Dry run only")
    assert _plan_states(server) == {plan: "applied", fresh_plan: "preview"}
    assert _bindings(server) == applied_bindings
    assert _snapshot(server, _PROTECTED + _READING) == before
    with _db(server) as db:
        mapping = json.loads(db.execute("SELECT plan_json FROM source_migration_plans WHERE id = ?", (fresh_plan,)).fetchone()[0])["mappings"][0]
        assert mapping["before_item_id"] == mapping["after_item_id"] == ids["keep"]
    _decision(page, "apply")
    expect(review.get_by_role("status")).to_contain_text("Reviewed bindings applied")
    assert _plan_states(server) == {plan: "applied", fresh_plan: "applied"}
    assert _bindings(server) == applied_bindings
    assert _snapshot(server, _PROTECTED + _READING) == before
    _decision(page, "retire")
    expect(review).to_contain_text("The old instance was disabled, not deleted")
    assert _enabled(server, ids["old_source"]) == 0
    assert _plan_states(server) == {plan: "applied", fresh_plan: "applied"}
    assert _bindings(server) == applied_bindings
    assert _snapshot(server, _PROTECTED + _READING) == before
    page.goto(f"{server['url']}/item/{ids['keep']}")
    old_holding = page.get_by_test_id("available-here").locator(f'[data-availability-source="source:{ids["old_source"]}"]')
    expect(old_holding).to_have_attribute("data-availability-state", "unknown")
    expect(old_holding.locator('a[target="_blank"]')).to_have_count(0)
    expect(new_holding.locator('a[target="_blank"]')).to_have_attribute("href", expected_url)
    # The fresh review is a binding no-op: rolling it back restores the source
    # enablement, not the original attachment. Undo that on its original receipt.
    page.goto(fresh_receipt_url)
    _decision(page, "rollback")
    page.reload()
    expect(review.get_by_role("status")).to_contain_text("Bindings rolled back")
    expect(review.locator('form[method="post"]')).to_have_count(0)
    assert _plan_states(server) == {plan: "applied", fresh_plan: "rolled_back"}
    assert _bindings(server) == applied_bindings
    assert _enabled(server, ids["old_source"]) == 1
    assert _snapshot(server, _PROTECTED + _READING) == before
    page.goto(receipt_url)
    expect(review.get_by_role("button", name="Roll back bindings", exact=True)).to_be_visible()
    _decision(page, "rollback")
    page.reload()
    expect(review.get_by_role("status")).to_contain_text("Bindings rolled back")
    expect(review.locator('form[method="post"]')).to_have_count(0)
    assert _bindings(server) == bindings
    assert _enabled(server, ids["old_source"]) == 1
    assert _snapshot(server, _PROTECTED + _READING) == before
    assert _plan_states(server) == {plan: "rolled_back", fresh_plan: "rolled_back"}


def test_binding_repair_refuses_stale_decision_then_persists_after_real_sync(machine_delivery, browser):
    server, page, ids = machine_delivery, machine_delivery["page"], machine_delivery["ids"]
    plan = _repair_preview(page, server, ids)
    receipt_url = page.url
    bindings = _bindings(server)
    with _browser_page(browser) as concurrent:
        _login(concurrent, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        concurrent.goto(f"{server['url']}/item/{ids['keep']}/edit")
        concurrent.locator("#item-edit-form").get_by_label("Title", exact=True).fill("A concurrent household title correction")
        _navigate(concurrent, concurrent.get_by_test_id("save-btn"))
    before = _snapshot(server, _PROTECTED + _READING)
    _decision(page, "apply", status=409)
    expect(page.get_by_role("heading", level=1)).to_have_text("Source review refused")
    expect(page.get_by_role("alert")).to_have_text("The bindings, settings or local values changed. Preview and review again.")
    assert _bindings(server) == bindings
    assert _snapshot(server, _PROTECTED + _READING) == before
    page.goto(receipt_url)
    expect(page.get_by_role("alert")).to_contain_text("This review is stale")
    expect(page.get_by_role("button", name="Apply reviewed bindings", exact=True)).to_have_count(0)
    fresh = _repair_preview(page, server, ids)
    assert fresh != plan
    fresh_url = page.url
    _decision(page, "apply")
    server["catalogues"]["state"]["new"] = "New provider title after the binding repair"
    page.goto(f"{server['url']}/sources/{ids['new_source']}")
    _navigate(page, page.get_by_role("button", name="Sync now", exact=True))
    expect(page.get_by_role("status")).to_have_text("Sync complete.")
    assert _bindings(server)[1][2] == ids["keep"]
    assert _snapshot(server, _PROTECTED + _READING) == before
    page.goto(fresh_url)
    expect(page.get_by_role("button", name="Retire old source", exact=True)).to_have_count(0)
    _decision(page, "rollback")
    assert _bindings(server) == bindings
    assert _snapshot(server, _PROTECTED + _READING) == before


@pytest.mark.parametrize("action", ("retire", "rollback"))
def test_stale_retire_and_rollback_forms_require_fresh_review(machine_delivery, browser, action):
    server, page, ids = machine_delivery, machine_delivery["page"], machine_delivery["ids"]
    _replacement_preview(page, server, ids)
    receipt_url = page.url
    _decision(page, "apply")
    with _browser_page(browser) as concurrent:
        _login(concurrent, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        concurrent.goto(f"{server['url']}/item/{ids['keep']}/edit")
        concurrent.locator("#item-edit-form").get_by_label("Title", exact=True).fill(f"Household edit before {action}")
        _navigate(concurrent, concurrent.get_by_test_id("save-btn"))
    before, bindings = _snapshot(server, _PROTECTED + _READING), _bindings(server)
    _decision(page, action, status=409)
    expect(page.get_by_role("heading", level=1)).to_have_text("Source review refused")
    expect(page.get_by_role("alert")).to_have_text("The bindings, settings or local values changed. Preview and review again.")
    assert _bindings(server) == bindings
    assert _enabled(server, ids["old_source"]) == 1
    assert _snapshot(server, _PROTECTED + _READING) == before
    page.goto(receipt_url)
    _decision(page, action)
    if action == "retire":
        assert _enabled(server, ids["old_source"]) == 0
        assert _bindings(server) == bindings
    else:
        assert _bindings(server)[1][2] == ids["previous"]
        expect(page.get_by_test_id("source-migration-review").get_by_role("status")).to_contain_text("Bindings rolled back")
    assert _snapshot(server, _PROTECTED + _READING) == before


def test_delivery_csrf_feature_and_source_role_boundaries_preserve_data(connected_library, browser):
    server, page = connected_library, connected_library["page"]
    for role in ("viewer", "editor"):
        _add_user(page, server, f"delivery-{role}", f"delivery-{role}-password", role)
    with _db(server) as db:
        keep = _item(db, "Guarded local edition", "ebook")
        previous = _item(db, "Guarded imported edition", "ebook")
        ids = _seed_machine_records(db, keep=keep, previous=previous)
    page.goto(f"{server['url']}/reading-life/items/{keep}")
    session = _session(page, ended="2025-01-31", status="finished")
    entry = _journal(page, "An account-private guard regression note.")
    page.goto(f"{server['url']}/reading-life?year=2025")
    goal = _goal(page)
    goal_path = goal.get_by_test_id("reading-goal-edit").get_attribute("action")
    plan = _replacement_preview(page, server, ids, manual=False)
    decision = _fields(page.locator('form[action$="/apply"]'))
    decision["reviewed"] = "1"
    reading_actions = (
        (f"/api/reading-life/items/{keep}/sessions", {"activity": "read", "status": "in_progress", "started_on": "2025-01-01", "submission_key": "d" * 32}),
        (f"/api/reading-life/sessions/{session}", {"activity": "read", "status": "paused", "started_on": "2025-01-01", "version": "1"}),
        (f"/api/reading-life/items/{keep}/journal", {"entry_on": "2025-02-01", "body": "Unprotected create", "submission_key": "e" * 32}),
        (f"/api/reading-life/journal/{entry}", {"entry_on": "2025-02-01", "body": "Unprotected edit", "version": "1"}),
        ("/api/reading-life/goals", {"year": "2025", "activity": "read", "count_rule": "sessions", "target": "8"}),
        (goal_path, {"target": "9", "version": "1"}),
        (goal_path + "/remove", {"version": "1"}),
    )
    source_actions = (
        ("/api/source-migrations/replacement/preview", {"old_source_id": str(ids["old_source"]), "new_source_id": str(ids["new_source"]), "mappings": f"{ids['old_record']},{ids['new_record']}"}),
        ("/api/source-migrations/repair/preview", {"source_id": str(ids["new_source"]), "record_id": str(ids["new_record"]), "item_id": str(keep), "manual_reviewed": "1"}),
        *((f"/api/source-migrations/{plan}/{action}", {k: v for k, v in decision.items() if k != "_csrf"}) for action in _DECISIONS),
    )
    tables = _PROTECTED + _READING + ("source_records", "source_migration_plans", "source_binding_versions", "item_metadata_provenance")
    before = _snapshot(server, tables)
    for path, fields in reading_actions + source_actions:
        for token in (None, "not-the-browser-token"):
            supplied = {**fields, **({"_csrf": token} if token else {})}
            response = page.request.post(server["url"] + path, form=supplied)
            assert response.status == 403 and "CSRF" in response.text(), path
    assert _snapshot(server, tables) == before

    for role in ("viewer", "editor"):
        with _browser_page(browser) as restricted:
            _login(restricted, server, f"delivery-{role}", f"delivery-{role}-password")
            expect(restricted.get_by_test_id("account-menu-source-migrations")).to_have_count(0)
            for path in ("/source-migrations", f"/source-migrations/{plan}"):
                restricted.goto(server["url"] + path)
                expect(restricted).to_have_url(f"{server['url']}/browse")
            for path, fields in source_actions:
                response = restricted.request.post(server["url"] + path, form=fields, headers={"X-CSRF-Token": _csrf(restricted)})
                assert response.status == 403 and "feature_disabled" not in response.text(), (role, path)
    assert _snapshot(server, tables) == before

    headers = {"X-CSRF-Token": _csrf(page)}
    response = page.request.post(f"{server['url']}/api/settings/features/connected_library", form={"enabled": "0"}, headers=headers)
    assert response.ok
    for path in ("/reading-life?year=2025", f"/reading-life/items/{keep}", f"/reading-life/sessions/{session}",
                 f"/reading-life/journal/{entry}", "/source-migrations", f"/source-migrations/{plan}"):
        response = page.goto(server["url"] + path)
        assert response.status == 200
        expect(page.get_by_test_id("feature-disabled")).to_be_visible()
        assert "account-private guard regression note" not in page.content()
        for key in ("reading-life", "source-migrations"):
            expect(page.get_by_test_id(f"account-menu-{key}")).to_have_count(0)
    for path, fields in reading_actions + source_actions:
        response = page.request.post(server["url"] + path, form=fields, headers=headers)
        assert response.status == 403 and response.json()["feature"] == "connected_library", path
    page.goto(f"{server['url']}/item/{keep}")
    expect(page.get_by_test_id("reading-life-item-link")).to_have_count(0)
    page.goto(f"{server['url']}/activity")
    expect(page.get_by_test_id("reading-life-activity-link")).to_have_count(0)
    assert _snapshot(server, tables) == before
    response = page.request.post(f"{server['url']}/api/settings/features/connected_library", form={"enabled": "1"}, headers=headers)
    assert response.ok
    page.goto(f"{server['url']}/reading-life?year=2025")
    expect(page.get_by_test_id("reading-journal-list")).to_contain_text("An account-private guard regression note.")
    expect(page.get_by_test_id("reading-goal")).to_contain_text("1 / 3 completed")
    assert _snapshot(server, tables) == before


@pytest.mark.parametrize("session_linked", (False, True), ids=("item-entry", "session-entry"))
def test_journal_initial_newline_survives_native_forms_and_validation(connected_library, session_linked):
    """Browser HTML parsing, not a Jinja string assertion, owns this regression."""
    server, page = connected_library, connected_library["page"]
    with _db(server) as db:
        item = _item(db, "A Journal With Intentional Blank Lines")
    page.goto(f"{server['url']}/reading-life/items/{item}")
    if session_linked:
        _session(page)
    original = "\nAn intentional blank first line.\nThe first private reflection."
    entry = _journal(page, original)
    form = page.get_by_test_id("reading-journal-form")
    expect(form.get_by_label("Private journal entry", exact=True)).to_have_value(original)
    page.reload()
    expect(form.get_by_label("Private journal entry", exact=True)).to_have_value(original)

    # Whitespace is nonempty for native required validation but not a journal
    # entry for the service. The 422 document must retain even its first LF.
    unsaved = "\n   \n"
    form.get_by_label("Private journal entry", exact=True).fill(unsaved)
    _submit(page, form, "Save journal correction", status=422)
    expect(page.get_by_role("alert")).to_contain_text("Write a journal entry of 1 to 10000 characters")
    expect(form.get_by_label("Private journal entry", exact=True)).to_have_value(unsaved)
    expect(form.locator('[name="version"]')).to_have_value("1")

    corrected = "\n\nA corrected reflection with two intentional blank lines."
    form.get_by_label("Private journal entry", exact=True).fill(corrected)
    form.get_by_label("Entry date", exact=True).fill("2025-02-02")
    _submit(page, form, "Save journal correction")
    page.reload()
    expect(form.get_by_label("Private journal entry", exact=True)).to_have_value(corrected)
    bodies = page.get_by_test_id("reading-revision-list").locator("li > p.whitespace-pre-wrap").all_text_contents()
    assert bodies == [corrected, original]
    with _db(server) as db:
        row = db.execute("SELECT body, version, history_json FROM reading_journal WHERE id = ?", (entry,)).fetchone()
        # Native URL-encoded forms use CRLF; textarea.value and HTML text nodes
        # expose LF. Compare those representations without dropping blank lines.
        assert (row["body"].replace("\r\n", "\n"), row["version"]) == (corrected, 2)
        assert [revision["body"].replace("\r\n", "\n") for revision in json.loads(row["history_json"])] == [original, corrected]


def test_source_review_exposes_exact_cached_opening_actions_and_labels(machine_delivery):
    """Review the same safe action destinations actually offered by the library."""
    server, page, ids = machine_delivery, machine_delivery["page"], machine_delivery["ids"]
    page.goto(f"{server['url']}/library")
    expected = set()
    for key, item in (("old", ids["keep"]), ("new", ids["previous"])):
        url = f"{server['catalogues']['root']}/{key}/edition.epub"
        member = page.locator(f'[data-library-member="{item}"]')
        action = member.get_by_role("link", name=f"Read · {key.title()} archive", exact=True)
        expect(action).to_have_attribute("href", url)
        expect(action).to_have_attribute("target", "_blank")
        expect(action).to_have_attribute("rel", "noopener noreferrer")
        expected.add((url, "Read"))
    _replacement_preview(page, server, ids)
    actions = page.get_by_test_id("source-migration-review").locator('a[target="_blank"]')
    observed = {(action.get_attribute("href"), action.inner_text().strip()) for action in actions.all()}
    assert observed == expected, f"Review opening actions differ from the cached library actions: {observed} != {expected}"
    for action in actions.all():
        expect(action).to_have_attribute("rel", "noopener noreferrer")
        expect(action).to_have_attribute("referrerpolicy", "no-referrer")
        expect(action).to_have_accessible_name("Read")
        card = action.locator("..")
        expect(card.locator("p.break-all")).to_have_text(action.get_attribute("href"))
        expect(card).to_contain_text("Record ID")
