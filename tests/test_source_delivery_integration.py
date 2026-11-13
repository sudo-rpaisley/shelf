"""Production registration, native forms, exact handoffs and enabled lifespan.

Provider HTTP is mocked. Routers, permission dependencies, request middleware,
the scheduler loop and persistence are the application's ordinary entry points.
"""

import html
import re
import threading
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from starlette.responses import Response

from app.services import source_progress
from tests.conftest import _insert_item
from tests.test_availability import _item, _record, _source
from tests.test_library_delivery_integration import _request
from tests.test_source_progress import (  # noqa: F401 — isolated HTTP fixtures
    REMOTE_USER, TOKEN, _state, bound, remote,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("header_token", [False, True])
@pytest.mark.parametrize("path,limit", [
    ("/api/audiobooks/1/metadata/preview", 4096),
    ("/api/audiobooks/not-an-id/metadata/preview/", 4096),
    ("/api/audiobooks/1/metadata/apply", 130000),
    ("/api/audiobooks/not-an-id/metadata/apply/", 130000),
    ("/api/library/items/not-an-id/preferred-source/", 16384),
    ("/api/source-progress/accounts/link/", 16384),
    ("/api/source-progress/accounts/not-an-id/disconnect/", 16384),
    ("/api/source-progress/accounts/not-an-id/schedule/", 16384),
    ("/api/source-progress/accounts/not-an-id/items/also-invalid/pull/", 16384),
    ("/api/source-progress/reviews/not-an-id/", 16384),
])
async def test_review_bounds_precede_csrf_and_path_parsing(path, limit, header_token):
    from app.main import CSRFMiddleware

    request, calls = _request(path, [b"x" * limit, b"x", b"unread tail"], header_token=header_token)
    next_handler = AsyncMock()
    reply = await CSRFMiddleware(AsyncMock()).dispatch(request, next_handler)
    assert reply.status_code == 413
    assert calls == [0, 1]
    next_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,fields", [
    ("/api/audiobooks/1/metadata/preview", {"marketplace": "us"}),
    ("/api/source-progress/accounts/link", {"consent": "pull_v1", "remote_user_id": "my-user"}),
])
async def test_bounded_native_forms_are_replayed_intact(path, fields):
    from app.main import CSRFMiddleware

    supplied = {"_csrf": "token", **fields}
    request, calls = _request(path, [urlencode(supplied).encode()])

    async def next_handler(replayed):
        assert dict(await replayed.form()) == supplied
        return Response(status_code=204)

    assert (await CSRFMiddleware(AsyncMock()).dispatch(request, next_handler)).status_code == 204
    assert calls == [0]


@pytest.mark.parametrize("client_name,can_review", [("editor_client", True), ("viewer_client", False)])
def test_audiobook_review_is_reachable_without_spending_provider_requests(request, client_name, can_review, db, monkeypatch):
    from app.services import audible_catalog

    client = request.getfixturevalue(client_name)
    lookup = AsyncMock(side_effect=AssertionError("GET must not spend a provider request"))
    monkeypatch.setattr(audible_catalog, "lookup", lookup)
    audio = _insert_item(db, title="Exact edition", media_type="audiobook", asin="B000TEST01")
    db.commit()
    page = client.get(f"/item/{audio}")
    assert page.status_code == 200
    assert (f'href="/api/audiobooks/{audio}/metadata"' in page.text) is can_review
    assert ('data-testid="audiobook-metadata-link"' in page.text) is can_review
    panel = client.get(f"/api/audiobooks/{audio}/metadata")
    assert panel.status_code == (200 if can_review else 403)
    assert panel.headers["cache-control"] == "no-store"
    lookup.assert_not_awaited()


def test_item_and_availability_use_exact_handoffs_without_router_injection(viewer_client, viewer_user, db):
    item = _item(db, "Two records, one source", "ebook")
    source = _source(db, name="Reading room")
    first = "https://books.example/read/first"
    second = "https://books.example/read/second"
    _record(db, source, item, external_id="first", media_type="ebook", open_url=first)
    removed = _record(db, source, item, external_id="second", media_type="ebook", open_url=second)
    db.commit()

    detail = viewer_client.get(f"/item/{item}")
    assert detail.status_code == 200
    assert f"/account/source-progress?item_id={item}" in detail.text
    fragment = viewer_client.get(f"/api/library/items/{item}/availability")
    paths = [html.unescape(value) for value in re.findall(
        r'href="(/api/library/items/\d+/open/Read\?binding=[a-f0-9]{64})"', fragment.text,
    )]
    assert fragment.status_code == 200 and len(paths) == len(set(paths)) == 2
    redirects = {viewer_client.get(path, follow_redirects=False).headers["location"]: path for path in paths}
    assert set(redirects) == {first, second}
    for path in paths:
        response = viewer_client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
    db.execute("DELETE FROM source_records WHERE id = ?", (removed,))
    db.commit()
    assert viewer_client.get(redirects[second], follow_redirects=False).status_code == 404
    assert viewer_client.get(redirects[first], follow_redirects=False).headers["location"] == first


def test_real_enabled_lifespan_executes_http_opt_in_and_awaits_shutdown(viewer_client, viewer_user, db, bound, remote, monkeypatch):
    from app import main

    completed = threading.Event()
    finish_schedule = source_progress._finish_schedule

    def finish_and_signal(*args, **kwargs):
        result = finish_schedule(*args, **kwargs)
        completed.set()
        return result

    # Only the polling cadence is shortened. Consent, claim, mocked HTTP,
    # conflict decisions, histories and finalization execute unchanged.
    monkeypatch.delenv("SHELF_DISABLE_SOURCE_PROGRESS")
    monkeypatch.setattr(main, "_SOURCE_PROGRESS_POLL_SECONDS", 0.01)
    monkeypatch.setattr(source_progress, "_finish_schedule", finish_and_signal)
    with viewer_client as live:
        linked = live.post("/api/source-progress/accounts/link", data={
            "source_key": bound["key"], "token": TOKEN, "remote_user_id": REMOTE_USER,
            "consent": source_progress.CONSENT_VERSION,
        }, headers={"Accept": "application/json"})
        assert linked.status_code == 200 and linked.json()["ok"]
        assert live.app.state.source_progress_scheduler_active is True
        account = linked.json()["account_id"]
        assert not completed.is_set()  # ordinary linkage is not consent to schedule
        scheduled = live.post(f"/api/source-progress/accounts/{account}/schedule", data={
            "interval_minutes": "15", "consent": source_progress.SCHEDULE_CONSENT_VERSION,
        }, headers={"Accept": "application/json"})
        assert scheduled.status_code == 200 and scheduled.json()["ok"]
        assert completed.wait(10), "The production lifespan never executed the opted-in pull"
        assert _state(db, viewer_user["id"], bound["item"])["progress"] == 25
        assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 1
        assert db.execute("SELECT last_status FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()[0] == "ok"
    assert live.app.state.source_progress_scheduler_active is False
    assert all(call.request.method == "GET" for call in remote.calls)
