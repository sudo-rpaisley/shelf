"""Offline remote contracts, consent, conflict clocks, atomicity and ownership."""

import json
import logging
import httpx
import pytest
import respx

from app import crypto
from app.database import get_db, set_setting
from app.services import item_write, media_groups, personal_library, source_progress as svc
from tests.conftest import _create_user, _TEST_CSRF_TOKEN
from tests.test_availability import _item, _record, _source
from tests.test_personal_library import _save
from tests.test_source_handoff import f4_client  # noqa: F401 — scoped router fixture


ROOT = "https://jellyfin.example/proxy"
ABS_ROOT = "http://abs.internal:80/audio"
TOKEN = "test-only-personal-access-token"
REMOTE_USER = "my-remote-user"
DATE = "2026-01-01T10:00:00Z"


def _jelly(position=250, total=1000, finished=False, clock=DATE, **values):
    return {"Id": "remote-item", "Type": "Movie", "RunTimeTicks": total * 10_000_000,
            "UserData": {"Played": finished, "PlaybackPositionTicks": position * 10_000_000,
                         "LastPlayedDate": clock}, **values}


def _abs(position=250, total=1000, finished=False, clock=1767261600000, **values):
    return {"libraryItemId": "remote-item", "userId": REMOTE_USER, "episodeId": None,
            "currentTime": position, "duration": total, "isFinished": finished,
            "lastUpdate": clock, "progress": position / total if total else 0, **values}


@pytest.fixture
def remote():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as routes:
        routes.get(f"{ROOT}/Users/Me", name="me").respond(200, json={"Id": REMOTE_USER, "AccessToken": TOKEN})
        routes.get(f"{ROOT}/Users/{REMOTE_USER}/Items/remote-item", name="progress").respond(200, json=_jelly())
        routes.get(f"{ABS_ROOT}/api/me", name="abs_me").respond(200, json={"id": REMOTE_USER, "token": TOKEN})
        routes.get(f"{ABS_ROOT}/api/me/progress/remote-item", name="abs_progress").respond(200, json=_abs())
        yield routes


@pytest.fixture
def bound(db):
    source = _source(db, provider="jellyfin", base_url=ROOT, browser_url="https://watch.example/proxy",
                     credentials_enc="unused-server-catalogue-key", options_json='{"user_id":"admin-catalogue-user"}')
    item = _item(db, "My film", "digital_video")
    record = _record(db, source, item, external_id="remote-item", media_type="digital_video")
    db.commit()
    return {"item": item, "key": f"source:{source}", "source": source, "record": record}


async def _link(uid, key):
    return await svc.link_account(uid, key, token=TOKEN, remote_user_id=REMOTE_USER, consent=svc.CONSENT_VERSION)


def _state(db, uid, item):
    return personal_library.state_for(db, uid, item)


def _pending(db, uid, item):
    return svc.context(db, uid, item_id=item)["pending"][0]


def _review(db, uid, pending, decision="accepted"):
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    result = svc.review(db, uid, pending["id"], token=pending["review_token"], decision=decision)
    db.commit()
    return result


@pytest.mark.asyncio
async def test_link_requires_separate_token_consent_and_matching_self_identity(db, viewer_user, bound, remote):
    uid = viewer_user["id"]
    for consent in ("", "true", "push_v1"):
        with pytest.raises(svc.ProgressError, match="consent_required"):
            await svc.link_account(uid, bound["key"], token=TOKEN, remote_user_id=REMOTE_USER, consent=consent)
    assert len(remote.calls) == 0
    with pytest.raises(svc.ProgressError, match="identity_mismatch"):
        await svc.link_account(uid, bound["key"], token=TOKEN, remote_user_id="admin-catalogue-user", consent=svc.CONSENT_VERSION)
    remote["me"].respond(200, json={"ServerName": "not a user", "secret": TOKEN})
    with pytest.raises(svc.ProgressError, match="malformed_response"):
        await _link(uid, bound["key"])
    assert db.execute("SELECT COUNT(*) FROM user_source_accounts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_link_encrypts_and_never_reuses_admin_key_or_shares_remote_user(db, viewer_user, bound, remote, caplog):
    other = _create_user("other", "password123", "Other", "viewer")
    caplog.set_level(logging.INFO, logger="httpx")
    account_id = await _link(viewer_user["id"], bound["key"])
    account = dict(db.execute("SELECT * FROM user_source_accounts WHERE id = ?", (account_id,)).fetchone())
    assert crypto.is_fernet_token(account["credentials_enc"])
    assert TOKEN not in json.dumps(account)
    assert account["consent_version"] == "pull_v1" and account["consented_at"]
    with pytest.raises(svc.ProgressError, match="account_in_use"):
        await _link(other["id"], bound["key"])
    dto = svc.context(db, viewer_user["id"])
    assert TOKEN not in json.dumps(dto) and "credentials_enc" not in json.dumps(dto)
    assert dto["capabilities"] == {"pull": True, "remote_write": False, "bidirectional": False, "scheduled": False}
    assert svc.context(db, other["id"])["accounts"] == []
    for call in remote.calls:
        assert call.request.headers["X-Emby-Token"] == TOKEN
        assert not call.request.url.params
        assert TOKEN not in str(call.request.url)
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_jellyfin_pull_exact_user_item_contract_and_idempotent_private_history(db, viewer_user, bound, remote):
    uid = viewer_user["id"]
    other = _create_user("other", "password123", "Other", "viewer")
    sibling = _item(db, "Print edition", "book")
    media_groups.link_items(db, bound["item"], sibling, link_type="format")
    db.commit()
    account = await _link(uid, bound["key"])
    before_item = dict(db.execute("SELECT * FROM items_live WHERE id = ?", (bound["item"],)).fetchone())
    assert (await svc.pull_item(uid, account, bound["item"]))["outcome"] == "accepted"
    first = _state(db, uid, bound["item"])
    assert first["status"] == "in_progress" and first["progress"] == 25
    assert first["started_at"] and first["finished_at"] is None
    assert _state(db, other["id"], bound["item"])["updated_at"] is None
    assert _state(db, uid, sibling)["updated_at"] is None
    assert dict(db.execute("SELECT * FROM items_live WHERE id = ?", (bound["item"],)).fetchone()) == before_item
    assert (await svc.pull_item(uid, account, bound["item"]))["outcome"] == "unchanged"
    assert _state(db, uid, bound["item"]) == first
    assert db.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 1
    event = db.execute("SELECT * FROM user_item_events").fetchone()
    assert event["event_type"] == "source_progress"
    assert json.loads(event["snapshot_json"])["source_progress"]["unit"] == "seconds"
    assert svc.context(db, uid, item_id=bound["item"])["pending"] == []
    for call in remote.calls:
        assert call.request.method == "GET" and call.request.headers["X-Emby-Token"] == TOKEN
        assert "admin-catalogue-user" not in str(call.request.url)
        assert "unused-server-catalogue-key" not in repr(call.request.headers)
        assert "authorization" not in call.request.headers
        assert not call.request.url.params


@pytest.mark.asyncio
async def test_completion_eof_and_reopen_require_explicit_flag_and_advancing_clock(db, viewer_user, bound, remote):
    uid, item = viewer_user["id"], bound["item"]
    account = await _link(uid, bound["key"])
    remote["progress"].respond(200, json=_jelly(position=1000))
    await svc.pull_item(uid, account, item)
    assert _state(db, uid, item)["progress"] == 99
    assert _state(db, uid, item)["status"] == "in_progress"
    remote["progress"].respond(200, json=_jelly(finished=True, clock="2026-01-02T10:00:00Z"))
    await svc.pull_item(uid, account, item)
    finished = _state(db, uid, item)
    assert finished["progress"] == 100 and finished["finished_at"]
    remote["progress"].respond(200, json=_jelly(position=0, clock="2026-01-02T10:00:00Z"))
    assert (await svc.pull_item(uid, account, item))["outcome"] == "conflict"
    pending = _pending(db, uid, item)
    assert pending["reason"] == "non_advancing_clock"
    assert _state(db, uid, item) == finished
    _review(db, uid, pending)
    reopened = _state(db, uid, item)
    assert reopened["status"] == "planned" and reopened["progress"] == 0 and reopened["finished_at"] is None
    assert reopened["started_at"] == finished["started_at"]
    with pytest.raises(svc.ProgressError, match="stale_review"):
        _review(db, uid, pending)


@pytest.mark.asyncio
@pytest.mark.parametrize("local_action", ["save", "reset"])
async def test_manual_edits_and_reset_win_unless_owner_reviews(db, viewer_user, bound, remote, local_action):
    uid, item = viewer_user["id"], bound["item"]
    account = await _link(uid, bound["key"])
    await svc.pull_item(uid, account, item)
    if local_action == "save":
        _save(db, uid, item, status="paused", progress="80", notes="private note", rating="5", favourite="1")
    else:
        db.execute("BEGIN IMMEDIATE")
        personal_library.reset(db, uid, item)
        db.commit()
    before = _state(db, uid, item)
    remote["progress"].respond(200, json=_jelly(position=500, clock="2026-01-03T10:00:00Z"))
    assert (await svc.pull_item(uid, account, item))["outcome"] == "conflict"
    pending = _pending(db, uid, item)
    assert pending["reason"] == "local_changed"
    assert _state(db, uid, item) == before
    await svc.pull_item(uid, account, item)
    assert _pending(db, uid, item)["review_token"] == pending["review_token"]
    _review(db, uid, pending, "kept_local")
    assert _state(db, uid, item) == before
    assert (await svc.pull_item(uid, account, item))["outcome"] == "unchanged"
    # Keeping one old observation is not permission to replace a newer manual
    # edit with a slightly-less-old remote observation on the next pull.
    remote["progress"].respond(200, json=_jelly(position=600, clock="2026-01-04T10:00:00Z"))
    assert (await svc.pull_item(uid, account, item))["outcome"] == "conflict"
    assert _pending(db, uid, item)["reason"] == "local_newer"
    _review(db, uid, _pending(db, uid, item))
    after = _state(db, uid, item)
    assert after["progress"] == 60
    for name in ("notes", "rating", "favourite", "on_list"):
        assert after[name] == before[name]


@pytest.mark.asyncio
async def test_first_pull_preserves_existing_state_and_unknown_clock_needs_review(db, viewer_user, bound, remote):
    uid, item = viewer_user["id"], bound["item"]
    _save(db, uid, item, status="in_progress", progress="90")
    account = await _link(uid, bound["key"])
    assert (await svc.pull_item(uid, account, item))["outcome"] == "conflict"
    assert _pending(db, uid, item)["reason"] == "existing_local_state"
    remote["progress"].respond(200, json=_jelly(clock=None))
    await svc.pull_item(uid, account, item)
    assert _state(db, uid, item)["progress"] == 90
    _review(db, uid, _pending(db, uid, item))
    assert _state(db, uid, item)["progress"] == 25
    # Next observation has no clock too: explicit acceptance wasn't blanket consent.
    remote["progress"].respond(200, json=_jelly(position=300, clock=None))
    await svc.pull_item(uid, account, item)
    assert _pending(db, uid, item)["reason"] == "unknown_clock"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["local", "reset", "binding", "source", "relink", "trash", "other_owner"])
async def test_review_cas_rejects_changed_local_binding_root_linkage_or_owner(db, viewer_user, bound, remote, change):
    uid, item = viewer_user["id"], bound["item"]
    other = _create_user("other", "password123", "Other", "viewer")
    _save(db, uid, item, status="paused", progress="75")
    account = await _link(uid, bound["key"])
    await svc.pull_item(uid, account, item)
    pending = _pending(db, uid, item)
    if change == "local":
        _save(db, uid, item, status="planned", progress="4")
    elif change == "reset":
        personal_library.reset(db, uid, item)
    elif change == "binding":
        db.execute("UPDATE source_records SET external_id = 'other-remote' WHERE id = ?", (bound["record"],))
    elif change == "source":
        db.execute("UPDATE source_instances SET base_url = 'https://other.example' WHERE id = ?", (bound["source"],))
    elif change == "relink":
        await _link(uid, bound["key"])
    elif change == "trash":
        item_write.trash_item(db, item)
    else:
        uid = other["id"]
    db.commit()
    before = _state(db, viewer_user["id"], item)
    with pytest.raises(svc.ProgressError, match="stale_review|not_found|relink_required"):
        _review(db, uid, pending)
    db.rollback()
    assert _state(db, viewer_user["id"], item) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "binding", "disconnect", "disable", "trash", "local"])
async def test_inflight_pull_rechecks_under_write_lock(db, viewer_user, bound, remote, change):
    uid, item = viewer_user["id"], bound["item"]
    account = await _link(uid, bound["key"])

    def racing_response(request):
        with get_db() as rival:
            rival.execute("BEGIN IMMEDIATE")  # No network/decryption under our write lock.
            if change == "source":
                rival.execute("UPDATE source_instances SET base_url = 'https://other.example' WHERE id = ?", (bound["source"],))
            elif change == "binding":
                rival.execute("UPDATE source_records SET external_id = 'other-remote' WHERE id = ?", (bound["record"],))
            elif change == "disconnect":
                svc.disconnect(rival, uid, account)
            elif change == "disable":
                rival.execute("UPDATE settings SET value = '0' WHERE key = 'feature.connected_library'")
                set_setting(rival, "feature.connected_library", "0")
            elif change == "trash":
                item_write.trash_item(rival, item)
            else:
                personal_library.save(rival, uid, item, personal_library.parse_form({"status": "paused", "progress": "88"}))
        return httpx.Response(200, json=_jelly())

    remote["progress"].mock(side_effect=racing_response)
    if change == "local":
        assert (await svc.pull_item(uid, account, item))["outcome"] == "conflict"
        assert _state(db, uid, item)["progress"] == 88
    else:
        with pytest.raises(svc.ProgressError, match="stale_config|not_found|disabled|relink_required"):
            await svc.pull_item(uid, account, item)
        assert db.execute("SELECT COUNT(*) FROM user_source_progress").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_abs_self_contract_audio_units_and_completion(db, viewer_user, remote):
    uid = viewer_user["id"]
    set_setting(db, "abs_url", ABS_ROOT)
    set_setting(db, "abs_token", "never-read-this-server-key")
    item = _item(db, "Audio", "audiobook", abs_id="remote-item")
    ebook = _item(db, "Ebook", "ebook", abs_id="ebook-item")
    db.commit()
    account = await _link(uid, "abs")
    await svc.pull_item(uid, account, item)
    assert _state(db, uid, item)["progress"] == 25
    remote["abs_progress"].respond(200, json=_abs(finished=True, clock=1767348000000))
    await svc.pull_item(uid, account, item)
    assert _state(db, uid, item)["progress"] == 100 and _state(db, uid, item)["status"] == "finished"
    with pytest.raises(svc.ProgressError, match="unsupported"):
        await svc.pull_item(uid, account, ebook)
    for call in remote.calls:
        assert call.request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(call.request.url) and not call.request.url.params
        assert call.request.method == "GET"


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [
    {"userId": "someone-else"}, {"libraryItemId": "another-item"}, {"episodeId": "episode"},
    {"episodeId": []}, {"currentTime": -1}, {"currentTime": 1001}, {"duration": 0},
    {"duration": "1000"}, {"currentTime": True}, {"isFinished": 1}, {"progress": 1.1},
    {"lastUpdate": "2026-01-01"}, {"lastUpdate": 10**30},
])
async def test_abs_malformed_or_other_user_progress_never_changes_state(db, viewer_user, remote, values):
    set_setting(db, "abs_url", ABS_ROOT)
    item = _item(db, "Audio", "audiobook", abs_id="remote-item")
    db.commit()
    account = await _link(viewer_user["id"], "abs")
    remote["abs_progress"].respond(200, json=_abs(**values))
    with pytest.raises(svc.ProgressError, match="malformed_response"):
        await svc.pull_item(viewer_user["id"], account, item)
    assert db.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM user_source_progress").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    _jelly(position=-1), _jelly(position=1001), _jelly(total=0), _jelly(finished="true"),
    _jelly(clock="not-a-timestamp"), _jelly(clock="2099-01-01T00:00:00Z"),
    _jelly(Id="wrong-id"), _jelly(Type="Series"), _jelly(UserData={}),
    _jelly(RunTimeTicks=1.0), _jelly(UserData={"Played": False, "PlaybackPositionTicks": True}),
    _jelly(RunTimeTicks=10**1000),
])
async def test_jellyfin_units_bounds_and_shapes_are_not_coerced(db, viewer_user, bound, remote, payload):
    account = await _link(viewer_user["id"], bound["key"])
    remote["progress"].respond(200, json=payload)
    with pytest.raises(svc.ProgressError, match="malformed_response"):
        await svc.pull_item(viewer_user["id"], account, bound["item"])
    assert db.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(302, "unsafe_redirect"), (401, "auth_failed"), (403, "auth_failed"), (404, "not_found"), (500, "http_error")])
async def test_remote_failure_is_safe_and_never_infers_a_reset(db, viewer_user, bound, remote, status, expected):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])
    await svc.pull_item(uid, account, bound["item"])
    before = _state(db, uid, bound["item"])
    remote["progress"].respond(status, headers={"location": f"https://other.example/?token={TOKEN}"}, text=TOKEN)
    with pytest.raises(svc.ProgressError, match=expected) as error:
        await svc.pull_item(uid, account, bound["item"])
    assert TOKEN not in str(error.value)
    assert _state(db, uid, bound["item"]) == before


@pytest.mark.asyncio
async def test_disconnect_retains_imported_state_and_history_and_erases_token_and_review(db, viewer_user, bound, remote):
    uid, item = viewer_user["id"], bound["item"]
    account = await _link(uid, bound["key"])
    await svc.pull_item(uid, account, item)
    before = _state(db, uid, item)
    db.execute("BEGIN IMMEDIATE")
    svc.disconnect(db, uid, account)
    db.commit()
    assert _state(db, uid, item) == before
    assert not db.execute("SELECT * FROM user_source_accounts").fetchall()
    assert not db.execute("SELECT * FROM user_source_progress").fetchall()
    assert len(svc.context(db, uid, item_id=item)["history"]) == 1
    assert (await _link(uid, bound["key"])) != account


@pytest.mark.asyncio
async def test_state_baseline_and_both_histories_rollback_together(db, viewer_user, bound, remote, monkeypatch):
    account = await _link(viewer_user["id"], bound["key"])

    def fail(*args, **kwargs):
        raise RuntimeError("forced event failure")

    monkeypatch.setattr(personal_library, "_record", fail)
    with pytest.raises(RuntimeError, match="forced event failure"):
        await svc.pull_item(viewer_user["id"], account, bound["item"])
    for table in ("user_source_progress", "user_item_state", "user_item_events", "user_source_progress_events"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_routes_owner_link_pull_review_disconnect_and_csrf(f4_client, db, viewer_user, bound, remote):
    uid, item = viewer_user["id"], bound["item"]
    headers = {"Accept": "application/json"}
    fields = {"source_key": bound["key"], "remote_user_id": REMOTE_USER, "token": TOKEN, "consent": "pull_v1", "user_id": "9999"}
    response = f4_client.post("/api/source-progress/accounts/link", data=fields, headers=headers)
    assert response.status_code == 200
    account = response.json()["account_id"]
    assert db.execute("SELECT user_id FROM user_source_accounts").fetchone()[0] == uid
    _save(db, uid, item, status="paused", progress="90")
    pull_url = f"/api/source-progress/accounts/{account}/items/{item}/pull"
    assert f4_client.post(pull_url, data={"_csrf": _TEST_CSRF_TOKEN}, headers=headers).json()["code"] == "conflict"
    page = f4_client.get(f"/account/source-progress?item_id={item}")
    assert page.status_code == 200 and "Conflict previews" in page.text and "90%" in page.text
    assert "25%" in page.text and "Replace local progress" in page.text
    assert TOKEN not in page.text and "credentials_enc" not in page.text
    assert "No automatic sync or remote writeback" in page.text and 'name="_csrf"' in page.text
    pending = _pending(db, uid, item)
    review_url = f"/api/source-progress/reviews/{pending['id']}"
    fields = {"review_token": pending["review_token"], "decision": "accepted"}
    assert f4_client.post(review_url, data=fields, headers={**headers, "X-CSRF-Token": ""}).status_code == 403
    assert f4_client.post(review_url, data=fields, headers=headers).status_code == 200
    assert _state(db, uid, item)["progress"] == 25
    other = _create_user("other", "password123", "Other", "viewer")
    from tests.test_personal_library import _login
    _login(f4_client, other)
    assert f4_client.post(pull_url, data={"_csrf": _TEST_CSRF_TOKEN}, headers=headers).status_code == 404
    assert f4_client.post(f"/api/source-progress/accounts/{account}/disconnect", data={"_csrf": _TEST_CSRF_TOKEN}, headers=headers).status_code == 404
    assert "my-remote-user" not in f4_client.get("/account/source-progress").text
    assert "My film" not in f4_client.get("/account/source-progress").text


@pytest.mark.parametrize("url", ["/api/source-progress/accounts/link", "/api/source-progress/accounts/1/disconnect",
                                  "/api/source-progress/accounts/1/items/1/pull", "/api/source-progress/reviews/1"])
def test_all_progress_mutations_require_csrf_and_connected_feature(f4_client, db, url):
    assert f4_client.post(url, data={"token": TOKEN}, headers={"X-CSRF-Token": ""}).status_code == 403
    set_setting(db, "feature.connected_library", "0")
    db.commit()
    assert f4_client.post(url, data={"token": TOKEN}).status_code == 403
    response = f4_client.get("/account/source-progress")
    assert 'data-testid="feature-disabled"' in response.text


def test_schema_bootstrap_is_additive_and_foreign_keys_purge_account_only(db, viewer_user, bound):
    from app.database import init_db

    init_db()
    init_db()
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert set(row[1] for row in db.execute("PRAGMA table_info(user_source_accounts)")) >= {"credentials_enc", "consent_version", "root_fingerprint"}


@pytest.mark.asyncio
async def test_abs_scoped_me_endpoint_accepts_version_without_user_id(db, viewer_user, remote):
    set_setting(db, "abs_url", ABS_ROOT)
    item = _item(db, "Audio", "audiobook", abs_id="remote-item")
    db.commit()
    payload = _abs()
    del payload["userId"]
    remote["abs_progress"].respond(200, json=payload)
    account = await _link(viewer_user["id"], "abs")
    assert (await svc.pull_item(viewer_user["id"], account, item))["outcome"] == "accepted"


@pytest.mark.asyncio
async def test_link_root_change_during_self_check_cannot_store_a_misdirected_token(db, viewer_user, bound, remote):
    def move_source(request):
        with get_db() as rival:
            rival.execute("BEGIN IMMEDIATE")
            rival.execute("UPDATE source_instances SET base_url = 'https://other.example' WHERE id = ?", (bound["source"],))
        return httpx.Response(200, json={"Id": REMOTE_USER})

    remote["me"].mock(side_effect=move_source)
    with pytest.raises(svc.ProgressError, match="stale_config"):
        await _link(viewer_user["id"], bound["key"])
    assert db.execute("SELECT COUNT(*) FROM user_source_accounts").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["limit_header", "limit_body", "encoding", "malformed", "timeout", "network", "identity"])
async def test_response_limits_safe_failures_and_identity_recheck(db, viewer_user, bound, remote, monkeypatch, failure):
    account = await _link(viewer_user["id"], bound["key"])
    monkeypatch.setattr(svc, "MAX_RESPONSE_BYTES", 256)
    expected = "malformed_response"
    if failure == "limit_header":
        remote["progress"].respond(200, headers={"content-length": "1000"}, content=b"{}")
        expected = "too_large"
    elif failure == "limit_body":
        remote["progress"].respond(200, content=b"x" * 257)
        expected = "too_large"
    elif failure == "encoding":
        remote["progress"].respond(200, headers={"content-encoding": "br"})
    elif failure == "malformed":
        remote["progress"].respond(200, content=b"not JSON: " + TOKEN.encode())
    elif failure == "timeout":
        remote["progress"].mock(side_effect=httpx.ReadTimeout(TOKEN))
        expected = "timeout"
    elif failure == "network":
        remote["progress"].mock(side_effect=httpx.ConnectError(TOKEN))
        expected = "unreachable"
    else:
        remote["me"].respond(200, json={"Id": "another-user"})
        expected = "identity_mismatch"
    with pytest.raises(svc.ProgressError, match=expected) as error:
        await svc.pull_item(viewer_user["id"], account, bound["item"])
    assert TOKEN not in str(error.value)
    assert db.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_unknown_clock_even_on_fresh_item_cannot_silently_create_progress(db, viewer_user, bound, remote):
    account = await _link(viewer_user["id"], bound["key"])
    remote["progress"].respond(200, json=_jelly(clock=None))
    assert (await svc.pull_item(viewer_user["id"], account, bound["item"]))["outcome"] == "conflict"
    assert _state(db, viewer_user["id"], bound["item"])["updated_at"] is None
    assert _pending(db, viewer_user["id"], bound["item"])["reason"] == "unknown_clock"


@pytest.mark.asyncio
async def test_unsupported_formats_ambiguous_binding_and_abs_feature_refuse_before_network(db, viewer_user, bound, remote):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])
    series = _item(db, "Series", "tv_series")
    _record(db, bound["source"], series, external_id="series", media_type="tv_series")
    _record(db, bound["source"], bound["item"], external_id="duplicate", media_type="digital_video")
    db.commit()
    with pytest.raises(svc.ProgressError, match="unsupported"):
        await svc.pull_item(uid, account, series)
    with pytest.raises(svc.ProgressError, match="ambiguous_binding"):
        await svc.pull_item(uid, account, bound["item"])
    set_setting(db, "abs_url", ABS_ROOT)
    set_setting(db, "feature.abs_sync", "0")
    db.commit()
    with pytest.raises(svc.ProgressError, match="disabled"):
        await _link(uid, "abs")
    assert len(remote.calls) == 1  # Only the original Jellyfin link self-check.


def test_gets_are_local_only_and_anonymous_cannot_view_accounts(f4_client, db, bound, remote):
    assert f4_client.get("/account/source-progress").status_code == 200
    assert f4_client.get(f"/api/library/items/{bound['item']}/handoff").status_code == 200
    assert len(remote.calls) == 0
    f4_client.cookies.delete("access_token")
    assert f4_client.get("/account/source-progress", follow_redirects=False).status_code == 303


def test_repeated_and_json_fields_refused_before_linking(f4_client, remote):
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    response = f4_client.post("/api/source-progress/accounts/link", content="consent=pull_v1&consent=pull_v1", headers=headers)
    assert response.status_code == 422 and response.json()["code"] == "invalid_form"
    assert f4_client.post("/api/source-progress/accounts/link", json={"token": TOKEN}, headers={"Accept": "application/json"}).status_code == 422
    assert len(remote.calls) == 0


@pytest.mark.asyncio
async def test_owner_history_and_conflict_pagination_are_nonmutating_and_complete(db, viewer_user, bound, remote, monkeypatch):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])
    observation = svc._normalise({"provider": "jellyfin"}, {"external_id": "remote-item", "media_type": "digital_video"}, REMOTE_USER, _jelly())
    for number in range(102):
        item = _item(db, f"Item {number}", "digital_video")
        db.execute(
            "INSERT INTO user_source_progress (account_id, item_id, binding_hash, remote_json, review_token, reason, observed_at) "
            "VALUES (?, ?, 'binding', ?, 'local-review-token', 'existing_local_state', '2026-01-01')",
            (account, item, svc._json(observation)),
        )
        db.execute(
            "INSERT INTO user_source_progress_events (user_id, item_id, source_key, decision, snapshot_json, event_key) "
            "VALUES (?, ?, ?, 'kept_local', ?, ?)",
            (uid, item, bound["key"], svc._json({"remote": observation}), str(number)),
        )
    db.commit()

    def no_decryption(*args, **kwargs):
        pytest.fail("GET context must never decrypt a remote token")

    monkeypatch.setattr(crypto, "decrypt_value", no_decryption)
    db.execute("PRAGMA query_only = ON")
    try:
        first = svc.context(db, uid)
        second = svc.context(db, uid, review_before=first["next_review_id"])
        assert len(first["pending"]) == 100 and len(second["pending"]) == 2
        assert not {row["id"] for row in first["pending"]} & {row["id"] for row in second["pending"]}
        events, cursor = [], None
        while True:
            page = svc.context(db, uid, history_before=cursor)
            events.extend(event["id"] for event in page["history"])
            cursor = page["next_history_id"]
            if cursor is None:
                break
        assert len(events) == len(set(events)) == 102
    finally:
        db.execute("PRAGMA query_only = OFF")


@pytest.mark.asyncio
async def test_merge_hook_preserves_external_history_and_invalidates_progress_baselines(db, viewer_user, bound, remote):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])
    await svc.pull_item(uid, account, bound["item"])
    kept = _item(db, "Kept", "digital_video")
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    personal_library.reparent(db, kept, bound["item"])
    svc.reparent(db, kept, bound["item"])
    db.execute("DELETE FROM items WHERE id = ?", (bound["item"],))
    db.commit()
    assert _state(db, uid, kept)["progress"] == 25
    assert db.execute("SELECT COUNT(*) FROM user_source_progress").fetchone()[0] == 0
    history = svc.context(db, uid, item_id=kept)["history"]
    assert len(history) == 1 and history[0]["item_id"] == kept
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
