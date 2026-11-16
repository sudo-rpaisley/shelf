"""Real offline cassette forms, server-generated reviews and catalogue saves.

No route.fulfill(), forged HTML/JWT, key-file reads or successful test endpoint.
The child ASGI app replaces only the outbound provider transport.
"""

import json
import os
import sqlite3
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    APP_DIR, _free_port, _OutputCapture, _wait_for_server,
    assert_page_clean, attach_page_guard,
)

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def live_server(tmp_path_factory, upc_stub):
    """Override the shared dict fixture locally, with the real fixture ASGI app.

    Mirror _boot_server's fixed fresh-data/rate/background/UPC environment. The
    UPC URL is mandatory via the dependency, even though this journey has none.
    Existing setup_admin/browser fixtures therefore work with this server too.
    """
    from app.config import SECRET_ENV_VARS

    root = tmp_path_factory.mktemp("cassette-server")
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
    assert calls, "The browser must reach real fixture-backed provider requests"
    assert not any(call.get("unexpected") for call in calls), calls
    assert not any(call["authorization_present"] for call in calls), calls


@pytest.fixture
def cassette_page(live_server, browser, setup_admin):
    context = browser.new_context(service_workers="block")
    context.add_init_script("localStorage.setItem('shelf-welcome-dismissed', '1');localStorage.setItem('shelf-shortcuts-seen', '1');")
    unexpected = []

    def local_only(route):
        if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost"}:
            route.continue_()
        else:
            unexpected.append(route.request.url)
            route.abort()

    context.route("**/*", local_only)
    page = attach_page_guard(context.new_page())
    try:
        page.goto(live_server["url"] + "/login")
        page.locator('[name="username"]').fill(setup_admin["username"])
        page.locator('[name="password"]').fill(setup_admin["password"])
        with page.expect_navigation():
            page.locator('button[type="submit"]').click()
        yield page
        assert_page_clean(page)
        assert not unexpected, f"Unexpected browser destinations: {unexpected!r}"
    finally:
        context.close()


def _artifact(page, tmp_path, label):
    from tests.e2e.test_responsive import _COLLAPSE, _MEASURE, _settle

    _settle(page)
    geometry = page.evaluate(_MEASURE)
    assert geometry["scrollWidth"] <= geometry["clientWidth"] + 1, geometry
    assert not page.evaluate(_COLLAPSE)
    page.screenshot(path=str(tmp_path / f"{label}.png"), full_page=True)
    assert_page_clean(page)


def _search(page, server, *, title="Hits Out of Hell", artist="Meat Loaf"):
    page.goto(server["url"] + "/music")
    form = page.locator('form[action="/music"][method="get"]')
    expect(form).to_have_count(1)
    form.locator('[name="q"]').fill(title)
    form.locator('[name="artist"]').fill(artist)
    form.locator('[name="barcode"]').fill("")
    form.locator('[name="format"]').select_option("cassette")
    with page.expect_navigation():
        form.locator('button[type="submit"]').click()
    query = parse_qs(urlsplit(page.url).query, keep_blank_values=True)
    assert query["q"] == [title] and query["artist"] == [artist] and query["format"] == ["cassette"]
    assert query.get("barcode", [""]) == [""]


def _rows(server, query, parameters=()):
    from contextlib import closing

    with closing(sqlite3.connect(server["data_dir"] / "shelf.db")) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, parameters)]


def _assert_no_search_writes(server, before):
    assert _rows(server, "SELECT * FROM items ORDER BY id") == before


def _assert_selected_track_order(panel, release_id):
    from tests.e2e.cassette_asgi import verified_track_facts

    expected = verified_track_facts(release_id)
    tracks = panel.locator("ol > li")
    expect(tracks).to_have_count(len(expected))
    listed = tracks.all_text_contents()
    assert all(number in text and title in text
               for (number, title, duration, duration_ms), text in zip(expected, listed, strict=True))
    if release_id == 18670711:
        assert "Dead Ringer For Love" in listed[2] and "Midnight At The Lost And Found" in listed[4]
        assert "All Revved Up With No Place To Go" in listed[5]
        assert not any("Modern Girl" in text for text in listed)
    elif release_id == 3935124:
        assert "Midnight At The Lost And Found" in listed[2] and "Dead Ringer For Love" in listed[4]
        assert "Modern Girl" in listed[5]
        assert not any("All Revved Up With No Place To Go" in text for text in listed)


@pytest.mark.parametrize("width,release_id,country,year", [(320, 18670711, "Canada", 1995), (1280, 3935124, "UK", 1984)])
def test_blank_barcode_search_review_chosen_pressing_save_reload(cassette_page, live_server, tmp_path, width, release_id, country, year):
    page = cassette_page
    page.set_viewport_size({"width": width, "height": 900})
    before = _rows(live_server, "SELECT * FROM items ORDER BY id")
    seen = []
    page.on("request", lambda request: seen.append((urlsplit(request.url).path, request.post_data))
            if request.method == "POST" else None)
    _search(page, live_server)
    candidates = page.locator('form[action="/api/music/preview"]').filter(
        has=page.locator('input[name="provider"][value="discogs"]'))
    expect(candidates).to_have_count(3)
    for other in (3935124, 8297774, 18670711):
        expect(candidates.filter(has=page.locator(f'input[name="release_id"][value="{other}"]'))).to_have_count(1)
    expect(page.get_by_text("Data provided by Discogs", exact=False).first).to_be_visible()
    _assert_no_search_writes(live_server, before)
    _artifact(page, tmp_path, "ambiguous-pressing-search")
    chosen = candidates.filter(has=page.locator(f'input[name="release_id"][value="{release_id}"]'))
    expect(chosen.locator('[name="format"]')).to_have_value("cassette")
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/music/preview" and response.ok):
        chosen.locator('button[type="submit"]').click()
    review = page.locator('form[action="/api/music/apply-match"]')
    expect(review).to_be_visible()
    expect(review.locator('input[name="ticket"]')).not_to_have_value("")
    expect(review.locator('select[name="media_type"]')).to_have_value("cassette")
    expect(review.locator('[name="image"][value="-1"]')).to_be_checked()
    preview = page.locator("article").filter(has=review)
    expect(preview).to_contain_text(country)
    expect(preview).to_contain_text(str(year))
    expect(preview.locator(f'a[href="https://www.discogs.com/release/{release_id}"]')).to_be_visible()
    _assert_selected_track_order(preview, release_id)
    _assert_no_search_writes(live_server, before)
    _artifact(page, tmp_path, "exact-pressing-review")
    publisher_choice = review.locator('[name="fields"][value="publisher"]')
    expect(publisher_choice).to_have_count(1)
    expect(review.locator("label").filter(has=publisher_choice)).to_contain_text("Epic")
    for choice in review.locator('[name="fields"]').all():
        choice.uncheck()
    expect(review.locator('[name="fields"]:checked')).to_have_count(0)
    expect(publisher_choice).not_to_be_checked()
    for field in ("title", "authors", "publish_year"):
        review.locator(f'[name="fields"][value="{field}"]').check()
    expect(publisher_choice).not_to_be_checked()
    expect(review.locator('[name="fields"]:checked')).to_have_count(3)
    with page.expect_navigation():
        review.locator('button[type="submit"]').click()
    assert urlsplit(page.url).path.startswith("/music/item/")
    item_id = int(urlsplit(page.url).path.rsplit("/", 1)[1])
    expect(page.get_by_role("heading", level=1)).to_have_text("Hits Out Of Hell")
    items = _rows(live_server, "SELECT * FROM items ORDER BY id")
    assert len(items) == len(before) + 1
    item = next(row for row in items if row["id"] == item_id)
    assert item["authors"] == "Meat Loaf" and item["publish_year"] == year and item["media_type"] == "cassette"
    assert item["source"] == "discogs" and item["location_id"] is None and item["isbn"] is None and item["upc"] is None
    assert item["publisher"] is None, "Unchecked publisher must remain NULL after creation"
    assert not _rows(live_server, "SELECT * FROM item_copies WHERE item_id = ?", (item_id,))
    release = _rows(live_server, "SELECT * FROM music_releases WHERE item_id = ?", (item_id,))[0]
    assert release["metadata_source"] == "discogs" and release["musicbrainz_release_id"] is None and release["musicbrainz_release_group_id"] is None
    assert _rows(live_server, "SELECT value FROM music_identifiers WHERE item_id = ? AND identifier_type = 'discogs_release_id'", (item_id,)) == [{"value": str(release_id)}]
    assert not _rows(live_server, "SELECT * FROM music_media WHERE item_id = ?", (item_id,))
    page.reload()
    expect(page.get_by_role("heading", level=1)).to_have_text("Hits Out Of Hell")
    assert _rows(live_server, "SELECT publisher FROM items WHERE id = ?", (item_id,)) == [{"publisher": None}]
    fresh = page.locator(f'[hx-get="/api/music/items/{item_id}/discogs?public_details=1"]')
    expect(fresh).to_be_visible()
    with page.expect_response(lambda response: f"/api/music/items/{item_id}/discogs?public_details=1" in response.url and response.ok):
        fresh.click()
    expect(page.locator("#discogs-panel")).to_contain_text(country)
    _assert_selected_track_order(page.locator("#discogs-panel"), release_id)
    _artifact(page, tmp_path, "saved-and-fresh-detail")
    preview_request = next(body for path, body in seen if path == "/api/music/preview")
    preview_controls = parse_qs(preview_request)
    assert preview_controls["provider"] == ["discogs"] and preview_controls["release_id"] == [str(release_id)]
    assert preview_controls["format"] == ["cassette"]
    apply_controls = parse_qs(next(body for path, body in seen if path == "/api/music/apply-match"))
    assert apply_controls["ticket"] and apply_controls["media_type"] == ["cassette"]
    assert apply_controls["image"] == ["-1"]
    assert set(apply_controls["fields"]) == {"title", "authors", "publish_year"}
    assert "publisher" not in apply_controls["fields"]
    audit = [json.loads(line) for line in live_server["audit"].read_text().splitlines()]
    assert any(call["url"] == f"https://api.discogs.com/releases/{release_id}" for call in audit)
    assert sum(call["url"] == f"https://api.discogs.com/releases/{release_id}" for call in audit) >= 2
    assert all("barcode" not in call["params"] for call in audit if call["url"].endswith("/database/search"))


@pytest.mark.parametrize("width", [320, 1280])
def test_unrecognized_cassette_search_uses_real_manual_fallback_without_fake_mbids(cassette_page, live_server, tmp_path, width):
    from tests.e2e.cassette_asgi import load_catalogue

    page = cassette_page
    page.set_viewport_size({"width": width, "height": 900})
    query = load_catalogue()["synthetic"]["manual_query"]
    before = _rows(live_server, "SELECT * FROM items ORDER BY id")
    _search(page, live_server, title=query["title"], artist=query["artist"])
    expect(page.locator('form[action="/api/music/preview"]').filter(
        has=page.locator('input[name="provider"][value="discogs"]'))).to_have_count(0)
    manual = page.locator('a[href*="add=manual"]')
    expect(manual).to_have_count(1)
    expect(manual).to_be_visible()
    _assert_no_search_writes(live_server, before)
    _artifact(page, tmp_path, "no-identified-pressing")
    with page.expect_navigation():
        manual.click()
    assert urlsplit(page.url).path == "/scan"
    panel = page.locator('[data-manual-host="panel"]')
    expect(panel).to_be_visible()
    form = panel.locator('form[hx-post="/api/items/manual"]')
    form.locator('[name="media_type"]').select_option("cassette")
    form.locator('[name="title"]').fill(query["title"])
    form.locator('[name="authors"]').fill(query["artist"])
    form.locator('[name="isbn"]').fill("")
    form.locator('[name="location_id"]').select_option("")
    _artifact(page, tmp_path, "native-manual-form")
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/items/manual" and response.ok):
        form.locator('button[type="submit"]').click()
    expect(page.locator("#scan-results")).to_contain_text(query["title"])
    after = _rows(live_server, "SELECT * FROM items ORDER BY id")
    assert len(after) == len(before) + 1
    row = after[-1]
    assert row["title"] == query["title"] and row["authors"] == query["artist"]
    assert row["media_type"] == "cassette" and row["source"] == "manual" and row["isbn"] is None and row["upc"] is None
    assert not _rows(live_server, "SELECT * FROM item_copies WHERE item_id = ?", (row["id"],))
    assert not _rows(live_server, "SELECT * FROM music_identifiers WHERE item_id = ?", (row["id"],))
    assert not _rows(live_server, "SELECT * FROM music_releases WHERE item_id = ? AND (musicbrainz_release_id IS NOT NULL OR musicbrainz_release_group_id IS NOT NULL)", (row["id"],))
    page.goto(live_server["url"] + f'/music/item/{row["id"]}')
    expect(page.get_by_role("heading", level=1)).to_have_text(query["title"])
    page.reload()
    expect(page.get_by_role("heading", level=1)).to_have_text(query["title"])
    _artifact(page, tmp_path, "manual-save-reloaded")
