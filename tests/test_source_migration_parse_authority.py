"""Delayed native parse failures must not render an old admin request context."""

import sqlite3
from urllib.parse import urlencode

import pytest

from app import auth, config
from app.database import get_db
from app.services import source_migration as migration, trash
from tests.test_source_migration import _seed
from tests.test_source_migration_authority import _request, _snapshot
from tests.test_source_migration_routes import _registered_migration_routes


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


@pytest.mark.parametrize("action", ["replacement", "repair", "apply", "retire", "rollback"])
@pytest.mark.parametrize("kind", ["duplicate", "missing", "oversized"])
@pytest.mark.parametrize("change", ["demote", "deleted", "revoke", "feature"])
def test_delayed_malformed_native_body_rechecks_original_actor_before_error_render(
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

    def delayed_body():
        # Valid CSRF header avoids the middleware consuming a native form
        # first. HTTPX's body iterator is consumed during the route's await.
        assert len(entry) == 1
        midpoint = max(1, len(raw) // 2)
        yield raw[:midpoint]
        with get_db() as writer:
            writer.execute("BEGIN IMMEDIATE")
            if change == "demote":
                writer.execute("UPDATE users SET role = 'viewer' WHERE id = 1")
            elif change == "deleted":
                writer.execute("DELETE FROM users WHERE id = 1")
            elif change == "revoke":
                writer.execute("UPDATE users SET token_version = token_version + 1 WHERE id = 1")
                # Simulate a later login/request-state refresh. It must not be
                # used instead of the already captured actor's old version.
                entry[0][0].state.user["token_version"] += 1
            else:
                writer.execute("INSERT INTO settings (key, value) VALUES ('feature.connected_library', '0') "
                               "ON CONFLICT(key) DO UPDATE SET value = '0'")
        yield raw[midpoint:]

    response = admin_client.post(path, content=delayed_body(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == (401 if change in {"deleted", "revoke"} else 403)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["content-type"].startswith("text/plain")
    assert "<nav" not in response.text and "<form" not in response.text and 'data-testid="trash-nag"' not in response.text
    assert all(not value.startswith("access_token=") for value in response.headers.get_list("set-cookie"))
    assert nag_reads == [] and issued_tokens == [] and len(entry) == 1
    assert _snapshot(db) == protected
    assert [tuple(row) for row in db.execute("SELECT * FROM source_instances ORDER BY id")] == sources


@pytest.mark.parametrize("action", ["replacement", "repair", "apply", "retire", "rollback"])
@pytest.mark.parametrize("kind", ["duplicate", "missing", "oversized"])
def test_valid_admin_keeps_ordinary_native_validation_message_and_recovery_link(
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
    response = admin_client.post(path, content=_malformed(body, kind, seed),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == (413 if kind == "oversized" else 422)
    assert response.headers["Cache-Control"] == "no-store"
    assert "Source review refused" in response.text and 'role="alert"' in response.text
    assert 'href="/source-migrations"' in response.text and "Start a fresh source review" in response.text
    assert len(actors) == 2 and actors[0] == actors[1]
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
