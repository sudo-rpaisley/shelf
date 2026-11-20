"""Native cache ownership, atomicity and exact identity; additive, unrun."""

import asyncio
import httpx
import pytest

from app import crypto
from app.database import get_db, set_setting
from app.services import source_continue as svc, source_progress
from app.services.source_progress import ProgressError
from tests.conftest import _create_user
from tests.test_availability import _item
from tests.test_continue_connectors import TOKEN, _rom
from tests.test_personal_library import _save


ROOT = "https://romm.native.invalid/proxy"


@pytest.fixture
def wire(db, monkeypatch):
    set_setting(db, "romm_url", ROOT)
    set_setting(db, "romm_public_url", "https://games.native.invalid")
    set_setting(db, "romm_token", "synthetic-household-catalogue-token-never-used")
    db.commit()
    state = {"items": [_rom()], "calls": [], "error": None, "hook": None}
    async def send(request):
        assert request.method == "GET" and TOKEN not in str(request.url)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        state["calls"].append(request)
        if request.url.path.endswith("/api/users/me"):
            return httpx.Response(200, json={"id": 42})
        assert request.url.path.endswith("/api/roms")
        if state["hook"] is not None:
            await state["hook"]()
        return httpx.Response(state["error"], text=TOKEN) if state["error"] else httpx.Response(200, json={"items": state["items"]})
    monkeypatch.setattr(svc, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(send), follow_redirects=False, trust_env=False))
    return state


async def _link(user_id, **fields):
    return await svc.link(user_id, "romm", token=TOKEN, remote_user_id="42", consent=svc.CONSENT_VERSION, **fields)


def _row(db, connection):
    value = db.execute("SELECT * FROM user_continue_connections WHERE id = ?", (connection,)).fetchone()
    return dict(value) if value else None


def _again(db, connection):
    db.execute("UPDATE user_continue_connections SET last_attempt_at = NULL WHERE id = ?", (connection,))
    db.commit()


def _snapshot(db):
    return {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in ("items", "item_copies", "source_records", "romm_records", "komga_records", "user_item_state",
                          "user_item_events", "user_source_accounts", "user_source_progress", "user_source_progress_events")}


def _seed(db, user_id, *, entries=None):
    set_setting(db, "romm_url", ROOT)
    set_setting(db, "romm_public_url", "https://games.native.invalid")
    source = svc._source(db, "romm")
    return db.execute("INSERT INTO user_continue_connections (user_id, source_key, provider, remote_user_id, credentials_enc, "
                      "root_fingerprint, revision, consent_version, consented_at, schedule_revision, entries_json, last_success_at, last_status) "
                      "VALUES (?, 'romm', 'romm', '42', 'not-a-usable-token', ?, 'revision', ?, ?, 'schedule', ?, ?, 'ok')",
                      (user_id, source["fingerprint"], svc.CONSENT_VERSION, svc._now(), svc._json(entries or []), svc._now())).lastrowid


@pytest.mark.asyncio
async def test_link_requires_personal_consent_identity_and_separate_refresh_and_automatic_consent(db, viewer_user, wire):
    uid = viewer_user["id"]
    with pytest.raises(ProgressError, match="consent_required"):
        await svc.link(uid, "romm", token=TOKEN, remote_user_id="42", consent="pull_v1")
    assert not wire["calls"]
    with pytest.raises(ProgressError, match="identity_mismatch"):
        await svc.link(uid, "romm", token=TOKEN, remote_user_id="43", consent=svc.CONSENT_VERSION)
    assert db.execute("SELECT COUNT(*) FROM user_continue_connections").fetchone()[0] == 0
    connection = await _link(uid)
    row = _row(db, connection)
    assert row["entries_json"] == "[]" and row["interval_minutes"] == 0 and row["last_status"] == "never"
    assert crypto.is_fernet_token(row["credentials_enc"]) and TOKEN not in row["credentials_enc"]
    assert all(call.url.path.endswith("/api/users/me") for call in wire["calls"])
    assert (await svc.run_scheduled_pass())["attempted"] == 0


@pytest.mark.asyncio
async def test_remote_account_is_not_shareable_across_shelf_users(db, viewer_user, wire):
    await _link(viewer_user["id"])
    other = _create_user("continue-other", "synthetic-password", "Other", "viewer")
    with pytest.raises(ProgressError, match="account_in_use"):
        await _link(other["id"])
    assert not svc.context(db, other["id"])["connections"]


@pytest.mark.asyncio
async def test_cache_refresh_preserves_all_manual_state_history_catalogue_and_provider_bindings(db, viewer_user, wire):
    uid = viewer_user["id"]
    item = _item(db, "Manual game", "video_game")
    db.execute("INSERT INTO romm_records (romm_id, item_id, platform_id) VALUES ('7', ?, '4')", (item,))
    db.commit()
    _save(db, uid, item, status="paused", progress="81", notes="private manual note", rating="5", on_list="1")
    before = _snapshot(db)
    connection = await _link(uid)
    assert (await svc.refresh(uid, connection))["count"] == 1
    assert _snapshot(db) == before
    context = svc.context(db, uid)
    assert context["entries"][0]["catalogue_id"] == item and context["entries"][0]["percent"] is None
    assert context["entries"][0]["source_name"] == "RomM"
    assert TOKEN not in str(context) and "synthetic-household-catalogue-token-never-used" not in str(context)
    assert "private manual note" not in str(context)


@pytest.mark.asyncio
async def test_remote_only_is_not_title_matched_to_physical_game_and_trash_does_not_resurface(db, viewer_user, wire):
    uid = viewer_user["id"]
    physical = _item(db, "Native game 7", "video_game")
    db.commit()
    connection = await _link(uid)
    await svc.refresh(uid, connection)
    card = svc.context(db, uid)["entries"][0]
    assert card["catalogue_id"] is None and "id" not in card["art"]
    digital = _item(db, "Exact digital game", "video_game")
    db.execute("INSERT INTO romm_records (romm_id, item_id, platform_id) VALUES ('7', ?, '4')", (digital,))
    db.commit()
    assert svc.context(db, uid)["entries"][0]["catalogue_id"] == digital != physical
    db.execute("UPDATE items SET deleted_at = '2026-01-01' WHERE id = ?", (digital,))
    db.commit()
    assert svc.context(db, uid)["entries"] == []
    with pytest.raises(ProgressError, match="not_found"):
        svc.resolve(db, uid, connection, card["key"])


@pytest.mark.asyncio
async def test_failure_keeps_old_cache_and_success_clock_with_redacted_staleness_and_no_retry(db, viewer_user, wire):
    uid = viewer_user["id"]
    connection = await _link(uid)
    await svc.refresh(uid, connection)
    before = _row(db, connection)
    _again(db, connection)
    wire["error"] = 401
    calls = len(wire["calls"])
    with pytest.raises(ProgressError, match="auth_failed"):
        await svc.refresh(uid, connection)
    after = _row(db, connection)
    assert after["entries_json"] == before["entries_json"] and after["last_success_at"] == before["last_success_at"]
    assert after["last_error"] == "auth_failed" and after["lease_token"] is None
    assert svc.context(db, uid)["entries"][0]["stale"]
    assert len(wire["calls"]) == calls + 2
    with pytest.raises(ProgressError, match="busy"):
        await svc.refresh(uid, connection)
    assert len(wire["calls"]) == calls + 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change,code", [("scope", "relink_required"), ("disabled", "disabled"),
                                         ("actor", "actor_changed"), ("schedule", "stale_schedule"), ("disconnect", "not_found")])
async def test_delayed_response_cannot_cross_configuration_actor_schedule_or_disconnect_boundary(db, viewer_user, wire, change, code):
    uid = viewer_user["id"]
    connection = await _link(uid)
    async def mutate():
        with get_db() as other:
            other.execute("BEGIN IMMEDIATE")
            if change == "scope":
                set_setting(other, "romm_excluded_platforms", '["4"]')
            elif change == "disabled":
                set_setting(other, "feature.romm", "0")
            elif change == "actor":
                other.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (uid,))
            elif change == "schedule":
                svc.set_schedule(other, uid, connection, interval_minutes=0)
            else:
                svc.disconnect(other, uid, connection)
    wire["hook"] = mutate
    with pytest.raises(ProgressError, match=code):
        await svc.refresh(uid, connection)
    row = _row(db, connection)
    if change == "disconnect":
        assert row is None
    else:
        assert row["entries_json"] == "[]"


@pytest.mark.asyncio
@pytest.mark.parametrize("suppress", [False, True])
async def test_cancellation_never_publishes_late_http_result_even_if_transport_suppresses_it(db, viewer_user, wire, suppress):
    uid = viewer_user["id"]
    connection = await _link(uid)
    entered = asyncio.Event()
    async def pause():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not suppress:
                raise
    wire["hook"] = pause
    task = asyncio.create_task(svc.refresh(uid, connection))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    row = _row(db, connection)
    assert row["entries_json"] == "[]" and row["last_success_at"] is None
    assert row["last_error"] == "interrupted" and row["lease_token"] is None


@pytest.mark.asyncio
async def test_separate_schedule_consent_disable_and_relink_do_not_inherit_automatic_authority(db, viewer_user, wire):
    uid = viewer_user["id"]
    connection = await _link(uid)
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(ProgressError, match="consent_required"):
        svc.set_schedule(db, uid, connection, interval_minutes=15, consent=svc.CONSENT_VERSION)
    db.rollback()
    db.execute("BEGIN IMMEDIATE")
    svc.set_schedule(db, uid, connection, interval_minutes=15, consent=svc.SCHEDULE_CONSENT_VERSION)
    db.commit()
    assert (await svc.run_scheduled_pass())["attempted"] == 1
    assert (await svc.run_scheduled_pass())["attempted"] == 0
    db.execute("BEGIN IMMEDIATE")
    svc.set_schedule(db, uid, connection, interval_minutes=0)
    db.commit()
    replacement = await _link(uid)
    assert replacement != connection and _row(db, connection) is None
    assert _row(db, replacement)["interval_minutes"] == 0 and _row(db, replacement)["entries_json"] == "[]"


@pytest.mark.asyncio
async def test_cache_and_progress_import_registries_share_the_same_remote_ownership_boundary(db, viewer_user, monkeypatch):
    uid = viewer_user["id"]
    other = _create_user("other-import-owner", "synthetic-password", "Other", "viewer")
    set_setting(db, "abs_url", "https://audio.native.invalid")
    db.commit()
    def client():
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "self", "mediaProgress": []})),
                                 follow_redirects=False, trust_env=False)
    monkeypatch.setattr(svc, "_client", client)
    monkeypatch.setattr(source_progress, "_client", client)
    cache = await svc.link(uid, "abs", token=TOKEN, remote_user_id="self", consent=svc.CONSENT_VERSION)
    with pytest.raises(ProgressError, match="account_in_use"):
        await source_progress.link_account(other["id"], "abs", token=TOKEN, remote_user_id="self", consent=source_progress.CONSENT_VERSION)
    db.execute("BEGIN IMMEDIATE")
    svc.disconnect(db, uid, cache)
    db.commit()
    await source_progress.link_account(other["id"], "abs", token=TOKEN, remote_user_id="self", consent=source_progress.CONSENT_VERSION)
    with pytest.raises(ProgressError, match="account_in_use"):
        await svc.link(uid, "abs", token=TOKEN, remote_user_id="self", consent=svc.CONSENT_VERSION)


@pytest.mark.parametrize("value", ["", "null", "{}", '[""]', '[null]', '["../bad", 4]'])
def test_malformed_saved_scope_fails_closed_instead_of_including_everything(db, viewer_user, value):
    set_setting(db, "romm_url", ROOT)
    set_setting(db, "romm_excluded_platforms", value)
    with pytest.raises(ProgressError, match="relink_required"):
        svc._source(db, "romm")


def test_romm_legacy_integer_exclusions_keep_their_scope_instead_of_becoming_included(db):
    set_setting(db, "romm_url", ROOT)
    set_setting(db, "romm_excluded_platforms", '[4, "5"]')
    assert svc._source(db, "romm")["excluded"] == ["4", "5"]


def test_schema_is_additive_and_keeps_original_progress_accounts_and_foreign_keys(db, viewer_user):
    definition = db.execute("SELECT sql FROM sqlite_master WHERE name = 'user_source_accounts'").fetchone()[0]
    assert "provider IN ('jellyfin', 'abs')" in definition
    assert {row[2] for row in db.execute("PRAGMA foreign_key_list(user_continue_connections)")} == {"users"}
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("SELECT COUNT(*) FROM user_source_accounts").fetchone()[0] == 0


def test_handoff_uses_current_source_and_exact_binding_without_network_decryption_or_writes(db, viewer_user, monkeypatch):
    from app.services import continue_connectors
    uid = viewer_user["id"]
    native = continue_connectors._entry("7", "4", kind="game", media_type="video_game", title="Native title")
    connection = _seed(db, uid, entries=[native])
    db.commit()
    def forbidden(*args, **kwargs):
        raise AssertionError("GET must not decrypt or perform remote work")
    monkeypatch.setattr(svc, "_client", forbidden)
    monkeypatch.setattr(crypto, "decrypt_value", forbidden)
    before = db.total_changes
    entry = svc.context(db, uid)["entries"][0]
    assert svc.resolve(db, uid, connection, entry["key"]) == "https://games.native.invalid/rom/7"
    assert db.total_changes == before and entry["catalogue_id"] is None
    set_setting(db, "romm_public_url", "https://new-games.native.invalid")
    with pytest.raises(ProgressError, match="relink_required"):
        svc.resolve(db, uid, connection, entry["key"])
    assert svc.context(db, uid)["entries"] == []
