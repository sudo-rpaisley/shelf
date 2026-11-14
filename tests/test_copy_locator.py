"""Bounded exact-copy reads, real hierarchy and native Arrange ordering."""

import sqlite3

import pytest

from app.services import copy_locator, item_copies, location_order, locations
from tests.conftest import _insert_item


def _copy(db, location_id=None, *, title="A copy", number=1, position=None,
          item_id=None, primary=0, missing=None, barcode=None):
    if item_id is None:
        item_id = _insert_item(db, title=title, isbn=None)
    copy_id = item_copies.insert_copy(db, {
        "item_id": item_id, "copy_number": number, "location_id": location_id,
        "is_primary": primary, "position_order": position,
        "missing_since": missing, "copy_barcode": barcode,
    })
    return item_id, copy_id


def _ids(rows):
    return [row["copy_id"] for row in rows]


def test_exact_secondary_not_item_number_or_primary_seam(db):
    primary_location = locations.create_location(db, "Primary shelf")
    actual = locations.create_location(db, "Secondary shelf")
    _copy(db, primary_location, title="Decoy")
    item_id = _insert_item(db, title="Shared title", isbn=None, location_id=primary_location)
    _, primary = _copy(db, primary_location, item_id=item_id, primary=1)
    _, target = _copy(db, actual, item_id=item_id, number=7, position=90,
                      missing="2026-01-02", barcode="SECONDARY-7")
    context = copy_locator.locate_copy(db, item_id, target, at=primary_location)
    row = context["target"]
    assert row["copy_id"] == target != primary
    assert row["item_id"] == item_id
    assert row["copy_number"] == 7
    assert row["position_order"] == 90  # saved value, not window index/rank
    assert row["copy_barcode"] == "SECONDARY-7"
    assert row["missing_since"] == "2026-01-02"
    assert row["media_type"] == "book" and row["title"] == "Shared title"
    assert row["changed_at"]
    assert context["location"]["id"] == actual
    assert context["location_changed"]
    assert context["before"] == context["after"] == []


@pytest.mark.parametrize("removed", ["wrong-item", "copy-trash", "item-trash", "no-copy"])
def test_live_exact_pair_or_no_context_never_substitution(db, removed):
    location_id = locations.create_location(db, "Shelf")
    item_id, copy_id = _copy(db, location_id)
    other, other_copy = _copy(db, location_id, title="Other private metadata")
    if removed == "wrong-item":
        item_id = other
    elif removed == "copy-trash":
        item_copies.trash_copy(db, copy_id)
    elif removed == "item-trash":
        from app.services.item_write import trash_item
        trash_item(db, item_id)
    else:
        db.execute("DELETE FROM item_copies WHERE id = ?", (copy_id,))
    assert copy_locator.locate_copy(db, item_id, copy_id) is None
    assert copy_locator.locate_copy(db, other, other_copy) is not None


@pytest.mark.parametrize("invalid", [-1, 0, 1 << 63, True, "1"])
def test_invalid_exact_ids_are_bounded_before_sql(db, invalid):
    item_id, copy_id = _copy(db)
    assert copy_locator.locate_copy(db, invalid, copy_id) is None
    assert copy_locator.locate_copy(db, item_id, invalid) is None


@pytest.mark.parametrize("invalid", [-1, 1 << 63, True, "1"])
def test_invalid_hint_cannot_become_a_record_selector(db, invalid):
    item_id, copy_id = _copy(db)
    with pytest.raises(ValueError, match="Invalid location hint"):
        copy_locator.locate_copy(db, item_id, copy_id, at=invalid)


def test_null_location_and_position_are_not_filled_from_item_seam(db):
    seam = locations.create_location(db, "Legacy seam")
    item_id = _insert_item(db, title="Unlocated exact copy", isbn=None, location_id=seam)
    _, copy_id = _copy(db, item_id=item_id, missing="2026-01-02")
    context = copy_locator.locate_copy(db, item_id, copy_id, at=0)
    assert context["location"] is None
    assert context["ancestors"] == context["before"] == context["after"] == []
    assert context["target"]["location_id"] is None
    assert context["target"]["position_order"] is None
    assert not context["location_changed"]
    assert context["previously_unlocated"]
    assert not context["window_limited"]


def test_neighbour_window_matches_independently_specified_null_last_ties(db):
    shelf = locations.create_location(db, "Native order")
    other = locations.create_location(db, "Not this shelf")
    specs = [
        (None, "a", 1), (4, "z", 1), (4, "A", 2), (0, "Z", 1),
        (4, "a", 1), (4, "a", 1), (None, "A", 2), (None, "z", 1),
        (-5, "Z", 1), (4, "Ä", 1), (4, "ä", 1), (4, "A", 1),
    ]
    pairs = [_copy(db, shelf, position=p, title=t, number=n) for p, t, n in specs]
    # SQLite NOCASE folds ASCII only; equal titles/copy numbers tie by copy ID.
    indexes = [8, 3, 4, 5, 11, 2, 1, 9, 10, 0, 6, 7]
    expected = [pairs[index][1] for index in indexes]
    _copy(db, other, position=4, title="a")
    trashed_item, _ = _copy(db, shelf, title="A", position=4)
    db.execute("UPDATE items SET deleted_at = '2026-01-01' WHERE id = ?", (trashed_item,))
    _, trashed_copy = _copy(db, shelf, title="A", position=4)
    db.execute("UPDATE item_copies SET deleted_at = '2026-01-01' WHERE id = ?", (trashed_copy,))

    assert _ids(location_order.direct_copies(db, shelf)) == expected
    for index, copy_id in enumerate(expected):
        item_id = next(item for item, cid in pairs if cid == copy_id)
        context = copy_locator.locate_copy(db, item_id, copy_id)
        assert _ids(context["before"]) == expected[max(0, index - 3):index]
        assert _ids(context["after"]) == expected[index + 1:index + 4]
        assert context["target"]["copy_id"] == copy_id
        assert context["before_limit_reached"] == (index >= 3)
        assert context["after_limit_reached"] == (index + 3 < len(expected))


class _RecordingDB:
    """Count actual fetched rows and parameters, not just the final slice."""
    def __init__(self, db):
        self.db = db
        self.calls = []

    def execute(self, sql, parameters=()):
        record = {"sql": sql, "parameters": parameters, "rows": 0}
        self.calls.append(record)
        cursor = self.db.execute(sql, parameters)

        class Cursor:
            def fetchone(self):
                row = cursor.fetchone()
                record["rows"] += int(row is not None)
                return row

            def fetchall(self):
                rows = cursor.fetchall()
                record["rows"] += len(rows)
                return rows

        return Cursor()


def test_late_large_shelf_target_with_fixed_sql_parameters_and_row_hydration(db, monkeypatch):
    shelf = locations.create_location(db, "Large shelf")
    pairs = [_copy(db, shelf, title=f"Title {n:04}", position=n) for n in range(1400)]
    recorder = _RecordingDB(db)

    def forbidden(*args, **kwargs):
        pytest.fail("locator called full Arrange/enrichment reader")

    monkeypatch.setattr(location_order, "direct_copies", forbidden)
    monkeypatch.setattr(location_order, "_optional_metadata", forbidden)
    changes_before = db.total_changes
    context = copy_locator.locate_copy(recorder, *pairs[1350])
    assert context["target"]["copy_id"] == pairs[1350][1]
    assert _ids(context["before"]) == [pair[1] for pair in pairs[1347:1350]]
    assert _ids(context["after"]) == [pair[1] for pair in pairs[1351:1354]]
    assert len(recorder.calls) == 4  # exact pair, one node, before, after
    assert sum(call["rows"] for call in recorder.calls) == 8
    for call in recorder.calls:
        assert " IN (" not in call["sql"]
        assert "periodical" not in call["sql"] and "music" not in call["sql"]
        assert len(call["parameters"]) <= 7
    for call in recorder.calls[-2:]:
        assert "WHERE c.location_id = ?" in call["sql"]
        assert "LIMIT ?" in call["sql"]
        assert call["parameters"][0] == shelf
        assert call["parameters"][-1] == 3
        assert call["rows"] == 3
    assert db.total_changes == changes_before


def test_real_parent_ids_not_slashes_and_current_rename_reparent(db):
    root = locations.create_location(db, "Room / literal slash")
    new_root = locations.create_location(db, "Other room")
    parent = locations.create_location(db, "Cabinet", parent_id=root)
    shelf = locations.create_location(db, "Shelf", parent_id=parent)
    item_id, copy_id = _copy(db, shelf)
    context = copy_locator.locate_copy(db, item_id, copy_id, at=shelf)
    assert [node["id"] for node in context["ancestors"]] == [root, parent]
    assert len(context["ancestors"]) == 2
    locations.update_location(db, parent, "Renamed cabinet", parent_id=new_root)
    context = copy_locator.locate_copy(db, item_id, copy_id, at=shelf)
    assert [node["id"] for node in context["ancestors"]] == [new_root, parent]
    assert context["ancestors"][-1]["label"] == "Renamed cabinet"
    assert context["location"]["name"] == "Other room / Renamed cabinet / Shelf"
    assert not context["location_changed"]  # no name/history hint to compare


@pytest.mark.parametrize("old_hint", [None, 0, 987654321])
def test_move_uses_current_exact_location_and_hint_never_reads_old_metadata(db, old_hint):
    first = locations.create_location(db, "Before")
    second = locations.create_location(db, "After")
    item_id, copy_id = _copy(db, first, position=37)
    item_copies.update_copy(db, copy_id, {"location_id": second})
    recorder = _RecordingDB(db)
    context = copy_locator.locate_copy(recorder, item_id, copy_id, at=old_hint)
    assert context["location"]["id"] == second
    assert context["target"]["position_order"] is None
    assert context["location_changed"] == (old_hint is not None)
    assert [c["parameters"] for c in recorder.calls if "FROM locations" in c["sql"]] == [(second,)]


@pytest.mark.parametrize("shape", ["self-cycle", "cycle", "deep"])
def test_cycle_safe_and_capped_ancestry(db, shape):
    root = locations.create_location(db, "Root")
    shelf = locations.create_location(db, "Shelf", parent_id=root)
    if shape == "self-cycle":
        db.execute("UPDATE locations SET parent_id = id WHERE id = ?", (shelf,))
    elif shape == "cycle":
        db.execute("UPDATE locations SET parent_id = ? WHERE id = ?", (shelf, root))
    else:
        for n in range(copy_locator.MAX_ANCESTRY_NODES + 5):
            shelf = locations.create_location(db, f"Node {n}", parent_id=shelf)
    item_id, copy_id = _copy(db, shelf)
    recorder = _RecordingDB(db)
    context = copy_locator.locate_copy(recorder, item_id, copy_id)
    ids = [node["id"] for node in context["ancestors"]] + [context["location"]["id"]]
    assert len(ids) == len(set(ids)) <= copy_locator.MAX_ANCESTRY_NODES
    assert context["ancestry_incomplete"]
    assert context["ancestry_issue"] == ("limit" if shape == "deep" else "cycle")
    assert len(recorder.calls) <= copy_locator.MAX_ANCESTRY_NODES + 3


@pytest.mark.parametrize("broken", ["location", "ancestor"])
def test_unavailable_node_is_explicit_without_substitution(db, broken):
    root = locations.create_location(db, "Broken root")
    shelf = locations.create_location(db, "Broken shelf", parent_id=root)
    item_id, copy_id = _copy(db, shelf)
    db.commit()
    # Corrupt-import negative control, not a production write route.
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("DELETE FROM locations WHERE id = ?", (shelf if broken == "location" else root,))
    context = copy_locator.locate_copy(db, item_id, copy_id)
    assert context["ancestry_incomplete"] and context["ancestry_issue"] == "unavailable"
    if broken == "location":
        assert context["location"] is None
        assert context["before"] == context["after"] == []
    else:
        assert context["location"]["id"] == shelf
        assert context["ancestors"] == []


def test_service_reads_work_with_sqlite_query_only(db):
    shelf = locations.create_location(db, "Read only")
    pair = _copy(db, shelf)
    db.commit()
    db.execute("PRAGMA query_only = ON")
    assert copy_locator.locate_copy(db, *pair)["target"]["copy_id"] == pair[1]
    # Negative control proves query_only is actually preventing writes.
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("UPDATE item_copies SET missing_since = NULL")
