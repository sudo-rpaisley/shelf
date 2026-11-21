"""Real editor/Save regression, SOURCE ONLY until the parent opens runtime gates.

No application endpoint is intercepted or invented. A disposable real ASGI app
serves the editor/search/Save routes; only provider boundaries return fixed
candidates/image bytes. Publication and the item/private writers stay real.
"""

import asyncio
import json
import re
import socket
import threading
import time

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import assert_page_clean, attach_page_guard
from tests.test_covers_phase4_004 import _format_bytes, _png_bytes, session_principal

pytestmark = pytest.mark.e2e

CHOICE_URL = "https://covers.openlibrary.org/b/id/400004-L.jpg"
MANUAL_URL = "https://manual-images.example.test/editor-004.png"
FAILED_MANUAL_URL = "https://manual-images.example.test/missing-004.png"
ORIGINAL_ISBN = "9786155420818"
EDITION_ISBN = "9780735211292"


def _catalogue_snapshot():
    from app.database import get_db

    with get_db() as db:
        return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("items", "library_items", "item_copies", "user_item_state", "user_reading_log")}


class _ManualProviderHold004:
    """Default-off, one-request hold owned by the real server's async loop."""

    def __init__(self):
        self.enabled = threading.Event()
        self.started = threading.Event()
        self.released = threading.Event()
        self._lock = threading.Lock()
        self._loop = None
        self._event = None

    async def wait(self):
        if not self.enabled.is_set():
            return
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        with self._lock:
            assert self._event is None, "this fixture holds exactly one actual manual provider call"
            self._loop, self._event = loop, event
            # Teardown may have released before the provider reached this edge.
            if self.released.is_set():
                event.set()
        self.started.set()
        await event.wait()

    def release(self):
        with self._lock:
            self.released.set()
            loop, event = self._loop, self._event
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(event.set)


@pytest.fixture
def editor_cover_app_004(db, admin_user, viewer_user, monkeypatch):
    import httpx
    import uvicorn
    from app import config
    from app.main import app
    from app.services import catalogue_security, covers, item_write, manual_cover, outbound, provider_result, user_state

    for name in ("SHELF_DISABLE_RATE_LIMIT", "SHELF_DISABLE_COVER_ENRICH",
                 "SHELF_DISABLE_PRICE_ALERTS", "SHELF_DISABLE_RELEASE_RADAR"):
        monkeypatch.setenv(name, "1")
    item_id = item_write.insert_item(db, title="Original editor title", isbn=ORIGINAL_ISBN,
        language="hu", media_type="book", manual_value=10,
        cover_path="covers/editor-original.png")
    original_bytes = _png_bytes(seed=1)
    (config.COVERS_DIR / "editor-original.png").write_bytes(original_bytes)
    for user, rating, status, notes in (
        (admin_user, 4, "want_to_read", "Keep my private rating"),
        (viewer_user, 1, "read", "Never overwrite another actor"),
    ):
        token = catalogue_security.set_actor(session_principal(db, user))
        try:
            user_state.save_state(db, user["id"], item_id,
                rating=rating, reading_status=status, personal_notes=notes)
        finally:
            catalogue_security.reset_actor(token)
    db.commit()

    searches, downloads, publications = [], [], []
    image_bytes = _format_bytes("jpeg")
    manual_calls = []
    manual_bytes = _png_bytes(seed=4)
    manual_hold = _ManualProviderHold004()

    async def manual_download(download_id, url):
        assert download_id < 0
        manual_calls.append((download_id, url))
        if url == FAILED_MANUAL_URL:
            return None
        assert url == MANUAL_URL
        await manual_hold.wait()
        assert covers.validate_uploaded_cover(manual_bytes)
        # Only this provider edge is doubled. Write the real negative-ID stage;
        # the actual route, locked publisher and transaction all remain real.
        return covers.write_uploaded_cover(download_id, manual_bytes)

    async def search(title, author, isbn, client, *, google_api_key=None):
        searches.append((title, author, isbn, google_api_key))
        return provider_result.found("openlibrary", [{
            "url": CHOICE_URL, "thumbnail": CHOICE_URL, "source": "Open Library",
            "label": "English cover from another edition", "edition_isbn": EDITION_ISBN,
            "edition_language": "en",
        }])

    async def fetch(client, method, url, **kwargs):
        assert method == "GET" and url == CHOICE_URL
        downloads.append((method, url, dict(kwargs)))
        return httpx.Response(200, content=image_bytes, headers={"content-type": "image/jpeg"},
            request=httpx.Request(method, url))

    real_publish = covers.publish_staged_cover
    def publish(conn, stage, relative, **kwargs):
        assert stage.item_id == item_id and stage.download_id < 0
        assert set(kwargs) == {"authorize", "store"}
        assert callable(kwargs["authorize"]) and callable(kwargs["store"])
        publications.append((stage.item_id, stage.download_id, relative))
        return real_publish(conn, stage, relative, **kwargs)

    monkeypatch.setattr(covers, "search_book_covers", search)
    monkeypatch.setattr(outbound, "fetch", fetch)
    monkeypatch.setattr(manual_cover, "download", manual_download)
    monkeypatch.setattr(covers, "publish_staged_cover", publish)
    # The shared app may already have been imported by an earlier unit case.
    # Point its REAL static mount at this test's isolated covers, not a stale
    # directory. No route response or permission dependency is substituted.
    mount = next(route for route in app.routes if getattr(route, "path", None) == "/covers")
    monkeypatch.setattr(mount.app, "directory", str(config.COVERS_DIR))
    monkeypatch.setattr(mount.app, "all_directories", [str(config.COVERS_DIR)])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "disposable real application did not start"
        yield {"url": base, "item_id": item_id, "searches": searches,
               "downloads": downloads, "publications": publications,
               "image_bytes": image_bytes, "covers_dir": config.COVERS_DIR,
               "manual_calls": manual_calls, "manual_bytes": manual_bytes,
               "manual_hold": manual_hold,
               "admin": admin_user, "other": viewer_user}
    finally:
        manual_hold.release()
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def test_cover_choice_keeps_real_dirty_controls_and_private_ratings_until_atomic_save(
    editor_cover_app_004, browser
):
    from app.database import get_db

    session = editor_cover_app_004
    base, item_id = session["url"], session["item_id"]
    context = browser.new_context(service_workers="block")
    try:
        # Only the external IMAGE resource is stubbed in the browser. Search
        # and Save must go through the real server, never route.fulfill HTML.
        context.route(CHOICE_URL, lambda route: route.fulfill(
            content_type="image/jpeg", body=session["image_bytes"]))
        page = attach_page_guard(context.new_page())
        page.goto(f"{base}/login")
        page.locator('input[name="username"]').fill("admin")
        page.locator('input[name="password"]').fill("password123")
        page.locator('button[type="submit"]').click()
        page.wait_for_url(f"{base}/")
        page.goto(f"{base}/item/{item_id}/edit")
        page.locator("#title").fill("Dirty title stays in this editor")
        page.locator("#reading_status").select_option("reading")
        page.locator('a[href="#edit-location"]').click()
        page.locator("#manual_value").fill("23.45")
        page.locator('a[href="#edit-artwork"]').click()
        with page.expect_response(lambda response: f"/api/items/{item_id}/cover-search" in response.url):
            page.get_by_role("button", name="Find artwork", exact=True).click()
        choice = page.locator(f'[data-edit-cover-select-url="{CHOICE_URL}"]')
        expect(choice).to_be_visible()
        assert choice.get_attribute("hx-post") is None
        assert session["searches"] == [("Original editor title", None, ORIGINAL_ISBN, None)]
        before = _catalogue_snapshot()
        with get_db() as db:
            other_before = tuple(db.execute("SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?",
                (session["other"]["id"], item_id)).fetchone())
        files_before = {p.name: p.read_bytes() for p in session["covers_dir"].iterdir()}
        before_url = page.url
        posts, navigations = [], []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        page.on("framenavigated", lambda frame: navigations.append(frame.url))

        choice.click()

        form = page.locator("#item-edit-form")
        expect(form.locator('[name="cover_choice_url"]')).to_have_value(CHOICE_URL)
        expect(form.locator('[name="cover_choice_edition_isbn"]')).to_have_value(EDITION_ISBN)
        expect(form.locator('[name="cover_choice_edition_language"]')).to_have_value("en")
        expect(choice).to_have_attribute("aria-pressed", "true")
        assert page.locator("#title").input_value() == "Dirty title stays in this editor"
        assert page.locator("#manual_value").input_value() == "23.45"
        assert page.locator("#reading_status").input_value() == "reading"
        assert page.url == before_url
        assert posts == navigations == []
        assert session["downloads"] == session["publications"] == []
        assert _catalogue_snapshot() == before
        assert {p.name: p.read_bytes() for p in session["covers_dir"].iterdir()} == files_before
        _assert_reactive_preview(page, CHOICE_URL)

        page.get_by_test_id("save-btn").click()
        page.wait_for_url(re.compile(rf"^{re.escape(base)}/item/{item_id}(?:\?.*)?$"))
        assert posts == [f"{base}/api/items/{item_id}"]
        assert session["downloads"] == [("GET", CHOICE_URL, {"follow_redirects": True, "retry_timeouts": True})]
        assert len(session["publications"]) == 1
        target, download_id, relative = session["publications"][0]
        assert target == item_id and relative == f"covers/{download_id}.jpg"
        with get_db() as db:
            item = dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
            mine = dict(db.execute("SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?",
                (session["admin"]["id"], item_id)).fetchone())
            other = tuple(db.execute("SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?",
                (session["other"]["id"], item_id)).fetchone())
        assert item["title"] == "Dirty title stays in this editor"
        assert item["manual_value"] == 23.45
        assert item["isbn"] == ORIGINAL_ISBN and item["language"] == "hu"
        assert (item["cover_edition_isbn"], item["cover_edition_language"]) == (EDITION_ISBN, "en")
        assert item["cover_path"] == f"covers/{item_id}.jpg"
        assert (session["covers_dir"] / f"{item_id}.jpg").read_bytes() == session["image_bytes"]
        assert not (session["covers_dir"] / f"{download_id}.jpg").exists()
        assert mine["reading_status"] == "reading" and mine["rating"] == 4
        assert mine["personal_notes"] == "Keep my private rating"
        assert other == other_before
        assert_page_clean(page)
    finally:
        context.close()


def _open_dirty_editor(session, browser):
    context = browser.new_context(service_workers="block")
    context.route(CHOICE_URL, lambda route: route.fulfill(
        content_type="image/jpeg", body=session["image_bytes"]))
    # A manual success must preview the real stored resource, never cause an
    # uncontrolled browser request to the user-pasted external host.
    context.route("https://manual-images.example.test/**", lambda route: route.abort())
    page = attach_page_guard(context.new_page())
    base = session["url"]
    page.goto(f"{base}/login")
    page.locator('input[name="username"]').fill("admin")
    page.locator('input[name="password"]').fill("password123")
    page.locator('button[type="submit"]').click()
    page.wait_for_url(f"{base}/")
    page.goto(f"{base}/item/{session['item_id']}/edit")
    page.locator("#title").fill("Dirty title stays in this editor")
    page.locator("#reading_status").select_option("reading")
    page.locator('a[href="#edit-location"]').click()
    page.locator("#manual_value").fill("23.45")
    page.locator('a[href="#edit-artwork"]').click()
    return context, page


def _assert_dirty_controls(page):
    assert page.locator("#title").input_value() == "Dirty title stays in this editor"
    assert page.locator("#manual_value").input_value() == "23.45"
    assert page.locator("#reading_status").input_value() == "reading"


def _assert_reactive_preview(page, source):
    image = page.locator('[data-edit-cover-preview]')
    expect(image).to_be_visible()
    # A matching src alone cannot prove that the real image decoded, or that
    # Alpine will not overwrite it on its next reactive update.
    pattern = r"^blob:" if source == "blob:" else rf"^{re.escape(source)}(?:\?.*)?$"
    expect(image).to_have_attribute("src", re.compile(pattern))
    state = image.evaluate("""async image => {
        await image.decode();
        await new Promise(resolve => window.Alpine.nextTick(resolve));
        return {complete: image.complete, width: image.naturalWidth,
            preview: window.Alpine.$data(image.closest('[x-data="coverDrop"]')).preview,
            src: image.getAttribute('src')};
    }""")
    assert state["complete"] is True and state["width"] > 0
    assert state["preview"] == state["src"]
    assert state["src"].startswith("blob:") if source == "blob:" else state["src"].split("?")[0] == source
    page.locator('a[href="#edit-general"]').click()
    page.locator('a[href="#edit-artwork"]').click()
    expect(image).to_be_visible()
    after = image.evaluate("""async image => {
        await image.decode();
        await new Promise(resolve => window.Alpine.nextTick(resolve));
        return {complete: image.complete, width: image.naturalWidth,
            preview: window.Alpine.$data(image.closest('[x-data="coverDrop"]')).preview,
            src: image.getAttribute('src')};
    }""")
    assert after["complete"] is True and after["width"] > 0
    assert after["preview"] == after["src"] == state["src"]


def _successful_artwork_fields(page):
    return page.evaluate("""() => {
        const data = new FormData(document.getElementById('item-edit-form'));
        return {url: data.get('url'), choice: data.get('cover_choice_url'),
            isbn: data.get('cover_choice_edition_isbn'), language: data.get('cover_choice_edition_language'),
            file: data.get('cover').name};
    }""")


def _find_choice(page, session):
    with page.expect_response(lambda response: f"/api/items/{session['item_id']}/cover-search" in response.url):
        page.get_by_role("button", name="Find artwork", exact=True).click()
    choice = page.locator(f'[data-edit-cover-select-url="{CHOICE_URL}"]')
    expect(choice).to_be_visible()
    assert choice.get_attribute("hx-post") is None
    expect(choice).to_contain_text("English cover from another edition")
    return choice


def test_successful_manual_htmx_then_provider_choice_saves_real_dirty_fields_and_provenance(
    editor_cover_app_004, browser
):
    from app.database import get_db

    session = editor_cover_app_004
    context, page = _open_dirty_editor(session, browser)
    target, base = session["item_id"], session["url"]
    try:
        before = _catalogue_snapshot()
        with get_db() as db:
            item_before = dict(db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone())
            other_before = tuple(db.execute("SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?",
                (session["other"]["id"], target)).fetchone())
        # An older upload must be superseded by a successfully applied manual
        # cover, not remain a second successful source at the final Save.
        page.locator("#cover-input").set_input_files({"name": "older-upload.png",
            "mimeType": "image/png", "buffer": _png_bytes(seed=2)})
        _assert_reactive_preview(page, "blob:")
        posts = []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        original_url = page.url
        page.locator("#edit-cover-url").fill(MANUAL_URL)
        with page.expect_response(lambda response: response.request.method == "POST" and
                                 response.url == f"{base}/api/items/{target}/cover-url") as used:
            page.get_by_test_id("edit-cover-url-submit").click()
        assert used.value.status == 200
        assert json.loads(used.value.headers["hx-trigger"])["showToast"]["type"] == "success"
        expect(page.locator("#edit-cover-url")).to_have_value("")
        expect(page.get_by_test_id("edit-cover-url-submit")).to_be_enabled()
        assert _successful_artwork_fields(page) == {"url": "", "choice": "", "isbn": "", "language": "", "file": ""}
        assert page.url == original_url
        assert posts == [f"{base}/api/items/{target}/cover-url"]
        assert len(session["manual_calls"]) == len(session["publications"]) == 1
        assert session["manual_calls"][0] == (session["publications"][0][1], MANUAL_URL)
        assert session["downloads"] == []
        with get_db() as db:
            item_after_manual = dict(db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone())
        for field in item_before.keys() - {"cover_path", "cover_edition_isbn", "cover_edition_language", "updated_at"}:
            assert item_after_manual[field] == item_before[field], field
        after_manual = _catalogue_snapshot()
        for table in before.keys() - {"items"}:
            assert after_manual[table] == before[table], table
        assert (session["covers_dir"] / item_after_manual["cover_path"].split("/")[-1]).read_bytes() == session["manual_bytes"]
        _assert_reactive_preview(page, "/" + item_after_manual["cover_path"])
        _assert_dirty_controls(page)

        choice = _find_choice(page, session)
        files_before_choice = {p.name: p.read_bytes() for p in session["covers_dir"].iterdir()}
        choice.click()
        expect(choice).to_have_attribute("aria-pressed", "true")
        _assert_reactive_preview(page, CHOICE_URL)
        assert _successful_artwork_fields(page) == {"url": "", "choice": CHOICE_URL,
            "isbn": EDITION_ISBN, "language": "en", "file": ""}
        _assert_dirty_controls(page)
        assert _catalogue_snapshot() == after_manual
        assert {p.name: p.read_bytes() for p in session["covers_dir"].iterdir()} == files_before_choice
        assert session["downloads"] == [] and len(session["publications"]) == 1
        assert posts == [f"{base}/api/items/{target}/cover-url"]

        with page.expect_response(lambda response: response.request.method == "POST" and
                                 response.url == f"{base}/api/items/{target}") as saved:
            page.get_by_test_id("save-btn").click()
        assert saved.value.status == 303, "the real Save must not hit the 400 mixed-source refusal"
        page.wait_for_url(re.compile(rf"^{re.escape(base)}/item/{target}(?:\?.*)?$"))
        assert posts == [f"{base}/api/items/{target}/cover-url", f"{base}/api/items/{target}"]
        assert len(session["publications"]) == 2
        assert session["downloads"] == [("GET", CHOICE_URL, {"follow_redirects": True, "retry_timeouts": True})]
        with get_db() as db:
            item = dict(db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone())
            mine = dict(db.execute("SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?",
                (session["admin"]["id"], target)).fetchone())
            other = tuple(db.execute("SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?",
                (session["other"]["id"], target)).fetchone())
        assert (item["title"], item["manual_value"]) == ("Dirty title stays in this editor", 23.45)
        assert (item["isbn"], item["language"]) == (ORIGINAL_ISBN, "hu")
        assert (item["cover_edition_isbn"], item["cover_edition_language"]) == (EDITION_ISBN, "en")
        assert (session["covers_dir"] / item["cover_path"].split("/")[-1]).read_bytes() == session["image_bytes"]
        assert mine["reading_status"] == "reading" and mine["rating"] == 4
        assert mine["personal_notes"] == "Keep my private rating"
        assert other == other_before
        assert_page_clean(page)
    finally:
        context.close()


def test_failed_manual_htmx_retains_url_dirty_controls_and_pending_choice_without_effects(
    editor_cover_app_004, browser
):
    session = editor_cover_app_004
    context, page = _open_dirty_editor(session, browser)
    try:
        choice = _find_choice(page, session)
        choice.click()
        _assert_reactive_preview(page, CHOICE_URL)
        before = _catalogue_snapshot()
        files_before = {p.name: p.read_bytes() for p in session["covers_dir"].iterdir()}
        page.locator("#edit-cover-url").fill(FAILED_MANUAL_URL)
        with page.expect_response(lambda response: response.request.method == "POST" and
                                 response.url.endswith(f"/api/items/{session['item_id']}/cover-url")) as refused:
            page.get_by_test_id("edit-cover-url-submit").click()
        assert refused.value.status == 200
        assert json.loads(refused.value.headers["hx-trigger"])["showToast"]["type"] == "error"
        expect(page.get_by_test_id("edit-cover-url-submit")).to_be_enabled()
        expect(page.locator("#edit-cover-url")).to_have_value(FAILED_MANUAL_URL)
        assert _successful_artwork_fields(page) == {"url": FAILED_MANUAL_URL, "choice": CHOICE_URL,
            "isbn": EDITION_ISBN, "language": "en", "file": ""}
        expect(choice).to_have_attribute("aria-pressed", "true")
        _assert_reactive_preview(page, CHOICE_URL)
        _assert_dirty_controls(page)
        assert _catalogue_snapshot() == before
        assert {p.name: p.read_bytes() for p in session["covers_dir"].iterdir()} == files_before
        assert session["publications"] == session["downloads"] == []
        assert len(session["manual_calls"]) == 1 and session["manual_calls"][0][1] == FAILED_MANUAL_URL
        assert_page_clean(page)
    finally:
        context.close()


def test_provider_file_drop_and_provider_again_share_real_successful_fields_and_reactive_preview(
    editor_cover_app_004, browser
):
    session = editor_cover_app_004
    context, page = _open_dirty_editor(session, browser)
    try:
        choice = _find_choice(page, session)
        before = _catalogue_snapshot()
        files_before = {p.name: p.read_bytes() for p in session["covers_dir"].iterdir()}
        posts = []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        choice.click()
        _assert_reactive_preview(page, CHOICE_URL)
        expect(choice).to_have_attribute("aria-pressed", "true")
        page.locator("#cover-input").set_input_files({"name": "picked-004.png",
            "mimeType": "image/png", "buffer": _png_bytes(seed=2)})
        _assert_reactive_preview(page, "blob:")
        picked_preview = page.locator('[data-edit-cover-preview]').get_attribute("src")
        assert _successful_artwork_fields(page) == {"url": "", "choice": "", "isbn": "", "language": "", "file": "picked-004.png"}
        expect(choice).to_have_attribute("aria-pressed", "false")

        transfer = page.evaluate_handle("""bytes => {
            const transfer = new DataTransfer();
            transfer.items.add(new File([new Uint8Array(bytes)], 'dropped-004.png', {type: 'image/png'}));
            return transfer;
        }""", list(_png_bytes(seed=3)))
        try:
            page.locator('[x-data="coverDrop"] .border-dashed').dispatch_event("drop", {"dataTransfer": transfer})
        finally:
            transfer.dispose()
        _assert_reactive_preview(page, "blob:")
        assert page.locator('[data-edit-cover-preview]').get_attribute("src") != picked_preview
        assert _successful_artwork_fields(page) == {"url": "", "choice": "", "isbn": "", "language": "", "file": "dropped-004.png"}
        expect(choice).to_have_attribute("aria-pressed", "false")

        choice.click()
        _assert_reactive_preview(page, CHOICE_URL)
        expect(choice).to_have_attribute("aria-pressed", "true")
        assert page.locator("#cover-input").evaluate("input => input.files.length") == 0
        assert _successful_artwork_fields(page) == {"url": "", "choice": CHOICE_URL,
            "isbn": EDITION_ISBN, "language": "en", "file": ""}
        _assert_dirty_controls(page)
        assert posts == []
        assert session["publications"] == session["downloads"] == session["manual_calls"] == []
        assert _catalogue_snapshot() == before
        assert {p.name: p.read_bytes() for p in session["covers_dir"].iterdir()} == files_before
        assert_page_clean(page)
    finally:
        context.close()


def test_held_manual_provider_blocks_native_save_until_explicit_save_of_newer_choice(
    editor_cover_app_004, browser
):
    from app.database import get_db

    session = editor_cover_app_004
    hold = session["manual_hold"]
    context, page = _open_dirty_editor(session, browser)
    target, base = session["item_id"], session["url"]
    try:
        choice = _find_choice(page, session)
        before = _catalogue_snapshot()
        files_before = {p.name: p.read_bytes() for p in session["covers_dir"].iterdir()}
        with get_db() as db:
            item_before = dict(db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone())
            mine_before = dict(db.execute("SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?",
                (session["admin"]["id"], target)).fetchone())
            other_before = dict(db.execute("SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?",
                (session["other"]["id"], target)).fetchone())
        assert (mine_before["rating"], mine_before["reading_status"], mine_before["personal_notes"]) == (
            4, "want_to_read", "Keep my private rating")
        assert (other_before["rating"], other_before["reading_status"], other_before["personal_notes"]) == (
            1, "read", "Never overwrite another actor")
        form = page.locator("#item-edit-form")
        busy_before = form.get_attribute("aria-busy")
        original_url = page.url
        posts = []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        # Observe the UA-generated submit before the form's capture barrier;
        # inspect cancellation after dispatch. No synthetic submit or endpoint
        # response is invented, and no listener here prevents the default action.
        page.evaluate("""() => {
            const form = document.getElementById('item-edit-form');
            window.coverEditorOrderSubmits004 = [];
            document.addEventListener('submit', event => {
                if (event.target !== form) return;
                const record = {trusted: event.isTrusted,
                    submitter: event.submitter && event.submitter.getAttribute('data-testid'),
                    get prevented() { return event.defaultPrevented; }};
                window.coverEditorOrderSubmits004.push(record);
                // Native UA dispatch can checkpoint microtasks between capture
                // listeners. Observe the Event's FINAL state when records are
                // read after dispatch, never snapshot before the form handler.
            }, true);
        }""")
        hold.enabled.set()
        page.locator("#edit-cover-url").fill(MANUAL_URL)
        page.get_by_test_id("edit-cover-url-submit").click()
        assert hold.started.wait(timeout=10), "the actual server-side manual provider edge never started"
        assert not hold.released.is_set()
        expect(page.locator("#edit-cover-url")).to_be_disabled()
        expect(page.get_by_test_id("edit-cover-url-submit")).to_be_disabled()
        expect(form).to_have_attribute("aria-busy", "true")
        assert len(session["manual_calls"]) == 1 and session["manual_calls"][0][1] == MANUAL_URL
        assert session["manual_calls"][0][0] < 0

        choice.click()
        expect(choice).to_have_attribute("aria-pressed", "true")
        _assert_reactive_preview(page, CHOICE_URL)
        _assert_dirty_controls(page)
        # This is the original bypass: disabled url is absent, so the server's
        # mixed-source guard cannot serialize pending B against in-flight A.
        assert _successful_artwork_fields(page) == {"url": None, "choice": CHOICE_URL,
            "isbn": EDITION_ISBN, "language": "en", "file": ""}
        assert form.evaluate("form => form.checkValidity()")
        expect(page.get_by_test_id("save-btn")).to_be_enabled()
        page.get_by_test_id("save-btn").click()  # Unforced, genuine native Save attempt while A is held.
        form.evaluate("""form => {
            HTMLFormElement.prototype.requestSubmit.call(form, form.querySelector('[data-testid="save-btn"]'));
        }""")  # A second genuine submit path must not bypass the capture barrier.
        # Native dispatch is synchronous; await its actual queued observation
        # microtasks, not wait_for_function's CSP-refused eval/polling machinery.
        assert page.evaluate("""async () => {
            await Promise.resolve();
            return window.coverEditorOrderSubmits004;
        }""") == [
            {"trusted": True, "submitter": "save-btn", "prevented": True},
            {"trusted": True, "submitter": "save-btn", "prevented": True},
        ]
        expect(page.locator("#toast-container").get_by_text(
            "Wait for the artwork URL request to finish, then save your changes.", exact=True).last).to_be_visible()
        assert page.url == original_url
        assert posts == [f"{base}/api/items/{target}/cover-url"]
        assert not hold.released.is_set()
        assert session["downloads"] == session["publications"] == []
        assert _catalogue_snapshot() == before, "blocked Save must not write metadata or either actor's private state"
        assert {p.name: p.read_bytes() for p in session["covers_dir"].iterdir()} == files_before
        _assert_dirty_controls(page)

        with page.expect_response(lambda response: response.request.method == "POST" and
                                 response.url == f"{base}/api/items/{target}/cover-url") as applied:
            hold.release()
        assert applied.value.status == 200
        assert json.loads(applied.value.headers["hx-trigger"])["showToast"]["type"] == "success"
        expect(page.locator("#edit-cover-url")).to_have_value("")
        expect(page.locator("#edit-cover-url")).to_be_enabled()
        expect(page.get_by_test_id("edit-cover-url-submit")).to_be_enabled()
        expect(page.get_by_test_id("save-btn")).to_be_enabled()
        assert form.get_attribute("aria-busy") == busy_before
        assert _successful_artwork_fields(page) == {"url": "", "choice": CHOICE_URL,
            "isbn": EDITION_ISBN, "language": "en", "file": ""}
        expect(choice).to_have_attribute("aria-pressed", "true")
        _assert_reactive_preview(page, CHOICE_URL)
        _assert_dirty_controls(page)
        assert posts == [f"{base}/api/items/{target}/cover-url"], "releasing A must not auto-save B"
        assert session["downloads"] == [] and len(session["publications"]) == 1
        assert session["publications"][0][:2] == (target, session["manual_calls"][0][0])
        with get_db() as db:
            item_after_a = dict(db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone())
        for field in item_before.keys() - {"cover_path", "cover_edition_isbn", "cover_edition_language", "updated_at"}:
            assert item_after_a[field] == item_before[field], field
        assert (session["covers_dir"] / item_after_a["cover_path"].split("/")[-1]).read_bytes() == session["manual_bytes"]
        after_a = _catalogue_snapshot()
        for table in before.keys() - {"items"}:
            assert after_a[table] == before[table], table

        with page.expect_response(lambda response: response.request.method == "POST" and
                                 response.url == f"{base}/api/items/{target}") as saved:
            page.get_by_test_id("save-btn").click()  # One NEW explicit Save, only after A's real response.
        assert saved.value.status == 303, "pending B must really Save, not hit mixed-source400 or an unrelated error"
        page.wait_for_url(re.compile(rf"^{re.escape(base)}/item/{target}(?:\?.*)?$"))
        assert posts == [f"{base}/api/items/{target}/cover-url", f"{base}/api/items/{target}"]
        assert len(session["manual_calls"]) == 1 and len(session["publications"]) == 2
        assert session["downloads"] == [("GET", CHOICE_URL, {"follow_redirects": True, "retry_timeouts": True})]
        assert session["publications"][1][0] == target
        assert session["publications"][1][1] < 0
        assert session["publications"][1][1] != session["publications"][0][1]
        with get_db() as db:
            item = dict(db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone())
            mine = dict(db.execute("SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?",
                (session["admin"]["id"], target)).fetchone())
            other = dict(db.execute("SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?",
                (session["other"]["id"], target)).fetchone())
        assert (item["title"], item["manual_value"]) == ("Dirty title stays in this editor", 23.45)
        assert (item["isbn"], item["language"]) == (ORIGINAL_ISBN, "hu")
        assert (item["cover_edition_isbn"], item["cover_edition_language"]) == (EDITION_ISBN, "en")
        assert (session["covers_dir"] / item["cover_path"].split("/")[-1]).read_bytes() == session["image_bytes"]
        for _, _, relative in session["publications"]:
            assert not (session["covers_dir"] / relative.split("/")[-1]).exists(), "real pending stages must be cleaned"
        assert mine["reading_status"] == "reading"
        assert mine["rating"] == mine_before["rating"] == 4
        assert mine["personal_notes"] == mine_before["personal_notes"] == "Keep my private rating"
        assert other["rating"] == 1 and other["personal_notes"] == "Never overwrite another actor"
        assert other == other_before
        assert_page_clean(page)
    finally:
        try:
            hold.release()  # Never strand the real server coroutine, even if an assertion above fails.
        finally:
            context.close()
