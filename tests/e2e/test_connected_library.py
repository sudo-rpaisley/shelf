"""Offline connected-library journeys against a fresh Standard install each time.

Use real forms, HTMX swaps and browser navigation. The OPDS server is loopback
only and records every request, including accidental acquisition downloads.
"""

import base64
import http.server
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    ADMIN_DISPLAY,
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    assert_page_clean,
    attach_page_guard,
)

pytestmark = pytest.mark.e2e


@contextmanager
def _browser_page(browser, **context_options):
    context = browser.new_context(**context_options)
    # G52: first-visit/welcome preferences precede setup or login navigation.
    context.add_init_script(
        "localStorage.setItem('shelf-welcome-dismissed', '1');"
        "localStorage.setItem('shelf-shortcuts-seen', '1');"
    )
    external_requests = []

    def local_only(route):
        if urlsplit(route.request.url).hostname in {"127.0.0.1", "localhost"}:
            route.continue_()
        else:
            external_requests.append(route.request.url)
            route.abort()

    context.route("**/*", local_only)
    page = attach_page_guard(context.new_page())
    try:
        yield page
        assert_page_clean(page)
        assert external_requests == [], "A connected-library journey attempted external I/O"
    finally:
        context.close()


def _navigate(page, control):
    """Arm the document wait before a normal form submission or link click."""
    with page.expect_navigation(wait_until="domcontentloaded") as navigation:
        control.click()
    assert navigation.value.status == 200


def _login(page, server, username, password):
    page.goto(f"{server['url']}/login")
    page.locator('input[name="username"]').fill(username)
    page.locator('input[name="password"]').fill(password)
    _navigate(page, page.get_by_role("button", name="Sign In", exact=True))
    expect(page).to_have_url(f"{server['url']}/")


@pytest.fixture
def connected_library(server_factory, browser):
    """No session-scoped catalogue or configuration can leak into this file."""
    server = server_factory()
    with _browser_page(browser) as page:
        page.goto(f"{server['url']}/setup")
        page.locator('input[name="username"]').fill(ADMIN_USERNAME)
        page.locator('input[name="display_name"]').fill(ADMIN_DISPLAY)
        page.locator('input[name="password"]').fill(ADMIN_PASSWORD)
        page.locator('input[name="password_confirm"]').fill(ADMIN_PASSWORD)
        page.locator('input[name="profile"][value="standard"]').check()
        _navigate(page, page.locator('button[type="submit"]'))
        expect(page).to_have_url(f"{server['url']}/")
        yield {**server, "page": page}


@contextmanager
def _db(server):
    connection = sqlite3.connect(server["data_dir"] / "shelf.db", timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _item(db, title, media_type="book", **values):
    fields = {
        "title": title, "media_type": media_type, "authors": "Élodie Vale",
        "source": "manual", **values,
    }
    return db.execute(
        f"INSERT INTO items ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
        list(fields.values()),
    ).lastrowid


def _link(db, first, second, kind="format"):
    db.execute(
        "INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, ?)",
        (*sorted((first, second)), kind),
    )


def _copy(db, item_id, location_id):
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, location_id, is_primary) "
        "VALUES (?, 1, ?, 1)", (item_id, location_id),
    )


def _cached_source(db, root):
    return db.execute(
        "INSERT INTO source_instances (provider, name, base_url, last_status) "
        "VALUES ('links', 'Reading room digital library', ?, 'never')", (root,),
    ).lastrowid


def _holding(db, source_id, item_id, url, media_type="ebook", state="available"):
    db.execute(
        "INSERT INTO source_records "
        "(source_id, item_id, external_id, title, media_type, open_url, availability, last_seen_at) "
        "SELECT ?, id, ?, title, ?, ?, ?, datetime('now') FROM items WHERE id = ?",
        (source_id, f"edition:{item_id}", media_type, url, state, item_id),
    )


def _add_user(page, server, username, password, role="viewer"):
    page.goto(f"{server['url']}/settings")
    page.get_by_test_id("tab-users").click()
    page.get_by_placeholder("Username", exact=True).fill(username)
    page.get_by_placeholder("Password (min 8 chars)", exact=True).fill(password)
    page.locator('select[x-model="newRole"]').select_option(role)
    with page.expect_response(
        lambda response: urlsplit(response.url).path == "/api/users"
        and response.request.method == "POST"
    ) as created:
        page.get_by_role("button", name="Add User", exact=True).click()
    assert created.value.ok
    assert created.value.json()["ok"] is True
    expect(page.get_by_text(f"@{username}", exact=True)).to_be_visible()


def _counts(page, matching, groups):
    counts = page.get_by_test_id("library-counts")
    expect(counts).to_have_attribute("data-matching-count", str(matching))
    expect(counts).to_have_attribute("data-group-count", str(groups))


def _open_details(details):
    expect(details).to_be_attached()
    if details.get_attribute("open") is None:
        details.locator(":scope > summary").click()
    expect(details).to_have_attribute("open", "")


def _choice(form, label):
    # Some labels wrap their select: its option text can join the name.
    return form.get_by_role("combobox", name=re.compile(rf"^{re.escape(label)}\b"))


def _seed_personal_state(server, title):
    with _db(server) as db:
        item = _item(db, title)
        user = db.execute("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)).fetchone()[0]
        db.execute(
            "INSERT INTO user_item_state (user_id, item_id, status, progress, on_list, notes) "
            "VALUES (?, ?, 'in_progress', 10, 1, 'Start at the first chapter.')",
            (user, item),
        )
    return user, item


def _personal_state(server, user, item):
    with _db(server) as db:
        row = db.execute(
            "SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?", (user, item),
        ).fetchone()
    return dict(row) if row else None


def _personal_controls(section, *, disabled):
    controls = section.locator("input, select, textarea, button")
    assert controls.count() >= 8, "The personal form's controls must not disappear while busy"
    for control in controls.all():
        if disabled:
            expect(control).to_be_disabled()
        else:
            expect(control).to_be_enabled()


def _draft_personal_change(form):
    form.get_by_label("My status", exact=True).select_option("paused")
    form.get_by_label("Progress", exact=False).fill("60")
    form.get_by_label("My notes", exact=True).fill("Resume after the cliffside chapter.")
    form.get_by_label("My List / Up Next", exact=True).check()


@pytest.fixture
def opds_feed():
    """An authenticated two-page feed; acquisitions must never be fetched."""
    state = {
        "title": "The Time Machine",
        "username": "local-opds-reader",
        "password": "private-opds-password-726",
        "requests": [],
    }
    authorization = "Basic " + base64.b64encode(
        f"{state['username']}:{state['password']}".encode()
    ).decode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib handler contract
            self._reply()

        def do_POST(self):  # noqa: N802
            self._reply()

        def do_PUT(self):  # noqa: N802
            self._reply()

        def do_DELETE(self):  # noqa: N802
            self._reply()

        def _reply(self):
            state["requests"].append((self.command, self.path, self.headers.get("Authorization")))
            status, body = 404, b"Not a feed"
            if self.command != "GET":
                status, body = 405, b"Read only"
            elif self.headers.get("Authorization") != authorization:
                status, body = 401, b"Authentication required"
            elif self.path == "/opds/catalog.xml":
                status = 200
                body = (
                    '<feed xmlns="http://www.w3.org/2005/Atom" '
                    'xmlns:dc="http://purl.org/dc/terms/">'
                    '<id>urn:shelf:e2e:catalogue</id><title>Reading room</title>'
                    '<link rel="next" href="page-2.xml" type="application/atom+xml"/>'
                    '<entry><id>urn:shelf:e2e:time-machine</id>'
                    f'<title>{escape(state["title"])}</title>'
                    '<author><name>H. G. Wells</name></author>'
                    '<dc:identifier>urn:isbn:9780141439976</dc:identifier>'
                    '<summary>A traveller visits the distant future.</summary>'
                    '<link rel="http://opds-spec.org/acquisition/open-access" '
                    'href="/files/unsafe.epub?token=must-not-reach-html" type="application/epub+zip"/>'
                    '<link rel="http://opds-spec.org/acquisition/open-access" '
                    'href="/files/time-machine.epub" type="application/epub+zip"/>'
                    '</entry></feed>'
                ).encode()
            elif self.path == "/opds/page-2.xml":
                status = 200
                body = b'<feed xmlns="http://www.w3.org/2005/Atom"><id>urn:shelf:e2e:last</id><title>End</title></feed>'
            self.send_response(status)
            self.send_header("Content-Type", "application/atom+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield {**state, "root": root, "url": f"{root}/opds/catalog.xml", "state": state}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_library_search_stacks_formats_but_not_adaptations_or_trash_bridges(connected_library):
    server, page = connected_library, connected_library["page"]
    with _db(server) as db:
        book = _item(db, "Northbound print")
        ebook = _item(db, "Northbound digital edition", "ebook")
        film = _item(db, "Northbound film", "dvd", authors="Avery Stone")
        bridge = _item(db, "Northbound discarded edition", "ebook", deleted_at="2026-11-01 10:00:00")
        audio = _item(db, "Northbound audio beyond Trash", "audiobook")
        _item(db, "Southbound field notes")
        _link(db, book, ebook)
        _link(db, ebook, film, "adaptation")
        _link(db, ebook, bridge)
        _link(db, bridge, audio)

    page.goto(f"{server['url']}/library")
    filters = page.get_by_test_id("library-filters")
    filters.get_by_label("Search your library", exact=True).fill("Northbound print")
    _navigate(page, filters.get_by_role("button", name="Search", exact=True))
    _counts(page, 1, 1)
    results = page.get_by_test_id("library-results")
    group = results.locator(f'[data-library-group="{book}"]')
    expect(group).to_have_attribute("data-member-ids", f"{book},{ebook}")
    expect(group).to_have_attribute("data-matching-ids", str(book))
    group.locator("summary").click()
    expect(group.get_by_role("link", name="Northbound digital edition", exact=True)).to_be_visible()
    expect(group.locator(f'[data-library-member="{ebook}"]')).to_contain_text(
        "Other format — does not match filters"
    )
    for excluded in (film, bridge, audio):
        expect(results.locator(f'[data-library-member="{excluded}"]')).to_have_count(0)

    filters.get_by_label("Search your library", exact=True).fill("Northbound")
    _navigate(page, filters.get_by_role("button", name="Search", exact=True))
    _counts(page, 4, 3)
    expect(results.locator(f'[data-library-group="{film}"]')).to_have_attribute("data-member-ids", str(film))
    expect(results.locator(f'[data-library-group="{audio}"]')).to_have_attribute("data-member-ids", str(audio))
    expect(results.locator(f'[data-library-member="{bridge}"]')).to_have_count(0)
    results.locator(f'[data-library-group="{book}"] summary').click()
    _navigate(page, results.get_by_role("link", name="Northbound print", exact=True))
    expect(page).to_have_url(f"{server['url']}/item/{book}?from=library")
    expect(page.get_by_role("link", name="Back to library", exact=False)).to_have_attribute("href", "/library")


def test_filters_match_one_member_and_one_holding_before_group_pagination(connected_library, opds_feed):
    server, page = connected_library, connected_library["page"]
    pairs = []
    with _db(server) as db:
        location = db.execute("INSERT INTO locations (name) VALUES ('Reading room shelf')").lastrowid
        source = _cached_source(db, opds_feed["root"])
        for number in range(1, 26):
            book = _item(db, f"Lantern route {number:02d} — print")
            ebook = _item(db, f"Lantern route {number:02d} — digital", "ebook", owned=0)
            pairs.append((book, ebook))
            _copy(db, book, location)
            _holding(db, source, ebook, f"{opds_feed['root']}/reader/{ebook}")
            _link(db, book, ebook)
        # The first print item has an available physical copy but an unavailable
        # digital holding. Source + availability must not borrow different rows.
        _holding(db, source, pairs[0][0], f"{opds_feed['root']}/reader/print", state="unavailable")

    page.goto(f"{server['url']}/library")
    filters = page.get_by_test_id("library-filters")
    filters.get_by_label("Search your library", exact=True).fill("Lantern route")
    _open_details(page.get_by_test_id("library-filter-options"))
    _choice(filters, "Format").select_option("ebook")
    _choice(filters, "Holding source").select_option("physical")
    _navigate(page, filters.get_by_role("button", name="Apply filters", exact=True))
    _counts(page, 0, 0)
    expect(page.get_by_role("heading", name="No matching results", exact=True)).to_be_visible()

    _open_details(page.get_by_test_id("library-filter-options"))
    _choice(filters, "Format").select_option("book")
    _choice(filters, "Holding source").select_option(f"source:{source}")
    _choice(filters, "Availability").select_option("available")
    _navigate(page, filters.get_by_role("button", name="Apply filters", exact=True))
    _counts(page, 0, 0)

    _open_details(page.get_by_test_id("library-filter-options"))
    _choice(filters, "Holding source").select_option("physical")
    _choice(filters, "Availability").select_option("physical")
    _choice(filters, "Catalogue ownership").select_option("owned")
    _navigate(page, filters.get_by_role("button", name="Apply filters", exact=True))
    _counts(page, 25, 25)
    results = page.get_by_test_id("library-results")
    expect(results.locator("[data-library-group]")).to_have_count(24)
    expect(results.locator("[data-library-member]")).to_have_count(48)
    first = results.locator(f'[data-library-group="{pairs[0][0]}"]')
    expect(first).to_have_attribute("data-member-ids", ",".join(map(str, pairs[0])))
    expect(first).to_have_attribute("data-matching-ids", str(pairs[0][0]))
    _navigate(page, page.get_by_test_id("library-pagination").get_by_role("link", name="Next", exact=True))
    assert parse_qs(urlsplit(page.url).query)["page"] == ["2"]
    _counts(page, 25, 25)
    expect(results.locator("[data-library-group]")).to_have_count(1)
    expect(results.locator("[data-library-group]")).to_have_attribute(
        "data-member-ids", ",".join(map(str, pairs[-1]))
    )
    _open_details(page.get_by_test_id("library-filter-options"))
    expect(_choice(filters, "Holding source")).to_have_value("physical")
    expect(_choice(filters, "Availability")).to_have_value("physical")
    expect(_choice(filters, "Format")).to_have_value("book")
    assert opds_feed["requests"] == []


def test_available_here_refreshes_after_editor_links_and_unlinks_without_reload(
    connected_library, browser, opds_feed
):
    server = connected_library
    username, password = "format-editor", "editor-password-726"
    _add_user(server["page"], server, username, password, "editor")
    with _db(server) as db:
        location = db.execute("INSERT INTO locations (name) VALUES ('Study alcove, upper shelf')").lastrowid
        book = _item(db, "The Harbour Atlas — print", location_id=location)
        audio = _item(db, "The Harbour Atlas — narrated", "audiobook", owned=0)
        _copy(db, book, location)
        source = _cached_source(db, opds_feed["root"])
        listen_url = f"{opds_feed['root']}/listen/harbour-atlas"
        _holding(db, source, audio, listen_url, "audiobook")

    with _browser_page(browser) as page:
        _login(page, server, username, password)
        page.goto(f"{server['url']}/item/{book}?from=library")
        available = page.get_by_test_id("available-here")
        expect(available).to_contain_text("Study alcove, upper shelf")
        expect(available.get_by_role("link", name="Listen", exact=True)).to_have_count(0)
        navigations = []
        page.on("request", lambda request: navigations.append(request.url) if request.is_navigation_request() else None)
        panel = page.locator("#related-media-panel")
        panel.get_by_text("Link another catalogue item", exact=True).click()
        search = panel.get_by_role("searchbox")
        with page.expect_response(
            lambda response: urlsplit(response.url).path == f"/api/related-media/items/{book}/search"
            and response.ok
        ):
            search.fill("The Harbour Atlas — narrated")
            search.press("End")  # HTMX listens for keyup, not only input.
        form = panel.locator(f'form:has(input[name="other_item_id"][value="{audio}"])')
        form.locator('select[name="link_type"]').select_option("format")
        with page.expect_response(
            lambda response: response.request.method == "POST"
            and urlsplit(response.url).path == f"/api/related-media/items/{book}/links"
            and response.ok
        ):
            form.get_by_role("button", name="Link", exact=True).click()
        expect(available).to_have_count(1)
        expect(available).to_contain_text("Study alcove, upper shelf")
        remote = available.locator(f'[data-availability-item="{audio}"]')
        expect(remote).to_contain_text("Reading room digital library")
        expect(remote).to_have_attribute("data-availability-state", "available")
        expect(remote.get_by_role("link", name="Listen", exact=True)).to_have_attribute("href", listen_url)
        expect(panel.get_by_test_id("available-here")).to_have_count(0)

        with page.expect_response(
            lambda response: response.request.method == "DELETE"
            and urlsplit(response.url).path == f"/api/related-media/items/{book}/links/{audio}"
            and response.ok
        ):
            panel.get_by_role("button", name="Unlink", exact=True).click()
        expect(remote).to_have_count(0)
        expect(available).to_contain_text("Study alcove, upper shelf")
        expect(available.get_by_role("link", name="Listen", exact=True)).to_have_count(0)
        assert navigations == []
        assert opds_feed["requests"] == []


def test_personal_activity_drives_continue_and_up_next_without_changing_shared_state(
    connected_library, browser
):
    server, admin = connected_library, connected_library["page"]
    username, password = "reading-viewer", "viewer-password-726"
    _add_user(admin, server, username, password)
    with _db(server) as db:
        item = _item(
            db, "A Garden Beyond Winter", owned=0, reading_status="read",
            date_finished="2025-12-31", notes="Shared catalogue notes stay unchanged.",
        )
        db.execute(
            "INSERT INTO list_items (list_id, item_id) SELECT id, ? FROM lists WHERE slug = 'wishlist'",
            (item,),
        )
        before = dict(db.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone())

    with _browser_page(browser) as page:
        _login(page, server, username, password)
        page.goto(f"{server['url']}/item/{item}")
        personal = page.get_by_test_id("personal-library")
        editor = page.get_by_test_id("personal-library-editor")
        expect(editor).not_to_have_attribute("open", "")
        _open_details(editor)
        form = page.get_by_test_id("personal-library-form")
        form.get_by_label("My status", exact=True).select_option("in_progress")
        form.get_by_label("Progress", exact=False).fill("35")
        form.get_by_label("My rating (1–5)", exact=True).fill("4")
        form.get_by_label("Favourite", exact=True).check()
        form.get_by_label("My List / Up Next", exact=True).check()
        form.get_by_label("My notes", exact=True).fill("Private: resume at the greenhouse chapter.")
        with page.expect_response(
            lambda response: response.request.method == "POST"
            and urlsplit(response.url).path == f"/api/library/items/{item}/personal"
            and response.ok
        ):
            form.get_by_role("button", name="Save my activity", exact=True).click()
        expect(editor).to_have_attribute("open", "")
        expect(personal.get_by_role("button", name="Reset my activity for this item", exact=True)).to_be_visible()
        page.reload()
        expect(editor).not_to_have_attribute("open", "")
        _open_details(editor)
        expect(form.get_by_label("My status", exact=True)).to_have_value("in_progress")
        expect(form.get_by_label("Progress", exact=False)).to_have_value("35")
        expect(form.get_by_label("My notes", exact=True)).to_have_value("Private: resume at the greenhouse chapter.")
        _navigate(page, personal.get_by_role("link", name="All my activity", exact=True))
        for section in ("activity-continue", "activity-up-next", "activity-favourites"):
            expect(page.get_by_test_id(section).get_by_role("link", name="A Garden Beyond Winter", exact=False)).to_be_visible()
        expect(page.get_by_test_id("activity-continue")).to_contain_text("35%")

        admin.goto(f"{server['url']}/activity")
        expect(admin.get_by_test_id("activity-continue").get_by_role("link")).to_have_count(0)
        expect(admin.get_by_test_id("activity-up-next").get_by_role("link")).to_have_count(0)
        expect(admin.get_by_test_id("activity-page")).not_to_contain_text("greenhouse chapter")
        with _db(server) as db:
            assert dict(db.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone()) == before
            assert db.execute("SELECT COUNT(*) FROM list_items WHERE item_id = ?", (item,)).fetchone()[0] == 1

        _navigate(page, page.get_by_test_id("activity-continue").get_by_role("link"))
        expect(editor).not_to_have_attribute("open", "")
        _open_details(editor)
        with page.expect_response(
            lambda response: response.request.method == "POST"
            and urlsplit(response.url).path == f"/api/library/items/{item}/personal/reset"
            and response.ok
        ):
            personal.get_by_role("button", name="Reset my activity for this item", exact=True).click()
        expect(editor).to_have_attribute("open", "")
        expect(form.get_by_label("My status", exact=True)).to_have_value("")
        expect(form.get_by_label("Progress", exact=False)).to_have_value("")
        expect(form.get_by_label("My List / Up Next", exact=True)).not_to_be_checked()
        expect(form.get_by_label("My notes", exact=True)).to_have_value("")
        personal.get_by_text("My recent history", exact=True).click()
        expect(page.get_by_test_id("personal-library-history")).to_contain_text("Reset — previous state")
        expect(page.get_by_test_id("personal-library-history")).to_contain_text("greenhouse chapter")
        _navigate(page, personal.get_by_role("link", name="All my activity", exact=True))
        for section in ("activity-continue", "activity-up-next", "activity-favourites"):
            expect(page.get_by_test_id(section).get_by_role("link")).to_have_count(0)
        with _db(server) as db:
            assert dict(db.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone()) == before
            assert db.execute("SELECT COUNT(*) FROM user_item_state WHERE item_id = ?", (item,)).fetchone()[0] == 0


@pytest.mark.parametrize("first_action", ["save", "reset"])
def test_personal_actions_disable_both_forms_until_the_held_response_finishes(
    connected_library, first_action
):
    """Save/Reset are single-flight, not a queue attached to a discarded node."""
    server, page = connected_library, connected_library["page"]
    user, item = _seed_personal_state(server, f"An Unhurried Journey — {first_action}")
    page.goto(f"{server['url']}/item/{item}")
    personal = page.get_by_test_id("personal-library")
    editor = page.get_by_test_id("personal-library-editor")
    expect(editor).not_to_have_attribute("open", "")
    _open_details(editor)
    form = page.get_by_test_id("personal-library-form")
    _draft_personal_change(form)
    save = personal.get_by_role("button", name="Save my activity", exact=True)
    reset = personal.get_by_role("button", name="Reset my activity for this item", exact=True)
    expect(save).to_be_enabled()
    expect(reset).to_be_enabled()
    paths = {"save": f"/api/library/items/{item}/personal", "reset": f"/api/library/items/{item}/personal/reset"}
    opposite = "reset" if first_action == "save" else "save"
    held, requests = [], []
    pattern = re.compile(rf"{re.escape(server['url'])}/api/library/items/{item}/personal(?:/reset)?$")

    def hold(route):
        held.append(route)

    def record(request):
        if request.method == "POST" and urlsplit(request.url).path in paths.values():
            requests.append(urlsplit(request.url).path)

    page.route(pattern, hold)
    page.on("request", record)
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and urlsplit(response.url).path == paths[first_action]
    ) as completed:
        with page.expect_request(
            lambda request: request.method == "POST" and urlsplit(request.url).path == paths[first_action]
        ):
            (save if first_action == "save" else reset).click()
        expect(save).to_be_disabled()
        expect(reset).to_be_disabled()
        _personal_controls(personal, disabled=True)
        assert len(held) == 1
        assert requests == [paths[first_action]]
        assert _personal_state(server, user, item)["progress"] == 10, "The parked request ran too early"

        # A real pointer click on the disabled opposite action must not submit
        # or leave a request queued against the soon-to-be-replaced section.
        blocked = reset if first_action == "save" else save
        blocked.scroll_into_view_if_needed()
        box = blocked.bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_timeout(100)
        assert requests == [paths[first_action]]
        assert len(held) == 1
        route = held[0]
        route.fulfill(response=route.fetch())
    assert completed.value.status == 200
    expect(editor).to_have_attribute("open", "")
    _personal_controls(personal, disabled=False)
    page.unroute(pattern, hold)
    if first_action == "save":
        expect(form.get_by_label("Progress", exact=False)).to_have_value("60")
        state = _personal_state(server, user, item)
        assert (state["status"], state["progress"], state["notes"]) == (
            "paused", 60, "Resume after the cliffside chapter.",
        )
        expect(reset).to_be_enabled()
    else:
        expect(form.get_by_label("My status", exact=True)).to_have_value("")
        expect(form.get_by_label("Progress", exact=False)).to_have_value("")
        expect(reset).to_have_count(0)
        assert _personal_state(server, user, item) is None
        _draft_personal_change(form)

    with page.expect_response(
        lambda response: response.request.method == "POST"
        and urlsplit(response.url).path == paths[opposite]
    ) as second:
        (reset if opposite == "reset" else save).click()
    assert second.value.status == 200
    expect(editor).to_have_attribute("open", "")
    _personal_controls(personal, disabled=False)
    expect(form.get_by_label("Progress", exact=False)).to_have_value("" if opposite == "reset" else "60")
    assert requests == [paths[first_action], paths[opposite]]
    state = _personal_state(server, user, item)
    if opposite == "reset":
        assert state is None
    else:
        assert (state["status"], state["progress"], state["on_list"], state["notes"]) == (
            "paused", 60, 1, "Resume after the cliffside chapter.",
        )
    with _db(server) as db:
        events = [row[0] for row in db.execute(
            "SELECT event_type FROM user_item_events WHERE user_id = ? AND item_id = ? ORDER BY id",
            (user, item),
        )]
        assert events == (["updated", "reset"] if first_action == "save" else ["reset", "updated"])
    page.reload()
    expect(editor).not_to_have_attribute("open", "")
    _open_details(editor)
    expect(form.get_by_label("Progress", exact=False)).to_have_value("" if opposite == "reset" else "60")


def test_personal_validation_error_reenables_both_actions_and_allows_retry(connected_library):
    server, page = connected_library, connected_library["page"]
    user, item = _seed_personal_state(server, "A Journey With a Corrected Note")
    page.goto(f"{server['url']}/item/{item}")
    personal = page.get_by_test_id("personal-library")
    editor = page.get_by_test_id("personal-library-editor")
    _open_details(editor)
    form = page.get_by_test_id("personal-library-form")
    # The browser accepts this pasted control character; the server's private
    # notes validation rejects it without swapping away the unsaved form.
    form.get_by_label("My notes", exact=True).fill("A pasted note with a null\x00character")
    held = []

    def hold(route):
        held.append(route)

    url = f"{server['url']}/api/library/items/{item}/personal"
    page.route(url, hold)
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as rejected:
        with page.expect_request(lambda request: request.url == url and request.method == "POST"):
            form.get_by_role("button", name="Save my activity", exact=True).click()
        _personal_controls(personal, disabled=True)
        assert len(held) == 1
        route = held[0]
        route.fulfill(response=route.fetch())
    assert rejected.value.status == 422
    expect(editor).to_have_attribute("open", "")
    _personal_controls(personal, disabled=False)
    expect(personal.get_by_role("button", name="Reset my activity for this item", exact=True)).to_be_enabled()
    expect(page.locator("#toast-container")).to_contain_text("Personal notes cannot contain null characters")
    assert _personal_state(server, user, item)["progress"] == 10
    page.unroute(url, hold)
    _draft_personal_change(form)
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as retried:
        form.get_by_role("button", name="Save my activity", exact=True).click()
    assert retried.value.status == 200
    _personal_controls(personal, disabled=False)
    expect(form.get_by_label("Progress", exact=False)).to_have_value("60")
    state = _personal_state(server, user, item)
    assert (state["progress"], state["notes"]) == (60, "Resume after the cliffside chapter.")


def test_admin_configures_local_opds_sync_and_preserves_local_edits_on_resync(connected_library, opds_feed):
    server, page = connected_library, connected_library["page"]
    page.goto(f"{server['url']}/sources")
    add = page.locator('form[action="/api/sources"]')
    _choice(add, "Provider").select_option("opds")
    add.get_by_label("Name", exact=True).fill("Reading room OPDS")
    add.get_by_label("Server or OPDS feed URL", exact=True).fill(opds_feed["url"])
    _navigate(page, add.get_by_role("button", name="Add source", exact=True))
    expect(page.get_by_role("heading", name="Reading room OPDS", exact=True)).to_be_visible()
    source_id = int(urlsplit(page.url).path.rsplit("/", 1)[1])
    configuration = page.get_by_test_id("source-configuration")
    expect(configuration).to_have_attribute("open", "")
    settings = page.locator(f'form[action="/api/sources/{source_id}/settings"]')
    settings.get_by_label("Username", exact=True).fill(opds_feed["username"])
    settings.get_by_label("Password", exact=True).fill(opds_feed["password"])
    settings.get_by_role("textbox", name=re.compile(r"^Options JSON\b")).fill(
        json.dumps({"media_type": "ebook", "max_pages": 4, "max_entries": 10})
    )
    _navigate(page, settings.get_by_role("button", name="Save settings", exact=True))
    expect(settings.get_by_label("Username", exact=True)).to_have_value("")
    expect(settings.get_by_label("Password", exact=True)).to_have_value("")
    expect(settings).to_contain_text("Credentials saved.")
    for secret in (opds_feed["username"], opds_feed["password"]):
        assert secret not in page.content()

    _navigate(page, page.get_by_role("button", name="Test connection", exact=True))
    expect(page.get_by_role("status")).to_contain_text("Connection successful.")
    with _db(server) as db:
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        assert db.execute("SELECT last_sync_at FROM source_instances WHERE id = ?", (source_id,)).fetchone()[0] is None
    _navigate(page, page.get_by_role("button", name="Sync now", exact=True))
    expect(page.get_by_role("status")).to_have_text("Sync complete.")
    report = page.get_by_role("heading", name="Status and report", exact=True).locator("..")
    expect(report).to_contain_text("Status: Complete")
    expect(configuration).not_to_have_attribute("open", "")
    report_box, configuration_box = report.bounding_box(), configuration.bounding_box()
    # The report is first in reading order; a laptop places Configuration
    # beside it, while narrow screens stack the same two sections.
    if page.viewport_size["width"] >= 1280:
        assert abs(report_box["y"] - configuration_box["y"]) <= 1
        assert report_box["x"] + report_box["width"] < configuration_box["x"]
    else:
        assert report_box["y"] + report_box["height"] < configuration_box["y"]
    with _db(server) as db:
        source = dict(db.execute("SELECT * FROM source_instances WHERE id = ?", (source_id,)).fetchone())
        item = dict(db.execute("SELECT * FROM items").fetchone())
        assert source["last_status"] == "ok"
        assert source["last_sync_at"] and source["last_attempt_at"]
        assert source["last_error"] is None
        assert opds_feed["password"] not in source["credentials_enc"]
        assert item["owned"] == 0
        assert db.execute("SELECT COUNT(*) FROM list_items WHERE item_id = ?", (item["id"],)).fetchone()[0] == 0
    page.reload()
    expect(report).to_contain_text(source["last_sync_at"])
    expect(configuration).not_to_have_attribute("open", "")

    page.goto(f"{server['url']}/library")
    filters = page.get_by_test_id("library-filters")
    filters.get_by_label("Search your library", exact=True).fill("The Time Machine")
    _navigate(page, filters.get_by_role("button", name="Search", exact=True))
    _counts(page, 1, 1)
    member = page.get_by_test_id("library-results").locator(f'[data-library-member="{item["id"]}"]')
    read = member.get_by_role("link", name="Read · Reading room OPDS", exact=True)
    expect(read).to_have_attribute("href", f"{opds_feed['root']}/files/time-machine.epub")
    expect(read).to_have_attribute("target", "_blank")
    expect(read).to_have_attribute("rel", "noopener noreferrer")
    parsed = urlsplit(read.get_attribute("href"))
    assert parsed.username is None and parsed.password is None and not parsed.query
    assert "must-not-reach-html" not in page.content()
    for secret in (opds_feed["username"], opds_feed["password"]):
        assert secret not in page.content()
    _navigate(page, member.get_by_role("link", name="The Time Machine", exact=True))
    expect(page.get_by_test_id("available-here").get_by_role("link", name="Read", exact=True)).to_have_attribute(
        "href", f"{opds_feed['root']}/files/time-machine.epub"
    )
    _navigate(page, page.get_by_role("link", name="Edit", exact=True))
    edit = page.locator("#item-edit-form")
    edit.get_by_label("Title", exact=True).fill("The Time Machine — household edition")
    edit.get_by_label("Publisher", exact=True).fill("Local reading room press")
    _navigate(page, page.get_by_test_id("save-btn"))
    expect(page.get_by_role("heading", level=1)).to_have_text("The Time Machine — household edition")
    opds_feed["state"]["title"] = "The Time Machine — revised remote record"
    page.goto(f"{server['url']}/sources/{source_id}")
    _navigate(page, page.get_by_role("button", name="Sync now", exact=True))
    expect(page.get_by_role("status")).to_have_text("Sync complete.")
    with _db(server) as db:
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        local = db.execute("SELECT title, publisher, owned FROM items WHERE id = ?", (item["id"],)).fetchone()
        assert tuple(local) == ("The Time Machine — household edition", "Local reading room press", 0)
        record = db.execute("SELECT item_id, title, availability FROM source_records WHERE source_id = ?", (source_id,)).fetchone()
        assert tuple(record) == (item["id"], opds_feed["state"]["title"], "available")
    page.goto(f"{server['url']}/item/{item['id']}")
    expect(page.get_by_role("heading", level=1)).to_have_text("The Time Machine — household edition")
    expect(page.get_by_test_id("available-here").get_by_role("link", name="Read", exact=True)).to_be_visible()
    assert [request[:2] for request in opds_feed["requests"]] == [
        ("GET", "/opds/catalog.xml"), ("GET", "/opds/page-2.xml"),
    ] * 3
    assert all(request[2] and request[2].startswith("Basic ") for request in opds_feed["requests"])


def test_saved_view_create_reload_and_delete_is_private_to_its_owner(connected_library, browser):
    server, admin = connected_library, connected_library["page"]
    username, password = "saved-view-reader", "saved-view-password-726"
    _add_user(admin, server, username, password)
    with _db(server) as db:
        selected = _item(db, "Owl House digital", "ebook", owned=0)
        _item(db, "Owl House print")
        _item(db, "A Different Journey", "ebook", owned=0)
    with _browser_page(browser) as page:
        _login(page, server, username, password)
        page.goto(f"{server['url']}/library")
        filters = page.get_by_test_id("library-filters")
        filters.get_by_label("Search your library", exact=True).fill("Owl House")
        _open_details(page.get_by_test_id("library-filter-options"))
        _choice(filters, "Format").select_option("ebook")
        _choice(filters, "Catalogue ownership").select_option("neither")
        _navigate(page, filters.get_by_role("button", name="Apply filters", exact=True))
        _counts(page, 1, 1)
        saved = page.get_by_test_id("library-saved-views")
        saved.locator("summary").click()
        saved.get_by_label("View name", exact=True).fill("Quiet weekend reading")
        _navigate(page, saved.get_by_role("button", name="Save current view", exact=True))
        expect(page.get_by_role("status").filter(has_text="Personal view saved.")).to_be_visible()
        saved.locator("summary").click()
        link = saved.get_by_role("link", name="Quiet weekend reading", exact=True)
        saved_url = link.get_attribute("href")
        _navigate(page, link)
        page.reload()
        expect(filters.get_by_label("Search your library", exact=True)).to_have_value("Owl House")
        _open_details(page.get_by_test_id("library-filter-options"))
        expect(_choice(filters, "Format")).to_have_value("ebook")
        expect(_choice(filters, "Catalogue ownership")).to_have_value("neither")
        _counts(page, 1, 1)
        expect(page.get_by_test_id("library-results").locator("[data-library-member]")).to_have_attribute(
            "data-library-member", str(selected)
        )

        # A valid foreign saved-view ID must neither apply filters nor reveal its name.
        admin.goto(f"{server['url']}{saved_url}")
        expect(admin.get_by_test_id("library-filters").get_by_label("Search your library", exact=True)).to_have_value("")
        expect(admin.get_by_test_id("library-saved-views")).not_to_contain_text("Quiet weekend reading")
        _counts(admin, 3, 3)
        saved.locator("summary").click()
        _navigate(page, saved.get_by_role("button", name="Delete saved view: Quiet weekend reading", exact=True))
        expect(page.get_by_role("status").filter(has_text="Personal view deleted.")).to_be_visible()
        expect(saved.get_by_role("link", name="Quiet weekend reading", exact=True)).to_have_count(0)
        page.goto(f"{server['url']}{saved_url}")
        expect(filters.get_by_label("Search your library", exact=True)).to_have_value("")
        _counts(page, 3, 3)
        with _db(server) as db:
            assert db.execute("SELECT COUNT(*) FROM library_saved_views").fetchone()[0] == 0


def test_attention_requires_explicit_accept_or_dismiss_and_never_infers_a_film(connected_library):
    server, page = connected_library, connected_library["page"]
    with _db(server) as db:
        book = _item(db, "The Lantern Keeper", authors="Amelia Reed")
        audio = _item(db, "The Lantern Keeper", "audiobook", authors="Amelia Reed")
        film = _item(db, "The Lantern Keeper", "digital_video", authors="Amelia Reed")
        trashed = _item(db, "The Lantern Keeper", "ebook", authors="Amelia Reed", deleted_at="2026-11-01 10:00:00")
        other_book = _item(db, "Maps of Tomorrow", authors="Iris Finch")
        other_ebook = _item(db, "Maps of Tomorrow", "ebook", authors="Iris Finch")
        before = [tuple(row) for row in db.execute("SELECT id, title, authors, deleted_at FROM items ORDER BY id")]

    page.goto(f"{server['url']}/needs-attention?category=connections")
    suggestions = page.locator("#connection-suggestions")
    expect(suggestions.locator("[data-connection-pair]")).to_have_count(2)
    accepted = suggestions.locator(f'[data-connection-pair="{book},{audio}"]')
    dismissed = suggestions.locator(f'[data-connection-pair="{other_book},{other_ebook}"]')
    expect(accepted.locator("[data-connection-evidence]")).to_contain_text("shared named creator")
    for excluded in (film, trashed):
        expect(suggestions.locator(f'a[href="/item/{excluded}"]')).to_have_count(0)
    with _db(server) as db:
        assert db.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM connection_dismissals").fetchone()[0] == 0

    with page.expect_response(
        lambda response: urlsplit(response.url).path == "/api/library/connections/accept"
        and response.request.method == "POST" and response.ok
    ):
        accepted.get_by_role("button", name="Accept format connection", exact=True).click()
    expect(suggestions.locator('[data-connection-result="accepted"]')).to_contain_text("No records were merged")
    expect(accepted).to_have_count(0)
    expect(dismissed).to_be_visible()
    with page.expect_response(
        lambda response: urlsplit(response.url).path == "/api/library/connections/dismiss"
        and response.request.method == "POST" and response.ok
    ):
        dismissed.get_by_role("button", name="Not the same work", exact=True).click()
    expect(suggestions.locator('[data-connection-result="dismissed"]')).to_contain_text("Suggestion dismissed")
    expect(dismissed).to_have_count(0)
    page.reload()
    expect(suggestions.locator("[data-connection-pair]")).to_have_count(0)
    suggestions.locator("summary").click()
    dismissal = suggestions.locator(f'[data-dismissed-connection="{other_book},{other_ebook}"]')
    expect(dismissal.get_by_role("link", name="Maps of Tomorrow", exact=True)).to_have_count(2)
    with _db(server) as db:
        assert [tuple(row) for row in db.execute("SELECT item_a_id, item_b_id, link_type FROM item_links")] == [(book, audio, "format")]
        assert [tuple(row) for row in db.execute("SELECT item_a_id, item_b_id FROM connection_dismissals")] == [(other_book, other_ebook)]
        assert [tuple(row) for row in db.execute("SELECT id, title, authors, deleted_at FROM items ORDER BY id")] == before

    page.goto(f"{server['url']}/library")
    filters = page.get_by_test_id("library-filters")
    filters.get_by_label("Search your library", exact=True).fill("The Lantern Keeper")
    _navigate(page, filters.get_by_role("button", name="Search", exact=True))
    _counts(page, 3, 2)
    results = page.get_by_test_id("library-results")
    expect(results.locator(f'[data-library-group="{book}"]')).to_have_attribute("data-member-ids", f"{book},{audio}")
    expect(results.locator(f'[data-library-group="{film}"]')).to_have_attribute("data-member-ids", str(film))
    expect(results.locator(f'[data-library-member="{trashed}"]')).to_have_count(0)


def _assert_phone_fits(page):
    metrics = page.evaluate(
        """() => {
            window.scrollTo(0, window.scrollY);
            return {width: document.documentElement.clientWidth,
                    scroll: document.documentElement.scrollWidth};
        }"""
    )
    assert metrics["width"] == 375
    assert metrics["scroll"] <= metrics["width"], f"Horizontal overflow on {page.url}: {metrics}"


def test_short_phone_account_menu_and_square_music_artwork(connected_library):
    server, page = connected_library, connected_library["page"]
    csrf = next(cookie["value"] for cookie in page.context.cookies() if cookie["name"] == "csrf_token")
    response = page.request.post(
        f"{server['url']}/api/settings/profile", form={"profile": "everything"},
        headers={"X-CSRF-Token": csrf}, max_redirects=0,
    )
    assert response.status == 303
    # Deliberately portrait-shaped fixture artwork: a music thumbnail must keep
    # its square box even beside a much taller title/creator metadata column.
    (server["data_dir"] / "covers" / "square-artwork.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450">'
        '<rect width="300" height="450" fill="#3e6267"/></svg>'
    )
    with _db(server) as db:
        item_id = _item(
            db, "A Very Long Album Title for the Quietest Possible Autumn Evening", "vinyl",
            authors="A Long Musical Artist Credit", cover_path="covers/square-artwork.svg",
        )
    page.set_viewport_size({"width": 375, "height": 568})
    page.goto(f"{server['url']}/library")
    artwork = page.locator(f'[data-library-member="{item_id}"] img')
    expect(artwork).to_be_visible()
    expect(artwork).to_have_attribute("data-cover-shape", "square")
    box = artwork.bounding_box()
    assert box["width"] >= 48
    assert abs(box["width"] - box["height"]) <= 1, f"Music artwork stretched: {box}"
    page.get_by_test_id("account-menu-button").click()
    menu = page.get_by_test_id("account-menu-panel")
    expect(menu).to_be_visible()
    box = menu.bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= 568, f"Account menu exceeds short phone: {box}"
    scroll = menu.evaluate("element => ({client: element.clientHeight, scroll: element.scrollHeight})")
    assert scroll["scroll"] > scroll["client"], "Everything's account menu needs its own vertical scroll"
    logout = page.get_by_test_id("account-menu-logout")
    logout.scroll_into_view_if_needed()
    expect(logout).to_be_visible()
    box = logout.bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= 568, "Sign out is not reachable"
    page.keyboard.press("Escape")
    expect(menu).not_to_be_visible()
    _assert_phone_fits(page)


def test_phone_main_navigation_and_library_controls_fit_375_pixels(connected_library, browser):
    server = connected_library
    long_location = (
        "Home / Upstairs landing / Reading room window shelf / Tall archive cabinet / "
        "Upper compartment / Box of illustrated journeys"
    )
    book_title = "An Atlas of Quiet Places and Long Autumn Journeys"
    video_title = "Evening Journeys Through the Northern Islands"
    with _db(server) as db:
        location = db.execute("INSERT INTO locations (name) VALUES (?)", (long_location,)).lastrowid
        book = _item(db, book_title, location_id=location)
        _copy(db, book, location)
        video = _item(db, video_title, "digital_video", authors="Robin Clearwater", duration_mins=142)
    with _browser_page(browser, viewport={"width": 375, "height": 812}) as page:
        _login(page, server, ADMIN_USERNAME, ADMIN_PASSWORD)
        menu = page.get_by_test_id("nav-menu-panel")
        page.get_by_role("button", name="Main menu", exact=True).click()
        expect(menu).to_be_visible()
        expect(menu.get_by_role("link", name="Library", exact=True)).to_be_visible()
        _assert_phone_fits(page)
        _navigate(page, menu.get_by_role("link", name="Library", exact=True))
        expect(page.get_by_test_id("library-page")).to_be_visible()
        filters = page.get_by_test_id("library-filters")
        options = page.get_by_test_id("library-filter-options")
        expect(options).not_to_have_attribute("open", "")
        expect(options.locator("summary")).to_have_text("Filters and display")
        search = filters.get_by_label("Search your library", exact=True)
        expect(search).to_be_visible()
        assert search.bounding_box()["width"] >= 120, "The phone search field collapsed"
        expect(page.get_by_role("navigation", name="Library presets", exact=True)).to_be_visible()
        results = page.get_by_test_id("library-results")
        _counts(page, 2, 2)
        page.evaluate("window.scrollTo(0, 0)")
        assert results.bounding_box()["y"] < 900, "Closed filters push results below a phone's first screen"
        card_heading = results.locator(f'[data-library-member="{book}"]').get_by_role(
            "heading", name=book_title, exact=True,
        )
        expect(card_heading).to_be_visible()
        assert card_heading.bounding_box()["y"] < 900, "The first matching card is buried below the filters"
        _assert_phone_fits(page)
        search.fill("Quiet Places")
        _open_details(options)
        _choice(filters, "Format").select_option("book")
        _choice(filters, "Availability").select_option("physical")
        _choice(filters, "Holding source").select_option("physical")
        _navigate(page, filters.get_by_role("button", name="Apply filters", exact=True))
        _counts(page, 1, 1)
        expect(options).not_to_have_attribute("open", "")
        expect(results).to_contain_text(long_location)
        _assert_phone_fits(page)
        saved = page.get_by_test_id("library-saved-views")
        saved.locator("summary").click()
        expect(saved.get_by_label("View name", exact=True)).to_be_visible()
        _assert_phone_fits(page)
        page.get_by_test_id("account-menu-button").click()
        account = page.get_by_test_id("account-menu-panel")
        expect(account).to_be_visible()
        for key in ("activity", "sources", "needs-attention"):
            expect(account.get_by_test_id(f"account-menu-{key}")).to_be_visible()
        _assert_phone_fits(page)
        _navigate(page, account.get_by_test_id("account-menu-sources"))
        expect(page.get_by_role("heading", name="Connected libraries", exact=True)).to_be_visible()
        _assert_phone_fits(page)

        for item, title in ((book, book_title), (video, video_title)):
            page.goto(f"{server['url']}/item/{item}")
            expect(page.get_by_role("heading", level=1)).to_have_text(title)
            expect(page.get_by_test_id("personal-library-editor")).not_to_have_attribute("open", "")
            expect(page.get_by_test_id("catalogue-status-heading")).to_have_text("Shared catalogue status")
            if item == book:
                expect(page.get_by_test_id("available-here")).to_contain_text(long_location)
            _assert_phone_fits(page)
            copy_location = page.get_by_role("combobox", name="Location for the new copy", exact=True)
            expect(copy_location).to_be_visible()
            copy_location.scroll_into_view_if_needed()
            box = copy_location.bounding_box()
            assert box["width"] > 0 and box["x"] >= 0 and box["x"] + box["width"] <= 375, (
                f"The new-copy location selector leaves the phone viewport on item {item}: {box}"
            )
            _assert_phone_fits(page)

        page.get_by_title("Keyboard shortcuts (?)", exact=True).click()
        shortcuts = page.locator("#shortcut-modal")
        expect(shortcuts).to_be_visible()
        close = shortcuts.get_by_role("button", name="Close keyboard shortcuts", exact=True)
        expect(close).to_be_visible()
        _assert_phone_fits(page)
        close.click()
        expect(shortcuts).to_be_hidden()
