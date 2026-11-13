"""Entry-session snapshots must not authorize delayed native migration forms."""

from datetime import timedelta
import sqlite3

import pytest

from app import config
from app.database import get_db
from app.services import source_migration as migration
from tests.test_source_migration import _seed
from tests.test_source_migration_routes import _registered_migration_routes, _replacement


def _request(client, db, action, seed):
    if action == "replacement":
        return "/api/source-migrations/replacement/preview", _replacement(seed)
    if action == "repair":
        return "/api/source-migrations/repair/preview", {"source_id": seed[1], "record_id": seed[3], "item_id": seed[4], "manual_reviewed": True}
    preview = client.post("/api/source-migrations/replacement/preview", json=_replacement(seed)).json()
    if action != "apply":
        assert client.post(f"/api/source-migrations/{preview['id']}/apply", json={"fingerprint": preview["fingerprint"], "reviewed": True}).json()["ok"]
        receipt = migration.detail(db, preview["id"], actor_id=1)
        fingerprint = receipt[action + "_fingerprint"]
    else:
        fingerprint = preview["fingerprint"]
    return f"/api/source-migrations/{preview['id']}/{action}", {"fingerprint": fingerprint, "reviewed": True}


def _snapshot(db):
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("source_records", "source_binding_versions", "source_migration_plans", "item_metadata_provenance", "items_live")}


@pytest.mark.parametrize("action", ["replacement", "repair", "apply", "retire", "rollback"])
@pytest.mark.parametrize("change", ["demote", "revoke", "rename", "deleted", "feature", "expired"])
def test_delayed_form_rechecks_original_session_and_feature_under_writer(admin_client, db, monkeypatch, action, change):
    from app.routers import source_migration as routes

    seed = _seed(db)
    path, body = _request(admin_client, db, action, seed)
    before = _snapshot(db)
    original = routes._body
    now = migration._now()

    async def delayed(request, *args):
        result = await original(request, *args)
        if change == "expired":
            monkeypatch.setattr(migration, "_now", lambda: now + timedelta(days=366))
        else:
            with get_db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if change == "demote":
                    conn.execute("UPDATE users SET role = 'viewer' WHERE id = 1")
                elif change == "revoke":
                    conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = 1")
                    # A mutated request state/re-login must not bless this work.
                    request.state.user["token_version"] += 1
                elif change == "rename":
                    conn.execute("UPDATE users SET username = 'Renamed' WHERE id = 1")
                elif change == "deleted":
                    conn.execute("DELETE FROM users WHERE id = 1")
                else:
                    conn.execute("INSERT INTO settings (key, value) VALUES ('feature.connected_library', '0') "
                                 "ON CONFLICT(key) DO UPDATE SET value = '0'")
        return result

    monkeypatch.setattr(routes, "_body", delayed)
    response = admin_client.post(path, json=body)
    assert response.status_code == (403 if change in {"demote", "feature"} else 401)
    assert response.json()["code"] == ("feature_disabled" if change == "feature" else "actor_changed")
    assert _snapshot(db) == before


def test_final_precommit_expiry_rolls_back_binding_authority_and_journal(admin_client, db, monkeypatch):
    seed = _seed(db)
    path, body = _request(admin_client, db, "apply", seed)
    before = _snapshot(db)
    original, now = migration.apply, migration._now()

    def expire_after_writes(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(migration, "_now", lambda: now + timedelta(days=366))
        return result

    monkeypatch.setattr(migration, "apply", expire_after_writes)
    response = admin_client.post(path, json=body)
    assert response.status_code == 401 and _snapshot(db) == before


def test_authority_guard_runs_at_entry_after_body_and_before_commit_with_same_actor(admin_client, db, monkeypatch):
    seed = _seed(db)
    path, body = _request(admin_client, db, "apply", seed)
    original, actors = migration.authorize_mutation, []

    def guarded(conn, actor):
        rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                rival.execute("BEGIN IMMEDIATE")
        finally:
            rival.close()
        actors.append(dict(actor))
        original(conn, actor)

    monkeypatch.setattr(migration, "authorize_mutation", guarded)
    assert admin_client.post(path, json=body).json()["ok"]
    assert len(actors) == 3 and actors[0] == actors[1] == actors[2]
    assert {"id", "username", "role", "token_version", "expires_at"} <= actors[0].keys()


def test_entry_recheck_ignores_cached_enabled_feature_before_reading_a_body(admin_client, db, monkeypatch):
    from app import features
    from app.routers import source_migration as routes

    seed = _seed(db)
    assert features.feature_enabled("connected_library")
    db.execute("INSERT INTO settings (key, value) VALUES ('feature.connected_library', '0')")
    db.commit()  # Deliberately leave the feature dependency's process cache stale.

    async def forbidden(*args):
        pytest.fail("A revoked entry capability must be refused before body parsing")

    monkeypatch.setattr(routes, "_body", forbidden)
    response = admin_client.post("/api/source-migrations/replacement/preview", json=_replacement(seed))
    assert response.status_code == 403 and response.json()["code"] == "feature_disabled"
    assert db.execute("SELECT COUNT(*) FROM source_migration_plans").fetchone()[0] == 0


def test_revoked_native_form_never_renders_stale_administrator_controls(admin_client, db, monkeypatch):
    from app.routers import source_migration as routes

    seed = _seed(db)
    original = routes._body

    async def delayed(request, *args):
        values = await original(request, *args)
        with get_db() as writer:
            writer.execute("UPDATE users SET role = 'viewer' WHERE id = 1")
        return values

    monkeypatch.setattr(routes, "_body", delayed)
    response = admin_client.post("/api/source-migrations/replacement/preview", data={
        "old_source_id": seed[0], "new_source_id": seed[1], "mappings": f"{seed[2]},{seed[3]}",
    })
    assert response.status_code == 403 and response.headers["content-type"].startswith("text/plain")
    assert "<nav" not in response.text and "<form" not in response.text
    assert db.execute("SELECT COUNT(*) FROM source_migration_plans").fetchone()[0] == 0


@pytest.mark.parametrize("path,expected", [
    ("/api/source-migrations/replacement/preview", 16384),
    ("/api/source-migrations/repair/preview/", 16384),
    ("/api/source-migrations/untrusted/apply", 16384),
    ("/api/source-migrations/untrusted/retire", 16384),
    ("/api/source-migrations/untrusted/rollback", 16384),
    ("/api/source-migrations/inventory", None),
    ("/api/sources/1/sync", None),
])
def test_form_limit_hook_is_pure_and_shape_bounded(path, expected):
    from app.routers.source_migration import form_limit

    assert form_limit(path) == expected
