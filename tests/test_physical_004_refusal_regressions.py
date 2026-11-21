"""Phase4 physical wire/rollback oracles; execute only after all-writer freeze.

RAW item fixtures do not create copies. Every copy and library mapping below is
intentional, and every NODE/FLAT conversion is explicit. Logging is the sole
permitted refusal side effect, never a catalogue/identity/allocation change.
"""

import json
import re
import sqlite3
import time
from html import unescape
from html.parser import HTMLParser

import pytest

from app.services import item_copies, libraries
from tests.conftest import _insert_item, _insert_node_location, _legacy_location


class _Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.inputs = {}
        self.selected = {}
        self.select = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and "name" in attrs:
            self.inputs[attrs["name"]] = attrs.get("value", "")
        if tag == "select":
            self.select = attrs.get("name")
        if tag == "option" and "selected" in attrs:
            self.selected[self.select] = attrs["value"]

    def handle_endtag(self, tag):
        if tag == "select":
            self.select = None


def _state(db):
    state = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}")]
             for table, order in (("items", "id"), ("item_copies", "id"),
                                  ("library_items", "item_id"), ("digital_holdings", "id"),
                                  ("item_tags", "item_id, tag_id"), ("list_items", "list_id, item_id"))}
    state["allocation"] = [tuple(row) for row in db.execute(
        "SELECT name, seq FROM sqlite_sequence WHERE name IN ('items', 'item_copies', 'digital_holdings') ORDER BY name")]
    return state


def _refusal(response, message):
    assert response.status_code == 200
    assert response.text.count('id="item-copies"') == 1
    alerts = re.findall(r'<p role="alert"[^>]*>(.*?)</p>', response.text, re.S)
    assert len(alerts) == 1
    assert unescape(re.sub(r"<[^>]*>", "", alerts[0])).strip() == message
    assert json.loads(response.headers["HX-Trigger"])["showToast"] == {"message": message, "type": "error"}


def _copy(db, parent, node, number=1, *, barcode=None):
    return item_copies.insert_copy(db, {
        "item_id": parent, "copy_number": number, "location_id": node,
        "is_primary": int(number == 1), "copy_barcode": barcode,
        "missing_since": "2026-01-02", "position_order": number + 4,
        "condition": "Stored grade", "notes": "Stored notes",
    })


def _request(client, operation, parent, child, data=None):
    base = f"/api/items/{parent}/copies"
    if operation == "collapsed":
        return client.get(base)
    if operation == "add":
        return client.post(base, data=data or {})
    if operation == "edit":
        return client.get(f"{base}/{child}/edit")
    if operation == "remove":
        return client.request("DELETE", f"{base}/{child}", data=data or {})
    suffix = {"update": "", "found": "/found", "assign": "/assign-code"}[operation]
    return client.post(f"{base}/{child}{suffix}", data=data or {})


@pytest.mark.parametrize("operation", ["edit", "update", "remove", "found", "assign"])
@pytest.mark.parametrize("child_state", ["missing", "wrong_parent", "trashed", "hidden_parent"])
def test_physical004_authorized_parent_child_refusal_is_complete_generic_and_atomic(
        editor_client, db, operation, child_state):
    node = _insert_node_location(db, "Physical004 room")
    flat = _legacy_location(db, node)
    assert flat is not None and flat != node
    parent = _insert_item(db, title="Authorized physical004 parent", isbn=None, location_id=flat)
    target = _copy(db, parent, node, barcode="PHYSICAL004-CHILD")
    _copy(db, parent, node, 2)
    if child_state in {"wrong_parent", "hidden_parent"}:
        library_id = (libraries.create_library(db, "DO-NOT-DISCLOSE-LIBRARY")["id"]
                      if child_state == "hidden_parent" else libraries.DEFAULT_LIBRARY_ID)
        other = _insert_item(db, title="DO-NOT-DISCLOSE-CHILD-TITLE", isbn=None, _library_id=library_id)
        target = _copy(db, other, node, barcode="DO-NOT-DISCLOSE-CHILD-BARCODE")
    elif child_state == "trashed":
        assert item_copies.trash_copy(db, target)["remaining"] == 1
    else:
        target = 999999
    db.commit()
    before = _state(db)
    response = _request(editor_client, operation, parent, target, {"notes": "Unsaved input"})
    _refusal(response, "Copy not found")
    for hidden in ("DO-NOT-DISCLOSE-LIBRARY", "DO-NOT-DISCLOSE-CHILD-TITLE", "DO-NOT-DISCLOSE-CHILD-BARCODE"):
        assert hidden not in response.text and hidden not in response.headers["HX-Trigger"]
    assert _state(db) == before
    assert editor_client.get(f"/api/items/{parent}/copies").status_code == 200


@pytest.mark.parametrize("operation", ["collapsed", "add", "edit", "update", "remove", "found", "assign"])
@pytest.mark.parametrize("parent_state", ["missing", "hidden", "trashed"])
def test_physical004_unavailable_parent_stays_exact404_without_disclosure(
        editor_client, db, operation, parent_state):
    node = _insert_node_location(db, "Parent boundary room")
    library_id = (libraries.create_library(db, "PRIVATE-PARENT-LIBRARY")["id"]
                  if parent_state == "hidden" else libraries.DEFAULT_LIBRARY_ID)
    parent = _insert_item(db, title="PRIVATE-PARENT-TITLE", isbn=None, _library_id=library_id)
    child = _copy(db, parent, node, barcode="PRIVATE-PARENT-BARCODE")
    if parent_state == "trashed":
        from app.services.item_write import trash_item

        assert trash_item(db, parent)
    if parent_state == "missing":
        parent = 999999
    db.commit()
    before = _state(db)
    response = _request(editor_client, operation, parent, child)
    assert response.status_code == 404
    assert response.json() == {"detail": "Item not found"}
    assert json.loads(response.headers["HX-Trigger"])["showToast"] == {"message": "Item not found", "type": "error"}
    assert "PRIVATE-PARENT" not in response.text
    assert _state(db) == before


@pytest.mark.parametrize("operation", ["add", "edit", "update", "remove", "found", "assign"])
def test_physical004_viewer_cannot_turn_refusals_into_write_authority(viewer_client, db, operation):
    node = _insert_node_location(db, "Viewer boundary room")
    parent = _insert_item(db, isbn=None)
    child = _copy(db, parent, node)
    db.commit()
    before = _state(db)
    assert _request(viewer_client, operation, parent, child).status_code == 403
    assert _state(db) == before


_UNSAVED = {
    "condition": "Typed grade", "notes": "<typed notes>", "acquired_date": "2026-10-11",
    "acquisition_source": "Typed shop", "acquisition_price": "12.75",
    "provenance": "Typed provenance", "copy_barcode": "TYPED-PHYSICAL004",
}


def _unsaved(response, submitted):
    inputs = _Inputs(response.text)
    for name in _UNSAVED:
        if name == "notes":
            assert "&lt;typed notes&gt;" in response.text
        else:
            assert inputs.inputs[name] == submitted[name]
    if submitted["location_id"].isdigit() and submitted["location_id"] != "999999":
        assert inputs.selected["location_id"] == submitted["location_id"]


@pytest.mark.parametrize("operation", ["add", "update"])
@pytest.mark.parametrize("bad,message", [
    ({"location_id": "999999"}, "Location not found"),
    ({"acquisition_price": "not-money"}, "Acquisition price must be a number"),
    ({"acquisition_price": "-1"}, "Acquisition price cannot be negative"),
    ({"acquisition_price": "inf"}, "Acquisition price must be a finite number"),
    ({"acquired_date": "not-a-date"}, "Acquisition date must be a valid date"),
    ({"condition": "x" * 61}, "Condition must be 60 characters or fewer"),
])
def test_physical004_validation_refusal_keeps_unsaved_fields_and_lands_a_fast_log(
        editor_client, db, operation, bad, message):
    node = _insert_node_location(db, "Unsaved room")
    parent = _insert_item(db, isbn=None)
    child = _copy(db, parent, node)
    db.commit()
    before = _state(db)
    submitted = {**_UNSAVED, "location_id": str(node), **bad}
    start = time.monotonic()
    response = _request(editor_client, operation, parent, child, submitted)
    elapsed = time.monotonic() - start
    _refusal(response, message)
    _unsaved(response, submitted)
    assert _state(db) == before
    log = db.execute("SELECT message FROM log_entries WHERE message LIKE ?",
                     (f"Copy {'add' if operation == 'add' else 'update'} refused%",)).fetchall()
    assert len(log) == 1
    assert elapsed < 2.0, "the logger must never wait for its own SQLite write lock"


@pytest.mark.parametrize("operation", ["add", "update"])
def test_physical004_integrity_refusal_rolls_back_landed_writes_before_log_and_render(
        editor_client, db, monkeypatch, operation):
    from app.routers import item_copies as copy_router

    node = _insert_node_location(db, "Integrity room")
    flat = _legacy_location(db, node)
    assert flat is not None and flat != node
    # Match the primary NODE's projection BEFORE patching the writer. A NULL
    # seam would re-enter the patched update during the native service call,
    # raising inside its savepoint and masking a missing ROUTE rollback.
    parent = _insert_item(db, isbn=None, location_id=flat)
    child = _copy(db, parent, node)
    db.commit()
    before = _state(db)
    name = f"{operation}_copy"
    real = getattr(copy_router.item_copies, name)
    writer_calls = []

    def write_then_fail(*args, **kwargs):
        writer_calls.append({})
        assert len(writer_calls) == 1, "the injected fault must follow the outer writer, not a recursive projection"
        result = real(*args, **kwargs)
        writer_db = args[0]
        assert writer_db.in_transaction
        if operation == "update":
            assert result is True, "the native outer update and its service savepoint must have returned"
            changed_copy = child
        else:
            assert isinstance(result, int) and not isinstance(result, bool) and result != child
            changed_copy = result
        landed = dict(writer_db.execute("SELECT * FROM item_copies WHERE id = ?", (changed_copy,)).fetchone())
        for field, value in _UNSAVED.items():
            assert landed[field] == (float(value) if field == "acquisition_price" else value)
        assert landed["location_id"] == node
        landed_state = _state(writer_db)
        assert landed_state != before, "a returned writer must have changed real rows before the injected exception"
        assert len(landed_state["item_copies"]) == len(before["item_copies"]) + (operation == "add")
        writer_calls[0].update(returned=result, landed_state=landed_state, changed_copy=changed_copy)
        # This exception is OUTSIDE the native writer/savepoint. Removing only
        # the route's IntegrityError rollback must now leave a changed snapshot.
        raise sqlite3.IntegrityError("PRIVATE INTERNAL CONSTRAINT DETAIL")

    monkeypatch.setattr(copy_router.item_copies, name, write_then_fail)
    submitted = {**_UNSAVED, "location_id": str(node)}
    start = time.monotonic()
    response = _request(editor_client, operation, parent, child, submitted)
    elapsed = time.monotonic() - start
    _refusal(response, "That copy barcode is already in use")
    _unsaved(response, submitted)
    assert len(writer_calls) == 1 and "returned" in writer_calls[0]
    assert writer_calls[0]["landed_state"] != before
    assert "PRIVATE INTERNAL CONSTRAINT DETAIL" not in response.text
    assert _state(db) == before
    assert db.execute("SELECT COUNT(*) FROM log_entries WHERE message LIKE ?",
                      (f"Copy {operation} refused%",)).fetchone()[0] == 1
    assert elapsed < 2.0


@pytest.mark.parametrize("operation", ["add", "update", "remove", "found", "assign"])
def test_physical004_success_has_real_effects_and_never_an_error_toast(editor_client, db, operation):
    node = _insert_node_location(db, "Successful room")
    parent = _insert_item(db, isbn=None)
    child = _copy(db, parent, node)
    survivor = _copy(db, parent, node, 2)
    db.commit()
    response = _request(editor_client, operation, parent, child, {"notes": "Saved notes"})
    assert response.status_code == 200
    assert response.text.count('id="item-copies"') == 1
    assert 'role="alert"' not in response.text and "HX-Trigger" not in response.headers
    row = db.execute("SELECT * FROM item_copies WHERE id = ?", (child,)).fetchone()
    if operation == "add":
        assert [copy["copy_number"] for copy in item_copies.copies_for_item(db, parent)] == [1, 2, 3]
    elif operation == "update":
        assert row["notes"] == "Saved notes"
    elif operation == "remove":
        assert row["deleted_at"] is not None
        assert db.execute("SELECT is_primary FROM item_copies WHERE id = ?", (survivor,)).fetchone()[0] == 1
    elif operation == "found":
        assert row["missing_since"] is None
    else:
        assert row["copy_barcode"] == "SHF-000001" and row["notes"] == "Stored notes"


def test_physical004_http_last_owned_refusal_does_not_accept_replay_or_timestamp_flags(editor_client, db):
    node = _insert_node_location(db, "Last owned room")
    parent = _insert_item(db, isbn=None, location_id=_legacy_location(db, node))
    child = _copy(db, parent, node, barcode="LAST-OWNED-PHYSICAL004")
    db.commit()
    before = _state(db)
    response = _request(editor_client, "remove", parent, child, {"replay": "true", "at": "2026-01-02 03:04:05"})
    _refusal(response, "An owned physical item must keep at least one copy")
    assert _state(db) == before


@pytest.mark.parametrize("operation", ["add", "update"])
@pytest.mark.parametrize("location,message", [
    (str(2**63), "Location not found"),
    (str(-(2**63) - 1), "Invalid location"),
    (str(2**100), "Location not found"),
    (str(-(2**100)), "Invalid location"),
    (str(2**63 - 1), "Location not found"),  # In-range, but no such NODE.
    (str(-(2**63)), "Invalid location"),  # In-range, but NODE IDs are positive.
])
def test_physical004_extreme_location_is_a_typed_complete_refusal_without_effects(
        editor_client, db, operation, location, message):
    node = _insert_node_location(db, "Extreme location room")
    parent = _insert_item(db, isbn=None, location_id=_legacy_location(db, node))
    child = _copy(db, parent, node, barcode="UNCHANGED-EXTREME-LOCATION")
    db.commit()
    before = _state(db)
    submitted = {**_UNSAVED, "location_id": location}
    response = _request(editor_client, operation, parent, child, submitted)
    _refusal(response, message)
    # An unavailable location cannot be an offered <option>; every other typed
    # field must survive the same complete refusal render, including on Add.
    inputs = _Inputs(response.text)
    for field in _UNSAVED:
        if field == "notes":
            assert "&lt;typed notes&gt;" in response.text
        else:
            assert inputs.inputs[field] == submitted[field]
    assert _state(db) == before


@pytest.mark.parametrize("operation", ["edit", "update", "remove", "found", "assign"])
@pytest.mark.parametrize("child_id", [-(2**63) - 1, 2**63, -(2**100), 2**100, -(2**63), 2**63 - 1])
def test_physical004_extreme_missing_child_is_generic200_without_binding_overflow(
        editor_client, db, operation, child_id):
    node = _insert_node_location(db, "Extreme child room")
    parent = _insert_item(db, isbn=None, location_id=_legacy_location(db, node))
    _copy(db, parent, node, barcode="UNCHANGED-EXTREME-CHILD")
    _copy(db, parent, node, 2)
    db.commit()
    before = _state(db)
    response = _request(editor_client, operation, parent, child_id, _UNSAVED)
    _refusal(response, "Copy not found")
    assert _state(db) == before  # Includes barcode, copy number and allocation.


@pytest.mark.parametrize("operation", ["edit", "update", "remove", "found", "assign"])
@pytest.mark.parametrize("parent_state,status", [("missing", 404), ("hidden", 404), ("readonly", 403)])
def test_physical004_oversized_child_does_not_preempt_parent_authority(
        editor_client, editor_user, db, operation, parent_state, status):
    node = _insert_node_location(db, "Extreme child authority room")
    library_id = libraries.create_library(db, "PRIVATE-EXTREME-CHILD-LIBRARY")["id"]
    if parent_state == "readonly":
        libraries.set_membership(db, library_id, editor_user["id"], "viewer")
    parent = _insert_item(db, title="PRIVATE-EXTREME-CHILD-TITLE", isbn=None, _library_id=library_id)
    _copy(db, parent, node, barcode="PRIVATE-EXTREME-CHILD-CODE")
    if parent_state == "missing":
        parent = 999999
    db.commit()
    before = _state(db)
    response = _request(editor_client, operation, parent, 2**63, _UNSAVED)
    assert response.status_code == status
    assert response.json() == {"detail": "Forbidden" if status == 403 else "Item not found"}
    assert "PRIVATE-EXTREME-CHILD" not in response.text
    assert _state(db) == before


@pytest.mark.parametrize("operation", ["edit", "update", "remove", "found", "assign"])
@pytest.mark.parametrize("child_id", [-(2**63), 2**63 - 1])
def test_physical004_real_signed64_boundary_child_remains_usable(editor_client, db, operation, child_id):
    node = _insert_node_location(db, "Boundary child room")
    parent = _insert_item(db, isbn=None, location_id=_legacy_location(db, node))
    # The actual SQLite boundary IDs are valid row identities. Seed the real
    # survivor FIRST so the max-ID fixture need not allocate another copy ID.
    survivor = _copy(db, parent, node, 2)
    db.execute(
        "INSERT INTO item_copies (id, item_id, copy_number, location_id, is_primary, "
        "missing_since, notes, position_order) VALUES (?, ?, 1, ?, 1, '2026-01-02', 'Boundary stored notes', 5)",
        (child_id, parent, node),
    )
    db.commit()
    before = _state(db)
    response = _request(editor_client, operation, parent, child_id, {"notes": "Boundary saved notes"})
    assert response.status_code == 200
    assert response.text.count('id="item-copies"') == 1
    assert 'role="alert"' not in response.text and "HX-Trigger" not in response.headers
    row = db.execute("SELECT * FROM item_copies WHERE id = ?", (child_id,)).fetchone()
    assert row["id"] == child_id and row["copy_number"] == 1
    if operation == "edit":
        assert f'hx-post="/api/items/{parent}/copies/{child_id}"' in response.text
        assert _state(db) == before
    elif operation == "update":
        assert row["notes"] == "Boundary saved notes"
    elif operation == "remove":
        assert row["deleted_at"] is not None
        assert db.execute("SELECT is_primary FROM item_copies WHERE id = ?", (survivor,)).fetchone()[0] == 1
    elif operation == "found":
        assert row["missing_since"] is None
    else:
        assert row["copy_barcode"] == "SHF-000001" and row["notes"] == "Boundary stored notes"
    assert db.execute("SELECT COUNT(*) FROM item_copies WHERE item_id = ?", (parent,)).fetchone()[0] == 2


@pytest.mark.parametrize("operation", ["add", "update"])
def test_physical004_real_max_signed64_node_is_a_valid_copy_location(editor_client, db, operation):
    root = _insert_node_location(db, "Boundary NODE room")
    flat = _legacy_location(db, root)
    assert flat is not None and flat != root
    boundary_node = 2**63 - 1
    db.execute("INSERT INTO location_nodes (id, parent_id, name) VALUES (?, ?, 'Max SQLite shelf')",
               (boundary_node, root))
    parent = _insert_item(db, isbn=None, location_id=flat)
    child = _copy(db, parent, root)
    db.commit()
    response = _request(editor_client, operation, parent, child,
                        {"location_id": str(boundary_node), "notes": "Boundary NODE placement"})
    assert response.status_code == 200
    assert response.text.count('id="item-copies"') == 1
    assert 'role="alert"' not in response.text and "HX-Trigger" not in response.headers
    copies = item_copies.copies_for_item(db, parent)
    if operation == "add":
        assert [(copy["copy_number"], copy["location_id"]) for copy in copies] == [(1, root), (2, boundary_node)]
    else:
        assert [(copy["id"], copy["location_id"]) for copy in copies] == [(child, boundary_node)]
        assert copies[0]["position_order"] is None
    assert db.execute("SELECT location_id FROM items WHERE id = ?", (parent,)).fetchone()[0] == flat
