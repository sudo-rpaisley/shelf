"""Future real-form music-cover journeys; authored, not executed here.

The metadata allowlist and image HTTP/DNS boundaries are synthetic. The actual
image reader/size/magic checks, unique staging, SQLite worker and publication run
unchanged. This is NOT native DNS/TLS/socket or live-provider qualification.
"""

from contextlib import closing
from copy import deepcopy
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest
from playwright.sync_api import expect

from tests.e2e.add_media_asgi import _discogs_params, _mb_query
from tests.e2e.cassette_asgi import local_png
from tests.e2e.conftest import APP_DIR, _free_port, _OutputCapture, _run_setup_wizard, _wait_for_server, assert_page_clean
from tests.e2e.test_add_media import add_page_factory, _calls, _rows, _snapshot  # noqa: F401
from tests.e2e.test_music_combined_search import TITLE, _search


pytestmark = pytest.mark.e2e
GROUP = "22222222-2222-4222-8222-222222222222"
DISCOGS_IMAGE = "https://i.discogs.com/synthetic-auto-front.jpg"
GROUP_IMAGE = f"https://coverartarchive.org/release-group/{GROUP}/front.jpg"


def create_app():
    """A future disposable app factory; no replacement HTML or test endpoints."""
    import httpx
    from app.services import manual_cover
    from tests.e2e.test_music_combined_search import create_app as original_factory

    app = original_factory()
    fixture = app.state.add_media_catalogue
    for identity in range(910001, 910005):
        fixture.discogs_details[str(identity)]["images"] = [
            {"type": "primary", "uri": DISCOGS_IMAGE, "uri150": DISCOGS_IMAGE}]
    mb = deepcopy(fixture.mb_details["11111111-1111-4111-8111-111111111102"])
    mb["release-group"] = {"id": GROUP, "title": TITLE, "primary-type": "Album"}
    query = _mb_query(title=TITLE, artist="Fixture Ensemble", kind="cd").removesuffix(" AND format:CD")
    fixture.mb_searches[query] = {"releases": [mb]}
    params = _discogs_params(title=TITLE, artist="Fixture Ensemble", kind="cd")
    params.pop("format")
    fixture.discogs_searches[tuple(sorted(params.items()))] = {"results": []}
    original_match = fixture._match

    def metadata(method, url, params, headers, kwargs):
        if (method == "GET" and url == f"https://coverartarchive.org/release-group/{GROUP}"
                and params == {} and "authorization" not in headers):
            return {"json": {"images": [{"front": True, "types": ["Front"], "image": GROUP_IMAGE}]}}
        return original_match(method, url, params, headers, kwargs)
    fixture._match = metadata
    audit = Path(os.environ["SHELF_MUSIC_AUTO_IMAGE_CALLS"])

    def record(value):
        with audit.open("a") as output:
            output.write(json.dumps(value) + "\n")

    async def addresses(host, port):
        allowed = host in {"coverartarchive.org", "i.discogs.com"} and port == 443
        record({"kind": "synthetic_dns", "host": host, "unexpected": not allowed})
        assert allowed
        return [ipaddress.ip_address("93.184.216.34")]

    async def image(request):
        allowed = (request.method == "GET" and str(request.url) in {DISCOGS_IMAGE, GROUP_IMAGE}
                   and "authorization" not in request.headers and "cookie" not in request.headers)
        record({"kind": "synthetic_image_http", "url": str(request.url), "unexpected": not allowed})
        assert allowed
        content = local_png() + b"\0" * 128
        return httpx.Response(200, content=content, headers={"content-type": "image/png", "content-length": str(len(content))}, request=request)

    def transport(address):
        assert address == "93.184.216.34"
        return httpx.MockTransport(image)
    manual_cover._resolve_addresses = addresses
    manual_cover._PinnedIPTransport = transport
    return app


@pytest.fixture
def add_server(request, tmp_path, browser):
    """No process/server/browser resources have been started for this source."""
    from app.config import SECRET_ENV_VARS

    root = tmp_path / "music-auto-cover-server"
    data_dir = root / "data"
    (data_dir / "covers").mkdir(parents=True)
    audit, images = root / "provider-calls.jsonl", root / "image-calls.jsonl"
    automatic = getattr(request, "param", "automatic") == "automatic"
    excluded = {*SECRET_ENV_VARS.values(), "FORWARDED_ALLOW_IPS", "DISCOGS_TOKEN", "SHELF_ADD_MEDIA_PREFIX",
                "SHELF_UPC_LOOKUP_URL", "SHELF_ISSN_PORTAL_URL", "SHELF_DISABLE_COVER_ENRICH"}
    env = {key: value for key, value in os.environ.items() if key not in excluded}
    env.update(DATA_DIR=str(data_dir), SHELF_I18N="on", SHELF_DISABLE_RATE_LIMIT="1",
               SHELF_DISABLE_COVER_ENRICH="" if automatic else "1", SHELF_DISABLE_PRICE_ALERTS="1",
               SHELF_DISABLE_RELEASE_RADAR="1", SHELF_DISABLE_SOURCE_PROGRESS="1",
               SHELF_ADD_MEDIA_SCENARIO="normal", SHELF_ADD_MEDIA_CALLS=str(audit), SHELF_MUSIC_AUTO_IMAGE_CALLS=str(images))
    port = _free_port()
    command = [sys.executable, "-m", "uvicorn", "tests.e2e.test_music_auto_covers:create_app", "--factory",
               "--host", "127.0.0.1", "--port", str(port)]
    process = subprocess.Popen(command, cwd=str(APP_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = _OutputCapture(process.stdout)
    origin = f"http://127.0.0.1:{port}"
    server = {"url": origin, "origin": origin, "prefix": "", "data_dir": data_dir, "audit": audit,
              "image_audit": images, "root": root, "port": port}
    try:
        _wait_for_server(origin + "/health", output=output)
        server["credentials"] = _run_setup_wizard(browser, origin)
        yield server
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        (root / "server-output.log").write_text(output.tail())
        assert not [call for call in _calls(server) if call.get("unexpected")]
        assert not [call for call in _image_calls(server) if call.get("unexpected")]


def _image_calls(server):
    path = server["image_audit"]
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _wait_cover(server, item_id):
    # Future bounded, read-only observation of the real worker's committed state;
    # no success endpoint, DOM fulfilment or image/download-function substitution.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = _rows(server, "SELECT cover_path FROM items WHERE id = ?", (item_id,))[0]
        if row["cover_path"]:
            return row["cover_path"]
        time.sleep(0.05)
    raise AssertionError("The synthetic worker did not publish a cover within its observation budget")


@pytest.mark.parametrize("javascript", [False, True])
@pytest.mark.parametrize("kind", ["vinyl", "cd", "cassette", "digital_music"])
def test_new_music_saved_without_review_artwork_receives_a_background_cover(add_server, add_page_factory, javascript, kind):
    page = add_page_factory(javascript=javascript, width=375)
    _search(page, add_server, kind, provider="discogs")
    with page.expect_navigation():
        page.get_by_test_id("add-media-preview").get_by_role("button", name="Preview this result", exact=True).click()
    expect(page.get_by_test_id("add-media-review")).to_contain_text("looked up in the background after saving")
    assert _image_calls(add_server) == []
    with page.expect_navigation():
        page.get_by_test_id("add-media-save").get_by_role("button", name="Save to collection", exact=True).click()
    expect(page.get_by_test_id("add-media-receipt")).to_contain_text("Added to your collection.")
    item = _rows(add_server, "SELECT id, media_type FROM items WHERE title = ?", (TITLE,))[0]
    path = _wait_cover(add_server, item["id"])
    assert item["media_type"] == kind and (add_server["data_dir"] / path).exists()
    assert _rows(add_server, "SELECT musicbrainz_release_id FROM music_releases WHERE item_id = ?", (item["id"],)) == [{"musicbrainz_release_id": None}]
    assert _rows(add_server, "SELECT value FROM music_identifiers WHERE item_id = ? AND identifier_type = 'discogs_release_id'", (item["id"],))
    assert [call["url"] for call in _image_calls(add_server) if call["kind"] == "synthetic_image_http"] == [DISCOGS_IMAGE]
    page.goto(add_server["url"] + f'/item/{item["id"]}')
    expect(page.locator(f'img[src="/{path}"]').first).to_be_visible()
    assert_page_clean(page)


@pytest.mark.parametrize("add_server", ["backfill"], indirect=True)
def test_settings_retry_fills_old_manual_music_but_keeps_chosen_removed_and_dismissed_covers(add_server, add_page_factory):
    with closing(sqlite3.connect(add_server["data_dir"] / "shelf.db")) as db:
        ids = []
        for protection in ("missing", "chosen", "removed", "dismissed"):
            cursor = db.execute("INSERT INTO items(title, authors, media_type, source, created_at, cover_path, cover_review_dismissed) "
                                "VALUES (?, 'Fixture Ensemble', 'cd', 'manual', '2001-01-01', ?, ?)",
                                (TITLE if protection == "missing" else TITLE + " " + protection,
                                 "covers/kept.jpg" if protection == "chosen" else None, int(protection == "dismissed")))
            ids.append(cursor.lastrowid)
            if protection == "removed":
                db.execute("INSERT INTO cover_sync_blocks(item_id, blocked) VALUES (?, 1)", (cursor.lastrowid,))
        db.commit()
    (add_server["data_dir"] / "covers" / "kept.jpg").write_bytes(local_png() + b"\0" * 128)
    before = _snapshot(add_server)
    page = add_page_factory(javascript=True)
    page.goto(add_server["url"] + "/settings")
    page.get_by_test_id("tab-data").click()
    page.get_by_role("button", name="Retry Missing Covers", exact=True).click()
    path = _wait_cover(add_server, ids[0])
    expect(page.locator('[x-text="retryResult.success"]')).to_have_text("1")
    assert (add_server["data_dir"] / path).exists()
    after = _snapshot(add_server)
    for rows in (before["items"], after["items"]):
        for row in rows:
            for key in ("cover_path", "cover_edition_isbn", "cover_edition_language", "updated_at"):
                row.pop(key, None)
    assert after == before
    assert _rows(add_server, "SELECT cover_path FROM items WHERE id = ?", (ids[1],)) == [{"cover_path": "covers/kept.jpg"}]
    assert all(_rows(add_server, "SELECT cover_path FROM items WHERE id = ?", (item,))[0]["cover_path"] is None for item in ids[2:])
    assert _rows(add_server, "SELECT * FROM music_releases") == []
    assert [call["url"] for call in _image_calls(add_server) if call["kind"] == "synthetic_image_http"] == [GROUP_IMAGE]
    assert_page_clean(page)
