"""Real Scan title-search/native manual regressions, with strict offline I/O.

No success HTML, login/review token or endpoint is fabricated. A held request in
the mode-switch case is continued to the real ASGI app without changing its body.
"""

import json
import os
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    APP_DIR, _free_port, _OutputCapture, _run_setup_wizard, _wait_for_server, assert_page_clean,
)
from tests.e2e.test_cassette_no_barcode import cassette_page, _rows

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def live_server(tmp_path_factory, upc_stub):
    """Same strict fixture ASGI transport, but no mandatory provider search.

    This journey is specifically a zero-provider-call title handoff. The old
    live_server fixture legitimately requires provider calls for its own search
    journeys, so use a local builder rather than overriding that fixture's checks.
    """
    from app.config import SECRET_ENV_VARS

    root = tmp_path_factory.mktemp("cassette-review-server")
    data_dir = root / "data"
    data_dir.mkdir()
    (data_dir / "covers").mkdir()
    audit = root / "provider-calls.jsonl"
    port = _free_port()
    excluded = {*SECRET_ENV_VARS.values(), "FORWARDED_ALLOW_IPS"}
    env = {key: value for key, value in os.environ.items() if key not in excluded}
    env.update({
        "DATA_DIR": str(data_dir), "SHELF_DISABLE_RATE_LIMIT": "1",
        "SHELF_DISABLE_COVER_ENRICH": "1", "SHELF_DISABLE_PRICE_ALERTS": "1",
        "SHELF_DISABLE_RELEASE_RADAR": "1", "SHELF_DISABLE_SOURCE_PROGRESS": "1",
        "SHELF_UPC_LOOKUP_URL": f'{upc_stub["url"]}/lookup',
        "SHELF_ISSN_PORTAL_URL": f'{upc_stub["url"]}/issn/{{issn}}',
        "SHELF_CASSETTE_CALLS": str(audit),
    })
    command = [sys.executable, "-m", "uvicorn", "tests.e2e.cassette_asgi:create_app", "--factory",
               "--host", "127.0.0.1", "--port", str(port)]
    process = subprocess.Popen(command, cwd=str(APP_DIR), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = _OutputCapture(process.stdout)
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(f"{base_url}/health", output=output)
        yield {"url": base_url, "data_dir": data_dir, "port": port, "audit": audit}
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        (root / "server-output.log").write_text(output.tail())
        calls = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
        assert not calls, f"Identifier-free Scan title handoff must not contact any provider: {calls!r}"


@pytest.fixture(scope="session")
def setup_admin(live_server, browser):
    # Keep setup tied to THIS module's server. The shared session fixture can
    # otherwise reuse another browser module's cached credentials without ever
    # configuring this fresh server when both files run in the composed gate.
    return _run_setup_wizard(browser, live_server["url"])


def _music_enabled(page, server, enabled):
    page.goto(server["url"] + "/settings")
    page.locator('[data-testid="tab-features"]').click()
    row = page.locator('[data-testid="feature-row-music"]')
    expect(row).to_be_visible()
    target = "on" if enabled else "off"
    if not row.locator(f'[data-feature-state="{target}"]').count():
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/settings/features/music") as saved:
            row.locator('[data-testid="feature-toggle-music"]').click()
        assert saved.value.status in (200, 303)
    expect(page.locator(f'[data-testid="feature-row-music"] [data-feature-state="{target}"]')).to_be_visible()


def _scan(page, server, mode):
    page.goto(server["url"] + "/scan")
    page.get_by_role("button", name="Wishlist" if mode == "wishlist" else "Add", exact=True).click()
    expect(page.locator("#scan-mode")).to_have_value(mode)
    page.locator("#media-type").select_option("cassette")
    expect(page.locator("#title-search-input")).to_be_visible()


def _title_search(page, title):
    search = page.locator("#title-search-input")
    search.fill(title)
    # The real input listens for keyup, not the synthetic input event of fill().
    search.press("End")


def _manual_form(page, title, music_enabled):
    fragment = page.locator('#title-search-results [data-testid="music-title-search"]')
    expect(fragment).to_be_visible()
    expect(fragment.locator('a[href^="/music?"]')).to_have_count(int(music_enabled))
    fragment.locator('[data-testid="music-manual-add"] > summary').click()
    form = fragment.locator('form[action="/api/items/manual"]')
    expect(form).to_be_visible()
    expect(form).to_have_attribute("method", "post")
    expect(form.locator('[name="title"]')).to_have_value(title)
    expect(form.locator('[name="media_type"]')).to_have_value("cassette")
    expect(form.locator('[name="_csrf"]')).not_to_have_value("")
    form.locator('[name="authors"]').fill("Synthetic Scan review artist")
    return form


def _native_save(page, server, form, title, expected_mode, before, tmp_path):
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/items/manual"
                              and response.request.method == "POST") as submitted:
        form.locator('button[type="submit"]').click()
    response = submitted.value
    assert response.status == 200, response.text()
    assert response.request.is_navigation_request(), "Exercise the genuine native POST, not a synthetic fetch"
    sent = parse_qs(response.request.post_data, keep_blank_values=True)
    assert sent["title"] == [title] and sent["authors"] == ["Synthetic Scan review artist"]
    assert sent["media_type"] == ["cassette"] and sent["_csrf"]
    expect(page.locator("body")).to_contain_text(title)
    rows = _rows(server, "SELECT * FROM items ORDER BY id")
    assert len(rows) == len(before) + 1
    assert [row for row in rows if row["id"] in {old["id"] for old in before}] == before
    item = next(row for row in rows if row["title"] == title)
    assert item["media_type"] == "cassette" and item["source"] == "manual"
    assert item["authors"] == "Synthetic Scan review artist"
    assert item["isbn"] is None and item["upc"] is None and item["cover_path"] is None
    assert item["location_id"] is None
    memberships = _rows(server,
        "SELECT l.slug FROM list_items li JOIN lists l ON l.id = li.list_id WHERE li.item_id = ?",
        (item["id"],))
    # Save rendered/physical evidence even when the ownership oracle is RED.
    page.screenshot(path=str(tmp_path / "native-manual-result.png"), full_page=True)
    (tmp_path / "native-manual-observation.json").write_text(json.dumps({
        "sent": sent, "expected_mode": expected_mode, "item": item, "memberships": memberships,
    }, indent=2) + "\n")
    assert_page_clean(page)
    assert item["owned"] == int(expected_mode == "add"), "Wishlist intent became owned Add"
    assert memberships == ([{"slug": "wishlist"}] if expected_mode == "wishlist" else [])
    assert sent.get("mode") == [expected_mode], "Native POST must carry the current canonical Scan intent"
    for table in ("item_copies", "music_identifiers", "music_releases", "music_media"):
        assert not _rows(server, f"SELECT * FROM {table} WHERE item_id = ?", (item["id"],)), table
    assert not _rows(server, "SELECT t.* FROM music_tracks t JOIN music_media m ON m.id = t.medium_id WHERE m.item_id = ?",
                     (item["id"],))
    assert _rows(server, "SELECT mode, result FROM scan_log WHERE item_id = ?", (item["id"],)) == [
        {"mode": expected_mode, "result": "wishlisted" if expected_mode == "wishlist" else "added"},
    ]


@pytest.mark.parametrize("music_enabled", [False, True], ids=["music-off", "music-on"])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_scan_title_search_native_manual_save_uses_selected_intent(
    cassette_page, live_server, tmp_path, music_enabled, mode,
):
    page = cassette_page
    _music_enabled(page, live_server, music_enabled)
    _scan(page, live_server, mode)
    title = f"Synthetic review {mode} {uuid4().hex}"
    before = _rows(live_server, "SELECT * FROM items ORDER BY id")
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/title-search" and response.ok) as searched:
        _title_search(page, title)
    request_query = parse_qs(urlsplit(searched.value.url).query)
    assert request_query["q"] == [title] and request_query["media_type"] == ["cassette"]
    form = _manual_form(page, title, music_enabled)
    assert _rows(live_server, "SELECT * FROM items ORDER BY id") == before
    _native_save(page, live_server, form, title, mode, before, tmp_path)
    assert request_query.get("mode") == [mode], "HTMX title search must include #scan-mode"


@pytest.mark.parametrize("music_enabled", [False, True], ids=["music-off", "music-on"])
@pytest.mark.parametrize("initial,current", [("add", "wishlist"), ("wishlist", "add")], ids=["add-to-wishlist", "wishlist-to-add"])
def test_scan_mode_switch_during_real_title_request_uses_current_intent(
    cassette_page, live_server, tmp_path, music_enabled, initial, current,
):
    page = cassette_page
    _music_enabled(page, live_server, music_enabled)
    _scan(page, live_server, initial)
    title = f"Synthetic switched review {initial} to {current} {uuid4().hex}"
    before = _rows(live_server, "SELECT * FROM items ORDER BY id")
    held = []
    pattern = live_server["url"] + "/api/title-search?*"

    def hold_local_request(route):
        assert urlsplit(route.request.url).netloc == urlsplit(live_server["url"]).netloc
        held.append(route)

    page.route(pattern, hold_local_request)
    try:
        with page.expect_request(lambda request: urlsplit(request.url).path == "/api/title-search") as searching:
            _title_search(page, title)
        # setMode clears already-visible results. Holding the actual earlier
        # HTMX request makes its old-mode result arrive AFTER that real UI
        # switch, deterministically exercising the live canonical binding.
        page.get_by_role("button", name="Wishlist" if current == "wishlist" else "Add", exact=True).click()
        expect(page.locator("#scan-mode")).to_have_value(current)
        assert len(held) == 1
        original_query = parse_qs(urlsplit(searching.value.url).query)
        assert original_query["q"] == [title]
        with page.expect_response(lambda response: response.url == searching.value.url and response.ok):
            held.pop().continue_()
        form = _manual_form(page, title, music_enabled)
        expect(page.locator("#scan-mode")).to_have_value(current)
        assert _rows(live_server, "SELECT * FROM items ORDER BY id") == before
        _native_save(page, live_server, form, title, current, before, tmp_path)
        assert original_query.get("mode") == [initial]
    finally:
        for route in held:
            route.abort()
        page.unroute(pattern, hold_local_request)
