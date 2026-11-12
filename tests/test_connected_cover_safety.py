"""Sync cover downloads must not replace local decisions or leak credentials."""

import sqlite3
from contextlib import contextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from app import config
from app.database import get_db
from app.services import covers, komga_sync, romm_sync
from app.services.item_write import trash_item, update_item_fields
from tests.conftest import _insert_item


IMAGE = b"\xff\xd8\xff" + b"downloaded image" * 15
LOCAL_BYTES = b"human-selected cover bytes"


@pytest.fixture(params=["komga", "romm"])
def provider(request):
    return request.param


def _module(provider):
    return komga_sync if provider == "komga" else romm_sync


def _server(provider):
    return f"https://{provider}.example"


def _cover_url(provider):
    return (_server(provider) + "/api/v1/books/one/thumbnail" if provider == "komga"
            else _server(provider) + "/assets/cover.jpg")


def _seed(provider, **fields):
    with get_db() as db:
        return _insert_item(
            db, isbn=None, media_type="comic" if provider == "komga" else "video_game",
            updated_at="2000-01-01", **fields,
        )


def _row(item_id):
    with get_db() as db:
        # Test snapshots deliberately include Trash, which must remain untouched.
        return dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())


async def _ingest(provider, client, item_id):
    return await _module(provider)._ingest_cover(
        client, _server(provider), "private-token", item_id,
        "one" if provider == "komga" else "/assets/cover.jpg",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["selected_default", "selected_other", "trash", "dismiss"])
async def test_local_choice_while_cover_request_is_in_flight_is_never_overwritten(provider, decision):
    item_id = _seed(provider)
    destination = config.COVERS_DIR / f"{item_id}.jpg"
    selected = config.COVERS_DIR / "selected.jpg"
    selected.write_bytes(LOCAL_BYTES)
    snapshot = None
    requests = []

    async def download(request):
        nonlocal snapshot
        requests.append(request)
        # This writer must be able to run while the network is pending: the
        # sync must not hold its own transaction around the await.
        with get_db() as db:
            db.execute("PRAGMA busy_timeout=0")
            db.execute("BEGIN IMMEDIATE")
            if decision.startswith("selected"):
                destination.write_bytes(LOCAL_BYTES)
                path = f"covers/{item_id}.jpg" if decision == "selected_default" else "covers/selected.jpg"
                update_item_fields(db, item_id, {"cover_path": path})
            elif decision == "trash":
                assert trash_item(db, item_id)
            else:
                db.execute("UPDATE items SET cover_review_dismissed = 1 WHERE id = ?", (item_id,))
        snapshot = _row(item_id)
        return httpx.Response(200, content=IMAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as client:
        assert await _ingest(provider, client, item_id) is False

    assert len(requests) == 1
    assert _row(item_id) == snapshot
    assert selected.read_bytes() == LOCAL_BYTES
    if decision.startswith("selected"):
        assert destination.read_bytes() == LOCAL_BYTES
    else:
        assert not destination.exists()
    if decision == "trash":
        assert snapshot["deleted_at"] is not None
    if decision == "dismiss":
        assert snapshot["cover_review_dismissed"] == 1


@pytest.mark.asyncio
async def test_trash_during_download_does_not_even_overwrite_an_unreferenced_cover_file(provider):
    item_id = _seed(provider)
    destination = config.COVERS_DIR / f"{item_id}.jpg"
    destination.write_bytes(LOCAL_BYTES)

    async def download(request):
        with get_db() as db:
            assert trash_item(db, item_id)
        return httpx.Response(200, content=IMAGE)

    async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as client:
        assert await _ingest(provider, client, item_id) is False

    assert destination.read_bytes() == LOCAL_BYTES
    assert _row(item_id)["cover_path"] is None
    assert _row(item_id)["deleted_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", ["selected", "dismissed", "trash"])
async def test_existing_cover_dismissal_or_trash_skips_cover_network_entirely(provider, existing):
    item_id = _seed(provider)
    with get_db() as db:
        if existing == "selected":
            update_item_fields(db, item_id, {"cover_path": "covers/selected.jpg"})
        elif existing == "dismissed":
            db.execute("UPDATE items SET cover_review_dismissed = 1 WHERE id = ?", (item_id,))
        else:
            assert trash_item(db, item_id)
    before = _row(item_id)

    def unexpected_request(request):
        pytest.fail("No cover request is allowed after a local no-fill decision")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        assert await _ingest(provider, client, item_id) is False

    assert _row(item_id) == before


@pytest.mark.asyncio
async def test_success_validates_before_lock_and_rechecks_under_write_lock_before_writing(provider, monkeypatch):
    module = _module(provider)
    item_id = _seed(provider, cover_edition_isbn="9780441172719", cover_edition_language="en")
    active = []
    queries = []
    original_validate = covers.validate_uploaded_cover
    original_write = covers.write_uploaded_cover
    validated = []
    requests = []

    @contextmanager
    def observed_db():
        with get_db() as db:
            db.set_trace_callback(queries.append)
            active.append(db)
            try:
                yield db
            finally:
                active.pop()

    def validate(content):
        assert not active, "Image validation must happen before acquiring the write lock"
        validated.append(content)
        return original_validate(content)

    def write(target_id, content):
        assert validated == [IMAGE]
        assert len(active) == 1 and active[0].in_transaction
        # Prove a write lock, not merely an open/deferred transaction (G18).
        contender = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()
        locked_queries = queries[queries.index("BEGIN IMMEDIATE") + 1:]
        assert any("FROM items_live" in sql and "cover_review_dismissed" in sql for sql in locked_queries)
        assert not (config.COVERS_DIR / f"{item_id}.jpg").exists()
        return original_write(target_id, content)

    def download(request):
        assert not active, "No DB connection should be held during HTTP"
        requests.append(request)
        return httpx.Response(200, content=IMAGE)

    def unexpected_log(*args, **kwargs):
        pytest.fail("Cover ingestion must not log under its write lock")

    monkeypatch.setattr(module, "get_db", observed_db)
    monkeypatch.setattr(covers, "validate_uploaded_cover", validate)
    monkeypatch.setattr(covers, "write_uploaded_cover", write)
    monkeypatch.setattr(module.logger, "warning", unexpected_log)
    monkeypatch.setattr(module.logger, "exception", unexpected_log)
    monkeypatch.setattr(covers.logger, "warning", unexpected_log)

    async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as client:
        assert await _ingest(provider, client, item_id) is True
        assert await _ingest(provider, client, item_id) is False  # Fill-only on future passes.

    assert len(requests) == 1
    assert requests[0].method == "GET"
    if provider == "komga":
        assert requests[0].headers["X-API-Key"] == "private-token"
    else:
        assert requests[0].headers["Authorization"] == "Bearer private-token"
    assert (config.COVERS_DIR / f"{item_id}.jpg").read_bytes() == IMAGE
    row = _row(item_id)
    assert row["cover_path"] == f"covers/{item_id}.jpg"
    assert row["cover_edition_isbn"] is None and row["cover_edition_language"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["html", "too_small", "too_large", "http_error", "timeout"])
async def test_invalid_or_failed_cover_downloads_never_touch_files_or_items(provider, response_kind, monkeypatch):
    item_id = _seed(provider)
    before = _row(item_id)
    monkeypatch.setattr(covers, "MAX_COVER_SIZE", 256)

    def download(request):
        if response_kind == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        content = {
            "html": b"<html>not an image</html>" * 5,
            "too_small": b"\xff\xd8\xff",
            "too_large": b"\xff\xd8\xff" + b"x" * 256,
            "http_error": IMAGE,
        }[response_kind]
        return httpx.Response(503 if response_kind == "http_error" else 200, content=content)

    def unexpected_write(*args, **kwargs):
        pytest.fail("Rejected cover bytes must never reach the file writer")

    monkeypatch.setattr(covers, "write_uploaded_cover", unexpected_write)
    async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as client:
        assert await _ingest(provider, client, item_id) is False
    assert _row(item_id) == before
    assert not (config.COVERS_DIR / f"{item_id}.jpg").exists()


def _candidate(provider):
    if provider == "komga":
        return {"komga_id": "one", "komga_library_id": "library", "library_kind": "comic", "title": "Example"}
    return {"romm_id": "one", "romm_platform_id": "1", "platform": "snes",
            "title": "Example", "cover_url": "/assets/cover.jpg"}


def _configure_sync(provider, monkeypatch):
    module = _module(provider)
    if provider == "komga":
        module.save_configuration(url=_server(provider), api_key="private-token")
        monkeypatch.setattr(module.komga_libraries, "fetch_libraries", AsyncMock(return_value=[
            {"id": "library", "kind": "comic"},
        ]))
        monkeypatch.setattr(module.komga_books, "fetch_library_candidates", AsyncMock(return_value=[_candidate(provider)]))
    else:
        module.save_configuration(url=_server(provider), token="private-token")
        monkeypatch.setattr(module.romm_client, "fetch_platforms", AsyncMock(return_value=[
            {"id": "1", "rom_count": 1},
        ]))

        async def candidates(*args):
            yield _candidate(provider)

        monkeypatch.setattr(module.romm_client, "iter_rom_candidates", candidates)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["dismiss", "remove"])
async def test_explicit_no_cover_decisions_survive_public_sync_and_keep_stats_shape(provider, decision, monkeypatch):
    module = _configure_sync(provider, monkeypatch)
    records = module.komga_records if provider == "komga" else module.romm_records
    with get_db() as db:
        item_id = records.persist_candidate(db, _candidate(provider))["item_id"]
    destination = config.COVERS_DIR / f"{item_id}.jpg"
    if decision == "remove":
        # Exercise the real removal endpoint without prescribing its dismissal
        # flag: the retained local bytes must survive either representation.
        from app.routers.items_covers import cover_remove

        destination.write_bytes(LOCAL_BYTES)
        with get_db() as db:
            update_item_fields(db, item_id, {"cover_path": f"covers/{item_id}.jpg"})
        assert (await cover_remove(item_id)).status_code == 200
    else:
        with get_db() as db:
            db.execute("UPDATE items SET cover_review_dismissed = 1 WHERE id = ?", (item_id,))

    with respx.mock(assert_all_called=False) as mock:
        cover = mock.get(_cover_url(provider)).respond(200, content=IMAGE)
        progress = AsyncMock()
        stats = await module.sync(on_progress=progress)
        if decision == "dismiss":
            assert cover.call_count == 0

    expected = {"created": 0, "updated": 1, "skipped": 0, "in_trash": 0, "errors": 0}
    if provider == "komga":
        expected["adopted"] = 0
    assert stats == expected
    progress.assert_awaited_once_with(1, 1, "Example", "updated")
    row = _row(item_id)
    assert row["cover_path"] is None
    if decision == "remove":
        assert destination.read_bytes() == LOCAL_BYTES
    else:
        assert row["cover_review_dismissed"] == 1
        assert not destination.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("cover_url", [
    "http://romm.example/assets/cover.jpg",
    "https://romm.example:8443/assets/cover.jpg",
    "https://user:password@romm.example/assets/cover.jpg",
    "https://@romm.example/assets/cover.jpg",
    "//user:password@romm.example/assets/cover.jpg",
    "//romm.example:8443/assets/cover.jpg",
    "https://evil.example/assets/cover.jpg",
    "https://romm.example:invalid/assets/cover.jpg",
    "https://romm.example:65536/assets/cover.jpg",
    "file:///assets/cover.jpg",
])
async def test_romm_unsafe_cover_origins_are_rejected_without_sending_a_token(cover_url):
    item_id = _seed("romm")

    def unexpected_request(request):
        pytest.fail("An unsafe cover target must never receive a request/token")

    assert romm_sync._cover_target("https://romm.example", cover_url) is None
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        assert await romm_sync._ingest_cover(
            client, "https://romm.example", "private-token", item_id, cover_url,
        ) is False
    assert not (config.COVERS_DIR / f"{item_id}.jpg").exists()


@pytest.mark.parametrize("server,cover_url,expected", [
    ("https://romm.example", "/assets/cover.jpg", "https://romm.example/assets/cover.jpg"),
    ("https://romm.example/root", "assets/cover.jpg", "https://romm.example/root/assets/cover.jpg"),
    ("https://ROMM.example:443", "https://romm.example/assets/cover.jpg", "https://romm.example/assets/cover.jpg"),
    ("https://romm.example", "https://ROMM.example:443/cover.jpg", "https://ROMM.example:443/cover.jpg"),
    ("http://romm:8080", "/cover.jpg", "http://romm:8080/cover.jpg"),
    ("http://192.168.1.2", "http://192.168.1.2:80/cover.jpg", "http://192.168.1.2:80/cover.jpg"),
    ("http://[::1]:8080", "/cover.jpg", "http://[::1]:8080/cover.jpg"),
    ("http://[::1]:8080", "http://[0:0:0:0:0:0:0:1]:8080/cover.jpg", "http://[0:0:0:0:0:0:0:1]:8080/cover.jpg"),
    ("https://bücher.example", "https://xn--bcher-kva.example/cover.jpg", "https://xn--bcher-kva.example/cover.jpg"),
])
def test_romm_relative_same_origin_and_lan_cover_targets_are_preserved(server, cover_url, expected):
    assert romm_sync._cover_target(server, cover_url) == expected


@pytest.mark.parametrize("server,cover_url", [
    ("https://user:password@romm.example", "/cover.jpg"),
    ("https://@romm.example", "/cover.jpg"),
    ("http://romm.example", "https://romm.example/cover.jpg"),
    ("http://romm.example:8080", "http://romm.example/cover.jpg"),
    ("https://romm.example", "https://romm.example:0/cover.jpg"),
    ("https://faß.example", "https://fass.example/cover.jpg"),
])
def test_romm_origin_matching_never_weakens_port_scheme_or_idna_boundaries(server, cover_url):
    assert romm_sync._cover_target(server, cover_url) is None


@pytest.mark.asyncio
async def test_cover_requests_never_follow_redirects_with_sync_credentials(provider):
    item_id = _seed(provider)
    requests = []

    def download(request):
        requests.append(request)
        assert str(request.url) == _cover_url(provider)
        return httpx.Response(302, headers={"Location": "http://elsewhere.example/cover.jpg"})

    # Even a caller with automatic redirects enabled cannot widen this helper's
    # authenticated request boundary (especially Komga's custom X-API-Key).
    async with httpx.AsyncClient(transport=httpx.MockTransport(download), follow_redirects=True) as client:
        assert await _ingest(provider, client, item_id) is False
    assert len(requests) == 1
    assert not (config.COVERS_DIR / f"{item_id}.jpg").exists()
