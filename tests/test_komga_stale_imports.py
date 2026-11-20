"""Future disposable-resource Komga absence/visibility regressions; not run here.

All remote responses are synthetic. URL/404 reports are not edition identity
proof; neither this module nor the implementation maps old IDs onto new IDs.
"""

import httpx
import pytest

from app.services import komga_books, komga_libraries, komga_presence, komga_records, komga_sync, library_catalog, metadata_policy
from tests.test_connected_layout import _Markup
from tests.test_komga_records import _candidate
from tests.test_media_navigation import _feature
from tests.test_personal_library import _save
from tests.test_sources import _source


SERVER = "https://komga.example"


def _seed(db):
    komga_sync.save_configuration(url=SERVER, api_key="synthetic-komga-token")
    old = _candidate(komga_id="old-id", komga_library_id="lib", library_kind="comic", isbn=None,
                     title="Retained old issue", series_name="Test series", series_position=1)
    live = {**old, "komga_id": "current-id", "title": "Current issue"}
    old_item = komga_records.persist_candidate(db, old)["item_id"]
    current_item = komga_records.persist_candidate(db, live)["item_id"]
    db.commit()
    return old_item, current_item, live


def _complete(db, seen=("current-id",)):
    metadata_policy.begin_write(db)
    revision = komga_presence.revision(db)
    token = komga_presence.start(db, revision)
    for identity in seen:
        komga_presence.observed(db, identity, "lib", SERVER)
    count = komga_presence.reconcile(db, "lib", SERVER, set(seen), expected=revision, token=token)
    db.commit()
    return count


def _retained(db):
    tables = ("items", "komga_records", "item_copies", "item_metadata_provenance", "user_item_state",
              "user_item_events", "reading_log", "reading_sessions")
    return {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")) for table in tables}


def test_completed_scope_hides_old_source_only_import_without_touching_identity_or_private_history(db, viewer_user):
    old, current, _ = _seed(db)
    _save(db, viewer_user["id"], old, status="in_progress", progress="37", notes="Private retained progress")
    before = _retained(db)
    assert _complete(db) == 1
    assert _retained(db) == before
    assert {row[0] for row in db.execute("SELECT id FROM catalogue_items_live")} == {current}
    assert {row[0] for row in db.execute("SELECT id FROM items_live")} == {old, current}
    assert komga_presence.states(db, ["old-id", "current-id"]) == {"old-id": "unavailable", "current-id": "available"}
    assert _complete(db) == 0
    assert _retained(db) == before


@pytest.mark.parametrize("protection", ["manual", "copy", "trashed-copy", "location", "abs", "romm", "other-source", "unknown-komga"])
def test_physical_manual_and_other_holdings_prevent_hiding_even_when_komga_is_missing(db, protection):
    from app.services import item_write, sources
    from tests.conftest import _insert_location

    old, _, _ = _seed(db)
    if protection == "manual":
        item_write.update_item_fields(db, old, {"source": "manual"})
    elif protection in {"copy", "trashed-copy"}:
        db.execute("INSERT INTO item_copies (item_id, copy_number, deleted_at) VALUES (?, 1, ?)",
                   (old, "2026-01-01" if protection == "trashed-copy" else None))
    elif protection == "location":
        item_write.update_item_fields(db, old, {"location_id": _insert_location(db)})
    elif protection == "abs":
        item_write.update_item_fields(db, old, {"abs_id": "other-abs-id"})
    elif protection == "romm":
        db.execute("INSERT INTO romm_records (romm_id, item_id, platform_id) VALUES ('other-rom', ?, 'platform')", (old,))
    elif protection == "other-source":
        db.commit()
        source_id = _source(provider="links")
        metadata_policy.begin_write(db)
        sources.attach_record(db, source_id, item_id=old, external_id="other", open_url="https://reader.example/book")
    else:
        db.execute("INSERT INTO komga_records (komga_id, item_id, library_id, kind) VALUES ('unknown-id', ?, 'other-lib', 'comic')", (old,))
    db.commit()
    _complete(db)
    assert db.execute("SELECT id FROM catalogue_items_live WHERE id = ?", (old,)).fetchone() is not None


def test_returning_id_becomes_visible_without_id_reassignment_and_config_changes_invalidate_absence(db):
    old, _, _ = _seed(db)
    _complete(db)
    metadata_policy.begin_write(db)
    komga_presence.observed(db, "old-id", "lib", SERVER)
    db.commit()
    assert db.execute("SELECT id FROM catalogue_items_live WHERE id = ?", (old,)).fetchone()
    _complete(db)
    komga_sync.save_configuration(url="https://replacement.example", api_key="different-synthetic-token")
    assert komga_presence.states(db, ["old-id"])["old-id"] == "unknown"
    assert db.execute("SELECT id FROM catalogue_items_live WHERE id = ?", (old,)).fetchone()


def test_changed_settings_and_superseded_generations_refuse_before_absence_writes(db):
    _seed(db)
    metadata_policy.begin_write(db)
    revision = komga_presence.revision(db)
    first = komga_presence.start(db, revision)
    second = komga_presence.start(db, revision)
    before = _retained(db)
    with pytest.raises(komga_libraries.KomgaError, match="superseded"):
        komga_presence.reconcile(db, "lib", SERVER, set(), expected=revision, token=first)
    assert _retained(db) == before and db.execute("SELECT COUNT(*) FROM komga_presence").fetchone()[0] == 0
    db.execute("UPDATE settings SET value = '[] changed scope' WHERE key = 'komga_excluded_libraries'")
    # Missing settings are also revision changes; ensure a row exists in this fixture.
    db.execute("INSERT INTO settings (key, value) VALUES ('komga_excluded_libraries', 'changed') "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    with pytest.raises(komga_libraries.KomgaError, match="settings changed"):
        komga_presence.reconcile(db, "lib", SERVER, set(), expected=revision, token=second)
    assert _retained(db) == before and db.execute("SELECT COUNT(*) FROM komga_presence").fetchone()[0] == 0


def test_changing_configuration_back_does_not_restore_an_older_scans_authority(db):
    _seed(db)
    metadata_policy.begin_write(db)
    revision = komga_presence.revision(db)
    token = komga_presence.start(db, revision)
    db.commit()
    komga_sync.save_configuration(url="https://temporary.example")
    komga_sync.save_configuration(url=SERVER)
    metadata_policy.begin_write(db)
    assert komga_presence.revision(db) == revision
    with pytest.raises(komga_libraries.KomgaError, match="superseded"):
        komga_presence.reconcile(db, "lib", SERVER, set(), expected=revision, token=token)
    assert db.execute("SELECT COUNT(*) FROM komga_presence").fetchone()[0] == 0


def test_visibility_is_shared_by_browse_search_series_recent_but_unavailable_details_and_history_remain(viewer_client, viewer_user, db):
    old, current, _ = _seed(db)
    _feature(db, "komga", True)
    _feature(db, "connected_library", True)
    _save(db, viewer_user["id"], old, status="in_progress", progress="17")
    _complete(db)
    before = _retained(db)
    for path in ("/browse", "/api/search", "/series", "/"):
        response = viewer_client.get(path)
        assert response.status_code == 200
        markup = _Markup(response.text)
        if path == "/":
            row = markup.one("ul", id="home-recent-row")
            ids = {node.attrs["data-item-id"] for node in markup.find("article", within=row)}
        elif path == "/series":
            ids = {node.attrs["href"].split("/")[2].split("?")[0] for node in markup.find("a")
                   if node.attrs.get("href", "").startswith("/item/")}
        else:
            ids = {node.attrs["data-item-id"] for node in markup.elements if "data-item-id" in node.attrs}
        assert str(old) not in ids and str(current) in ids
    default = library_catalog.search_catalog(db, {"view": "items"})
    unavailable = library_catalog.search_catalog(db, {"view": "items", "availability": "unavailable"})
    assert default["matching_count"] == 1
    assert unavailable["matching_count"] == 1
    assert viewer_client.get(f"/item/{old}").status_code == 200
    assert "Retained old issue" in viewer_client.get("/activity").text
    assert komga_sync.item_action(old) is None and komga_sync.item_action(current) is not None
    assert _retained(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("complete,later_failure", [(True, False), (False, False), (True, True)])
async def test_sync_reconciles_only_after_every_selected_library_succeeds(db, monkeypatch, complete, later_failure):
    old, current, candidate = _seed(db)

    async def libraries(*args, **kwargs):
        return [{"id": identity, "kind": "comic"} for identity in (["lib", "broken"] if later_failure else ["lib"])]
    async def candidates(*args, library_id, **kwargs):
        if library_id == "broken":
            raise komga_libraries.KomgaError("Synthetic later library failure")
        return komga_books.BookBatch([candidate], complete=complete)
    async def no_cover(*args, **kwargs):
        return False
    monkeypatch.setattr(komga_sync.komga_libraries, "fetch_libraries", libraries)
    monkeypatch.setattr(komga_sync.komga_books, "fetch_library_candidates", candidates)
    monkeypatch.setattr(komga_sync, "_ingest_cover", no_cover)
    result = await komga_sync.sync()
    hidden = complete and not later_failure
    assert result.get("unavailable", 0) == int(hidden)
    assert bool(db.execute("SELECT id FROM catalogue_items_live WHERE id = ?", (old,)).fetchone()) is not hidden
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 2
    assert db.execute("SELECT item_id FROM komga_records WHERE komga_id = 'current-id'").fetchone()[0] == current


@pytest.mark.asyncio
@pytest.mark.parametrize("pages,complete", [
    ([{"content": [{"id": "one"}], "last": True, "totalPages": 1, "totalElements": 1}], True),
    ([{"content": [], "last": True, "totalPages": 0, "totalElements": 0}], True),
    ([{"content": [{"id": "one"}], "last": False}, {"content": [{"id": "one"}], "last": True}], False),
    ([{"content": [], "last": False}], False),
    ([{"content": [{"id": "one"}], "totalPages": 3}, {"content": [{"id": "two"}], "last": True}], False),
    ([{"content": [{"id": "one"}], "totalElements": 2, "totalPages": 2},
      {"content": [{"id": "two"}], "totalElements": 3, "totalPages": 2, "last": True}], False),
    ([{"content": [{"id": "one", "libraryId": "wrong"}], "last": True}], False),
])
async def test_page_completion_evidence_is_separate_from_deduplicated_results(respx_mock, pages, complete):
    route = respx_mock.post(SERVER + "/api/v1/books/list").mock(side_effect=[httpx.Response(200, json=page) for page in pages])
    async with httpx.AsyncClient() as client:
        batch = await komga_books.fetch_library_books(client, SERVER, "synthetic-token", "lib")
    assert isinstance(batch, list) and batch.complete is complete
    assert route.call_count == len(pages)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"last": True}, {"content": None, "last": True}, {"content": [], "last": "true"}])
async def test_malformed_response_cannot_authorize_absence(respx_mock, payload):
    respx_mock.post(SERVER + "/api/v1/books/list").respond(200, json=payload)
    async with httpx.AsyncClient() as client:
        with pytest.raises(komga_libraries.KomgaError):
            await komga_books.fetch_library_books(client, SERVER, "synthetic-token", "lib")
