"""Post-freeze real-app oracles for the fork's two physical FK domains.

Do not execute during concurrent authoring. Unlike the isolated companion,
these exercise the central schema, real library ACLs, writer and templates.
"""

from html.parser import HTMLParser

import pytest
from fastapi import HTTPException

from app.services import audits, holdings, item_copies, libraries
from app.services import catalogue_security as security
from app.services.item_write import insert_item, trash_item, update_item_fields


def _domains(db):
    maximum = max(
        db.execute("SELECT COALESCE(MAX(id), 0) FROM locations").fetchone()[0],
        db.execute("SELECT COALESCE(MAX(id), 0) FROM location_nodes").fetchone()[0],
    ) + 1000
    legacy_a, legacy_b = maximum + 1, maximum + 2
    node_a, shelf_a, shelf_b = maximum + 101, maximum + 102, maximum + 103
    db.executemany("INSERT INTO locations(id, name) VALUES (?, ?)", [
        (legacy_a, f"Physical domain A {maximum}"), (legacy_b, f"Physical domain B {maximum}")])
    # Deliberately SAME integer as legacy A, but explicitly maps to legacy B.
    db.executemany("INSERT INTO location_nodes(id, name, legacy_location_id) VALUES (?, ?, ?)", [
        (node_a, f"Physical domain A {maximum}", legacy_a),
        (legacy_a, f"Physical domain B {maximum}", legacy_b)])
    db.executemany("INSERT INTO location_nodes(id, parent_id, name) VALUES (?, ?, ?)", [
        (shelf_a, node_a, "Shelf A"), (shelf_b, legacy_a, "Shelf B")])
    return legacy_a, legacy_b, node_a, shelf_a, shelf_b


def _primary(db, item_id):
    return db.execute(
        "SELECT * FROM item_copies WHERE item_id = ? AND is_primary = 1", (item_id,)
    ).fetchone()


def test_physical_domain_insert_creates_unlocated_owned_copy_and_no_digital_copy(db):
    item = insert_item(db, title="Unlocated physical domain", media_type="book", owned=1)
    primary = _primary(db, item)
    assert primary is not None
    assert primary["location_id"] is None
    digital = insert_item(db, title="Digital domain", media_type="ebook", owned=1)
    assert item_copies.copies_for_item(db, digital) == []
    item_copies.delete_copies_for_item(db, item)
    holdings.ensure_foundation(db)
    update_item_fields(db, item, {"title": "Intentionally zero copies"})
    assert item_copies.copies_for_item(db, item) == []


def test_physical_domain_http_precise_move_and_legacy_updates_never_flatten(db, editor_client):
    legacy_a, legacy_b, node_a, _shelf_a, shelf_b = _domains(db)
    item = insert_item(db, title="Domain move", media_type="book", owned=1, location_id=legacy_a)
    primary = _primary(db, item)
    assert primary is not None and primary["location_id"] == node_a
    copy_id = primary["id"]
    db.commit()
    response = editor_client.post(f"/api/items/{item}/copies/{copy_id}", data={
        "location_id": shelf_b, "condition": "good", "notes": "Fork notes",
        "acquisition_source": "Bookshop", "provenance": "Signed history",
        "acquired_date": "2026-09-03", "acquisition_price": "12.50", "copy_barcode": "DOMAIN-SIGNED"})
    assert response.status_code == 200
    row = db.execute("SELECT * FROM item_copies WHERE id = ?", (copy_id,)).fetchone()
    assert row["location_id"] == shelf_b
    assert row["provenance"] == "Signed history" and row["notes"] == "Fork notes"
    assert row["acquisition_price"] == 12.5
    assert db.execute("SELECT location_id FROM items WHERE id = ?", (item,)).fetchone()[0] == legacy_b
    update_item_fields(db, item, {"location_id": legacy_a, "title": "Precise shelf wins"})
    assert _primary(db, item)["id"] == copy_id
    assert _primary(db, item)["location_id"] == shelf_b


def test_physical_domain_primary_trash_restore_and_last_copy_refusal(db, editor_client):
    legacy_a, legacy_b, _node_a, _shelf_a, shelf_b = _domains(db)
    item = insert_item(db, title="Domain Trash", media_type="book", owned=1, location_id=legacy_a)
    first = _primary(db, item)["id"]
    item_copies.update_copy(db, first, {"copy_barcode": "DOMAIN-FIRST", "provenance": "Original"})
    second = item_copies.add_copy(db, item, {"location_id": shelf_b, "position_order": 7})
    item_copies.mark_missing(db, first)
    db.commit()
    assert editor_client.delete(f"/api/items/{item}/copies/{first}").status_code == 200
    assert _primary(db, item)["id"] == second
    assert _primary(db, item)["position_order"] == 7
    assert db.execute("SELECT location_id FROM items WHERE id = ?", (item,)).fetchone()[0] == legacy_b
    refusal = editor_client.delete(f"/api/items/{item}/copies/{second}")
    assert refusal.status_code == 200
    assert "must keep at least one copy" in refusal.text
    restored = item_copies.restore_copy(db, first)
    assert restored["copy_id"] == first and not restored["is_primary"]
    row = db.execute("SELECT * FROM item_copies WHERE id = ?", (first,)).fetchone()
    assert row["copy_barcode"] == "DOMAIN-FIRST" and row["provenance"] == "Original"
    assert row["missing_since"] is not None
    db.commit()
    assert editor_client.post(f"/api/items/{item}/copies/{first}/found").status_code == 200
    assert db.execute("SELECT missing_since FROM item_copies WHERE id = ?", (first,)).fetchone()[0] is None


def test_physical_domain_audit_fallback_and_copy_sets_use_nodes_only(db):
    legacy_a, _legacy_b, node_a, shelf_a, _shelf_b = _domains(db)
    item = insert_item(db, title="Node expected set", media_type="book", owned=1, location_id=legacy_a)
    first = _primary(db, item)["id"]
    second = item_copies.add_copy(db, item, {"location_id": shelf_a})
    assert {r["copy_id"] for r in audits.expected_rows(db, node_a)} == {first}
    assert {r["copy_id"] for r in audits.expected_rows(db, shelf_a)} == {second}
    assert audits.expected_rows(db, legacy_a) == []  # equal integer belongs to B's node
    item_copies.delete_copies_for_item(db, item)
    fallback = audits.expected_rows(db, node_a)
    assert [(r["item_id"], r["copy_id"], r["location_id"]) for r in fallback] == [(item, None, node_a)]
    assert audits.expected_rows(db, legacy_a) == []


def test_physical_domain_hidden_trashed_barcode_collision_no_disclosure(db, editor_client):
    visible = insert_item(db, title="Visible domain", media_type="book", owned=1)
    private_library = libraries.create_library(db, "Physical hidden library")["id"]
    hidden = insert_item(db, title="HIDDEN PHYSICAL BLOCKER", media_type="book", owned=1,
                         library_id=private_library)
    hidden_copy = _primary(db, hidden)["id"]
    item_copies.update_copy(db, hidden_copy, {"copy_barcode": "DOMAIN-PRIVATE"})
    trash_item(db, hidden)
    db.commit()
    response = editor_client.post(f"/api/items/{visible}/copies", data={"copy_barcode": "DOMAIN-PRIVATE"})
    assert response.status_code == 200
    assert "already in use" in response.text
    assert "HIDDEN PHYSICAL BLOCKER" not in response.text
    assert "Physical hidden library" not in response.text
    assert len(item_copies.copies_for_item(db, visible)) == 1
    assert db.execute("SELECT copy_barcode FROM item_copies WHERE id = ?", (hidden_copy,)).fetchone()[0] == "DOMAIN-PRIVATE"


def test_physical_domain_hidden_restore_and_readonly_library_write_denied(db, editor_client, editor_user):
    private_library = libraries.create_library(db, "Physical restore boundary")["id"]
    hidden = insert_item(db, title="Hidden restorable", media_type="book", owned=1, library_id=private_library)
    first = _primary(db, hidden)["id"]
    second = item_copies.add_copy(db, hidden)
    item_copies.trash_copy(db, first)
    db.commit()
    token = security.set_actor(dict(editor_user))
    try:
        security.install_catalogue_views(db)
        with pytest.raises(HTTPException) as denied:
            item_copies.restore_copy(db, first)
        assert denied.value.status_code == 404
    finally:
        security.reset_actor(token)
        security.install_catalogue_views(db)
    libraries.set_membership(db, private_library, editor_user["id"], "viewer")
    db.commit()
    response = editor_client.post(f"/api/items/{hidden}/copies/{second}", data={"condition": "good"})
    assert response.status_code == 403
    assert db.execute("SELECT condition FROM item_copies WHERE id = ?", (second,)).fetchone()[0] is None


class _Elements(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def matching(self, **attrs):
        return [values for _tag, values in self.elements
                if all(key.replace("_", "-") in values and values[key.replace("_", "-")] == value
                       for key, value in attrs.items())]


def _sign_in(client, user):
    from app.auth import create_token

    client.cookies.set("access_token", create_token(
        user["id"], user["username"], user["role"], user["display_name"]))


def _collector_on(db):
    from app.features import set_feature_enabled

    set_feature_enabled(db, "collector", True)


def _assert_audit_http_counts(client, audit_id, *, scans, **counts):
    page = client.get(f"/audits/{audit_id}")
    summary = client.get(f"/api/audits/summary?audit_id={audit_id}")
    listing = client.get("/audits")
    assert page.status_code == summary.status_code == listing.status_code == 200
    expected = {section: counts.get(section, 0) for section in audits.SECTIONS}
    assert page.context["report"].scans == scans
    assert page.context["report"].counts == expected
    assert summary.context["counts"]["scans"] == scans
    assert f'<span data-audit-summary="scans">{scans}</span>' in summary.text
    assert next(row for row in listing.context["sessions"] if row["id"] == audit_id)["scans"] == scans
    for section, count in expected.items():
        assert _Elements(page.text).matching(data_audit_section=section, data_audit_count=str(count))
    return page


def test_physical_review_private_audit_provenance_survives_native_item_purge(
        db, client, editor_user, viewer_user, admin_user):
    _collector_on(db)
    _legacy_a, _legacy_b, node_a, _shelf_a, _shelf_b = _domains(db)
    private_library = libraries.create_library(db, "Private enduring audit provenance")["id"]
    libraries.set_membership(db, private_library, viewer_user["id"], "editor")
    private = insert_item(db, title="PRIVATE AUDIT ITEM", media_type="book", owned=1,
                          library_id=private_library)
    audit_id = audits.create_session(db, scope="library", location_id=None)
    actor_token = security.set_actor(dict(viewer_user))
    try:
        audits.add_entry(db, audit_id, item_id=private, copy_id=None, location_id=node_a,
                         raw_code="PRIVATE-RAW-BEFORE-PURGE", result="elsewhere")
    finally:
        security.reset_actor(actor_token)
    trash_item(db, private)
    actor_token = security.set_actor(dict(viewer_user))
    try:
        audits.add_entry(db, audit_id, item_id=private, copy_id=None, location_id=node_a,
                         raw_code="PRIVATE-RAW-IN-TRASH", result="in_trash")
    finally:
        security.reset_actor(actor_token)
    snapshots = db.execute(
        "SELECT security_library_id, security_user_id FROM audit_entries WHERE session_id=? ORDER BY id",
        (audit_id,)).fetchall()
    assert [tuple(row) for row in snapshots] == [(private_library, viewer_user["id"])] * 2
    assert not {"security_library_id", "security_user_id"} & {
        row["from"] for row in db.execute("PRAGMA foreign_key_list(audit_entries)")}
    db.commit()

    def assert_views():
        _sign_in(client, editor_user)  # An editor of main, not this private library.
        hidden = _assert_audit_http_counts(client, audit_id, scans=0)
        assert "PRIVATE-RAW-BEFORE-PURGE" not in hidden.text
        assert "PRIVATE-RAW-IN-TRASH" not in hidden.text
        _sign_in(client, viewer_user)  # Global viewer, editor of the snapshotted library.
        allowed = _assert_audit_http_counts(client, audit_id, scans=2, elsewhere=1, unexpected=1)
        assert "PRIVATE-RAW-BEFORE-PURGE" in allowed.text
        assert "PRIVATE-RAW-IN-TRASH" in allowed.text

    assert_views()
    _sign_in(client, admin_user)
    purged = client.delete(f"/api/trash/items/{private}")
    assert purged.status_code == 200
    assert db.execute("SELECT 1 FROM items WHERE id=?", (private,)).fetchone() is None
    assert [tuple(row) for row in db.execute(
        "SELECT item_id,security_library_id,security_user_id FROM audit_entries WHERE session_id=? ORDER BY id",
        (audit_id,))] == [(None, private_library, viewer_user["id"])] * 2
    assert_views()


def test_physical_review_unknown_legacy_orphan_and_unmatched_scans_fail_closed(
        db, client, editor_user, viewer_user, admin_user):
    _collector_on(db)
    libraries.set_membership(db, libraries.DEFAULT_LIBRARY_ID, viewer_user["id"], "editor")
    audit_id = audits.create_session(db, scope="library", location_id=None)
    db.execute(
        "INSERT INTO audit_entries(session_id,item_id,raw_code,result,copy_specific,security_library_id,security_user_id) "
        "VALUES(?,NULL,'UNKNOWN-LEGACY-PRIVATE-RAW','confirmed',0,NULL,NULL)", (audit_id,))
    actor_token = security.set_actor(dict(viewer_user))
    try:
        unmatched = audits.add_entry(db, audit_id, item_id=None, copy_id=None, location_id=None,
                                    raw_code="RECORDED-ACTOR-ONLY-RAW", result="not_owned")
    finally:
        security.reset_actor(actor_token)
    assert tuple(db.execute(
        "SELECT security_library_id,security_user_id FROM audit_entries WHERE id=?", (unmatched,)).fetchone()
    ) == (None, viewer_user["id"])
    db.commit()
    _sign_in(client, editor_user)
    denied = _assert_audit_http_counts(client, audit_id, scans=0)
    assert "UNKNOWN-LEGACY-PRIVATE-RAW" not in denied.text
    assert "RECORDED-ACTOR-ONLY-RAW" not in denied.text
    _sign_in(client, viewer_user)
    own = _assert_audit_http_counts(client, audit_id, scans=1, unexpected=1)
    assert "RECORDED-ACTOR-ONLY-RAW" in own.text
    assert "UNKNOWN-LEGACY-PRIVATE-RAW" not in own.text
    _sign_in(client, admin_user)
    admin = _assert_audit_http_counts(client, audit_id, scans=2, found=1, unexpected=1)
    assert "UNKNOWN-LEGACY-PRIVATE-RAW" in admin.text
    assert "RECORDED-ACTOR-ONLY-RAW" in admin.text


def test_physical_review_repeated_stale_assign_keeps_exactly_one_usable_panel_root(db, editor_client):
    _collector_on(db)
    legacy_a, _legacy_b, node_a, _shelf_a, shelf_b = _domains(db)
    item = insert_item(db, title="Repeated assign panel", media_type="book", owned=1, location_id=legacy_a)
    copy_id = _primary(db, item)["id"]
    db.commit()
    form = {"location_id": str(shelf_b), "condition": "good", "notes": "Unsaved copy notes",
            "provenance": "Unsaved copy provenance", "acquisition_source": "Unsaved shop", "copy_barcode": ""}
    url = f"/api/items/{item}/copies/{copy_id}/assign-code"
    code = None
    for attempt in range(3):
        response = editor_client.post(url, data=form)
        assert response.status_code == 200
        dom = _Elements(response.text)
        assert len(dom.matching(id="item-copies")) == 1
        assert dom.matching(data_testid="save-copy") and dom.matching(data_testid="cancel-copy")
        assert dom.matching(name="provenance")[0]["value"] == "Unsaved copy provenance"
        assert dom.matching(name="acquisition_source")[0]["value"] == "Unsaved shop"
        assert "Unsaved copy notes" in response.text
        current = db.execute("SELECT * FROM item_copies WHERE id=?", (copy_id,)).fetchone()
        if attempt == 0:
            code = current["copy_barcode"]
            assert code
        else:
            assert "This copy already has a code" in response.text
        assert current["copy_barcode"] == code
        assert dom.matching(name="copy_barcode")[0]["value"] == code
        assert current["location_id"] == node_a
        assert current["notes"] is None and current["provenance"] is None
    second = item_copies.add_copy(db, item)
    item_copies.trash_copy(db, copy_id)
    db.commit()
    missing = editor_client.post(url, data=form)
    dom = _Elements(missing.text)
    assert missing.status_code == 200
    assert len(dom.matching(id="item-copies")) == 1
    assert "Copy not found" in missing.text
    assert dom.matching(data_testid="add-copy")
    assert dom.matching(data_copy_id=str(second))
    assert db.execute("SELECT copy_barcode FROM item_copies WHERE id=?", (copy_id,)).fetchone()[0] == code


@pytest.mark.parametrize("global_role,library_role,expected", [
    ("viewer", "editor", True), ("editor", "viewer", False),
])
def test_physical_review_arrange_ui_uses_library_authority_not_global_role(
        db, client, editor_user, global_role, library_role, expected):
    legacy_a, _legacy_b, node_a, _shelf_a, _shelf_b = _domains(db)
    db.execute("UPDATE users SET role=? WHERE id=?", (global_role, editor_user["id"]))
    libraries.set_membership(db, libraries.DEFAULT_LIBRARY_ID, editor_user["id"], library_role)
    item = insert_item(db, title="Arrange authority", media_type="book", owned=1, location_id=legacy_a)
    copy_id = _primary(db, item)["id"]
    db.commit()
    _sign_in(client, {**editor_user, "role": global_role})
    page = client.get(f"/locations/{node_a}/arrange")
    assert page.status_code == 200
    assert page.context["can_edit"] is expected
    dom = _Elements(page.text)
    assert dom.matching(data_can_edit="1" if expected else "0")
    assert bool(dom.matching(data_save_order=None)) is expected
    assert dom.matching(data_copy_id=str(copy_id))[0]["draggable"] == ("true" if expected else "false")
    response = client.post(f"/api/locations/{node_a}/order", json={"copy_ids": [copy_id]})
    assert response.status_code == (200 if expected else 403)
    assert db.execute("SELECT position_order FROM item_copies WHERE id=?", (copy_id,)).fetchone()[0] == (1 if expected else None)


def test_physical_review_mixed_readonly_shelf_disables_arrange_and_api_remains_atomic(db, editor_client, editor_user):
    legacy_a, _legacy_b, node_a, _shelf_a, _shelf_b = _domains(db)
    readonly_library = libraries.create_library(db, "Read-only arrange boundary")["id"]
    libraries.set_membership(db, readonly_library, editor_user["id"], "viewer")
    first = insert_item(db, title="Editable shelf item", media_type="book", owned=1, location_id=legacy_a)
    second = insert_item(db, title="Readable shelf item", media_type="book", owned=1,
                         location_id=legacy_a, library_id=readonly_library)
    first_copy, second_copy = _primary(db, first)["id"], _primary(db, second)["id"]
    item_copies.update_copy(db, first_copy, {"position_order": 7})
    item_copies.update_copy(db, second_copy, {"position_order": 11})
    db.commit()
    page = editor_client.get(f"/locations/{node_a}/arrange")
    assert page.status_code == 200 and page.context["can_edit"] is False
    dom = _Elements(page.text)
    assert dom.matching(data_can_edit="0")
    assert not dom.matching(data_save_order=None)
    assert not dom.matching(draggable="true")
    response = editor_client.post(f"/api/locations/{node_a}/order", json={"copy_ids": [second_copy, first_copy]})
    assert response.status_code == 403
    assert [row[0] for row in db.execute(
        "SELECT position_order FROM item_copies WHERE id IN (?,?) ORDER BY id", (first_copy, second_copy))] == [7, 11]


@pytest.mark.parametrize("revoked", [False, True])
def test_physical_review_missing_selection_refuses_all_targets_after_authority_loss(
        db, editor_client, editor_user, revoked):
    _collector_on(db)
    legacy_a, _legacy_b, node_a, _shelf_a, _shelf_b = _domains(db)
    other_library = libraries.create_library(db, "Missing selection private boundary")["id"]
    libraries.set_membership(db, other_library, editor_user["id"], "editor")
    editable = insert_item(db, title="Editable missing", media_type="book", owned=1, location_id=legacy_a)
    other = insert_item(db, title="PRIVATE MISSING TARGET", media_type="book", owned=1,
                        location_id=legacy_a, library_id=other_library)
    first_copy, other_copy = _primary(db, editable)["id"], _primary(db, other)["id"]
    audit_id = audits.create_session(db, scope="library", location_id=None)
    db.commit()
    prior = editor_client.get(f"/audits/{audit_id}")
    assert prior.status_code == 200
    assert prior.context["report"].missing_copy_ids() == {first_copy, other_copy}
    if revoked:
        libraries.remove_membership(db, other_library, editor_user["id"])
    else:
        libraries.set_membership(db, other_library, editor_user["id"], "viewer")
    db.commit()
    refusal = editor_client.post(f"/api/audits/{audit_id}/missing",
                                 data={"copy_id": [first_copy, other_copy, 999999]})
    assert refusal.status_code == 200
    assert refusal.context["error"] == "Those copies cannot be updated"
    if revoked:
        assert "PRIVATE MISSING TARGET" not in refusal.text
        assert "Missing selection private boundary" not in refusal.text
    assert [row[0] for row in db.execute(
        "SELECT missing_since FROM item_copies WHERE id IN (?,?) ORDER BY id", (first_copy, other_copy))] == [None, None]
    found = insert_item(db, title="Nonmissing native no-op", media_type="book", owned=1, location_id=legacy_a)
    found_copy = _primary(db, found)["id"]
    audits.add_entry(db, audit_id, item_id=found, copy_id=found_copy, location_id=node_a,
                     raw_code="FOUND-NONMISSING", result="confirmed")
    db.commit()
    permitted = editor_client.post(f"/api/audits/{audit_id}/missing",
                                   data={"copy_id": [first_copy, found_copy, 999999]})
    assert permitted.status_code == 200 and not permitted.context["error"]
    assert db.execute("SELECT missing_since FROM item_copies WHERE id=?", (first_copy,)).fetchone()[0] is not None
    assert db.execute("SELECT missing_since FROM item_copies WHERE id=?", (found_copy,)).fetchone()[0] is None
    assert db.execute("SELECT missing_since FROM item_copies WHERE id=?", (other_copy,)).fetchone()[0] is None


def _alias_catalogue_state(db):
    return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY {key}")]
            for table, key in (("items", "id"), ("item_copies", "id"), ("location_nodes", "id"),
                               ("locations", "id"), ("library_items", "item_id"))}


def test_physical_compat_admin_alias_delete_cleans_no_action_refs_without_moving_copies(
        db, client, editor_user, admin_user):
    legacy_a, legacy_b, node_a, shelf_a, _shelf_b = _domains(db)
    assert next(row["on_delete"] for row in db.execute("PRAGMA foreign_key_list(items)")
                if row["from"] == "location_id") == "NO ACTION"
    live = insert_item(db, title="Live alias reference", media_type="book", owned=1, location_id=legacy_a)
    precise = _primary(db, live)["id"]
    item_copies.update_copy(db, precise, {
        "location_id": shelf_a, "position_order": 7, "copy_barcode": "ALIAS-LIVE-PRECISE",
        "condition": "Collector A+", "acquired_date": "2020-02-03", "acquisition_price": 12.5,
        "notes": "Preserved notes", "provenance": "Signed history"})
    historical = item_copies.add_copy(db, live, {
        "location_id": shelf_a, "position_order": 9, "copy_barcode": "ALIAS-TRASHED-COPY"})
    item_copies.trash_copy(db, historical)
    trashed = insert_item(db, title="Trashed alias reference", media_type="book", owned=1, location_id=legacy_a)
    item_copies.update_copy(db, _primary(db, trashed)["id"], {
        "location_id": shelf_a, "position_order": 2, "copy_barcode": "ALIAS-TRASHED-ITEM"})
    trash_item(db, trashed)
    root = insert_item(db, title="Mapped root stays placed", media_type="book", owned=1, location_id=legacy_a)
    unrelated = insert_item(db, title="Equal integer unrelated node", media_type="book", owned=1, location_id=legacy_b)
    unrelated_copy = _primary(db, unrelated)["id"]
    assert _primary(db, root)["location_id"] == node_a
    assert _primary(db, unrelated)["location_id"] == legacy_a  # NODE domain, not the deleted flat alias.
    db.commit()
    before = _alias_catalogue_state(db)
    _sign_in(client, editor_user)
    denied = client.post(f"/api/locations/{legacy_a}/delete", follow_redirects=False)
    assert denied.status_code == 403  # Location destruction remains GLOBAL admin.
    assert _alias_catalogue_state(db) == before
    _sign_in(client, admin_user)
    deleted = client.post(f"/api/locations/{legacy_a}/delete", follow_redirects=False)
    assert deleted.status_code == 303 and deleted.headers["location"] == "/settings"
    after = _alias_catalogue_state(db)
    assert after["items"] == [{**row, "location_id": None} if row["location_id"] == legacy_a else row
                              for row in before["items"]]
    assert after["item_copies"] == before["item_copies"]
    assert after["location_nodes"] == [
        {**row, "legacy_location_id": None} if row["legacy_location_id"] == legacy_a else row
        for row in before["location_nodes"]]
    assert after["locations"] == [row for row in before["locations"] if row["id"] != legacy_a]
    assert after["library_items"] == before["library_items"]
    assert db.execute("SELECT deleted_at FROM items WHERE id=?", (trashed,)).fetchone()[0] is not None
    assert db.execute("SELECT location_id FROM item_copies WHERE id=?", (unrelated_copy,)).fetchone()[0] == legacy_a
    holdings.sync_item(db, live)
    holdings.sync_item(db, root)
    assert _alias_catalogue_state(db) == after  # Later projection cannot flatten the detached root/child.
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("readonly", [False, True])
def test_physical_compat_alias_cleanup_refuses_hidden_or_readonly_copy_parent_atomically(db, editor_user, readonly):
    from app.services import locations as locations_svc

    legacy_a, legacy_b, _node_a, shelf_a, _shelf_b = _domains(db)
    private_library = libraries.create_library(db, "Alias cleanup boundary")["id"]
    if readonly:
        libraries.set_membership(db, private_library, editor_user["id"], "viewer")
    insert_item(db, title="Editable alias reference", media_type="book", owned=1, location_id=legacy_a)
    protected = insert_item(db, title="Protected precise copy", media_type="book", owned=1,
                            location_id=legacy_a, library_id=private_library)
    item_copies.update_copy(db, _primary(db, protected)["id"], {"location_id": shelf_a, "position_order": 8})
    # A precise child copy remains under A even when the independent item seam
    # moves to B; detaching A's mapping must still preflight this copy's parent.
    update_item_fields(db, protected, {"location_id": legacy_b})
    trash_item(db, protected)
    db.commit()
    before = _alias_catalogue_state(db)
    actor_token = security.set_actor(dict(editor_user))
    try:
        security.install_catalogue_views(db)
        with pytest.raises(HTTPException) as denied:
            locations_svc.delete_legacy_location(db, legacy_a)
        assert denied.value.status_code == (403 if readonly else 404)
        assert _alias_catalogue_state(db) == before
    finally:
        security.reset_actor(actor_token)
        security.install_catalogue_views(db)


@pytest.mark.parametrize("grade", ["Good", "Fine", "Very Good", "Ex-library", "Original Condition", "Collector A+ / signed"])
def test_physical_compat_free_text_condition_add_update_and_context(db, editor_client, grade):
    item = insert_item(db, title="Personal copy grading", media_type="book", owned=1)
    primary = _primary(db, item)["id"]
    db.commit()
    added = editor_client.post(f"/api/items/{item}/copies", data={"condition": f"  {grade}  "})
    assert added.status_code == 200 and not added.context["copy_error"]
    secondary = db.execute("SELECT * FROM item_copies WHERE item_id=? AND is_primary=0", (item,)).fetchone()
    assert secondary["condition"] == grade
    updated = editor_client.post(f"/api/items/{item}/copies/{primary}", data={"condition": f"  {grade}  "})
    assert updated.status_code == 200 and not updated.context["copy_error"]
    assert _primary(db, item)["condition"] == grade
    panel = editor_client.get(f"/api/items/{item}/copies/{primary}/edit")
    assert panel.status_code == 200
    assert panel.context["copy"]["condition"] == grade
    assert (grade, grade) in panel.context["conditions"]
    assert panel.context["condition_max_length"] == 60
    assert ("good", "Good") in panel.context["condition_presets"]
    assert ("Fine", "Fine") in panel.context["condition_presets"]
    condition = _Elements(panel.text).matching(name="condition")
    assert len(condition) == 1 and condition[0]["type"] == "text" and condition[0]["value"] == grade


def test_physical_compat_legacy_condition_survives_omission_and_failed_validation(db, editor_client):
    item = insert_item(db, title="Legacy grading history", media_type="book", owned=1)
    copy_id = _primary(db, item)["id"]
    historical = "Historical collector's description " + "x" * 60
    item_copies.update_copy(db, copy_id, {
        "condition": historical, "notes": "Keep notes", "copy_barcode": "LEGACY-GRADE",
        "acquisition_price": 14.25, "acquired_date": "2020-01-02", "provenance": "Keep history"})
    db.commit()
    panel = editor_client.get(f"/api/items/{item}/copies/{copy_id}/edit")
    assert (historical, historical) in panel.context["conditions"]
    omitted = editor_client.post(f"/api/items/{item}/copies/{copy_id}", data={"notes": "Changed only"})
    assert omitted.status_code == 200 and not omitted.context["copy_error"]
    assert _primary(db, item)["condition"] == historical
    unchanged = editor_client.post(f"/api/items/{item}/copies/{copy_id}",
                                   data={"condition": historical, "notes": "Still unchanged grade"})
    assert unchanged.status_code == 200 and not unchanged.context["copy_error"]
    before = dict(_primary(db, item))
    refused = editor_client.post(f"/api/items/{item}/copies/{copy_id}",
                                 data={"condition": historical, "acquisition_price": "not-money"})
    assert refused.status_code == 200
    assert refused.context["copy_error"] == "Acquisition price must be a number"
    assert dict(_primary(db, item)) == before
    too_long = editor_client.post(f"/api/items/{item}/copies/{copy_id}", data={"condition": "X" * 61})
    assert too_long.status_code == 200
    assert too_long.context["copy_error"] == "Condition must be 60 characters or fewer"
    assert dict(_primary(db, item)) == before
    boundary = editor_client.post(f"/api/items/{item}/copies/{copy_id}", data={"condition": "X" * 60})
    assert boundary.status_code == 200 and not boundary.context["copy_error"]
    assert _primary(db, item)["condition"] == "X" * 60
