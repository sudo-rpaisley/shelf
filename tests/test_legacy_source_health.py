"""Legacy source health is local, redacted, and never a connectivity probe."""

import asyncio
import inspect
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from app import crypto
from app.database import get_db, set_setting
from app.services import audiobookshelf, komga_sync, legacy_sources, romm_sync
from tests.conftest import _insert_item


PRIVATE = "https://username:password@private.example/api?token=private-token"


def _status(provider):
    with get_db() as db:
        return next(row for row in legacy_sources.statuses(db) if row["key"] == provider)


def _health_values(provider):
    with get_db() as db:
        return dict(db.execute(
            "SELECT key, value FROM settings WHERE key LIKE ? ORDER BY key",
            (f"connection.{provider}.%",),
        ).fetchall())


def _fail(*args, **kwargs):
    pytest.fail("Status reads must not open connections, log, or access the network")


def test_statuses_use_only_the_callers_connection_without_writes_or_network(db, monkeypatch):
    monkeypatch.setattr(legacy_sources, "get_db", _fail)
    monkeypatch.setattr(httpx, "AsyncClient", _fail)
    monkeypatch.setattr(crypto, "decrypt_value", _fail)
    db.execute("BEGIN IMMEDIATE")
    before = db.total_changes

    rows = legacy_sources.statuses(db)

    assert db.total_changes == before
    assert [row["key"] for row in rows] == ["abs", "komga", "romm"]
    assert [row["name"] for row in rows] == ["Audiobookshelf", "Komga", "RomM"]
    for row in rows:
        assert set(row) == {
            "key", "provider", "name", "enabled", "configured", "records_count",
            "last_attempt_at", "last_sync_at", "last_status", "last_error",
            "capabilities", "settings_path",
        }
        assert row["provider"] == row["key"]
        assert row["enabled"] is True
        assert row["configured"] is False
        assert row["records_count"] == 0
        assert row["last_attempt_at"] is None
        assert row["last_sync_at"] is None
        assert row["last_status"] == "never"
        assert row["last_error"] is None
        assert row["capabilities"] == {
            "sync": True, "open": True, "remote_write": False,
            "scheduled_sync": row["key"] == "abs",
        }
        assert row["settings_path"] == "/settings"


def test_statuses_see_uncommitted_config_and_flags_without_cached_helpers(db, monkeypatch):
    from app import features

    monkeypatch.setattr(features, "_cached_flags", {"abs_sync": True, "komga": True, "romm": True})
    monkeypatch.setattr(features, "feature_enabled", _fail)
    monkeypatch.setattr(komga_sync, "configuration", _fail)
    monkeypatch.setattr(romm_sync, "configuration", _fail)
    monkeypatch.setattr(legacy_sources, "get_db", _fail)
    monkeypatch.setattr(crypto, "decrypt_value", _fail)
    monkeypatch.setattr(crypto.logger, "debug", _fail)
    monkeypatch.setattr(crypto.logger, "warning", _fail)
    db.execute("BEGIN IMMEDIATE")
    for provider, credential in (("abs", "token"), ("komga", "api_key"), ("romm", "token")):
        set_setting(db, f"{provider}_url", PRIVATE)
        set_setting(db, f"{provider}_{credential}", "private-token")
        set_setting(db, f"{provider}_public_url", PRIVATE)
    set_setting(db, "feature.abs_sync", "0")
    set_setting(db, "feature.komga", "0")
    set_setting(db, "feature.romm", "0")

    rows = legacy_sources.statuses(db)

    assert all(row["configured"] is True and row["enabled"] is False for row in rows)
    assert PRIVATE not in json.dumps(rows)
    assert "private-token" not in json.dumps(rows)
    set_setting(db, "feature.komga", "1")
    set_setting(db, "romm_token", "")
    fresh = {row["key"]: row for row in legacy_sources.statuses(db)}
    assert fresh["komga"]["enabled"] is True
    assert fresh["romm"]["configured"] is False


def test_abs_env_only_configuration_is_reported_without_copying_credentials(db, monkeypatch):
    monkeypatch.setenv("ABS_URL", PRIVATE)
    monkeypatch.setenv("ABS_TOKEN", "private-token")
    monkeypatch.setattr(httpx, "AsyncClient", _fail)

    row = next(row for row in legacy_sources.statuses(db) if row["key"] == "abs")

    assert row["configured"] is True
    assert PRIVATE not in json.dumps(row)
    assert "private-token" not in json.dumps(row)
    assert db.execute("SELECT 1 FROM settings WHERE key IN ('abs_url', 'abs_token')").fetchone() is None


def test_encrypted_configuration_is_checked_silently_even_under_a_write_lock(db, monkeypatch):
    monkeypatch.delenv("SHELF_ENCRYPTION_KEY", raising=False)
    key = crypto.get_encryption_key()
    db.execute("BEGIN IMMEDIATE")
    for provider, credential in (("abs", "token"), ("komga", "api_key"), ("romm", "token")):
        set_setting(db, f"{provider}_url", PRIVATE)
        set_setting(db, f"{provider}_{credential}", crypto.encrypt_value("private-token", key))
    monkeypatch.setattr(crypto, "decrypt_value", _fail)
    monkeypatch.setattr(crypto, "get_encryption_key", _fail)
    monkeypatch.setattr(crypto.logger, "warning", _fail)
    monkeypatch.setattr(legacy_sources, "get_db", _fail)
    assert all(row["configured"] for row in legacy_sources.statuses(db))

    set_setting(db, "abs_token", crypto.encrypt_value("private-token", "wrong-key"))
    assert legacy_sources.statuses(db)[0]["configured"] is False

    # A missing key file must not be recreated by a nominally read-only DTO.
    crypto._keyfile_path("encryption.key").unlink()
    monkeypatch.setattr(crypto, "_cached_encryption_key", None)
    monkeypatch.delenv("SHELF_ENCRYPTION_KEY", raising=False)
    assert not any(row["configured"] for row in legacy_sources.statuses(db))
    assert not crypto._keyfile_path("encryption.key").exists()


def test_live_record_counts_hide_trash_and_use_provider_records_not_item_source(db):
    abs_live = _insert_item(db, isbn=None, abs_id="abs-live", source="manual")
    abs_trash = _insert_item(db, isbn=None, abs_id="abs-trash")
    komga_live = _insert_item(db, isbn=None, media_type="comic", source="manual")
    komga_trash = _insert_item(db, isbn=None, media_type="comic")
    romm_live = _insert_item(db, isbn=None, media_type="video_game", source="manual")
    romm_trash = _insert_item(db, isbn=None, media_type="video_game")
    _insert_item(db, isbn=None, source="audiobookshelf")  # No remote identity.
    for remote_id, item_id in (("k1", komga_live), ("k2", komga_live), ("k3", komga_trash)):
        db.execute(
            "INSERT INTO komga_records (komga_id, item_id, library_id, kind) VALUES (?, ?, 'lib', 'comic')",
            (remote_id, item_id),
        )
    for remote_id, item_id in (("r1", romm_live), ("r2", romm_trash)):
        db.execute(
            "INSERT INTO romm_records (romm_id, item_id, platform_id) VALUES (?, ?, 'platform')",
            (remote_id, item_id),
        )
    db.executemany(
        "UPDATE items SET deleted_at = '2026-01-01' WHERE id = ?",
        [(abs_trash,), (komga_trash,), (romm_trash,)],
    )

    rows = {row["key"]: row for row in legacy_sources.statuses(db)}

    assert rows["abs"]["records_count"] == 1
    assert rows["komga"]["records_count"] == 2
    assert rows["romm"]["records_count"] == 1
    assert db.execute("SELECT id FROM items_live WHERE id = ?", (abs_live,)).fetchone()


def test_statuses_redact_corrupt_health_and_do_not_trust_old_scheduler_stamp(db):
    set_setting(db, "abs_last_sync", "2026-01-01T00:00:00+00:00")
    assert legacy_sources.statuses(db)[0]["last_sync_at"] is None
    for field in ("last_status", "last_error", "last_attempt_at", "last_sync_at"):
        set_setting(db, f"connection.abs.{field}", PRIVATE)
    row = legacy_sources.statuses(db)[0]
    assert row["last_status"] == "never"
    assert row["last_attempt_at"] is None
    assert row["last_sync_at"] is None
    assert row["last_error"] is None

    set_setting(db, "connection.abs.last_status", "error")
    assert legacy_sources.statuses(db)[0]["last_error"] == "connector_error"
    set_setting(db, "connection.abs.last_error", "timeout")
    set_setting(db, "connection.abs.last_attempt_at", "2026-01-01T12:00:00Z")
    row = legacy_sources.statuses(db)[0]
    assert row["last_error"] == "timeout"
    assert row["last_attempt_at"] == "2026-01-01T12:00:00+00:00"
    assert PRIVATE not in json.dumps(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,stats,status,error", [
    ("abs", {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}, "ok", None),
    ("abs", {"error": PRIVATE}, "error", "connector_error"),
    ("abs", {"added": 1, "errors": 1}, "partial", "incomplete"),
    ("abs", {"added": 0, "unchanged": 0, "errors": 2}, "error", "connector_error"),
    ("abs", {"unchanged": 1, "errors": 1}, "partial", "incomplete"),
    ("komga", {"created": 0, "adopted": 0, "updated": 0, "skipped": 0, "in_trash": 0, "errors": 0}, "ok", None),
    ("komga", {"adopted": 1, "errors": 1}, "partial", "incomplete"),
    ("komga", {"created": 0, "errors": 1}, "error", "connector_error"),
    ("romm", {"created": 0, "updated": 0, "skipped": 0, "in_trash": 0, "errors": 0}, "ok", None),
    ("romm", {"updated": 1, "errors": 1}, "partial", "incomplete"),
    ("romm", {"errors": 1}, "error", "connector_error"),
    ("abs", {"warnings": [PRIVATE]}, "partial", "incomplete"),
    ("komga", {"partial": True, "errors": 1}, "partial", "incomplete"),
    ("romm", {"complete": False}, "partial", "incomplete"),
    ("romm", {"status": "partial"}, "partial", "incomplete"),
    ("abs", {"skipped": 1, "in_trash": 1, "errors": 0}, "ok", None),
    ("abs", {"ok": False, "message": PRIVATE}, "error", "connector_error"),
    ("komga", {"success": False, "detail": PRIVATE}, "error", "connector_error"),
    ("romm", None, "error", "malformed_response"),
])
async def test_tracking_understands_existing_stats_without_persisting_payloads(provider, stats, status, error):
    async def run(url, token, *, context):
        assert url == PRIVATE and token == "private-token" and context == PRIVATE
        attempt = _status(provider)
        assert attempt["last_attempt_at"] is not None
        assert attempt["last_status"] == "never"
        return stats

    returned = await legacy_sources.run_tracked(provider, run, PRIVATE, "private-token", context=PRIVATE)

    assert returned is stats
    row = _status(provider)
    assert row["last_status"] == status
    assert row["last_error"] == error
    assert (row["last_sync_at"] is not None) is (status == "ok")
    saved = _health_values(provider)
    assert set(saved) == {
        f"connection.{provider}.last_attempt_at", f"connection.{provider}.last_status",
        f"connection.{provider}.last_error",
    } | ({f"connection.{provider}.last_sync_at"} if status == "ok" else set())
    assert PRIVATE not in json.dumps(saved)
    assert "private-token" not in json.dumps(saved)


@pytest.mark.asyncio
async def test_last_success_survives_failure_and_partial_then_recovers(monkeypatch):
    times = iter(f"2026-01-01T00:00:0{i}+00:00" for i in range(8))
    monkeypatch.setattr(legacy_sources, "_now", lambda: next(times))
    await legacy_sources.run_tracked("abs", AsyncMock(return_value={"added": 1, "errors": 0}))
    assert _status("abs")["last_sync_at"] == "2026-01-01T00:00:01+00:00"

    await legacy_sources.run_tracked("abs", AsyncMock(return_value={"error": PRIVATE}))
    failed = _status("abs")
    assert failed["last_attempt_at"] == "2026-01-01T00:00:02+00:00"
    assert failed["last_status"] == "error"
    assert failed["last_sync_at"] == "2026-01-01T00:00:01+00:00"

    await legacy_sources.run_tracked("abs", AsyncMock(return_value={"added": 1, "errors": 1}))
    partial = _status("abs")
    assert partial["last_status"] == "partial"
    assert partial["last_sync_at"] == failed["last_sync_at"]

    await legacy_sources.run_tracked("abs", AsyncMock(return_value={"unchanged": 2, "errors": 0}))
    recovered = _status("abs")
    assert recovered["last_status"] == "ok"
    assert recovered["last_error"] is None
    assert recovered["last_sync_at"] == "2026-01-01T00:00:07+00:00"
    assert _health_values("abs")["connection.abs.last_error"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,code", [
    ("timeout", "timeout"), ("connect", "unreachable"), ("auth", "auth_failed"),
    ("forbidden", "forbidden"), ("rate_limit", "rate_limited"),
    ("http", "http_error"), ("unexpected", "unexpected_error"),
    ("wrapped_timeout", "timeout"), ("cancelled", "interrupted"),
])
async def test_exceptions_are_reraised_with_only_safe_health_codes(kind, code):
    await legacy_sources.run_tracked("romm", AsyncMock(return_value={"created": 1}))
    success = _status("romm")["last_sync_at"]
    if kind == "timeout":
        error = httpx.ReadTimeout(PRIVATE)
    elif kind == "connect":
        error = httpx.ConnectError(PRIVATE)
    elif kind in {"auth", "forbidden", "rate_limit", "http"}:
        status_code = {"auth": 401, "forbidden": 403, "rate_limit": 429, "http": 500}[kind]
        request = httpx.Request("GET", PRIVATE, headers={"Authorization": "private-token"})
        error = httpx.HTTPStatusError(PRIVATE, request=request, response=httpx.Response(status_code))
    elif kind == "cancelled":
        error = asyncio.CancelledError(PRIVATE)
    else:
        error = RuntimeError(PRIVATE)
        if kind == "wrapped_timeout":
            error.__cause__ = httpx.ReadTimeout(PRIVATE)

    async def fail():
        raise error

    with pytest.raises(type(error)) as caught:
        await legacy_sources.run_tracked("romm", fail)

    assert caught.value is error
    row = _status("romm")
    assert row["last_status"] == "error"
    assert row["last_error"] == code
    assert row["last_sync_at"] == success
    assert PRIVATE not in json.dumps(_health_values("romm"))
    assert "private-token" not in json.dumps(_health_values("romm"))


@pytest.mark.asyncio
async def test_health_transactions_end_before_and_begin_after_underlying_sync(monkeypatch):
    active = 0
    transactions = []

    @contextmanager
    def tracking_db():
        nonlocal active
        assert active == 0
        with get_db() as db:
            active += 1
            transactions.append(db)
            try:
                yield db
            finally:
                active -= 1

    monkeypatch.setattr(legacy_sources, "get_db", tracking_db)

    async def sync():
        assert active == 0
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            assert legacy_sources.statuses(db)[0]["last_attempt_at"]
            set_setting(db, "test_sync_write", "committed")
            assert active == 0
        return {"added": 1}

    await legacy_sources.run_tracked("abs", sync)
    assert active == 0
    assert len(transactions) == 2
    with get_db() as db:
        assert db.execute("SELECT value FROM settings WHERE key = 'test_sync_write'").fetchone()[0] == "committed"


@pytest.mark.asyncio
async def test_failed_health_write_does_not_replace_the_original_sync_exception(monkeypatch):
    error = RuntimeError(PRIVATE)
    storage_error = RuntimeError("Health storage failed")

    def failed_recording(*args):
        raise storage_error

    monkeypatch.setattr(legacy_sources, "_record_result", failed_recording)
    with pytest.raises(RuntimeError) as caught:
        await legacy_sources.run_tracked("abs", AsyncMock(side_effect=error))
    assert caught.value is error
    assert caught.value.__cause__ is storage_error


@pytest.mark.asyncio
async def test_overlapping_runs_keep_latest_attempt_outcome_without_losing_success():
    started, release = asyncio.Event(), asyncio.Event()

    async def older_sync():
        started.set()
        await release.wait()
        return {"added": 1}

    older = asyncio.create_task(legacy_sources.run_tracked("abs", older_sync))
    await started.wait()
    await legacy_sources.run_tracked("abs", AsyncMock(return_value={"error": PRIVATE}))
    newest_attempt = _status("abs")["last_attempt_at"]
    release.set()
    await older

    row = _status("abs")
    assert row["last_attempt_at"] == newest_attempt
    assert row["last_status"] == "error"
    assert row["last_error"] == "connector_error"
    assert row["last_sync_at"] is not None


@pytest.mark.asyncio
async def test_unknown_provider_is_rejected_before_any_metadata_or_callable():
    run = AsyncMock()
    with pytest.raises(ValueError, match="Unknown legacy source provider"):
        await legacy_sources.run_tracked(PRIVATE, run)
    run.assert_not_awaited()
    with get_db() as db:
        assert not db.execute("SELECT 1 FROM settings WHERE key LIKE 'connection.%'").fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("module,provider", [(komga_sync, "komga"), (romm_sync, "romm")])
async def test_public_sync_wrapper_preserves_signature_stats_and_progress(module, provider, monkeypatch):
    stats = {"created": 1, "updated": 0, "skipped": 0, "in_trash": 0, "errors": 0}
    progress = []

    async def callback(*event):
        progress.append(event)

    async def implementation(on_progress=None):
        await on_progress(1, 1, "Example", "created")
        return stats

    monkeypatch.setattr(module, "_sync", implementation)
    signature = inspect.signature(module.sync)
    assert list(signature.parameters) == ["on_progress"]
    assert signature.parameters["on_progress"].default is None
    returned = await module.sync(callback)
    assert returned is stats
    assert progress == [(1, 1, "Example", "created")]
    assert _status(provider)["last_status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy", [False, True])
@respx.mock
async def test_real_abs_library_failure_is_not_stamped_as_complete(healthy):
    root = "https://abs.example"
    libraries = [{"id": "bad", "name": "Bad", "mediaType": "book"}]
    if healthy:
        libraries.append({"id": "good", "name": "Audiobooks", "mediaType": "book"})
        respx.get(f"{root}/api/libraries/good/items").respond(200, json={"results": [{
            "id": "item-1", "media": {"metadata": {"title": "Example"}, "duration": 3600},
        }]})
        respx.get(f"{root}/api/items/item-1/cover").respond(404)
    respx.get(f"{root}/api/libraries").respond(200, json={"libraries": libraries})
    respx.get(f"{root}/api/libraries/bad/items").respond(503)

    stats = await legacy_sources.run_tracked("abs", audiobookshelf.sync, root, "token")

    assert stats["errors"] == 1
    assert stats["added"] == int(healthy)
    assert _status("abs")["last_status"] == ("partial" if healthy else "error")
    assert _status("abs")["last_sync_at"] is None
    for call in respx.calls:
        assert call.request.headers["Authorization"] == "Bearer token"


@pytest.mark.asyncio
@respx.mock
async def test_real_abs_early_error_dictionary_records_failure_unchanged():
    respx.get("https://abs.example/api/libraries").respond(401)
    stats = await legacy_sources.run_tracked("abs", audiobookshelf.sync, "https://abs.example", "token")
    assert stats == {"error": "Failed to connect: HTTP 401"}
    assert _status("abs")["last_status"] == "error"
    assert _status("abs")["last_sync_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("module,provider", [(komga_sync, "komga"), (romm_sync, "romm")])
@pytest.mark.parametrize("healthy", [False, True])
async def test_real_service_library_failures_are_counted(module, provider, healthy, monkeypatch):
    kwargs = {"url": f"https://{provider}.example", "api_key" if provider == "komga" else "token": "secret"}
    module.save_configuration(**kwargs)
    monkeypatch.setattr(module, "_ingest_cover", AsyncMock(return_value=False))

    if provider == "komga":
        libraries = [{"id": "bad", "kind": "comic"}]
        if healthy:
            libraries.append({"id": "good", "kind": "comic"})

        async def candidates(*args, library_id, kind):
            if library_id == "bad":
                raise module.komga_libraries.KomgaError(PRIVATE)
            return [{"komga_id": "k1", "title": "Example"}]

        monkeypatch.setattr(module.komga_libraries, "fetch_libraries", AsyncMock(return_value=libraries))
        monkeypatch.setattr(module.komga_books, "fetch_library_candidates", candidates)
        monkeypatch.setattr(module.komga_records, "persist_candidate", lambda *a: {"action": "created", "item_id": 1})
    else:
        platforms = [{"id": "bad", "rom_count": 1}]
        if healthy:
            platforms.append({"id": "good", "rom_count": 1})

        async def candidates(client, server, token, platform):
            if platform["id"] == "bad":
                raise module.romm_client.RomMError(PRIVATE)
            yield {"romm_id": "r1", "title": "Example"}

        monkeypatch.setattr(module.romm_client, "fetch_platforms", AsyncMock(return_value=platforms))
        monkeypatch.setattr(module.romm_client, "iter_rom_candidates", candidates)
        monkeypatch.setattr(module.romm_records, "persist_candidate", lambda *a: {"action": "created", "item_id": 1})

    stats = await module.sync()
    assert stats["errors"] == 1
    assert stats["created"] == int(healthy)
    assert _status(provider)["last_status"] == ("partial" if healthy else "error")
    assert _status(provider)["last_sync_at"] is None
    assert PRIVATE not in json.dumps(_health_values(provider))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_manual_abs_endpoints_track_without_changing_stats_or_sse_headers(stream, monkeypatch):
    from app.routers import sync as router

    with get_db() as db:
        set_setting(db, "abs_url", "https://abs.example")
        set_setting(db, "abs_token", "secret")
    stats = {"added": 1, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}

    async def run(url, token, on_progress=None):
        assert (url, token) == ("https://abs.example", "secret")
        if on_progress is not None:
            await on_progress(1, 1, "Example", "added")
        return stats

    monkeypatch.setattr(router.audiobookshelf, "sync", run)
    if stream:
        response = await router.sync_audiobookshelf_stream(None)
        assert dict(response.headers) == {"content-type": "text/event-stream; charset=utf-8"}
        frames = [json.loads(frame.removeprefix("data: ").strip()) async for frame in response.body_iterator]
        assert frames == [
            {"type": "progress", "current": 1, "total": 1, "title": "Example", "status": "added"},
            {"type": "done", **stats},
        ]
    else:
        assert await router.sync_audiobookshelf(None) is stats
    assert _status("abs")["last_status"] == "ok"


@pytest.mark.parametrize("client_fixture", ["viewer_client", "editor_client"])
@pytest.mark.parametrize("stream", [False, True])
def test_existing_abs_role_permissions_still_refuse_before_tracking(client_fixture, stream, request, monkeypatch):
    from app.routers import sync as router

    client = request.getfixturevalue(client_fixture)
    monkeypatch.setattr(router.audiobookshelf, "sync", AsyncMock(side_effect=AssertionError("Must not sync")))
    response = (client.get("/api/sync/audiobookshelf/stream") if stream
                else client.post("/api/sync/audiobookshelf"))
    assert response.status_code == 403
    assert _status("abs")["last_status"] == "never"
    assert _status("abs")["last_attempt_at"] is None


@pytest.mark.parametrize("stream", [False, True])
def test_existing_abs_feature_gate_still_refuses_before_tracking(admin_client, stream, monkeypatch):
    from app import features
    from app.routers import sync as router

    with get_db() as db:
        features.set_feature_enabled(db, "abs_sync", False)
    monkeypatch.setattr(router.audiobookshelf, "sync", AsyncMock(side_effect=AssertionError("Must not sync")))
    if stream:
        response = admin_client.get("/api/sync/audiobookshelf/stream", headers={"Accept": "text/event-stream"})
        assert response.status_code == 200
        assert json.loads(response.text.removeprefix("data: ").strip())["type"] == "error"
        assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    else:
        assert admin_client.post("/api/sync/audiobookshelf").status_code == 403
    assert _status("abs")["last_status"] == "never"
    assert _status("abs")["last_attempt_at"] is None
