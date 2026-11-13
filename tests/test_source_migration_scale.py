"""31,400-record scopes: bounded pages, whole-scope evidence and stale fences."""

import json
from time import perf_counter
import tracemalloc

import pytest

from app.services import source_migration as migration
from app.database import get_db
from tests.test_source_migration import ACTOR, _apply, _binding, _receipt

COUNT = 31400


@pytest.fixture
def large_pair(db):
    old = db.execute("INSERT INTO source_instances (provider, name, base_url) VALUES ('opds', 'Large old', 'http://old.invalid')").lastrowid
    new = db.execute("INSERT INTO source_instances (provider, name, base_url) VALUES ('opds', 'Large new', 'http://new.invalid')").lastrowid
    db.executemany("INSERT INTO items (id, title, media_type, owned, source) VALUES (?, 'Same title', 'ebook', 0, 'test')",
                   ((number,) for number in range(1, COUNT * 2 + 1)))
    for source_id, start, prefix in ((old, 0, "old"), (new, COUNT, "new")):
        db.executemany(
            "INSERT INTO source_records (id, source_id, item_id, external_id, title, media_type, metadata_json) "
            "VALUES (?, ?, ?, ?, 'Same title', 'ebook', ?)",
            ((start + number, source_id, start + number, f"{prefix}:{number:06d}",
              json.dumps({"provider_ids": {"Asin": f"B{number:09d}"}})) for number in range(1, COUNT + 1)),
        )
    db.commit()
    return old, new


def _preview(db, pair, old_id=COUNT, new_id=COUNT * 2, **options):
    return migration.preview_replacement(db, actor_id=ACTOR, old_source_id=pair[0], new_source_id=pair[1],
                                         mappings=[{"old_record_id": old_id, "new_record_id": new_id}], **options)


def test_large_paged_inventory_complete_fingerprints_late_mapping_and_replay(db, large_pair):
    tracemalloc.start()
    started = perf_counter()
    context = migration.workspace(db, *large_pair, old_after=COUNT - 100, new_after=COUNT * 2 - 100)
    elapsed = perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"BENCH migration records_per_source={COUNT} page={migration.PAGE_SIZE} seconds={elapsed:.3f} python_peak_bytes={peak}")
    assert peak < 12 * 1024 * 1024  # A full item/record hydration is many times larger.
    assert len(context["old_records"]) == len(context["new_records"]) == migration.PAGE_SIZE
    assert context["old_page"]["total"] == context["new_page"]["total"] == COUNT
    repair_page = migration.repair_records(db, large_pair[1], after=COUNT * 2 - 50)
    assert repair_page["total"] == COUNT and len(repair_page["records"]) == migration.PAGE_SIZE
    assert repair_page["records"][-1]["id"] == COUNT * 2 and repair_page["previous_before"] is not None
    assert len(context["suggestions"]) == migration.PAGE_SIZE
    following = migration.workspace(db, *large_pair, old_after=context["old_page"]["next_after"],
                                    new_after=context["new_page"]["next_after"])
    assert following["old_records"][-1]["id"] == COUNT and following["new_records"][-1]["id"] == COUNT * 2
    assert following["old_page"]["next_after"] is None and following["new_page"]["next_after"] is None
    previous = migration.record_page(db, large_pair[0], before=following["old_page"]["previous_before"])
    assert [row["id"] for row in previous["records"]] == [row["id"] for row in context["old_records"]]
    exact = migration.record_page(db, large_pair[1], external_id=f"new:{COUNT:06d}")
    assert len(exact["records"]) == 1 and exact["records"][0]["id"] == COUNT * 2

    # The selected pair is on page one; changing an unseen late-page row must
    # invalidate its preview, not merely update an approximate coverage count.
    early = _preview(db, large_pair, old_id=1, new_id=COUNT + 1)
    db.execute("UPDATE source_records SET title = 'Late unseen change' WHERE id = ?", (COUNT * 2 - 2,))
    db.commit()
    with pytest.raises(migration.MigrationError) as error:
        _apply(db, early)
    assert error.value.code == "stale_preview"
    db.rollback()

    preview = _preview(db, large_pair)
    assert preview["plan"]["coverage"] == {"old_records": COUNT, "new_records": COUNT, "selected": 1,
                                             "unmapped_old": COUNT - 1, "unmapped_new": COUNT - 1}
    started = perf_counter()
    assert _apply(db, preview)["ok"]
    print(f"BENCH migration complete_apply_seconds={perf_counter() - started:.3f}")
    assert _binding(db, COUNT * 2) == COUNT
    assert db.execute("SELECT COUNT(*) FROM source_records WHERE item_id != id").fetchone()[0] == 1
    changes = db.total_changes
    assert _apply(db, preview)["replayed"] and db.total_changes == changes
    assert _receipt(db, preview)["coverage"]["unmapped_old"] == COUNT - 1
    fingerprint = _receipt(db, preview)["retire_fingerprint"]
    db.execute("UPDATE source_records SET availability = 'unavailable' WHERE id = ?", (COUNT - 3,))
    db.commit()
    with pytest.raises(migration.MigrationError) as error:
        migration.retire(db, preview["id"], actor_id=ACTOR, fingerprint=fingerprint, reviewed=True)
    assert error.value.code == "coverage_changed"
    db.rollback()
    current = _receipt(db, preview)
    assert current["coverage_changed"] and "retire_fingerprint" not in current
    assert migration.rollback(db, preview["id"], actor_id=ACTOR, fingerprint=current["rollback_fingerprint"], reviewed=True)["ok"]
    assert _binding(db, COUNT * 2) == COUNT * 2
    assert db.execute("SELECT COUNT(*) FROM source_records WHERE item_id != id").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == COUNT * 2
    assert db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == COUNT * 2
    assert db.execute("SELECT COUNT(*) FROM copies_live").fetchone()[0] == 0


def test_large_candidates_check_collisions_outside_page_and_never_match_titles(db, large_pair):
    for source_id, item_id, identity, asin in ((large_pair[0], 1, "late-old-collision", "B000000001"),
                                             (large_pair[1], COUNT + 2, "late-new-collision", "B000000002")):
        db.execute("INSERT INTO source_records (source_id, item_id, external_id, title, media_type, metadata_json) "
                   "VALUES (?, ?, ?, 'Same title', 'ebook', ?)",
                   (source_id, item_id, identity, json.dumps({"provider_ids": {"Asin": asin}})))
    db.execute("UPDATE source_records SET metadata_json = '{}' WHERE id IN (?, ?)", (3, COUNT + 3))
    db.commit()
    context = migration.workspace(db, *large_pair)
    suggested = {pair["new_record_id"] for pair in context["suggestions"]}
    assert not {COUNT + 1, COUNT + 2, COUNT + 3} & suggested
    assert COUNT + 4 in suggested and len(suggested) == migration.PAGE_SIZE - 3
    for old_id, new_id in ((1, COUNT + 1), (2, COUNT + 2), (3, COUNT + 3)):
        with pytest.raises(migration.MigrationError) as error:
            _preview(db, large_pair, old_id=old_id, new_id=new_id)
        assert error.value.code == "review_required"
    manual = _preview(db, large_pair, old_id=3, new_id=COUNT + 3, manual_reviewed=True)
    assert manual["plan"]["coverage"]["old_records"] == manual["plan"]["coverage"]["new_records"] == COUNT + 1
    assert _apply(db, manual)["ok"] and _binding(db, COUNT + 3) == 3


@pytest.mark.parametrize("cursor", [-1, True, 2**63, "untrusted"])
def test_inventory_cursors_are_bounded_ids_not_client_scope_definitions(db, cursor):
    source_id = db.execute("INSERT INTO source_instances (provider, name, base_url) VALUES ('opds', 'Local', 'http://old.invalid')").lastrowid
    with pytest.raises(migration.MigrationError):
        migration.record_page(db, source_id, after=cursor)


def test_page_snapshot_and_later_preview_recheck_concurrent_identity_collision(db, monkeypatch):
    from tests.test_source_migration import _seed, _preview as preview_pair, ISBN

    seed = _seed(db)
    original, changed = migration._iter_inventory, []

    def concurrent(conn, source_id):
        for record in original(conn, source_id):
            if not changed:
                changed.append(True)
                with get_db() as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute("INSERT INTO source_records (source_id, item_id, external_id, title, media_type, metadata_json) "
                                   "VALUES (?, ?, 'new-collision', 'Same title', 'ebook', ?)",
                                   (seed[0], seed[4], json.dumps({"provider_ids": {"Isbn13": ISBN}})))
            yield record

    monkeypatch.setattr(migration, "_iter_inventory", concurrent)
    db.execute("BEGIN")
    context = migration.workspace(db, seed[0], seed[1])
    assert context["old_page"]["total"] == 1 and len(context["suggestions"]) == 1
    db.commit()
    monkeypatch.setattr(migration, "_iter_inventory", original)
    with pytest.raises(migration.MigrationError) as error:
        preview_pair(db, seed)
    assert error.value.code == "review_required"
    assert db.execute("SELECT COUNT(*) FROM source_records WHERE source_id = ?", (seed[0],)).fetchone()[0] == 2
