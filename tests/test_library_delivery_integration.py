"""Shared registration and streaming bounds for the first delivery lanes."""

from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.http_body import READING_LIFE_FORM_BYTES, SOURCE_MIGRATION_FORM_BYTES


def _request(path, chunks, *, header_token=False):
    calls = []

    async def receive():
        index = len(calls)
        calls.append(index)
        return {"type": "http.request", "body": chunks[index], "more_body": index + 1 < len(chunks)}

    headers = [(b"content-type", b"application/x-www-form-urlencoded"),
               (b"cookie", b"csrf_token=token")]
    if header_token:
        headers.append((b"x-csrf-token", b"token"))
    return Request({"type": "http", "method": "POST", "path": path,
                    "query_string": b"", "headers": headers}, receive), calls


def test_delivery_routes_are_registered_without_test_fixture_installation():
    from app.main import app

    expected = {
        ("GET", "/reading-life"), ("GET", "/source-migrations"),
        ("POST", "/api/source-migrations/replacement/preview"),
        ("GET", "/api/v1/items"), ("GET", "/api/v1/loans"),
        ("GET", "/api/v1/wishlist"), ("GET", "/api/v1/series"),
        ("GET", "/api/v1/upcoming"), ("GET", "/api/v1/schema"),
    }
    registered = {(method, route.path) for route in app.routes
                  for method in getattr(route, "methods", ())}
    assert expected <= registered


def test_national_provider_is_paced_and_labelled():
    from app import config
    from app.services import browse_counts, scan_outcome

    assert config.HOST_RATE_LIMITS["data.bn.org.pl"] >= 1.0
    assert browse_counts.source_label("bn") == "BN Poland"
    assert scan_outcome.PROVIDER_LABELS["bn"] == "BN Poland"


@pytest.mark.asyncio
@pytest.mark.parametrize("path,limit", [
    ("/api/source-migrations/replacement/preview", SOURCE_MIGRATION_FORM_BYTES),
    ("/api/source-migrations/repair/preview/", SOURCE_MIGRATION_FORM_BYTES),
    ("/api/source-migrations/plan/apply", SOURCE_MIGRATION_FORM_BYTES),
    ("/api/reading-life/items/1/journal", READING_LIFE_FORM_BYTES),
])
async def test_native_form_csrf_bounds_delivery_forms_before_parsing(path, limit):
    from app.main import CSRFMiddleware

    request, calls = _request(path, [b"x" * limit, b"x", b"unread tail"])
    call_next = AsyncMock()
    response = await CSRFMiddleware(AsyncMock()).dispatch(request, call_next)
    assert response.status_code == 413
    assert calls == [0, 1]
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_journal_route_bounds_header_authenticated_forms_too():
    from app.routers import reading_life

    request, calls = _request("/api/reading-life/items/1/journal",
                              [b"x" * READING_LIFE_FORM_BYTES, b"x", b"unread tail"], header_token=True)
    with pytest.raises(HTTPException) as error:
        await reading_life._form(request, ("body",), lambda form: form["body"])
    assert error.value.status_code == 413
    assert calls == [0, 1]


@pytest.mark.asyncio
async def test_maximum_multilingual_journal_fits_native_form_and_replay():
    from app.main import CSRFMiddleware
    from app.routers import reading_life

    body = "📚" * 10000
    encoded = urlencode({"_csrf": "token", "body": body}).encode()
    assert len(encoded) < READING_LIFE_FORM_BYTES
    request, calls = _request("/api/reading-life/items/1/journal", [encoded])

    async def call_next(replayed):
        parsed, supplied, error = await reading_life._form(replayed, ("body",), lambda form: form["body"])
        assert parsed == body and supplied["body"] == body and error is None
        return Response(status_code=204)

    response = await CSRFMiddleware(AsyncMock()).dispatch(request, call_next)
    assert response.status_code == 204
    assert calls == [0]
