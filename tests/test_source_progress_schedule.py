"""Explicit automatic pulls: consent, durable baselines/leases, bounded batches."""

import asyncio
from datetime import datetime

import httpx
import pytest

from app.database import get_db, set_setting
from app.services import source_progress as svc
from tests.conftest import _create_user
from tests.test_availability import _item, _record
from tests.test_personal_library import _save
from tests.test_source_handoff import f4_client  # noqa: F401
from tests.test_source_progress import (  # noqa: F401 — isolated fixtures
    ABS_ROOT, REMOTE_USER, ROOT, TOKEN, _jelly, _link, _pending, _review, _state, bound, remote,
)
from tests.test_source_progress_review import _change_scope


def _enable(db, uid, account, minutes=15):
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    svc.set_schedule(db, uid, account, interval_minutes=minutes, consent=svc.SCHEDULE_CONSENT_VERSION)
    db.commit()
    row = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()
    return dict(row) if row else None


def _due(db, account):
    db.execute("UPDATE user_source_schedules SET next_due_at = '2000-01-01T00:00:00.000000+00:00' WHERE account_id = ?", (account,))
    db.commit()


@pytest.mark.asyncio
async def test_manual_link_is_not_automatic_pull_consent(db, viewer_user, bound, remote):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])
    assert svc.next_due() is None
    assert (await svc.run_scheduled_pass())["accounts"] == 0
    assert len(remote.calls) == 1
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(svc.ProgressError, match="consent_required"):
        svc.set_schedule(db, uid, account, interval_minutes=15, consent="pull_v1")
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM user_source_schedules").fetchone()[0] == 0
    assert _state(db, uid, bound["item"])["updated_at"] is None


@pytest.mark.asyncio
async def test_automatic_pull_uses_manual_baselines_conflicts_history_and_idempotence(db, viewer_user, bound, remote):
    uid, item = viewer_user["id"], bound["item"]
    other = _create_user("other-scheduled", "password123", "Other", "viewer")
    account = await _link(uid, bound["key"])
    schedule = _enable(db, uid, account)
    assert svc.next_due() == schedule["next_due_at"]
    result = await svc.run_scheduled_pass()
    assert result["ok"] and result["accounts"] == result["attempted"] == result["accepted"] == 1
    assert _state(db, uid, item)["progress"] == 25
    assert _state(db, other["id"], item)["updated_at"] is None
    assert (await svc.run_scheduled_pass())["accounts"] == 0
    _due(db, account)
    assert (await svc.run_scheduled_pass())["unchanged"] == 1
    assert db.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 1
    _save(db, uid, item, status="paused", progress="90", notes="local-only", rating="5")
    remote["progress"].respond(200, json=_jelly(position=500, clock="2026-01-03T10:00:00Z"))
    _due(db, account)
    result = await svc.run_scheduled_pass()
    assert result["conflict"] == 1 and _state(db, uid, item)["progress"] == 90
    pending = _pending(db, uid, item)
    assert pending["reason"] == "local_changed"
    _due(db, account)
    assert (await svc.run_scheduled_pass())["conflict"] == 1
    assert _pending(db, uid, item)["review_token"] == pending["review_token"]
    _review(db, uid, pending, "kept_local")
    _due(db, account)
    assert (await svc.run_scheduled_pass())["unchanged"] == 1
    assert _state(db, uid, item)["progress"] == 90 and _state(db, uid, item)["notes"] == "local-only"
    assert all(call.request.method == "GET" for call in remote.calls)
    assert TOKEN not in str(svc.context(db, uid))


@pytest.mark.asyncio
async def test_automatic_refresh_cannot_undo_reset_or_import_changed_progress_without_review(db, viewer_user, bound, remote):
    from app.services import personal_library

    uid, item = viewer_user["id"], bound["item"]
    account = await _link(uid, bound["key"])
    _enable(db, uid, account)
    assert (await svc.run_scheduled_pass())["accepted"] == 1
    db.execute("BEGIN IMMEDIATE")
    personal_library.reset(db, uid, item)
    db.commit()
    _due(db, account)
    assert (await svc.run_scheduled_pass())["unchanged"] == 1
    assert _state(db, uid, item)["updated_at"] is None
    remote["progress"].respond(200, json=_jelly(position=500, clock="2026-01-03T10:00:00Z"))
    _due(db, account)
    assert (await svc.run_scheduled_pass())["conflict"] == 1
    assert _state(db, uid, item)["updated_at"] is None
    assert _pending(db, uid, item)["reason"] == "local_changed"


@pytest.mark.asyncio
async def test_partial_job_failure_backs_off_and_resumes_without_duplicate_imports(db, viewer_user, bound, remote):
    uid = viewer_user["id"]
    second = _item(db, "Second", "digital_video")
    _record(db, bound["source"], second, external_id="second", media_type="digital_video")
    db.commit()
    progress = remote.get(f"{ROOT}/Users/{REMOTE_USER}/Items/second").respond(401, text=TOKEN)
    account = await _link(uid, bound["key"])
    _enable(db, uid, account)
    result = await svc.run_scheduled_pass()
    assert result["accepted"] == result["errors"] == 1 and not result["ok"]
    row = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()
    assert row["last_error"] == "auth_failed" and row["last_status"] == "partial"
    assert row["cursor_item_id"] == bound["item"]
    assert (datetime.fromisoformat(row["next_due_at"]) - datetime.fromisoformat(row["last_finished_at"])).total_seconds() == 900
    assert TOKEN not in str(dict(row))
    progress.respond(200, json=_jelly(Id="second"))
    _due(db, account)
    assert (await svc.run_scheduled_pass())["accepted"] == 1
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 2
    _due(db, account)
    assert (await svc.run_scheduled_pass())["unchanged"] == 2
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_bounded_batches_have_durable_cursor_and_continue_without_silent_truncation(db, viewer_user, bound, remote):
    uid = viewer_user["id"]
    items = [bound["item"]]
    for external in ("second", "third"):
        item = _item(db, external, "digital_video")
        _record(db, bound["source"], item, external_id=external, media_type="digital_video")
        remote.get(f"{ROOT}/Users/{REMOTE_USER}/Items/{external}").respond(200, json=_jelly(Id=external))
        items.append(item)
    db.commit()
    account = await _link(uid, bound["key"])
    _enable(db, uid, account)
    for index, item in enumerate(items):
        if index:
            _due(db, account)
        result = await svc.run_scheduled_pass(max_accounts=1, max_items=1)
        assert result["attempted"] == result["accepted"] == 1
        row = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()
        seconds = (datetime.fromisoformat(row["next_due_at"]) - datetime.fromisoformat(row["last_finished_at"])).total_seconds()
        assert row["cursor_item_id"] == (item if index < 2 else 0)
        assert seconds == (svc.SCHEDULE_BATCH_GAP_SECONDS if index < 2 else 15 * 60)
        assert _state(db, uid, item)["progress"] == 25
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 3
    assert (await svc.run_scheduled_pass())["attempted"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disable", "new_consent", "disconnect", "relink", "scope", "actor", "lease"])
async def test_inflight_schedule_cannot_apply_after_its_authority_changes(db, viewer_user, bound, remote, change):
    uid, item = viewer_user["id"], bound["item"]
    account = await _link(uid, bound["key"])
    _enable(db, uid, account)
    started, release = asyncio.Event(), asyncio.Event()

    async def hold(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=_jelly())

    remote["progress"].mock(side_effect=hold)
    task = asyncio.create_task(svc.run_scheduled_pass(max_accounts=1))
    await asyncio.wait_for(started.wait(), 5)
    current = dict(db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone())
    assert current["lease_token"] and svc.next_due() == current["lease_expires_at"]
    assert (await svc.run_scheduled_pass())["accounts"] == 0
    if change == "disable":
        _enable(db, uid, account, 0)
    elif change == "new_consent":
        _enable(db, uid, account, 60)
    elif change == "relink":
        await _link(uid, bound["key"])
    else:
        with get_db() as rival:
            rival.execute("BEGIN IMMEDIATE")
            if change == "disconnect":
                svc.disconnect(rival, uid, account)
            elif change == "scope":
                _change_scope(rival, bound["source"])
            elif change == "actor":
                rival.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (uid,))
            else:
                rival.execute("UPDATE user_source_schedules SET lease_token = 'new-lease-owner' WHERE account_id = ?", (account,))
    after_change = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()
    after_change = dict(after_change) if after_change else None
    release.set()
    result = await task
    assert not result["ok"] and result["accepted"] == 0 and result["errors"] == 1
    assert _state(db, uid, item)["updated_at"] is None
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 0
    final = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()
    if change in {"disable", "new_consent", "disconnect", "relink", "lease"}:
        assert (dict(final) if final else None) == after_change
    if change == "disconnect":
        assert db.execute("SELECT COUNT(*) FROM user_source_accounts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_expired_lease_recovers_and_old_worker_is_fenced(db, viewer_user, bound, remote):
    uid, item = viewer_user["id"], bound["item"]
    account = await _link(uid, bound["key"])
    _enable(db, uid, account)
    _, stale = svc._claim_schedule()  # Simulate process loss, with no in-memory task.
    db.execute("UPDATE user_source_schedules SET lease_expires_at = '2000-01-01T00:00:00.000000+00:00' WHERE account_id = ?", (account,))
    db.commit()
    assert (await svc.run_scheduled_pass())["accepted"] == 1
    with pytest.raises(svc.ProgressError, match="stale_schedule"):
        await svc.pull_item(uid, account, item, _lease=stale)
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_budget_timeout_releases_job_without_partial_current_item_write(db, viewer_user, bound, remote):
    uid, item = viewer_user["id"], bound["item"]
    account = await _link(uid, bound["key"])
    _enable(db, uid, account)

    async def hold(request):
        await asyncio.Event().wait()

    remote["progress"].mock(side_effect=hold)
    result = await svc.run_scheduled_pass(max_seconds=0.1)
    assert not result["ok"] and result["errors"] == 1 and result["accepted"] == 0
    row = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()
    assert row["last_status"] == "interrupted" and row["last_error"] == "timeout" and row["lease_token"] is None
    assert _state(db, uid, item)["updated_at"] is None


@pytest.mark.asyncio
async def test_cancellation_records_interruption_releases_lease_and_reraises(db, viewer_user, bound, remote):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])
    _enable(db, uid, account)
    started = asyncio.Event()

    async def hold(request):
        started.set()
        await asyncio.Event().wait()

    remote["progress"].mock(side_effect=hold)
    task = asyncio.create_task(svc.run_scheduled_pass())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()
    assert row["lease_token"] is None and row["last_status"] == "interrupted" and row["last_error"] == "interrupted"
    assert db.execute("SELECT COUNT(*) FROM user_item_events").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["connected_library", "source", "actor"])
async def test_gates_and_revoked_scheduled_authority_do_no_network_work(db, viewer_user, bound, remote, gate):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])
    _enable(db, uid, account)
    if gate == "connected_library":
        set_setting(db, "feature.connected_library", "0")
    elif gate == "source":
        db.execute("UPDATE source_instances SET enabled = 0 WHERE id = ?", (bound["source"],))
    else:
        db.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (uid,))
    db.commit()
    assert svc.next_due() is None
    assert (await svc.run_scheduled_pass())["accounts"] == 0
    assert len(remote.calls) == 1
    if gate == "actor":
        assert not svc.context(db, uid)["accounts"][0]["schedule"]["authorization_current"]
        _enable(db, uid, account)
        assert (await svc.run_scheduled_pass())["accepted"] == 1


@pytest.mark.asyncio
async def test_abs_automatic_pull_uses_personal_token_and_obeys_abs_feature(db, viewer_user, remote):
    uid = viewer_user["id"]
    set_setting(db, "abs_url", ABS_ROOT)
    set_setting(db, "abs_token", "unused-server-wide-token")
    item = _item(db, "Audio", "audiobook", abs_id="remote-item")
    db.commit()
    account = await _link(uid, "abs")
    _enable(db, uid, account)
    set_setting(db, "feature.abs_sync", "0")
    db.commit()
    assert svc.next_due() is None and (await svc.run_scheduled_pass())["attempted"] == 0
    set_setting(db, "feature.abs_sync", "1")
    db.commit()
    assert (await svc.run_scheduled_pass())["accepted"] == 1
    assert _state(db, uid, item)["progress"] == 25
    assert all(call.request.headers["Authorization"] == f"Bearer {TOKEN}" for call in remote.calls)


def test_schedule_native_form_owner_csrf_consent_and_honest_worker_status(f4_client, db, viewer_user, bound, remote):
    headers = {"Accept": "application/json"}
    fields = {"source_key": bound["key"], "remote_user_id": REMOTE_USER, "token": TOKEN, "consent": "pull_v1"}
    account = f4_client.post("/api/source-progress/accounts/link", data=fields, headers=headers).json()["account_id"]
    path = f"/api/source-progress/accounts/{account}/schedule"
    assert f4_client.post(path, data={"interval_minutes": "15"}, headers=headers).json()["code"] == "consent_required"
    fields = {"interval_minutes": "15", "consent": "scheduled_pull_v1", "user_id": "99999"}
    assert f4_client.post(path, data=fields, headers={**headers, "X-CSRF-Token": ""}).status_code == 403
    assert f4_client.post(path, data=fields, headers=headers).status_code == 200
    row = db.execute("SELECT * FROM user_source_schedules").fetchone()
    assert row["consent_version"] == "scheduled_pull_v1"
    html = f4_client.get("/account/source-progress").text
    assert 'data-testid="progress-scheduler-inactive"' in html and "no automatic freshness" in html
    assert 'name="consent" value="scheduled_pull_v1"' in html and 'name="_csrf"' in html
    assert TOKEN not in html
    assert f4_client.post(path, data={"interval_minutes": "0"}, headers=headers).status_code == 200
    assert not db.execute("SELECT * FROM user_source_schedules").fetchall()
    other = _create_user("other-schedule", "password123", "Other", "viewer")
    from tests.test_personal_library import _login
    _login(f4_client, other)
    assert f4_client.post(path, data=fields, headers=headers).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"max_accounts": 4}, {"max_accounts": True}, {"max_items": 21}, {"max_items": 0}, {"max_seconds": 61}])
async def test_scheduler_cannot_be_called_with_unbounded_work(arguments):
    with pytest.raises(ValueError, match="budget"):
        await svc.run_scheduled_pass(**arguments)
