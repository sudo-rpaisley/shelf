"""ABS exclusions require known library identity, never a per-item remote guess."""

import json
import sqlite3

import pytest

from app.database import set_setting
from app.services import item_write, source_progress as svc
from tests.test_availability import _item
from tests.test_personal_library import _save
from tests.test_source_progress import ABS_ROOT, _link, _state, remote  # noqa: F401 — isolated HTTP fixture
from tests.test_source_progress_schedule import _due, _enable


def _exclusions(client, db, value):
    """Use the real admin configuration writer, not a hand-written scope row."""
    db.commit()
    response = client.post("/api/sync/audiobookshelf/libraries", json={"excluded": value})
    assert response.status_code == 200 and response.json()["ok"]
    assert json.loads(db.execute("SELECT value FROM settings WHERE key = 'abs_excluded_libraries'").fetchone()[0]) == value


def _progress_rows(db):
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in (
        "user_item_state", "user_item_events", "user_source_progress", "user_source_progress_events",
    )}


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["manual", "scheduled"])
async def test_legacy_unstamped_item_from_excluded_library_is_not_fetched(db, admin_client, admin_user, remote, flow):
    uid = admin_user["id"]
    set_setting(db, "abs_url", ABS_ROOT)
    item = _item(db, "Legacy excluded audio", "audiobook", abs_id="remote-item", abs_library_id=None)
    db.commit()
    _exclusions(admin_client, db, ["excluded-library"])
    # Remote truth is excluded, but Shelf's old row is unstamped. The fix must
    # refuse, NOT query either endpoint to infer membership from remote data.
    library = remote.get(f"{ABS_ROOT}/api/libraries/excluded-library/items").respond(
        200, json={"results": [{"id": "remote-item", "libraryId": "excluded-library"}]},
    )
    detail = remote.get(f"{ABS_ROOT}/api/items/remote-item").respond(
        200, json={"id": "remote-item", "libraryId": "excluded-library"},
    )
    account = await _link(uid, "abs")
    assert await _link(uid, "abs") == account  # A fresh relink is not proof of item scope.
    if flow == "scheduled":
        _enable(db, uid, account)
    before = _progress_rows(db)
    calls = len(remote.calls)
    if flow == "manual":
        with pytest.raises(svc.ProgressError, match="not_found"):
            await svc.pull_item(uid, account, item)
    else:
        result = await svc.run_scheduled_pass()
        assert result["accepted"] == result["conflict"] == result["unchanged"] == 0
    assert not remote["abs_progress"].called and not library.called and not detail.called
    assert len(remote.calls) == calls and _progress_rows(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["manual", "scheduled"])
@pytest.mark.parametrize("library_id", [
    None, "", " ", "\t", "bad id", "bad\nid", "bad\x00id", ".", "..", "x" * 201,
    " excluded-library ", "excluded-library", "allowed-library",
])
async def test_scoped_abs_requires_valid_known_allowed_library_before_http(db, admin_client, admin_user, remote, flow, library_id):
    uid = admin_user["id"]
    set_setting(db, "abs_url", ABS_ROOT)
    item = _item(db, "Scoped audio", "audiobook", abs_id="remote-item", abs_library_id=library_id)
    db.commit()
    _exclusions(admin_client, db, ["excluded-library"])
    allowed = library_id == "allowed-library"
    if not allowed:
        _save(db, uid, item, status="paused", progress="90", notes="Preserve local state/history")
    account = await _link(uid, "abs")
    if flow == "scheduled":
        _enable(db, uid, account)
    before, calls = _progress_rows(db), len(remote.calls)
    if flow == "manual":
        if allowed:
            assert (await svc.pull_item(uid, account, item))["outcome"] == "accepted"
        else:
            with pytest.raises(svc.ProgressError, match="not_found"):
                await svc.pull_item(uid, account, item)
    else:
        result = await svc.run_scheduled_pass()
        assert result["accepted"] == int(allowed)
        assert result["conflict"] == result["unchanged"] == 0
    if allowed:
        assert remote["abs_progress"].call_count == 1
        assert _state(db, uid, item)["progress"] == 25
        assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 1
    else:
        assert not remote["abs_progress"].called and len(remote.calls) == calls
        assert _progress_rows(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["manual", "scheduled"])
async def test_explicitly_unrestricted_scope_preserves_legacy_null_library_contract(db, admin_client, admin_user, remote, flow):
    uid = admin_user["id"]
    set_setting(db, "abs_url", ABS_ROOT)
    item = _item(db, "Unrestricted legacy audio", "audiobook", abs_id="remote-item", abs_library_id=None)
    db.commit()
    _exclusions(admin_client, db, [])
    account = await _link(uid, "abs")
    if flow == "manual":
        assert (await svc.pull_item(uid, account, item))["outcome"] == "accepted"
    else:
        _enable(db, uid, account)
        assert (await svc.run_scheduled_pass())["accepted"] == 1
    assert _state(db, uid, item)["progress"] == 25


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["", " ", "bad id", "bad\nid", "bad\x00id", ".", "..", "x" * 201])
async def test_malformed_exclusion_entry_fails_closed_for_link_pull_and_schedule(db, admin_client, admin_user, remote, entry):
    uid = admin_user["id"]
    set_setting(db, "abs_url", ABS_ROOT)
    item = _item(db, "Audio", "audiobook", abs_id="remote-item", abs_library_id=None)
    db.commit()
    _exclusions(admin_client, db, [])
    account = await _link(uid, "abs")
    _enable(db, uid, account)
    _exclusions(admin_client, db, [entry])  # The existing writer permits string entries.
    before, calls = _progress_rows(db), len(remote.calls)
    with pytest.raises(svc.ProgressError, match="relink_required"):
        await _link(uid, "abs")
    with pytest.raises(svc.ProgressError, match="relink_required"):
        await svc.pull_item(uid, account, item)
    with pytest.raises(svc.ProgressError, match="relink_required"):
        _enable(db, uid, account)
    db.rollback()
    result = await svc.run_scheduled_pass()
    assert result["errors"] == 1 and result["accepted"] == result["conflict"] == 0
    assert len(remote.calls) == calls and not remote["abs_progress"].called
    assert _progress_rows(db) == before


@pytest.mark.asyncio
async def test_scheduler_filters_unstamped_blank_and_excluded_before_batch_limit(db, admin_client, admin_user, remote):
    uid = admin_user["id"]
    set_setting(db, "abs_url", ABS_ROOT)
    for number, library in enumerate([None, "", " ", "excluded-library"] * 8):
        _item(db, str(number), "audiobook", abs_id=f"blocked-{number}", abs_library_id=library)
    allowed = _item(db, "Allowed", "audiobook", abs_id="remote-item", abs_library_id="allowed-library")
    db.commit()
    _exclusions(admin_client, db, ["excluded-library"])
    account = await _link(uid, "abs")
    _enable(db, uid, account)
    with svc.get_db() as connection:
        account_row, source = svc._account(connection, uid, account)
        assert svc._scheduled_items(connection, account_row, source, 0, 1) == [allowed]
    result = await svc.run_scheduled_pass(max_items=1)
    assert result["attempted"] == result["accepted"] == 1
    assert _state(db, uid, allowed)["progress"] == 25


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["manual", "scheduled"])
async def test_identified_allowed_library_can_resume_after_unstamped_refusal(db, admin_client, admin_user, remote, flow):
    uid = admin_user["id"]
    set_setting(db, "abs_url", ABS_ROOT)
    item = _item(db, "Previously unstamped", "audiobook", abs_id="remote-item", abs_library_id=None)
    db.commit()
    _exclusions(admin_client, db, ["excluded-library"])
    account = await _link(uid, "abs")
    if flow == "manual":
        with pytest.raises(svc.ProgressError, match="not_found"):
            await svc.pull_item(uid, account, item)
    else:
        _enable(db, uid, account)
        assert (await svc.run_scheduled_pass())["attempted"] == 0
    assert not remote["abs_progress"].called
    # A catalogue writer identifying the actual allowed library, not a guessed
    # item-detail/progress response, supplies the missing binding fact.
    item_write.update_item_fields(db, item, {"abs_library_id": "allowed-library"})
    db.commit()
    if flow == "manual":
        assert (await svc.pull_item(uid, account, item))["outcome"] == "accepted"
    else:
        _due(db, account)
        assert (await svc.run_scheduled_pass())["accepted"] == 1
    assert remote["abs_progress"].call_count == 1
    assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_large_real_exclusion_list_uses_bounded_sql_parameters(db, admin_client, admin_user, remote):
    uid = admin_user["id"]
    set_setting(db, "abs_url", ABS_ROOT)
    allowed = _item(db, "Allowed", "audiobook", abs_id="remote-item", abs_library_id="allowed-library")
    db.commit()
    _exclusions(admin_client, db, [f"excluded-{number}" for number in range(1000)])
    account = await _link(uid, "abs")
    previous = db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)
    try:
        account_row, source = svc._account(db, uid, account)
        assert svc._scheduled_items(db, account_row, source, 0, 1) == [allowed]
    finally:
        db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [None, 123, {"id": "excluded-library"}])
async def test_real_writer_rejects_nonstring_entries_without_widening_unknown_scope(db, admin_client, admin_user, remote, entry):
    uid = admin_user["id"]
    set_setting(db, "abs_url", ABS_ROOT)
    item = _item(db, "Unstamped", "audiobook", abs_id="remote-item", abs_library_id=None)
    db.commit()
    _exclusions(admin_client, db, ["excluded-library"])
    response = admin_client.post("/api/sync/audiobookshelf/libraries", json={"excluded": [entry]})
    assert response.status_code == 200 and not response.json()["ok"]
    assert json.loads(db.execute("SELECT value FROM settings WHERE key = 'abs_excluded_libraries'").fetchone()[0]) == ["excluded-library"]
    account = await _link(uid, "abs")
    _enable(db, uid, account)
    before, calls = _progress_rows(db), len(remote.calls)
    with pytest.raises(svc.ProgressError, match="not_found"):
        await svc.pull_item(uid, account, item)
    assert (await svc.run_scheduled_pass())["accepted"] == 0
    assert len(remote.calls) == calls and _progress_rows(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    pytest.param("", id="blank"), pytest.param(None, id="sql-null"),
    pytest.param(" \t\n", id="whitespace"), pytest.param("null", id="json-null"),
    pytest.param("{}", id="object"), pytest.param("false", id="boolean"),
    pytest.param("0", id="number"), pytest.param('""', id="empty-json-string"),
    pytest.param('"[]"', id="json-string-not-list"), pytest.param("[", id="truncated-json"),
    pytest.param("[null]", id="null-entry"), pytest.param('[""]', id="blank-entry"),
    pytest.param(b"[]", id="nontext-storage"),
])
async def test_persisted_invalid_policy_blocks_all_http_and_cannot_resurrect_consent(db, viewer_user, remote, raw):
    uid = viewer_user["id"]
    set_setting(db, "abs_url", ABS_ROOT)
    set_setting(db, "abs_excluded_libraries", "[]")
    item = _item(db, "Legacy unstamped audio", "audiobook", abs_id="remote-item", abs_library_id=None)
    db.commit()
    _save(db, uid, item, status="paused", progress="90", notes="Keep my state and history")
    account = await _link(uid, "abs")
    _enable(db, uid, account)
    before = _progress_rows(db)
    linked = dict(db.execute("SELECT * FROM user_source_accounts WHERE id = ?", (account,)).fetchone())
    generation = db.execute("SELECT generation FROM user_source_link_state WHERE user_id = ? AND source_key = 'abs'", (uid,)).fetchone()[0]
    consent_fields = ("revision", "interval_minutes", "consent_version", "consented_at",
                      "actor_username", "actor_role", "actor_token_version")
    schedule = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()
    consent = {key: schedule[key] for key in consent_fields}
    # Exercise a real persisted raw setting, including SQL NULL/BLOB. Neither
    # the legacy config route's coercions nor a mocked source DTO can hide it.
    set_setting(db, "abs_excluded_libraries", raw)
    db.commit()
    assert db.execute("SELECT value FROM settings WHERE key = 'abs_excluded_libraries'").fetchone()[0] == raw
    calls = len(remote.calls)
    with pytest.raises(svc.ProgressError, match="relink_required"):
        await _link(uid, "abs")
    with pytest.raises(svc.ProgressError, match="relink_required"):
        await svc.pull_item(uid, account, item)
    with pytest.raises(svc.ProgressError, match="relink_required"):
        _enable(db, uid, account)
    db.rollback()
    result = await svc.run_scheduled_pass()
    assert result["errors"] == 1 and result["attempted"] == result["accepted"] == result["conflict"] == 0
    assert len(remote.calls) == calls and not remote["abs_progress"].called
    assert _progress_rows(db) == before
    assert dict(db.execute("SELECT * FROM user_source_accounts WHERE id = ?", (account,)).fetchone()) == linked
    assert db.execute("SELECT generation FROM user_source_link_state WHERE user_id = ? AND source_key = 'abs'", (uid,)).fetchone()[0] == generation
    schedule = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()
    assert {key: schedule[key] for key in consent_fields} == consent

    db.execute("BEGIN IMMEDIATE")
    svc.disconnect(db, uid, account)
    db.commit()
    tombstone = db.execute("SELECT generation FROM user_source_link_state WHERE user_id = ? AND source_key = 'abs'", (uid,)).fetchone()[0]
    with pytest.raises(svc.ProgressError, match="relink_required"):
        await _link(uid, "abs")  # A new link after disconnect must also fail before HTTP.
    assert db.execute("SELECT COUNT(*) FROM user_source_accounts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM user_source_schedules").fetchone()[0] == 0
    assert db.execute("SELECT generation FROM user_source_link_state WHERE user_id = ? AND source_key = 'abs'", (uid,)).fetchone()[0] == tombstone
    assert len(remote.calls) == calls and _progress_rows(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["absent", "explicit-empty", "json-whitespace-around-empty"])
@pytest.mark.parametrize("flow", ["manual", "scheduled"])
async def test_only_absent_or_valid_empty_policy_defaults_to_all_libraries(db, viewer_user, remote, policy, flow):
    uid = viewer_user["id"]
    set_setting(db, "abs_url", ABS_ROOT)
    item = _item(db, "Legacy unscoped audio", "audiobook", abs_id="remote-item", abs_library_id=None)
    db.execute("DELETE FROM settings WHERE key = 'abs_excluded_libraries'")
    if policy != "absent":
        set_setting(db, "abs_excluded_libraries", "[]" if policy == "explicit-empty" else " \n[]\t")
    db.commit()
    account = await _link(uid, "abs")
    if flow == "manual":
        assert (await svc.pull_item(uid, account, item))["outcome"] == "accepted"
    else:
        _enable(db, uid, account)
        assert (await svc.run_scheduled_pass())["accepted"] == 1
    assert _state(db, uid, item)["progress"] == 25
