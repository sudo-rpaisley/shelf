"""Operation-local digest reuse must never become a connection/session cache."""

import json
import sqlite3
import hashlib

import pytest

from app.database import get_db
from app import config
from app.services import source_migration as migration, sources
from app.services.isbn import canonical_isbn_pair
from tests.test_source_migration import ACTOR, _apply, _binding, _preview, _receipt, _record, _seed


class _InventoryEvidence:
    """Observe actual verification/reads; a valid final hash alone is not proof."""

    def __init__(self, db, monkeypatch, source_ids):
        self.db, self.source_ids = db, sorted(source_ids)
        self.reset()
        original_iter = migration._iter_inventory
        original_combined = migration._combined_inventory
        original_matches = migration._projection_matches
        original_execute = db.execute

        def counted(conn, source_id):
            scan = {"source_id": source_id, "record_ids": [], "complete": False}
            self.scans.append(scan)
            for row in original_iter(conn, source_id):
                scan["record_ids"].append(row["id"])
                yield row
            scan["complete"] = True

        def combined(conn, plan, *, projection=None):
            self.combined_calls.append(projection is not None)
            return original_combined(conn, plan, projection=projection)

        def matches(conn, plan, projection, changes):
            proof = {"expected_ids": sorted(projection["expected"]),
                     "edit_ids": sorted(projection["edits"]), "bounded_record_ids": []}
            self.matches.append(proof)
            self.verifying = proof
            try:
                proof["result"] = original_matches(conn, plan, projection, changes)
                return proof["result"]
            finally:
                self.verifying = None

        def execute(sql, parameters=()):
            if sql == migration._INVENTORY_SELECT + "WHERE r.id = ?":
                assert self.verifying is not None, "Bounded post-read must occur inside verification"
                assert len(parameters) == 1
                self.bounded_record_ids.append(parameters[0])
                self.verifying["bounded_record_ids"].append(parameters[0])
            return original_execute(sql, parameters)

        monkeypatch.setattr(migration, "_iter_inventory", counted)
        monkeypatch.setattr(migration, "_combined_inventory", combined)
        monkeypatch.setattr(migration, "_projection_matches", matches)
        monkeypatch.setattr(db, "execute", execute)

    def reset(self):
        self.scans, self.combined_calls, self.matches, self.bounded_record_ids = [], [], [], []
        self.verifying = None

    def assert_pairs(self, number):
        # Compare identities/order with an independent complete query, not just
        # the iterator's own counter (which could itself miss the same rows).
        pair = [(source_id, [row[0] for row in self.db.execute(
            "SELECT id FROM source_records WHERE source_id = ? ORDER BY external_id", (source_id,),
        )], True) for source_id in self.source_ids]
        assert [(s["source_id"], s["record_ids"], s["complete"]) for s in self.scans] == pair * number

    def assert_fast_apply(self, record_ids):
        self.assert_pairs(1)
        assert self.combined_calls == [True]  # No actual-after fallback stream.
        assert len(self.matches) == 1 and self.matches[0]["result"] is True
        proof = self.matches[0]
        assert proof["expected_ids"] == proof["edit_ids"] == sorted(record_ids)
        assert sorted(proof["bounded_record_ids"]) == sorted(record_ids)
        assert self.bounded_record_ids == proof["bounded_record_ids"]

    def assert_witness_read(self):
        self.assert_pairs(1)  # Still a full current-input read on each operation.
        assert self.combined_calls == self.matches == self.bounded_record_ids == []


def test_applied_receipt_reuses_one_complete_snapshot_not_three_pairs_of_scans(db, monkeypatch):
    seed = _seed(db)
    _record(db, seed[0], "late-unmapped-old", isbn=None)
    _record(db, seed[1], "late-unmapped-new", isbn=None)
    preview = _preview(db, seed)
    _apply(db, preview)
    db.commit()
    original, scans = migration._iter_inventory, []

    def counted(conn, source_id):
        count = 0
        for row in original(conn, source_id):
            count += 1
            yield row
        scans.append((source_id, count))

    monkeypatch.setattr(migration, "_iter_inventory", counted)
    result = _receipt(db, preview)
    assert {"rollback_fingerprint", "retire_fingerprint"} <= result.keys()
    assert result["coverage"]["unmapped_old"] == result["coverage"]["unmapped_new"] == 1
    assert scans == [(seed[0], 2), (seed[1], 2)]


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _digest(rows):
    return hashlib.sha256(b"".join((_json(row) + "\n").encode() for row in rows)).hexdigest()


def _reference_records(db, source_id):
    # Deliberately simple N+1 linear reference on tiny correctness fixtures,
    # independent of the optimized join/stream. NOT performance evidence.
    for row in db.execute("SELECT * FROM source_records WHERE source_id = ? ORDER BY external_id", (source_id,)):
        record = dict(row)
        version = db.execute("SELECT revision FROM source_binding_versions WHERE record_id = ?", (record["id"],)).fetchone()
        live = db.execute("SELECT id FROM items_live WHERE id = ?", (record["item_id"],)).fetchone()
        isbn = db.execute("SELECT value_json FROM item_metadata_provenance WHERE item_id = ? AND source_key = ? AND field_name = 'isbn'",
                          (record["item_id"], sources.metadata_source_key(source_id, record["external_id"]))).fetchone()
        yield record | {"migration_revision": version[0] if version else 0,
                        "migration_live_id": live[0] if live else None, "migration_isbn": isbn[0] if isbn else None}


def _reference_identifiers(db, record):
    # Original v2 normalization, including the intentionally repeated checks.
    cleaned = sources._provider_ids(migration._object(record["metadata_json"]).get("provider_ids"))
    identities = {}
    for name, value in cleaned.items():
        if name.startswith("Isbn"):
            identities.setdefault("ISBN", set()).add(canonical_isbn_pair(value)[0])
        elif name in migration._IDENTITIES:
            identities.setdefault(name, set()).add(value.lower() if name.startswith("MusicBrainz") else value)
    isbn = migration._object(record["migration_isbn"]).get("observed")
    if isinstance(isbn, str):
        import re
        if re.fullmatch(r"[0-9Xx -]{1,100}", isbn):
            pair = canonical_isbn_pair(isbn)
            if pair:
                identities.setdefault("ISBN", set()).add(pair[0])
    return {key: sorted(values) for key, values in sorted(identities.items())}


def _reference_inventory(db, plan):
    raw, reviewed = [], []
    for source_id in sorted({plan["old_source_id"], plan["new_source_id"]}):
        records = list(_reference_records(db, source_id))
        raw.append([source_id, _digest(records)])
        rows = [{key: value for key, value in record.items() if key not in {
            "last_seen_at", "created_at", "updated_at", "migration_revision", "migration_isbn",
        }} | {"identifiers": _reference_identifiers(db, record)} for record in records]
        source = migration._source(db, source_id)
        reviewed.append([[source[key] for key in ("id", "provider", "enabled", "config_revision")], _digest(rows)])
    return raw, _hash(["complete-inventory-v2", reviewed])


@pytest.mark.parametrize("metadata", [
    {"description": ('中文 العربية हिन्दी Истории \\ \"metadata_json\":null\n' * 160),
     "provider_ids": {"Isbn13": "9780306406157", "Asin": "B000000001"}},
    {"provider_ids": {"Isbn10": "0306406152", "Isbn": "978-0-306-40615-7",
                      "MusicBrainzAlbum": "12345678-ABCD-4321-ABCD-1234567890AB"}},
    {"provider_ids": {"Isbn13": "9780306406158", "Asin": "invalid", "Tmdb": "not-an-id"}},
])
def test_fused_stream_and_memo_are_byte_exact_v2_with_all_columns_and_provenance(db, metadata):
    seed = _seed(db, metadata=metadata)
    _record(db, seed[0], 'late-"metadata_json":null-🌍', isbn=None, metadata=metadata)
    db.execute("UPDATE source_records SET library_id = '中文', format = 'EPUB', last_seen_at = '2030-01-01', "
               "open_url = 'https://example.invalid/read?id=1', updated_at = '2030-02-02'")
    db.execute("INSERT INTO item_metadata_provenance (item_id, source_key, field_name, value_json) "
               "VALUES (?, 'other:provider', 'description', ?)", (seed[4], '{"observed":"عربي","local":null,"overridden":true}'))
    preview = _preview(db, seed, manual_reviewed=True)
    db.commit()
    raw, reviewed = _reference_inventory(db, preview["plan"])
    with migration._FingerprintMemo(db, combined=True) as memo:
        assert migration._combined_inventory(db, preview["plan"]) == (raw, reviewed)
        assert migration._inventory_fingerprint(db, preview["plan"], memo=memo) == reviewed
        state = migration._state(db, preview["plan"])
        assert state["complete_inventory"] == raw
        for purpose in ("apply", "rollback", "retire"):
            expected = _hash(["source-migration-v2-streamed", purpose, preview["id"], preview["plan"], state])
            assert migration._fingerprint(db, preview["plan"], purpose=purpose, plan_id=preview["id"], memo=memo) == expected
        assert all(isinstance(value, str) and len(value) == 64 for value in memo._purposes.values())
        assert all(isinstance(digest, str) and len(digest) == 64 for _, digest in memo._raw)
    assert not db.in_transaction
    assert memo._raw is memo._inventory is None and memo._purposes == {}
    with pytest.raises(RuntimeError, match="outside its operation"):
        migration._fingerprint(db, preview["plan"], memo=memo)


def test_own_write_then_inner_rollback_disables_memo_for_the_entire_context(db):
    seed = _seed(db)
    preview = _preview(db, seed)
    db.commit()
    with migration._FingerprintMemo(db, combined=True) as memo:
        before = migration._fingerprint(db, preview["plan"], purpose="rollback", memo=memo)
        db.execute("SAVEPOINT own_edit")
        db.execute("UPDATE source_records SET metadata_json = '{\"description\":\"Changed\"}' WHERE id = ?", (seed[3],))
        changed = migration._fingerprint(db, preview["plan"], purpose="rollback", memo=memo)
        assert changed != before and memo._disabled
        db.execute("ROLLBACK TO SAVEPOINT own_edit")
        db.execute("RELEASE SAVEPOINT own_edit")
        assert migration._fingerprint(db, preview["plan"], purpose="rollback", memo=memo) == before
        assert memo._raw is None and memo._purposes == {}


def _change(conn, seed, late, change):
    if change == "config":
        conn.execute("UPDATE source_instances SET config_revision = config_revision + 1, name = 'Changed' WHERE id = ?", (seed[1],))
    elif change == "late_inventory":
        conn.execute("UPDATE source_records SET availability = 'unavailable' WHERE id = ?", (late,))
    elif change == "metadata":
        conn.execute("UPDATE source_records SET metadata_json = '{\"description\":\"晚 صفحہ\"}' WHERE id = ?", (late,))
    elif change == "provenance":
        conn.execute("INSERT INTO item_metadata_provenance (item_id, source_key, field_name, value_json) "
                     "VALUES (?, 'other:provider', 'description', '{\"observed\":\"New\",\"local\":null}')", (seed[4],))
    else:
        for target in (seed[5], seed[4]):
            repair = migration.preview_repair(conn, actor_id=ACTOR, source_id=seed[1], record_id=seed[3], item_id=target, manual_reviewed=True)
            _apply(conn, repair)


@pytest.mark.parametrize("change", ["config", "late_inventory", "metadata", "provenance", "binding_aba"])
def test_same_open_connection_never_reuses_hashes_across_calls_or_external_commits(db, change):
    seed = _seed(db)
    late, _ = _record(db, seed[1], "zz-late-unmapped", isbn=None)
    preview = _preview(db, seed)
    _apply(db, preview)
    db.commit()
    old = _receipt(db, preview)
    changes = db.total_changes
    with get_db() as writer:
        writer.execute("BEGIN IMMEDIATE")
        _change(writer, seed, late, change)
    assert db.total_changes == changes  # Connection-lifetime counters cannot catch this.
    fresh = _receipt(db, preview)
    if change == "binding_aba":
        assert fresh["conflict"] and "rollback_fingerprint" not in fresh
    else:
        assert fresh["rollback_fingerprint"] != old["rollback_fingerprint"]
    with pytest.raises(migration.MigrationError):
        migration.retire(db, preview["id"], actor_id=ACTOR, fingerprint=old["retire_fingerprint"], reviewed=True)
    db.rollback()
    assert db.execute("SELECT enabled FROM source_instances WHERE id = ?", (seed[0],)).fetchone()[0] == 1
    assert _binding(db, seed[3]) == seed[4]


@pytest.mark.parametrize("change", ["config", "late_inventory", "metadata", "provenance"])
def test_direct_receipt_pins_one_actual_read_snapshot_then_next_call_sees_rival(db, monkeypatch, change):
    seed = _seed(db)
    late, _ = _record(db, seed[1], "zz-late-unmapped", isbn=None)
    preview = _preview(db, seed)
    _apply(db, preview)
    db.commit()
    old = _receipt(db, preview)
    original, changed = migration._iter_inventory, []

    def concurrent(conn, source_id):
        for row in original(conn, source_id):
            if not changed:
                changed.append(True)
                with get_db() as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    _change(writer, seed, late, change)
            yield row

    monkeypatch.setattr(migration, "_iter_inventory", concurrent)
    snapshot = _receipt(db, preview)
    assert snapshot["rollback_fingerprint"] == old["rollback_fingerprint"]
    assert snapshot["retire_fingerprint"] == old["retire_fingerprint"]
    assert not db.in_transaction
    assert _receipt(db, preview)["rollback_fingerprint"] != old["rollback_fingerprint"]


def test_caller_rollback_discards_applied_receipt_even_with_unchanged_connection(db):
    seed = _seed(db)
    preview = _preview(db, seed)
    db.commit()
    _apply(db, preview)
    assert _receipt(db, preview)["state"] == "applied"
    db.rollback()
    fresh = _receipt(db, preview)
    assert fresh["state"] == "preview" and not fresh["conflict"] and "rollback_fingerprint" not in fresh
    assert _binding(db, seed[3]) == seed[5]
    assert _apply(db, preview)["ok"]


@pytest.mark.parametrize("boundary", ["commit", "rollback"])
def test_transaction_end_and_rebegin_mid_receipt_never_returns_cached_tickets(db, monkeypatch, boundary):
    seed = _seed(db)
    preview = _preview(db, seed)
    _apply(db, preview)
    db.commit()
    original = migration._check_reviewed_inventory

    def crossed(conn, plan, result, **kwargs):
        getattr(conn, boundary)()
        conn.execute("BEGIN")  # in_transaction remains true, but it is NOT the old snapshot.
        return original(conn, plan, result, **kwargs)

    monkeypatch.setattr(migration, "_check_reviewed_inventory", crossed)
    with pytest.raises(migration.MigrationError, match="changed"):
        _receipt(db, preview)
    db.rollback()


@pytest.mark.parametrize("boundary", ["commit", "rollback"])
def test_transaction_end_after_comparison_still_refuses_retire_before_domain_write(db, monkeypatch, boundary):
    seed = _seed(db)
    preview = _preview(db, seed)
    _apply(db, preview)
    db.commit()
    ticket = _receipt(db, preview)["retire_fingerprint"]
    original = migration._check_fingerprint

    def crossed(submitted, expected):
        original(submitted, expected)
        getattr(db, boundary)()
        db.execute("BEGIN")

    monkeypatch.setattr(migration, "_check_fingerprint", crossed)
    with pytest.raises(migration.MigrationError):
        migration.retire(db, preview["id"], actor_id=ACTOR, fingerprint=ticket, reviewed=True)
    db.rollback()
    assert db.execute("SELECT enabled FROM source_instances WHERE id = ?", (seed[0],)).fetchone()[0] == 1
    assert not json.loads(db.execute("SELECT result_json FROM source_migration_plans WHERE id = ?", (preview["id"],)).fetchone()[0])["retired"]


def test_existing_deferred_transaction_is_promoted_before_the_actual_hash_compare(db, monkeypatch):
    seed = _seed(db)
    preview = _preview(db, seed)
    db.commit()
    db.execute("BEGIN")
    db.execute("SELECT id FROM source_records LIMIT 1").fetchone()
    original, checked = migration._state, []

    def reserved(conn, plan, **kwargs):
        rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                rival.execute("BEGIN IMMEDIATE")
        finally:
            rival.close()
        checked.append(True)
        return original(conn, plan, **kwargs)

    monkeypatch.setattr(migration, "_state", reserved)
    assert _apply(db, preview)["ok"] and checked == [True]


def test_stale_deferred_snapshot_cannot_be_used_as_writer_authority(db):
    seed = _seed(db)
    preview = _preview(db, seed)
    db.commit()
    db.execute("BEGIN")
    db.execute("SELECT id FROM source_records LIMIT 1").fetchone()
    with get_db() as writer:
        writer.execute("UPDATE source_records SET title = 'Late writer' WHERE id = ?", (seed[3],))
    with pytest.raises(migration.MigrationError) as error:
        _apply(db, preview)
    assert error.value.code == "busy"
    db.rollback()
    assert _binding(db, seed[3]) == seed[5]
    assert db.execute("SELECT state FROM source_migration_plans WHERE id = ?", (preview["id"],)).fetchone()[0] == "preview"


def test_digest_memo_never_memoizes_actor_or_feature_authority(db):
    seed = _seed(db)
    db.execute("INSERT INTO users (id, username, password, role) VALUES (?, 'MemoAdmin', 'test-only-hash', 'admin')", (ACTOR,))
    preview = _preview(db, seed)
    actor = {"id": ACTOR, "username": "MemoAdmin", "role": "admin", "token_version": 1,
             "expires_at": migration._now().timestamp() + 3600}
    with migration._FingerprintMemo(db, combined=True) as memo:
        migration._fingerprint(db, preview["plan"], memo=memo)
        migration.authorize_mutation(db, actor)
        db.execute("UPDATE users SET role = 'viewer' WHERE id = ?", (ACTOR,))
        with pytest.raises(migration.MigrationError) as denied:
            migration.authorize_mutation(db, actor)
        assert denied.value.code == "actor_changed"
    assert _binding(db, seed[3]) == seed[5]


@pytest.mark.parametrize("target_snapshot", [None, '{"observed":null}', '{"observed":"9780441172719"}',
                                             '{"local":"old","overridden":true}', '{"observed":""}', 'malformed'])
def test_complete_projected_postimage_matches_actual_all_fields_and_target_isbn_precedence(db, monkeypatch, target_snapshot):
    seed = _seed(db)
    _record(db, seed[0], "late-old", isbn=None)
    _record(db, seed[1], "late-new", isbn=None)
    if target_snapshot is not None:
        key = sources.metadata_source_key(seed[1], "new-id")
        db.execute("INSERT INTO item_metadata_provenance (item_id, source_key, field_name, value_json) VALUES (?, ?, 'isbn', ?)",
                   (seed[4], key, target_snapshot))
    preview = _preview(db, seed)
    evidence = _InventoryEvidence(db, monkeypatch, seed[:2])
    assert _apply(db, preview)["ok"]
    evidence.assert_fast_apply(seed[2:4])
    result = json.loads(db.execute("SELECT result_json FROM source_migration_plans WHERE id = ?", (preview["id"],)).fetchone()[0])
    raw, reviewed = _reference_inventory(db, preview["plan"])
    assert result["inventory_fingerprint"] == reviewed
    assert result["inventory_witness"] == {"format": "raw-v2-inputs-1", "records": raw,
                                            "sources": migration._inventory_sources(db, preview["plan"])}
    db.commit()
    evidence.reset()
    receipt = _receipt(db, preview)
    assert not receipt["coverage_changed"] and "retire_fingerprint" in receipt
    evidence.assert_witness_read()
    evidence.reset()
    assert migration.retire(db, preview["id"], actor_id=ACTOR, fingerprint=receipt["retire_fingerprint"], reviewed=True)["ok"]
    evidence.assert_witness_read()
    assert db.execute("SELECT enabled FROM source_instances WHERE id = ?", (seed[0],)).fetchone()[0] == 0


@pytest.mark.parametrize("noop_shares_target", [False, True])
def test_mixed_changed_noop_and_shared_items_use_verified_projection_once_per_operation(db, monkeypatch, noop_shares_target):
    seed = _seed(db)
    old_shared, _ = _record(db, seed[0], "old-shared", item_id=seed[4])
    new_shared, _ = _record(db, seed[1], "new-shared", item_id=seed[5])
    old_noop, noop_item = _record(db, seed[0], "old-noop", item_id=seed[4] if noop_shares_target else None)
    new_noop, _ = _record(db, seed[1], "new-noop", item_id=noop_item)
    _record(db, seed[0], "zz-unmapped-old", isbn=None)
    _record(db, seed[1], "zz-unmapped-new", isbn=None)
    noop_keys = [sources.metadata_source_key(source_id, external_id)
                 for source_id, external_id in ((seed[0], "old-noop"), (seed[1], "new-noop"))]

    def noop_provenance():
        return [tuple(row) for row in db.execute(
            "SELECT * FROM item_metadata_provenance WHERE source_key IN (?, ?) ORDER BY item_id, source_key, field_name", noop_keys,
        )]

    unchanged = noop_provenance()
    preview = migration.preview_replacement(
        db, actor_id=ACTOR, old_source_id=seed[0], new_source_id=seed[1], manual_reviewed=True,
        mappings=[{"old_record_id": old, "new_record_id": new}
                  for old, new in ((seed[2], seed[3]), (old_shared, new_shared), (old_noop, new_noop))],
    )
    assert sum(m["before_item_id"] != m["after_item_id"] for m in preview["plan"]["mappings"]) == 2
    evidence = _InventoryEvidence(db, monkeypatch, seed[:2])
    assert _apply(db, preview)["ok"]
    evidence.assert_fast_apply([seed[2], seed[3], old_shared, new_shared])
    raw, reviewed = _reference_inventory(db, preview["plan"])
    result = json.loads(db.execute("SELECT result_json FROM source_migration_plans WHERE id = ?", (preview["id"],)).fetchone()[0])
    assert result["inventory_witness"]["records"] == raw and result["inventory_fingerprint"] == reviewed
    assert _binding(db, seed[3]) == _binding(db, new_shared) == seed[4]
    assert _binding(db, new_noop) == noop_item
    assert result["versions"][str(new_noop)] == 0
    assert db.execute("SELECT 1 FROM source_binding_versions WHERE record_id IN (?, ?)", (old_noop, new_noop)).fetchone() is None
    assert noop_provenance() == unchanged  # No binding/authority ownership for no-ops, even on a shared target.
    db.commit()
    evidence.reset()
    receipt = _receipt(db, preview)
    assert not receipt["coverage_changed"] and not receipt["conflict"]
    assert receipt["coverage"] == {"old_records": 4, "new_records": 4, "selected": 3, "unmapped_old": 1, "unmapped_new": 1}
    evidence.assert_witness_read()
    evidence.reset()
    assert migration.retire(db, preview["id"], actor_id=ACTOR, fingerprint=receipt["retire_fingerprint"], reviewed=True)["ok"]
    evidence.assert_witness_read()
    assert noop_provenance() == unchanged


@pytest.mark.parametrize("observer", ["trigger", "generated-column"])
def test_unknown_observer_fixture_takes_conservative_actual_full_read_path(db, monkeypatch, observer):
    seed = _seed(db)
    _record(db, seed[0], "late-old", isbn=None)
    _record(db, seed[1], "late-new", isbn=None)
    preview = _preview(db, seed)
    if observer == "trigger":
        # Test-only TEMP constraint, not production DDL or an epoch mechanism.
        db.execute("CREATE TEMP TRIGGER fixture_constraint BEFORE UPDATE ON source_records BEGIN SELECT 1; END")
    else:
        execute = db.execute

        def generated_flag(sql, parameters=()):
            rows = execute(sql, parameters)
            if sql == "PRAGMA table_xinfo(source_records)":
                # Simulate SQLite's generated-column eligibility flag without
                # changing production DDL or lying about actual row contents.
                return iter([tuple(row[:6]) + (2,) for row in rows])
            return rows

        monkeypatch.setattr(db, "execute", generated_flag)
    evidence = _InventoryEvidence(db, monkeypatch, seed[:2])
    assert _apply(db, preview)["ok"]
    evidence.assert_pairs(2)  # Full before-state + ACTUAL after-state, not a fast apply.
    assert evidence.combined_calls == evidence.matches == evidence.bounded_record_ids == []
    result = json.loads(db.execute("SELECT result_json FROM source_migration_plans WHERE id = ?", (preview["id"],)).fetchone()[0])
    assert "inventory_witness" not in result
    assert result["inventory_fingerprint"] == _reference_inventory(db, preview["plan"])[1]
    db.commit()
    evidence.reset()
    receipt = _receipt(db, preview)
    assert not receipt["coverage_changed"]
    evidence.assert_pairs(1)
    assert evidence.combined_calls == [False]  # Fresh complete normalization, NOT raw-witness reuse.
    assert evidence.matches == evidence.bounded_record_ids == []
    evidence.reset()
    assert migration.retire(db, preview["id"], actor_id=ACTOR, fingerprint=receipt["retire_fingerprint"], reviewed=True)["ok"]
    evidence.assert_pairs(1)
    assert evidence.combined_calls == [False]
    assert evidence.matches == evidence.bounded_record_ids == []


@pytest.mark.parametrize("mask_counter", [False, True])
def test_unexpected_actual_write_never_seals_unverified_projection_even_if_count_matches(db, monkeypatch, mask_counter):
    seed = _seed(db)
    _record(db, seed[0], "late-old", isbn=None)
    _record(db, seed[1], "late-new", isbn=None)
    preview = _preview(db, seed)
    original, project = migration._rebind, migration._apply_projection

    def unexpected(conn, record, target, plan_id, stamp=None):
        result = original(conn, record, target, plan_id, stamp)
        conn.execute("UPDATE source_records SET library_id = 'Unexpected actual value' WHERE id = ?", (record["id"],))
        return result

    def counter(conn, plan):
        projection = project(conn, plan)
        if mask_counter:
            projection["changes"] += 1
        return projection

    monkeypatch.setattr(migration, "_rebind", unexpected)
    monkeypatch.setattr(migration, "_apply_projection", counter)
    evidence = _InventoryEvidence(db, monkeypatch, seed[:2])
    assert _apply(db, preview)["ok"]
    evidence.assert_pairs(2)
    assert evidence.combined_calls == [True, False]  # Projected before + conservative actual after.
    assert len(evidence.matches) == 1 and evidence.matches[0]["result"] is False
    assert evidence.matches[0]["expected_ids"] == evidence.matches[0]["edit_ids"] == sorted(seed[2:4])
    # The complete stream orders the old record first; only the changed new
    # record mismatches, after the old record's bounded proof succeeds.
    assert evidence.bounded_record_ids == (list(seed[2:4]) if mask_counter else [])
    result = json.loads(db.execute("SELECT result_json FROM source_migration_plans WHERE id = ?", (preview["id"],)).fetchone()[0])
    raw, reviewed = _reference_inventory(db, preview["plan"])
    assert result["inventory_witness"]["records"] == raw
    assert result["inventory_fingerprint"] == reviewed


def test_witness_raw_mismatch_is_not_a_coverage_failure_for_timestamp_only_refresh(db):
    seed = _seed(db)
    preview = _preview(db, seed)
    _apply(db, preview)
    db.commit()
    before = _receipt(db, preview)
    db.execute("UPDATE source_records SET updated_at = '2030-01-01', last_seen_at = '2030-01-01'")
    db.commit()
    after = _receipt(db, preview)
    assert not after["coverage_changed"] and "retire_fingerprint" in after
    assert before["retire_fingerprint"] != after["retire_fingerprint"]


def test_legacy_receipt_without_raw_witness_keeps_exact_v2_guarded_tickets(db):
    seed = _seed(db)
    preview = _preview(db, seed)
    _apply(db, preview)
    first = _receipt(db, preview)
    result = json.loads(db.execute("SELECT result_json FROM source_migration_plans WHERE id = ?", (preview["id"],)).fetchone()[0])
    result.pop("inventory_witness")
    db.execute("UPDATE source_migration_plans SET result_json = ? WHERE id = ?", (json.dumps(result), preview["id"]))
    second = _receipt(db, preview)
    assert second["retire_fingerprint"] == first["retire_fingerprint"]
    assert second["rollback_fingerprint"] == first["rollback_fingerprint"]
