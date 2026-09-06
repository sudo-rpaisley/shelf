"""Store-queue request/value boundary retained across the 0.34 merge."""

import json

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from starlette.requests import Request as StarletteRequest

from app.auth import require_role
from app.routers import store
from app.services import isbn as isbn_svc


_original_store_queue = store.store_queue


def _remove_route(path: str, method: str) -> None:
    method = method.upper()
    store.router.routes[:] = [
        route
        for route in store.router.routes
        if not (
            getattr(route, "path", None) == path
            and method in (getattr(route, "methods", None) or set())
        )
    ]


async def _request_with_json(request: Request, payload: dict) -> StarletteRequest:
    body = json.dumps(payload).encode("utf-8")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = dict(request.scope)
    headers = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() not in (b"content-length", b"content-type")
    ]
    headers.extend(
        [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
    )
    scope["headers"] = headers
    patched = StarletteRequest(scope, receive)
    patched.state.user = request.state.user
    return patched


_remove_route("/api/store/queue", "POST")


@store.router.post("/api/store/queue")
async def integrated_store_queue(
    request: Request,
    _=Depends(require_role("editor")),
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    isbns = body.get("isbns")
    if not isinstance(isbns, list) or not all(isinstance(value, str) for value in isbns):
        return JSONResponse(
            {"error": "isbns must be a list of strings"}, status_code=400
        )

    values = list(dict.fromkeys(isbns))[: store.QUEUE_BATCH_LIMIT]
    valid = []
    validity = []
    for raw in values:
        if isbn_svc.canonical_isbn_pair(raw) is None:
            validity.append(False)
        else:
            validity.append(True)
            valid.append(raw)

    valid_results = []
    if valid:
        patched = await _request_with_json(request, {"isbns": valid})
        response = await _original_store_queue(patched, _=request.state.user)
        if isinstance(response, dict):
            valid_results = list(response.get("results") or [])
        else:
            return response

    valid_iter = iter(valid_results)
    results = []
    for raw, is_valid in zip(values, validity):
        if is_valid:
            try:
                results.append(next(valid_iter))
            except StopIteration:
                results.append({"isbn": raw, "status": "error"})
        else:
            results.append({"isbn": raw, "status": "invalid"})

    return {"results": results}
