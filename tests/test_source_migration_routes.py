"""Admin/feature/CSRF boundaries and usable native source-review forms."""

import pytest

from app import features
from app.database import get_db
from app.services import source_migration as migration
from tests.test_source_migration import _binding, _protected, _seed


@pytest.fixture(autouse=True)
def _registered_migration_routes(client):
    """The coordinator owns central registration; keep this slice independent."""
    from app.routers import source_migration as routes

    app = client.app
    before = list(app.router.routes)
    if not any(getattr(route, "path", None) == "/source-migrations" for route in before):
        app.include_router(routes.page_router)
        app.include_router(routes.api_router)
    yield
    app.router.routes[:] = before


def _replacement(seed, **overrides):
    return {"old_source_id": seed[0], "new_source_id": seed[1],
            "mappings": [{"old_record_id": seed[2], "new_record_id": seed[3]}], **overrides}


def _api_preview(client, seed, **overrides):
    response = client.post("/api/source-migrations/replacement/preview", json=_replacement(seed, **overrides))
    assert response.status_code == 200, response.text
    return response.json()


def _off():
    with get_db() as conn:
        features.set_feature_enabled(conn, "connected_library", False)


API_PATHS = ("/replacement/preview", "/repair/preview", "/" + "0" * 32 + "/apply",
             "/" + "0" * 32 + "/retire", "/" + "0" * 32 + "/rollback")


@pytest.mark.parametrize("client_name", ["viewer_client", "editor_client"])
@pytest.mark.parametrize("suffix", API_PATHS)
def test_every_source_review_mutation_is_admin_only(request, client_name, suffix):
    response = request.getfixturevalue(client_name).post("/api/source-migrations" + suffix, json={})
    assert response.status_code == 403


@pytest.mark.parametrize("client_name", ["viewer_client", "editor_client"])
@pytest.mark.parametrize("path", ["/source-migrations", "/source-migrations/" + "0" * 32])
def test_review_pages_are_not_readable_to_nonadmins(request, client_name, path):
    response = request.getfixturevalue(client_name).get(path, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/browse"


def test_role_refusal_precedes_disabled_feature(viewer_client):
    _off()
    response = viewer_client.post("/api/source-migrations/repair/preview", json={})
    assert response.status_code == 403 and "feature_disabled" not in response.text
    assert "Connected library" not in response.text


@pytest.mark.parametrize("suffix", API_PATHS)
def test_feature_off_refuses_all_review_mutations(admin_client, suffix):
    _off()
    response = admin_client.post("/api/source-migrations" + suffix, json={})
    assert response.status_code == 403 and response.json()["feature"] == "connected_library"


@pytest.mark.parametrize("path", ["/source-migrations", "/source-migrations/" + "0" * 32])
def test_feature_off_hides_review_pages(admin_client, path):
    _off()
    response = admin_client.get(path)
    assert response.status_code == 200 and 'data-testid="feature-disabled"' in response.text


@pytest.mark.parametrize("suffix", API_PATHS)
def test_every_review_mutation_requires_csrf(admin_client, suffix):
    admin_client.headers.pop("X-CSRF-Token")
    response = admin_client.post("/api/source-migrations" + suffix, json={})
    assert response.status_code == 403 and "CSRF" in response.text


def test_admin_workspace_shows_evidence_and_native_preview_forms_without_credentials(admin_client, db):
    seed = _seed(db)
    response = admin_client.get(f"/source-migrations?old_source_id={seed[0]}&new_source_id={seed[1]}")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert "9780306406157" in response.text
    assert f"{seed[2]},{seed[3]}" in response.text
    assert 'action="/api/source-migrations/replacement/preview"' in response.text
    assert 'action="/api/source-migrations/repair/preview"' in response.text
    assert 'name="_csrf" value="test-csrf-token-fixed"' in response.text
    assert "never automatic identity evidence" in response.text
    assert "test-only" not in response.text and "gAAAAA" not in response.text and "books.invalid" not in response.text


def test_single_source_record_lookup_makes_machine_repair_discoverable(admin_client, db):
    seed = _seed(db)
    response = admin_client.get(f"/source-migrations?repair_source_id={seed[1]}&repair_record_id={seed[3]}")
    assert response.status_code == 200 and "Machine records" in response.text
    assert f"Record ID #{seed[3]}" in response.text
    assert f'name="record_id" value="{seed[3]}"' in response.text
    assert "9780306406157" in response.text and "new-id" in response.text
    assert "test-only" not in response.text and "books.invalid" not in response.text


def test_native_csrf_preview_apply_retire_and_rollback_are_usable_without_javascript(admin_client, db):
    seed = _seed(db)
    admin_client.headers.pop("X-CSRF-Token")
    csrf = admin_client.cookies.get("csrf_token")
    response = admin_client.post("/api/source-migrations/replacement/preview", data={
        "_csrf": csrf, "old_source_id": str(seed[0]), "new_source_id": str(seed[1]), "mappings": f"{seed[2]},{seed[3]}\n",
    }, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/source-migrations/")
    page = admin_client.get(location)
    assert page.status_code == 200 and "Dry run only" in page.text
    assert "Explicit administrator review" not in page.text and "Exact remote identifier evidence" in page.text
    assert f'After' in page.text and f'/item/{seed[4]}' in page.text
    row = db.execute("SELECT id, fingerprint FROM source_migration_plans").fetchone()
    response = admin_client.post(f"/api/source-migrations/{row['id']}/apply", data={
        "_csrf": csrf, "fingerprint": row["fingerprint"], "reviewed": "1",
    }, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == location
    assert _binding(db, seed[3]) == seed[4]
    with get_db() as conn:
        receipt = migration.detail(conn, row["id"], actor_id=1)
    page = admin_client.get(location)
    assert "Reviewed bindings applied" in page.text
    assert 'name="fingerprint"' in page.text and "/retire" in page.text and "/rollback" in page.text
    response = admin_client.post(f"/api/source-migrations/{row['id']}/retire", data={
        "_csrf": csrf, "fingerprint": receipt["retire_fingerprint"], "reviewed": "1",
    }, follow_redirects=False)
    assert response.status_code == 303
    with get_db() as conn:
        receipt = migration.detail(conn, row["id"], actor_id=1)
    response = admin_client.post(f"/api/source-migrations/{row['id']}/rollback", data={
        "_csrf": csrf, "fingerprint": receipt["rollback_fingerprint"], "reviewed": "1",
    }, follow_redirects=False)
    assert response.status_code == 303 and _binding(db, seed[3]) == seed[5]
    assert "Bindings rolled back" in admin_client.get(location).text


def test_json_repair_preview_then_apply_preserves_local_state_and_reports_manual_evidence(admin_client, db):
    seed = _seed(db)
    before = _protected(db)
    response = admin_client.post("/api/source-migrations/repair/preview", json={
        "source_id": seed[1], "record_id": seed[3], "item_id": seed[4], "manual_reviewed": True,
    })
    assert response.status_code == 200
    preview = response.json()
    assert preview["plan"]["mappings"][0]["evidence"] == [{"kind": "reviewed_record_and_item_ids", "record_id": seed[3], "item_id": seed[4]}]
    response = admin_client.post(f"/api/source-migrations/{preview['id']}/apply", json={"fingerprint": preview["fingerprint"], "reviewed": True})
    assert response.status_code == 200 and response.json()["ok"]
    assert _binding(db, seed[3]) == seed[4] and _protected(db) == before


def test_html_conflict_is_visible_and_never_reflects_supplied_diagnostics_or_redirects(admin_client, db):
    seed = _seed(db)
    preview = _api_preview(admin_client, seed)
    response = admin_client.post(f"/api/source-migrations/{preview['id']}/apply", data={
        "fingerprint": "hostile-input-not-for-errors", "reviewed": "1",
    }, headers={"Referer": "https://hostile.invalid/", "HX-Current-URL": "https://hostile.invalid/"})
    assert response.status_code == 409 and 'role="alert"' in response.text
    assert 'href="/source-migrations"' in response.text
    assert "hostile-input" not in response.text and "hostile.invalid" not in response.text
    assert _binding(db, seed[3]) == seed[5]


@pytest.mark.parametrize("body", [
    {"source_id": 1, "record_id": 2, "item_id": 3, "manual_reviewed": False},
    {"source_id": 1, "record_id": 2, "item_id": 3, "manual_reviewed": True, "metadata_json": "untrusted"},
    {"source_id": True, "record_id": 2, "item_id": 3, "manual_reviewed": True},
])
def test_review_forms_reject_unreviewed_values_and_client_supplied_metadata(admin_client, body):
    response = admin_client.post("/api/source-migrations/repair/preview", json=body)
    assert response.status_code in (404, 422) and not response.json()["ok"]
    assert "untrusted" not in response.text


@pytest.mark.parametrize("payload,content_type", [
    ('{"source_id":1,"source_id":2,"record_id":3,"item_id":4,"manual_reviewed":true}', "application/json"),
    ("source_id=1&source_id=2&record_id=3&item_id=4&manual_reviewed=1", "application/x-www-form-urlencoded"),
    ("[]", "application/json"),
    ('{"source_id":1}', "text/plain"),
])
def test_duplicate_fields_and_wrong_content_types_are_refused(admin_client, payload, content_type):
    response = admin_client.post("/api/source-migrations/repair/preview", content=payload, headers={"Content-Type": content_type})
    assert response.status_code == 422


def test_route_body_limit_rejects_large_json_before_staging_a_plan(admin_client, db):
    response = admin_client.post("/api/source-migrations/replacement/preview", content="x" * 16385,
                                 headers={"Content-Type": "application/json"})
    assert response.status_code == 413 and db.execute("SELECT COUNT(*) FROM source_migration_plans").fetchone()[0] == 0


def test_get_pages_and_get_action_urls_do_not_create_reviews_or_change_bindings(admin_client, db, monkeypatch):
    from app.services.source_connectors import opds

    seed = _seed(db)

    def forbidden(*args, **kwargs):
        pytest.fail("Review GET must not fetch")

    monkeypatch.setattr(opds, "fetch_catalogue", forbidden)
    before = _protected(db)
    assert admin_client.get("/source-migrations").status_code == 200
    assert admin_client.get(f"/source-migrations?old_source_id={seed[0]}&new_source_id={seed[1]}").status_code == 200
    for suffix in API_PATHS:
        assert admin_client.get("/api/source-migrations" + suffix).status_code == 405
    assert _protected(db) == before
    assert db.execute("SELECT COUNT(*) FROM source_migration_plans").fetchone()[0] == 0


def test_html_titles_and_external_ids_are_escaped(admin_client, db):
    seed = _seed(db)
    db.execute("UPDATE source_records SET title = '<script>source()</script>', external_id = '<img onerror=bad>' WHERE id = ?", (seed[3],))
    db.commit()
    response = admin_client.get(f"/source-migrations?old_source_id={seed[0]}&new_source_id={seed[1]}")
    assert response.status_code == 200 and "&lt;script&gt;" in response.text
    assert "<script>source()" not in response.text and "<img onerror=bad>" not in response.text
    preview = _api_preview(admin_client, seed, manual_reviewed=True)
    response = admin_client.get(f"/source-migrations/{preview['id']}")
    assert response.status_code == 200 and "&lt;img onerror=bad&gt;" in response.text


def test_second_administrator_cannot_use_another_administrators_review(admin_client, db):
    seed = _seed(db)
    preview = migration.preview_replacement(db, actor_id=1234, **_replacement(seed))
    db.commit()
    assert admin_client.get(f"/source-migrations/{preview['id']}").status_code == 404
    response = admin_client.post(f"/api/source-migrations/{preview['id']}/apply", json={"fingerprint": preview["fingerprint"], "reviewed": True})
    assert response.status_code == 404 and _binding(db, seed[3]) == seed[5]


def test_failure_during_second_mapping_rolls_back_first_binding_and_authority_changes(admin_client, db, monkeypatch):
    from tests.test_source_migration import _record

    seed = _seed(db)
    old_second, _ = _record(db, seed[0], "old-second", isbn="9780140328721")
    new_second, _ = _record(db, seed[1], "new-second", isbn="9780140328721")
    db.commit()
    preview = _api_preview(admin_client, seed, mappings=[
        {"old_record_id": seed[2], "new_record_id": seed[3]},
        {"old_record_id": old_second, "new_record_id": new_second},
    ])
    before = _protected(db)
    provenance = [tuple(row) for row in db.execute("SELECT * FROM item_metadata_provenance ORDER BY item_id, source_key, field_name")]
    original, calls = migration._rebind, []

    def fail_second(*args):
        calls.append(True)
        if len(calls) == 2:
            raise migration.MigrationError("write_failed", "Binding change refused.", 409)
        return original(*args)

    monkeypatch.setattr(migration, "_rebind", fail_second)
    response = admin_client.post(f"/api/source-migrations/{preview['id']}/apply", json={"fingerprint": preview["fingerprint"], "reviewed": True})
    assert response.status_code == 409
    assert _binding(db, seed[3]) == seed[5] and _protected(db) == before
    assert [tuple(row) for row in db.execute("SELECT * FROM item_metadata_provenance ORDER BY item_id, source_key, field_name")] == provenance
    assert db.execute("SELECT COUNT(*) FROM source_binding_versions").fetchone()[0] == 0
    assert db.execute("SELECT state FROM source_migration_plans WHERE id = ?", (preview["id"],)).fetchone()[0] == "preview"
