"""The central CSRF middleware consumes this pure early-limit hook."""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routers import source_progress as routes


@pytest.mark.parametrize("path", [
    "/api/library/items/1/preferred-source", "/api/library/items/not-an-int/preferred-source/",
    "/api/library/items/9223372036854775808/preferred-source///",
    "/api/source-progress/accounts/link/", "/api/source-progress/accounts/-1/disconnect",
    "/api/source-progress/accounts/é/schedule/", "/api/source-progress/accounts/1/items/nope/pull/",
    "/api/source-progress/accounts/overflow/items/999999999999999999999/pull",
    "/api/source-progress/reviews/NaN", "/api/source-progress/reviews/1/",
])
def test_form_limit_matches_exact_owned_route_shapes_including_malformed_ids(path):
    assert routes.form_limit(path) == routes.FORM_BYTES == 16384


@pytest.mark.parametrize("path", [
    "/account/source-progress", "/api/source-progress/accounts", "/api/source-progress/accounts/link-extra",
    "/api/source-progress/accounts/1/schedule/other", "/api/source-progress/accounts//disconnect",
    "/api/source-progress/reviews/1/other", "/api/library/items/1/personal",
    "/api/library/items/1/open/Read", "/prefix/api/source-progress/accounts/link", "", None,
])
def test_form_limit_does_not_claim_other_routes(path):
    assert routes.form_limit(path) is None


@pytest.mark.asyncio
async def test_route_parser_stops_at_limit_without_reading_tail():
    chunks = [b"token=" + b"a" * (routes.FORM_BYTES - 6), b"x", b"unread-tail"]
    seen = []

    async def receive():
        chunk = chunks[len(seen)]
        seen.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": len(seen) < len(chunks)}

    request = Request({"type": "http", "method": "POST", "path": "/api/source-progress/accounts/link",
                       "headers": [(b"content-type", b"application/x-www-form-urlencoded")]}, receive)
    with pytest.raises(HTTPException) as failure:
        await routes._form(request)
    assert failure.value.status_code == 413
    assert len(seen) == 2 and b"unread-tail" not in seen


@pytest.mark.asyncio
async def test_route_parser_accepts_exact_limit():
    body = b"notes=" + b"a" * (routes.FORM_BYTES - 6)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/api/source-progress/accounts/link",
                       "headers": [(b"content-type", b"application/x-www-form-urlencoded")]}, receive)
    assert len((await routes._form(request))["notes"]) == routes.FORM_BYTES - 6
