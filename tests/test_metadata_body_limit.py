"""Metadata form bounds apply before parsing, including plain-form CSRF."""

from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.http_body import BodyTooLarge, METADATA_FORM_BYTES, bounded_body


def _request(chunks, *, header_token=False, path="/api/library/items/1/metadata/resolve"):
    calls = []

    async def receive():
        number = len(calls)
        calls.append(number)
        return {"type": "http.request", "body": chunks[number], "more_body": number + 1 < len(chunks)}

    headers = [(b"content-type", b"application/x-www-form-urlencoded"), (b"cookie", b"csrf_token=token")]
    if header_token:
        headers.append((b"x-csrf-token", b"token"))
    request = Request({"type": "http", "method": "POST", "path": path,
                       "headers": headers, "query_string": b""}, receive)
    return request, calls


@pytest.mark.asyncio
async def test_body_limit_stops_before_consuming_the_rest_of_a_stream():
    request, calls = _request([b"x" * 2048] * 10)
    with pytest.raises(BodyTooLarge):
        await bounded_body(request, METADATA_FORM_BYTES)
    assert calls == [0, 1, 2]


@pytest.mark.asyncio
async def test_accepted_body_remains_parseable_and_reusable():
    request, calls = _request([b"_csrf=token&", b"decision=keep_local"])
    body = await bounded_body(request, METADATA_FORM_BYTES)
    assert await request.body() == body
    assert (await request.form())["decision"] == "keep_local"
    assert await bounded_body(request, METADATA_FORM_BYTES) == body
    assert calls == [0, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/", "//"])
async def test_plain_form_csrf_stops_oversized_metadata_before_parsing(suffix):
    from app.main import CSRFMiddleware

    request, calls = _request([b"x" * 2048] * 10, path="/api/library/items/1/metadata/resolve" + suffix)
    call_next = AsyncMock()
    response = await CSRFMiddleware(AsyncMock()).dispatch(request, call_next)
    assert response.status_code == 413
    assert calls == [0, 1, 2]
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_form_csrf_replays_accepted_bytes_for_the_route():
    from app.main import CSRFMiddleware
    from starlette.responses import Response

    request, calls = _request([b"_csrf=token&decision=", b"keep_local"])

    async def call_next(replayed):
        assert (await replayed.form())["decision"] == "keep_local"
        return Response(status_code=204)

    response = await CSRFMiddleware(AsyncMock()).dispatch(request, call_next)
    assert response.status_code == 204 and calls == [0, 1]


@pytest.mark.asyncio
async def test_header_authenticated_form_is_bounded_by_the_route(monkeypatch):
    from app.routers import metadata_review

    request, calls = _request([b"x" * 2048] * 10, header_token=True)
    # An oversize error should stop before opening any transaction or looking
    # up observations. Rendering is outside the body-reading unit under test.
    expected = object()
    monkeypatch.setattr(metadata_review, "_refused", lambda req, item_id, error: expected)
    assert await metadata_review.resolve_metadata(request, 1) is expected
    assert calls == [0, 1, 2]
