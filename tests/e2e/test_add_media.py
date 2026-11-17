"""Independent native Add Media acceptance, against real HTTP and fresh SQLite.

No fulfilled HTML, minted login/review token, signing-key read, test success route,
provider-function stub, skip, or xfail. Source/UI/unit-author WIP is not an oracle.
The base predates /add: a 404 on it is honest feature absence, not a bug reproduction
or transferable GREEN. Locale literals explicitly require the parent's later handoff.
"""

import base64
from contextlib import closing
import json
import os
import re
import sqlite3
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests.e2e.add_media_asgi import (
    DISCOGS_CREDENTIAL, FixtureCatalogue, IGDB_CLIENT, IGDB_SECRET, TMDB_CREDENTIAL,
)
from tests.e2e.conftest import (
    APP_DIR, _free_port, _OutputCapture, _run_setup_wizard, _wait_for_server,
    assert_page_clean, attach_page_guard,
)

pytestmark = pytest.mark.e2e

# Do not invent translations or derive expected UI wording from the same catalog
# under test. Parent must supply these literal oracles after its localization pass.
# These are hard assertions (NOT skips/xfails) until that finite handoff is complete.
PARENT_LOCALE_LITERALS = {
    "it": {
        "heading": "Aggiungi contenuti",
        "search": "Cerca o continua",
        "wishlist": "Aggiungi alla lista desideri",
        "artwork_later": "In questa verifica non viene caricata né selezionata alcuna immagine di copertina. Potrai aggiungerla in seguito.",
        "saved": "Aggiunto alla tua lista desideri.",
    },
    "zh_Hans": {
        "heading": "添加媒体",
        "search": "搜索或继续",
        "wishlist": "加入心愿单",
        "artwork_later": "此次确认未加载或选择任何封面图片。你可以稍后添加封面图片。",
        "saved": "已加入你的心愿单。",
    },
}
LOCALE_LITERAL_KEYS = {"heading", "search", "wishlist", "artwork_later", "saved"}

# Separate receipt-label ABI: parent alone supplies actual compiled foreign
# literals. Synthetic DOM-harness labels below are never locale acceptance.
PARENT_RECEIPT_LITERALS = {
    "it": {
        "item_reference": "Riferimento dell'elemento",
        "collection_state": "Stato memorizzato nella raccolta",
        "owned": "Posseduti",
        "wishlisted": "Nella lista desideri",
        "neither": "Né posseduto né nella lista desideri",
    },
    "zh_Hans": {
        "item_reference": "条目编号",
        "collection_state": "已保存的收藏状态",
        "owned": "已拥有",
        "wishlisted": "在心愿单中",
        "neither": "未拥有，也不在心愿单中",
    },
}
RECEIPT_LITERAL_KEYS = {"item_reference", "collection_state", "owned", "wishlisted", "neither"}
ENGLISH_RECEIPT_LITERALS = {"item_reference": "Item reference", "collection_state": "Stored collection state",
                           "owned": "Owned", "wishlisted": "Wishlisted", "neither": "Neither owned nor wishlisted"}

TYPE_LABELS = {"book": "Book", "audiobook": "Audiobook", "ebook": "eBook", "comic": "Comic",
               "manga": "Manga", "dvd": "DVD", "video_game": "Video Game", "cassette": "Cassette",
               "cd": "CD", "vinyl": "Vinyl", "digital_music": "Digital Music", "tv_series": "TV Series",
               "digital_video": "Digital Video", "magazine": "Magazine"}
SEARCH_KEYS = {"_csrf", "context", "mode", "media_type", "method", "provider", "q", "artist",
               "title_hint", "platform", "country"}
POST_KEYS = {"search": SEARCH_KEYS, "preview": {"_csrf", "context", "selection"},
             "save": {"_csrf", "context", "ticket", "location_id", "tags", "reading_status", "fields"},
             "manual": {"_csrf", "context", "title", "authors", "publisher", "publish_year", "platform",
                        "series_name", "language", "identifier_kind", "identifier", "location_id", "tags", "reading_status"}}
TABLES = ("items", "item_copies", "music_releases", "music_media", "music_tracks",
          "music_identifiers", "list_items", "item_tags", "scan_log")


def _rows(server, sql, parameters=()):
    # Every assertion opens a fresh connection, never the app's uncommitted one.
    with closing(sqlite3.connect(server["data_dir"] / "shelf.db")) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, parameters)]


def _snapshot(server):
    return {table: _rows(server, f'SELECT * FROM "{table}" ORDER BY rowid') for table in TABLES}


def _calls(server):
    audit = server["audit"]
    return [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []


def _path(server, path):
    return server["prefix"] + path


@pytest.fixture(scope="session")
def playwright_instance():
    """Same real engine; give Node a fresh, writable, short-lived temp directory.

    The prepared runner's /tmp/opencode is root-owned 0755 in this session. Do
    not chmod it, edit the shared runner, install a browser, or drop its guards.
    Its long pytest basetemp also exceeds Chromium's Unix-socket path bound.
    Only engine scratch uses /tmp here; logs, databases and screenshots stay in
    the runner's unique bounded tree. This scratch directory is always removed.
    """
    import tempfile

    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="shelf-add-browser-", dir="/tmp") as runtime:
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("TMPDIR", runtime)
            with sync_playwright() as instance:
                yield instance


@pytest.fixture
def add_server(request, tmp_path, upc_stub, browser):
    """One disposable real application/database per case; no production credentials."""
    from app.config import SECRET_ENV_VARS

    options = getattr(request, "param", {})
    if isinstance(options, str):
        options = {"scenario": options}
    scenario, prefix = options.get("scenario", "normal"), options.get("prefix", "")
    root = tmp_path / "add-media-server"
    data_dir = root / "data"
    (data_dir / "covers").mkdir(parents=True)
    audit = root / "provider-calls.jsonl"
    port = _free_port()
    excluded = {*SECRET_ENV_VARS.values(), "FORWARDED_ALLOW_IPS", "DISCOGS_TOKEN"}
    env = {key: value for key, value in os.environ.items() if key not in excluded}
    env.update({
        "DATA_DIR": str(data_dir), "SHELF_I18N": "on", "SHELF_DISABLE_RATE_LIMIT": "1",
        "SHELF_DISABLE_COVER_ENRICH": "1", "SHELF_DISABLE_PRICE_ALERTS": "1",
        "SHELF_DISABLE_RELEASE_RADAR": "1", "SHELF_DISABLE_SOURCE_PROGRESS": "1",
        "SHELF_UPC_LOOKUP_URL": f'{upc_stub["url"]}/lookup',
        "SHELF_ISSN_PORTAL_URL": f'{upc_stub["url"]}/issn/{{issn}}',
        "SHELF_ADD_MEDIA_CALLS": str(audit), "SHELF_ADD_MEDIA_SCENARIO": scenario,
        "SHELF_ADD_MEDIA_PREFIX": prefix,
    })
    if scenario != "no_credentials":
        env.update(TMDB_API_KEY=TMDB_CREDENTIAL, IGDB_CLIENT_ID=IGDB_CLIENT, IGDB_CLIENT_SECRET=IGDB_SECRET)
    command = [sys.executable, "-m", "uvicorn", "tests.e2e.add_media_asgi:create_app", "--factory",
               "--host", "127.0.0.1", "--port", str(port)]
    process = subprocess.Popen(command, cwd=str(APP_DIR), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = _OutputCapture(process.stdout)
    origin = f"http://127.0.0.1:{port}"
    server = {"url": origin + prefix, "origin": origin, "prefix": prefix, "data_dir": data_dir,
              "audit": audit, "port": port, "root": root}
    (root / "server-command.json").write_text(json.dumps({"command": command, "scenario": scenario,
                                                        "prefix": prefix}, indent=2) + "\n")
    try:
        # Root health/setup/login are existing native bootstrap surfaces. The
        # mounted case deliberately tests Add only, not legacy /shelf/health.
        _wait_for_server(server["origin"] + "/health", output=output)
        server["credentials"] = _run_setup_wizard(browser, server["origin"])
        with closing(sqlite3.connect(data_dir / "shelf.db")) as db:
            cursor = db.execute("INSERT INTO locations(name) VALUES (?)", ("Add Media Shelf",))
            server["location_id"] = cursor.lastrowid
            if scenario == "music_disabled":
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('feature.music','0')")
            if scenario == "discogs_rejected":
                # Known disposable legacy plaintext setting, not a key read or a
                # fake auth cookie. The real client must send it once, never retry
                # anonymously after rejection. No value is retained in the audit.
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('discogs_token',?)",
                           (DISCOGS_CREDENTIAL,))
            db.commit()
        yield server
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        (root / "server-output.log").write_text(output.tail())
        calls = _calls(server)
        assert not [call for call in calls if call.get("unexpected")], calls


@pytest.fixture
def add_page_factory(add_server, browser, tmp_path):
    """Real login, guarded pages, same-origin-only resources; JS off by default."""
    owned = []

    def create(*, width=1280, javascript=False, locale="en-US"):
        context = browser.new_context(viewport={"width": width, "height": 900},
                                      java_script_enabled=javascript, locale=locale, service_workers="block")
        if javascript:
            # Existing global welcome overlays only. No Add draft/intent is stored.
            context.add_init_script("localStorage.setItem('shelf-welcome-dismissed','1');"
                                    "localStorage.setItem('shelf-shortcuts-seen','1');")
        state = {"external": [], "console": [], "http": [], "failed": [], "posts": [],
                 "add_io": [], "requests": [], "expected_http": set(), "artifacts": 0,
                 "width": width, "page_index": len(owned)}

        def local_only(route):
            parts = urlsplit(route.request.url)
            if f"{parts.scheme}://{parts.netloc}" == add_server["origin"]:
                route.continue_()
            else:
                state["external"].append(route.request.url)
                route.abort()

        context.route("**/*", local_only)
        page = attach_page_guard(context.new_page())
        owned.append((context, page, state))
        page.on("console", lambda message: state["console"].append(
            {"type": message.type, "text": message.text, "location": message.location}))
        page.on("response", lambda response: state["http"].append((urlsplit(response.url).path, response.status))
                if response.status >= 400 else None)
        page.on("requestfailed", lambda request: state["failed"].append((request.url, request.failure)))

        def record(request):
            # All destinations, resource types and successful same-origin requests
            # count; do not retain non-Add POST bodies (including real login).
            state["requests"].append((request.method, request.url, request.resource_type))
            path = urlsplit(request.url).path
            if path == _path(add_server, "/add") or path.startswith(_path(add_server, "/add/")):
                state["add_io"].append((request.method, path, request.resource_type))
                if request.method == "POST":
                    state["posts"].append((path, parse_qs(request.post_data or "", keep_blank_values=True)))

        context.on("request", record)
        page.goto(add_server["origin"] + "/login")
        page.locator('[name="username"]').fill(add_server["credentials"]["username"])
        page.locator('[name="password"]').fill(add_server["credentials"]["password"])
        with page.expect_navigation():
            page.locator('button[type="submit"]').click()
        assert urlsplit(page.url).path == "/"
        page._add_media_state = state
        return page

    try:
        yield create
    finally:
        problems = []
        for index, (context, page, state) in enumerate(owned):
            try:
                # Retain the actual last page even if the FIRST /add was a 404.
                page.screenshot(path=str(tmp_path / f"last-{index}-{state['width']}.png"), full_page=True)
                assert_page_clean(page)
                assert not state["external"], state["external"]
                assert not state["failed"], state["failed"]
                assert not [entry for entry in state["http"] if entry not in state["expected_http"]], state["http"]
                for message in state["console"]:
                    text = message["text"]
                    assert not re.search(r"content.security.policy|violat.*policy|refused to (load|execute|apply)|\bCSP\b",
                                         text, re.I), message
                    if message["type"] == "error":
                        location = urlsplit(message["location"].get("url", "")).path
                        expected_resource_error = any(
                            location == path and f"status of {status}" in text
                            for path, status in state["expected_http"])
                        assert expected_resource_error, message
                assert all(kind == "document" for method, path, kind in state["add_io"]), state["add_io"]
            except Exception as error:
                problems.append(str(error))
            finally:
                context.close()
        assert not problems, "\n\n".join(problems)


def _artifact(page, tmp_path, stage):
    state = page._add_media_state
    state["artifacts"] += 1
    path = tmp_path / f"p{state['page_index']}-{state['artifacts']:02d}-{state['width']}-{stage}.png"
    page.screenshot(path=str(path), full_page=True)
    dimensions = page.evaluate("""() => ({viewport: document.documentElement.clientWidth,
        page: document.documentElement.scrollWidth,
        root: document.querySelector('[data-testid="add-media"]')?.scrollWidth || 0})""")
    assert dimensions["page"] <= dimensions["viewport"] + 1, dimensions
    assert dimensions["root"] <= dimensions["viewport"] + 1, dimensions
    assert_page_clean(page)


def _response_policy(response):
    assert "no-store" in response.headers.get("cache-control", ""), response.headers
    assert "set-cookie" not in response.headers, "Add must not refresh/issue auth or CSRF cookies"
    csp = response.headers.get("content-security-policy", "")
    directives = {}
    for directive in csp.split(";"):
        tokens = directive.split()
        if not tokens:
            continue
        name = tokens[0].lower()
        assert name not in directives, f"Duplicate CSP directive: {name}"
        directives[name] = tuple(tokens[1:])
    assert directives.get("script-src") == ("'self'",), csp
    assert "script-src-elem" not in directives and "script-src-attr" not in directives, csp


def _open(page, server, tmp_path, *, mode="add", media_type=""):
    before = _calls(server)
    suffix = f"?mode={mode}" + (f"&media_type={media_type}" if media_type else "")
    response = page.goto(server["url"] + "/add" + suffix)
    # Preserve absence-phase screenshots before the positive assertion.
    state = page._add_media_state
    state["artifacts"] += 1
    page.screenshot(path=str(tmp_path / f"p{state['page_index']}-{state['artifacts']:02d}-{state['width']}-initial.png"), full_page=True)
    assert response.status == 200, f"Expected actual /add; got {response.status} (base feature absence is NOT GREEN)"
    _response_policy(response)
    root = page.get_by_test_id("add-media")
    expect(root).to_be_visible()
    expect(root.locator('img,input[name="image"],input[name="owned"],input[name="item_id"],input[type="file"]')).to_have_count(0)
    assert _calls(server) == before, "GET must not query providers"
    _assert_forms(page, server)
    return root.get_by_test_id("add-media-search")


def _assert_forms(page, server):
    root = page.get_by_test_id("add-media")
    handlers = root.evaluate("""root => [root, ...root.querySelectorAll('*')].flatMap(el =>
        [...el.attributes].filter(a => /^on|^hx-/i.test(a.name)).map(a => a.name))""")
    assert not handlers, handlers
    expect(root.locator('img,[name="image"],[name="owned"],[name="item_id"],[name="upload"],'
                        '[name="shop_url"],input[type="file"]')).to_have_count(0)
    for form in root.locator("form").all():
        action = urlsplit(form.get_attribute("action") or "")
        assert f"{action.scheme}://{action.netloc}" == server["origin"], action
        assert action.path in {_path(server, "/add/" + stage) for stage in ("search", "preview", "save", "manual")}, action
        assert form.get_attribute("method").lower() == "post"
        expect(form.locator('input[name="_csrf"]')).to_have_count(1)
        expect(form.locator('input[name="_csrf"]')).not_to_have_value("")
        expect(form.locator('input[name="context"]')).to_have_count(1)
        expect(form.locator('input[name="context"]')).not_to_have_value("")


def _submit(page, server, form, stage, *, status=200):
    path = _path(server, "/add/" + stage)
    if status >= 400:
        page._add_media_state["expected_http"].add((path, status))
    with page.expect_response(lambda response: urlsplit(response.url).path == path
                              and response.request.method == "POST") as received:
        with page.expect_navigation():
            form.locator('button[type="submit"]').first.click()
    response = received.value
    assert response.status == status, f"{stage}: {response.status} != {status}"
    controls = parse_qs(response.request.post_data or "", keep_blank_values=True)
    assert {"_csrf", "context"} <= set(controls) <= POST_KEYS[stage], controls
    assert all(len(values) == 1 for key, values in controls.items() if key != "fields"), controls
    if "fields" in controls:
        assert len(controls["fields"]) <= 6 and len(set(controls["fields"])) == len(controls["fields"]), controls
    _response_policy(response)
    assert urlsplit(page.url).path in {_path(server, "/add"), path}, page.url
    assert urlsplit(page.url).netloc == urlsplit(server["origin"]).netloc
    _assert_forms(page, server)
    return response


def _search(page, server, tmp_path, *, mode="add", media_type="cassette", method="title", provider="auto",
            query="Hits Out of Hell", artist="Meat Loaf", title_hint="", country="", platform=""):
    form = _open(page, server, tmp_path, mode=mode, media_type=media_type)
    for name, value in (("mode", mode), ("media_type", media_type), ("method", method), ("provider", provider)):
        control = form.locator(f'[name="{name}"]')
        if control.input_value() != value:
            control.select_option(value)
    for name, value in (("q", query), ("artist", artist), ("title_hint", title_hint), ("country", country)):
        control = form.locator(f'[name="{name}"]')
        if value or control.is_visible():
            control.fill(value)
    platform_control = form.locator('[name="platform"]')
    if platform_control.input_value() != platform:
        platform_control.select_option(platform)
    before = _snapshot(server)
    _submit(page, server, form, "search")
    assert _snapshot(server) == before, "Search must not create catalogue/copy/list/provenance/log records"
    path, controls = page._add_media_state["posts"][-1]
    assert path == _path(server, "/add/search") and set(controls) <= SEARCH_KEYS
    for name, value in (("mode", mode), ("media_type", media_type), ("method", method), ("provider", provider), ("q", query)):
        assert controls[name] == [value], controls
    assert controls["context"] and controls["_csrf"]
    _artifact(page, tmp_path, "results")


def _unsigned_payload(token):
    # Observation only: no signature bypass, re-signing, or fabricated authority.
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _contains(value, expected):
    if isinstance(value, dict):
        return any(_contains(entry, expected) for entry in value.values())
    if isinstance(value, (tuple, list)):
        return any(_contains(entry, expected) for entry in value)
    return str(value) == str(expected)


def _candidate(page, identifier):
    matches = []
    for form in page.get_by_test_id("add-media-results").get_by_test_id("add-media-preview").all():
        token = form.locator('[name="selection"]').input_value()
        if _contains(_unsigned_payload(token), identifier):
            matches.append(form)
    assert len(matches) == 1, f"Expected one server-signed selection for {identifier}, got {len(matches)}"
    return matches[0]


def _preview(page, server, tmp_path, identifier):
    before = _snapshot(server)
    _submit(page, server, _candidate(page, identifier), "preview")
    review = page.get_by_test_id("add-media-review")
    expect(review).to_be_visible()
    save = review.get_by_test_id("add-media-save")
    expect(save.locator('[name="ticket"]')).not_to_have_value("")
    assert _snapshot(server) == before, "Preview must not mutate native catalogue state"
    expect(review.locator("img")).to_have_count(0)
    expect(save.locator('[name="image"],[name="owned"],[name="item_id"],input[type="file"]')).to_have_count(0)
    _artifact(page, tmp_path, "review")
    return review, save


def _details(save, server, *, status=""):
    save.locator('[name="location_id"]').fill(str(server["location_id"]))
    save.locator('[name="tags"]').fill("Browser; Fixture")
    if status:
        save.locator('[name="reading_status"]').select_option(status)


def _receipt(page, server, tmp_path, save, *, title, mode, media_type, status=None, stage="save", english=True):
    receipt_literals = _receipt_literal_values(page, english=english)
    source, creator = _literal_receipt_expectations(save, title=title, stage=stage)
    with page.expect_response(lambda response: response.request.method == "GET"
                              and urlsplit(response.url).path == _path(server, "/add")
                              and "receipt" in parse_qs(urlsplit(response.url).query)) as final_get:
        response = _submit(page, server, save, stage, status=303)
    assert final_get.value.status == 200
    _response_policy(final_get.value)
    location = urlsplit(response.headers["location"])
    assert location.path == _path(server, "/add") and set(parse_qs(location.query)) == {"receipt"}, location
    assert urlsplit(page.url).path == _path(server, "/add"), page.url
    receipt_token = parse_qs(urlsplit(page.url).query)["receipt"][0]
    payload = _unsigned_payload(receipt_token)
    facts = _named_receipt_facts(payload)
    committed = _rows(server, "SELECT * FROM items WHERE id=?", (facts["item_id"],))
    assert len(committed) == 1, facts
    item = committed[0]
    wished = bool(_rows(server, "SELECT 1 FROM list_items li JOIN lists l ON l.id=li.list_id "
                       "WHERE li.item_id=? AND l.slug='wishlist'", (item["id"],)))
    expected_status = status or ("wishlisted" if mode == "wishlist" else "added")
    _assert_receipt_named_facts(facts, item, wishlisted=wished, status=expected_status,
                                mode=mode, media_type=media_type, source=source, creator=creator, title=title)
    receipt = page.get_by_test_id("add-media-receipt")
    expect(receipt).to_be_visible()
    expect(receipt).to_contain_text(title)
    if english:
        if mode is None:
            expect(receipt).to_contain_text(re.compile(r"neither|not owned.*not.*wishlist", re.I))
        else:
            expect(receipt).to_contain_text(re.compile("wishlist" if mode == "wishlist" else r"\b(add|added|owned|collection)\b", re.I))
        expect(receipt).to_contain_text(TYPE_LABELS[media_type])
        expect(receipt).to_contain_text(re.compile(r"added|saved|wishlist|promot|duplicate|already|restor", re.I))
    _assert_rendered_receipt(_receipt_dom_evidence(receipt, server, literals=receipt_literals),
                             item, source=source, creator=creator, mode=mode, english=english)
    expect(page.get_by_test_id("add-media-error")).to_have_count(0)
    _artifact(page, tmp_path, "receipt")
    after = _snapshot(server)
    before_calls = _calls(server)
    reload_response = page.reload()
    assert reload_response.status == 200
    _response_policy(reload_response)
    expect(page.get_by_test_id("add-media-receipt")).to_contain_text(title)
    _assert_rendered_receipt(_receipt_dom_evidence(page.get_by_test_id("add-media-receipt"), server, literals=receipt_literals),
                             item, source=source, creator=creator, mode=mode, english=english)
    assert _snapshot(server) == after and _calls(server) == before_calls, "Receipt refresh is read-only, not POST replay"
    return receipt


def _saved_item(server, *, title, mode, media_type, source, location=False):
    items = _rows(server, "SELECT * FROM items ORDER BY id")
    assert len(items) == 1, items
    item = items[0]
    assert (item["title"], item["owned"], item["media_type"], item["source"]) == (
        title, int(mode == "add"), media_type, source)
    assert item["cover_path"] is None, "Stage 1 must not request/store new-host artwork"
    memberships = _rows(server, "SELECT l.slug FROM lists l JOIN list_items li ON li.list_id=l.id WHERE li.item_id=?",
                        (item["id"],))
    assert memberships == ([{"slug": "wishlist"}] if mode == "wishlist" else []), memberships
    copies = _rows(server, "SELECT item_id,copy_number,location_id,is_primary,copy_barcode FROM item_copies WHERE item_id=?",
                   (item["id"],))
    if location:
        assert item["location_id"] == server["location_id"]
        assert copies == [{"item_id": item["id"], "copy_number": 1, "location_id": server["location_id"],
                           "is_primary": 1, "copy_barcode": None}], copies
        tags = _rows(server, "SELECT t.name FROM tags t JOIN item_tags it ON it.tag_id=t.id WHERE it.item_id=? ORDER BY t.name",
                     (item["id"],))
        assert tags == [{"name": "Browser"}, {"name": "Fixture"}], tags
    else:
        assert item["location_id"] is None and not copies, (item, copies)
    return item


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_native_direct_discogs_catalogue_search_selective_review_save(add_page_factory, add_server, tmp_path, width, mode):
    page = add_page_factory(width=width)
    _search(page, add_server, tmp_path, mode=mode, provider="discogs", method="catalog_number", query="40-26156",
            title_hint="Hits Out of Hell")
    results = page.get_by_test_id("add-media-results")
    expect(results.get_by_test_id("add-media-preview")).to_have_count(3)
    for fact in ("Discogs", "UK", "Europe", "Canada", "1984", "1995", "Epic", "Cleveland International Records",
                 "EPC 40-26156", "BT 57615", "Cassette"):
        expect(results).to_contain_text(fact)
    for identifier in (3935124, 8297774, 18670711):
        _candidate(page, identifier)
    chosen = 18670711 if width == 320 else 3935124
    review, save = _preview(page, add_server, tmp_path, chosen)
    for fact in ("Hits Out Of Hell", "Meat Loaf", "Cassette", "Canada" if chosen == 18670711 else "UK",
                 "1995" if chosen == 18670711 else "1984", "BT 57615" if chosen == 18670711 else "EPC 40-26156"):
        expect(review).to_contain_text(fact)
    expect(review.locator(f'a[href="https://www.discogs.com/release/{chosen}"]')).to_be_visible()
    _track_facts(review, chosen)
    for choice in save.locator('[name="fields"]').all():
        choice.uncheck()
    for field in ("title", "authors", "publish_year"):
        save.locator(f'[name="fields"][value="{field}"]').check()
    expect(save.locator('[name="fields"][value="publisher"]')).not_to_be_checked()
    _details(save, add_server)
    _receipt(page, add_server, tmp_path, save, title="Hits Out Of Hell", mode=mode, media_type="cassette")
    item = _saved_item(add_server, title="Hits Out Of Hell", mode=mode, media_type="cassette", source="discogs", location=True)
    assert item["authors"] == "Meat Loaf" and item["publisher"] is None
    assert item["publish_year"] == (1995 if chosen == 18670711 else 1984)
    assert item["isbn"] is None and item["isbn10"] is None and item["upc"] is None
    release = _rows(add_server, "SELECT * FROM music_releases WHERE item_id=?", (item["id"],))
    assert len(release) == 1 and release[0]["metadata_source"] == "discogs"
    assert release[0]["musicbrainz_release_id"] is None and release[0]["musicbrainz_release_group_id"] is None
    assert _rows(add_server, "SELECT identifier_type,value FROM music_identifiers WHERE item_id=?", (item["id"],)) == [
        {"identifier_type": "discogs_release_id", "value": str(chosen)}]
    assert not _rows(add_server, "SELECT * FROM music_media") and not _rows(add_server, "SELECT * FROM music_tracks")
    calls = _calls(add_server)
    assert calls and all(call["url"].startswith("https://api.discogs.com/") for call in calls), calls
    search = [call for call in calls if call["url"].endswith("/database/search")]
    assert len(search) == 1 and search[0]["params"] == {"type": "release", "per_page": "20", "page": "1",
        "format": "Cassette", "catno": "40-26156", "artist": "Meat Loaf", "release_title": "Hits Out of Hell"}
    assert any(call["url"] == f"https://api.discogs.com/releases/{chosen}" for call in calls)
    assert all("barcode" not in call["params"] for call in calls)


def _track_facts(review, identifier):
    # Independent literal oracles, not read from served JSON or a normalizer.
    tracks = review.locator("ol > li").all_text_contents()
    assert len(tracks) == (12 if identifier == 8297774 else 10), tracks
    for number, title in (("A1", "Bat Out Of Hell"), ("A2", "Read 'Em And Weep"), ("B4", "Razor's Edge")):
        assert any(number in text and title in text for text in tracks), tracks
    if identifier == 18670711:
        assert "Dead Ringer For Love" in tracks[2] and "Midnight At The Lost And Found" in tracks[4]
        assert "All Revved Up With No Place To Go" in tracks[5]
        assert not any("Modern Girl" in text for text in tracks)
    else:
        assert "Midnight At The Lost And Found" in tracks[2] and "Dead Ringer For Love" in tracks[4]
        assert "Modern Girl" in tracks[5] and not any("All Revved Up With No Place To Go" in text for text in tracks)
    if identifier == 8297774:
        assert "B5-I" in tracks[9] and "Paradise" in tracks[9]
        assert "B5-II" in tracks[10] and "Let Me Sleep On It" in tracks[10]
        assert "B5-III" in tracks[11] and "Praying For The End Of Time" in tracks[11]
        assert not any("8:28" in text for text in tracks[9:]), "Index duration is not a child duration"


@pytest.mark.parametrize("width", [320, 1280])
def test_native_auto_remains_mb_first_and_competing_european_tracks(add_page_factory, add_server, tmp_path, width):
    page = add_page_factory(width=width)
    _search(page, add_server, tmp_path)
    calls = _calls(add_server)
    assert [call["url"] for call in calls] == ["https://musicbrainz.org/ws/2/release/", "https://api.discogs.com/database/search"]
    assert calls[0]["params"] == {"fmt": "json", "limit": "30",
        "query": 'release:"Hits Out of Hell" AND artist:"Meat Loaf" AND format:Cassette'}
    review, save = _preview(page, add_server, tmp_path, 8297774)
    expect(review).to_contain_text("Europe")
    expect(review).to_contain_text("Cleveland International Records")
    _track_facts(review, 8297774)
    assert not _rows(add_server, "SELECT * FROM items")


@pytest.mark.parametrize("provider", ["auto", "discogs"])
@pytest.mark.parametrize("width", [320, 1280])
def test_raw_short_catalogue_is_not_barcode_release_id_or_manual_title(add_page_factory, add_server, tmp_path, provider, width):
    page = add_page_factory(width=width)
    _search(page, add_server, tmp_path, provider=provider, method="catalog_number", query="4026156", artist="")
    expect(page.get_by_test_id("add-media-preview")).to_have_count(0)
    manual = page.get_by_test_id("add-media-manual")
    expect(manual).to_be_visible()
    expect(manual.locator('[name="title"]')).to_have_value("")
    expect(manual.locator('[name="identifier"]')).to_have_value("")
    calls = _calls(add_server)
    assert len(calls) == 1, calls
    if provider == "auto":
        assert calls[0]["url"] == "https://musicbrainz.org/ws/2/release/"
        assert calls[0]["params"]["query"] == 'catno:"4026156" AND format:Cassette'
    else:
        assert calls[0]["url"] == "https://api.discogs.com/database/search"
        assert calls[0]["params"] == {"type": "release", "per_page": "20", "page": "1", "format": "Cassette", "catno": "4026156"}
    assert not _rows(add_server, "SELECT * FROM items")


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
@pytest.mark.parametrize("media_type,index", [("cassette", 1), ("cd", 2), ("vinyl", 3), ("digital_music", 4)])
def test_native_explicit_mb_saves_real_canonical_tree_without_discogs(add_page_factory, add_server, tmp_path, width, mode, media_type, index):
    page = add_page_factory(width=width)
    _search(page, add_server, tmp_path, mode=mode, media_type=media_type, provider="musicbrainz",
            query="Paper Lantern Sessions", artist="Fixture Ensemble", country="IT")
    _assert_provider_filters(_calls(add_server), provider="musicbrainz", country="IT")
    identifier = f"11111111-1111-4111-8111-{111111111100 + index:012d}"
    review, save = _preview(page, add_server, tmp_path, identifier)
    for fact in ("Paper Lantern Sessions", "Fixture Ensemble", "Fixture Label", "IT", "2001", "Signal at Dawn", "Quiet Platform"):
        expect(review).to_contain_text(fact)
    for choice in save.locator('[name="fields"]').all():
        choice.check()
    _receipt(page, add_server, tmp_path, save, title="Paper Lantern Sessions", mode=mode, media_type=media_type)
    item = _saved_item(add_server, title="Paper Lantern Sessions", mode=mode, media_type=media_type, source="musicbrainz")
    assert item["authors"] == "Fixture Ensemble" and item["publisher"] == "Fixture Label" and item["upc"] == "0012345678905"
    release = _rows(add_server, "SELECT musicbrainz_release_id,musicbrainz_release_group_id,metadata_source FROM music_releases")
    assert release == [{"musicbrainz_release_id": identifier,
                        "musicbrainz_release_group_id": "22222222-2222-4222-8222-222222222201", "metadata_source": "musicbrainz"}]
    assert _rows(add_server, "SELECT position,number,title,duration_ms,musicbrainz_recording_id FROM music_tracks ORDER BY position") == [
        {"position": 1, "number": "A1", "title": "Signal at Dawn", "duration_ms": 61000,
         "musicbrainz_recording_id": "33333333-3333-4333-8333-333333333301"},
        {"position": 2, "number": "B1", "title": "Quiet Platform", "duration_ms": 92000,
         "musicbrainz_recording_id": "33333333-3333-4333-8333-333333333302"}]
    assert _rows(add_server, "SELECT position,format,track_count FROM music_media") == [
        {"position": 1, "format": {"cassette": "Cassette", "cd": "CD", "vinyl": "Vinyl", "digital_music": "Digital Media"}[media_type], "track_count": 2}]
    assert not _rows(add_server, "SELECT * FROM music_identifiers WHERE identifier_type='discogs_release_id'")
    assert all(call["url"].startswith("https://musicbrainz.org/ws/2/release/") for call in _calls(add_server))


@pytest.mark.parametrize("media_type,index", [("cd", 2), ("vinyl", 3), ("digital_music", 4)])
def test_native_direct_discogs_other_selected_formats(add_page_factory, add_server, tmp_path, media_type, index):
    page = add_page_factory()
    _search(page, add_server, tmp_path, media_type=media_type, provider="discogs",
            query="Synthetic Format Probe", artist="Fixture Ensemble")
    review, save = _preview(page, add_server, tmp_path, 910000 + index)
    expect(review).to_contain_text({"cd": "CD", "vinyl": "Vinyl", "digital_music": "File"}[media_type])
    for choice in save.locator('[name="fields"]').all():
        choice.check()
    _receipt(page, add_server, tmp_path, save, title="Synthetic Format Probe", mode="add", media_type=media_type)
    item = _saved_item(add_server, title="Synthetic Format Probe", mode="add", media_type=media_type, source="discogs")
    assert _rows(add_server, "SELECT value FROM music_identifiers WHERE item_id=? AND identifier_type='discogs_release_id'", (item["id"],)) == [{"value": str(910000 + index)}]
    assert not _rows(add_server, "SELECT * FROM music_media") and not _rows(add_server, "SELECT * FROM music_tracks")
    assert all(call["url"].startswith("https://api.discogs.com/") for call in _calls(add_server))


def test_native_mixed_mb_media_preserves_explicit_cd_choice_and_both_facts(add_page_factory, add_server, tmp_path):
    page = add_page_factory(width=320)
    _search(page, add_server, tmp_path, media_type="cd", provider="musicbrainz", query="Mixed Medium Probe", artist="Fixture Ensemble")
    review, save = _preview(page, add_server, tmp_path, "11111111-1111-4111-8111-111111111105")
    expect(review).to_contain_text("CD")
    expect(review).to_contain_text("Vinyl")
    for choice in save.locator('[name="fields"]').all():
        choice.check()
    _receipt(page, add_server, tmp_path, save, title="Mixed Medium Probe", mode="add", media_type="cd")
    _saved_item(add_server, title="Mixed Medium Probe", mode="add", media_type="cd", source="musicbrainz")
    assert _rows(add_server, "SELECT position,format FROM music_media ORDER BY position") == [
        {"position": 1, "format": "CD"}, {"position": 2, "format": "Vinyl"}]


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
@pytest.mark.parametrize("kind,title,identifier,source", [
    ("book", "The Synthetic Atlas", "9780306406157", "openlibrary"),
    ("dvd", "Synthetic Cinema: Second Cut", 930002, "tmdb"),
    ("video_game", "Synthetic Switch Quest", 920001, "igdb"),
])
def test_native_nonmusic_search_preview_and_save_choices(add_page_factory, add_server, tmp_path, width, mode, kind, title, identifier, source):
    page = add_page_factory(width=width)
    query = "Synthetic Cinema" if kind == "dvd" else title
    _search(page, add_server, tmp_path, mode=mode, media_type=kind, query=query, artist="", platform="switch" if kind == "video_game" else "")
    if kind == "video_game":
        _assert_provider_filters(_calls(add_server), provider="igdb", platform="switch")
    review, save = _preview(page, add_server, tmp_path, identifier)
    expect(review).to_contain_text(title)
    expect(save.locator('[name="fields"]')).to_have_count(0)
    _details(save, add_server, status="want_to_read")
    _receipt(page, add_server, tmp_path, save, title=title, mode=mode, media_type=kind)
    item = _saved_item(add_server, title=title, mode=mode, media_type=kind, source=source, location=True)
    assert item["reading_status"] == "want_to_read"
    assert not _rows(add_server, "SELECT * FROM music_releases") and not _rows(add_server, "SELECT * FROM music_identifiers")
    assert not _rows(add_server, "SELECT * FROM music_media") and not _rows(add_server, "SELECT * FROM music_tracks")
    if kind == "book":
        assert (item["isbn"], item["isbn10"], item["authors"], item["publisher"], item["publish_year"], item["page_count"], item["language"]) == (
            "9780306406157", "0306406152", "Ada Fixture", "Fixture Press", 2005, 123, "en")
        assert item["hardcover_book_id"] is None and item["hardcover_edition_id"] is None
    elif kind == "video_game":
        assert item["platform"] == "switch" and item["publisher"] == "Fixture Games" and item["publish_year"] == 2010
        assert item["authors"] is None, "Native game insert does not persist the developer display field"
        assert item["isbn"] is None and item["upc"] is None
        assert any("where id = 920001;" in (call["content"] or "") for call in _calls(add_server))
    else:
        assert item["publish_year"] == 2011 and "Second synthetic film" in item["description"]
        assert item["authors"] is None and item["isbn"] is None and item["upc"] is None
        assert len(_calls(add_server)) == 1, "TMDb preview uses its signed selected snapshot, not a first-title relookup"
    assert item.get("igdb_id") is None and item.get("tmdb_id") is None, "Never invent durable external bindings"


@pytest.mark.parametrize("kind", ["audiobook", "ebook", "comic", "manga"])
def test_native_other_bibliographic_types_keep_isbn_identity(add_page_factory, add_server, tmp_path, kind):
    page = add_page_factory()
    _search(page, add_server, tmp_path, media_type=kind, query="The Synthetic Atlas", artist="")
    review, save = _preview(page, add_server, tmp_path, "9780306406157")
    _receipt(page, add_server, tmp_path, save, title="The Synthetic Atlas", mode="add", media_type=kind)
    item = _saved_item(add_server, title="The Synthetic Atlas", mode="add", media_type=kind, source="openlibrary")
    assert item["isbn"] == "9780306406157" and item["isbn10"] == "0306406152"


@pytest.mark.parametrize("width", [320, 1280])
def test_native_isbn10_original_grammar_resolves_to_canonical_pair(add_page_factory, add_server, tmp_path, width):
    page = add_page_factory(width=width)
    _search(page, add_server, tmp_path, media_type="book", method="isbn", query="0-306-40615-2", artist="")
    review, save = _preview(page, add_server, tmp_path, "9780306406157")
    _receipt(page, add_server, tmp_path, save, title="The Synthetic Atlas", mode="add", media_type="book")
    item = _saved_item(add_server, title="The Synthetic Atlas", mode="add", media_type="book", source="openlibrary")
    assert item["isbn"] == "9780306406157" and item["isbn10"] == "0306406152"
    assert all(call["url"].startswith("https://openlibrary.org/") for call in _calls(add_server))


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_native_retail_barcode_is_one_facts_lookup_then_explicit_manual(add_page_factory, add_server, tmp_path, width, mode):
    page = add_page_factory(width=width)
    _search(page, add_server, tmp_path, media_type="dvd", method="barcode", query="012345678905", artist="", mode=mode)
    calls = _calls(add_server)
    assert len(calls) == 1 and calls[0]["params"] == {"upc": "012345678905"}, calls
    expect(page.get_by_test_id("add-media-preview")).to_have_count(0)
    manual = page.get_by_test_id("add-media-manual")
    expect(manual).to_be_visible()
    manual.locator('[name="title"]').fill("My Retail Disc")
    manual.locator('[name="identifier_kind"]').select_option("barcode")
    manual.locator('[name="identifier"]').fill("012345678905")
    _receipt(page, add_server, tmp_path, manual, title="My Retail Disc", mode=mode, media_type="dvd", stage="manual")
    item = _saved_item(add_server, title="My Retail Disc", mode=mode, media_type="dvd", source="manual")
    assert item["upc"] == "0012345678905" and item["isbn"] is None and item["isbn10"] is None
    assert _calls(add_server) == calls, "No hidden TMDb/IGDB ladder or artwork after UPC facts"


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("kind", ["tv_series", "digital_video", "magazine"])
def test_native_unsupported_lookup_offers_meaningful_same_host_manual(add_page_factory, add_server, tmp_path, width, kind):
    page = add_page_factory(width=width)
    _search(page, add_server, tmp_path, media_type=kind, query="An unsupported lookup", artist="", mode="wishlist")
    expect(page.get_by_test_id("add-media")).to_contain_text(re.compile(r"manual|not supported|unavailable", re.I))
    assert not _calls(add_server)
    manual = page.get_by_test_id("add-media-manual")
    expect(manual).to_be_visible()
    manual.locator('[name="title"]').fill("A deliberately manual holding")
    manual.locator('[name="authors"]').fill("Named by the user")
    manual.locator('[name="identifier_kind"]').select_option("none")
    manual.locator('[name="identifier"]').fill("")
    _receipt(page, add_server, tmp_path, manual, title="A deliberately manual holding", mode="wishlist", media_type=kind, stage="manual")
    item = _saved_item(add_server, title="A deliberately manual holding", mode="wishlist", media_type=kind, source="manual")
    assert item["authors"] == "Named by the user" and item["isbn"] is None and item["upc"] is None
    assert not _calls(add_server)


@pytest.mark.parametrize("width", [320, 1280])
def test_native_unidentified_cassette_manual_has_no_invented_pressing(add_page_factory, add_server, tmp_path, width):
    page = add_page_factory(width=width)
    _search(page, add_server, tmp_path, provider="discogs", query="No identified pressing", artist="Fixture Ensemble")
    manual = page.get_by_test_id("add-media-manual")
    expect(manual).to_be_visible()
    manual.locator('[name="title"]').fill("The tape I actually hold")
    manual.locator('[name="authors"]').fill("Fixture Ensemble")
    manual.locator('[name="identifier_kind"]').select_option("none")
    manual.locator('[name="identifier"]').fill("")
    calls = _calls(add_server)
    _receipt(page, add_server, tmp_path, manual, title="The tape I actually hold", mode="add", media_type="cassette", stage="manual")
    item = _saved_item(add_server, title="The tape I actually hold", mode="add", media_type="cassette", source="manual")
    assert item["isbn"] is None and item["upc"] is None
    assert not _rows(add_server, "SELECT * FROM music_releases") and not _rows(add_server, "SELECT * FROM music_identifiers")
    assert not _rows(add_server, "SELECT * FROM music_media") and not _rows(add_server, "SELECT * FROM music_tracks")
    assert _calls(add_server) == calls


@pytest.mark.parametrize("add_server", ["discogs_rejected", "discogs_rate_limited", "discogs_transport", "discogs_unreadable", "mb_transport"], indirect=True)
def test_native_provider_failure_truthfully_retains_manual_without_fallback(add_page_factory, add_server, tmp_path):
    page = add_page_factory(width=320)
    scenario = json.loads((add_server["root"] / "server-command.json").read_text())["scenario"]
    is_mb = scenario == "mb_transport"
    _search(page, add_server, tmp_path, provider="auto" if is_mb else "discogs",
            query="Hits Out of Hell" if is_mb else "Provider Failure Probe", artist="Meat Loaf" if is_mb else "Fixture Ensemble")
    _provider_outcome(page, {"discogs_rejected": "rejected", "discogs_rate_limited": "rate_limited",
                             "discogs_transport": "transport_failed", "discogs_unreadable": "transport_failed",
                             "mb_transport": "transport_failed"}[scenario])
    expect(page.get_by_test_id("add-media-preview")).to_have_count(0)
    expect(page.get_by_test_id("add-media-manual")).to_be_visible()
    assert "SYNTHETIC-DO-NOT-REFLECT" not in page.content() and DISCOGS_CREDENTIAL not in page.content()
    calls = _calls(add_server)
    assert len(calls) == 1, "Failure is not genuine emptiness and must not trigger another provider/anonymous retry"
    assert calls[0]["url"].startswith("https://musicbrainz.org/" if is_mb else "https://api.discogs.com/")
    if scenario == "discogs_rejected":
        assert calls[0]["authorization_present"]
    manual = page.get_by_test_id("add-media-manual")
    manual.locator('[name="title"]').fill("Saved manually after provider refusal")
    manual.locator('[name="identifier_kind"]').select_option("none")
    manual.locator('[name="identifier"]').fill("")
    _receipt(page, add_server, tmp_path, manual, title="Saved manually after provider refusal", mode="add", media_type="cassette", stage="manual")
    _saved_item(add_server, title="Saved manually after provider refusal", mode="add", media_type="cassette", source="manual")
    assert _calls(add_server) == calls


@pytest.mark.parametrize("add_server", ["no_credentials"], indirect=True)
@pytest.mark.parametrize("kind,query", [("dvd", "Synthetic Cinema"), ("video_game", "Synthetic Switch Quest")])
def test_native_missing_credentials_is_not_no_results_and_manual_works(add_page_factory, add_server, tmp_path, kind, query):
    page = add_page_factory()
    _search(page, add_server, tmp_path, media_type=kind, query=query, artist="")
    _provider_outcome(page, "no_credential")
    manual = page.get_by_test_id("add-media-manual")
    expect(manual).to_be_visible()
    assert not _calls(add_server)
    manual.locator('[name="title"]').fill("A credential-free manual item")
    manual.locator('[name="identifier_kind"]').select_option("none")
    manual.locator('[name="identifier"]').fill("")
    _receipt(page, add_server, tmp_path, manual, title="A credential-free manual item", mode="add", media_type=kind, stage="manual")
    _saved_item(add_server, title="A credential-free manual item", mode="add", media_type=kind, source="manual")


@pytest.mark.parametrize("add_server,expected_status", [("format_conflict", 409), ("detail_mismatch", 200), ("detail_unreadable", 200)], indirect=["add_server"])
def test_native_provider_detail_refusal_never_claims_a_save(add_page_factory, add_server, tmp_path, expected_status):
    page = add_page_factory(width=320)
    _search(page, add_server, tmp_path, provider="discogs", query="Synthetic Format Probe", artist="Fixture Ensemble")
    before = _snapshot(add_server)
    _submit(page, add_server, _candidate(page, 910001), "preview", status=expected_status)
    if expected_status == 409:
        expect(page.get_by_test_id("add-media-error")).to_be_visible()
    else:
        _provider_outcome(page, "transport_failed")
    expect(page.get_by_test_id("add-media-save")).to_have_count(0)
    expect(page.get_by_test_id("add-media-receipt")).to_have_count(0)
    if expected_status != 409:
        expect(page.get_by_test_id("add-media-manual")).to_be_visible()
    assert _snapshot(add_server) == before
    assert "SYNTHETIC-DO-NOT-REFLECT" not in page.content()
    _artifact(page, tmp_path, "refusal")
    if expected_status == 409:
        _fresh_manual_recovery(page, add_server, tmp_path, before, _calls(add_server), media_type="cassette")


@pytest.mark.parametrize("method,query", [("barcode", "4026156"), ("isbn", "isbn:9780306406157"),
    ("isbn", "9780306406158"), ("isbn", "９７８０３０６４０６１５７"), ("isbn", "0-306-40615-3")])
def test_native_invalid_identifier_refusal_is_focused_and_zero_provider(add_page_factory, add_server, tmp_path, method, query):
    page = add_page_factory(width=320)
    form = _open(page, add_server, tmp_path, media_type="book")
    form.locator('[name="method"]').select_option(method)
    form.locator('[name="q"]').fill(query)
    # Negative server-boundary probe only; browser constraint validation must not
    # make a malformed native POST test pass without dispatching one.
    form.evaluate("form => { form.noValidate = true; }")
    before = _snapshot(add_server)
    _submit(page, add_server, form, "search", status=422)
    error = page.get_by_test_id("add-media-error")
    expect(error).to_be_visible()
    focused_error = error.evaluate("el => el === document.activeElement || el.contains(document.activeElement)")
    assert focused_error or page.locator('[name="q"]').evaluate("el => el === document.activeElement"), "Error summary or invalid input must receive focus"
    assert not _calls(add_server) and _snapshot(add_server) == before
    _artifact(page, tmp_path, "invalid-identifier")


def test_native_nojs_review_displays_and_uses_immutable_captured_wishlist(add_page_factory, add_server, tmp_path):
    page = add_page_factory(width=320)
    _search(page, add_server, tmp_path, mode="wishlist", media_type="cd", provider="musicbrainz",
            query="Paper Lantern Sessions", artist="Fixture Ensemble")
    review, save = _preview(page, add_server, tmp_path, "11111111-1111-4111-8111-111111111102")
    expect(review).to_contain_text(re.compile("wishlist", re.I))
    expect(review).to_contain_text("CD")
    search = page.get_by_test_id("add-media-search")
    search.locator('[name="mode"]').select_option("add")
    search.locator('[name="q"]').fill("Unsubmitted mutable text")
    expect(review).to_contain_text(re.compile("wishlist", re.I))
    expect(review).not_to_contain_text("Unsubmitted mutable text")
    _receipt(page, add_server, tmp_path, save, title="Paper Lantern Sessions", mode="wishlist", media_type="cd")
    _saved_item(add_server, title="Paper Lantern Sessions", mode="wishlist", media_type="cd", source="musicbrainz")


@pytest.mark.parametrize("stage", ["results", "review", "manual"])
@pytest.mark.parametrize("field,value", [("q", "Changed"), ("media_type", "cd"), ("method", "catalog_number"),
    ("provider", "musicbrainz"), ("mode", "wishlist"), ("artist", "Other artist"), ("title_hint", "Other title"),
    ("country", "GB"), ("platform", "switch")])
def test_optional_js_permanently_invalidates_all_old_actions_without_io(add_page_factory, add_server, tmp_path, stage, field, value):
    page = add_page_factory(width=320, javascript=True)
    _search(page, add_server, tmp_path, provider="discogs",
            query="No identified pressing" if stage == "manual" else "Hits Out of Hell",
            artist="Fixture Ensemble" if stage == "manual" else "Meat Loaf")
    if stage == "review":
        _preview(page, add_server, tmp_path, 3935124)
    target = page.get_by_test_id({"results": "add-media-results", "review": "add-media-review", "manual": "add-media-manual"}[stage])
    expect(target).to_be_visible()
    search = page.get_by_test_id("add-media-search")
    control = search.locator(f'[name="{field}"]')
    original = control.input_value()
    calls = _calls(add_server)
    posts = list(page._add_media_state["posts"])
    complete_io = _io_snapshot(page, add_server)
    captured = target.inner_text()
    enhancement = page.locator('script[src*="add-media.js"]').count() > 0
    # Some refinements are deliberately hidden when inapplicable. Programmatic
    # form-control events still must invalidate; no dispatched provider request.
    control.evaluate("""(element, value) => { element.value = value;
        element.dispatchEvent(new Event('input', {bubbles: true}));
        element.dispatchEvent(new Event('change', {bubbles: true})); }""", value)

    def assert_no_old_actions():
        if not enhancement:
            # No enhancement is a permitted implementation. Native immutable
            # captured-intent actions must not borrow this other form's values.
            expect(target).to_have_text(captured)
            return
        # Removal, hiding, or disabled controls are all acceptable; a live old
        # submit is not. Hidden forms are not treated as implicitly disabled.
        for selector in ("add-media-preview", "add-media-save", "add-media-manual"):
            for form in page.get_by_test_id(selector).all():
                buttons = form.locator('button[type="submit"],input[type="submit"]')
                assert not form.is_visible() or all(not button.is_enabled() for button in buttons.all()), selector

    assert_no_old_actions()
    control.evaluate("""(element, value) => { element.value = value;
        element.dispatchEvent(new Event('input', {bubbles: true}));
        element.dispatchEvent(new Event('change', {bubbles: true})); }""", original)
    assert_no_old_actions()
    page.wait_for_timeout(300)
    assert _calls(add_server) == calls and page._add_media_state["posts"] == posts
    _assert_no_io(page, add_server, complete_io)
    assert not _rows(add_server, "SELECT * FROM items")
    _artifact(page, tmp_path, f"invalidated-{stage}-{field}")


@pytest.mark.parametrize("width", [320, 1280])
def test_keyboard_labels_explicit_search_and_hostile_metadata_escape(add_page_factory, add_server, tmp_path, width):
    page = add_page_factory(width=width, javascript=True)
    form = _open(page, add_server, tmp_path, media_type="cassette")
    for name in ("mode", "media_type", "method", "provider", "q", "artist", "title_hint", "platform", "country"):
        control = form.locator(f'[name="{name}"]')
        assert control.evaluate("el => !!(el.labels?.length || el.getAttribute('aria-label') || el.getAttribute('aria-labelledby'))"), name
    complete_io = _io_snapshot(page, add_server)
    form.locator('[name="provider"]').select_option("discogs")
    form.locator('[name="q"]').fill("Hostile Metadata")
    form.locator('[name="artist"]').fill("Fixture Ensemble")
    page.wait_for_timeout(300)
    assert not _calls(add_server), "Typing/changing controls is not explicit Search"
    _assert_no_io(page, add_server, complete_io)
    form.locator('[name="q"]').focus()
    expect(form.locator('[name="q"]')).to_be_focused()
    with page.expect_response(lambda response: urlsplit(response.url).path == _path(add_server, "/add/search")) as response:
        with page.expect_navigation():
            page.keyboard.press("Enter")
    assert response.value.status == 200
    results = page.get_by_test_id("add-media-results")
    expect(results).to_contain_text('<img src=x onerror="alert(1)">')
    expect(results.locator("img,script,svg,iframe,[onerror],[onload],[onclick]")).to_have_count(0)
    _artifact(page, tmp_path, "escaped-long-results")
    review, save = _preview(page, add_server, tmp_path, 910099)
    expect(review).to_contain_text('<iframe src="https://evil.invalid/">not markup</iframe>')
    expect(review.locator("img,script,svg,iframe,[onerror],[onload],[onclick]")).to_have_count(0)
    assert page.evaluate("typeof window.BAD") == "undefined"
    _artifact(page, tmp_path, "escaped-long-review")


@pytest.mark.parametrize("add_server", ["music_disabled"], indirect=True)
@pytest.mark.parametrize("width", [320, 1280])
def test_entry_is_discoverable_with_music_off_and_manual_still_native(add_page_factory, add_server, tmp_path, width):
    page = add_page_factory(width=width)
    entry = page.get_by_test_id("add-media-entry").filter(visible=True)
    assert entry.count() >= 1
    expect(entry.first).to_have_attribute("href", add_server["url"] + "/add")
    assert not entry.first.locator("xpath=ancestor::*[@data-add-disclosure or @data-nav-key]").count()
    with page.expect_navigation():
        entry.first.click()
    assert urlsplit(page.url).path == _path(add_server, "/add")
    form = page.get_by_test_id("add-media-search")
    form.locator('[name="media_type"]').select_option("cassette")
    form.locator('[name="method"]').select_option("manual")
    _submit(page, add_server, form, "search")
    manual = page.get_by_test_id("add-media-manual")
    expect(manual).to_be_visible()
    manual.locator('[name="title"]').fill("Music off, manual tape")
    manual.locator('[name="identifier_kind"]').select_option("none")
    _receipt(page, add_server, tmp_path, manual, title="Music off, manual tape", mode="add", media_type="cassette", stage="manual")
    _saved_item(add_server, title="Music off, manual tape", mode="add", media_type="cassette", source="manual")
    assert not _calls(add_server)


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_entry_uses_real_role_policy(add_page_factory, add_server, tmp_path, role):
    with closing(sqlite3.connect(add_server["data_dir"] / "shelf.db")) as db:
        db.execute("UPDATE users SET role=? WHERE username=?", (role, add_server["credentials"]["username"]))
        db.commit()
    page = add_page_factory(width=320)
    entry = page.get_by_test_id("add-media-entry").filter(visible=True)
    if role == "viewer":
        expect(entry).to_have_count(0)
        before = _snapshot(add_server)
        page.goto(add_server["url"] + "/add")
        assert urlsplit(page.url).path != _path(add_server, "/add"), "Viewer must follow existing native Browse policy"
        assert _snapshot(add_server) == before and not _calls(add_server)
    else:
        assert entry.count() >= 1
        _open(page, add_server, tmp_path, media_type="book")


@pytest.mark.parametrize("add_server", [{"prefix": "/shelf"}], indirect=True)
def test_native_mounted_prefix_forms_and_prg_do_not_escape_host(add_page_factory, add_server, tmp_path):
    page = add_page_factory(width=320)
    _search(page, add_server, tmp_path, media_type="dvd", query="Synthetic Cinema", artist="", mode="wishlist")
    review, save = _preview(page, add_server, tmp_path, 930002)
    _receipt(page, add_server, tmp_path, save, title="Synthetic Cinema: Second Cut", mode="wishlist", media_type="dvd")
    _saved_item(add_server, title="Synthetic Cinema: Second Cut", mode="wishlist", media_type="dvd", source="tmdb")
    assert all(path.startswith("/shelf/add/") for path, controls in page._add_media_state["posts"])


@pytest.mark.parametrize("locale,browser_locale", [("it", "it-IT"), ("zh_Hans", "zh-CN")])
@pytest.mark.parametrize("width", [320, 1280])
def test_real_compiled_locale_native_discogs_journey(add_page_factory, add_server, tmp_path, locale, browser_locale, width):
    page = add_page_factory(width=width, locale=browser_locale)
    _search(page, add_server, tmp_path, mode="wishlist", provider="discogs", method="catalog_number", query="40-26156",
            title_hint="Hits Out of Hell")
    literals = PARENT_LOCALE_LITERALS[locale]
    assert isinstance(literals, dict) and set(literals) == LOCALE_LITERAL_KEYS, (
        f"PARENT HANDOFF NEEDED: exact {locale} literals for {sorted(LOCALE_LITERAL_KEYS)} after catalog compilation; "
        "do not invent translations or count this pending case as GREEN")
    assert all(isinstance(value, str) and value for value in literals.values())
    assert page.locator("html").get_attribute("lang").replace("_", "-").lower() == locale.replace("_", "-").lower()
    root = page.get_by_test_id("add-media")
    for key in ("heading", "search", "wishlist"):
        expect(root).to_contain_text(literals[key])
    review, save = _preview(page, add_server, tmp_path, 3935124)
    expect(review).to_contain_text(literals["artwork_later"])
    for choice in save.locator('[name="fields"]').all():
        choice.check()
    receipt = _receipt(page, add_server, tmp_path, save, title="Hits Out Of Hell", mode="wishlist", media_type="cassette", english=False)
    expect(receipt).to_contain_text(literals["saved"])
    expect(receipt).to_contain_text(PARENT_RECEIPT_LITERALS[locale]["wishlisted"])
    _saved_item(add_server, title="Hits Out Of Hell", mode="wishlist", media_type="cassette", source="discogs")
    _artifact(page, tmp_path, f"compiled-{locale}-receipt")


def test_native_real_csrf_rejects_changed_field_before_provider_work(add_page_factory, add_server, tmp_path):
    page = add_page_factory(width=320)
    form = _open(page, add_server, tmp_path, media_type="book")
    form.locator('[name="q"]').fill("The Synthetic Atlas")
    form.locator('[name="_csrf"]').evaluate("el => { el.value = 'not-the-issued-csrf'; }")
    before = _snapshot(add_server)
    path = _path(add_server, "/add/search")
    page._add_media_state["expected_http"].add((path, 403))
    with page.expect_response(lambda response: urlsplit(response.url).path == path) as received:
        with page.expect_navigation():
            form.locator('button[type="submit"]').first.click()
    assert received.value.status == 403
    _response_policy(received.value)
    assert re.search(r"csrf|forbidden|invalid|security", page.locator("body").inner_text(), re.I)
    assert not _calls(add_server) and _snapshot(add_server) == before
    _artifact(page, tmp_path, "csrf-refusal")


def test_native_tampered_review_is_real_refusal_with_manual_not_success(add_page_factory, add_server, tmp_path):
    page = add_page_factory(width=320)
    _search(page, add_server, tmp_path, provider="discogs")
    review, save = _preview(page, add_server, tmp_path, 3935124)
    ticket = save.locator('[name="ticket"]')
    # Alter the signed payload, without forging/signing another ticket or reading
    # a key. This is a genuine browser-submitted hidden control corruption probe.
    ticket.evaluate("el => { const p=el.value.split('.'); p[1]='e30'; el.value=p.join('.'); }")
    before, calls = _snapshot(add_server), _calls(add_server)
    _submit(page, add_server, save, "save", status=409)
    expect(page.get_by_test_id("add-media-error")).to_be_visible()
    expect(page.get_by_test_id("add-media-receipt")).to_have_count(0)
    _assert_no_live_prepared_save(page)
    assert _snapshot(add_server) == before and _calls(add_server) == calls
    _artifact(page, tmp_path, "tampered-review-refusal")
    _fresh_manual_recovery(page, add_server, tmp_path, before, calls, media_type="cassette")


def test_native_two_reviewed_tabs_duplicate_refusal_preserves_first_save(add_page_factory, add_server, tmp_path):
    first = add_page_factory(width=1280)
    second = add_page_factory(width=320)
    _search(first, add_server, tmp_path, provider="discogs")
    first_review, first_save = _preview(first, add_server, tmp_path, 3935124)
    _search(second, add_server, tmp_path, provider="discogs", mode="wishlist")
    second_review, second_save = _preview(second, add_server, tmp_path, 3935124)
    for save in (first_save, second_save):
        for choice in save.locator('[name="fields"]').all():
            choice.check()
    _receipt(first, add_server, tmp_path, first_save, title="Hits Out Of Hell", mode="add", media_type="cassette")
    before = _snapshot(add_server)
    calls = _calls(add_server)
    _submit(second, add_server, second_save, "save", status=409)
    expect(second.get_by_test_id("add-media-error")).to_be_visible()
    expect(second.get_by_test_id("add-media-receipt")).to_have_count(0)
    _assert_no_live_prepared_save(second)
    assert _snapshot(add_server) == before, "A second signed music review cannot demote or duplicate the first release"
    assert _calls(add_server) == calls, "Duplicate refusal must not issue provider work"
    _saved_item(add_server, title="Hits Out Of Hell", mode="add", media_type="cassette", source="discogs")
    _artifact(second, tmp_path, "duplicate-refusal")
    _fresh_manual_recovery(second, add_server, tmp_path, before, calls, media_type="cassette")


def test_native_book_promotes_wishlist_then_duplicate_receipt_reports_actual_owned_mode(add_page_factory, add_server, tmp_path):
    page = add_page_factory(width=320)
    _search(page, add_server, tmp_path, mode="wishlist", media_type="book", query="The Synthetic Atlas", artist="")
    review, save = _preview(page, add_server, tmp_path, "9780306406157")
    _details(save, add_server, status="want_to_read")
    _receipt(page, add_server, tmp_path, save, title="The Synthetic Atlas", mode="wishlist", media_type="book")
    original = _saved_item(add_server, title="The Synthetic Atlas", mode="wishlist", media_type="book", source="openlibrary", location=True)
    copies = _rows(add_server, "SELECT * FROM item_copies")
    for requested, status in (("add", "promoted"), ("wishlist", "duplicate")):
        _search(page, add_server, tmp_path, mode=requested, media_type="book", query="The Synthetic Atlas", artist="")
        review, save = _preview(page, add_server, tmp_path, "9780306406157")
        # Deliberately different defaults must NOT overwrite an existing item.
        save.locator('[name="location_id"]').fill("")
        save.locator('[name="tags"]').fill("Must not replace existing tags")
        save.locator('[name="reading_status"]').select_option("read")
        _receipt(page, add_server, tmp_path, save, title="The Synthetic Atlas", mode="add", media_type="book", status=status)
        current = _saved_item(add_server, title="The Synthetic Atlas", mode="add", media_type="book", source="openlibrary", location=True)
        assert current["id"] == original["id"] and current["reading_status"] == "want_to_read"
        assert _rows(add_server, "SELECT * FROM item_copies") == copies
        for name in ("title", "authors", "publisher", "publish_year", "isbn", "isbn10", "source", "location_id"):
            assert current[name] == original[name], name


@pytest.mark.parametrize("method,url,params", [
    ("POST", "https://api.discogs.com/database/search", {"type": "release"}),
    ("GET", "https://api.discogs.com.evil.invalid/releases/3935124", {}),
    ("GET", "https://user@api.discogs.com/releases/3935124", {}),
    ("GET", "https://api.discogs.com/releases/4026156", {}),
    ("GET", "https://api.discogs.com/releases/3935124", {"unexpected": "1"}),
    ("GET", "https://coverartarchive.org/release/11111111-1111-4111-8111-111111111101", {}),
    ("GET", "https://i.discogs.com/forbidden-artwork.jpg", {}),
    ("GET", "https://api.discogs.com:invalid/releases/3935124", {}),
    ("GET", "https://[not-an-ip]/releases/3935124", {}),
    ("GET", "https://api.discogs.com/releases/3935124", [("unexpected", "1"), ("unexpected", "2")]),
])
def test_fixture_boundary_raises_and_retains_unexpected_requests(method, url, params):
    """Harness negative control, not product/browser acceptance GREEN."""
    fixture = FixtureCatalogue()
    with pytest.raises(AssertionError, match="Unauthorized fixture outbound"):
        _fixture_fetch_in_owned_worker(fixture, method, url, params=params)
    assert len(fixture.calls) == 1 and fixture.calls[0]["unexpected"] is True
    with pytest.raises(AssertionError, match="Unexpected provider requests"):
        fixture.assert_clean()


# CSP01-CSP08 are HARNESS metadata controls, not application/browser acceptance.
# Captured policy from the ratified review; no production policy/template is read
# or changed here. Only a synthetic response's headers enter the REAL helper.
_FIXTURE_BOUNDARY_PRODUCTION_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; connect-src 'self'; font-src 'self' data:; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none';"
)


@pytest.mark.parametrize("csp", [
    pytest.param(_FIXTURE_BOUNDARY_PRODUCTION_CSP, id="CSP01-production-inline-styles-allowed"),
    pytest.param(
        " frame-ancestors 'none' ; form-action 'self'; base-uri 'self'; "
        "font-src 'self' data:; connect-src 'self'; img-src 'self' data: blob: https:; "
        "STYLE-SRC\t'self'  'unsafe-inline' ; \tSCRIPT-SRC\t'self'  ; default-src 'self'; ",
        id="CSP02-reordered-whitespace-directive-case"),
])
def test_fixture_boundary_response_policy_accepts_self_only_scripts(csp):
    """Real-helper positive controls, including unchanged inline-style support."""
    import httpx

    response = httpx.Response(200, headers={"cache-control": "no-store", "content-security-policy": csp})
    _response_policy(response)


@pytest.mark.parametrize("overrides", [
    pytest.param({"content-security-policy": "script-src 'self' 'unsafe-inline';"},
                 id="CSP03-unsafe-inline-javascript"),
    pytest.param({"content-security-policy": "script-src 'self' 'unsafe-eval';"},
                 id="CSP03-unsafe-eval-javascript"),
    pytest.param({"content-security-policy": "script-src 'self' https://foreign.invalid;"},
                 id="CSP04-foreign-script-host"),
    pytest.param({"content-security-policy": "script-src 'self' *;"},
                 id="CSP04-wildcard-script-source"),
    pytest.param({"content-security-policy": "script-src 'self' https:;"},
                 id="CSP04-scheme-wide-script-source"),
    pytest.param({"content-security-policy": "script-src 'self'; script-src-elem 'self';"},
                 id="CSP05-script-src-elem-override"),
    pytest.param({"content-security-policy": "script-src 'self'; script-src-attr 'none';"},
                 id="CSP05-script-src-attr-override"),
    pytest.param({"content-security-policy": "script-src https://foreign.invalid; script-src 'self';"},
                 id="CSP06-duplicate-unsafe-then-safe"),
    pytest.param({"content-security-policy": "script-src 'self'; script-src https://foreign.invalid;"},
                 id="CSP06-duplicate-safe-then-unsafe"),
    pytest.param({"content-security-policy": "script-src 'self'; SCRIPT-SRC 'self';"},
                 id="CSP06-duplicate-case-variant"),
    pytest.param({"content-security-policy": None}, id="CSP07-missing-enforcing-csp"),
    pytest.param({"content-security-policy": ""}, id="CSP07-empty-enforcing-csp"),
    pytest.param({"content-security-policy": "default-src 'self';"}, id="CSP07-missing-script-src"),
    pytest.param({"content-security-policy": "default-src 'self'; script-src;"}, id="CSP07-empty-script-src"),
    pytest.param({"content-security-policy": "not-script-src 'self';"}, id="CSP07-substring-decoy"),
    pytest.param({"content-security-policy": None,
                  "content-security-policy-report-only": _FIXTURE_BOUNDARY_PRODUCTION_CSP},
                 id="CSP07-report-only-without-enforcement"),
    pytest.param({"content-security-policy": "script-src 'SELF';"}, id="CSP07-source-token-case-not-normalized"),
    pytest.param({"content-security-policy": _FIXTURE_BOUNDARY_PRODUCTION_CSP, "cache-control": None},
                 id="CSP08-missing-no-store"),
    pytest.param({"content-security-policy": _FIXTURE_BOUNDARY_PRODUCTION_CSP, "cache-control": "private, max-age=30"},
                 id="CSP08-cacheable-response"),
    pytest.param({"content-security-policy": _FIXTURE_BOUNDARY_PRODUCTION_CSP, "set-cookie": "fixture=nonsecret; Path=/"},
                 id="CSP08-set-cookie-present"),
])
def test_fixture_boundary_response_policy_rejects_invalid_headers(overrides):
    """Each negative must make the ACTUAL helper reject, not an unused stand-in."""
    import httpx

    headers = {"cache-control": "no-store", "content-security-policy": "script-src 'self';"}
    for name, value in overrides.items():
        if value is None:
            headers.pop(name, None)
        else:
            headers[name] = value
    response = httpx.Response(200, headers=headers)
    with pytest.raises(AssertionError):
        _response_policy(response)


def _io_snapshot(page, server):
    return tuple(page._add_media_state["requests"]), _calls(server)


def _assert_no_io(page, server, before):
    requests, providers = _io_snapshot(page, server)
    assert requests == before[0], f"Unexpected browser requests in no-I/O window: {requests[len(before[0]):]!r}"
    assert providers == before[1], f"Unexpected provider work in no-I/O window: {providers[len(before[1]):]!r}"


def _assert_outcome_meaning(text, outcome):
    patterns = {
        "rejected": r"\b(reject\w*|refus\w*|unauthori[sz]ed|forbidden)\b",
        "rate_limited": r"\b(rate[\s-]*limit\w*|too many requests|quota)\b",
        "transport_failed": r"\b(connect\w*|network|transport|unreachable|(?:request|lookup) failed|could not be reached)\b",
        "unresolved": r"\b(no usable results|no (?:matching |identified )?(?:results|releases?)|details could not be read|could not read (?:the )?details)\b",
        "no_credential": r"\b(missing|not configured|unconfigured|configur\w*|required|need\w*)\b",
    }
    assert outcome in patterns
    qualified_transport = _QUALIFIED_TRANSPORT_WORDING.casefold() in " ".join(text.split()).casefold()
    assert re.search(patterns[outcome], text, re.I) or (outcome == "transport_failed" and qualified_transport), (outcome, text)
    if qualified_transport:
        assert outcome == "transport_failed", (outcome, text)
    if outcome == "no_credential":
        assert re.search(r"\b(key|credentials?|settings)\b", text, re.I), text
    for other in ("rejected", "rate_limited", "unresolved"):
        if other != outcome:
            assert not re.search(patterns[other], text, re.I), (outcome, other, text)


def _provider_outcome(page, outcome):
    # Provider status and HTTP errors are distinct, not interchangeable selectors.
    root = page.get_by_test_id("add-media")
    expect(page.get_by_test_id("add-media-error")).to_have_count(0)
    statuses = root.get_by_role("status").filter(visible=True)
    assert statuses.count() >= 1, "Provider outcome needs an actual visible status region"
    text = "\n".join(statuses.all_inner_texts())
    _assert_outcome_meaning(text, outcome)


def _assert_no_live_prepared_save(page):
    for save in page.get_by_test_id("add-media-save").all():
        buttons = save.locator('button[type="submit"],input[type="submit"]')
        assert not save.is_visible() or (buttons.count() > 0 and all(not button.is_enabled() for button in buttons.all()))


def _fresh_manual_recovery(page, server, tmp_path, before, calls, *, media_type):
    _assert_no_live_prepared_save(page)
    expect(page.get_by_test_id("add-media-receipt")).to_have_count(0)
    assert _snapshot(server) == before and _calls(server) == calls
    search = _open(page, server, tmp_path, media_type=media_type)
    search.locator('[name="method"]').select_option("manual")
    search.locator('[name="q"]').fill("A fresh deliberate manual entry")
    _submit(page, server, search, "search")
    expect(page.get_by_test_id("add-media-manual")).to_be_visible()
    expect(page.get_by_test_id("add-media-save")).to_have_count(0)
    expect(page.get_by_test_id("add-media-receipt")).to_have_count(0)
    assert _snapshot(server) == before and _calls(server) == calls


def _literal_receipt_expectations(save, *, title, stage):
    if stage == "manual":
        return "manual", save.locator('[name="authors"]').input_value().strip() or None
    literals = {
        "Hits Out Of Hell": ("discogs", "Meat Loaf"),
        "Paper Lantern Sessions": ("musicbrainz", "Fixture Ensemble"),
        "Synthetic Format Probe": ("discogs", "Fixture Ensemble"),
        "Mixed Medium Probe": ("musicbrainz", "Fixture Ensemble"),
        "The Synthetic Atlas": ("openlibrary", "Ada Fixture"),
        "Synthetic Cinema: Second Cut": ("tmdb", None),
        "Synthetic Switch Quest": ("igdb", None),
    }
    source, creator = literals[title]
    authors_choice = save.locator('[name="fields"][value="authors"]')
    if authors_choice.count() and not authors_choice.is_checked():
        creator = None
    return source, creator


def _named_receipt_facts(payload):
    assert payload.get("purpose") == "shelf-add-receipt-v1", payload
    candidate = payload.get("candidate")
    assert isinstance(candidate, dict), payload
    matches = []

    def visit(value):
        if isinstance(value, dict):
            if {"status", "item_id", "source"} <= value.keys():
                modes = [key for key in ("actual_mode", "mode") if key in value]
                types = [key for key in ("actual_media_type", "media_type") if key in value]
                if len(modes) == len(types) == 1:
                    matches.append({"status": value["status"], "item_id": value["item_id"], "source": value["source"],
                                    "mode": value[modes[0]], "media_type": value[types[0]]})
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(candidate)
    assert len(matches) == 1, "Expected one named receipt snapshot, not unrelated matching token values"
    facts = matches[0]
    assert type(facts["item_id"]) is int and facts["item_id"] > 0, facts
    assert facts["mode"] in ("add", "wishlist", None), facts
    return facts


def _assert_receipt_named_facts(facts, item, *, wishlisted, status, mode, media_type, source, creator, title):
    actual_mode = "add" if item["owned"] else "wishlist" if wishlisted else None
    assert facts == {"status": status, "item_id": item["id"], "source": source,
                     "mode": mode, "media_type": media_type}, facts
    assert mode == actual_mode, (mode, actual_mode, item)
    assert (item["title"], item["source"], item["authors"], item["media_type"]) == (title, source, creator, media_type), item


def _receipt_literal_values(page, *, english):
    if english:
        return ENGLISH_RECEIPT_LITERALS
    language = page.locator("html").get_attribute("lang").replace("_", "-").casefold()
    key = {"it": "it", "zh-hans": "zh_Hans"}.get(language)
    assert key is not None, ("No ratified receipt literal mapping for locale", language)
    values = PARENT_RECEIPT_LITERALS[key]
    assert isinstance(values, dict) and set(values) == RECEIPT_LITERAL_KEYS, (
        f"PARENT HANDOFF NEEDED: actual compiled {key} receipt literals for {sorted(RECEIPT_LITERAL_KEYS)}")
    assert all(isinstance(value, str) and value for value in values.values())
    return values


def _receipt_dom_evidence(receipt, server, *, literals=None):
    literals = ENGLISH_RECEIPT_LITERALS if literals is None else literals
    assert set(literals) == RECEIPT_LITERAL_KEYS and all(isinstance(value, str) and value for value in literals.values())
    normalize = lambda value: " ".join(value.split()).casefold()
    text = receipt.inner_text()
    references, collection_states = [], []
    for term in receipt.locator("dt").all():
        if not term.is_visible():
            continue
        value = term.locator("xpath=following-sibling::*[1][self::dd]")
        if value.count() != 1 or not value.is_visible():
            continue
        label, displayed = normalize(term.inner_text()), value.inner_text().strip()
        if label == normalize(literals["item_reference"]):
            assert re.fullmatch(r"[1-9][0-9]*", displayed), ("Item reference is not a positive displayed ID", displayed)
            references.append(int(displayed))
        elif label == normalize(literals["collection_state"]):
            collection_states.append(displayed)
    return {"text": text, "item_ids": tuple(references), "collection_states": tuple(collection_states),
            "receipt_literals": dict(literals)}


def _assert_rendered_receipt(evidence, item, *, source, creator, mode, english=True):
    text = evidence["text"]
    assert evidence["item_ids"] and set(evidence["item_ids"]) == {item["id"]}, evidence
    labels = {"openlibrary": r"Open\s*Library", "musicbrainz": "MusicBrainz", "discogs": "Discogs",
              "tmdb": "TMDb", "igdb": "IGDB", "manual": "Manual"}
    assert re.search(r"\b(?:" + labels[source] + r")\b", text, re.I), (source, text)
    assert item["source"] == source and item["authors"] == creator
    assert item["title"] in text, evidence
    if creator is not None:
        assert creator in text, (creator, text)
    if item["media_type"] in ("video_game", "dvd"):
        if creator is None:
            assert "Unpersisted Fixture Developer" not in text, text
        assert not re.search(r"\b(?:IGDB|TMDb)(?:\s+(?:game|movie))?\s*(?:ID|identifier|binding)\s*[:#=]?\s*[0-9]+", text, re.I), text
    assert not re.search(r"\bcaptured\s+action\b", text, re.I), "Receipt must describe actual stored mode, not a captured request"
    literals = evidence.get("receipt_literals", ENGLISH_RECEIPT_LITERALS)
    states = evidence.get("collection_states", ())
    assert len(states) == 1, ("Expected one actual stored collection-state field", states)
    normalize = lambda value: " ".join(value.split()).casefold()
    meanings = {normalize(literals["owned"]): "add", normalize(literals["wishlisted"]): "wishlist",
                normalize(literals["neither"]): None}
    assert len(meanings) == 3, "Receipt state literal meanings must be distinct"
    displayed = normalize(states[0])
    assert displayed in meanings, ("Unknown displayed stored collection state", states[0])
    assert meanings[displayed] == mode, (mode, states[0], text)


def _assert_provider_filters(calls, *, provider, country="", platform=""):
    if provider == "musicbrainz":
        searches = [call for call in calls if call["method"] == "GET" and call["url"] == "https://musicbrainz.org/ws/2/release/"]
        assert len(searches) == 1, searches
        clauses = searches[0]["params"]["query"].split(" AND ")
        country_clauses = [clause for clause in clauses if clause.startswith("country:")]
        assert country_clauses == ([f'country:"{country}"'] if country else []), searches
    elif provider == "igdb":
        searches = [call for call in calls if call["method"] == "POST" and call["url"] == "https://api.igdb.com/v4/games"
                    and (call.get("content") or "").startswith("search ")]
        assert len(searches) == 1, searches
        clauses = [clause.strip() for clause in searches[0]["content"].split(";") if clause.strip().startswith("where ")]
        assert platform in ("", "switch"), platform
        assert clauses == (["where platforms = (130)"] if platform else []), searches
    else:
        raise AssertionError("No additional provider destinations or filter forms are authorized by this oracle")


@pytest.mark.parametrize("outcome,text", [
    ("rejected", "The provider rejected this request. Add manually instead."),
    ("rate_limited", "The provider rate limit was reached. Try later or add manually."),
    ("transport_failed", "The provider could not be reached. Add manually instead."),
    ("unresolved", "Details could not be read. Add manually instead."),
    ("no_credential", "Configure the missing provider API key in settings, or add manually."),
])
def test_fixture_boundary_outcome_meaning_accepts_distinct_categories(outcome, text):
    _assert_outcome_meaning(text, outcome)


@pytest.mark.parametrize("outcome,text", [
    ("rejected", "The provider rate limit was reached."),
    ("rate_limited", "The provider rejected this request."),
    ("transport_failed", "No usable results were returned."),
    ("unresolved", "The provider could not be reached."),
    ("no_credential", "The provider rejected these credentials."),
])
def test_fixture_boundary_outcome_meaning_rejects_wrong_category(outcome, text):
    with pytest.raises(AssertionError):
        _assert_outcome_meaning(text, outcome)


def _filter_metadata(provider, observed):
    if provider == "musicbrainz":
        query = 'release:"Paper Lantern Sessions" AND artist:"Fixture Ensemble" AND format:Cassette'
        if observed:
            query += ' AND country:"IT"'
        return [{"method": "GET", "url": "https://musicbrainz.org/ws/2/release/", "params": {"query": query}}]
    query = 'search "Synthetic Switch Quest"; fields name; '
    if observed:
        query += "where platforms = (130); "
    return [{"method": "POST", "url": "https://api.igdb.com/v4/games", "content": query + "limit 10;"}]


@pytest.mark.parametrize("provider,requested", [("musicbrainz", "IT"), ("musicbrainz", ""), ("igdb", "switch"), ("igdb", "")])
def test_fixture_boundary_filter_oracle_accepts_exact_and_blank_controls(provider, requested):
    _assert_provider_filters(_filter_metadata(provider, requested), provider=provider,
                             country=requested if provider == "musicbrainz" else "",
                             platform=requested if provider == "igdb" else "")


@pytest.mark.parametrize("provider,requested,observed", [
    ("musicbrainz", "IT", ""), ("musicbrainz", "", "IT"), ("igdb", "switch", ""), ("igdb", "", "switch"),
])
def test_fixture_boundary_filter_oracle_rejects_discarded_or_invented_filter(provider, requested, observed):
    with pytest.raises(AssertionError):
        _assert_provider_filters(_filter_metadata(provider, observed), provider=provider,
                                 country=requested if provider == "musicbrainz" else "",
                                 platform=requested if provider == "igdb" else "")


@pytest.mark.parametrize("mode,owned,wishlisted", [("add", 1, False), ("wishlist", 0, True), (None, 0, False)])
def test_fixture_boundary_receipt_named_facts_use_actual_stored_state(mode, owned, wishlisted):
    item = {"id": 7, "title": "Synthetic Switch Quest", "owned": owned, "media_type": "video_game",
            "source": "igdb", "authors": None}
    payload = {"purpose": "shelf-add-receipt-v1", "candidate": {
        "intent": {"mode": "wishlist"}, "receipt": {"status": "duplicate", "item_id": 7,
        "actual_mode": mode, "actual_media_type": "video_game", "source": "igdb"}}}
    _assert_receipt_named_facts(_named_receipt_facts(payload), item, wishlisted=wishlisted, status="duplicate",
                                mode=mode, media_type="video_game", source="igdb", creator=None, title="Synthetic Switch Quest")
    label = ENGLISH_RECEIPT_LITERALS[{"add": "owned", "wishlist": "wishlisted", None: "neither"}[mode]]
    _assert_rendered_receipt({"text": "Synthetic Switch Quest — Source: IGDB — Item #7\nMode: " + label,
                              "item_ids": (7,), "collection_states": (label,)},
                             item, source="igdb", creator=None, mode=mode)
    # Surrounding prose is independent of the structured stored-state fact;
    # neither a requested mode nor historical Wishlist text can replace it.
    prose = {"add": "Owned in your collection", "wishlist": "Wishlisted", None: "Neither owned nor wishlisted"}[mode]
    _assert_rendered_receipt({"text": "Synthetic Switch Quest — IGDB — Item #7\n" + prose,
                              "item_ids": (7,), "collection_states": (label,)},
                             item, source="igdb", creator=None, mode=mode)


@pytest.mark.parametrize("kind,source,creator,text,ids", [
    ("video_game", "igdb", None, "Synthetic Switch Quest — IGDB — Unpersisted Fixture Developer", (7,)),
    ("video_game", "igdb", None, "Synthetic Switch Quest — Source: IGDB — IGDB ID: 920001", (7,)),
    ("dvd", "tmdb", None, "Synthetic Switch Quest — Source: TMDb — TMDb ID: 930002", (7,)),
    ("video_game", "igdb", None, "Synthetic Switch Quest — Source: IGDB", (8,)),
    ("video_game", "igdb", None, "Synthetic Switch Quest — Source: TMDb", (7,)),
    ("book", "openlibrary", "Ada Fixture", "Synthetic Switch Quest — Source: Open Library", (7,)),
    ("video_game", "igdb", None, "Synthetic Switch Quest — Source: IGDB\nMode: Wishlist", (7,)),
])
def test_fixture_boundary_receipt_oracle_rejects_invented_or_missing_facts(kind, source, creator, text, ids):
    # Metadata only, never fake HTML or a successful browser endpoint. The same
    # checker consumes actual DOM observations in every production receipt journey.
    item = {"id": 7, "title": "Synthetic Switch Quest", "media_type": kind, "source": source, "authors": creator}
    before = dict(item)
    if "Mode:" not in text:
        text += "\nMode: Add"
    displayed_state = ENGLISH_RECEIPT_LITERALS["wishlisted" if "Mode: Wishlist" in text else "owned"]
    with pytest.raises(AssertionError):
        _assert_rendered_receipt({"text": text, "item_ids": ids, "collection_states": (displayed_state,)},
                                 item, source=source, creator=creator, mode="add")
    assert item == before


def test_fixture_boundary_receipt_oracle_rejects_wrong_named_item_despite_matching_actor_value():
    payload = {"purpose": "shelf-add-receipt-v1", "candidate": {"actor": {"id": 7}, "receipt": {
        "status": "added", "item_id": 8, "actual_mode": "add", "actual_media_type": "video_game", "source": "igdb"}}}
    item = {"id": 7, "title": "Synthetic Switch Quest", "owned": 1, "media_type": "video_game", "source": "igdb", "authors": None}
    with pytest.raises(AssertionError):
        _assert_receipt_named_facts(_named_receipt_facts(payload), item, wishlisted=False, status="added", mode="add",
                                    media_type="video_game", source="igdb", creator=None, title="Synthetic Switch Quest")


def test_fixture_boundary_request_ledger_rejects_real_health_get(add_page_factory, add_server):
    """Explicit HARNESS counterexample: real 200 /health, not product acceptance."""
    page = add_page_factory()
    page.wait_for_load_state("networkidle")
    before = _io_snapshot(page, add_server)
    posts = list(page._add_media_state["posts"])
    add_io = list(page._add_media_state["add_io"])
    health = add_server["origin"] + "/health"
    with page.expect_response(lambda response: response.url == health and response.request.method == "GET") as received:
        result = page.evaluate("url => fetch(url).then(async r => ({status:r.status, body:await r.json()}))", health)
    assert received.value.status == 200 and result["status"] == 200 and result["body"].get("status") == "ok"
    assert page._add_media_state["posts"] == posts and page._add_media_state["add_io"] == add_io
    assert _calls(add_server) == before[1], "The weaker provider/Add-only ledgers still see no work"
    assert page._add_media_state["requests"][len(before[0]):] == [("GET", health, "fetch")]
    with pytest.raises(AssertionError, match="Unexpected browser requests"):
        _assert_no_io(page, add_server, before)


@pytest.mark.parametrize("country", ["", "IT"])
def test_native_country_filter_and_blank_reach_actual_mb_request(add_page_factory, add_server, tmp_path, country):
    page = add_page_factory(width=320)
    _search(page, add_server, tmp_path, provider="musicbrainz", query="Paper Lantern Sessions", artist="Fixture Ensemble", country=country)
    _assert_provider_filters(_calls(add_server), provider="musicbrainz", country=country)
    assert page._add_media_state["posts"][-1][1].get("country", [""]) == [country]
    _candidate(page, "11111111-1111-4111-8111-111111111101")
    assert not _rows(add_server, "SELECT * FROM items")


@pytest.mark.parametrize("platform", ["", "switch"])
def test_native_platform_filter_and_blank_reach_actual_igdb_request(add_page_factory, add_server, tmp_path, platform):
    page = add_page_factory(width=320)
    _search(page, add_server, tmp_path, media_type="video_game", query="Synthetic Switch Quest", artist="", platform=platform)
    _assert_provider_filters(_calls(add_server), provider="igdb", platform=platform)
    assert page._add_media_state["posts"][-1][1].get("platform", [""]) == [platform]
    _candidate(page, 920001)
    assert not _rows(add_server, "SELECT * FROM items")


def _require_real_enhancement(page):
    assert page.locator('script[src*="add-media.js"]').count() > 0, (
        "HARNESS_GAP: no optional enhancement is present; reset/history/eventless invalidation has not been exercised")


def _assert_all_prepared_actions_invalid(page):
    for selector in ("add-media-preview", "add-media-save", "add-media-manual"):
        for form in page.get_by_test_id(selector).all():
            buttons = form.locator('button[type="submit"],input[type="submit"]')
            assert not form.is_visible() or (buttons.count() > 0 and all(not button.is_enabled() for button in buttons.all())), selector


@pytest.mark.parametrize("width", [320, 1280])
def test_optional_js_native_form_reset_never_revives_prepared_review(add_page_factory, add_server, tmp_path, width):
    page = add_page_factory(width=width, javascript=True)
    _search(page, add_server, tmp_path, provider="discogs")
    review, save = _preview(page, add_server, tmp_path, 3935124)
    _require_real_enhancement(page)
    expect(save.locator('button[type="submit"]').first).to_be_visible()
    expect(save.locator('button[type="submit"]').first).to_be_enabled()
    search = page.get_by_test_id("add-media-search")
    query = search.locator('[name="q"]')
    original = query.input_value()
    before, io = _snapshot(add_server), _io_snapshot(page, add_server)
    query.fill("A changed query before genuine native reset")
    _assert_all_prepared_actions_invalid(page)
    reset = search.locator('button[type="reset"],input[type="reset"]')
    if reset.count():
        reset.first.click()
    else:
        search.evaluate("form => form.reset()")  # Actual browser reset algorithm/event, not a fabricated event.
    expect(query).to_have_value(original)
    _assert_all_prepared_actions_invalid(page)
    page.wait_for_timeout(300)
    _assert_no_io(page, add_server, io)
    assert _snapshot(add_server) == before
    _artifact(page, tmp_path, "genuine-reset-no-revival")


@pytest.mark.parametrize("width", [320, 1280])
def test_optional_js_actual_back_restored_query_cannot_revive_prepared_review(add_page_factory, add_server, tmp_path, width):
    page = add_page_factory(width=width, javascript=True)
    _native_history_round_trip(page, add_server, tmp_path)


def test_optional_js_eventless_after_load_change_blocks_prepared_submit_without_dispatch(add_page_factory, add_server, tmp_path):
    page = add_page_factory(width=320, javascript=True)
    _search(page, add_server, tmp_path, provider="discogs")
    review, save = _preview(page, add_server, tmp_path, 3935124)
    _require_real_enhancement(page)
    button = save.locator('button[type="submit"]').first
    expect(button).to_be_enabled()
    before, io = _snapshot(add_server), _io_snapshot(page, add_server)
    page.get_by_test_id("add-media-search").locator('[name="q"]').evaluate(
        "element => { element.value = 'An eventless restored or autofilled query'; }")
    if save.is_visible() and button.is_enabled():
        button.click(no_wait_after=True)
    page.wait_for_timeout(300)
    _assert_no_io(page, add_server, io)
    assert _snapshot(add_server) == before, (
        "Stale UI dispatch is a client-intent defect, not evidence of a signing bypass; the backend still uses captured authority")
    _assert_all_prepared_actions_invalid(page)
    _artifact(page, tmp_path, "eventless-change-blocked-before-dispatch")


_QUALIFIED_TRANSPORT_WORDING = (
    "The provider search could not be completed. "
    "This does not mean there are no matching editions."
)


def test_fixture_boundary_v4_qualified_transport_wording():
    _assert_outcome_meaning(_QUALIFIED_TRANSPORT_WORDING, "transport_failed")


@pytest.mark.parametrize("outcome,text", [
    ("unresolved", _QUALIFIED_TRANSPORT_WORDING),
    ("rejected", _QUALIFIED_TRANSPORT_WORDING),
    ("rate_limited", _QUALIFIED_TRANSPORT_WORDING),
    ("transport_failed", "No matching editions were returned."),
    ("transport_failed", "The provider search could not be completed. There are no matching editions."),
])
def test_fixture_boundary_v4_qualified_transport_rejects_wrong_category_or_empty(outcome, text):
    with pytest.raises(AssertionError):
        _assert_outcome_meaning(text, outcome)


@pytest.fixture
def receipt_dom_harness(browser):
    """Only isolated oracle-metadata fragments on about:blank; NEVER an Add page.

    This is browser-DOM HARNESS evidence, not HTTP/auth/commit/locale acceptance.
    The exact production receipt extractor receives a real Playwright Locator.
    """
    context = browser.new_context(service_workers="block")
    requests, console_errors = [], []
    context.on("request", lambda request: requests.append(request.url))
    context.route("**/*", lambda route: route.abort())
    page = attach_page_guard(context.new_page())
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

    def fragment(markup):
        assert page.url == "about:blank"
        page.evaluate("""markup => {
            const parsed = new DOMParser().parseFromString(markup, 'text/html');
            const metadata = document.createElement('section');
            metadata.id = 'harness-receipt-metadata';
            metadata.append(...parsed.body.childNodes);
            document.body.replaceChildren(metadata);
        }""", markup)
        return page.locator("#harness-receipt-metadata")

    try:
        yield fragment
        assert page.url == "about:blank"
        assert_page_clean(page)
        assert not console_errors, console_errors
        assert not requests, requests
    finally:
        context.close()


def test_fixture_boundary_v4_dom_qualified_item_reference(receipt_dom_harness):
    receipt = receipt_dom_harness("""<p>Synthetic Switch Quest — Source: IGDB</p>
        <dl><dt>Item reference</dt><dd>7</dd>
        <dt>Stored collection state</dt><dd>Owned</dd></dl>""")
    evidence = _receipt_dom_evidence(receipt, {"origin": "http://127.0.0.1", "prefix": ""})
    item = {"id": 7, "title": "Synthetic Switch Quest", "media_type": "video_game", "source": "igdb", "authors": None}
    _assert_rendered_receipt(evidence, item, source="igdb", creator=None, mode="add")


def test_fixture_boundary_v4_dom_stored_state_ignores_historical_wishlist(receipt_dom_harness):
    receipt = receipt_dom_harness("""<p>Synthetic Switch Quest — Source: IGDB — Item #7</p>
        <p>Originally on Wishlist. Requested action: Wishlist.</p>
        <dl><dt>Item reference</dt><dd>7</dd>
        <dt>Stored collection state</dt><dd>Owned</dd></dl>""")
    evidence = _receipt_dom_evidence(receipt, {"origin": "http://127.0.0.1", "prefix": ""})
    item = {"id": 7, "title": "Synthetic Switch Quest", "media_type": "video_game", "source": "igdb", "authors": None}
    _assert_rendered_receipt(evidence, item, source="igdb", creator=None, mode="add")


def _declared_history_resources(page, server):
    """Exact declarations in the prepared document, not observed back traffic.

    Optional cache revalidation is bounded to one request per declared tuple.
    There is no GET, /static/, image, script, or other resource-type exemption.
    """
    resources = set()
    for element in page.locator('script[src],link[rel~="stylesheet"][href],link[rel~="icon"][href]').all():
        declared = element.evaluate("el => ({tag:el.tagName.toLowerCase(), rel:el.rel || '', url:el.src || el.href})")
        parts = urlsplit(declared["url"])
        if parts.scheme not in ("http", "https"):
            continue
        assert f"{parts.scheme}://{parts.netloc}" == server["origin"], declared
        kind = "script" if declared["tag"] == "script" else "stylesheet" if "stylesheet" in declared["rel"].split() else "image"
        resources.add(("GET", parts._replace(fragment="").geturl(), kind))
    return tuple(sorted(resources))


def _assert_history_transition_delta(observed, *, navigation, resources):
    from collections import Counter

    actual, required, caps = Counter(observed), Counter(navigation), Counter(resources)
    assert all(count == 1 for count in caps.values()), "Resource expectations cannot silently inflate multiplicity"
    assert not required - actual, f"Missing established history navigation: {required - actual!r}"
    unexpected = actual - required - caps
    assert not unexpected, f"Unexpected history requests outside independently established transition (protocol/fixture gap): {unexpected!r}"


@pytest.mark.parametrize("observed,navigation", [
    ((), ()),
    ((("GET", "http://127.0.0.1/add", "document"), ("GET", "http://127.0.0.1/static/base.css", "stylesheet")),
     (("GET", "http://127.0.0.1/add", "document"),)),
    ((("GET", "http://127.0.0.1/static/app.js", "script"),), ()),
])
def test_fixture_boundary_v4_history_delta_accepts_only_established_navigation_resources(observed, navigation):
    resources = (("GET", "http://127.0.0.1/static/base.css", "stylesheet"), ("GET", "http://127.0.0.1/static/app.js", "script"))
    _assert_history_transition_delta(observed, navigation=navigation, resources=resources)


@pytest.mark.parametrize("extra", [
    ("GET", "http://127.0.0.1/health", "other"),
    ("GET", "http://127.0.0.1/static/unlisted.svg", "image"),
    ("GET", "http://127.0.0.1/static/base.css", "stylesheet"),
    ("GET", "http://127.0.0.1/static/base.css", "fetch"),
    ("GET", "http://127.0.0.1/static/app.js?unlisted=1", "script"),
])
def test_fixture_boundary_v4_history_delta_rejects_extra_gets_and_multiplicity(extra):
    navigation = (("GET", "http://127.0.0.1/add", "document"),)
    resources = (("GET", "http://127.0.0.1/static/base.css", "stylesheet"),)
    observed = navigation + resources + (extra,)
    with pytest.raises(AssertionError, match="Unexpected history requests"):
        _assert_history_transition_delta(observed, navigation=navigation, resources=resources)


def test_fixture_boundary_v4_history_delta_rejects_missing_established_navigation():
    with pytest.raises(AssertionError, match="Missing established history navigation"):
        _assert_history_transition_delta((), navigation=(("GET", "http://127.0.0.1/add", "document"),), resources=())


def test_fixture_boundary_v4_history_rejects_real_nonxhr_get(add_page_factory, add_server):
    """Real successful non-XHR document GET, solely a HARNESS counterexample.

    This base declares no icon (the initial image-probe gap is retained). Use
    its existing real /health surface, never a synthetic success endpoint or
    an exception for unexpected image/resource requests.
    """
    page = add_page_factory()
    page.wait_for_load_state("networkidle")
    resources = _declared_history_resources(page, add_server)
    probe = add_server["origin"] + "/health?add_media_history_oracle=v4"
    before = _io_snapshot(page, add_server)
    received = page.goto(probe)
    assert received.status == 200 and received.json().get("status") == "ok"
    delta = tuple(page._add_media_state["requests"][len(before[0]):])
    assert delta == (("GET", probe, "document"),), delta
    assert not any(method == "POST" or kind in ("fetch", "xhr") for method, url, kind in delta), "The V3 transition filter misses this successful GET"
    assert _calls(add_server) == before[1]
    with pytest.raises(AssertionError, match="Unexpected history requests"):
        _assert_history_transition_delta(delta, navigation=(), resources=resources)


@pytest.mark.parametrize("mode,state_key,prose", [
    ("add", "owned", "Previously on Wishlist. Requested action: Wishlist. The current state below is authoritative."),
    ("wishlist", "wishlisted", "Previously Owned. Requested action: Add. The current state below is authoritative."),
    (None, "neither", "Requested action: Wishlist. Neither prior ownership nor requested intent decides the state below."),
])
def test_fixture_boundary_v4_dom_current_collection_state_is_independent(receipt_dom_harness, mode, state_key, prose):
    state = ENGLISH_RECEIPT_LITERALS[state_key]
    receipt = receipt_dom_harness(f"""<p>Synthetic Switch Quest — Source: IGDB</p><p>{prose}</p>
        <dl><dt>Item reference</dt><dd><bdi>7</bdi></dd>
        <dt>Stored collection state</dt><dd>{state}</dd></dl>""")
    evidence = _receipt_dom_evidence(receipt, {"origin": "http://127.0.0.1", "prefix": ""})
    assert evidence["item_ids"] == (7,) and evidence["collection_states"] == (state,)
    item = {"id": 7, "title": "Synthetic Switch Quest", "media_type": "video_game", "source": "igdb", "authors": None}
    _assert_rendered_receipt(evidence, item, source="igdb", creator=None, mode=mode)


@pytest.mark.parametrize("fields", [
    pytest.param('<dt>Item reference</dt><dd>8</dd><dt>Stored collection state</dt><dd>Owned</dd>', id="wrong-item-reference"),
    pytest.param('<dt>Actor reference</dt><dd>7</dd><dt>Provider release number</dt><dd>7</dd>'
                 '<dt>Stored collection state</dt><dd>Owned</dd>', id="actor-and-provider-numbers-are-not-item-reference"),
    pytest.param('<dt>Actor reference</dt><dd>7</dd><dt>Stored collection state</dt><dd>Owned</dd>', id="bare-item-number-in-prose-is-not-qualified-field"),
    pytest.param('<dt>Item reference</dt><dd hidden>7</dd><dt>Stored collection state</dt><dd>Owned</dd>', id="hidden-reference-is-not-display-evidence"),
    pytest.param('<dt>Item reference</dt><dd>7x</dd><dt>Stored collection state</dt><dd>Owned</dd>', id="non-numeric-reference"),
    pytest.param('<dt>Item reference</dt><dd>7</dd><dt>Stored collection state</dt><dd>Wishlist</dd>', id="wrong-current-state-despite-owned-prose"),
    pytest.param('<dt>Item reference</dt><dd>7</dd><dt>Stored collection state</dt><dd>Unknown state</dd>', id="unknown-is-not-known-neither"),
    pytest.param('<dt>Item reference</dt><dd>7</dd>', id="prose-is-not-stored-state-field"),
    pytest.param('<dt>Item reference</dt><dd>7</dd><dt>Requested action</dt><dd>Add</dd>', id="requested-action-is-not-stored-state-field"),
])
def test_fixture_boundary_v4_dom_rejects_unqualified_reference_or_wrong_state(receipt_dom_harness, fields):
    receipt = receipt_dom_harness(f"""<p>Synthetic Switch Quest — Source: IGDB — Item #7</p>
        <p>Historical Owned collection. Requested action: Add.</p><dl>{fields}</dl>""")
    item = {"id": 7, "title": "Synthetic Switch Quest", "media_type": "video_game", "source": "igdb", "authors": None}
    with pytest.raises(AssertionError):
        evidence = _receipt_dom_evidence(receipt, {"origin": "http://127.0.0.1", "prefix": ""})
        _assert_rendered_receipt(evidence, item, source="igdb", creator=None, mode="add")


@pytest.mark.parametrize("mode,state_key", [("add", "owned"), ("wishlist", "wishlisted"), (None, "neither")])
def test_fixture_boundary_v4_dom_uses_explicit_synthetic_label_metadata(receipt_dom_harness, mode, state_key):
    """Label-plumbing HARNESS only, not invented Italian/Chinese translations."""
    labels = {"item_reference": "HARNESS reference token", "collection_state": "HARNESS collection token",
              "owned": "HARNESS owned token", "wishlisted": "HARNESS wish token", "neither": "HARNESS neither token"}
    state = labels[state_key]
    receipt = receipt_dom_harness(f"""<p>Synthetic Switch Quest — Source: IGDB</p>
        <dl><dt>{labels['item_reference']}</dt><dd>7</dd>
        <dt>{labels['collection_state']}</dt><dd>{state}</dd></dl>""")
    evidence = _receipt_dom_evidence(receipt, {"origin": "http://127.0.0.1", "prefix": ""}, literals=labels)
    assert evidence["item_ids"] == (7,) and evidence["collection_states"] == (state,)
    item = {"id": 7, "title": "Synthetic Switch Quest", "media_type": "video_game", "source": "igdb", "authors": None}
    _assert_rendered_receipt(evidence, item, source="igdb", creator=None, mode=mode, english=False)


def test_fixture_boundary_v5_dom_accepts_literal_wishlisted(receipt_dom_harness):
    """DOM-label HARNESS: the expected source literal is not read from the map."""
    receipt = receipt_dom_harness("""<p>Synthetic Switch Quest — Source: IGDB</p>
        <p>Requested action: Add. Previously Owned.</p>
        <dl><dt>Item reference</dt><dd>7</dd>
        <dt>Stored collection state</dt><dd>Wishlisted</dd></dl>""")
    evidence = _receipt_dom_evidence(receipt, {"origin": "http://127.0.0.1", "prefix": ""})
    assert evidence["collection_states"] == ("Wishlisted",)
    item = {"id": 7, "title": "Synthetic Switch Quest", "media_type": "video_game", "source": "igdb", "authors": None}
    _assert_rendered_receipt(evidence, item, source="igdb", creator=None, mode="wishlist")


@pytest.mark.parametrize("current_state", ["Owned", "Wishlist"])
def test_fixture_boundary_v5_dom_requested_historical_wishlist_cannot_replace_current_state(receipt_dom_harness, current_state):
    receipt = receipt_dom_harness(f"""<p>Synthetic Switch Quest — Source: IGDB</p>
        <p>Requested action: Wishlist. Previously on Wishlist.</p>
        <dl><dt>Item reference</dt><dd>7</dd>
        <dt>Stored collection state</dt><dd>{current_state}</dd></dl>""")
    evidence = _receipt_dom_evidence(receipt, {"origin": "http://127.0.0.1", "prefix": ""})
    item = {"id": 7, "title": "Synthetic Switch Quest", "media_type": "video_game", "source": "igdb", "authors": None}
    with pytest.raises(AssertionError):
        _assert_rendered_receipt(evidence, item, source="igdb", creator=None, mode="wishlist")


# Parent source-grounded V1 ratification: these literal identities are NOT learned
# from the Back window or broadened to generic GET/static/type allowances.
_NATIVE_HISTORY_ASSETS = (
    ("/static/css/app.css", "stylesheet"),
    ("/static/css/media-navigation.source.css", "stylesheet"),
    ("/static/css/command-palette.source.css", "stylesheet"),
    ("/static/css/cover-size.source.css", "stylesheet"),
    ("/static/css/item-density.source.css", "stylesheet"),
    ("/static/css/copy-locator.source.css", "stylesheet"),
    ("/static/vendor/htmx-2.0.4.min.js", "script"),
    ("/static/js/component-load-guard.js", "script"),
    ("/static/js/components.js", "script"),
    ("/static/js/components-library.js", "script"),
    ("/static/js/components-item.js", "script"),
    ("/static/js/components-settings.js", "script"),
    ("/static/vendor/alpinejs-csp-3.15.9.min.js", "script"),
    ("/static/js/csrf.js", "script"),
    ("/static/js/media-navigation.js", "script"),
    ("/static/js/app.js", "script"),
    ("/static/js/command-palette.js", "script"),
    ("/static/js/cover-size.js", "script"),
    ("/static/js/copy-locator.js", "script"),
    ("/static/js/add-media.js", "script"),
)
_NATIVE_HISTORY_DETAIL = {"method": "GET", "url": "https://api.discogs.com/releases/3935124", "params": {},
                          "authorization_present": False, "data_keys": [], "content": None}
_NATIVE_HISTORY_INTENT = {"mode": "add", "media_type": "cassette", "method": "title", "query": "Hits Out of Hell",
                          "artist": "Meat Loaf", "title_hint": "", "platform": "", "country": "", "music_provider": "discogs"}


def _native_history_resources(server):
    assert len(_NATIVE_HISTORY_ASSETS) == len(set(_NATIVE_HISTORY_ASSETS)) == 20
    return tuple(("GET", server["origin"] + path, kind) for path, kind in _NATIVE_HISTORY_ASSETS)


def _assert_native_history_declarations(page, server):
    from collections import Counter

    actual = []
    for element in page.locator('script[src],link[rel~="stylesheet"][href],link[rel~="icon"][href]').all():
        declared = element.evaluate("el => ({tag:el.tagName.toLowerCase(), rel:el.rel || '', url:el.src || el.href})")
        kind = "script" if declared["tag"] == "script" else "stylesheet" if "stylesheet" in declared["rel"].split() else "image"
        actual.append(("GET", declared["url"], kind))
    # Keep duplicates, full URLs and queries: declarations must match all twenty
    # literal source-grounded identities/counts BEFORE any historical intent edit.
    assert Counter(actual) == Counter(_native_history_resources(server)), ("Native history declarations changed", actual)


def _native_history_request_fact(page, request):
    return {"method": request.method, "url": request.url, "kind": request.resource_type,
            "navigation": request.is_navigation_request(), "main_frame": request.frame == page.main_frame,
            "body": request.post_data_buffer}


def _native_history_response_fact(page, response):
    return {"request": _native_history_request_fact(page, response.request),
            "status": response.status, "headers": dict(response.headers)}


def _native_history_observe(page):
    trace = {"requests": [], "responses": []}
    # Passive actual browser events only. No routes, synthetic events or altered
    # request bodies; the existing complete context ledger remains authoritative.
    page.context.on("request", lambda request: trace["requests"].append(_native_history_request_fact(page, request)))
    page.context.on("response", lambda response: trace["responses"].append(_native_history_response_fact(page, response)))
    return trace


def _native_history_signed_intent(save):
    payload = _unsigned_payload(save.locator('[name="ticket"]').input_value())
    assert payload.get("purpose") == "shelf-add-music-review-v1", payload.get("purpose")
    candidate = payload.get("candidate")
    assert isinstance(candidate, dict) and isinstance(candidate.get("add_media_intent"), dict)
    return dict(candidate["add_media_intent"])


def _native_history_original(page, server, response, save):
    request = _native_history_request_fact(page, response.request)
    assert (request["method"], request["url"], request["kind"]) == ("POST", server["url"] + "/add/preview", "document")
    assert request["navigation"] is True and request["main_frame"] is True
    assert response.status == 200
    _response_policy(response)
    body = request["body"]
    assert isinstance(body, bytes) and body
    controls = parse_qs(body.decode("ascii"), keep_blank_values=True, strict_parsing=True)
    assert set(controls) == {"_csrf", "context", "selection"}
    assert all(len(values) == 1 and values[0] for values in controls.values())
    assert _unsigned_payload(controls["selection"][0]).get("purpose") == "shelf-add-selection-v1"
    intent = _native_history_signed_intent(save)
    assert intent == _NATIVE_HISTORY_INTENT, intent
    return {"body": body, "controls": controls, "intent": intent}


def _native_history_evidence(page, server, before, trace):
    current = _io_snapshot(page, server)
    assert current[0][:len(before[0])] == before[0]
    assert current[1][:len(before[1])] == before[1]
    return {"observed": current[0][len(before[0]):], "requests": tuple(trace["requests"]),
            "responses": tuple(trace["responses"]), "providers": current[1][len(before[1]):]}


def _assert_native_history_transition(evidence, *, server, original):
    """STRICT complete native trace, shared by productive Back and real-extra-GET.

    The older optional-resource metadata checker/three positives stay unchanged.
    Here every ratified asset, request property, response and provider read is
    mandatory, not an optional cap or a baseline learned from suspect traffic.
    """
    from collections import Counter
    from types import SimpleNamespace

    navigation = (("POST", server["url"] + "/add/preview", "document"),)
    resources = _native_history_resources(server)
    observed = evidence["observed"]
    _assert_history_transition_delta(observed, navigation=navigation, resources=resources)
    expected = Counter(navigation + resources)
    assert Counter(observed) == expected, ("Missing mandatory native-history requests", expected - Counter(observed))
    triplet = lambda request: (request["method"], request["url"], request["kind"])
    requests = evidence["requests"]
    assert Counter(triplet(request) for request in requests) == expected, "Detailed events must cover the ENTIRE request ledger"
    preview = [request for request in requests if triplet(request) == navigation[0]]
    assert len(preview) == 1
    assert preview[0]["navigation"] is True and preview[0]["main_frame"] is True, preview
    assert preview[0]["body"] == original["body"], "Back must replay the EXACT original accepted preview body"
    controls = parse_qs(preview[0]["body"].decode("ascii"), keep_blank_values=True, strict_parsing=True)
    assert controls == original["controls"] and set(controls) == {"_csrf", "context", "selection"}
    assert all(len(values) == 1 and values[0] for values in controls.values())
    for request in requests:
        if triplet(request) != navigation[0]:
            assert request["navigation"] is False and request["main_frame"] is True and request["body"] is None, request
    responses = evidence["responses"]
    assert Counter(triplet(response["request"]) for response in responses) == expected, "Missing or extra native-history responses"
    for response in responses:
        assert response["status"] == 200, (response["request"]["url"], response["status"])
        if triplet(response["request"]) == navigation[0]:
            assert response["request"] == preview[0]
            # Header-only adapter to the UNCHANGED policy oracle; production
            # evidence comes from the real response event, never fabricated HTTP.
            _response_policy(SimpleNamespace(headers=response["headers"]))
    assert evidence["providers"] == [_NATIVE_HISTORY_DETAIL], (
        "Expected exactly one additional normal Discogs detail read, no search/MB/artwork/other provider work", evidence["providers"])


def _native_history_round_trip(page, server, tmp_path):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    _search(page, server, tmp_path, provider="discogs")
    initial_providers = _calls(server)
    with page.expect_response(lambda response: response.request.method == "POST"
                              and response.url == server["url"] + "/add/preview") as accepted:
        review, save = _preview(page, server, tmp_path, 3935124)
    _require_real_enhancement(page)
    expect(save.locator('button[type="submit"]').first).to_be_visible()
    expect(save.locator('button[type="submit"]').first).to_be_enabled()
    original = _native_history_original(page, server, accepted.value, save)
    assert _calls(server) == initial_providers + [_NATIVE_HISTORY_DETAIL]
    _assert_native_history_declarations(page, server)
    restored_query = "An unsubmitted query genuinely restored by browser history"
    query = page.get_by_test_id("add-media-search").locator('[name="q"]')
    expect(query).to_have_value(original["intent"]["query"])
    assert restored_query != original["intent"]["query"]
    before, edit_io = _snapshot(server), _io_snapshot(page, server)
    query.fill(restored_query)
    _assert_all_prepared_actions_invalid(page)
    _assert_no_io(page, server, edit_io)
    assert _snapshot(server) == before
    providers_before_away = _calls(server)
    response = page.goto(server["url"] + "/add?mode=wishlist&media_type=book")
    assert response.status == 200
    _response_policy(response)
    page.wait_for_load_state("networkidle")
    assert _calls(server) == providers_before_away, "The deliberate GET away must not query providers"
    assert _snapshot(server) == before
    trace = _native_history_observe(page)
    io_before_back = _io_snapshot(page, server)
    try:
        back_response = page.go_back(wait_until="load", timeout=10000)
    except PlaywrightTimeoutError as error:
        pytest.fail(f"HARNESS_GAP: real browser back restoration timed out; classification requires the retained page/request evidence: {error}")
    assert back_response is not None and back_response.status == 200
    _response_policy(back_response)
    _assert_native_history_transition(_native_history_evidence(page, server, io_before_back, trace), server=server, original=original)
    root = page.get_by_test_id("add-media")
    assert root.count() == 1, "HARNESS_GAP: back did not restore an actual Add page"
    query = root.get_by_test_id("add-media-search").locator('[name="q"]')
    observed_query = query.input_value()
    assert observed_query == restored_query, ("HARNESS_GAP: the differing historical control state was not restored", observed_query)
    assert observed_query != original["intent"]["query"]
    navigation_type = page.evaluate("performance.getEntriesByType('navigation')[0]?.type")
    assert navigation_type == "back_forward", (
        "HARNESS_GAP: no actual history navigation was observed; no synthetic pageshow/popstate substitute is accepted", navigation_type)
    restored_review = page.get_by_test_id("add-media-review")
    expect(restored_review).to_be_visible()
    restored_save = restored_review.get_by_test_id("add-media-save")
    expect(restored_save).to_have_count(1)
    assert _native_history_signed_intent(restored_save) == original["intent"]
    _assert_all_prepared_actions_invalid(page)
    assert _snapshot(server) == before
    # Recheck the ENTIRE transition around baseline capture: DOM observations,
    # provider audit reads, wait and artifact creation cannot hide a late request.
    _assert_native_history_transition(_native_history_evidence(page, server, io_before_back, trace), server=server, original=original)
    clean = _io_snapshot(page, server)
    _assert_native_history_transition(_native_history_evidence(page, server, io_before_back, trace), server=server, original=original)
    page.wait_for_timeout(300)
    _assert_no_io(page, server, clean)
    _assert_native_history_transition(_native_history_evidence(page, server, io_before_back, trace), server=server, original=original)
    assert _snapshot(server) == before
    _artifact(page, tmp_path, "actual-history-restored-intent")
    _assert_no_io(page, server, clean)
    _assert_native_history_transition(_native_history_evidence(page, server, io_before_back, trace), server=server, original=original)
    return {"before": io_before_back, "trace": trace, "original": original}


def test_native_history_full_transition_rejects_real_nonxhr_get(add_page_factory, add_server, tmp_path):
    """HARNESS counterexample requiring actual Add; NEVER author-base feature GREEN."""
    page = add_page_factory(width=320, javascript=True)
    transition = _native_history_round_trip(page, add_server, tmp_path)
    before = _io_snapshot(page, add_server)
    catalogue = _snapshot(add_server)
    posts, add_io = list(page._add_media_state["posts"]), list(page._add_media_state["add_io"])
    probe = add_server["origin"] + "/health?add_media_native_history_oracle=v5"
    received = page.goto(probe)
    assert received.status == 200 and received.json().get("status") == "ok"
    delta = tuple(page._add_media_state["requests"][len(before[0]):])
    assert delta == (("GET", probe, "document"),), delta
    assert not any(method == "POST" or kind in ("fetch", "xhr") for method, url, kind in delta)
    assert page._add_media_state["posts"] == posts and page._add_media_state["add_io"] == add_io
    assert _calls(add_server) == before[1], "Weaker Add/provider-only ledgers miss this extra successful non-XHR GET"
    assert _snapshot(add_server) == catalogue
    # No fabricated legitimate prefix: this is the actual already-qualified Back
    # ledger PLUS the genuine successful health document request, using the SAME
    # strict checker that requires all twenty assets in the productive cases.
    augmented = _native_history_evidence(page, add_server, transition["before"], transition["trace"])
    with pytest.raises(AssertionError, match="Unexpected history requests"):
        _assert_native_history_transition(augmented, server=add_server, original=transition["original"])


def _native_history_metadata():
    """Pure oracle metadata, NOT HTTP, signed authority, an Add page or a commit."""
    server = {"origin": "http://127.0.0.1", "url": "http://127.0.0.1", "prefix": ""}
    original = {"body": b"_csrf=HARNESS-csrf&context=HARNESS-context&selection=HARNESS-selection",
                "controls": {"_csrf": ["HARNESS-csrf"], "context": ["HARNESS-context"], "selection": ["HARNESS-selection"]}}
    requests = [{"method": "POST", "url": server["url"] + "/add/preview", "kind": "document",
                 "navigation": True, "main_frame": True, "body": original["body"]}]
    requests.extend({"method": method, "url": url, "kind": kind, "navigation": False, "main_frame": True, "body": None}
                    for method, url, kind in _native_history_resources(server))
    responses = [{"request": dict(request), "status": 200,
                  "headers": {"cache-control": "no-store", "content-security-policy": "script-src 'self';"}}
                 for request in requests]
    evidence = {"observed": tuple((request["method"], request["url"], request["kind"]) for request in requests),
                "requests": requests, "responses": responses, "providers": json.loads(json.dumps([_NATIVE_HISTORY_DETAIL]))}
    return server, original, evidence


def test_fixture_boundary_v5_native_history_accepts_exact_full_transition_metadata():
    server, original, evidence = _native_history_metadata()
    _assert_native_history_transition(evidence, server=server, original=original)
    evidence["observed"] = tuple(reversed(evidence["observed"]))
    evidence["requests"].reverse()
    evidence["responses"].reverse()
    _assert_native_history_transition(evidence, server=server, original=original)


@pytest.mark.parametrize("fault", [
    "missing-css", "missing-script", "duplicate-asset", "asset-query", "asset-type", "extra-get", "extra-post",
    "missing-preview", "duplicate-preview", "non-navigation-preview", "other-frame-preview", "changed-preview-body",
    "preview-status", "preview-cacheable", "preview-cookie", "preview-csp", "missing-provider", "duplicate-provider",
    "provider-search", "provider-params", "provider-authorization", "provider-musicbrainz", "detailed-event-gap", "missing-response",
])
def test_fixture_boundary_v5_native_history_rejects_incomplete_or_changed_transition_metadata(fault):
    """All are HARNESS metadata failures, not fabricated application responses."""
    from copy import deepcopy

    server, original, evidence = _native_history_metadata()
    requests, responses = evidence["requests"], evidence["responses"]
    if fault in ("missing-css", "missing-script", "missing-preview"):
        index = {"missing-css": 1, "missing-script": -1, "missing-preview": 0}[fault]
        requests.pop(index)
        responses.pop(index)
    elif fault in ("duplicate-asset", "duplicate-preview"):
        index = 1 if fault == "duplicate-asset" else 0
        requests.append(deepcopy(requests[index]))
        responses.append(deepcopy(responses[index]))
    elif fault in ("asset-query", "asset-type"):
        key, value = ("url", requests[1]["url"] + "?unratified=1") if fault == "asset-query" else ("kind", "image")
        requests[1][key] = responses[1]["request"][key] = value
    elif fault in ("extra-get", "extra-post"):
        extra = {"method": "GET" if fault == "extra-get" else "POST",
                 "url": server["origin"] + ("/health" if fault == "extra-get" else "/add/save"),
                 "kind": "document", "navigation": True, "main_frame": True, "body": None}
        requests.append(extra)
        responses.append({"request": dict(extra), "status": 200, "headers": {}})
    elif fault in ("non-navigation-preview", "other-frame-preview", "changed-preview-body"):
        key, value = {"non-navigation-preview": ("navigation", False), "other-frame-preview": ("main_frame", False),
                      "changed-preview-body": ("body", original["body"].replace(b"HARNESS-selection", b"HARNESS-other-selection"))}[fault]
        requests[0][key] = responses[0]["request"][key] = value
    elif fault == "preview-status":
        responses[0]["status"] = 409
    elif fault in ("preview-cacheable", "preview-cookie", "preview-csp"):
        key, value = {"preview-cacheable": ("cache-control", "private, max-age=30"),
                      "preview-cookie": ("set-cookie", "HARNESS=nonsecret; Path=/"),
                      "preview-csp": ("content-security-policy", "script-src 'self' 'unsafe-inline';")}[fault]
        responses[0]["headers"][key] = value
    elif fault == "missing-provider":
        evidence["providers"] = []
    elif fault == "duplicate-provider":
        evidence["providers"].append(deepcopy(evidence["providers"][0]))
    elif fault in ("provider-search", "provider-params", "provider-authorization", "provider-musicbrainz"):
        key, value = {"provider-search": ("url", "https://api.discogs.com/database/search"),
                      "provider-params": ("params", {"extra": "1"}), "provider-authorization": ("authorization_present", True),
                      "provider-musicbrainz": ("url", "https://musicbrainz.org/ws/2/release/")}[fault]
        evidence["providers"][0][key] = value
    elif fault == "detailed-event-gap":
        requests.pop()
    elif fault == "missing-response":
        responses.pop()
    else:
        raise AssertionError(f"Unimplemented HARNESS fault: {fault}")
    if fault != "detailed-event-gap":
        evidence["observed"] = tuple((request["method"], request["url"], request["kind"]) for request in requests)
    with pytest.raises(AssertionError):
        _assert_native_history_transition(evidence, server=server, original=original)


def _fixture_fetch_in_owned_worker(fixture, method, url, *, params):
    """Own the fixture coroutine and its event loop off SyncPlaywright's thread."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    def invoke():
        # Creation AND asyncio.run/await occur inside this single owned worker.
        return asyncio.run(fixture.fetch(None, method, url, params=params))

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="add-media-fixture") as worker:
        return worker.submit(invoke).result(timeout=10)


def test_fixture_boundary_owned_worker_with_alive_playwright(browser):
    """Engine-backed lifecycle HARNESS, not an authenticated Add host or save."""
    import asyncio

    assert browser.is_connected(), "The real session browser must remain connected"
    loop = asyncio.get_running_loop()
    assert loop.is_running() and browser._impl_obj._connection._loop is loop, (
        "Exercise the actual alive SyncPlaywright connection/running-loop context")
    fixture = FixtureCatalogue()
    response = _fixture_fetch_in_owned_worker(fixture, "GET", "https://api.discogs.com/releases/3935124", params={})
    assert response.status_code == 200 and response.json()["id"] == 3935124
    assert fixture.calls == [{"method": "GET", "url": "https://api.discogs.com/releases/3935124", "params": {},
                              "authorization_present": False, "data_keys": [], "content": None}]
    assert not fixture.unexpected
    fixture.assert_clean()


def _v8_set_disposable_entry_role(server, role):
    """Fixture setup only, BEFORE genuine native login; never mint authority."""
    assert role in {"editor", "admin", "viewer"}
    with closing(sqlite3.connect(server["data_dir"] / "shelf.db")) as db:
        changed = db.execute("UPDATE users SET role=? WHERE username=?", (role, server["credentials"]["username"]))
        assert changed.rowcount == 1
        db.commit()


def _v8_visible_entry_witness(page, server):
    """One shared actual-DOM witness; no request, click or expected value from UI."""
    entries = page.get_by_test_id("add-media-entry").filter(visible=True)
    assert entries.count() >= 1, "Missing actually visible native Add Media entry"
    expected = server["url"] + "/add"
    for entry in entries.all():
        assert entry.evaluate("element => element.tagName === 'A'"), "The entry must be an actual native anchor"
        assert entry.get_attribute("href") == expected, "Visible Add Media entry has the wrong target"
        assert not entry.locator("xpath=ancestor-or-self::*[@data-nav-disclosure or @data-nav-tab or @data-nav-menu-tab]").count(), (
            "The Add action must remain outside NAV tab/disclosure membership")
        assert entry.evaluate("""element => {
            if (element.closest('main')) return false;
            const nav = element.closest('nav[data-media-navigation]');
            if (nav) return !nav.closest('main');
            const actionRow = element.closest('body > div.app-shell');
            return !!actionRow && !actionRow.querySelector('main');
        }"""), "Every visible Add Media entry must be in the global shell outside main"
    return entries


def _v8_cookie_fingerprint(page):
    """Compare the disposable real cookie jar without retaining authority values."""
    import hashlib

    cookies = sorted(page.context.cookies(), key=lambda cookie: (cookie["domain"], cookie["path"], cookie["name"]))
    return hashlib.sha256(json.dumps(cookies, sort_keys=True).encode()).hexdigest()


def _v8_follow_actual_entry(page, server, tmp_path, *, keyboard):
    assert urlsplit(page.url).path == "/", "Start on the ordinary real page after genuine login"
    page.wait_for_load_state("networkidle")
    before, providers = _snapshot(server), _calls(server)
    assert not providers, "Ordinary entry discovery must not consult providers"
    cookies = _v8_cookie_fingerprint(page)
    add_offset = len(page._add_media_state["add_io"])
    posts = list(page._add_media_state["posts"])
    entry = _v8_visible_entry_witness(page, server).first
    target = server["url"] + "/add"
    if keyboard:
        entry.focus()
        expect(entry).to_be_focused()
    with page.expect_response(lambda response: response.url == target and response.request.method == "GET") as reached:
        with page.expect_navigation(wait_until="load"):
            if keyboard:
                page.keyboard.press("Enter")
            else:
                entry.click()
    response = reached.value
    assert response.status == 200 and response.url == page.url == target
    assert response.request.is_navigation_request() and response.request.frame == page.main_frame
    assert response.request.resource_type == "document" and response.request.post_data_buffer is None
    _response_policy(response)
    root = page.get_by_test_id("add-media")
    expect(root).to_be_visible()
    search = root.get_by_test_id("add-media-search")
    expect(search).to_be_visible()
    assert search.evaluate("element => element.tagName === 'FORM'"), "Reach actual native host forms"
    _assert_forms(page, server)
    assert page._add_media_state["add_io"][add_offset:] == [("GET", _path(server, "/add"), "document")]
    assert page._add_media_state["posts"] == posts
    assert _calls(server) == providers and _snapshot(server) == before
    assert _v8_cookie_fingerprint(page) == cookies, "The Add GET must not issue or refresh browser authority"
    _artifact(page, tmp_path, "native-entry-keyboard" if keyboard else "native-entry-click")
    assert _calls(server) == providers and _snapshot(server) == before
    assert _v8_cookie_fingerprint(page) == cookies


@pytest.mark.parametrize("add_server", ["normal", "music_disabled"], indirect=True)
@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("role", ["editor", "admin"])
def test_native_v8_entry_link_reaches_canonical_add_without_javascript(add_page_factory, add_server, tmp_path, role, width):
    _v8_set_disposable_entry_role(add_server, role)
    page = add_page_factory(width=width, javascript=False)
    # Both are real anchor activations. Never replace the witnessed link with a
    # direct GET/_open call after merely counting an entry on the ordinary page.
    _v8_follow_actual_entry(page, add_server, tmp_path, keyboard=width == 320)


@pytest.mark.parametrize("add_server", ["normal", "music_disabled"], indirect=True)
@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("identity", ["viewer", "anonymous"])
def test_native_v8_entry_and_add_authority_refused_for_viewer_or_anonymous(add_page_factory, add_server, tmp_path, identity, width):
    from urllib.parse import urljoin

    _v8_set_disposable_entry_role(add_server, "viewer" if identity == "viewer" else "editor")
    page = add_page_factory(width=width, javascript=False)
    if identity == "anonymous":
        # Start with a genuine authenticated context, then remove its REAL
        # cookies using the browser API; never forge a session or auth decision.
        page.context.clear_cookies()
        login = page.goto(add_server["origin"] + "/")
        assert login.status == 200 and urlsplit(page.url).path == "/login"
    page.wait_for_load_state("networkidle")
    expect(page.get_by_test_id("add-media-entry").filter(visible=True)).to_have_count(0)
    before, providers = _snapshot(add_server), _calls(add_server)
    assert not providers
    add_offset = len(page._add_media_state["add_io"])
    posts = list(page._add_media_state["posts"])
    target = add_server["url"] + "/add"
    with page.expect_response(lambda response: response.url == target and response.request.method == "GET") as refused:
        final = page.goto(target)
    response = refused.value
    assert response.status == 303, "The INITIAL Add response must retain native auth/role refusal"
    assert response.request.is_navigation_request() and response.request.frame == page.main_frame
    assert response.request.resource_type == "document" and response.request.post_data_buffer is None
    _response_policy(response)
    destination = urlsplit(urljoin(response.url, response.headers["location"]))
    expected_path = "/browse" if identity == "viewer" else "/login"
    assert f"{destination.scheme}://{destination.netloc}" == add_server["origin"]
    assert destination.path == expected_path and urlsplit(page.url).path == expected_path
    assert final.status == 200
    expect(page.get_by_test_id("add-media-entry").filter(visible=True)).to_have_count(0)
    for selector in ("add-media", "add-media-preview", "add-media-save", "add-media-manual", "add-media-receipt"):
        expect(page.get_by_test_id(selector)).to_have_count(0)
    assert page._add_media_state["add_io"][add_offset:] == [("GET", _path(add_server, "/add"), "document")]
    assert page._add_media_state["posts"] == posts
    assert _calls(add_server) == providers and _snapshot(add_server) == before
    # Only the INITIAL Add response gets Add's no-cookie policy. The ordinary
    # Browse/Login destination keeps its existing non-Add cookie behavior.
    _artifact(page, tmp_path, f"native-entry-refused-{identity}")
    assert _calls(add_server) == providers and _snapshot(add_server) == before


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("fault", ["missing", "hidden", "wrong-target"])
def test_native_v8_entry_witness_rejects_real_page_dom_fault_without_io(add_page_factory, add_server, tmp_path, fault, width):
    """Add-dependent HARNESS: fault ONLY actual observed links on a REAL page."""
    _v8_set_disposable_entry_role(add_server, "editor")
    page = add_page_factory(width=width, javascript=False)
    assert urlsplit(page.url).path == "/"
    page.wait_for_load_state("networkidle")
    # This must succeed BEFORE injecting a fault. A base with no actual Add
    # entry is absence, not a vacuous negative-control pass or fabricated page.
    entries = _v8_visible_entry_witness(page, add_server)
    before, io = _snapshot(add_server), _io_snapshot(page, add_server)
    cookies = _v8_cookie_fingerprint(page)
    entries.evaluate_all("""(observedEntries, fault) => {
        for (const entry of observedEntries) {
            if (fault.kind === 'missing') entry.remove();
            else if (fault.kind === 'hidden') {
                entry.hidden = true;
                entry.style.setProperty('display', 'none', 'important');
            }
            else if (fault.kind === 'wrong-target') entry.setAttribute('href', fault.wrongTarget);
            else throw new Error('Unknown entry HARNESS fault');
        }
    }""", {"kind": fault, "wrongTarget": add_server["origin"] + "/health"})
    reason = "wrong target" if fault == "wrong-target" else "Missing actually visible"
    with pytest.raises(AssertionError, match=reason):
        _v8_visible_entry_witness(page, add_server)
    page.wait_for_timeout(300)
    _assert_no_io(page, add_server, io)
    assert _snapshot(add_server) == before
    assert _v8_cookie_fingerprint(page) == cookies
    _artifact(page, tmp_path, f"HARNESS-native-entry-{fault}")
    _assert_no_io(page, add_server, io)
    assert _snapshot(add_server) == before and _v8_cookie_fingerprint(page) == cookies


def _v9_assert_entry_dom_window_clean(page, server, tmp_path, before, io, cookies, stage):
    page.wait_for_timeout(300)
    _assert_no_io(page, server, io)
    assert _snapshot(server) == before and _v8_cookie_fingerprint(page) == cookies
    _artifact(page, tmp_path, stage)
    _assert_no_io(page, server, io)
    assert _snapshot(server) == before and _v8_cookie_fingerprint(page) == cookies


@pytest.mark.parametrize("width", [320, 1280])
def test_native_v9_entry_witness_accepts_actual_global_shell_nav(add_page_factory, add_server, tmp_path, width):
    """Real-page DOM HARNESS: global navigation is allowed, not all nav forbidden."""
    _v8_set_disposable_entry_role(add_server, "editor")
    page = add_page_factory(width=width, javascript=False)
    page.wait_for_load_state("networkidle")
    entries = _v8_visible_entry_witness(page, add_server)
    entry = entries.first.element_handle()
    navs = page.locator("nav[data-media-navigation]").filter(visible=True)
    assert navs.count() >= 1, "HARNESS_GAP: need the actual visible global shell navigation"
    nav = navs.first.element_handle()
    assert entry is not None and nav is not None
    assert nav.evaluate("element => !element.closest('main')")
    before, io = _snapshot(add_server), _io_snapshot(page, add_server)
    cookies = _v8_cookie_fingerprint(page)
    try:
        # Move the observed REAL anchor into the existing REAL global nav. No
        # cloned link, fabricated application HTML, auth, HTTP or writer decision.
        entry.evaluate("(element, nav) => nav.appendChild(element)", nav)
        assert entry.is_visible() and entry.get_attribute("href") == add_server["url"] + "/add"
        assert entry.evaluate("element => !element.closest('[data-nav-disclosure],[data-nav-tab],[data-nav-menu-tab]')")
        _v8_visible_entry_witness(page, add_server)
    finally:
        _v9_assert_entry_dom_window_clean(page, add_server, tmp_path, before, io, cookies, "HARNESS-v9-global-nav-allowed")


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("fault", ["data-nav-tab", "data-nav-menu-tab", "open-disclosure", "main"])
def test_native_v9_entry_witness_rejects_actual_membership_or_main(add_page_factory, add_server, tmp_path, fault, width):
    """Add-dependent HARNESS on actual published markers/shell/main, before clicks."""
    _v8_set_disposable_entry_role(add_server, "editor")
    page = add_page_factory(width=width, javascript=False)
    page.wait_for_load_state("networkidle")
    entries = _v8_visible_entry_witness(page, add_server)
    entry = entries.first.element_handle()
    assert entry is not None
    destination = None
    if fault == "open-disclosure":
        targets = page.locator("nav[data-media-navigation] details[data-nav-disclosure]").filter(visible=True)
        assert targets.count() >= 1, "HARNESS_GAP: need an actual visible native shell disclosure"
        destination = targets.first.element_handle()
        assert destination is not None and destination.evaluate("element => !element.closest('main')")
    elif fault == "main":
        targets = page.locator("main").filter(visible=True)
        assert targets.count() >= 1, "HARNESS_GAP: need the ordinary page's actual visible main"
        destination = targets.first.element_handle()
        assert destination is not None
    before, io = _snapshot(add_server), _io_snapshot(page, add_server)
    cookies = _v8_cookie_fingerprint(page)
    try:
        if fault in ("data-nav-tab", "data-nav-menu-tab"):
            entry.evaluate("(element, marker) => element.setAttribute(marker, 'HARNESS-v9')", fault)
            assert entry.get_attribute(fault) == "HARNESS-v9"
        elif fault == "open-disclosure":
            entry.evaluate("(element, details) => { details.open = true; details.appendChild(element); }", destination)
            assert destination.evaluate("element => element.open && element.matches('details[data-nav-disclosure]')")
        else:
            entry.evaluate("(element, main) => main.prepend(element)", destination)
            assert entry.evaluate("element => !!element.closest('main')")
        # Keep every other real matching entry in place. The witness must inspect
        # ALL visible matches rather than filter this misplaced/member one away.
        assert entry.is_visible() and entry.get_attribute("href") == add_server["url"] + "/add"
        with pytest.raises(AssertionError, match="outside NAV|global shell"):
            _v8_visible_entry_witness(page, add_server)
    finally:
        # Also execute these checks/artifacts for the genuine DID NOT RAISE RED.
        _v9_assert_entry_dom_window_clean(page, add_server, tmp_path, before, io, cookies, f"HARNESS-v9-entry-{fault}")
