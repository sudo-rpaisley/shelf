"""Route parse refusals and earlier middleware bounds are separate boundaries."""

import sqlite3
from urllib.parse import urlencode

import httpx
import pytest
from starlette.requests import Request

from app import auth, config, features
from app.database import get_db
from app.services import source_migration as migration, trash
from tests.test_source_migration import _seed
from tests.test_source_migration_authority import _request, _snapshot
from tests.test_source_migration_routes import _registered_migration_routes


def _change_authority(change, request=None):
    with get_db() as writer:
        writer.execute("BEGIN IMMEDIATE")
        if change == "demote":
            writer.execute("UPDATE users SET role = 'viewer' WHERE id = 1")
        elif change == "deleted":
            writer.execute("DELETE FROM users WHERE id = 1")
        elif change == "revoke":
            writer.execute("UPDATE users SET token_version = token_version + 1 WHERE id = 1")
            if request is not None:
                # A newer request-state version cannot bless the old cookie or
                # replace an actor already captured by the route.
                request.state.user["token_version"] += 1
        else:
            writer.execute("INSERT INTO settings (key, value) VALUES ('feature.connected_library', '0') "
                           "ON CONFLICT(key) DO UPDATE SET value = '0'")


def _native_request(client, path, raw, between_chunks=None):
    """Direct route fixture: bypass middleware, not the parser or actor checks."""
    prepared = client.build_request("POST", path, content=raw, headers={
        "Content-Type": "application/x-www-form-urlencoded",
    })
    midpoint = max(1, len(raw) // 2)
    calls = []

    async def receive():
        index = len(calls)
        calls.append(index)
        if index == 0:
            return {"type": "http.request", "body": raw[:midpoint], "more_body": True}
        assert index == 1
        if between_chunks is not None:
            between_chunks(request)
        return {"type": "http.request", "body": raw[midpoint:], "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "scheme": "https", "path": path,
        "query_string": b"", "headers": [(name.lower(), value) for name, value in prepared.headers.raw],
        "app": client.app,
    }, receive)
    request.state.user = auth.get_current_user(request)
    assert request.state.user is not None
    return request, calls


async def _call_route(request, action):
    from app.routers import source_migration as routes

    if action == "replacement":
        return await routes.preview_replacement(request)
    if action == "repair":
        return await routes.preview_repair(request)
    return await getattr(routes, action)(request, request.url.path.split("/")[-2])


def _malformed(body, kind, seed):
    native = dict(body)
    if "mappings" in native:
        native["mappings"] = f"{seed[2]},{seed[3]}\n"
    native = {key: "1" if value is True else str(value) for key, value in native.items()}
    first = next(iter(native))
    if kind == "missing":
        return urlencode({key: value for key, value in native.items() if key != first}).encode()
    encoded = urlencode(native).encode()
    if kind == "duplicate":
        return encoded + b"&" + urlencode({first: native[first]}).encode()
    return encoded + b"&padding=" + b"x" * 16384


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replacement", "repair", "apply", "retire", "rollback"])
@pytest.mark.parametrize("kind", ["duplicate", "missing", "oversized"])
@pytest.mark.parametrize("change", ["demote", "deleted", "revoke", "feature"])
async def test_route_parser_failure_rechecks_original_actor_before_error_render(
    admin_client, db, monkeypatch, action, kind, change,
):
    from app.routers import source_migration as routes

    seed = _seed(db)
    path, body = _request(admin_client, db, action, seed)
    protected = _snapshot(db)
    sources = [tuple(row) for row in db.execute("SELECT * FROM source_instances ORDER BY id")]
    raw = _malformed(body, kind, seed)
    entry, nag_reads, issued_tokens = [], [], []
    original = routes._request_actor

    def capture(request):
        actor = original(request)
        entry.append((request, dict(actor)))
        return actor

    def nag(user):
        nag_reads.append(dict(user))
        return {"count": 7}

    def issue(*args, **kwargs):
        issued_tokens.append(True)
        pytest.fail("Error recovery must not issue or adopt a replacement session")

    monkeypatch.setattr(routes, "_request_actor", capture)
    monkeypatch.setattr(trash, "nag_state", nag)
    monkeypatch.setattr(auth, "create_token", issue)

    def during_parse(request):
        # The integrated middleware bounds even header-authenticated bodies
        # first. This direct-route test separately covers the parser's own
        # bounded stream/error path, after the route captured its actor.
        assert len(entry) == 1
        _change_authority(change, request)

    request, reads = _native_request(admin_client, path, raw, during_parse)
    response = await _call_route(request, action)
    assert response.status_code == (401 if change in {"deleted", "revoke"} else 403)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["content-type"].startswith("text/plain")
    text = response.body.decode()
    assert "<nav" not in text and "<form" not in text and 'data-testid="trash-nag"' not in text
    assert all(not value.startswith("access_token=") for value in response.headers.getlist("set-cookie"))
    assert nag_reads == [] and issued_tokens == [] and len(entry) == 1
    assert reads == [0, 1]
    assert _snapshot(db) == protected
    assert [tuple(row) for row in db.execute("SELECT * FROM source_instances ORDER BY id")] == sources


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replacement", "repair", "apply", "retire", "rollback"])
@pytest.mark.parametrize("kind", ["duplicate", "missing", "oversized"])
async def test_route_parser_keeps_authorized_validation_message_and_recovery_link(
    admin_client, db, monkeypatch, action, kind,
):
    seed = _seed(db)
    path, body = _request(admin_client, db, action, seed)
    protected = _snapshot(db)
    original, actors = migration.authorize_mutation, []

    def locked(conn, actor):
        assert conn.in_transaction
        rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                rival.execute("BEGIN IMMEDIATE")
        finally:
            rival.close()
        actors.append(dict(actor))
        original(conn, actor)

    monkeypatch.setattr(migration, "authorize_mutation", locked)
    request, reads = _native_request(admin_client, path, _malformed(body, kind, seed))
    response = await _call_route(request, action)
    assert response.status_code == (413 if kind == "oversized" else 422)
    assert response.headers["Cache-Control"] == "no-store"
    text = response.body.decode()
    assert "Source review refused" in text and 'role="alert"' in text
    assert 'href="/source-migrations"' in text and "Start a fresh source review" in text
    assert len(actors) == 2 and actors[0] == actors[1]
    assert reads == [0, 1]
    assert _snapshot(db) == protected


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["replacement", "repair", "apply", "retire", "rollback"])
@pytest.mark.parametrize("kind", ["duplicate", "missing", "oversized"])
@pytest.mark.parametrize("change", ["demote", "deleted", "revoke", "feature"])
@pytest.mark.parametrize("header_token", [False, True])
async def test_registered_app_streams_through_early_bounds_without_stale_context(
    admin_client, db, monkeypatch, action, kind, change, header_token,
):
    from app import http_body
    from app.routers import source_migration as routes

    seed = _seed(db)
    path, body = _request(admin_client, db, action, seed)
    protected = _snapshot(db)
    sources = [tuple(row) for row in db.execute("SELECT * FROM source_instances ORDER BY id")]
    raw = _malformed({**body, "_csrf": admin_client.cookies.get("csrf_token")}, kind, seed)
    bounded, route_calls, nag_reads, body_reads, issued_tokens = [], [], [], [], []
    original_bound, original_actor = http_body.bounded_body, routes._request_actor

    async def capture_bound(request, limit):
        assert not route_calls  # The ordinary middleware sees the body first.
        bounded.append((request, dict(request.state.user), limit))
        return await original_bound(request, limit)

    def capture_actor(request):
        route_calls.append(path)
        return original_actor(request)

    def issue(*args, **kwargs):
        issued_tokens.append(True)
        pytest.fail("A refused streamed form must not mint a newer session")

    monkeypatch.setattr(http_body, "bounded_body", capture_bound)
    monkeypatch.setattr(routes, "_request_actor", capture_actor)
    monkeypatch.setattr(trash, "nag_state", lambda user: nag_reads.append(dict(user)))
    monkeypatch.setattr(auth, "create_token", issue)

    async def streamed_body():
        assert len(bounded) == 1 and bounded[0][1]["role"] == "admin"
        assert bounded[0][2] == routes.FORM_BYTES and route_calls == []
        midpoint = min(len(raw) // 2, routes.FORM_BYTES)
        body_reads.append("first")
        yield raw[:midpoint]
        assert route_calls == []
        _change_authority(change, bounded[0][0])
        body_reads.append("last")
        yield raw[midpoint:]

    headers = dict(admin_client.headers)
    if not header_token:
        headers.pop("x-csrf-token", None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_client.app),
                                base_url="https://testserver", headers=headers,
                                cookies=admin_client.cookies) as client:
        response = await client.post(path, content=streamed_body(), headers={
            "Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Mode": "navigate",
        })

    # Oversize is refused at the earlier, context-free middleware boundary.
    # Within-budget parse failures reach the fresh route-entry capability.
    expected = 413 if kind == "oversized" else (401 if change in {"deleted", "revoke"} else 403)
    assert response.status_code == expected
    assert response.headers["cache-control"] == "no-store"
    if response.headers["content-type"].startswith("application/json"):
        # A fresh feature dependency can refuse before the route itself. Its
        # finite API error is also context-free, never stale administrator HTML.
        assert change == "feature" and kind != "oversized"
        assert response.json() == {
            "error": "feature_disabled", "feature": "connected_library", "ok": False,
            "message": features.disabled_message("connected_library"),
        }
    else:
        assert response.headers["content-type"].startswith("text/plain")
    assert "<nav" not in response.text and "<form" not in response.text and 'data-testid="trash-nag"' not in response.text
    assert all(not value.startswith("access_token=") for value in response.headers.get_list("set-cookie"))
    assert body_reads == ["first", "last"] and nag_reads == [] and issued_tokens == []
    if kind == "oversized":
        assert route_calls == [] and response.text == "Review form too large"
    assert _snapshot(db) == protected
    assert [tuple(row) for row in db.execute("SELECT * FROM source_instances ORDER BY id")] == sources


@pytest.mark.parametrize("action", ["replacement", "repair", "apply", "retire", "rollback"])
@pytest.mark.parametrize("header_token", [False, True])
def test_authorized_oversized_form_is_refused_before_route_context(
    admin_client, db, monkeypatch, action, header_token,
):
    from app.routers import source_migration as routes

    seed = _seed(db)
    path, body = _request(admin_client, db, action, seed)
    protected = _snapshot(db)

    def forbidden(*args, **kwargs):
        pytest.fail("Early 413 must not consult route actor, parser, or template context")

    monkeypatch.setattr(routes, "_request_actor", forbidden)
    monkeypatch.setattr(trash, "nag_state", forbidden)
    if not header_token:
        admin_client.headers.pop("X-CSRF-Token")
    response = admin_client.post(path, content=_malformed({
        **body, "_csrf": admin_client.cookies.get("csrf_token"),
    }, "oversized", seed), headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 413 and response.text == "Review form too large"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("text/plain")
    assert _snapshot(db) == protected


@pytest.mark.parametrize("action", ["replacement", "repair", "apply"])
def test_parse_error_with_unavailable_authority_check_fails_closed_without_context(admin_client, db, monkeypatch, action):
    seed = _seed(db)
    path, body = _request(admin_client, db, action, seed)
    protected = _snapshot(db)
    original, calls = migration.authorize_mutation, []

    def unavailable(conn, actor):
        calls.append(True)
        if len(calls) == 2:
            raise sqlite3.OperationalError("test-only private database diagnostic")
        original(conn, actor)

    monkeypatch.setattr(migration, "authorize_mutation", unavailable)
    response = admin_client.post(path, content=_malformed(body, "missing", seed),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 503 and response.headers["Cache-Control"] == "no-store"
    assert response.headers["content-type"].startswith("text/plain")
    assert "<nav" not in response.text and "private database diagnostic" not in response.text
    assert _snapshot(db) == protected


@pytest.mark.parametrize("failure", [sqlite3.IntegrityError, sqlite3.DataError])
@pytest.mark.parametrize("expired", [False, True])
def test_native_decision_write_failure_rechecks_original_actor_after_rollback(admin_client, db, monkeypatch, failure, expired):
    from datetime import timedelta

    seed = _seed(db)
    path, body = _request(admin_client, db, "apply", seed)
    protected = _snapshot(db)
    original, actors, now = migration.authorize_mutation, [], migration._now()
    nag_reads = []

    def checked(conn, actor):
        actors.append(dict(actor))
        if len(actors) == 3:
            assert _snapshot(conn) == protected  # Failed writes rolled back first.
        original(conn, actor)

    def failed(conn, *args, **kwargs):
        conn.execute("UPDATE source_records SET title = 'Uncommitted change' WHERE id = ?", (seed[3],))
        if expired:
            monkeypatch.setattr(migration, "_now", lambda: now + timedelta(days=366))
        raise failure("test-only private database diagnostic")

    monkeypatch.setattr(migration, "authorize_mutation", checked)
    monkeypatch.setattr(migration, "apply", failed)
    monkeypatch.setattr(trash, "nag_state", lambda user: nag_reads.append(True))
    native = {key: "1" if value is True else str(value) for key, value in body.items()}
    response = admin_client.post(path, content=urlencode(native), headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == (401 if expired else 409)
    assert response.headers["Cache-Control"] == "no-store" and "private database diagnostic" not in response.text
    if expired:
        assert response.headers["content-type"].startswith("text/plain") and "<nav" not in response.text and nag_reads == []
    else:
        assert 'role="alert"' in response.text and 'href="/source-migrations"' in response.text
    assert len(actors) == 3 and actors[0] == actors[1] == actors[2]
    assert _snapshot(db) == protected
