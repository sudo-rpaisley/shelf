"""Phase4 CSV identity/precise-placement contracts; execute only after freeze."""

import csv
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import _insert_item, _seed_personal_state
from tests.test_csv_roundtrip import _csv, _export, _import


def _dump(db):
    return "\n".join(db.iterdump())


def _key_fields(key):
    return {"title": "Alias target", "authors": "Writer", "isbn": "9780441172719" if key == "isbn" else None,
            **({"upc": "0012345678905"} if key == "upc" else {})}


def _alias_content(key, monkeypatch, **extra):
    from app.services import reading_imports

    if key == "upc":
        # Exercise the route's normalized UPC rung without widening the native
        # nineteen-column generic format. Libib's real UPC parser has separate
        # unchanged tests; this narrow normalizer double changes no authority.
        real = reading_imports.NORMALIZERS[reading_imports.GENERIC]

        def with_upc(row):
            norm = real(row)
            norm["upc"] = row.get("upc")
            return norm

        monkeypatch.setitem(reading_imports.NORMALIZERS, reading_imports.GENERIC, with_upc)
    row = {**_key_fields(key), "media_type": "kids_book", **extra}
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)
    return out.getvalue()


@pytest.mark.parametrize("key", ["isbn", "upc", "title"])
@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("mode", ["skip", "update"])
def test_alias_uses_exact_legacy_first_and_only_then_canonical_slot(
    admin_client, viewer_user, db, monkeypatch, key, retained, deleted, mode,
):
    from app.services import item_write

    canonical = _insert_item(db, **_key_fields(key), media_type="book", publisher="Canonical untouched")
    target = _insert_item(db, **_key_fields(key), media_type="kids_book", publisher="Retained original") if retained else canonical
    _seed_personal_state(db, viewer_user["id"], target, personal_notes="Unchanged other actor", wishlist=1)
    if deleted:
        item_write.trash_item(db, target)
    db.commit()
    before = _dump(db)
    private_before = tuple(db.execute("SELECT * FROM user_item_state WHERE item_id=?", (target,)).fetchone())
    content = _alias_content(key, monkeypatch, publisher="Imported", tags="Incoming tag", location="Unused on skip")
    # Existing zero-copy histories may retain a flat label on update, but may
    # never acquire an invented primary from a metadata/ownership round trip.
    result = _import(admin_client, content, mode=mode)
    assert result["errors"] == []
    assert (result["imported"], result["skipped"], result["restored"]) == (
        int(mode == "update" and not deleted), int(mode == "skip" and not deleted), int(deleted),
    )
    row = db.execute("SELECT id,media_type,publisher,deleted_at FROM items WHERE id=?", (target,)).fetchone()
    assert row[:] == (target, "kids_book" if retained else "book",
                      "Imported" if mode == "update" else "Retained original" if retained else "Canonical untouched", None)
    assert db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 0
    assert tuple(db.execute("SELECT * FROM user_item_state WHERE item_id=?", (target,)).fetchone()) == private_before
    if retained:
        assert db.execute("SELECT publisher,deleted_at FROM items WHERE id=?", (canonical,)).fetchone()[:] == ("Canonical untouched", None)
    if mode == "skip" and not deleted:
        assert _dump(db) == before
    assert result["locations_created"] == ([] if mode == "skip" else ["Unused on skip"])


@pytest.mark.parametrize("alias_first", [False, True])
def test_new_alias_and_canonical_rows_share_the_actual_new_slot_key(admin_client, db, alias_first):
    types = ["kids_book", "book"] if alias_first else ["book", "kids_book"]
    content = "title,isbn,media_type\n" + "".join(f"New alias,9780441172719,{media}\n" for media in types)
    result = _import(admin_client, content)
    assert (result["imported"], result["skipped"], result["errors"]) == (1, 1, [])
    assert [tuple(r) for r in db.execute("SELECT title,media_type FROM items")] == [("New alias", "book")]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["isbn", "upc", "title"])
async def test_hidden_exact_legacy_blocks_visible_canonical_without_row_effects(db, editor_user, monkeypatch, key):
    from app.routers import items_csv
    from app.services import catalogue_security, libraries

    private = libraries.create_library(db, "Undisclosed alias parent")["id"]
    _insert_item(db, **_key_fields(key), media_type="kids_book", _library_id=private, publisher="Hidden original")
    _insert_item(db, **_key_fields(key), media_type="book", publisher="Visible original")
    db.commit()
    before = _dump(db)
    content = _alias_content(key, monkeypatch, publisher="Must not change", location="Must not create", tags="Must not attach")
    upload = SimpleNamespace(read=AsyncMock(return_value=content.encode()))
    request = SimpleNamespace(state=SimpleNamespace(user=editor_user), form=AsyncMock(return_value={"mode": "update", "file": upload}))
    # This is the route's internal service boundary under a real restricted
    # actor, not an override of the separate native admin-only HTTP dependency.
    token = catalogue_security.set_actor(editor_user)
    try:
        result = await items_csv.import_csv(request, _=editor_user)
    finally:
        catalogue_security.reset_actor(token)
    assert result["errors"] == ["Row 2: Item is unavailable or not editable"]
    assert (result["imported"], result["restored"], result["skipped"], result["locations_created"]) == (0, 0, 0, [])
    assert "Hidden original" not in str(result) and "Undisclosed" not in str(result)
    assert _dump(db) == before


@pytest.mark.parametrize("mode", ["Skip", "merge", "overwrite"])
def test_invalid_mode_keeps_exact_envelope_and_never_reads_the_file(admin_client, db, monkeypatch, mode):
    from starlette.datastructures import UploadFile

    _insert_item(db, title="Invalid mode sentinel", isbn=None, publisher="Keep")
    db.commit()
    before = _dump(db)
    read = AsyncMock(side_effect=AssertionError("invalid mode read the upload"))
    monkeypatch.setattr(UploadFile, "read", read)
    result = _import(admin_client, "not even a valid CSV", mode=mode)
    assert result == {"error": "Invalid import mode", "imported": 0, "skipped": 0, "errors": []}
    read.assert_not_awaited()
    assert _dump(db) == before


def test_exact_nested_primary_move_projects_flat_and_preserves_secondary_identity_and_order(admin_client, db):
    from app.services import item_copies, location_tree, locations
    from app.services.item_write import insert_item

    db.execute("INSERT INTO locations(id,name) VALUES(81,'Mapped room')")
    db.execute("INSERT INTO location_nodes(id,name) VALUES(81,'Unrelated integer twin')")
    root = location_tree.legacy_location_to_node(db, 81, create=True)
    old = locations.create_location(db, "Before", parent_id=root)
    new = locations.create_location(db, "After", parent_id=root)
    other = locations.create_location(db, "Other shelf")
    item_id = insert_item(db, title="Exact placement", isbn=None, authors="Writer")
    primary = db.execute("SELECT id FROM copies_live WHERE item_id=? AND is_primary=1", (item_id,)).fetchone()[0]
    item_copies.update_copy(db, primary, {"location_id": old, "position_order": 19})
    secondary = item_copies.add_copy(db, item_id, {"location_id": other, "position_order": 31, "copy_barcode": "csv004-secondary"})
    db.commit()
    second_before = tuple(db.execute("SELECT * FROM item_copies WHERE id=?", (secondary,)).fetchone())
    tree_before = [tuple(r) for r in db.execute("SELECT * FROM location_nodes ORDER BY id")]
    result = _import(admin_client, _csv({"title": "Exact placement", "authors": "Writer", "location": "Mapped room / After"}), mode="update")
    assert (result["imported"], result["errors"], result["locations_created"]) == (1, [], [])
    assert db.execute("SELECT location_id FROM items WHERE id=?", (item_id,)).fetchone()[0] == 81
    assert db.execute("SELECT id,location_id,position_order,copy_number FROM item_copies WHERE id=?", (primary,)).fetchone()[:] == (primary, new, None, 1)
    assert tuple(db.execute("SELECT * FROM item_copies WHERE id=?", (secondary,)).fetchone()) == second_before
    assert [tuple(r) for r in db.execute("SELECT * FROM location_nodes ORDER BY id")] == tree_before
    assert next(csv.DictReader(io.StringIO(_export(admin_client))))["location"] == "Mapped room / After"
    assert new != 81 and location_tree.node_to_legacy_location(db, new) == 81


def test_plain_existing_node_never_adopts_equal_flat_integer(admin_client, db):
    db.execute("INSERT INTO locations(id,name) VALUES(71,'Unrelated flat')")
    db.execute("INSERT INTO location_nodes(id,name) VALUES(71,'Physical only')")
    db.commit()
    result = _import(admin_client, _csv({"title": "Node only", "location": "Physical only"}))
    assert (result["imported"], result["errors"], result["locations_created"]) == (1, [], [])
    assert db.execute("SELECT location_id FROM items WHERE title='Node only'").fetchone()[0] is None
    assert db.execute("SELECT location_id FROM item_copies").fetchone()[0] == 71
    assert db.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 1


@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("precise", [False, True])
def test_intentional_zero_copy_state_is_preserved_or_unrepresentable_path_refused(admin_client, db, deleted, precise):
    from app.services import item_write, locations

    legacy = locations.create_legacy_location(db, "Zero copy flat")
    node = locations.create_location(db, "No flat mapping")
    item_id = _insert_item(db, title="Zero copy history", isbn=None, owned=0, publisher="Original")
    if deleted:
        item_write.trash_item(db, item_id)
    db.commit()
    before = _dump(db)
    label = "No flat mapping" if precise else "Zero copy flat"
    result = _import(admin_client, _csv({"title": "Zero copy history", "owned": "1", "publisher": "Changed", "location": label}), mode="update")
    assert db.execute("SELECT COUNT(*) FROM item_copies WHERE item_id=?", (item_id,)).fetchone()[0] == 0
    if precise:
        assert result["errors"] == ["Row 2: A precise CSV location requires an existing primary copy"]
        assert (result["imported"], result["restored"], result["locations_created"]) == (0, 0, [])
        assert _dump(db) == before
    else:
        assert result["errors"] == []
        assert (result["imported"], result["restored"], result["locations_created"]) == (int(not deleted), int(deleted), [])
        assert db.execute("SELECT owned,publisher,location_id FROM items WHERE id=?", (item_id,)).fetchone()[:] == (1, "Changed", legacy)
    assert db.execute("SELECT legacy_location_id FROM location_nodes WHERE id=?", (node,)).fetchone()[0] is None


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unknown_or_ambiguous_nested_path_never_creates_a_foreign_tree(admin_client, db, ambiguous):
    from app.services import locations

    if ambiguous:
        locations.create_location(db, "A / B")
        root = locations.create_location(db, "A")
        locations.create_location(db, "B", parent_id=root)
    db.commit()
    before = _dump(db)
    result = _import(admin_client, _csv({"title": "Must not insert", "location": "A / B", "tags": "Must not attach"}))
    assert (result["imported"], result["error_count"], result["locations_created"]) == (0, 1, [])
    assert result["errors"] == (["Row 2: Location path is ambiguous"] if ambiguous else ['Row 2: location "A / B" not found'])
    assert _dump(db) == before


@pytest.mark.parametrize("deleted", [False, True])
def test_primary_move_failure_rolls_back_row_and_caches_before_same_key_retry(admin_client, db, monkeypatch, deleted):
    from app.services import item_copies, item_write, locations

    old = locations.create_location(db, "Original node")
    item_id = item_write.insert_item(db, title="Retry placement", isbn="9780441172719", publisher="Original")
    primary = db.execute("SELECT id FROM copies_live WHERE item_id=? AND is_primary=1", (item_id,)).fetchone()[0]
    item_copies.update_copy(db, primary, {"location_id": old})
    if deleted:
        item_write.trash_item(db, item_id)
    db.commit()
    update = item_copies.update_copy
    moved = []

    def fail_once(conn, copy_id, fields, **kwargs):
        previous = conn.execute("SELECT location_id FROM copies_live WHERE id=?", (copy_id,)).fetchone()[0]
        result = update(conn, copy_id, fields, **kwargs)
        if "location_id" in fields and fields["location_id"] != previous:
            moved.append(copy_id)
            if len(moved) == 1:
                raise ValueError("CSV primary move failed")
        return result

    monkeypatch.setattr(item_copies, "update_copy", fail_once)
    row = {"title": "Retry placement", "isbn": "9780441172719", "location": "Retry flat", "publisher": "Changed", "tags": "Retry tag"}
    result = _import(admin_client, _csv(row, row), mode="update")
    assert moved == [primary, primary]
    assert result["errors"] == ["Row 2: CSV primary move failed"]
    assert (result["imported"], result["restored"], result["skipped"], result["error_count"]) == (int(not deleted), int(deleted), 0, 1)
    assert result["locations_created"] == ["Retry flat"] and result["dropped"] == []
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert db.execute("SELECT id FROM item_copies").fetchone()[0] == primary
    assert db.execute("SELECT COUNT(*) FROM locations WHERE name='Retry flat'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM item_tags WHERE item_id=?", (item_id,)).fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_trashed_primary_exports_precise_path_but_requires_parent_editor(admin_client, viewer_user, db):
    from fastapi import HTTPException
    from app.services import csv_import, item_copies, item_write, locations

    root = locations.create_location(db, "Trash room")
    child = locations.create_location(db, "Precise shelf", parent_id=root)
    item_id = item_write.insert_item(db, title="Placed Trash", isbn=None)
    primary = db.execute("SELECT id FROM copies_live WHERE item_id=? AND is_primary=1", (item_id,)).fetchone()[0]
    item_copies.update_copy(db, primary, {"location_id": child})
    item_write.trash_item(db, item_id)
    db.commit()
    exported = next(csv.DictReader(io.StringIO(_export(admin_client))))
    assert exported["location"] == "Trash room / Precise shelf" and exported["deleted"] == "1"
    before = _dump(db)
    with pytest.raises(HTTPException) as refused:
        csv_import.export_locations(db, [{"id": item_id, "deleted_at": "trashed", "location_name": None}], user=viewer_user)
    assert refused.value.status_code == 403
    assert _dump(db) == before
