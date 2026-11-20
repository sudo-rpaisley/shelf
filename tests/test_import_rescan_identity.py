"""Exact import-binding repair/regressions on future disposable DBs only.

Authored, not collected or executed at this source-only checkpoint. No test
asserts that title similarity proves duplicates or that live data was repaired.
"""

import asyncio

import pytest
import respx

from app.services import audiobookshelf, import_bindings, item_write, komga_records, metadata_policy, romm_records, sources
from tests.test_komga_records import _candidate as _komga
from tests.test_romm_records import _candidate as _romm
from tests.test_sources import _book, _catalogue, _source


def _provider(name, identity="one", **fields):
    if name == "komga":
        return komga_records, _komga(komga_id=identity, isbn=None, **fields), "komga_records", "komga_id", "manga"
    return romm_records, _romm(romm_id=identity, **fields), "romm_records", "romm_id", "video_game"


def _merge(client, keep, other, db):
    db.commit()
    response = client.post("/api/items/merge", json={"keep_id": keep, "merge_ids": [other]})
    assert response.status_code == 200 and response.json() == {"ok": True, "merged": 1}


def _personal(db):
    tables = ("item_copies", "checkouts", "reading_log", "user_item_state", "user_item_events", "reading_sessions")
    return {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")) for table in tables}


@pytest.mark.parametrize("name", ["komga", "romm"])
def test_user_confirmed_merge_preserves_all_remote_keys_and_rescan_never_recreates_losing_item(admin_client, db, name):
    service, first, table, key, _ = _provider(name, "one")
    _, second, _, _, _ = _provider(name, "two", title="Second imported entry")
    keep = service.persist_candidate(db, first)["item_id"]
    other = service.persist_candidate(db, second)["item_id"]
    bindings = [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY {key}")]
    _merge(admin_client, keep, other, db)
    merged = [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY {key}")]
    assert merged == [{**row, "item_id": keep} for row in bindings]
    personal = _personal(db)
    for _ in range(2):
        for candidate in (first, second):
            result = service.persist_candidate(db, candidate)
            assert result["item_id"] == keep and result["action"] == "updated"
            db.commit()
    assert db.execute("SELECT id FROM items_live").fetchall()[0][0] == keep
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM items WHERE id = ?", (other,)).fetchone()[0] == 0
    assert _personal(db) == personal


@pytest.mark.parametrize("name", ["komga", "romm"])
def test_rescan_recovers_a_previously_lost_binding_after_merge_without_changing_manual_metadata(admin_client, db, name):
    service, candidate, table, key, kind = _provider(name)
    remote = service.persist_candidate(db, candidate)["item_id"]
    keep = item_write.insert_item(db, {"title": "My chosen title", "authors": "My own credit", "media_type": kind,
                                       "source": "manual", "notes": "Private catalogue annotation"})
    _merge(admin_client, keep, remote, db)
    # Reproduce the old cascading loss, not the corrected merge implementation.
    db.execute(f"DELETE FROM {table} WHERE {key} = ?", ("one",))
    db.commit()
    before = dict(db.execute("SELECT * FROM items WHERE id = ?", (keep,)).fetchone())
    personal = _personal(db)
    result = service.persist_candidate(db, candidate)
    assert result["item_id"] == keep and result["action"] == "updated"
    assert dict(db.execute("SELECT * FROM items WHERE id = ?", (keep,)).fetchone()) == before
    assert db.execute(f"SELECT item_id FROM {table} WHERE {key} = ?", ("one",)).fetchone()[0] == keep
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 1 and _personal(db) == personal


@pytest.mark.parametrize("name", ["komga", "romm"])
def test_missing_binding_to_trash_is_seen_before_any_metadata_binding_or_item_write(db, name):
    service, candidate, table, key, _ = _provider(name)
    item = service.persist_candidate(db, candidate)["item_id"]
    db.execute(f"DELETE FROM {table} WHERE {key} = ?", ("one",))
    db.execute("UPDATE items SET deleted_at = '2026-11-20 10:00:00' WHERE id = ?", (item,))
    db.commit()
    before = tuple(db.iterdump())
    changes = db.total_changes
    result = service.persist_candidate(db, candidate)
    assert result["item_id"] == item and result["action"] == "in_trash"
    assert db.total_changes == changes and tuple(db.iterdump()) == before
    assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["komga", "romm"])
def test_ambiguous_prior_targets_refuse_before_creating_or_rebinding_an_item(db, name):
    service, candidate, _, _, kind = _provider(name)
    for title in ("First catalogue identity", "Second catalogue identity"):
        item = item_write.insert_item(db, {"title": title, "media_type": kind})
        metadata_policy.record_initial(db, item, f"{name}:one", {"title": title})
    db.commit()
    before = tuple(db.iterdump())
    with pytest.raises(import_bindings.ImportBindingConflict, match="multiple catalogue targets"):
        service.persist_candidate(db, candidate)
    assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("name", ["komga", "romm"])
def test_identical_titles_with_different_remote_ids_are_not_automatic_duplicate_proof(db, name):
    service, first, _, _, _ = _provider(name, "one")
    _, second, _, _, _ = _provider(name, "two")
    a = service.persist_candidate(db, first)["item_id"]
    b = service.persist_candidate(db, second)["item_id"]
    assert a != b and db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 2


def test_prior_lookup_is_exact_provider_scoped_and_never_owns_the_transaction(db):
    one = item_write.insert_item(db, {"title": "One", "media_type": "comic"})
    metadata_policy.record_initial(db, one, "komga:opaque_%:one", {"title": "One"})
    db.commit()
    with pytest.raises(ValueError, match="caller's transaction"):
        import_bindings.previous_item(db, "komga", "opaque_%:one")
    metadata_policy.begin_write(db)
    assert import_bindings.previous_item(db, "komga", "opaque_%:one")["id"] == one
    assert import_bindings.previous_item(db, "romm", "opaque_%:one") is None
    assert import_bindings.previous_item(db, "komga", "opaque_%") is None
    assert db.in_transaction


@respx.mock
def test_abs_rescan_recovers_merged_alias_without_replacing_the_canonical_id_or_manual_state(admin_client, db):
    keep = item_write.insert_item(db, {"title": "Canonical title", "media_type": "audiobook", "source": "audiobookshelf",
                                       "abs_id": "canonical", "abs_library_id": "original-library"})
    other = item_write.insert_item(db, {"title": "Alias title", "media_type": "audiobook", "source": "audiobookshelf",
                                        "abs_id": "alias", "abs_library_id": "audio"})
    metadata_policy.record_initial(db, keep, "audiobookshelf:canonical", {"title": "Canonical title"})
    metadata_policy.record_initial(db, other, "audiobookshelf:alias", {"title": "Alias title"})
    _merge(admin_client, keep, other, db)
    personal = _personal(db)
    root = "http://abs.example"
    respx.get(root + "/api/libraries").respond(200, json={"libraries": [{"id": "audio", "mediaType": "book"}]})
    respx.get(root + "/api/libraries/audio/items").respond(200, json={"results": [{
        "id": "alias", "media": {"metadata": {"title": "Corrected alias title"}, "numAudioFiles": 1, "duration": 3600},
    }]})
    respx.get(root + "/api/items/alias/cover").respond(404)
    for _ in range(2):
        stats = asyncio.run(audiobookshelf.sync(root, "synthetic-token"))
        assert stats["added"] == stats["errors"] == 0
    row = db.execute("SELECT id, title, abs_id, abs_library_id FROM items_live").fetchone()
    assert tuple(row) == (keep, "Canonical title", "canonical", "original-library")
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 1 and _personal(db) == personal


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["opds", "jellyfin"])
async def test_connected_sources_keep_instance_scoped_record_ids_through_merge_and_repeat_scan(admin_client, db, monkeypatch, provider):
    credentials = {"token": "synthetic-token"} if provider == "jellyfin" else {}
    first = _source(provider=provider, credentials=credentials)
    second = _source(provider=provider, name="Other instance", base_url="http://other.lan/catalogue", credentials=credentials)
    _catalogue(monkeypatch, [_book("opaque:one", media_type="ebook")], provider=provider)
    assert (await sources.sync_instance(first))["summary"]["added"] == 1
    assert (await sources.sync_instance(second))["summary"]["added"] == 1
    rows = [dict(row) for row in db.execute("SELECT * FROM source_records ORDER BY id")]
    assert rows[0]["source_id"] != rows[1]["source_id"] and rows[0]["item_id"] != rows[1]["item_id"]
    keep, other = rows[0]["item_id"], rows[1]["item_id"]
    _merge(admin_client, keep, other, db)
    for source_id in (first, second, first):
        result = await sources.sync_instance(source_id)
        assert result["summary"]["added"] == 0 and result["summary"]["updated"] == 1
    after = [dict(row) for row in db.execute("SELECT id, source_id, external_id, item_id FROM source_records ORDER BY id")]
    assert after == [{key: keep if key == "item_id" else row[key] for key in ("id", "source_id", "external_id", "item_id")} for row in rows]
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 1
