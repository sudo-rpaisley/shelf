"""Future JS/no-JS combined-search journeys; authored, not executed here.

Only the closed fixture transport is extended with synthetic provider responses.
Real native forms, authentication, CSRF, parsers, signing and SQLite stay in use.
Separate fresh server/browser/resource authorization is required before execution.
"""

from copy import deepcopy
import os
import subprocess
import sys

import pytest
from playwright.sync_api import expect

from tests.e2e.add_media_asgi import _discogs_params, _mb_query, _search_row
from tests.e2e.conftest import APP_DIR, _free_port, _OutputCapture, _run_setup_wizard, _wait_for_server, assert_page_clean
from tests.e2e.test_add_media import add_page_factory, _calls, _snapshot  # noqa: F401


pytestmark = pytest.mark.e2e
TITLE = "Combined Catalogue Probe"


def create_app():
    """Disposable uvicorn factory; no replacement HTML or feature/test endpoints."""
    from tests.e2e.add_media_asgi import create_app as original_factory

    app = original_factory()
    fixture = app.state.add_media_catalogue
    for index, kind in enumerate(("cassette", "cd", "vinyl", "digital_music"), 1):
        mb = deepcopy(fixture.mb_details[f"11111111-1111-4111-8111-{111111111100 + index:012d}"])
        mb["title"] = TITLE
        dg = deepcopy(fixture.discogs_details[str(910000 + index)])
        dg["title"] = TITLE
        fixture.mb_details[mb["id"]] = mb
        fixture.discogs_details[str(dg["id"])] = dg
        for artist in ("", "Fixture Ensemble"):
            fixture.mb_searches[_mb_query(title=TITLE, artist=artist, kind=kind)] = {"releases": [mb]}
            params = _discogs_params(title=TITLE, artist=artist, kind=kind)
            fixture.discogs_searches[tuple(sorted(params.items()))] = {"results": [_search_row(dg)]}
    return app


@pytest.fixture
def add_server(request, tmp_path, browser):
    """Fresh synthetic instance; this fixture has NOT been launched here."""
    from app.config import SECRET_ENV_VARS

    root = tmp_path / "combined-music-server"
    data_dir = root / "data"
    (data_dir / "covers").mkdir(parents=True)
    audit = root / "provider-calls.jsonl"
    scenario = getattr(request, "param", "normal")
    excluded = {*SECRET_ENV_VARS.values(), "FORWARDED_ALLOW_IPS", "DISCOGS_TOKEN", "SHELF_ADD_MEDIA_PREFIX",
                "SHELF_UPC_LOOKUP_URL", "SHELF_ISSN_PORTAL_URL"}
    env = {key: value for key, value in os.environ.items() if key not in excluded}
    env.update(DATA_DIR=str(data_dir), SHELF_I18N="on", SHELF_DISABLE_RATE_LIMIT="1",
               SHELF_DISABLE_COVER_ENRICH="1", SHELF_DISABLE_PRICE_ALERTS="1", SHELF_DISABLE_RELEASE_RADAR="1",
               SHELF_DISABLE_SOURCE_PROGRESS="1", SHELF_ADD_MEDIA_SCENARIO=scenario, SHELF_ADD_MEDIA_CALLS=str(audit))
    port = _free_port()
    command = [sys.executable, "-m", "uvicorn", "tests.e2e.test_music_combined_search:create_app", "--factory",
               "--host", "127.0.0.1", "--port", str(port)]
    process = subprocess.Popen(command, cwd=str(APP_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = _OutputCapture(process.stdout)
    origin = f"http://127.0.0.1:{port}"
    server = {"url": origin, "origin": origin, "prefix": "", "data_dir": data_dir, "audit": audit, "root": root, "port": port}
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


def _search(page, server, kind, *, provider="auto"):
    page.goto(server["url"] + "/add")
    page.locator("#add-media-type").select_option(kind)
    page.locator("#add-media-method").select_option("title")
    page.locator("#add-media-query").fill(TITLE)
    if provider != "auto":
        page.get_by_test_id("add-search-advanced").locator("summary").click()
        page.locator("#add-media-provider").select_option(provider)
    with page.expect_navigation(wait_until="domcontentloaded") as navigation:
        page.get_by_test_id("add-media-search").get_by_role("button", name="Search or continue", exact=True).click()
    assert navigation.value.status == 200


@pytest.mark.parametrize("javascript", [False, True])
@pytest.mark.parametrize("kind", ["cassette", "cd", "vinyl", "digital_music"])
def test_automatic_queries_both_and_keeps_both_native_preview_actions_without_writes(add_server, add_page_factory, javascript, kind):
    page = add_page_factory(javascript=javascript, width=375)
    before = _snapshot(add_server)
    _search(page, add_server, kind)
    status = page.get_by_test_id("music-search-status")
    expect(status.locator('[data-music-provider="musicbrainz"]')).to_have_attribute("data-search-outcome", "found")
    expect(status.locator('[data-music-provider="discogs"]')).to_have_attribute("data-search-outcome", "found")
    actions = page.get_by_test_id("add-media-preview")
    expect(actions).to_have_count(2)
    expect(actions.nth(0)).to_contain_text("MusicBrainz")
    expect(actions.nth(1)).to_contain_text("Discogs")
    calls = _calls(add_server)
    assert [call["url"] for call in calls] == ["https://musicbrainz.org/ws/2/release/", "https://api.discogs.com/database/search"]
    assert not any(call["authorization_present"] for call in calls)
    assert _snapshot(add_server) == before
    assert_page_clean(page)


@pytest.mark.parametrize("add_server,failed_provider,outcome", [
    ("mb_transport", "musicbrainz", "transport_failed"),
    ("discogs_rate_limited", "discogs", "rate_limited"),
], indirect=["add_server"])
@pytest.mark.parametrize("javascript", [False, True])
def test_partial_provider_failure_is_visible_beside_the_other_catalogues_result(add_server, add_page_factory, javascript, failed_provider, outcome):
    page = add_page_factory(javascript=javascript)
    before = _snapshot(add_server)
    _search(page, add_server, "cd")
    status = page.get_by_test_id("music-search-status")
    expect(status.locator(f'[data-music-provider="{failed_provider}"]')).to_have_attribute("data-search-outcome", outcome)
    expect(status.locator(f'[data-music-provider="{failed_provider}"]')).to_contain_text("not a no-match result")
    expect(page.get_by_test_id("add-media-preview")).to_have_count(1)
    assert len(_calls(add_server)) == 2
    assert _snapshot(add_server) == before
    assert_page_clean(page)


@pytest.mark.parametrize("javascript", [False, True])
@pytest.mark.parametrize("provider", ["musicbrainz", "discogs"])
def test_explicit_provider_choice_still_searches_only_that_catalogue(add_server, add_page_factory, javascript, provider):
    page = add_page_factory(javascript=javascript)
    before = _snapshot(add_server)
    _search(page, add_server, "cd", provider=provider)
    expect(page.get_by_test_id("add-media-preview")).to_have_count(1)
    expect(page.get_by_test_id("music-search-status")).to_have_count(0)
    assert [call["url"] for call in _calls(add_server)] == [
        "https://musicbrainz.org/ws/2/release/" if provider == "musicbrainz" else "https://api.discogs.com/database/search",
    ]
    assert _snapshot(add_server) == before
    assert_page_clean(page)
