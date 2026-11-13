"""Reviewed machine rebindings preserve catalogue data and revoke false authority."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import sqlite3
import threading

import pytest

from app import config
from app.database import get_db, init_db
from app.services import item_write, metadata_policy, metadata_review, source_migration as migration, sources
from app.services.source_connectors import opds
from app.services.source_connectors.base import Catalogue, SourceRecord
from tests.conftest import _insert_item, _insert_location

ISBN = "9780306406157"
OTHER_ISBN = "9780441172719"
ACTOR = 17


def _instance(db, name="Old", provider="opds"):
    return sources.create_instance(db, provider=provider, name=name,
                                   base_url="http://books.invalid/opds" if provider == "opds" else "",
                                   credentials={"password": f"test-only-{name}-credential"} if provider == "opds" else {})


def _record(db, source_id, external_id="remote-1", *, title="Remote title", isbn=ISBN,
            metadata=None, item_id=None, description="Remote description", media_type="ebook"):
    if item_id is None:
        item_id = _insert_item(db, title=title, isbn=None, media_type=media_type, description=description)
    record = SourceRecord(external_id, title, media_type, isbn=isbn, metadata=metadata or {})
    sources._snapshot(db, source_id, item_id, record, "2026-11-01 00:00:00")
    key = sources.metadata_source_key(source_id, external_id)
    metadata_policy.record_initial(db, item_id, key,
                                   {"title": title, "description": description, "isbn": isbn, "media_type": media_type})
    record_id = db.execute("SELECT id FROM source_records WHERE source_id = ? AND external_id = ?", (source_id, external_id)).fetchone()[0]
    return record_id, item_id


def _seed(db, *, old_isbn=ISBN, new_isbn=ISBN, metadata=None):
    old, new = _instance(db), _instance(db, "New")
    old_record, keep = _record(db, old, "old-id", isbn=old_isbn, metadata=metadata)
    new_record, previous = _record(db, new, "new-id", isbn=new_isbn, metadata=metadata)
    db.commit()
    return old, new, old_record, new_record, keep, previous


def _preview(db, seeded, **kwargs):
    old, new, old_record, new_record, _, _ = seeded
    return migration.preview_replacement(db, actor_id=ACTOR, old_source_id=old, new_source_id=new,
                                         mappings=[{"old_record_id": old_record, "new_record_id": new_record}], **kwargs)


def _apply(db, preview):
    return migration.apply(db, preview["id"], actor_id=ACTOR, fingerprint=preview["fingerprint"], reviewed=True)


def _receipt(db, preview):
    return migration.detail(db, preview["id"], actor_id=ACTOR)


def _binding(db, record_id):
    return db.execute("SELECT item_id FROM source_records WHERE id = ?", (record_id,)).fetchone()[0]


def _protected(db):
    tables = ("items_live", "copies_live", "item_tags", "list_items", "user_item_state", "user_item_events", "reading_log", "checkouts")
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
            for table in tables}


def _provenance(db, item_id, source_id, external_id, field="description"):
    key = sources.metadata_source_key(source_id, external_id)
    row = db.execute("SELECT value_json FROM item_metadata_provenance WHERE item_id = ? AND source_key = ? AND field_name = ?",
                     (item_id, key, field)).fetchone()
    return json.loads(row[0])


def test_unique_remote_isbn_evidence_is_suggested_without_writing_or_title_matching(db):
    seed = _seed(db)
    old, new, old_record, new_record, keep, previous = seed
    item_write.update_item_fields(db, keep, {"title": "Unrelated local title", "isbn": OTHER_ISBN})
    db.commit()
    before = db.total_changes
    context = migration.workspace(db, old, new)
    assert db.total_changes == before
    assert context["suggestions"] == [{"old_record_id": old_record, "new_record_id": new_record,
                                        "evidence": [{"kind": "ISBN", "value": ISBN}]}]
    assert _binding(db, new_record) == previous
    assert keep != previous


def test_identical_titles_authors_and_external_ids_are_not_identity_evidence(db):
    seed = _seed(db, old_isbn=None, new_isbn=None)
    old, new, old_record, new_record, _, _ = seed
    db.execute("UPDATE source_records SET external_id = 'same-server-local-id'")
    db.commit()
    assert migration.workspace(db, old, new)["suggestions"] == []
    with pytest.raises(migration.MigrationError, match="unique conflict-free"):
        _preview(db, seed)
    assert db.execute("SELECT COUNT(*) FROM source_migration_plans").fetchone()[0] == 0


@pytest.mark.parametrize("identifiers,expected", [
    ({"Imdb": "tt12345", "Tmdb": "5678"}, True),
    ({"MusicBrainzArtist": "12345678-1234-4321-abcd-1234567890ab"}, False),
    ({"Imdb": "not an id"}, False),
    ({"Isbn10": "0306406152"}, True),
])
def test_only_shaped_work_identifiers_supply_evidence(db, identifiers, expected):
    seed = _seed(db, old_isbn=None, new_isbn=None, metadata={"provider_ids": identifiers})
    assert bool(migration.workspace(db, seed[0], seed[1])["suggestions"]) is expected


@pytest.mark.parametrize("ambiguous_side", ["old", "new"])
def test_ambiguous_identifiers_never_suggest_a_pair(db, ambiguous_side):
    seed = _seed(db)
    _record(db, seed[0 if ambiguous_side == "old" else 1], "ambiguous-second")
    db.commit()
    assert migration.workspace(db, seed[0], seed[1])["suggestions"] == []
    with pytest.raises(migration.MigrationError, match="unique conflict-free"):
        _preview(db, seed)
    assert _preview(db, seed, manual_reviewed=True)["plan"]["mappings"][0]["evidence"][-1]["kind"] == "reviewed_record_ids"


def test_conflicting_provider_identifiers_require_visible_manual_review(db):
    seed = _seed(db, metadata={"provider_ids": {"Imdb": "tt12345"}})
    db.execute("UPDATE source_records SET metadata_json = ? WHERE id = ?", ('{"provider_ids":{"Imdb":"tt99999"}}', seed[3]))
    db.commit()
    assert migration.workspace(db, seed[0], seed[1])["suggestions"] == []
    with pytest.raises(migration.MigrationError, match="unique conflict-free"):
        _preview(db, seed)
    preview = _preview(db, seed, manual_reviewed=True)
    assert preview["plan"]["mappings"][0]["conflicts"] == ["Different Imdb observations"]


def test_preview_stages_only_ids_evidence_and_digest_without_domain_writes_or_credentials(db):
    seed = _seed(db)
    before = _protected(db)
    records = [tuple(row) for row in db.execute("SELECT * FROM source_records")]
    instances = [tuple(row) for row in db.execute("SELECT * FROM source_instances")]
    preview = _preview(db, seed)
    assert preview["state"] == "preview" and len(preview["fingerprint"]) == 64
    assert _protected(db) == before
    assert records == [tuple(row) for row in db.execute("SELECT * FROM source_records")]
    assert instances == [tuple(row) for row in db.execute("SELECT * FROM source_instances")]
    payload = json.dumps(preview) + json.dumps(_receipt(db, preview))
    stored = db.execute("SELECT plan_json, result_json FROM source_migration_plans").fetchone()
    payload += str(tuple(stored))
    assert "credential" not in payload and "books.invalid" not in payload and "gAAAAA" not in payload


def test_apply_preserves_catalogue_copies_activity_manual_values_and_cover_decisions(db):
    seed = _seed(db)
    old, new, _, new_record, keep, previous = seed
    location = _insert_location(db)
    item_write.update_item_fields(db, keep, {"title": "Hand edited", "description": None, "notes": "Local note",
                                           "owned": 1, "location_id": location, "cover_path": "covers/keep.jpg",
                                           "cover_review_dismissed": 1})
    cover_key = sources.metadata_source_key(new, "new-id")
    db.execute("INSERT INTO item_metadata_provenance (item_id, source_key, field_name, value_json) VALUES (?, ?, 'cover_path', ?)",
               (keep, cover_key, '{"local":null,"overridden":true,"observed":null}'))
    cover_before = tuple(db.execute("SELECT * FROM item_metadata_provenance WHERE item_id = ? AND field_name = 'cover_path'", (keep,)).fetchone())
    user_id = db.execute("INSERT INTO users (username, password, display_name, role) VALUES ('Reader', 'test-hash', 'Reader', 'viewer')").lastrowid
    db.execute("INSERT INTO user_item_state (user_id, item_id, notes) VALUES (?, ?, 'Private note')", (user_id, keep))
    db.execute("INSERT INTO user_item_events (user_id, item_id, event_type, snapshot_json) VALUES (?, ?, 'saved', '{}')", (user_id, keep))
    tag = db.execute("INSERT INTO tags (name) VALUES ('Keep tag')").lastrowid
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (keep, tag))
    db.commit()
    before = _protected(db)
    credentials = [tuple(row) for row in db.execute("SELECT id, credentials_enc FROM source_instances ORDER BY id")]
    preview = _preview(db, seed, retire_old=True)
    assert _apply(db, preview)["ok"]
    assert _binding(db, new_record) == keep
    assert _protected(db) == before
    assert [tuple(row) for row in db.execute("SELECT id, credentials_enc FROM source_instances ORDER BY id")] == credentials
    assert db.execute("SELECT enabled FROM source_instances WHERE id = ?", (old,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 2
    assert tuple(db.execute("SELECT * FROM item_metadata_provenance WHERE item_id = ? AND field_name = 'cover_path'", (keep,)).fetchone()) == cover_before
    assert _provenance(db, previous, new, "new-id")["overridden"] is True
    assert "applied" not in _provenance(db, previous, new, "new-id")
    assert _provenance(db, keep, old, "old-id")["overridden"] is True
    assert _provenance(db, keep, new, "new-id", "description")["local"] is None
    assert "applied" not in _provenance(db, keep, new, "new-id")


async def test_wrong_attachment_repair_is_persistent_across_sync_and_loses_wrong_metadata_authority(db, monkeypatch):
    source_id = _instance(db)
    record_id, wrong = _record(db, source_id, title="Wrongly applied source title")
    right = _insert_item(db, title="Correct local title", isbn=None, media_type="ebook", description=None, notes="Keep")
    # A historical attachment must not smuggle its stale applied/follow claim
    # back onto this item just because it has the same source key again.
    key = sources.metadata_source_key(source_id, "remote-1")
    metadata_policy.record_initial(db, right, key, {"title": "Correct local title", "description": None})
    db.execute("UPDATE item_metadata_provenance SET value_json = ? WHERE item_id = ? AND source_key = ? AND field_name = 'title'",
               ('{"observed":"Correct local title","local":"Correct local title","applied":"Correct local title","follow_source":true}', right, key))
    db.commit()
    before = _protected(db)
    preview = migration.preview_repair(db, actor_id=ACTOR, source_id=source_id, record_id=record_id, item_id=right, manual_reviewed=True)
    _apply(db, preview)
    db.commit()
    assert _protected(db) == before

    async def fetch(source, credentials):
        assert source["id"] == source_id
        return Catalogue([SourceRecord("remote-1", "Changed remote title", "ebook", metadata={"description": "New remote description", "language": "fr"})])

    monkeypatch.setattr(opds, "fetch_catalogue", fetch)
    for _ in range(2):
        result = await sources.sync_instance(source_id)
        assert result["ok"] and result["summary"]["updated"] == 1 and result["summary"]["added"] == 0
    assert _binding(db, record_id) == right
    assert _protected(db) == before
    for item_id in (wrong, right):
        for field in ("title", "description", "language"):
            saved = _provenance(db, item_id, source_id, "remote-1", field)
            assert saved["overridden"] is True and "applied" not in saved and "follow_source" not in saved


def test_unrelated_source_provenance_is_not_changed(db):
    seed = _seed(db)
    key = "komga:unrelated"
    metadata_policy.record_initial(db, seed[4], key, {"description": "Remote description"})
    db.commit()
    before = tuple(db.execute("SELECT * FROM item_metadata_provenance WHERE source_key = ?", (key,)).fetchone())
    _apply(db, _preview(db, seed))
    assert tuple(db.execute("SELECT * FROM item_metadata_provenance WHERE source_key = ?", (key,)).fetchone()) == before


@pytest.mark.parametrize("pairs", [
    [], None, [{"old_record_id": 1, "new_record_id": 2, "evidence": "client claim"}],
    [{"old_record_id": 1, "new_record_id": 2}] * 2,
])
def test_invalid_or_duplicate_mapping_sets_never_stage_a_plan(db, pairs):
    seed = _seed(db)
    if pairs and len(pairs) == 2:
        pairs = [{"old_record_id": seed[2], "new_record_id": seed[3]}] * 2
    with pytest.raises(migration.MigrationError):
        migration.preview_replacement(db, actor_id=ACTOR, old_source_id=seed[0], new_source_id=seed[1], mappings=pairs)
    assert db.execute("SELECT COUNT(*) FROM source_migration_plans").fetchone()[0] == 0


def test_manual_review_does_not_permit_record_ids_from_other_instances(db):
    seed = _seed(db)
    with pytest.raises(migration.MigrationError) as error:
        migration.preview_replacement(db, actor_id=ACTOR, old_source_id=seed[0], new_source_id=seed[1],
                                      mappings=[{"old_record_id": seed[3], "new_record_id": seed[2]}], manual_reviewed=True)
    assert error.value.status_code == 404
    assert _binding(db, seed[3]) == seed[5]


def test_manual_pairs_without_identifiers_keep_unmapped_records_and_show_coverage(db):
    seed = _seed(db, old_isbn=None, new_isbn=None)
    old_extra, old_item = _record(db, seed[0], "unmapped-old", isbn=None)
    new_extra, new_item = _record(db, seed[1], "unmapped-new", isbn=None)
    db.commit()
    preview = _preview(db, seed, manual_reviewed=True)
    assert preview["plan"]["coverage"] == {"old_records": 2, "new_records": 2, "selected": 1, "unmapped_old": 1, "unmapped_new": 1}
    assert preview["plan"]["mappings"][0]["evidence"] == [{"kind": "reviewed_record_ids", "old_record_id": seed[2], "new_record_id": seed[3]}]
    _apply(db, preview)
    assert _binding(db, old_extra) == old_item and _binding(db, new_extra) == new_item


def test_same_instance_replacement_uses_explicit_repair_instead(db):
    seed = _seed(db)
    with pytest.raises(migration.MigrationError, match="different old and replacement"):
        migration.preview_replacement(db, actor_id=ACTOR, old_source_id=seed[0], new_source_id=seed[0],
                                      mappings=[{"old_record_id": seed[2], "new_record_id": seed[2]}], manual_reviewed=True)


@pytest.mark.parametrize("malformed", [
    '{"provider_ids":{"Imdb":"tt12345"},"provider_ids":{"Imdb":"tt99999"}}',
    '{"provider_ids":{"Tmdb":NaN}}', "[invalid", '"old scalar"',
])
def test_malformed_ambiguous_snapshot_json_never_supplies_identity_evidence(db, malformed):
    seed = _seed(db, old_isbn=None, new_isbn=None)
    db.execute("UPDATE source_records SET metadata_json = ?", (malformed,))
    db.commit()
    assert migration.workspace(db, seed[0], seed[1])["suggestions"] == []
    preview = _preview(db, seed, manual_reviewed=True)
    assert _apply(db, preview)["ok"]


@pytest.mark.parametrize("change", ["snapshot", "local", "cover", "provenance", "config", "insert", "version", "lease", "trashed", "rebound"])
def test_changed_preview_is_refused_before_any_binding_write(db, change):
    seed = _seed(db)
    preview = _preview(db, seed)
    if change == "snapshot":
        db.execute("UPDATE source_records SET metadata_json = '{\"description\":\"Changed\"}' WHERE id = ?", (seed[3],))
    elif change == "local":
        item_write.update_item_fields(db, seed[4], {"notes": "Changed after review"})
    elif change == "cover":
        item_write.update_item_fields(db, seed[4], {"cover_review_dismissed": 1})
    elif change == "provenance":
        db.execute("UPDATE item_metadata_provenance SET observed_at = '2026-11-02'")
    elif change == "config":
        db.execute("UPDATE source_instances SET config_revision = config_revision + 1 WHERE id = ?", (seed[1],))
    elif change == "insert":
        _record(db, seed[1], "new-after-preview")
    elif change == "version":
        db.execute("INSERT INTO source_binding_versions (record_id, revision, last_plan_id) VALUES (?, 1, ?)", (seed[3], preview["id"]))
    elif change == "lease":
        db.execute("UPDATE source_instances SET sync_token = 'in-flight' WHERE id = ?", (seed[1],))
    elif change == "trashed":
        item_write.trash_item(db, seed[4])
    else:
        db.execute("UPDATE source_records SET item_id = ? WHERE id = ?", (seed[4], seed[3]))
    db.commit()
    before, bound = _protected(db), _binding(db, seed[3])
    with pytest.raises(migration.MigrationError) as error:
        _apply(db, preview)
    assert error.value.status_code == 409
    assert _binding(db, seed[3]) == bound and _protected(db) == before
    assert db.execute("SELECT state FROM source_migration_plans WHERE id = ?", (preview["id"],)).fetchone()[0] == "preview"


def test_preview_expires_and_apply_requires_confirmation_and_exact_actor(db, monkeypatch):
    seed = _seed(db)
    preview = _preview(db, seed)
    with pytest.raises(migration.MigrationError, match="Explicitly confirm"):
        migration.apply(db, preview["id"], actor_id=ACTOR, fingerprint=preview["fingerprint"])
    with pytest.raises(migration.MigrationError) as error:
        migration.apply(db, preview["id"], actor_id=ACTOR + 1, fingerprint=preview["fingerprint"], reviewed=True)
    assert error.value.status_code == 404
    now = migration._now()
    monkeypatch.setattr(migration, "_now", lambda: now + timedelta(minutes=16))
    with pytest.raises(migration.MigrationError, match="expired"):
        _apply(db, preview)
    assert _binding(db, seed[3]) == seed[5]


@pytest.mark.parametrize("fingerprint", ["", "0" * 64, "x" * 64, "a" * 65, None, []])
def test_forged_or_malformed_fingerprints_never_apply(db, fingerprint):
    seed = _seed(db)
    preview = _preview(db, seed)
    with pytest.raises(migration.MigrationError) as error:
        migration.apply(db, preview["id"], actor_id=ACTOR, fingerprint=fingerprint, reviewed=True)
    assert error.value.status_code == 409 and _binding(db, seed[3]) == seed[5]


def test_apply_and_rollback_replays_do_not_repeat_writes_or_reapply_a_rolled_back_plan(db):
    seed = _seed(db)
    preview = _preview(db, seed)
    assert _apply(db, preview)["replayed"] is False
    before = db.total_changes
    assert _apply(db, preview)["replayed"] is True and db.total_changes == before
    fingerprint = _receipt(db, preview)["rollback_fingerprint"]
    assert migration.rollback(db, preview["id"], actor_id=ACTOR, fingerprint=fingerprint, reviewed=True)["replayed"] is False
    before = db.total_changes
    assert migration.rollback(db, preview["id"], actor_id=ACTOR, fingerprint=fingerprint, reviewed=True)["replayed"] is True
    assert db.total_changes == before and _binding(db, seed[3]) == seed[5]
    with pytest.raises(migration.MigrationError):
        _apply(db, preview)


def test_fresh_rollback_preserves_intervening_local_edits_and_never_restores_authority(db):
    seed = _seed(db)
    preview = _preview(db, seed, retire_old=True)
    _apply(db, preview)
    stale = _receipt(db, preview)["rollback_fingerprint"]
    item_write.update_item_fields(db, seed[4], {"title": "Edited after apply", "description": None})
    db.commit()
    before = _protected(db)
    with pytest.raises(migration.MigrationError):
        migration.rollback(db, preview["id"], actor_id=ACTOR, fingerprint=stale, reviewed=True)
    fresh = _receipt(db, preview)["rollback_fingerprint"]
    assert fresh != stale
    assert migration.rollback(db, preview["id"], actor_id=ACTOR, fingerprint=fresh, reviewed=True)["ok"]
    assert _binding(db, seed[3]) == seed[5] and _protected(db) == before
    assert db.execute("SELECT enabled FROM source_instances WHERE id = ?", (seed[0],)).fetchone()[0] == 1
    for item_id in (seed[4], seed[5]):
        row = _provenance(db, item_id, seed[1], "new-id")
        assert row["overridden"] is True and "applied" not in row and "follow_source" not in row


def _follow_description(db, item_id, source_id, external_id):
    key = sources.metadata_source_key(source_id, external_id)
    row = next(row for row in metadata_review.context_for(db, item_id)["metadata_rows"]
               if row["source_key"] == key and row["field_name"] == "description")
    metadata_review.resolve_field(db, item_id, key, "description", "follow_source", row["fingerprint"])
    db.commit()


@pytest.mark.parametrize("side", ["original", "target"])
def test_rollback_refuses_superseding_follow_source_even_after_receipt_refresh(db, side):
    seed = _seed(db)
    preview = _preview(db, seed)
    _apply(db, preview)
    _follow_description(db, seed[5 if side == "original" else 4], seed[1], "new-id")
    receipt = _receipt(db, preview)
    fingerprint = receipt.get("rollback_fingerprint") or migration._fingerprint(db, preview["plan"], purpose="rollback", plan_id=preview["id"])
    before = [tuple(row) for row in db.execute("SELECT * FROM item_metadata_provenance ORDER BY item_id, source_key, field_name")]
    with pytest.raises(migration.MigrationError) as error:
        migration.rollback(db, preview["id"], actor_id=ACTOR, fingerprint=fingerprint, reviewed=True)
    assert error.value.code == "authority_changed" and error.value.status_code == 409
    assert receipt["rollback_blocked"] and "rollback_fingerprint" not in receipt
    assert _binding(db, seed[3]) == seed[4]
    assert [tuple(row) for row in db.execute("SELECT * FROM item_metadata_provenance ORDER BY item_id, source_key, field_name")] == before


def test_binding_noop_rollback_preserves_later_real_resolver_decision_on_adopted_isbn(db):
    old, new = _instance(db), _instance(db, "New")
    item_id = _insert_item(db, isbn=ISBN, media_type="ebook", description="Local description")
    for source_id, external_id in ((old, "old-id"), (new, "new-id")):
        assert sources._persist_record(db, {"id": source_id}, SourceRecord(
            external_id, "Remote title", "ebook", isbn=ISBN, metadata={"description": "Reviewed remote description"},
        ), "2026-11-01 00:00:00") == "linked"
    rows = db.execute("SELECT id FROM source_records ORDER BY source_id").fetchall()
    seed = old, new, rows[0][0], rows[1][0], item_id, item_id
    db.commit()
    preview = _preview(db, seed)
    _apply(db, preview)
    _follow_description(db, item_id, new, "new-id")
    receipt = _receipt(db, preview)
    before = [tuple(row) for row in db.execute("SELECT * FROM item_metadata_provenance ORDER BY item_id, source_key, field_name")]
    assert migration.rollback(db, preview["id"], actor_id=ACTOR, fingerprint=receipt["rollback_fingerprint"], reviewed=True)["ok"]
    assert [tuple(row) for row in db.execute("SELECT * FROM item_metadata_provenance ORDER BY item_id, source_key, field_name")] == before
    saved = _provenance(db, item_id, new, "new-id")
    assert saved["follow_source"] is True and saved["applied"] == "Reviewed remote description"
    assert _binding(db, seed[3]) == item_id
    sources._persist_record(db, {"id": new}, SourceRecord(
        "new-id", "Remote title", "ebook", isbn=ISBN, metadata={"description": "New followed description"},
    ), "2026-11-02 00:00:00")
    assert db.execute("SELECT description FROM items_live WHERE id = ?", (item_id,)).fetchone()[0] == "New followed description"


@pytest.mark.parametrize("side", ["old", "new"])
@pytest.mark.parametrize("change", ["added", "removed", "replaced", "unavailable"])
def test_retirement_requires_new_complete_coverage_review_after_inventory_changes(db, side, change):
    seed = _seed(db)
    source_id = seed[0 if side == "old" else 1]
    if change != "added":
        extra_id, _ = _record(db, source_id, "extra-before-review", isbn=None)
        db.commit()
    preview = _preview(db, seed)
    _apply(db, preview)
    if change in {"removed", "replaced"}:
        db.execute("DELETE FROM source_records WHERE id = ?", (extra_id,))
    elif change == "unavailable":
        db.execute("UPDATE source_records SET availability = 'unavailable' WHERE id = ?", (extra_id,))
    if change in {"added", "replaced"}:
        _record(db, source_id, "added-after-apply", isbn=None)
    db.commit()
    receipt = _receipt(db, preview)
    fingerprint = receipt.get("retire_fingerprint") or migration._fingerprint(db, preview["plan"], purpose="retire", plan_id=preview["id"])
    before = _protected(db)
    with pytest.raises(migration.MigrationError) as error:
        migration.retire(db, preview["id"], actor_id=ACTOR, fingerprint=fingerprint, reviewed=True)
    assert error.value.code == "coverage_changed" and error.value.status_code == 409
    assert receipt["coverage_changed"] and "retire_fingerprint" not in receipt
    assert receipt["coverage"][f"{side}_records"] == db.execute("SELECT COUNT(*) FROM source_records WHERE source_id = ?", (source_id,)).fetchone()[0]
    assert db.execute("SELECT enabled FROM source_instances WHERE id = ?", (seed[0],)).fetchone()[0] == 1
    assert _protected(db) == before


async def test_added_holdings_from_real_sync_require_complete_rereview_before_retirement(db, monkeypatch):
    seed = _seed(db)
    preview = _preview(db, seed)
    _apply(db, preview)
    db.commit()

    async def fetch(source, credentials):
        return Catalogue([SourceRecord("old-id", "Remote title", "ebook", isbn=ISBN),
                          SourceRecord("added-by-sync", "New old-source holding", "ebook")])

    monkeypatch.setattr(opds, "fetch_catalogue", fetch)
    assert (await sources.sync_instance(seed[0]))["summary"]["added"] == 1
    receipt = _receipt(db, preview)
    assert receipt["coverage"]["unmapped_old"] == 1 and receipt["coverage_changed"]
    assert "retire_fingerprint" not in receipt
    before = _protected(db)
    refreshed = _preview(db, seed)
    assert refreshed["plan"]["coverage"]["unmapped_old"] == 1
    _apply(db, refreshed)
    current = _receipt(db, refreshed)
    assert migration.retire(db, refreshed["id"], actor_id=ACTOR, fingerprint=current["retire_fingerprint"], reviewed=True)["ok"]
    assert _protected(db) == before
    assert db.execute("SELECT COUNT(*) FROM source_records WHERE source_id = ?", (seed[0],)).fetchone()[0] == 2


async def test_timestamp_only_sync_refresh_does_not_invent_changed_coverage(db, monkeypatch):
    seed = _seed(db)
    preview = _preview(db, seed)
    _apply(db, preview)
    db.commit()

    async def fetch(source, credentials):
        return Catalogue([SourceRecord("old-id", "Remote title", "ebook", isbn=ISBN)])

    monkeypatch.setattr(opds, "fetch_catalogue", fetch)
    assert (await sources.sync_instance(seed[0]))["ok"]
    receipt = _receipt(db, preview)
    assert not receipt["coverage_changed"] and "retire_fingerprint" in receipt


def test_old_receipts_without_inventory_reference_cannot_authorize_retirement(db):
    seed = _seed(db)
    preview = _preview(db, seed)
    _apply(db, preview)
    result = json.loads(db.execute("SELECT result_json FROM source_migration_plans WHERE id = ?", (preview["id"],)).fetchone()[0])
    result.pop("inventory_fingerprint")
    db.execute("UPDATE source_migration_plans SET result_json = ? WHERE id = ?", (json.dumps(result), preview["id"]))
    assert _receipt(db, preview)["coverage_changed"]
    fingerprint = migration._fingerprint(db, preview["plan"], purpose="retire", plan_id=preview["id"])
    with pytest.raises(migration.MigrationError) as error:
        migration.retire(db, preview["id"], actor_id=ACTOR, fingerprint=fingerprint, reviewed=True)
    assert error.value.code == "coverage_changed"


def test_old_preview_without_opening_review_requires_a_fresh_preview(db):
    seed = _seed(db)
    preview = _preview(db, seed)
    plan = preview["plan"]
    for mapping in plan["mappings"]:
        for field in ("openings_before", "openings_after", "openings_after_retirement"):
            mapping.pop(field)
    fingerprint = migration._fingerprint(db, plan)
    db.execute("UPDATE source_migration_plans SET plan_json = ?, fingerprint = ? WHERE id = ?",
               (json.dumps(plan), fingerprint, preview["id"]))
    assert _receipt(db, preview)["conflict"]
    with pytest.raises(migration.MigrationError) as error:
        migration.apply(db, preview["id"], actor_id=ACTOR, fingerprint=fingerprint, reviewed=True)
    assert error.value.code == "review_required" and _binding(db, seed[3]) == seed[5]


@pytest.mark.parametrize("new_url,new_media,reason", [
    (None, "ebook", "missing_link"),
    ("https://reader.invalid/new?token=do-not-display-this-token", "ebook", "unsafe_link"),
    ("https://reader.invalid/video", "digital_video", "incompatible_media"),
])
def test_opening_preview_shows_before_after_and_retirement_loss_without_secrets_or_network(db, monkeypatch, new_url, new_media, reason):
    seed = _seed(db)
    old_url = "https://reader.invalid/read?id=old-book"
    db.execute("UPDATE source_records SET open_url = ? WHERE id = ?", (old_url, seed[2]))
    db.execute("UPDATE source_records SET open_url = ?, media_type = ? WHERE id = ?", (new_url, new_media, seed[3]))
    db.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("Opening review must not decrypt credentials or fetch a server")

    monkeypatch.setattr(sources, "_credentials", forbidden)
    monkeypatch.setattr(opds, "fetch_catalogue", forbidden)
    context = migration.workspace(db, seed[0], seed[1])
    assert context["old_records"][0]["opening"]["label"] == "Read"
    preview = _preview(db, seed, manual_reviewed=True)
    mapping = preview["plan"]["mappings"][0]
    before = {opening["record_id"]: opening for opening in mapping["openings_before"]}
    retired = {opening["record_id"]: opening for opening in mapping["openings_after_retirement"]}
    assert before[seed[2]]["usable"] and before[seed[2]]["url"] == old_url
    assert retired[seed[2]]["reason_code"] == "source_disabled" and not retired[seed[2]]["usable"]
    assert retired[seed[3]]["reason_code"] == reason and not retired[seed[3]]["usable"]
    assert not any(opening["usable"] for opening in mapping["openings_after_retirement"])
    assert retired[seed[3]]["item_id"] == seed[4]
    encoded = json.dumps(context) + json.dumps(preview) + json.dumps(_receipt(db, preview))
    assert "do-not-display-this-token" not in encoded and "test-only" not in encoded and "gAAAAA" not in encoded


async def test_rollback_after_followup_sync_reverts_binding_not_fresh_remote_snapshots(db, monkeypatch):
    seed = _seed(db)
    preview = _preview(db, seed)
    _apply(db, preview)
    db.commit()

    async def fetch(source, credentials):
        return Catalogue([SourceRecord("new-id", "Fresh remote snapshot", "ebook", isbn=ISBN)])

    monkeypatch.setattr(opds, "fetch_catalogue", fetch)
    assert (await sources.sync_instance(seed[1]))["ok"]
    fingerprint = _receipt(db, preview)["rollback_fingerprint"]
    migration.rollback(db, preview["id"], actor_id=ACTOR, fingerprint=fingerprint, reviewed=True)
    assert _binding(db, seed[3]) == seed[5]
    assert db.execute("SELECT title FROM source_records WHERE id = ?", (seed[3],)).fetchone()[0] == "Fresh remote snapshot"


@pytest.mark.parametrize("action", ["retire", "rollback"])
def test_retire_and_rollback_refuse_a_fingerprint_made_before_concurrent_changes(db, action):
    seed = _seed(db)
    preview = _preview(db, seed)
    _apply(db, preview)
    fingerprint = _receipt(db, preview)[action + "_fingerprint"]
    db.execute("UPDATE source_records SET availability = 'unknown' WHERE id = ?", (seed[3],))
    db.commit()
    with pytest.raises(migration.MigrationError) as error:
        getattr(migration, action)(db, preview["id"], actor_id=ACTOR, fingerprint=fingerprint, reviewed=True)
    assert error.value.status_code == 409 and _binding(db, seed[3]) == seed[4]
    assert db.execute("SELECT enabled FROM source_instances WHERE id = ?", (seed[0],)).fetchone()[0] == 1


def test_retire_is_reviewed_disable_only_and_replay_safe(db):
    seed = _seed(db)
    preview = _preview(db, seed)
    _apply(db, preview)
    receipt = _receipt(db, preview)
    before = _protected(db)
    result = migration.retire(db, preview["id"], actor_id=ACTOR, fingerprint=receipt["retire_fingerprint"], reviewed=True)
    assert result["ok"] and not result["replayed"]
    assert db.execute("SELECT enabled FROM source_instances WHERE id = ?", (seed[0],)).fetchone()[0] == 0
    before_changes = db.total_changes
    assert migration.retire(db, preview["id"], actor_id=ACTOR, fingerprint=receipt["retire_fingerprint"], reviewed=True)["replayed"]
    assert db.total_changes == before_changes and _protected(db) == before
    assert _receipt(db, preview)["retired"]


def test_retired_instance_with_subsequent_settings_change_is_not_reenabled_by_rollback(db):
    seed = _seed(db)
    preview = _preview(db, seed, retire_old=True)
    _apply(db, preview)
    db.execute("UPDATE source_instances SET config_revision = config_revision + 1, name = 'Later settings' WHERE id = ?", (seed[0],))
    db.commit()
    assert _receipt(db, preview)["conflict"]
    with pytest.raises(migration.MigrationError):
        migration.rollback(db, preview["id"], actor_id=ACTOR,
                           fingerprint=migration._fingerprint(db, preview["plan"], purpose="rollback", plan_id=preview["id"]), reviewed=True)
    assert db.execute("SELECT enabled FROM source_instances WHERE id = ?", (seed[0],)).fetchone()[0] == 0


def test_superseded_binding_cannot_be_rolled_back_even_after_aba(db):
    seed = _seed(db)
    preview = _preview(db, seed)
    _apply(db, preview)
    for target in (seed[5], seed[4]):
        repair = migration.preview_repair(db, actor_id=ACTOR, source_id=seed[1], record_id=seed[3], item_id=target, manual_reviewed=True)
        _apply(db, repair)
    assert _binding(db, seed[3]) == seed[4]
    assert _receipt(db, preview)["conflict"]
    with pytest.raises(migration.MigrationError):
        migration.rollback(db, preview["id"], actor_id=ACTOR, fingerprint="0" * 64, reviewed=True)


def test_guard_reads_are_under_write_lock_and_no_network_or_credential_decryption_occurs(db, monkeypatch):
    seed = _seed(db)
    preview = _preview(db, seed)
    db.commit()
    original = migration._state
    calls = []

    def locked(conn, plan):
        assert conn.in_transaction
        rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                rival.execute("BEGIN IMMEDIATE")
        finally:
            rival.close()
        calls.append(True)
        return original(conn, plan)

    def forbidden(*args, **kwargs):
        pytest.fail("Binding writes must not decrypt credentials or contact a connector")

    monkeypatch.setattr(migration, "_state", locked)
    monkeypatch.setattr(sources, "_credentials", forbidden)
    monkeypatch.setattr(opds, "fetch_catalogue", forbidden)
    with get_db() as conn:
        assert _apply(conn, preview)["ok"]
    assert calls == [True]


async def test_inflight_sync_refuses_binding_apply_and_a_fresh_preview_succeeds_after_release(db, monkeypatch):
    seed = _seed(db)
    preview = _preview(db, seed)
    db.commit()
    source = sources._claim(seed[1])
    with pytest.raises(migration.MigrationError) as error:
        _apply(db, preview)
    assert error.value.code == "busy" and _binding(db, seed[3]) == seed[5]
    db.rollback()
    assert sources._apply(source, Catalogue([SourceRecord("new-id", "Refreshed title", "ebook", isbn=ISBN)]))["ok"]
    fresh = _preview(db, seed)
    assert _apply(db, fresh)["ok"] and _binding(db, seed[3]) == seed[4]


def test_parallel_apply_serializes_and_second_request_is_a_receipt_not_a_second_write(db):
    seed = _seed(db)
    preview = _preview(db, seed)
    db.commit()
    barrier = threading.Barrier(2)

    def act():
        barrier.wait()
        with get_db() as conn:
            return _apply(conn, preview)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: act(), range(2)))
    assert all(result["ok"] for result in results)
    assert sorted(result["replayed"] for result in results) == [False, True]
    assert db.execute("SELECT revision FROM source_binding_versions WHERE record_id = ?", (seed[3],)).fetchone()[0] == 1


@pytest.mark.parametrize("bad", [True, 0, -1, 2**63, "1.2", "1e2", "١", {}, None])
def test_unbindable_ids_are_rejected(db, bad):
    with pytest.raises(migration.MigrationError):
        migration.preview_repair(db, actor_id=ACTOR, source_id=bad, record_id=1, item_id=2, manual_reviewed=True)


def test_manual_provider_and_trashed_catalogue_are_never_adopted(db):
    manual = _instance(db, provider="links")
    record_id, item_id = _record(db, manual)
    target = _insert_item(db, isbn=None)
    db.commit()
    with pytest.raises(migration.MigrationError, match="Manual and legacy"):
        migration.preview_repair(db, actor_id=ACTOR, source_id=manual, record_id=record_id, item_id=target, manual_reviewed=True)
    seed = _seed(db)
    item_write.trash_item(db, seed[5])
    db.commit()
    with pytest.raises(migration.MigrationError):
        _preview(db, seed)
    assert db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 3


def test_new_tables_bootstrap_an_existing_database_without_changing_saved_bindings(db):
    seed = _seed(db)
    db.execute("DROP TABLE source_binding_versions")
    db.execute("DROP TABLE source_migration_plans")
    db.commit()
    init_db()
    assert _binding(db, seed[3]) == seed[5]
    assert db.execute("SELECT COUNT(*) FROM source_migration_plans").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM source_binding_versions").fetchone()[0] == 0
