"""RomM-specific lifetime/identity regressions; execute only after parent freeze.

HTTP transports are in-memory. Successful stages contain actual image bytes and
use the real shared publisher, item writer, schema and transaction outcome hooks.
"""

import asyncio
from contextlib import closing, contextmanager
import logging
from pathlib import Path
import sqlite3

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.staticfiles import StaticFiles

from app import config, features
from app.auth import create_token, get_current_user
from app.database import get_db, get_setting
from app.services import api_tokens, catalogue_security, covers, holdings, item_write, libraries
from app.services import romm_records, romm_sync, sync_jobs
from tests.conftest import _seed_personal_state
from tests.test_romm_sync import _webp_image


SERVER = "https://romm.example"
TOKEN = "local-regression-token"


def _seed(db, romm_id="guard-rom", platform_id="2"):
    result = romm_records.persist_candidate(db, {
        "romm_id": romm_id, "romm_platform_id": platform_id, "title": "Guard game",
        "platform": "snes", "platform_name": "Super Nintendo",
    })
    return result["item_id"]


def _files():
    return {path.name: path.read_bytes() for path in covers.COVERS_DIR.iterdir()} if covers.COVERS_DIR.exists() else {}


def _old_cover(item_id):
    image = _webp_image().replace(b"Local", b"Prior")
    covers.COVERS_DIR.mkdir(parents=True, exist_ok=True)
    (covers.COVERS_DIR / f"{item_id}.webp").write_bytes(image)
    return image


def _download(item_id, handler):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await romm_sync._ingest_cover(client, SERVER, TOKEN, item_id, "/cover.jpg", "guard-rom", "2")
    return asyncio.run(run())


def _resolved_session_principal(db, user):
    version = db.execute("SELECT token_version FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
    signed = create_token(user["id"], user["username"], user["role"], token_version=version)
    request = Request({"type": "http", "headers": [(b"cookie", f"access_token={signed}".encode())]})
    principal = get_current_user(request)
    assert principal is not None
    assert (principal["id"], principal["username"], principal["role"], principal["token_version"]) == (
        user["id"], user["username"], user["role"], version,
    )
    return principal


def _diagnostic_sink(monkeypatch, caplog):
    # Keep a real logging diagnostic, but isolate its sink from app's optional
    # SQLite log handler so exact whole-DB rollback assertions ignore NO tables.
    logger = logging.getLogger("romm_candidate004.cover_diagnostic")
    monkeypatch.setattr(romm_sync, "logger", logger)
    caplog.set_level(logging.WARNING, logger=logger.name)
    return logger.name


def _bind_cached_covers_mount(client, monkeypatch):
    """Rebind the existing real mount, not a new app or a second byte cache."""
    mounts = [route.app for route in client.app.routes if getattr(route, "path", None) == "/covers"]
    assert len(mounts) == 1 and isinstance(mounts[0], StaticFiles)
    mount = mounts[0]
    directory = str(config.COVERS_DIR)
    monkeypatch.setattr(mount, "directory", directory)
    monkeypatch.setattr(mount, "all_directories", [directory])
    return mount


@pytest.mark.parametrize("mount_binding", ["current", "cached_stale"])
def test_romm_real_webp_publication_uses_shared_stage_writer_and_mime(
    admin_client, db, monkeypatch, tmp_path, mount_binding,
):
    from app.main import app

    assert admin_client.app is app  # The shared fixture reuses this cached application.
    cached_mount = next(route.app for route in app.routes if getattr(route, "path", None) == "/covers")
    assert catalogue_security.current_actor() is None  # Trusted household/background job remains supported.
    item_id = _seed(db)
    db.commit()
    image = _webp_image()
    stale_image = image.replace(b"Local", b"Stale")
    stale_dir = tmp_path / "previous-test-covers"
    if mount_binding == "cached_stale":
        stale_dir.mkdir()
        assert stale_dir != config.COVERS_DIR and stale_image != image
        (stale_dir / f"{item_id}.webp").write_bytes(stale_image)
        monkeypatch.setattr(cached_mount, "directory", str(stale_dir))
        monkeypatch.setattr(cached_mount, "all_directories", [str(stale_dir)])
        stale_response = admin_client.get(f"/covers/{item_id}.webp")
        assert stale_response.status_code == 200
        assert stale_response.headers["content-type"] == "image/webp"
        assert stale_response.content == stale_image
    mount = _bind_cached_covers_mount(admin_client, monkeypatch)
    assert mount is cached_mount
    assert mount.directory == str(config.COVERS_DIR)
    assert mount.all_directories == [str(config.COVERS_DIR)]
    assert covers.COVERS_DIR == config.COVERS_DIR
    assert admin_client.get(f"/covers/{item_id}.webp").status_code == 404
    calls = []
    publisher = covers.publish_staged_cover
    writer = item_write.update_item_fields

    def inspect_publish(conn, stage, path, *, authorize, store):
        assert path == f"covers/{stage.download_id}.webp"
        assert stage.download_id < 0
        assert all(stage.path_for(suffix).parent == config.COVERS_DIR for suffix in stage.SUFFIXES)
        assert stage.path_for(".webp").read_bytes() == image

        def locked_authorize(conn):
            assert conn.in_transaction
            calls.append("authorize")
            return authorize(conn)

        return publisher(conn, stage, path, authorize=locked_authorize, store=store)

    def inspect_write(conn, target, fields, **kwargs):
        assert conn.in_transaction
        assert catalogue_security.current_actor() is None
        assert target == item_id
        assert fields == {"cover_path": f"covers/{item_id}.webp"}
        calls.append("store")
        return writer(conn, target, fields, **kwargs)

    def response(request):
        assert catalogue_security.current_actor() is None
        assert _files() == {}  # No final or pending file before the network await.
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert str(request.url) == f"{SERVER}/cover.jpg"
        return httpx.Response(200, content=image, headers={"content-type": "image/webp"})

    monkeypatch.setattr(covers, "publish_staged_cover", inspect_publish)
    monkeypatch.setattr(item_write, "update_item_fields", inspect_write)
    assert _download(item_id, response) is True
    assert calls == ["authorize", "store"]
    assert db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()[0] == f"covers/{item_id}.webp"
    assert _files() == {f"{item_id}.webp": image}
    served = admin_client.get(f"/covers/{item_id}.webp")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/webp"
    assert served.content == image
    assert served.content != stale_image
    assert admin_client.app is app and mount is cached_mount
    if mount_binding == "cached_stale":
        assert {path.name: path.read_bytes() for path in stale_dir.iterdir()} == {f"{item_id}.webp": stale_image}


@pytest.mark.parametrize("change", [
    "trash", "sidecar_rebind", "platform_rebind", "library_move", "appeared_cover",
    "membership_revoked", "account_revoked",
])
def test_romm_mid_download_change_preserves_final_and_cleans_all_owned_suffixes(
    db, editor_user, monkeypatch, change,
):
    item_id = _seed(db)
    other = item_write.insert_item(db, title="Other game", media_type="digital_game")
    destination = libraries.create_library(db, "Other allowed library")["id"]
    libraries.set_membership(db, destination, editor_user["id"], "editor")
    db.commit()
    old = _old_cover(item_id)
    unrelated = covers.COVERS_DIR / "-4242.webp"
    unrelated.write_bytes(old)
    image = _webp_image()
    changed_snapshot = []
    stage_ids = []
    saver = covers.stage_uploaded_cover

    def extra_pending(stage, content, *, content_type):
        path = saver(stage, content, content_type=content_type)
        assert path == f"covers/{stage.download_id}.webp"
        assert stage.download_id != -4242
        stage_ids.append(stage.download_id)
        # Model abandoned sibling filenames owned by THIS stage, not another job.
        # Every file contains real image bytes; none is used as a fake success.
        for suffix in stage.SUFFIXES:
            stage.path_for(suffix).write_bytes(image)
        return path

    def response(request):
        assert _files() == {f"{item_id}.webp": old, "-4242.webp": old}
        with get_db() as conn:
            if change == "trash":
                assert item_write.trash_item(conn, item_id)
            elif change == "sidecar_rebind":
                conn.execute("UPDATE romm_records SET item_id = ? WHERE romm_id = 'guard-rom'", (other,))
            elif change == "platform_rebind":
                conn.execute("UPDATE romm_records SET platform_id = '1' WHERE romm_id = 'guard-rom'")
            elif change == "library_move":
                item_write.update_item_fields(conn, item_id, {"library_id": destination})
            elif change == "appeared_cover":
                item_write.update_item_fields(conn, item_id, {"cover_path": f"covers/{item_id}.webp"})
            elif change == "membership_revoked":
                libraries.set_membership(conn, 1, editor_user["id"], "viewer")
            else:
                conn.execute("UPDATE users SET role = 'viewer' WHERE id = ?", (editor_user["id"],))
            changed_snapshot.append(tuple(conn.iterdump()))
        return httpx.Response(200, content=image, headers={"content-type": "image/webp"})

    monkeypatch.setattr(covers, "stage_uploaded_cover", extra_pending)
    actor = catalogue_security.set_actor(editor_user)
    try:
        assert _download(item_id, response) is False
    finally:
        catalogue_security.reset_actor(actor)
    assert len(stage_ids) == 1
    assert tuple(db.iterdump()) == changed_snapshot[0]
    assert _files() == {f"{item_id}.webp": old, "-4242.webp": old}


@pytest.mark.parametrize("fault", ["missing", "different_bytes", "symlink", "wrong_suffix"])
def test_romm_checks_actual_staged_bytes_not_a_success_path(db, monkeypatch, fault):
    item_id = _seed(db)
    db.commit()
    old = _old_cover(item_id)
    image = _webp_image()
    sentinel = covers.COVERS_DIR / "other.webp"
    sentinel.write_bytes(old)
    before = tuple(db.iterdump())
    saver = covers.stage_uploaded_cover
    reached = []

    def corrupt_stage(stage, content, *, content_type):
        path = saver(stage, content, content_type=content_type)
        pending = stage.path_for(".webp")
        assert pending.read_bytes() == image
        reached.append(path)
        if fault == "missing":
            pending.unlink()
        elif fault == "different_bytes":
            pending.write_bytes(old)
        elif fault == "symlink":
            pending.unlink()
            pending.symlink_to(sentinel)
        else:
            pending.replace(stage.path)
            return f"covers/{stage.download_id}.jpg"
        return path

    monkeypatch.setattr(covers, "stage_uploaded_cover", corrupt_stage)
    assert _download(item_id, lambda request: httpx.Response(
        200, content=image, headers={"content-type": "image/webp"},
    )) is False
    assert len(reached) == 1
    assert tuple(db.iterdump()) == before
    assert _files() == {f"{item_id}.webp": old, "other.webp": old}


@pytest.mark.parametrize("failure", ["store", "outer_rollback"])
def test_romm_cover_file_and_db_recover_together(db, monkeypatch, failure):
    item_id = _seed(db)
    db.commit()
    old = _old_cover(item_id)
    image = _webp_image()
    before = tuple(db.iterdump())
    reached = []
    if failure == "store":
        writer = item_write.update_item_fields

        def refusing_store(conn, target, fields, **kwargs):
            writer(conn, target, fields, **kwargs)
            reached.append("store")
            raise ValueError("injected after canonical cover update")

        monkeypatch.setattr(item_write, "update_item_fields", refusing_store)
    else:
        native_get_db = romm_sync.get_db
        acquisitions = 0

        @contextmanager
        def refusing_outer_transaction():
            nonlocal acquisitions
            acquisitions += 1
            with native_get_db() as conn:
                yield conn
                if acquisitions == 2:
                    during = _files()
                    backups = {name: data for name, data in during.items() if name.startswith(".cover-backup-")}
                    assert len(backups) == 1
                    assert list(backups.values()) == [old]
                    assert during == {f"{item_id}.webp": image, **backups}
                    reached.append("outer_rollback")
                    raise ValueError("injected after real publication before outer commit")

        monkeypatch.setattr(romm_sync, "get_db", refusing_outer_transaction)
    with pytest.raises(ValueError, match="injected after"):
        _download(item_id, lambda request: httpx.Response(
            200, content=image, headers={"content-type": "image/webp"},
        ))
    assert reached == [failure]
    assert tuple(db.iterdump()) == before
    assert _files() == {f"{item_id}.webp": old}


@pytest.mark.parametrize("target", [
    "https://images.example/cover.webp", "http://romm.example/cover.webp",
    "https://romm.example:444/cover.webp", "//images.example/cover.webp",
    "https://user:secret@romm.example/cover.webp", "https://romm.example:bad/cover.webp",
])
def test_romm_strict_cover_origin_makes_zero_requests_or_writes(db, target):
    item_id = _seed(db)
    db.commit()
    old = _old_cover(item_id)
    before = tuple(db.iterdump())
    calls = []

    def forbidden_request(request):
        calls.append(request)
        pytest.fail("strict-origin refusal sent a request/credential")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_request)) as client:
            return await romm_sync._ingest_cover(client, SERVER, TOKEN, item_id, target, "guard-rom", "2")

    assert asyncio.run(run()) is False
    assert calls == []
    assert tuple(db.iterdump()) == before
    assert _files() == {f"{item_id}.webp": old}


@pytest.mark.parametrize("response_kind", ["redirect", "mismatched_mime"])
def test_romm_cover_redirect_or_mime_mismatch_never_publishes(db, response_kind):
    item_id = _seed(db)
    db.commit()
    old = _old_cover(item_id)
    before = tuple(db.iterdump())
    calls = []

    def response(request):
        calls.append(str(request.url))
        assert str(request.url) == f"{SERVER}/cover.jpg"
        if response_kind == "redirect":
            return httpx.Response(302, headers={"location": "https://images.example/cover.webp"})
        return httpx.Response(200, content=_webp_image(), headers={"content-type": "image/jpeg"})

    assert _download(item_id, response) is False
    assert calls == [f"{SERVER}/cover.jpg"]
    assert tuple(db.iterdump()) == before
    assert _files() == {f"{item_id}.webp": old}


@pytest.mark.parametrize("target", ["missing", "hidden", "trashed"])
@pytest.mark.parametrize("endpoint", ["open", "action"])
def test_romm_unavailable_http_target_is_generic_404_without_service_or_effects(
    viewer_client, db, monkeypatch, target, endpoint,
):
    item_id = 999999
    if target != "missing":
        item_id = _seed(db)
        if target == "hidden":
            hidden = libraries.create_library(db, "Private RomM library")["id"]
            libraries.assign_item(db, item_id, hidden)
        else:
            assert item_write.trash_item(db, item_id)
    db.commit()
    before = tuple(db.iterdump())

    def forbidden_action(*args, **kwargs):
        pytest.fail("unavailable target reached RomM action/provider work")

    monkeypatch.setattr(romm_sync, "item_action", forbidden_action)
    response = viewer_client.get(f"/api/romm/items/{item_id}/{endpoint}", follow_redirects=False)
    assert response.status_code == 404
    assert response.json() == {"detail": "Item not found"}
    assert "Private RomM" not in response.text
    assert "Guard game" not in response.text
    assert "location" not in response.headers
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("retained", ["romm", "other_provider", "physical_copy"])
def test_romm_cleanup_detaches_excluded_identity_but_preserves_retained_holdings(db, retained):
    item_id = _seed(db)
    if retained == "romm":
        db.execute("INSERT INTO romm_records (romm_id, item_id, platform_id) VALUES ('retained-rom', ?, '1')", (item_id,))
        holdings.sync_item(db, item_id)
    elif retained == "other_provider":
        holdings.reconcile_digital_holdings(db, item_id, "other-provider", [{"external_id": "keep-me"}])
    else:
        # An exact historical physical attachment is not retyped or trashed.
        item_write.update_item_fields(db, item_id, {"media_type": "video_game", "owned": 0})
        item_write.update_item_fields(db, item_id, {"owned": 1})
        assert db.execute("SELECT COUNT(*) FROM copies_live WHERE item_id = ?", (item_id,)).fetchone()[0] == 1
    copies = [tuple(row) for row in db.execute("SELECT * FROM item_copies WHERE item_id = ?", (item_id,))]
    assert romm_records.cleanup_excluded_platforms(db, {"2"}) == {"deleted": 0, "detached": 1}
    item = db.execute("SELECT deleted_at, romm_id, romm_platform_id, source FROM items WHERE id = ?", (item_id,)).fetchone()
    assert dict(item) == {"deleted_at": None, "romm_id": None, "romm_platform_id": None, "source": "romm"}
    assert db.execute("SELECT 1 FROM romm_records WHERE romm_id = 'guard-rom'").fetchone() is None
    assert db.execute("SELECT 1 FROM digital_holdings WHERE provider = 'romm' AND external_id = 'guard-rom'").fetchone() is None
    assert [tuple(row) for row in db.execute("SELECT * FROM item_copies WHERE item_id = ?", (item_id,))] == copies
    if retained == "romm":
        assert romm_records.records_for_item(db, item_id) == [{"romm_id": "retained-rom", "platform_id": "1"}]
    elif retained == "other_provider":
        assert db.execute("SELECT external_id FROM digital_holdings WHERE provider = 'other-provider'").fetchone()[0] == "keep-me"


def test_romm_cleanup_trashes_once_without_deleting_provider_or_personal_history(db, viewer_user):
    item_id = _seed(db)
    db.execute("INSERT INTO romm_records (romm_id, item_id, platform_id) VALUES ('another-excluded-rom', ?, '2')", (item_id,))
    holdings.sync_item(db, item_id)
    _seed_personal_state(db, viewer_user["id"], item_id, personal_notes="Retain history", reading_status="read")
    before_children = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                       for table in ("romm_records", "digital_holdings", "user_item_state", "item_copies")}
    assert romm_records.cleanup_excluded_platforms(db, {"2"}) == {"deleted": 1, "detached": 0}
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (item_id,)).fetchone()[0] is not None
    assert db.execute("SELECT 1 FROM items_live WHERE id = ?", (item_id,)).fetchone() is None
    assert {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in before_children} == before_children
    before_repeat = tuple(db.iterdump())
    assert romm_records.cleanup_excluded_platforms(db, {"2"}) == {"deleted": 0, "detached": 0}
    assert tuple(db.iterdump()) == before_repeat


def test_romm_cleanup_includes_legacy_digital_holding_only_availability(db):
    item_id = _seed(db)
    # Historical availability represented solely by the retained holding seam.
    db.execute("DELETE FROM romm_records WHERE item_id = ?", (item_id,))
    db.execute("UPDATE items SET romm_id = NULL, romm_platform_id = NULL WHERE id = ?", (item_id,))
    holding = [tuple(row) for row in db.execute("SELECT * FROM digital_holdings WHERE item_id = ?", (item_id,))]
    assert len(holding) == 1
    assert romm_records.cleanup_excluded_platforms(db, {"2"}) == {"deleted": 1, "detached": 0}
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (item_id,)).fetchone()[0] is not None
    assert [tuple(row) for row in db.execute("SELECT * FROM digital_holdings WHERE item_id = ?", (item_id,))] == holding


def test_romm_cleanup_busy_native_lock_has_no_effects(admin_client, db, monkeypatch):
    _seed(db)
    db.execute("INSERT INTO settings (key, value) VALUES ('romm_excluded_platforms', '[\"2\"]')")
    db.commit()
    before = tuple(db.iterdump())
    monkeypatch.setattr(sync_jobs, "is_running", lambda provider: False)
    assert romm_sync._sync_lock.acquire(blocking=False)
    try:
        response = admin_client.post("/api/sync/romm/platforms/cleanup")
    finally:
        romm_sync._sync_lock.release()
    assert response.status_code == 409
    assert response.json() == {"ok": False, "message": "RomM sync is running"}
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("interval", ["off", "daily", "weekly"])
def test_romm_disabled_schedule_configuration_is_admin_only_and_starts_no_work(admin_client, db, monkeypatch, interval):
    features.set_feature_enabled(db, "romm", False)
    db.commit()
    before = {tuple(row) for row in db.execute("SELECT key, value FROM settings")}

    def forbidden_work(*args, **kwargs):
        pytest.fail("schedule configuration started sync/job/provider work")

    monkeypatch.setattr(sync_jobs, "start", forbidden_work)
    monkeypatch.setattr(romm_sync, "sync", forbidden_work)
    monkeypatch.setattr(romm_sync.romm_client, "fetch_platforms", forbidden_work)
    response = admin_client.post("/api/sync/romm/schedule", data={"interval": interval}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert get_setting(db, "romm_sync_interval") == interval
    assert {tuple(row) for row in db.execute("SELECT key, value FROM settings")} == {
        row for row in before if row[0] != "romm_sync_interval"
    } | {("romm_sync_interval", interval)}
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_romm_disabled_schedule_refuses_viewer_without_effects(viewer_client, db):
    features.set_feature_enabled(db, "romm", False)
    db.commit()
    before = tuple(db.iterdump())
    response = viewer_client.post("/api/sync/romm/schedule", data={"interval": "daily"}, follow_redirects=False)
    assert response.status_code == 403
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("interval", ["hourly", "monthly"])
def test_romm_disabled_schedule_still_validates_without_effects(admin_client, db, interval):
    features.set_feature_enabled(db, "romm", False)
    db.commit()
    before = tuple(db.iterdump())
    response = admin_client.post("/api/sync/romm/schedule", data={"interval": interval}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json() == {"ok": False, "message": "Invalid sync interval"}
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("fault", ["writer", "filesystem"])
@pytest.mark.parametrize("healthy_sibling", [False, True])
def test_romm_real_batch_counts_one_failure_recovers_exactly_and_continues(
    db, monkeypatch, caplog, fault, healthy_sibling,
):
    failed = _seed(db)
    healthy = _seed(db, "healthy-rom") if healthy_sibling else None
    db.commit()
    old = _old_cover(failed)
    image = _webp_image()
    before = tuple(db.iterdump())
    failed_before = dict(db.execute("SELECT * FROM items WHERE id = ?", (failed,)).fetchone())
    child_before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} WHERE item_id = ?", (failed,))]
                    for table in ("romm_records", "digital_holdings", "item_copies", "user_item_state")}
    diagnostic_name = _diagnostic_sink(monkeypatch, caplog)
    reached = []
    stage_ids = []
    saver = covers.stage_uploaded_cover

    def inspect_stage(stage, content, *, content_type):
        path = saver(stage, content, content_type=content_type)
        assert path == f"covers/{stage.download_id}.webp"
        assert stage.path_for(".webp").read_bytes() == image
        stage_ids.append(stage.download_id)
        return path

    monkeypatch.setattr(covers, "stage_uploaded_cover", inspect_stage)
    if fault == "writer":
        writer = item_write.update_item_fields

        def refusing_writer(conn, item_id, fields, **kwargs):
            writer(conn, item_id, fields, **kwargs)
            if item_id == failed:
                assert conn.execute("SELECT cover_path FROM items WHERE id = ?", (failed,)).fetchone()[0] == f"covers/{failed}.webp"
                reached.append(item_id)
                raise ValueError(f"injected after real canonical writer; credential={TOKEN}")

        monkeypatch.setattr(item_write, "update_item_fields", refusing_writer)
    else:
        replace = Path.replace

        def refusing_replace(path, target):
            result = replace(path, target)
            if path.name.startswith("-") and Path(target) == covers.COVERS_DIR / f"{failed}.webp":
                reached.append(failed)
                raise OSError(f"injected after real atomic replace; credential={TOKEN}")
            return result

        monkeypatch.setattr(Path, "replace", refusing_replace)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=image, headers={"content-type": "image/webp"},
        ))) as client:
            jobs = [(client, SERVER, TOKEN, failed, "/failed.webp", "guard-rom", "2")]
            if healthy_sibling:
                jobs.append((client, SERVER, TOKEN, healthy, "/healthy.webp", "healthy-rom", "2"))
            stats = {"covers": 0, "cover_errors": 0}
            await romm_sync._drain_cover_batch(jobs, stats)
            assert jobs == []
            return stats

    assert asyncio.run(run()) == {"covers": int(healthy_sibling), "cover_errors": 1}
    assert reached == [failed]
    assert len(stage_ids) == 1 + int(healthy_sibling)
    assert dict(db.execute("SELECT * FROM items WHERE id = ?", (failed,)).fetchone()) == failed_before
    assert {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} WHERE item_id = ?", (failed,))]
            for table in child_before} == child_before
    expected_files = {f"{failed}.webp": old}
    if healthy_sibling:
        assert db.execute("SELECT cover_path FROM items WHERE id = ?", (healthy,)).fetchone()[0] == f"covers/{healthy}.webp"
        expected_files[f"{healthy}.webp"] = image
    else:
        assert tuple(db.iterdump()) == before
    assert _files() == expected_files  # Includes absence of every pending/preimage file.
    messages = [record.getMessage() for record in caplog.records if record.name == diagnostic_name]
    error_type = "ValueError" if fault == "writer" else "OSError"
    assert messages == [f"RomM cover ingestion failed for item {failed} ({error_type})"]
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("fault", ["transport", "deadline", "http_503"])
def test_romm_real_batch_reports_transport_and_deadline_failures_not_missing_covers(db, monkeypatch, caplog, fault):
    item_id = _seed(db)
    db.commit()
    old = _old_cover(item_id)
    before = tuple(db.iterdump())
    diagnostic_name = _diagnostic_sink(monkeypatch, caplog)
    requests = []
    if fault == "deadline":
        monkeypatch.setattr(romm_sync, "COVER_WALL_TIMEOUT", 0)

    async def run():
        never = asyncio.Event()

        async def response(request):
            requests.append(str(request.url))
            if fault == "transport":
                raise httpx.ReadTimeout(f"synthetic transport credential={TOKEN}", request=request)
            if fault == "deadline":
                await never.wait()  # The actual asyncio deadline cancels this suspended request.
            return httpx.Response(503, content=_webp_image(), headers={"content-type": "image/webp"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            jobs = [(client, SERVER, TOKEN, item_id, "/failed.webp", "guard-rom", "2")]
            stats = {"covers": 0, "cover_errors": 0}
            await romm_sync._drain_cover_batch(jobs, stats)
            assert jobs == []
            return stats

    assert asyncio.run(run()) == {"covers": 0, "cover_errors": 1}
    assert requests == [f"{SERVER}/failed.webp"]
    assert tuple(db.iterdump()) == before
    assert _files() == {f"{item_id}.webp": old}
    error_type = {"transport": "ReadTimeout", "deadline": "TimeoutError", "http_503": "RomMCoverError"}[fault]
    assert [record.getMessage() for record in caplog.records if record.name == diagnostic_name] == [
        f"RomM cover ingestion failed for item {item_id} ({error_type})",
    ]
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("refusal", ["no_target", "missing_404", "appeared_cover", "mismatched_mime"])
def test_romm_real_batch_expected_cover_refusals_are_not_operational_errors(db, monkeypatch, caplog, refusal):
    item_id = _seed(db)
    db.commit()
    old = _old_cover(item_id)
    before = tuple(db.iterdump())
    selected = []
    requests = []
    diagnostic_name = _diagnostic_sink(monkeypatch, caplog)

    def response(request):
        requests.append(str(request.url))
        if refusal == "appeared_cover":
            with get_db() as conn:
                item_write.update_item_fields(conn, item_id, {"cover_path": f"covers/{item_id}.webp"})
                selected.append(tuple(conn.iterdump()))
        status = 404 if refusal == "missing_404" else 200
        mime = "image/jpeg" if refusal == "mismatched_mime" else "image/webp"
        return httpx.Response(status, content=_webp_image(), headers={"content-type": mime})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            target = None if refusal == "no_target" else "/optional.webp"
            jobs = [(client, SERVER, TOKEN, item_id, target, "guard-rom", "2")]
            stats = {"covers": 0, "cover_errors": 0}
            await romm_sync._drain_cover_batch(jobs, stats)
            assert jobs == []
            return stats

    assert asyncio.run(run()) == {"covers": 0, "cover_errors": 0}
    assert requests == ([] if refusal == "no_target" else [f"{SERVER}/optional.webp"])
    assert tuple(db.iterdump()) == (selected[0] if refusal == "appeared_cover" else before)
    assert _files() == {f"{item_id}.webp": old}
    assert [record for record in caplog.records if record.name == diagnostic_name] == []


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_romm_unchanged_resolved_authority_publishes_with_final_decision_and_writer_locks(
    db, editor_user, viewer_user, monkeypatch, role,
):
    item_id = _seed(db)
    user = editor_user if role == "editor" else viewer_user
    if role == "viewer":
        libraries.set_membership(db, 1, user["id"], "editor")  # D5: real library authority, no global promotion.
    db.commit()
    principal = _resolved_session_principal(db, user)
    image = _webp_image()
    binding = romm_records.cover_binding
    writer = item_write.update_item_fields
    decisions = []
    locks = []

    def prove_writer_lock(phase):
        with closing(sqlite3.connect(config.DATABASE_PATH, timeout=0)) as rival:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                rival.execute("BEGIN IMMEDIATE")
        locks.append(phase)

    def checked_binding(conn, target, romm_id=None, platform_id=None):
        assert catalogue_security.current_actor() == principal
        assert (target, romm_id, platform_id) == (item_id, "guard-rom", "2")
        decisions.append(target)
        if len(decisions) == 2:
            prove_writer_lock("final-decision")
        return binding(conn, target, romm_id, platform_id)

    def checked_writer(conn, target, fields, **kwargs):
        assert catalogue_security.current_actor() == principal
        assert target == item_id and fields == {"cover_path": f"covers/{item_id}.webp"}
        prove_writer_lock("final-writer")
        return writer(conn, target, fields, **kwargs)

    def response(request):
        assert catalogue_security.current_actor() == principal
        assert _files() == {}
        return httpx.Response(200, content=image, headers={"content-type": "image/webp"})

    monkeypatch.setattr(romm_records, "cover_binding", checked_binding)
    monkeypatch.setattr(item_write, "update_item_fields", checked_writer)
    actor = catalogue_security.set_actor(principal)
    try:
        assert _download(item_id, response) is True
        assert catalogue_security.current_actor() == principal
    finally:
        catalogue_security.reset_actor(actor)
    assert decisions == [item_id, item_id]
    assert locks == ["final-decision", "final-writer"]
    assert _files() == {f"{item_id}.webp": image}
    assert db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()[0] == f"covers/{item_id}.webp"
    with closing(sqlite3.connect(config.DATABASE_PATH, timeout=0)) as rival:
        rival.execute("BEGIN IMMEDIATE")  # Real outer commit released the lock.
        rival.rollback()


def test_romm_actually_resolved_admin_bearer_remains_viewer_and_never_downloads(client, db, admin_user):
    item_id = _seed(db)
    issued = api_tokens.create(db, admin_user["id"], "RomM cover cap regression")
    assert issued is not None
    features.set_feature_enabled(db, "api", True)
    db.commit()
    # Real HTTP bearer authentication/read succeeds; no hand-built request.state.user.
    assert client.get("/api/v1/stats", headers={"Authorization": f"Bearer {issued['token']}"}).status_code == 200
    principal = api_tokens.resolve(issued["token"])
    assert principal["role"] == "viewer"
    assert principal["account_role"] == "admin"
    assert principal["api_token_id"] == issued["id"]
    before = tuple(db.iterdump())
    old = _old_cover(item_id)
    requests = []

    def forbidden_request(request):
        requests.append(str(request.url))
        pytest.fail("viewer-capped bearer reached the provider")

    actor = catalogue_security.set_actor(principal)
    try:
        assert catalogue_security.require_item_role(db, item_id, "viewer")["id"] == item_id
        with pytest.raises(HTTPException) as refused:
            catalogue_security.require_item_role(db, item_id, "editor")
        assert refused.value.status_code == 403
        assert _download(item_id, forbidden_request) is False
    finally:
        catalogue_security.reset_actor(actor)
    assert requests == []
    assert tuple(db.iterdump()) == before
    assert _files() == {f"{item_id}.webp": old}


@pytest.mark.parametrize("finish", ["expired_scope", "cancelled"])
def test_romm_event_suspended_request_scope_expiry_and_cancellation_clean_real_pending_bytes(
    db, editor_user, monkeypatch, finish,
):
    item_id = _seed(db)
    db.commit()
    principal = _resolved_session_principal(db, editor_user)
    before = tuple(db.iterdump())
    old = _old_cover(item_id)
    unrelated = covers.COVERS_DIR / "-4242.webp"
    unrelated.write_bytes(old)
    image = _webp_image()
    stages = []
    stage_type = covers.CoverStage

    class ObservedStage(stage_type):
        def __enter__(self):
            stages.append(self)
            return super().__enter__()

    monkeypatch.setattr(covers, "CoverStage", ObservedStage)

    async def run():
        suspended, resume = asyncio.Event(), asyncio.Event()
        actor = catalogue_security.set_actor(principal)
        task = None

        async def response(request):
            assert catalogue_security.current_actor() == principal
            assert len(stages) == 1 and stages[0].download_id != -4242
            # Real validated bytes model a partial producer's owned pending file.
            pending = covers.stage_uploaded_cover(stages[0], image, content_type="image/webp")
            assert pending == f"covers/{stages[0].download_id}.webp"
            assert stages[0].path_for(".webp").read_bytes() == image
            suspended.set()
            await resume.wait()
            return httpx.Response(200, content=image, headers={"content-type": "image/webp"})

        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                task = asyncio.create_task(romm_sync._ingest_cover(
                    client, SERVER, TOKEN, item_id, "/suspended.webp", "guard-rom", "2",
                ))
                await asyncio.wait_for(suspended.wait(), timeout=5)
                assert not task.done()
                assert stages[0].path_for(".webp").read_bytes() == image
                if finish == "expired_scope":
                    catalogue_security.reset_actor(actor)  # Invalidates the copied task's real holder.
                    actor = None
                    assert catalogue_security.current_actor() is None
                    resume.set()
                    assert await asyncio.wait_for(task, timeout=5) is False
                else:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
        finally:
            resume.set()
            if task is not None and not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            if actor is not None:
                catalogue_security.reset_actor(actor)
        assert catalogue_security.current_actor() is None

    asyncio.run(run())
    assert len(stages) == 1
    assert tuple(db.iterdump()) == before
    assert _files() == {f"{item_id}.webp": old, "-4242.webp": old}


def test_romm_later_cleanup_authority_refusal_rolls_back_earlier_detach_inside_committing_caller(
    db, editor_user, monkeypatch,
):
    targets = [_seed(db, "cleanup-a"), _seed(db, "cleanup-b")]
    second_library = libraries.create_library(db, "Cleanup second library")["id"]
    libraries.set_membership(db, second_library, editor_user["id"], "editor")
    item_write.update_item_fields(db, targets[1], {"library_id": second_library})
    for item_id in targets:
        item_write.update_item_fields(db, item_id, {"source": "manual"})
    unrelated = item_write.insert_item(db, title="Unrelated caller work", media_type="book")
    db.commit()
    principal = _resolved_session_principal(db, editor_user)
    detach = romm_records._detach_record
    visited = []
    partial = []

    def later_refusal(conn, romm_id):
        item = romm_records._attachment(conn, romm_id)
        visited.append(item["id"])
        if len(visited) == 1:
            detach(conn, romm_id)
            assert conn.execute("SELECT romm_id FROM items WHERE id = ?", (item["id"],)).fetchone()[0] is None
            assert conn.execute("SELECT 1 FROM romm_records WHERE romm_id = ?", (romm_id,)).fetchone() is None
            partial.append(item["id"])
        else:
            assert len(visited) == 2 and partial == visited[:1]
            # Inject persisted revocation only after the first REAL detach. The
            # second native guard, not a manufactured exception, must refuse.
            parent = libraries.item_library_id(conn, item["id"])
            libraries.set_membership(conn, parent, editor_user["id"], "viewer")
            detach(conn, romm_id)
            pytest.fail("later native editor guard did not refuse")

    monkeypatch.setattr(romm_records, "_detach_record", later_refusal)
    actor = catalogue_security.set_actor(principal)
    try:
        with get_db() as conn:
            item_write.update_item_fields(conn, unrelated, {"notes": "caller work must commit"})
            before_cleanup = tuple(conn.iterdump())
            with pytest.raises(HTTPException) as refused:
                romm_records.cleanup_excluded_platforms(conn, {"2"})
            assert refused.value.status_code == 403
            assert tuple(conn.iterdump()) == before_cleanup
            assert len(visited) == 2 and set(visited) == set(targets)
            # Exception is caught INSIDE this caller: its outer transaction will
            # commit, so a partial cleanup cannot be hidden by caller rollback.
    finally:
        catalogue_security.reset_actor(actor)
    assert tuple(db.iterdump()) == before_cleanup
    assert db.execute("SELECT notes FROM items WHERE id = ?", (unrelated,)).fetchone()[0] == "caller work must commit"
    assert _files() == {}
