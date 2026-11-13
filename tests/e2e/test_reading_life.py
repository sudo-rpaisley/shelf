"""Native journal forms in a real browser, including HTML textarea parsing.

The temporary loopback ASGI server uses the real application and middleware.
This branch has no central reading-life registration yet, so the fixture adds
only that router when missing; integrated trees exercise their own wiring.
"""

import threading
from urllib.parse import urlsplit

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.conftest import _create_user, _insert_item
from tests.e2e.conftest import _free_port, _wait_for_server, assert_page_clean, attach_page_guard


pytestmark = pytest.mark.e2e


@pytest.fixture
def reading_life_server(monkeypatch, _isolated_db):
    for name in ("RATE_LIMIT", "COVER_ENRICH", "PRICE_ALERTS", "RELEASE_RADAR"):
        monkeypatch.setenv(f"SHELF_DISABLE_{name}", "1")

    from app.main import app
    from app.routers import reading_life

    if not any(getattr(route, "path", "") == "/reading-life" for route in app.routes):
        monkeypatch.setattr(app.router, "routes", list(app.router.routes))
        app.include_router(reading_life.router)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, loop="asyncio", log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(f"{base_url}/health")
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "the disposable reading-life server did not stop"


def _submit(page, button):
    with page.expect_navigation(wait_until="domcontentloaded") as navigation:
        button.click()
    assert navigation.value.status == 200


@pytest.mark.parametrize("leading_newlines", [0, 1, 2])
def test_untouched_native_journal_save_keeps_leading_newlines_without_a_revision(
    reading_life_server, browser, db, leading_newlines,
):
    user = _create_user("journal-reader", "reader-password-726", "Journal Reader", "viewer")
    item_id = _insert_item(db, isbn=None, title="Native journal round trip")
    db.commit()
    body = "\n" * leading_newlines + "First reflection\nSecond line with trailing spaces  "
    base_url = reading_life_server
    context = browser.new_context()
    context.add_init_script(
        "localStorage.setItem('shelf-welcome-dismissed', '1');"
        "localStorage.setItem('shelf-shortcuts-seen', '1');"
    )
    external_requests = []

    def local_only(route):
        if urlsplit(route.request.url).hostname == "127.0.0.1":
            route.continue_()
        else:
            external_requests.append(route.request.url)
            route.abort()

    context.route("**/*", local_only)
    page = attach_page_guard(context.new_page())
    try:
        page.goto(f"{base_url}/login")
        page.get_by_label("Username", exact=True).fill(user["username"])
        page.get_by_label("Password", exact=True).fill("reader-password-726")
        _submit(page, page.get_by_role("button", name="Sign In", exact=True))
        response = page.goto(f"{base_url}/reading-life/items/{item_id}")
        assert response.status == 200
        expect(page.get_by_label("Private journal entry", exact=True)).to_have_value("")
        page.get_by_label("Private journal entry", exact=True).fill(body)
        _submit(page, page.get_by_role("button", name="Add journal entry", exact=True))
        entry_id = int(urlsplit(page.url).path.rsplit("/", 1)[1])
        before = dict(db.execute("SELECT * FROM reading_journal WHERE id = ? AND user_id = ?", (entry_id, user["id"])).fetchone())
        # Native form submission uses CRLF on the wire. The DOM exposes LF; both
        # representations must round-trip unchanged, without editing/refilling.
        assert before["body"].replace("\r\n", "\n") == body
        assert before["version"] == 1
        rendered_value = page.get_by_label("Private journal entry", exact=True).input_value()
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and urlsplit(response.url).path == f"/api/reading-life/journal/{entry_id}") as saved:
            _submit(page, page.get_by_role("button", name="Save journal correction", exact=True))
        assert saved.value.status == 303
        after = dict(db.execute("SELECT * FROM reading_journal WHERE id = ?", (entry_id,)).fetchone())
        assert after == before, "an untouched native save changed the journal body or created a revision"
        assert rendered_value == body
        expect(page.get_by_label("Private journal entry", exact=True)).to_have_value(body)
        assert external_requests == []
        assert_page_clean(page)
    finally:
        context.close()
