"""Offline F4/X2 delivery through the integrated application and native forms.

Remote contracts run on a loopback HTTP server, not patched services or routers.
R7 coverage is GET-only: no provider permission, lookup or artwork is assumed.
"""

import hashlib
import http.server
import json
import os
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import ADMIN_PASSWORD, ADMIN_USERNAME, _run_setup_wizard
from tests.e2e.test_connected_library import (
    _add_user, _browser_page, _choice, _db, _item, _login, _navigate,
    connected_library,
)
from tests.e2e.test_library_delivery import _assert_handoff, _csrf, _decision, _snapshot, _submit


pytestmark = pytest.mark.e2e


def _post(page, form, label):
    """Document navigation proves native CSRF/forms work with JavaScript off."""
    path = form.get_attribute("action")
    with page.expect_response(lambda r: r.request.method == "POST" and urlsplit(r.url).path == path) as posted:
        _navigate(page, form.get_by_role("button", name=label, exact=True))
    assert posted.value.status == 303
    assert posted.value.request.is_navigation_request()
    assert "x-csrf-token" not in posted.value.request.headers
    assert parse_qs(posted.value.request.post_data)["_csrf"] == [_csrf(page)]


def seed_progress_layout(db, user, item):
    """A populated GET-only account DTO; its invalid token must never be needed."""
    root = "http://127.0.0.1:1/jellyfin"
    source = db.execute("INSERT INTO source_instances (provider, name, base_url, last_status) VALUES ('jellyfin', 'Reading room personal playback progress', ?, 'ok')", (root,)).lastrowid
    db.execute("INSERT INTO source_records (source_id, item_id, external_id, title, media_type) VALUES (?, ?, 'layout-audio', 'The narrated archive', 'audiobook')", (source, item))
    fingerprint = hashlib.sha256(json.dumps([f"source:{source}", "jellyfin", root, 1], separators=(",", ":")).encode()).hexdigest()
    account = db.execute(
        "INSERT INTO user_source_accounts (user_id, source_key, provider, remote_user_id, credentials_enc, root_fingerprint, revision, consent_version, consented_at) "
        "VALUES (?, ?, 'jellyfin', 'layout-remote-reader', 'invalid-token-for-read-only-layout', ?, 'layout-revision', 'pull_v1', '2025-01-01T00:00:00+00:00')",
        (user, f"source:{source}", fingerprint),
    ).lastrowid
    actor = db.execute("SELECT username, role, token_version FROM users WHERE id = ?", (user,)).fetchone()
    db.execute(
        "INSERT INTO user_source_schedules (account_id, revision, interval_minutes, consent_version, consented_at, actor_username, actor_role, actor_token_version, next_due_at) "
        "VALUES (?, 'layout-schedule', 60, 'scheduled_pull_v1', '2025-01-01', ?, ?, ?, '2099-01-01T00:00:00+00:00')",
        (account, *actor),
    )
    remote = {"status": "in_progress", "progress": 35, "position": 350, "total": 1000, "unit": "seconds", "updated_at": "2025-01-01T00:00:00+00:00"}
    db.execute(
        "INSERT INTO user_source_progress (account_id, item_id, binding_hash, remote_json, review_token, review_local_marker, reason, observed_at) "
        "VALUES (?, ?, 'layout-binding', ?, ?, 'layout-marker', 'existing_local_state', '2025-01-01')",
        (account, item, json.dumps(remote), "a" * 43),
    )
    db.execute(
        "INSERT INTO user_source_progress_events (user_id, item_id, source_key, decision, snapshot_json, event_key) VALUES (?, ?, ?, 'accepted', ?, 'layout-progress-history')",
        (user, item, f"source:{source}", json.dumps({"remote": remote, "before": {"status": "paused", "progress": 10}, "after": {"status": "in_progress", "progress": 35}})),
    )
    return account


@pytest.fixture
def playback_server():
    state = {"requests": [], "position": {"reader-one": 250, "reader-two": 100}, "day": 1}
    tokens = {f"fixture-{user}-token": user for user in ("reader-one", "reader-two")}
    tokens["fixture-catalogue-token"] = "catalogue-reader"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parts = urlsplit(self.path)
            token = self.headers.get("X-Emby-Token") or self.headers.get("Authorization", "").removeprefix("Bearer ")
            user = tokens.get(token)
            state["requests"].append((self.command, parts.path, user, parts.query))
            status, payload = 404, {"error": "No such fixture endpoint"}
            if user:
                if parts.path == "/internal/jellyfin/Users/Me":
                    status, payload = 200, {"Id": user}
                elif parts.path == "/internal/jellyfin/Users/catalogue-reader/Views":
                    status, payload = 200, {"Items": [{"Id": "films-and-audio"}], "TotalRecordCount": 1}
                elif parts.path == "/internal/jellyfin/Items" and user == "catalogue-reader":
                    status, payload = 200, {"Items": [
                        {"Id": "film", "Name": "The Estuary Film", "Type": "Movie", "RunTimeTicks": 10_000_000_000},
                        {"Id": "audio", "Name": "The Estuary Narrated Edition", "Type": "AudioBook", "RunTimeTicks": 10_000_000_000},
                    ], "TotalRecordCount": 2, "StartIndex": 0}
                elif parts.path in (f"/internal/jellyfin/Users/{user}/Items/film", f"/internal/jellyfin/Users/{user}/Items/audio"):
                    identity = parts.path.rsplit("/", 1)[1]
                    status, payload = 200, {"Id": identity, "Type": "Movie" if identity == "film" else "AudioBook", "RunTimeTicks": 10_000_000_000,
                        "UserData": {"Played": False, "PlaybackPositionTicks": state["position"][user] * 10_000_000,
                                     "LastPlayedDate": f"2025-01-{state['day']:02d}T12:00:00Z"}}
                elif parts.path == "/internal/abs/api/me":
                    status, payload = 200, {"id": user}
                elif parts.path == "/internal/abs/api/me/progress/audio":
                    status, payload = 200, {"libraryItemId": "audio", "userId": user, "episodeId": None,
                        "currentTime": state["position"][user], "duration": 1000, "isFinished": False,
                        "lastUpdate": 1735732800000 + (state["day"] - 1) * 86_400_000}
            else:
                status, payload = 401, {"error": "Use a fixture user token"}
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            state["requests"].append((self.command, self.path, None, ""))
            self.send_error(405)

        def log_message(self, *args):
            pass

    stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=stub.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"root": f"http://127.0.0.1:{stub.server_address[1]}", "state": state}
        assert all(method == "GET" and path.startswith("/internal/") for method, path, user, query in state["requests"])
    finally:
        stub.shutdown()
        stub.server_close()
        thread.join(timeout=5)


@pytest.fixture
def active_library(server_factory, browser):
    # The unit-isolation fixture disables this worker. Opt this subprocess back
    # in explicitly; do not call its service hook or shorten production timers.
    assert "SHELF_AUDIBLE_CATALOG_ENABLED" not in os.environ
    assert "SHELF_AUDIBLE_CATALOG_GRANT_REFERENCE" not in os.environ
    server = server_factory({"SHELF_DISABLE_SOURCE_PROGRESS": ""})
    _run_setup_wizard(browser, server["url"], profile="everything")
    with _browser_page(browser) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        yield {**server, "page": page}


@pytest.fixture
def playback_library(active_library, playback_server):
    server, page = active_library, active_library["page"]
    root = playback_server["root"]
    page.goto(server["url"] + "/sources")
    add = page.locator('form[action="/api/sources"]')
    _choice(add, "Provider").select_option("jellyfin")
    add.get_by_label("Name", exact=True).fill("A household Jellyfin")
    add.get_by_label("Server or OPDS feed URL", exact=True).fill(root + "/internal/jellyfin")
    _post(page, add, "Add source")
    source = int(urlsplit(page.url).path.rsplit("/", 1)[1])
    form = page.locator(f'form[action="/api/sources/{source}/settings"]')
    form.locator('[name="browser_url"]').fill(root + "/browser/jellyfin")
    form.locator('[name="token"]').fill("fixture-catalogue-token")
    form.locator('[name="options_json"]').fill(json.dumps({"user_id": "catalogue-reader", "library_ids": ["films-and-audio"]}))
    _post(page, form, "Save settings")
    _post(page, page.locator(f'form[action="/api/sources/{source}/sync"]'), "Sync now")
    expect(page.get_by_role("status")).to_have_text("Sync complete.")
    assert "fixture-catalogue-token" not in page.content()
    with _db(server) as db:
        ids = dict(db.execute("SELECT external_id, item_id FROM source_records WHERE source_id = ?", (source,)))
        db.execute("UPDATE items SET abs_id = 'audio', abs_library_id = 'audio-library', asin = 'B012345678' WHERE id = ?", (ids["audio"],))
        for key, value in (("abs_url", root + "/internal/abs"), ("abs_public_url", root + "/browser/abs"), ("abs_excluded_libraries", "[]")):
            db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        backup = db.execute("INSERT INTO source_instances (provider, name, base_url, last_status) VALUES ('links', 'Z household backup', ?, 'ok')", (root,)).lastrowid
        db.execute("INSERT INTO source_records (source_id, item_id, external_id, title, media_type, open_url) VALUES (?, ?, 'backup-film', 'Film backup', 'digital_video', ?)",
                   (backup, ids["film"], root + "/browser/backup/film"))
    for role in ("one", "two"):
        _add_user(page, server, f"source-reader-{role}", f"source-reader-{role}-password")
    yield {**server, "remote": playback_server, "source": source, "backup": backup, **ids}


def _progress_page(page, server, item):
    page.goto(f"{server['url']}/account/source-progress?item_id={item}")
    expect(page.get_by_test_id("source-progress-page")).to_be_visible()


def _link(page, key, user):
    details = page.locator('details:has(form[action="/api/source-progress/accounts/link"])')
    details.locator("summary").click()
    form = details.locator("form")
    form.locator('[name="source_key"]').select_option(key)
    form.locator('[name="remote_user_id"]').fill(user)
    form.locator('[name="token"]').fill(f"fixture-{user}-token")
    form.locator('[name="consent"]').check()
    _post(page, form, "Verify and link my account")
    expect(page.get_by_role("status")).to_contain_text("linked for explicit pulls only")
    assert f"fixture-{user}-token" not in page.content()
    disconnect = page.locator('form[action$="/disconnect"]').last
    return int(disconnect.get_attribute("action").split("/")[-2])


def _state(server, username, item):
    with _db(server) as db:
        row = db.execute("SELECT s.status, s.progress, s.notes FROM user_item_state s JOIN users u ON u.id = s.user_id WHERE u.username = ? AND s.item_id = ?", (username, item)).fetchone()
        return tuple(row) if row else None


def test_preferred_handoff_is_private_exact_and_revalidated_without_remote_navigation(playback_library, browser):
    server, admin = playback_library, playback_library["page"]
    item = server["film"]
    expected = server["remote"]["root"] + "/browser/jellyfin/web/index.html#!/details?id=film"
    before = len(server["remote"]["state"]["requests"])
    with _browser_page(browser, java_script_enabled=False) as reader:
        _login(reader, server, "source-reader-one", "source-reader-one-password")
        reader.goto(f"{server['url']}/item/{item}")
        available = reader.get_by_test_id("available-here")
        expect(available).to_contain_text("No live connection check")
        exact = available.locator(f'[data-availability-source="source:{server["source"]}"] a[target="_blank"]')
        stale_exact = _assert_handoff(reader, exact, expected)
        prefer = available.locator('form:has(input[name="action"][value="Watch"])')
        prefer.locator('[name="source_key"]').select_option(f"source:{server['source']}")
        _post(reader, prefer, "Save my preference")
        reader.reload()
        expect(prefer.locator('[name="source_key"]')).to_have_value(f"source:{server['source']}")
        admin.goto(f"{server['url']}/item/{item}")
        expect(admin.get_by_test_id("available-here").locator('select[name="source_key"]').first).to_have_value("")
        admin.goto(f"{server['url']}/sources/{server['source']}")
        _post(admin, admin.locator('form[action$="/disable"]'), "Disable source")
        response = reader.request.get(server["url"] + stale_exact, max_redirects=0)
        assert response.status == 404 and "location" not in response.headers
        fallback = reader.request.get(f"{server['url']}/api/library/items/{item}/open/Watch", max_redirects=0)
        assert fallback.status == 303 and fallback.headers["location"] == server["remote"]["root"] + "/browser/backup/film"
        assert fallback.headers["referrer-policy"] == "no-referrer"
        reader.reload()
        expect(available.get_by_role("status")).to_contain_text("preferred source is unavailable")
        dead = available.locator(f'[data-availability-source="source:{server["source"]}"]')
        expect(dead).to_have_attribute("data-availability-state", "unknown")
        expect(dead.locator('a[target="_blank"]')).to_have_count(0)
        expect(prefer.locator('[name="source_key"]')).to_have_value(f"source:{server['source']}")
    assert len(server["remote"]["state"]["requests"]) == before, "GET/display/redirect must not probe a remote server"


@pytest.mark.parametrize("provider", ("jellyfin", "abs"))
def test_private_native_progress_link_pull_conflicts_schedule_and_disconnect(playback_library, browser, provider):
    server = playback_library
    item = server["film" if provider == "jellyfin" else "audio"]
    sibling = server["audio" if provider == "jellyfin" else "film"]
    key = f"source:{server['source']}" if provider == "jellyfin" else "abs"
    remote = server["remote"]["state"]
    protected = _snapshot(server)
    # Personal state/events are the intended output, never shared catalogue rows.
    protected = {k: v for k, v in protected.items() if k not in {"user_item_state", "user_item_events"}}
    with _browser_page(browser, java_script_enabled=False) as one, _browser_page(browser, java_script_enabled=False) as two:
        _login(one, server, "source-reader-one", "source-reader-one-password")
        _login(two, server, "source-reader-two", "source-reader-two-password")
        _progress_page(one, server, item)
        before_get = len(remote["requests"])
        one.reload()
        assert len(remote["requests"]) == before_get
        first = _link(one, key, "reader-one")
        for path in (f"/api/source-progress/accounts/{first}/disconnect", f"/api/source-progress/accounts/{first}/schedule",
                     f"/api/source-progress/accounts/{first}/items/{item}/pull", "/api/source-progress/accounts/link"):
            denied = one.request.post(server["url"] + path, form={})
            assert denied.status == 403 and "CSRF" in denied.text()
        with _db(server) as db:
            assert db.execute("SELECT COUNT(*) FROM user_source_schedules").fetchone()[0] == 0
        _progress_page(one, server, item)
        pull = one.locator(f'form[action="/api/source-progress/accounts/{first}/items/{item}/pull"]')
        _post(one, pull, "Pull this edition; preview conflicts")
        expect(one.get_by_role("status")).to_contain_text("Remote progress accepted")
        assert _state(server, "source-reader-one", item)[:2] == ("in_progress", 25)
        assert _state(server, "source-reader-one", sibling) is None
        _post(one, pull, "Pull this edition; preview conflicts")
        expect(one.get_by_role("status")).to_contain_text("No new remote progress")
        one.goto(f"{server['url']}/item/{item}")
        one.get_by_test_id("personal-library-editor").locator("summary").click()
        form = one.get_by_test_id("personal-library-form")
        form.get_by_label("My status", exact=True).select_option("paused")
        form.get_by_label("Progress", exact=False).fill("80")
        form.get_by_label("My notes", exact=True).fill("This account's manual progress note.")
        _post(one, form, "Save my activity")
        remote["position"]["reader-one"], remote["day"] = 500, 2
        _progress_page(one, server, item)
        _post(one, pull, "Pull this edition; preview conflicts")
        review = one.locator("[data-progress-review]")
        expect(review).to_contain_text("80%")
        expect(review).to_contain_text("50%")
        foreign_path = review.locator("form").get_attribute("action")
        foreign_token = review.locator('[name="review_token"]').input_value()
        response = two.request.post(server["url"] + foreign_path, form={"review_token": foreign_token, "decision": "accepted"},
                                    headers={"Accept": "application/json", "X-CSRF-Token": _csrf(two)})
        assert response.status == 404
        _post(one, review.locator("form"), "Keep my local progress")
        assert _state(server, "source-reader-one", item)[:2] == ("paused", 80)
        remote["position"]["reader-one"], remote["day"] = 600, 3
        _progress_page(one, server, item)
        _post(one, pull, "Pull this edition; preview conflicts")
        _post(one, review.locator("form"), "Replace local progress with this remote observation")
        assert _state(server, "source-reader-one", item) == ("in_progress", 60, "This account's manual progress note.")
        expect(one.locator("#source-progress-history")).to_contain_text("Kept local progress")
        _progress_page(two, server, item)
        expect(two.locator("#source-progress-history")).not_to_contain_text("Accepted remote progress")
        assert "reader-one" not in two.locator("#source-progress-accounts").inner_text()
        before_foreign = len(remote["requests"])
        for path in (f"/api/source-progress/accounts/{first}/disconnect", f"/api/source-progress/accounts/{first}/items/{item}/pull"):
            denied = two.request.post(server["url"] + path, form={}, headers={"Accept": "application/json", "X-CSRF-Token": _csrf(two)})
            assert denied.status == 404
        assert len(remote["requests"]) == before_foreign
        second = _link(two, key, "reader-two")
        _progress_page(two, server, item)
        _post(two, two.locator(f'form[action="/api/source-progress/accounts/{second}/items/{item}/pull"]'), "Pull this edition; preview conflicts")
        assert _state(server, "source-reader-two", item)[:2] == ("in_progress", 10)
        schedule = two.locator(f'form[action="/api/source-progress/accounts/{second}/schedule"]')
        schedule.locator("..").locator("summary").click()
        schedule.locator('[name="interval_minutes"]').select_option("15")
        _post(two, schedule, "Save my automatic-pull choice")
        expect(two.get_by_role("status")).to_contain_text("Explicit consent")
        with _db(server) as db:
            assert db.execute("SELECT COUNT(*) FROM user_source_schedules WHERE account_id = ?", (second,)).fetchone()[0] == 0
        schedule.locator("..").locator("summary").click()
        schedule.locator('[name="interval_minutes"]').select_option("15")
        schedule.locator('[name="consent"]').check()
        remote["position"]["reader-two"], remote["day"] = 200, 4
        _post(two, schedule, "Save my automatic-pull choice")
        expect(two.get_by_role("status")).to_contain_text("automatic-pull choice was saved")
        expect(two.get_by_test_id("progress-scheduler-inactive")).to_have_count(0)
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline and _state(server, "source-reader-two", item)[1] != 20:
            two.wait_for_timeout(250)
        assert _state(server, "source-reader-two", item)[:2] == ("in_progress", 20), "Real lifespan worker did not run the separately consented schedule"
        two.reload()
        schedule.locator("..").locator("summary").click()
        schedule.locator('[name="interval_minutes"]').select_option("0")
        schedule.locator('[name="consent"]').uncheck()
        _post(two, schedule, "Save my automatic-pull choice")
        with _db(server) as db:
            assert db.execute("SELECT COUNT(*) FROM user_source_schedules WHERE account_id = ?", (second,)).fetchone()[0] == 0
        _post(one, one.locator(f'form[action="/api/source-progress/accounts/{first}/disconnect"]'), "Disconnect and forget my token")
        expect(one.get_by_role("status")).to_contain_text("local progress and history remain")
        expect(one.locator("#source-progress-history")).to_contain_text("Accepted remote progress")
        no_remote_get = len(remote["requests"])
        one.reload()
        two.reload()
        assert len(remote["requests"]) == no_remote_get
        assert _state(server, "source-reader-one", item)[1] == 60
        assert _state(server, "source-reader-two", item)[1] == 20
        with _db(server) as db:
            assert db.execute("SELECT COUNT(*) FROM user_source_accounts WHERE id = ?", (first,)).fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM user_source_accounts WHERE id = ?", (second,)).fetchone()[0] == 1
            for username, count in (("source-reader-one", 3), ("source-reader-two", 2)):
                assert db.execute("SELECT COUNT(*) FROM user_source_progress_events e JOIN users u ON e.user_id = u.id WHERE u.username = ? AND e.item_id = ?", (username, item)).fetchone()[0] == count
        for pg in (one, two):
            assert "fixture-reader-" not in pg.content()
    current = _snapshot(server)
    assert {k: v for k, v in current.items() if k not in {"user_item_state", "user_item_events"}} == protected
    personal_requests = [(path, who, query) for method, path, who, query in remote["requests"] if who != "catalogue-reader"]
    assert personal_requests and all(not query for path, who, query in personal_requests)
    assert all("catalogue-reader" not in path for path, who, query in personal_requests)


def test_audiobook_metadata_get_is_offline_and_does_not_infer_provider_permission(playback_library):
    server, page = playback_library, playback_library["page"]
    before = list(server["remote"]["state"]["requests"])
    items = _snapshot(server)
    response = page.goto(f"{server['url']}/api/audiobooks/{server['audio']}/metadata")
    assert response.status == 200 and response.headers["cache-control"] == "no-store"
    expect(page.get_by_role("heading", name="Audiobook metadata", level=1, exact=True)).to_be_visible()
    expect(page.get_by_test_id("audiobook-metadata")).to_be_visible()
    expect(page.get_by_test_id("audible-provider-blocked")).to_contain_text("Operator opt-in is not a licence")
    expect(page.get_by_test_id("audiobook-preview-form")).to_have_count(0)
    expect(page.get_by_test_id("audiobook-preview")).to_have_count(0)
    expect(page.get_by_test_id("audiobook-metadata").locator("img")).to_have_count(0)
    assert server["remote"]["state"]["requests"] == before
    assert _snapshot(server) == items


def test_native_inventory_keysets_exact_lookup_batch_limit_and_whole_scope_retirement(connected_library, playback_server):
    server, page = connected_library, connected_library["page"]
    with _db(server) as db:
        sources, records = [], []
        for side in ("old", "new"):
            source = db.execute("INSERT INTO source_instances (provider, name, base_url, credentials_enc) VALUES ('opds', ?, ?, 'not-a-token-needed-for-GET')",
                                (f"Paged {side} library", playback_server["root"] + "/never-fetch")).lastrowid
            sources.append(source)
            rows = []
            for number in range(102):
                item = _item(db, f"Paged {side} edition {number:03d}", "ebook")
                record = db.execute("INSERT INTO source_records (source_id, item_id, external_id, title, media_type, metadata_json, open_url) VALUES (?, ?, ?, ?, 'ebook', ?, ?)",
                    (source, item, f"{side}:{number:03d}", f"Paged remote {number:03d}", json.dumps({"provider_ids": {"Asin": f"B{number:09d}"}}),
                     f"{playback_server['root']}/reader/{side}/{number}")).lastrowid
                rows.append((record, item))
            records.append(rows)
    before = _snapshot(server)
    page.goto(server["url"] + "/source-migrations")
    compare = page.locator('form:has(select[name="old_source_id"])')
    for key, value in zip(("old_source_id", "new_source_id"), sources):
        compare.locator(f'[name="{key}"]').select_option(str(value))
    _navigate(page, compare.get_by_role("button", name="Compare local snapshots", exact=True))
    start_url = page.url

    def inventory(side):
        return page.get_by_test_id("migration-inventory-page").filter(has=page.locator("summary").filter(has_text=f"{side} source records"))

    for label, rows in (("Old", records[0]), ("Replacement", records[1])):
        page.goto(start_url)
        seen = []
        for expected_size in (50, 50, 2):
            panel = inventory(label)
            panel.locator("summary").click()
            expect(panel.locator("article")).to_have_count(expected_size)
            ids = [int(re.search(r"Record ID #(\d+)", text).group(1)) for text in panel.locator("article > p:first-child").all_text_contents()]
            seen.extend(ids)
            if expected_size == 50:
                _navigate(page, panel.get_by_role("link", name="Next records", exact=True))
        assert seen == [row[0] for row in rows]
        _navigate(page, panel.get_by_role("link", name="Previous records", exact=True))
        panel = inventory(label)
        panel.locator("summary").click()
        expect(panel.locator("article")).to_have_count(50)
    page.goto(start_url)
    lookup = page.locator('form:has(input[name="new_external_id"])')
    lookup.locator('[name="new_external_id"]').fill("new:101")
    _navigate(page, lookup.get_by_role("button", name="Look up external ID", exact=True))
    exact = inventory("Replacement")
    exact.locator("summary").click()
    expect(exact.locator("article")).to_have_count(1)
    expect(exact).to_contain_text(f"Record ID #{records[1][-1][0]}")
    assert "not-a-token-needed-for-GET" not in page.content()
    assert _snapshot(server) == before
    form = page.locator('form[action="/api/source-migrations/replacement/preview"]')
    pairs = [f"{old[0]},{new[0]}" for old, new in zip(*records)]
    form.locator('[name="mappings"]').fill("\n".join(pairs[:101]))
    _submit(page, form, "Create dry-run preview", status=422)
    expect(page.get_by_role("alert")).to_contain_text("between 1 and 100")
    page.goto(start_url)
    form.locator('[name="mappings"]').fill("\n".join(pairs[:100]))
    _submit(page, form, "Create dry-run preview")
    original_receipt = page.url
    review = page.get_by_test_id("source-migration-review")
    expect(review).to_contain_text("Old source records: 102")
    expect(review).to_contain_text("Selected pairs: 100")
    expect(review).to_contain_text("Unmapped old records: 2")
    _decision(page, "apply")
    # A separate, real native repair changes an unseen/unmapped late record.
    # Refreshing the original receipt must not authorize retiring that new scope.
    page.goto(start_url)
    lookup.locator('[name="new_external_id"]').fill("new:101")
    _navigate(page, lookup.get_by_role("button", name="Look up external ID", exact=True))
    inventory("Replacement").locator("summary").click()
    _navigate(page, inventory("Replacement").get_by_role("link", name="Review this attachment", exact=True))
    repair = page.locator('form[action="/api/source-migrations/repair/preview"]')
    expect(repair.locator('[name="record_id"]')).to_have_value(str(records[1][-1][0]))
    repair.locator('[name="item_id"]').fill(str(records[0][-1][1]))
    repair.locator('[name="manual_reviewed"]').check()
    _submit(page, repair, "Preview binding repair")
    repair_receipt = page.url
    _decision(page, "apply")
    page.goto(original_receipt)
    expect(review.get_by_role("button", name="Retire old source", exact=True)).to_have_count(0)
    expect(review.get_by_role("alert")).to_contain_text("fresh complete replacement preview")
    _navigate(page, review.get_by_role("link", name="Review the complete current source inventories", exact=True))
    fresh_inventory_url = page.url
    for label in ("Old", "Replacement"):
        page.goto(fresh_inventory_url)
        for expected_size in (50, 50, 2):
            panel = inventory(label)
            panel.locator("summary").click()
            expect(panel.locator("article")).to_have_count(expected_size)
            if expected_size == 50:
                _navigate(page, panel.get_by_role("link", name="Next records", exact=True))
    page.goto(fresh_inventory_url)
    form.locator('[name="mappings"]').fill("\n".join(pairs[:100]))
    _submit(page, form, "Create dry-run preview")
    fresh_receipt = page.url
    assert fresh_receipt not in (original_receipt, repair_receipt)
    expect(review).to_contain_text("Old source records: 102")
    expect(review).to_contain_text("Unmapped old records: 2")
    _decision(page, "apply")
    _decision(page, "retire")
    expect(review).to_contain_text("The old instance was disabled, not deleted")
    denied = page.request.get(f"{server['url']}/api/library/items/{records[0][-1][1]}/open/Read?source_key=source:{sources[0]}", max_redirects=0)
    assert denied.status == 404 and "location" not in denied.headers, "Retirement must remove unmatched old-source actions too"
    with _db(server) as db:
        assert db.execute("SELECT enabled FROM source_instances WHERE id = ?", (sources[0],)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM source_records WHERE source_id = ?", (sources[0],)).fetchone()[0] == 102
        assert db.execute("SELECT item_id FROM source_records WHERE id = ?", (records[1][-2][0],)).fetchone()[0] == records[1][-2][1]
        assert db.execute("SELECT item_id FROM source_records WHERE id = ?", (records[1][-1][0],)).fetchone()[0] == records[0][-1][1]
    assert _snapshot(server) == before
    assert playback_server["state"]["requests"] == [], "Inventory GETs and reviewed decisions must not contact a connector"
    _decision(page, "rollback")
    page.goto(repair_receipt)
    _decision(page, "rollback")
    page.goto(original_receipt)
    _decision(page, "rollback")
    with _db(server) as db:
        assert db.execute("SELECT enabled FROM source_instances WHERE id = ?", (sources[0],)).fetchone()[0] == 1
        assert dict(db.execute("SELECT id, item_id FROM source_records")) == dict(records[0] + records[1])
        assert [row[0] for row in db.execute("SELECT state FROM source_migration_plans")] == ["rolled_back"] * 3
    assert _snapshot(server) == before
    assert playback_server["state"]["requests"] == []
