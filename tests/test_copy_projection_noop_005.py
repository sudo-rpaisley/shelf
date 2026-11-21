"""Canonical metadata/cover SQL writes must not edit unchanged physical copies.

Cover-path writes below exercise the real item_write/holdings projection only;
they do not publish image bytes or claim browser/file-publication coverage.
"""

import pytest

from app.services import holdings, item_copies, item_write
from tests.conftest import _insert_item, _insert_node_location, _legacy_location

_CREATED = "1998-01-02 03:04:05"
_UPDATED = "2001-02-03 04:05:06"
_CHANGES = {
    "metadata": {"title": "Updated title", "publisher": "Updated publisher", "notes": "Updated shared notes"},
    "cover_only": {"cover_path": "covers/sql-only-005.png"},
}


def _rows(db):
    # Physical table, all columns INCLUDING timestamps, deletion and provenance.
    return [dict(row) for row in db.execute("SELECT * FROM item_copies ORDER BY id")]


def _locations(db):
    root = _insert_node_location(db, "Projection original room")
    other = _insert_node_location(db, "Projection destination room")
    child = _insert_node_location(db, "Precise shelf", parent_id=root)
    unmapped = db.execute("INSERT INTO location_nodes (name) VALUES ('Unmapped precise root')").lastrowid
    flat, other_flat = _legacy_location(db, root), _legacy_location(db, other)
    assert flat is not None and flat != root
    assert other_flat is not None and other_flat != other
    return {"unlocated": None, "mapped_root": root, "precise_child": child,
            "unmapped_root": unmapped}, flat, other_flat, other


def _copy(db, item_id, node, *, number=4, primary=True, replay=False):
    return item_copies.insert_copy(db, {
        "item_id": item_id, "copy_number": number, "location_id": node,
        "is_primary": int(primary), "position_order": number + 5,
        "copy_barcode": f"COPY-005-{item_id}-{number}", "condition": "Stored grade",
        "notes": "Stored copy notes", "acquired_date": "1998-01-02",
        "acquisition_source": "Original shop", "acquisition_price": 12.5,
        "provenance": "Signed first owner", "missing_since": "2000-01-01",
        "created_at": _CREATED, "updated_at": _UPDATED,
    }, replay=replay)


def _seed(db, placement):
    nodes, flat, other_flat, other = _locations(db)
    # Explicit RAW fixture: creation is not the operation under test. An unmapped
    # precise NODE deliberately wins even over an inconsistent old FLAT seam.
    item_id = _insert_item(db, title="Original title", isbn=None,
        location_id=None if placement == "unlocated" else flat,
        publisher="Original publisher", notes="Original shared notes",
        cover_path="covers/original-005.png", cover_edition_isbn="9780735211292",
        cover_edition_language="en")
    primary = _copy(db, item_id, nodes[placement])
    _copy(db, item_id, other, number=9, primary=False)
    assert all(row["updated_at"] == _UPDATED for row in _rows(db))
    return item_id, primary, other_flat, other


def _write(db, item_id, fields, writer):
    if writer == "single":
        item_write.update_item_fields(db, item_id, fields)
    else:
        assert writer == "bulk"
        item_write.update_items_fields(db, [item_id], fields)


@pytest.mark.parametrize("writer", ["single", "bulk"])
@pytest.mark.parametrize("placement", ["unlocated", "mapped_root", "precise_child", "unmapped_root"])
@pytest.mark.parametrize("kind", ["metadata", "cover_only"])
def test_canonical_metadata_and_cover_writes_preserve_every_copy_column(db, writer, placement, kind):
    item_id, _primary, _other_flat, _other = _seed(db, placement)
    before = _rows(db)
    original = dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())

    _write(db, item_id, _CHANGES[kind], writer)

    assert _rows(db) == before
    current = dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
    for name, value in _CHANGES[kind].items():
        assert current[name] == value and current[name] != original[name]
    assert current["location_id"] == original["location_id"]
    if kind == "cover_only":
        assert current["cover_edition_isbn"] is None and current["cover_edition_language"] is None
        for name in ("title", "publisher", "notes", "isbn", "owned"):
            assert current[name] == original[name]


@pytest.mark.parametrize("placement", ["unlocated", "mapped_root"])
def test_identical_cover_projection_does_not_invoke_the_copy_writer(db, monkeypatch, placement):
    item_id, _primary, _other_flat, _other = _seed(db, placement)
    before = _rows(db)

    def reject_update(*args, **kwargs):
        raise AssertionError("an identical projection must not invoke update_copy")

    monkeypatch.setattr(item_copies, "update_copy", reject_update)
    item_write.update_item_fields(db, item_id, _CHANGES["cover_only"])
    assert _rows(db) == before
    assert db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()[0] == _CHANGES["cover_only"]["cover_path"]


@pytest.mark.parametrize("writer", ["single", "bulk"])
@pytest.mark.parametrize("placement,target", [
    ("mapped_root", "other"), ("mapped_root", "clear"), ("unlocated", "other"),
])
def test_actual_flat_move_or_clear_uses_the_same_copy_and_preserves_secondaries(db, writer, placement, target):
    item_id, primary, other_flat, other = _seed(db, placement)
    before = _rows(db)
    flat, node = (other_flat, other) if target == "other" else (None, None)

    _write(db, item_id, {"location_id": flat}, writer)

    after = _rows(db)
    assert after[0]["id"] == primary and after[0]["updated_at"] != _UPDATED
    assert after == [{**before[0], "location_id": node, "position_order": None,
                      "updated_at": after[0]["updated_at"]}, *before[1:]]
    assert db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()[0] == flat


@pytest.mark.parametrize("writer", ["single", "bulk"])
@pytest.mark.parametrize("placement", ["precise_child", "unmapped_root"])
@pytest.mark.parametrize("target", ["other", "clear"])
def test_explicit_flat_move_or_clear_never_flattens_a_precise_copy(db, writer, placement, target):
    item_id, _primary, other_flat, _other = _seed(db, placement)
    before = _rows(db)
    flat = other_flat if target == "other" else None
    assert db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()[0] != flat

    _write(db, item_id, {"location_id": flat}, writer)

    assert _rows(db) == before
    assert db.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()[0] == flat


@pytest.mark.parametrize("writer", ["single", "bulk"])
@pytest.mark.parametrize("history", ["none", "all_trash"])
def test_metadata_repeated_owned_and_reads_never_backfill_an_intentionally_empty_item(db, writer, history):
    nodes, flat, other_flat, _other = _locations(db)
    item_id = _insert_item(db, title="Intentional zero copies", isbn=None, location_id=flat)
    if history == "all_trash":
        copy_id = _copy(db, item_id, nodes["mapped_root"])
        live = _rows(db)
        with pytest.raises(ValueError, match="must keep at least one copy"):
            item_copies.trash_copy(db, copy_id)
        assert _rows(db) == live
        # Reproduce an intentionally all-Trash archive, not an HTTP bypass.
        item_copies.trash_copy(db, copy_id, replay=True, at="2001-02-04 05:06:07")
        db.execute("UPDATE item_copies SET updated_at = ? WHERE id = ?", (_UPDATED, copy_id))
    before = _rows(db)
    assert len(before) == (1 if history == "all_trash" else 0)
    if before:
        assert before[0]["deleted_at"] == "2001-02-04 05:06:07"

    for fields in (_CHANGES["metadata"], _CHANGES["cover_only"], {"owned": 1, "location_id": other_flat}):
        _write(db, item_id, fields, writer)
        assert _rows(db) == before
    for _ in range(2):
        holdings.ensure_foundation(db)
        holdings.sync_all_holdings(db)
        assert item_copies.backfill_legacy_locations(db) == 0
        assert item_copies.copies_for_item(db, item_id) == []
        assert _rows(db) == before
    if history == "all_trash":
        holdings.sync_item(db, item_id, create_copy=True)
        assert _rows(db) == before, "even explicit creation intent must preserve Trash history"


@pytest.mark.parametrize("placement", ["unlocated", "mapped_root"])
@pytest.mark.parametrize("history", ["none", "all_trash"])
def test_real_explicit_acquisition_creates_only_without_prior_copy_history(db, placement, history):
    nodes, flat, _other_flat, _other = _locations(db)
    legacy = None if placement == "unlocated" else flat
    item_id = _insert_item(db, title="Explicit acquisition", isbn=None, owned=0, location_id=legacy)
    if history == "all_trash":
        copy_id = _copy(db, item_id, nodes[placement], replay=True)
        item_copies.trash_copy(db, copy_id, at="2001-02-04 05:06:07")
        db.execute("UPDATE item_copies SET updated_at = ? WHERE id = ?", (_UPDATED, copy_id))
    before = _rows(db)

    item_write.update_item_fields(db, item_id, {"owned": 1, "location_id": legacy})

    assert db.execute("SELECT owned FROM items WHERE id = ?", (item_id,)).fetchone()[0] == 1
    after = _rows(db)
    if history == "all_trash":
        assert after == before
        assert item_copies.copies_for_item(db, item_id) == []
    else:
        assert before == [] and len(after) == 1
        assert (after[0]["item_id"], after[0]["copy_number"], after[0]["is_primary"], after[0]["location_id"]) == (
            item_id, 1, 1, nodes[placement])
        assert after[0]["copy_barcode"] is None and after[0]["position_order"] is None
    # A just-created copy's native timestamp could hide a same-second no-op
    # write. Reset only the fixture timestamp before the repeated-owned probe.
    db.execute("UPDATE item_copies SET updated_at = ? WHERE item_id = ?", (_UPDATED, item_id))
    after = _rows(db)
    item_write.update_item_fields(db, item_id, {"owned": 1, "notes": "Not another acquisition"})
    assert _rows(db) == after


@pytest.mark.parametrize("placement", ["unlocated", "mapped_root"])
def test_new_owned_creation_and_last_copy_refusal_remain_native(db, placement):
    nodes, flat, _other_flat, _other = _locations(db)
    item_id = item_write.insert_item(db, title="New owned physical item", owned=1,
                                    location_id=None if placement == "unlocated" else flat)
    copies = _rows(db)
    assert len(copies) == 1
    assert (copies[0]["item_id"], copies[0]["copy_number"], copies[0]["is_primary"], copies[0]["location_id"]) == (
        item_id, 1, 1, nodes[placement])
    assert copies[0]["copy_barcode"] is None and copies[0]["position_order"] is None
    with pytest.raises(ValueError, match="must keep at least one copy"):
        item_copies.trash_copy(db, copies[0]["id"])
    assert _rows(db) == copies


@pytest.mark.parametrize("placement", ["unlocated", "mapped_root"])
def test_explicit_copy_metadata_edits_at_the_same_location_still_really_write(db, placement):
    _item_id, primary, _other_flat, _other = _seed(db, placement)
    before = _rows(db)

    assert item_copies.update_copy(db, primary, {
        "location_id": before[0]["location_id"], "notes": "Explicit copy metadata edit", "condition": "Fine",
    }) is True

    after = _rows(db)
    assert after[0]["updated_at"] != _UPDATED
    assert after == [{**before[0], "notes": "Explicit copy metadata edit", "condition": "Fine",
                      "updated_at": after[0]["updated_at"]}, *before[1:]]
