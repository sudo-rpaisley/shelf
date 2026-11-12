"""Source persistence, concurrency and authorization; all fetches are fixtures."""

import asyncio
import base64
import json
from urllib.parse import quote

import httpx
import pytest

from app import crypto, features
from app.database import get_db
from app.services import item_write, metadata_review, sources
from app.services.source_connectors import PROVIDERS, jellyfin, opds
from app.services.source_connectors.base import Catalogue, ConnectorError, SourceRecord
from tests.conftest import _insert_item, _insert_location

ISBN = "9780306406157"
SECRET = "private-password-not-for-pages"


def _source(**overrides):
    fields = {"provider": "opds", "name": "Home books", "base_url": "http://books.lan/opds"}
    fields.update(overrides)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        return sources.create_instance(db, **fields)


def _row(source_id):
    with get_db() as db:
        return dict(db.execute("SELECT * FROM source_instances WHERE id = ?", (source_id,)).fetchone())


def _book(identity="opaque:1", title="Remote book", media_type="ebook", **values):
    return SourceRecord(identity, title, media_type, **values)


def _catalogue(monkeypatch, records, *, complete=True, warnings=None, provider="opds"):
    result = Catalogue(records, complete, warnings or [])

    async def fetch(source, credentials):
        return result

    monkeypatch.setattr(jellyfin if provider == "jellyfin" else opds, "fetch_catalogue", fetch)
    return result


def test_credentials_are_encrypted_and_dtos_are_redacted(db):
    source_id = _source(credentials={"username": "private-reader", "password": SECRET})
    stored = _row(source_id)["credentials_enc"]
    assert stored.startswith("gAAAAA") and SECRET not in stored
    assert json.loads(crypto.decrypt_value(stored, crypto.get_encryption_key())) == {
        "username": "private-reader", "password": SECRET,
    }
    listing = sources.list_instances(db)
    detail = sources.get_instance(db, source_id)
    for payload in (listing, detail):
        encoded = json.dumps(payload)
        assert SECRET not in encoded and "private-reader" not in encoded and stored not in encoded
    assert "base_url" not in listing[0] and "options" not in listing[0]
    assert detail["credentials_saved"] is True


@pytest.mark.parametrize("fields,code", [
    ({"provider": "unknown"}, "unsupported_provider"),
    ({"name": ""}, "invalid_config"), ({"name": "x" * 201}, "invalid_config"),
    ({"base_url": "https://u:p@host/opds"}, "invalid_url"),
    ({"base_url": "http://host/opds?api_key=secret"}, "invalid_url"),
    ({"options": {"password": SECRET}}, "invalid_options"),
    ({"options": {"max_pages": 51}}, "invalid_options"),
    ({"options": {"media_type": {}}}, "invalid_options"),
    ({"options": "[]"}, "invalid_options"),
    ({"credentials": {"unexpected": SECRET}}, "invalid_credentials"),
    ({"credentials": {"username": "contains:colon"}}, "invalid_credentials"),
    ({"enabled": "true"}, "invalid_config"),
])
def test_invalid_configuration_writes_nothing(db, fields, code):
    with pytest.raises(ConnectorError) as exc:
        _source(**fields)
    assert exc.value.code == code
    assert db.execute("SELECT COUNT(*) FROM source_instances").fetchone()[0] == 0


def test_saved_credentials_preserved_replaced_and_cleared(db):
    source_id = _source(credentials={"password": SECRET})
    before = _row(source_id)
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        sources.save_settings(conn, source_id, name="Renamed", base_url=before["base_url"])
    assert _row(source_id)["credentials_enc"] == before["credentials_enc"]
    assert _row(source_id)["config_revision"] == 2
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        sources.save_settings(conn, source_id, name="Renamed", base_url=before["base_url"], clear_credentials=True)
    assert _row(source_id)["credentials_enc"] == ""


async def test_two_instances_same_external_identity_do_not_collide(db, monkeypatch):
    first = _source()
    second = _source(name="Other server", base_url="http://other.lan/opds")
    _catalogue(monkeypatch, [_book()])
    assert (await sources.sync_instance(first))["summary"]["added"] == 1
    assert (await sources.sync_instance(second))["summary"]["added"] == 1
    rows = db.execute("SELECT source_id, external_id, item_id FROM source_records ORDER BY source_id").fetchall()
    assert len(rows) == 2 and rows[0]["external_id"] == rows[1]["external_id"]
    assert rows[0]["item_id"] != rows[1]["item_id"]


async def test_idempotent_sync_imports_neither_owned_nor_wishlisted(db, monkeypatch):
    source_id = _source()
    _catalogue(monkeypatch, [_book(isbn=ISBN)])
    first = await sources.sync_instance(source_id)
    second = await sources.sync_instance(source_id)
    assert first["summary"]["added"] == 1 and second["summary"]["updated"] == 1
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 1
    item = db.execute("SELECT * FROM items_live").fetchone()
    assert item["owned"] == 0 and item["location_id"] is None
    assert db.execute("SELECT COUNT(*) FROM list_items").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM copies_live").fetchone()[0] == 0
    assert _row(source_id)["last_status"] == "ok"
    assert _row(source_id)["last_sync_at"] is not None


async def test_exact_isbn_only_matches_same_media_type_without_changing_local_state(db, monkeypatch):
    local_id = _insert_item(db, title="My edited ebook", isbn=ISBN, media_type="ebook", owned=1)
    paper_id = _insert_item(db, title="Paper edition", isbn=ISBN, media_type="book")
    db.commit()
    before = dict(db.execute("SELECT * FROM items_live WHERE id = ?", (local_id,)).fetchone())
    source_id = _source()
    _catalogue(monkeypatch, [_book(isbn="0-306-40615-2")])
    result = await sources.sync_instance(source_id)
    assert result["summary"]["linked"] == 1 and result["summary"]["added"] == 0
    assert db.execute("SELECT item_id FROM source_records").fetchone()[0] == local_id != paper_id
    assert dict(db.execute("SELECT * FROM items_live WHERE id = ?", (local_id,)).fetchone()) == before


async def test_same_isbn_different_media_does_not_cross_link(db, monkeypatch):
    local_id = _insert_item(db, isbn=ISBN, media_type="book")
    db.commit()
    source_id = _source()
    _catalogue(monkeypatch, [_book(isbn=ISBN)])
    result = await sources.sync_instance(source_id)
    assert result["summary"]["added"] == 1 and result["summary"]["linked"] == 0
    assert db.execute("SELECT item_id FROM source_records").fetchone()[0] != local_id
    assert db.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 0


async def test_title_matching_never_adopts_a_local_item(db, monkeypatch):
    _insert_item(db, title="Remote book", isbn=None, media_type="ebook", authors="Same Writer")
    db.commit()
    source_id = _source()
    _catalogue(monkeypatch, [_book(authors="Same Writer")])
    assert (await sources.sync_instance(source_id))["summary"]["added"] == 1
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 2


async def test_invalid_isbn_cannot_claim_an_existing_identity(db, monkeypatch):
    _insert_item(db, isbn=ISBN, media_type="ebook")
    db.commit()
    source_id = _source()
    _catalogue(monkeypatch, [_book(isbn="prefix" + ISBN)])
    assert (await sources.sync_instance(source_id))["summary"]["added"] == 1
    imported = db.execute("SELECT i.isbn FROM items_live i JOIN source_records r ON r.item_id = i.id").fetchone()
    assert imported["isbn"] is None


async def test_ambiguous_legacy_isbn_is_skipped_not_guessed(db, monkeypatch):
    _insert_item(db, isbn=ISBN, media_type="ebook")
    _insert_item(db, isbn=None, isbn10="0306406152", media_type="ebook")
    db.commit()
    source_id = _source()
    _catalogue(monkeypatch, [_book(isbn=ISBN)])
    result = await sources.sync_instance(source_id)
    assert result["status"] == "partial" and result["summary"]["skipped"] == 1
    assert result["summary"]["warnings"] == ["ambiguous_isbn"]
    assert db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 0


async def test_remote_refresh_preserves_all_local_edits_and_only_updates_snapshot(db, monkeypatch):
    source_id = _source()
    catalogue = _catalogue(monkeypatch, [_book(isbn=ISBN)])
    await sources.sync_instance(source_id)
    item_id = db.execute("SELECT item_id FROM source_records").fetchone()[0]
    location_id = _insert_location(db)
    item_write.update_item_fields(db, item_id, {
        "title": "My title", "authors": "My author", "description": "Private synopsis",
        "notes": "Private notes", "location_id": location_id, "owned": 1,
        "reading_status": "read", "media_type": "book",
    })
    db.commit()
    before = dict(db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone())
    copies = [dict(r) for r in db.execute("SELECT * FROM copies_live").fetchall()]
    catalogue.records[:] = [_book(title="Changed remote title", authors="Remote Author", isbn=ISBN,
                                 open_url="https://public.example/books/1", metadata={"description": "Remote synopsis"})]
    result = await sources.sync_instance(source_id)
    assert result["summary"]["updated"] == 1
    assert dict(db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()) == before
    assert [dict(r) for r in db.execute("SELECT * FROM copies_live").fetchall()] == copies
    record = db.execute("SELECT * FROM source_records").fetchone()
    assert record["title"] == "Changed remote title" and record["media_type"] == "ebook"
    assert json.loads(record["metadata_json"])["description"] == "Remote synopsis"


async def test_snapshot_metadata_allowlist_and_credential_links(db, monkeypatch):
    source_id = _source(credentials={"password": SECRET})
    _catalogue(monkeypatch, [_book(open_url="https://public.example/book?api_key=anything",
                                  metadata={"description": "Safe", "password": SECRET, "unknown": "discard"})])
    result = await sources.sync_instance(source_id)
    assert result["status"] == "ok"
    row = db.execute("SELECT * FROM source_records").fetchone()
    assert row["open_url"] is None and json.loads(row["metadata_json"]) == {"description": "Safe"}


async def test_reflected_credential_is_never_persisted(db, monkeypatch):
    source_id = _source(credentials={"password": SECRET})
    _catalogue(monkeypatch, [_book(title="Echo " + SECRET)])
    result = await sources.sync_instance(source_id)
    assert result["status"] == "partial"
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 0
    assert SECRET not in json.dumps(_row(source_id))


async def test_complete_only_missing_reconciliation_and_return(db, monkeypatch):
    source_id = _source()
    catalogue = _catalogue(monkeypatch, [_book("one"), _book("two")])
    await sources.sync_instance(source_id)
    db.execute("UPDATE source_instances SET last_sync_at = '2000-01-01 00:00:00' WHERE id = ?", (source_id,))
    db.commit()
    catalogue.records[:] = [_book("one")]
    catalogue.complete = False
    partial = await sources.sync_instance(source_id)
    assert partial["status"] == "partial" and partial["summary"]["unavailable"] == 0
    assert _row(source_id)["last_sync_at"] == "2000-01-01 00:00:00"
    assert db.execute("SELECT availability FROM source_records WHERE external_id = 'two'").fetchone()[0] == "available"
    catalogue.complete = True
    complete = await sources.sync_instance(source_id)
    assert complete["summary"]["unavailable"] == 1
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 2
    catalogue.records.append(_book("two"))
    await sources.sync_instance(source_id)
    assert db.execute("SELECT availability FROM source_records WHERE external_id = 'two'").fetchone()[0] == "available"


@pytest.mark.parametrize("error", [ConnectorError("auth_failed"), ConnectorError("rate_limited"), httpx.ReadTimeout(SECRET), RuntimeError(SECRET)])
async def test_fetch_failures_preserve_last_success_availability_and_recover(db, monkeypatch, error):
    source_id = _source()
    _catalogue(monkeypatch, [_book()])
    await sources.sync_instance(source_id)
    db.execute("UPDATE source_instances SET last_sync_at = '2000-01-01 00:00:00' WHERE id = ?", (source_id,))
    db.commit()

    async def fail(source, credentials):
        raise error

    monkeypatch.setattr(opds, "fetch_catalogue", fail)
    result = await sources.sync_instance(source_id)
    row = _row(source_id)
    assert result["ok"] is False and row["last_status"] == "error"
    assert row["last_sync_at"] == "2000-01-01 00:00:00"
    assert row["last_attempt_at"] > row["last_sync_at"]
    assert row["sync_token"] is None and SECRET not in json.dumps(result)
    assert db.execute("SELECT availability FROM source_records").fetchone()[0] == "available"
    _catalogue(monkeypatch, [_book()])
    assert (await sources.sync_instance(source_id))["status"] == "ok"


async def test_existing_source_attachment_to_trash_stays_hidden(db, monkeypatch):
    source_id = _source()
    _catalogue(monkeypatch, [_book()])
    await sources.sync_instance(source_id)
    item_id = db.execute("SELECT item_id FROM source_records").fetchone()[0]
    db.execute("BEGIN IMMEDIATE")
    item_write.trash_item(db, item_id)
    db.commit()
    result = await sources.sync_instance(source_id)
    assert result["summary"]["in_trash"] == 1
    assert sources.list_records(db, source_id) == []
    assert sources.list_item_records(db, item_id) == []
    assert db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 0
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (item_id,)).fetchone()[0]


async def test_isbn_in_trash_is_not_restored_or_reimported(db, monkeypatch):
    item_id = _insert_item(db, isbn=ISBN, media_type="ebook")
    item_write.trash_item(db, item_id)
    db.commit()
    source_id = _source()
    _catalogue(monkeypatch, [_book(isbn=ISBN)])
    result = await sources.sync_instance(source_id)
    assert result["summary"]["in_trash"] == 1
    assert db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["settings", "disable"])
async def test_configuration_race_discards_fetched_records(db, monkeypatch, change):
    source_id = _source()

    async def fetch(source, credentials):
        # A second writer can proceed while the remote fetch is in progress.
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if change == "settings":
                sources.save_settings(conn, source_id, name="Changed", base_url="http://new.lan/opds")
            else:
                sources.disable_instance(conn, source_id)
        return Catalogue([_book()])

    monkeypatch.setattr(opds, "fetch_catalogue", fetch)
    result = await sources.sync_instance(source_id)
    assert result == {"ok": False, "code": "stale_config"}
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 0
    assert _row(source_id)["config_revision"] == 2


async def test_parallel_sync_lease_refuses_second_fetch(db, monkeypatch):
    source_id = _source()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def fetch(source, credentials):
        calls.append(source["id"])
        entered.set()
        await release.wait()
        return Catalogue([_book()])

    monkeypatch.setattr(opds, "fetch_catalogue", fetch)
    first = asyncio.create_task(sources.sync_instance(source_id))
    await entered.wait()
    try:
        assert await sources.sync_instance(source_id) == {"ok": False, "code": "busy"}
    finally:
        release.set()
    assert (await first)["status"] == "ok" and calls == [source_id]


async def test_expired_interrupted_lease_recovers(db, monkeypatch):
    source_id = _source()
    db.execute("UPDATE source_instances SET sync_token = 'interrupted-token', last_status = 'syncing', "
               "last_attempt_at = '2000-01-01 00:00:00' WHERE id = ?", (source_id,))
    db.commit()
    _catalogue(monkeypatch, [_book()])
    assert (await sources.sync_instance(source_id))["status"] == "ok"
    assert _row(source_id)["sync_token"] is None


async def test_cancellation_releases_owned_lease(db, monkeypatch):
    source_id = _source()
    entered = asyncio.Event()

    async def fetch(source, credentials):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(opds, "fetch_catalogue", fetch)
    task = asyncio.create_task(sources.sync_instance(source_id))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _row(source_id)["sync_token"] is None
    assert _row(source_id)["last_error"] == "interrupted"


async def test_decryption_is_outside_write_lock_and_failure_is_durable(db, monkeypatch):
    source_id = _source(credentials={"password": SECRET})

    def decrypt(*args, **kwargs):
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO settings (key, value) VALUES ('decrypt-probe', 'ok')")
        return ""

    monkeypatch.setattr(crypto, "decrypt_value", decrypt)
    result = await sources.sync_instance(source_id)
    assert result["code"] == "credentials_unavailable"
    assert _row(source_id)["last_error"] == "credentials_unavailable"


async def test_connection_test_reads_without_import_or_sync_timestamp_changes(db, monkeypatch):
    source_id = _source(credentials={"username": "private-reader", "password": SECRET})
    before = _row(source_id)

    async def fetch(source, credentials):
        assert source["options"] == {}
        assert credentials == {"username": "private-reader", "password": SECRET}
        return Catalogue([_book()])

    monkeypatch.setattr(opds, "fetch_catalogue", fetch)
    assert await sources.test_instance(source_id) == {"ok": True, "status": "ok", "count": 1}
    assert _row(source_id) == before
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 0


async def test_links_provider_is_honestly_manual_only(db):
    source_id = _source(provider="links", base_url="")
    assert not PROVIDERS["links"].syncable and PROVIDERS["links"].manual_links
    assert await sources.sync_instance(source_id) == {"ok": False, "code": "manual_only"}
    assert await sources.test_instance(source_id) == {"ok": False, "code": "manual_only"}
    assert _row(source_id)["last_attempt_at"] is None


def test_manual_attachment_is_idempotent_and_never_changes_local_item(db):
    source_id = _source(provider="links", base_url="")
    item_id = _insert_item(db)
    before = dict(db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone())
    record_id = sources.attach_record(db, source_id, item_id=item_id, external_id="shelf:1", open_url="https://library.example/record/1")
    assert sources.attach_record(db, source_id, item_id=item_id, external_id="shelf:1", open_url="https://library.example/record/1") == record_id
    assert dict(db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()) == before
    assert len(sources.list_records(db, source_id)) == 1


def test_manual_attachment_refuses_identity_reparenting_and_trash(db):
    source_id = _source(provider="links", base_url="")
    first, second = _insert_item(db, isbn=None), _insert_item(db, isbn=None)
    sources.attach_record(db, source_id, item_id=first, external_id="one", open_url="https://library.example/1")
    with pytest.raises(ConnectorError, match="record_conflict"):
        sources.attach_record(db, source_id, item_id=second, external_id="one", open_url="https://library.example/2")
    item_write.trash_item(db, second)
    with pytest.raises(ConnectorError, match="not_found"):
        sources.attach_record(db, source_id, item_id=second, external_id="two", open_url="https://library.example/2")


def test_disabled_instance_hides_item_availability_without_deleting_records(db):
    source_id = _source(provider="links", base_url="")
    item_id = _insert_item(db)
    sources.attach_record(db, source_id, item_id=item_id, external_id="one", open_url="https://library.example/1")
    assert sources.list_item_records(db, item_id)
    sources.disable_instance(db, source_id)
    assert sources.list_item_records(db, item_id) == []
    assert sources.list_records(db, source_id)
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 1


def _off():
    with get_db() as db:
        features.set_feature_enabled(db, "connected_library", False)


def test_sources_routes_are_registered():
    from app.main import app

    paths = {route.path for route in app.routes}
    assert {"/sources", "/sources/{source_id}", "/api/sources", "/api/sources/{source_id}/sync"} <= paths


def test_viewer_overview_is_redacted_and_admin_controls_hidden(viewer_client):
    _source(credentials={"username": "private-reader", "password": SECRET})
    response = viewer_client.get("/sources")
    assert response.status_code == 200 and "Home books" in response.text
    assert SECRET not in response.text and "private-reader" not in response.text
    assert "books.lan" not in response.text and "/api/sources" not in response.text
    assert '<form method="post" action="/api/sources"' not in response.text


@pytest.mark.parametrize("role_fixture", ["viewer_client", "editor_client"])
@pytest.mark.parametrize("suffix", ["", "/1/settings", "/1/test", "/1/sync", "/1/disable", "/1/records"])
def test_all_source_mutations_are_admin_only(request, role_fixture, suffix):
    client = request.getfixturevalue(role_fixture)
    response = client.post("/api/sources" + suffix, json={})
    assert response.status_code == 403


def test_viewer_cannot_read_source_detail(viewer_client):
    source_id = _source()
    response = viewer_client.get(f"/sources/{source_id}", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/browse"


def test_role_check_precedes_feature_gate(viewer_client):
    _off()
    response = viewer_client.post("/api/sources/1/sync", json={})
    assert response.status_code == 403 and "feature_disabled" not in response.text
    assert "Connected library" not in response.text


@pytest.mark.parametrize("suffix", ["", "/1/settings", "/1/test", "/1/sync", "/1/disable", "/1/records"])
def test_feature_off_refuses_every_source_api(admin_client, suffix):
    _off()
    response = admin_client.post("/api/sources" + suffix, json={})
    assert response.status_code == 403 and response.json()["feature"] == "connected_library"


def test_feature_off_refuses_both_source_pages(admin_client):
    _off()
    for path in ("/sources", "/sources/1"):
        response = admin_client.get(path)
        assert response.status_code == 200 and 'data-testid="feature-disabled"' in response.text


@pytest.mark.parametrize("suffix", ["", "/1/settings", "/1/test", "/1/sync", "/1/disable", "/1/records"])
def test_every_source_mutation_requires_csrf(admin_client, suffix):
    del admin_client.headers["X-CSRF-Token"]
    response = admin_client.post("/api/sources" + suffix, json={})
    assert response.status_code == 403 and "CSRF" in response.text


def test_browser_form_csrf_and_303_create_settings_flow(admin_client, db):
    del admin_client.headers["X-CSRF-Token"]
    token = admin_client.cookies.get("csrf_token")
    response = admin_client.post("/api/sources", data={"_csrf": token, "provider": "opds", "name": "Form source", "base_url": "http://books.lan/opds"}, follow_redirects=False)
    assert response.status_code == 303
    source_id = db.execute("SELECT id FROM source_instances").fetchone()[0]
    page = admin_client.get(response.headers["location"])
    assert page.status_code == 200 and "Form source" in page.text
    response = admin_client.post(f"/api/sources/{source_id}/settings", data={
        "_csrf": token, "name": "Configured", "base_url": "http://books.lan/opds", "enabled": "1",
        "username": "private-reader", "password": SECRET, "options_json": '{"media_type":"comic"}',
    }, follow_redirects=False)
    assert response.status_code == 303
    page = admin_client.get(response.headers["location"])
    assert page.status_code == 200 and "Configured" in page.text
    assert SECRET not in page.text and "private-reader" not in page.text
    assert _row(source_id)["credentials_enc"].startswith("gAAAAA")


def test_json_create_rejects_invalid_config_without_reflecting_input(admin_client):
    response = admin_client.post("/api/sources", json={"provider": "opds", "name": "Unsafe", "base_url": "http://user:" + SECRET + "@books.lan/opds"})
    assert response.status_code == 200 and not response.json()["ok"]
    assert response.json()["code"] == "invalid_url" and SECRET not in response.text


def test_get_pages_and_get_action_paths_never_sync_or_mutate(admin_client, monkeypatch, db):
    source_id = _source()
    before = _row(source_id)

    async def forbidden(*args, **kwargs):
        pytest.fail("GET must not fetch a remote catalogue")

    monkeypatch.setattr(opds, "fetch_catalogue", forbidden)
    for path in ("/sources", f"/sources/{source_id}"):
        assert admin_client.get(path).status_code == 200
    for suffix in ("sync", "test", "disable", "settings", "records"):
        assert admin_client.get(f"/api/sources/{source_id}/{suffix}").status_code == 405
    assert _row(source_id) == before
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 0


def test_sources_names_and_snapshot_titles_are_escaped_in_html(admin_client, db):
    source_id = _source(provider="links", base_url="", name='<script>alert("source")</script>')
    item_id = _insert_item(db, title='<script>alert("record")</script>')
    sources.attach_record(db, source_id, item_id=item_id, external_id="one", open_url="https://library.example/1")
    db.commit()
    response = admin_client.get(f"/sources/{source_id}")
    assert response.status_code == 200
    assert '<script>alert("source")</script>' not in response.text
    assert '<script>alert("record")</script>' not in response.text
    assert "&lt;script&gt;" in response.text


async def test_service_overall_fetch_deadline_is_durable(db, monkeypatch):
    source_id = _source()
    monkeypatch.setattr(sources, "SYNC_SECONDS", 0)

    async def fetch(source, credentials):
        await asyncio.Event().wait()

    monkeypatch.setattr(opds, "fetch_catalogue", fetch)
    result = await sources.sync_instance(source_id)
    assert result["code"] == "timeout"
    assert _row(source_id)["last_error"] == "timeout" and _row(source_id)["sync_token"] is None


def test_api_sync_and_test_use_post_and_safe_json(admin_client, monkeypatch, db):
    source_id = _source()
    _catalogue(monkeypatch, [_book()])
    test_result = admin_client.post(f"/api/sources/{source_id}/test", json={}).json()
    assert test_result["ok"] and db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 0
    sync_result = admin_client.post(f"/api/sources/{source_id}/sync", json={}).json()
    assert sync_result["ok"] and sync_result["status"] == "ok"
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 1


def test_manual_link_api_and_config_reenable(admin_client, db):
    created = admin_client.post("/api/sources", json={"provider": "links", "name": "Public library"}).json()
    assert created["ok"]
    source_id = created["id"]
    item_id = _insert_item(db)
    db.commit()
    body = {"item_id": item_id, "external_id": "public:1", "open_url": "https://library.example/catalogue?id=1"}
    attached = admin_client.post(f"/api/sources/{source_id}/records", json=body).json()
    assert attached["ok"] and sources.list_item_records(db, item_id)
    assert admin_client.post(f"/api/sources/{source_id}/disable", json={}).json()["ok"]
    assert sources.list_item_records(db, item_id) == []
    enabled = admin_client.post(f"/api/sources/{source_id}/settings", json={"name": "Public library", "enabled": True}).json()
    assert enabled["ok"] and sources.list_item_records(db, item_id)


def test_json_and_form_error_never_reflect_unknown_diagnostics(admin_client, monkeypatch):
    source_id = _source()

    async def fail(*args):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(opds, "fetch_catalogue", fail)
    response = admin_client.post(f"/api/sources/{source_id}/test", json={})
    assert not response.json()["ok"] and SECRET not in response.text
    response = admin_client.post(f"/api/sources/{source_id}/test", data={})
    assert response.status_code == 200 and 'role="status"' in response.text
    assert SECRET not in response.text


async def test_actual_opds_fixture_imports_without_any_live_network(db, monkeypatch):
    from tests.test_opds_connector import atom, entry, install_transport

    requests = install_transport(monkeypatch, lambda request: httpx.Response(200, content=atom(entry())))
    source_id = _source()
    result = await sources.sync_instance(source_id)
    assert result["status"] == "ok" and result["summary"]["added"] == 1
    assert requests[0].url.host == "books.lan"
    assert sources.list_records(db, source_id)[0]["external_id"] == "opaque:1"


@pytest.mark.parametrize("echo", ['private%252Dpassword%252Dnot%252Dfor%252Dpages', SECRET])
async def test_encoded_reflected_secrets_are_not_stored(db, monkeypatch, echo):
    source_id = _source(credentials={"password": SECRET})
    _catalogue(monkeypatch, [_book(metadata={"description": echo})])
    result = await sources.sync_instance(source_id)
    assert result["status"] == "partial" and result["summary"]["warnings"] == ["invalid_record"]
    assert db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 0


async def test_service_record_cap_is_partial_and_preserves_missing_availability(db, monkeypatch):
    source_id = _source()
    catalogue = _catalogue(monkeypatch, [_book("old")])
    await sources.sync_instance(source_id)
    monkeypatch.setattr(sources, "MAX_RECORDS", 1)
    catalogue.records[:] = [_book("new-one"), _book("new-two")]
    result = await sources.sync_instance(source_id)
    assert result["status"] == "partial" and result["summary"]["warnings"] == ["limit_reached"]
    assert db.execute("SELECT availability FROM source_records WHERE external_id = 'old'").fetchone()[0] == "available"


def test_partial_json_settings_preserve_omitted_configuration(admin_client):
    source_id = _source(options={"media_type": "manga"}, credentials={"password": SECRET})
    before = _row(source_id)
    response = admin_client.post(f"/api/sources/{source_id}/settings", json={"name": "Renamed only"}).json()
    after = _row(source_id)
    assert response["ok"] and after["name"] == "Renamed only"
    for key in ("base_url", "options_json", "credentials_enc", "enabled"):
        assert after[key] == before[key]


def _provenance(db, item_id, source_id, identity, field):
    key = sources.metadata_source_key(source_id, identity)
    row = db.execute("SELECT value_json FROM item_metadata_provenance WHERE item_id = ? AND source_key = ? AND field_name = ?",
                     (item_id, key, field)).fetchone()
    assert row is not None
    return json.loads(row[0])


def _save(source_id, **overrides):
    current = _row(source_id)
    fields = {"name": current["name"], "base_url": current["base_url"],
              "browser_url": current["browser_url"] or "", "options": json.loads(current["options_json"]),
              "enabled": bool(current["enabled"]), **overrides}
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        sources.save_settings(db, source_id, **fields)


@pytest.mark.parametrize("seconds_field", ["duration", "duration_seconds"])
async def test_new_import_projects_valid_descriptions_and_records_initial_provenance(db, monkeypatch, seconds_field):
    source_id = _source()
    identity = "urn:uuid:三体/opaque ?&=identifier"
    metadata = {"subtitle": "A subtitle", "description": "A description", "publisher": "A publisher",
                "language": "fr", "year": 2026, "series_name": "A series", "series_position": 2.5,
                "pages": 321, seconds_field: 7530}
    _catalogue(monkeypatch, [_book(identity, authors="A Writer", metadata=metadata)])
    assert (await sources.sync_instance(source_id))["status"] == "ok"
    item = db.execute("SELECT * FROM items_live").fetchone()
    expected = {"title": "Remote book", "authors": "A Writer", "subtitle": "A subtitle",
                "description": "A description", "publisher": "A publisher", "language": "fr",
                "publish_year": 2026, "series_name": "A series", "series_position": 2.5,
                "page_count": 321, "duration_mins": 125}
    assert {name: item[name] for name in expected} == expected
    for name, value in expected.items():
        if name == "subtitle":  # Creation-only until shared policy supports it.
            continue
        snapshot = _provenance(db, item["id"], source_id, identity, name)
        assert snapshot == {"observed": value, "applied": value, "local": value, "overridden": False}
    review = metadata_review.context_for(db, item["id"])
    assert any(r["field_name"] == "description" for r in review["metadata_rows"])
    key = sources.metadata_source_key(source_id, identity)
    assert len(key) <= 256 and "三体" not in key
    assert key != sources.metadata_source_key(source_id + 1, identity)


async def test_unedited_provider_fields_follow_changes_without_identity_or_personal_mutations(db, monkeypatch):
    source_id = _source()
    catalogue = _catalogue(monkeypatch, [_book(isbn=ISBN, authors="First Writer", metadata={"description": "First", "year": 2001})])
    await sources.sync_instance(source_id)
    item_id = db.execute("SELECT item_id FROM source_records").fetchone()[0]
    catalogue.records[:] = [_book(title="Revised title", isbn="9780441172719", media_type="audiobook", authors="Second Writer",
                                 metadata={"description": "Second", "year": 2002, "publisher": "New publisher", "duration": 3600})]
    result = await sources.sync_instance(source_id)
    assert result["summary"]["updated"] == 1
    item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    assert (item["title"], item["authors"], item["description"], item["publish_year"]) == ("Revised title", "Second Writer", "Second", 2002)
    assert item["publisher"] == "New publisher" and item["duration_mins"] == 60
    assert item["isbn"] == ISBN and item["media_type"] == "ebook" and item["owned"] == 0
    assert item["location_id"] is None and item["cover_path"] is None
    assert db.execute("SELECT COUNT(*) FROM list_items").fetchone()[0] == 0
    db.execute("UPDATE items SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (item_id,))
    db.commit()
    await sources.sync_instance(source_id)
    assert db.execute("SELECT updated_at FROM items_live WHERE id = ?", (item_id,)).fetchone()[0] == "2000-01-01 00:00:00"


async def test_adoption_fills_genuine_blanks_but_preserves_nonempty_fields_and_explicit_clears(db, monkeypatch):
    item_id = _insert_item(db, title="My title", isbn=ISBN, media_type="ebook", description="My description")
    db.commit()
    source_id = _source()
    catalogue = _catalogue(monkeypatch, [_book(isbn=ISBN, authors="Source writer", metadata={"description": "Source description", "language": "fr"})])
    assert (await sources.sync_instance(source_id))["summary"]["linked"] == 1
    item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    assert item["title"] == "My title" and item["description"] == "My description"
    assert item["authors"] == "Source writer" and item["language"] == "fr"
    assert "applied" not in _provenance(db, item_id, source_id, "opaque:1", "description")
    item_write.update_item_fields(db, item_id, {"description": None, "language": ""})
    db.commit()
    catalogue.records[:] = [_book(isbn=ISBN, metadata={"description": "New source description", "language": "en"})]
    for _ in range(2):
        await sources.sync_instance(source_id)
    item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    assert item["description"] is None and item["language"] == ""
    assert _provenance(db, item_id, source_id, "opaque:1", "description")["overridden"] is True
    assert _provenance(db, item_id, source_id, "opaque:1", "language")["overridden"] is True


async def test_explicit_follow_source_on_adopted_item_follows_future_syncs(db, monkeypatch):
    item_id = _insert_item(db, isbn=ISBN, media_type="ebook", description="My description")
    db.commit()
    source_id = _source()
    catalogue = _catalogue(monkeypatch, [_book(isbn=ISBN, metadata={"description": "Source one"})])
    await sources.sync_instance(source_id)
    row = next(r for r in metadata_review.context_for(db, item_id)["metadata_rows"] if r["field_name"] == "description")
    db.execute("BEGIN IMMEDIATE")
    metadata_review.resolve_field(db, item_id, row["source_key"], "description", "follow_source", row["fingerprint"])
    db.commit()
    catalogue.records[:] = [_book(isbn=ISBN, metadata={"description": "Source two"})]
    await sources.sync_instance(source_id)
    assert db.execute("SELECT description FROM items_live WHERE id = ?", (item_id,)).fetchone()[0] == "Source two"


async def test_legacy_attachment_has_no_claim_on_existing_nonempty_fields(db, monkeypatch):
    source_id = _source()
    item_id = _insert_item(db, title="Remote book", isbn=None, media_type="ebook", description="Legacy description")
    db.execute("INSERT INTO source_records (source_id, item_id, external_id, title, media_type) VALUES (?, ?, 'opaque:1', 'Remote book', 'ebook')",
               (source_id, item_id))
    db.commit()
    catalogue = _catalogue(monkeypatch, [_book(metadata={"description": "Legacy description"})])
    await sources.sync_instance(source_id)
    catalogue.records[:] = [_book(title="Changed remote", metadata={"description": "New description"})]
    await sources.sync_instance(source_id)
    item = db.execute("SELECT title, description FROM items_live WHERE id = ?", (item_id,)).fetchone()
    assert tuple(item) == ("Remote book", "Legacy description")
    assert "applied" not in _provenance(db, item_id, source_id, "opaque:1", "description")


async def test_nested_provider_identifiers_are_bounded_shaped_and_searchable(db, monkeypatch):
    from app.services import library_catalog

    source_id = _source()
    music_id = "12345678-1234-4321-abcd-1234567890ab"
    _catalogue(monkeypatch, [_book(metadata={"provider_ids": {
        "Imdb": "tt7654321", "Tmdb": "998877", "Tvdb": "12345", "MusicBrainzAlbum": music_id,
        "Isbn13": ISBN, "Arbitrary": SECRET, "Token": SECRET, "Asin": "B012345678",
        "MusicBrainzReleaseGroup": "https://bad.example/?token=secret",
    }})])
    await sources.sync_instance(source_id)
    stored = json.loads(db.execute("SELECT metadata_json FROM source_records").fetchone()[0])
    assert stored["provider_ids"] == {"Imdb": "tt7654321", "Tmdb": "998877", "Tvdb": "12345", "MusicBrainzAlbum": music_id, "Isbn13": ISBN, "Asin": "B012345678"}
    for identifier in ("tt7654321", "imdb:tt7654321", "998877", music_id, ISBN):
        assert library_catalog.search_catalog(db, {"q": identifier})["matching_count"] == 1
    assert library_catalog.search_catalog(db, {"q": SECRET})["matching_count"] == 0


async def test_nested_identifier_credential_reflection_rejects_record(db, monkeypatch):
    source_id = _source(credentials={"password": "tt7654321"})
    _catalogue(monkeypatch, [_book(metadata={"provider_ids": {"Imdb": "tt7654321"}})])
    result = await sources.sync_instance(source_id)
    assert result["status"] == "partial" and result["summary"]["warnings"] == ["invalid_record"]
    assert db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM item_metadata_provenance").fetchone()[0] == 0


async def test_invalid_descriptive_values_are_dropped_without_poisoning_other_records(db, monkeypatch):
    source_id = _source()
    _catalogue(monkeypatch, [_book("bad-metadata", metadata={
        "publisher": ["not a string"], "description": {"arbitrary": "object"}, "year": True,
        "pages": -1, "series_position": float("inf"), "duration": float("nan"),
    }), _book("good-metadata", metadata={"description": "Good", "year": 2026, "pages": 100})])
    result = await sources.sync_instance(source_id)
    assert result["status"] == "ok" and result["summary"]["added"] == 2
    item = db.execute("SELECT i.* FROM items_live i JOIN source_records r ON r.item_id = i.id WHERE r.external_id = 'bad-metadata'").fetchone()
    for field in ("publisher", "description", "publish_year", "page_count", "series_position", "duration_mins"):
        assert item[field] is None


async def test_url_shaped_opds_id_cannot_wrongly_adopt_an_existing_isbn(db, monkeypatch):
    from tests.test_opds_connector import atom, entry, install_transport

    item_id = _insert_item(db, isbn=ISBN, media_type="ebook")
    db.commit()
    install_transport(monkeypatch, lambda request: httpx.Response(200, content=atom(entry("https://example.test/books/" + ISBN))))
    source_id = _source()
    result = await sources.sync_instance(source_id)
    assert result["summary"]["added"] == 1 and result["summary"]["linked"] == 0
    assert db.execute("SELECT item_id FROM source_records").fetchone()[0] != item_id


@pytest.mark.parametrize("changed", [
    {"base_url": "http://other.lan/opds"}, {"browser_url": "https://browser.example/opds"},
    {"options": {"media_type": "manga"}}, {"options": {"max_pages": 1}},
    {"credentials": {"password": "replacement-password"}}, {"clear_credentials": True},
])
async def test_material_config_change_resets_health_and_cached_availability_until_observed(db, monkeypatch, changed):
    source_id = _source(credentials={"password": SECRET})
    catalogue = _catalogue(monkeypatch, [_book("one"), _book("two")])
    await sources.sync_instance(source_id)
    assert _row(source_id)["last_sync_at"]
    _save(source_id, **changed)
    state = _row(source_id)
    assert state["last_status"] == "never" and state["last_sync_at"] is None and state["last_attempt_at"] is None
    assert state["last_error"] is None and json.loads(state["last_summary_json"]) == {}
    assert {r[0] for r in db.execute("SELECT availability FROM source_records")} == {"unknown"}
    catalogue.records[:] = [_book("one")]
    catalogue.complete = False
    await sources.sync_instance(source_id)
    assert _row(source_id)["last_sync_at"] is None
    assert db.execute("SELECT availability FROM source_records WHERE external_id = 'two'").fetchone()[0] == "unknown"

    async def fail(*args):
        raise ConnectorError("auth_failed")

    monkeypatch.setattr(opds, "fetch_catalogue", fail)
    await sources.sync_instance(source_id)
    assert db.execute("SELECT availability FROM source_records WHERE external_id = 'two'").fetchone()[0] == "unknown"
    _catalogue(monkeypatch, [_book("one")])
    await sources.sync_instance(source_id)
    assert _row(source_id)["last_sync_at"]
    assert db.execute("SELECT availability FROM source_records WHERE external_id = 'two'").fetchone()[0] == "unavailable"


async def test_cosmetic_and_effectively_identical_settings_preserve_health_and_blank_credentials(db, monkeypatch):
    source_id = _source(credentials={"username": "reader", "password": SECRET})
    _catalogue(monkeypatch, [_book()])
    await sources.sync_instance(source_id)
    before = _row(source_id)
    _save(source_id, name="New display name", credentials={"username": "", "password": ""}, options={
        "media_type": "ebook", "max_pages": 20, "max_entries": 2000, "max_depth": 2,
    })
    after = _row(source_id)
    for field in ("last_status", "last_sync_at", "last_attempt_at", "last_summary_json", "credentials_enc"):
        assert after[field] == before[field]
    assert db.execute("SELECT availability FROM source_records").fetchone()[0] == "available"
    db.execute("UPDATE source_instances SET last_status = 'error', last_error = 'auth_failed' WHERE id = ?", (source_id,))
    db.commit()
    _save(source_id, name="Another display name")
    assert _row(source_id)["last_status"] == "error" and _row(source_id)["last_error"] == "auth_failed"


def test_manual_links_remain_usable_after_disable_reenable_and_root_changes(db):
    source_id = _source(provider="links", base_url="")
    item_id = _insert_item(db)
    sources.attach_record(db, source_id, item_id=item_id, external_id="manual:1", open_url="https://library.example/record/1")
    sources.disable_instance(db, source_id)
    db.commit()
    _save(source_id, enabled=True, base_url="https://other.example/library", browser_url="https://public.example/library")
    assert sources.list_item_records(db, item_id)[0]["availability"] == "available"
    assert sources.list_item_records(db, item_id)[0]["open_url"] == "https://library.example/record/1"


async def test_jellyfin_library_scope_resets_but_reordered_identical_selection_does_not(db, monkeypatch):
    source_id = _source(provider="jellyfin", base_url="http://films.lan", credentials={"token": SECRET},
                        options={"user_id": "user-1", "library_ids": ["one", "two"]})
    _catalogue(monkeypatch, [_book(media_type="digital_video")], provider="jellyfin")
    await sources.sync_instance(source_id)
    before = _row(source_id)
    _save(source_id, options={"user_id": "user-1", "library_ids": ["two", "one", "one"]})
    assert _row(source_id)["last_sync_at"] == before["last_sync_at"]
    assert db.execute("SELECT availability FROM source_records").fetchone()[0] == "available"
    _save(source_id, options={"user_id": "user-1", "library_ids": ["two"]})
    assert _row(source_id)["last_sync_at"] is None and _row(source_id)["last_status"] == "never"
    assert db.execute("SELECT availability FROM source_records").fetchone()[0] == "unknown"


async def test_real_jellyfin_adapter_projection_and_identifier_search(db):
    import respx
    from app.services import library_catalog

    root = "http://films.lan/jellyfin"
    music_id = "12345678-1234-4321-abcd-1234567890ab"
    source_id = _source(provider="jellyfin", base_url=root, credentials={"token": SECRET}, options={"user_id": "user-1"})
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as server:
        server.get(f"{root}/Users/user-1/Views").respond(200, json={"Items": [{"Id": "movies"}], "TotalRecordCount": 1})
        server.get(f"{root}/Items").respond(200, json={"Items": [{
            "Id": "movie-1", "Type": "Movie", "Name": "Actual movie", "Overview": "Remote synopsis",
            "ProductionYear": 2026, "RunTimeTicks": 7500 * 10000000,
            "ProviderIds": {"Imdb": "tt7654321", "Tmdb": "998877", "MusicBrainzAlbum": music_id},
        }], "TotalRecordCount": 1})
        result = await sources.sync_instance(source_id)
    assert result["status"] == "ok"
    item = db.execute("SELECT * FROM items_live").fetchone()
    assert (item["description"], item["publish_year"], item["duration_mins"]) == ("Remote synopsis", 2026, 125)
    for query in ("imdb:tt7654321", "tmdb:998877", music_id):
        assert library_catalog.search_catalog(db, {"q": query})["matching_count"] == 1


async def test_record_validation_failure_rolls_back_only_that_item_and_its_provenance(db, monkeypatch):
    source_id = _source()
    _catalogue(monkeypatch, [_book("bad", title="Bad record"), _book("good", title="Good record")])
    original = sources.metadata_policy.record_initial

    def fail_one(db, item_id, key, fields):
        original(db, item_id, key, fields)
        if fields["title"] == "Bad record":
            raise ValueError("invalid descriptive provider value")

    monkeypatch.setattr(sources.metadata_policy, "record_initial", fail_one)
    result = await sources.sync_instance(source_id)
    assert result["status"] == "partial" and result["summary"]["skipped"] == 1 and result["summary"]["added"] == 1
    assert [r[0] for r in db.execute("SELECT title FROM items_live")] == ["Good record"]
    assert db.execute("SELECT COUNT(*) FROM item_metadata_provenance WHERE source_key = ?",
                      (sources.metadata_source_key(source_id, "bad"),)).fetchone()[0] == 0


@pytest.mark.parametrize("broken", ["atom-next", "atom-subsection", "json-next", "json-subsection"])
async def test_real_opds_malformed_traversal_preserves_prior_missing_record(db, monkeypatch, broken):
    from tests.test_opds_connector import atom, entry, install_transport, publication

    phase = [0]

    def handler(request):
        if not phase[0]:
            return httpx.Response(200, content=atom(entry("A") + entry("B")))
        if broken.startswith("atom"):
            relation = broken.split("-", 1)[1]
            return httpx.Response(200, content=atom(entry("A"), f'<link rel="{relation}"/>'))
        relation = broken.split("-", 1)[1]
        return httpx.Response(200, json={"publications": [publication("A")], "links": [{"rel": relation, "href": None}]})

    requests = install_transport(monkeypatch, handler)
    source_id = _source()
    assert (await sources.sync_instance(source_id))["status"] == "ok"
    last_success = _row(source_id)["last_sync_at"]
    phase[0] = 1
    result = await sources.sync_instance(source_id)
    assert result["status"] == "partial" and "malformed_response" in result["summary"]["warnings"]
    assert result["summary"]["unavailable"] == 0 and _row(source_id)["last_sync_at"] == last_success
    assert db.execute("SELECT availability FROM source_records WHERE external_id = 'B'").fetchone()[0] == "available"
    assert len(requests) == 2


async def test_real_opds_redirect_escape_never_fetches_private_path_or_reconciles_missing(db, monkeypatch):
    from tests.test_opds_connector import install_transport, publication

    phase = [0]

    def handler(request):
        assert request.url.path != "/opds/private"
        if request.url.path == "/opds/public/books":
            return httpx.Response(302, headers={"location": "/opds/private"})
        assert request.url.path == "/opds/public"
        if not phase[0]:
            return httpx.Response(200, json={"publications": [publication("A"), publication("B")]})
        return httpx.Response(200, json={"publications": [publication("A")], "navigation": [{"href": "/opds/public/books"}]})

    requests = install_transport(monkeypatch, handler)
    source_id = _source(base_url="http://books.lan/opds/public", credentials={"username": "reader", "password": SECRET})
    assert (await sources.sync_instance(source_id))["status"] == "ok"
    phase[0] = 1
    result = await sources.sync_instance(source_id)
    assert result["status"] == "partial" and "unsafe_redirect" in result["summary"]["warnings"]
    assert db.execute("SELECT availability FROM source_records WHERE external_id = 'B'").fetchone()[0] == "available"
    assert len(requests) == 3


@pytest.mark.parametrize("declared", [2, -1, "2", True])
async def test_real_opds_group_counts_cannot_produce_false_complete_reconciliation(db, monkeypatch, declared):
    from tests.test_opds_connector import install_transport, publication

    phase = [0]

    def handler(request):
        if not phase[0]:
            return httpx.Response(200, json={"publications": [publication("A"), publication("B")]})
        return httpx.Response(200, json={"metadata": {"numberOfItems": 1}, "groups": [{
            "metadata": {"numberOfItems": declared}, "publications": [publication("A")],
        }]})

    requests = install_transport(monkeypatch, handler)
    source_id = _source()
    assert (await sources.sync_instance(source_id))["status"] == "ok"
    phase[0] = 1
    result = await sources.sync_instance(source_id)
    assert result["status"] == "partial" and result["summary"]["unavailable"] == 0
    assert db.execute("SELECT availability FROM source_records WHERE external_id = 'B'").fetchone()[0] == "available"
    assert len(requests) == 2


async def test_real_opds_fully_enumerated_group_counts_allow_complete_reconciliation(db, monkeypatch):
    from tests.test_opds_connector import install_transport, publication

    phase = [0]

    def handler(request):
        if not phase[0]:
            return httpx.Response(200, json={"publications": [publication("A"), publication("B"), publication("C")]})
        return httpx.Response(200, json={"metadata": {"numberOfItems": 2}, "groups": [
            {"metadata": {"numberOfItems": 1}, "publications": [publication("A")]},
            {"metadata": {"numberOfItems": 1}, "publications": [publication("B")]},
        ]})

    install_transport(monkeypatch, handler)
    source_id = _source()
    await sources.sync_instance(source_id)
    phase[0] = 1
    result = await sources.sync_instance(source_id)
    assert result["status"] == "ok" and result["summary"]["unavailable"] == 1
    assert db.execute("SELECT availability FROM source_records WHERE external_id = 'C'").fetchone()[0] == "unavailable"
    assert {r[0] for r in db.execute("SELECT availability FROM source_records WHERE external_id IN ('A', 'B')")} == {"available"}


@pytest.mark.parametrize("channel", ["title", "link"])
@pytest.mark.parametrize("representation", ["base64", "basic-prefix", "percent", "html"])
async def test_real_opds_basic_credential_echo_never_reaches_catalogue_snapshots_or_rendered_dtos(db, monkeypatch, admin_client, channel, representation):
    from tests.test_opds_connector import atom, entry, install_transport

    basic = base64.b64encode(f"reader:{SECRET}".encode()).decode()

    def handler(request):
        assert request.headers["authorization"] == "Basic " + basic
        echo = "Basic " + basic if representation == "basic-prefix" else basic
        if representation == "percent":
            echo = quote(quote(echo, safe=""), safe="")
        elif representation == "html":
            echo = "".join(f"&#{ord(c)};" for c in echo)
        if channel == "title":
            payload = atom(entry("echo-record", title="Echo " + echo))
        else:
            payload = atom(entry("echo-record", links=f'<link rel="{opds.ACQUISITION}" href="https://public.example/book?id={quote(echo, safe="")}"/>'))
        return httpx.Response(200, content=payload)

    install_transport(monkeypatch, handler)
    source_id = _source(credentials={"username": "reader", "password": SECRET})
    result = await sources.sync_instance(source_id)
    assert result["status"] == "partial" and "invalid_record" in result["summary"]["warnings"]
    for table in ("items_live", "source_records", "item_metadata_provenance"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    dto = json.dumps([sources.get_instance(db, source_id), sources.list_instances(db), sources.list_records(db, source_id)])
    page = admin_client.get(f"/sources/{source_id}")
    assert page.status_code == 200
    for secret in (SECRET, basic, "Basic " + basic):
        assert secret not in dto and secret not in page.text and secret not in json.dumps(result)


@pytest.mark.parametrize("password", [ISBN, "0306406152", "0-306-40615-2"])
async def test_real_opds_isbn_shaped_password_cannot_be_persisted_as_separate_isbn(db, monkeypatch, password):
    from tests.test_opds_connector import atom, entry, install_transport

    install_transport(monkeypatch, lambda request: httpx.Response(200, content=atom(entry(
        "opaque-record", extra=f"<dc:identifier>urn:isbn:{password}</dc:identifier>"))))
    source_id = _source(credentials={"username": "reader", "password": password})
    result = await sources.sync_instance(source_id)
    assert result["status"] == "partial" and result["summary"]["warnings"] == ["invalid_record"]
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 0


async def test_compressed_opds_response_preserves_prior_holdings_without_body_read(db, monkeypatch):
    from tests.test_opds_connector import atom, entry, install_transport

    phase, reads = [0], []

    class CompressedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append(True)
            raise AssertionError("Compressed body must not be consumed")
            yield b"unreachable"

    def handler(request):
        if not phase[0]:
            return httpx.Response(200, content=atom(entry("A") + entry("B")))
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=CompressedBody())

    install_transport(monkeypatch, handler)
    source_id = _source()
    await sources.sync_instance(source_id)
    last_success = _row(source_id)["last_sync_at"]
    phase[0] = 1
    result = await sources.sync_instance(source_id)
    assert result["code"] == "unsupported_encoding" and reads == []
    assert _row(source_id)["last_sync_at"] == last_success
    assert _row(source_id)["last_error"] == "unsupported_encoding"
    assert {r[0] for r in db.execute("SELECT availability FROM source_records")} == {"available"}
