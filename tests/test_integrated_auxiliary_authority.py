"""Real auxiliary HTTP boundaries; run only after the parent freezes all writers.

No replacement application, dependency overrides, fake security schema, skips or
accepted alternative outcomes. Source guards supplement (not replace) dispatch.
"""

import ast
from pathlib import Path

import pytest

from app import features
from app.services import api_tokens, item_write, libraries, periodical_records, user_state
from tests.conftest import _insert_item


BARCODE = "977016173700805"
PUBLIC_FIELDS = {
    "title", "authors", "cover_path", "media_type", "publish_year", "series_name", "series_position",
}
CATALOGUE_TABLES = (
    "items", "library_items", "item_copies", "collections", "collection_items", "item_links",
    "item_series", "list_items", "periodical_publications", "periodical_issues",
    "user_item_state", "user_reading_log", "duplicate_dismissals", "upcoming_releases",
)


def _item(db, title, *, library_id=1, **fields):
    return _insert_item(db, title=title, isbn=None, _library_id=library_id, **fields)


def _snapshot(db):
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in CATALOGUE_TABLES}


def _collection(db, name="Aux collection", members=()):
    collection_id = db.execute(
        "INSERT INTO collections (name, description) VALUES (?, 'Original description')", (name,),
    ).lastrowid
    db.executemany(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        [(collection_id, item_id) for item_id in members],
    )
    return collection_id


def _link_request(client, surface, anchor, target, *, remove=False):
    if surface == "fork":
        suffix = "/remove" if remove else ""
        return client.post(f"/api/items/{anchor}/related/{target}{suffix}", follow_redirects=False)
    if remove:
        return client.delete(f"/api/related-media/items/{anchor}/links/{target}")
    return client.post(
        f"/api/related-media/items/{anchor}/links",
        data={"other_item_id": target, "link_type": "adaptation"},
    )


def _confirm(client, **fields):
    return client.post("/api/periodicals/confirm", data={
        "raw_barcode": BARCODE, "publication_title": "Aux Journal", "issue_number": "1",
        **fields,
    }, follow_redirects=False)


def _issue(db, *, library_id=1, media_type="magazine", trashed=False):
    publication = periodical_records.upsert_publication(db, title="Aux Journal", issn="0161-7370")
    item = _item(db, "Secret issue identity", media_type=media_type, library_id=library_id)
    periodical_records.link_issue(
        db, item_id=item, publication_id=publication, issue_number="1",
        barcode_ean=BARCODE[:13], barcode_supplement=BARCODE[13:],
    )
    if trashed:
        item_write.trash_item(db, item)
    return item, publication


def _release(db, series="Aux Saga", *, library_id=1, hc_id=701):
    parent = _item(db, f"{series} volume one", library_id=library_id, series_name=series)
    watch = db.execute("INSERT INTO release_watches (series_name) VALUES (?)", (series,)).lastrowid
    release = db.execute(
        "INSERT INTO upcoming_releases (watch_id, hc_book_id, title, release_date) "
        "VALUES (?, ?, ?, '2027-01-01')", (watch, hc_id, f"{series} volume two"),
    ).lastrowid
    return parent, release


def _share(client, db, *, scope="collection", library_id=1):
    response = client.post("/api/share", data={
        "scope": scope, "label": "Aux public capability", "library_id": library_id,
    }, follow_redirects=False)
    assert response.status_code == 303
    return dict(db.execute("SELECT * FROM share_links ORDER BY id DESC LIMIT 1").fetchone())


@pytest.mark.parametrize("global_role,library_role,allowed", [
    ("viewer", "editor", True), ("editor", "viewer", False),
])
def test_collections_use_library_authority_not_global_role(db, request, global_role, library_role, allowed):
    client = request.getfixturevalue(f"{global_role}_client")
    user = request.getfixturevalue(f"{global_role}_user")
    libraries.set_membership(db, 1, user["id"], library_role)
    item = _item(db, "Membership target")
    collection = _collection(db)
    db.commit()
    before = _snapshot(db)

    added = client.post(f"/api/items/{item}/collections", data={"collection_id": collection})
    created = client.post("/api/collections", data={"name": "New empty definition"}, follow_redirects=False)
    assert added.status_code == (200 if allowed else 403)
    assert created.status_code == (303 if allowed else 403)
    if not allowed:
        assert _snapshot(db) == before
        return
    assert added.context["can_edit_item"] is True
    assert "Aux collection" in added.text
    renamed = client.post(f"/api/collections/{collection}", data={"name": "Renamed"}, follow_redirects=False)
    assert renamed.status_code == 303
    removed = client.delete(f"/api/items/{item}/collections/{collection}")
    assert removed.status_code == 200
    assert "This item is not in a collection" in removed.text
    assert client.delete(f"/api/collections/{collection}").status_code == 200
    assert db.execute("SELECT 1 FROM items WHERE id = ?", (item,)).fetchone()


@pytest.mark.parametrize("operation", ["rename", "delete"])
@pytest.mark.parametrize("trashed", [False, True])
def test_collection_definition_refuses_hidden_children_without_partial_effects(
    db, viewer_client, viewer_user, operation, trashed,
):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    private = libraries.create_library(db, "Hidden collection child library")
    visible = _item(db, "Visible collection child")
    hidden = _item(db, "NEVER EXPOSE COLLECTION CHILD", library_id=private["id"])
    if trashed:
        item_write.trash_item(db, hidden)
    collection = _collection(db, members=(visible, hidden))
    db.commit()
    before = _snapshot(db)

    page = viewer_client.get("/collections")
    card = next(row for row in page.context["collections"] if row["id"] == collection)
    assert card["item_count"] == 1
    assert [row["id"] for row in card["preview_items"]] == [visible]
    assert card["can_edit"] is False
    assert "NEVER EXPOSE COLLECTION CHILD" not in page.text
    if operation == "rename":
        response = viewer_client.post(f"/api/collections/{collection}", data={
            "name": "Must not rename", "description": "Must not replace",
        }, follow_redirects=False)
    else:
        response = viewer_client.delete(f"/api/collections/{collection}")
    assert response.status_code == 404
    assert "NEVER EXPOSE COLLECTION CHILD" not in response.text
    assert _snapshot(db) == before


@pytest.mark.parametrize("membership_role,status", [(None, 404), ("viewer", 403)])
@pytest.mark.parametrize("remove", [False, True])
def test_membership_mutations_authorize_the_actual_parent(db, viewer_client, viewer_user, membership_role, status, remove):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    other = libraries.create_library(db, "Membership boundary")
    if membership_role:
        libraries.set_membership(db, other["id"], viewer_user["id"], membership_role)
    target = _item(db, "Private membership target", library_id=other["id"])
    collection = _collection(db, members=(target,) if remove else ())
    db.commit()
    before = _snapshot(db)
    response = (viewer_client.delete(f"/api/items/{target}/collections/{collection}") if remove else
                viewer_client.post(f"/api/items/{target}/collections", data={"collection_id": collection}))
    assert response.status_code == status
    assert "Private membership target" not in response.text
    assert _snapshot(db) == before


@pytest.mark.parametrize("surface", ["fork", "modern"])
@pytest.mark.parametrize("global_role,library_role,allowed", [
    ("viewer", "editor", True), ("editor", "viewer", False),
])
def test_both_related_surfaces_use_library_authority(db, request, surface, global_role, library_role, allowed):
    client = request.getfixturevalue(f"{global_role}_client")
    user = request.getfixturevalue(f"{global_role}_user")
    libraries.set_membership(db, 1, user["id"], library_role)
    anchor, target = _item(db, "Related anchor"), _item(db, "Related target")
    db.commit()
    before = _snapshot(db)
    response = _link_request(client, surface, anchor, target)
    assert response.status_code == ((303 if surface == "fork" else 200) if allowed else 403)
    if not allowed:
        assert _snapshot(db) == before
        return
    edge = db.execute("SELECT * FROM item_links").fetchone()
    assert (edge["item_a_id"], edge["item_b_id"]) == (anchor, target)
    assert _link_request(client, surface, anchor, target, remove=True).status_code == (
        303 if surface == "fork" else 200
    )
    assert db.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 0


@pytest.mark.parametrize("surface", ["fork", "modern"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("remove", [False, True])
@pytest.mark.parametrize("target_role,status", [(None, 404), ("viewer", 403)])
def test_link_and_unlink_preflight_both_parents(db, viewer_client, viewer_user, surface, reverse, remove, target_role, status):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    other = libraries.create_library(db, "Other link parent")
    if target_role:
        libraries.set_membership(db, other["id"], viewer_user["id"], target_role)
    anchor = _item(db, "Editable anchor")
    target = _item(db, "OTHER LINK PARENT", library_id=other["id"])
    if remove:
        db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'related')",
                   (anchor, target))
    db.commit()
    before = _snapshot(db)
    a, b = (target, anchor) if reverse else (anchor, target)
    response = _link_request(viewer_client, surface, a, b, remove=remove)
    assert response.status_code == status
    assert "OTHER LINK PARENT" not in response.text
    assert _snapshot(db) == before


def test_fork_group_detach_refuses_a_hidden_third_edge_parent(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    private = libraries.create_library(db, "Hidden manual edge parent")
    anchor, target = _item(db, "Detach anchor"), _item(db, "Detach target")
    hidden = _item(db, "Hidden third parent", library_id=private["id"])
    db.executemany("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'related')",
                   [(anchor, target), (target, hidden)])
    db.commit()
    before = _snapshot(db)
    response = _link_request(viewer_client, "fork", anchor, target, remove=True)
    assert response.status_code == 404
    assert _snapshot(db) == before


@pytest.mark.parametrize("surface", ["fork", "modern"])
def test_related_search_scopes_anchor_and_editable_candidates(db, viewer_client, viewer_user, surface):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    private = libraries.create_library(db, "Hidden search")
    readonly = libraries.create_library(db, "Read-only search")
    libraries.set_membership(db, readonly["id"], viewer_user["id"], "viewer")
    anchor, editable = _item(db, "Aux Search anchor"), _item(db, "Aux Search editable")
    hidden = _item(db, "Aux Search HIDDEN", library_id=private["id"])
    readonly_item = _item(db, "Aux Search READONLY", library_id=readonly["id"])
    db.commit()
    prefix = "/api/items" if surface == "fork" else "/api/related-media/items"
    suffix = "/related/search" if surface == "fork" else "/search"
    response = viewer_client.get(f"{prefix}/{anchor}{suffix}", params={"q": "Aux Search"})
    assert response.status_code == 200
    assert [row["id"] for row in response.context["candidates"]] == [editable]
    assert response.context["candidates"][0]["can_edit_item"] is True
    assert "Aux Search HIDDEN" not in response.text and "Aux Search READONLY" not in response.text
    assert viewer_client.get(f"{prefix}/{hidden}{suffix}").status_code == 404
    assert viewer_client.get(f"{prefix}/{readonly_item}{suffix}").status_code == 403


def test_related_panel_has_per_item_editor_flags_and_no_hidden_bridge(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    readonly = libraries.create_library(db, "Related read only")
    private = libraries.create_library(db, "Related hidden bridge")
    libraries.set_membership(db, readonly["id"], viewer_user["id"], "viewer")
    anchor = _item(db, "Panel anchor")
    ro = _item(db, "Read only neighbour", library_id=readonly["id"])
    hidden = _item(db, "Secret bridge", library_id=private["id"])
    beyond = _item(db, "Beyond hidden bridge")
    db.executemany("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'related')",
                   [(anchor, ro), (anchor, hidden), (hidden, beyond)])
    db.commit()
    panel = viewer_client.get(f"/api/related-media/items/{anchor}/panel")
    assert panel.status_code == 200
    assert panel.context["can_edit"] is True
    assert [row["id"] for row in panel.context["related_items"]] == [ro]
    row = panel.context["related_items"][0]
    assert row["direct"] is True and row["can_edit_item"] is False and row["can_unlink"] is False
    assert "Secret bridge" not in panel.text and "Beyond hidden bridge" not in panel.text
    assert viewer_client.get(f"/api/related-media/items/{hidden}/panel").status_code == 404


def test_modern_link_checks_authority_inside_lock_before_any_edge_write(db, viewer_client, viewer_user, monkeypatch):
    from app.routers import related_media

    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    anchor, target = _item(db, "Locked anchor"), _item(db, "Locked target")
    db.commit()
    actual, checked = related_media.require_item_role, []

    def observe(connection, item_id, role, **kwargs):
        assert connection.in_transaction
        assert connection.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 0
        checked.append(item_id)
        return actual(connection, item_id, role, **kwargs)

    monkeypatch.setattr(related_media, "require_item_role", observe)
    assert _link_request(viewer_client, "modern", anchor, target).status_code == 200
    assert checked == [anchor, target]


@pytest.mark.parametrize("surface", ["fork", "modern"])
def test_caught_late_link_value_error_has_no_effects(db, viewer_client, viewer_user, monkeypatch, surface):
    from app.services import media_groups

    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    anchor, target = _item(db, "Atomic link anchor"), _item(db, "Atomic link target")
    db.commit()
    before = _snapshot(db)
    actual = media_groups.link_items

    def refuse_after_insert(*args, **kwargs):
        actual(*args, **kwargs)
        raise item_write.ItemValueError("Late link refusal")

    monkeypatch.setattr(media_groups, "link_items", refuse_after_insert)
    response = _link_request(viewer_client, surface, anchor, target)
    assert response.status_code == 400 and response.text == "Late link refusal"
    assert _snapshot(db) == before


@pytest.mark.parametrize("global_role,library_role,allowed", [
    ("viewer", "editor", True), ("editor", "viewer", False),
])
def test_periodical_confirmation_uses_library_authority(db, request, global_role, library_role, allowed):
    client = request.getfixturevalue(f"{global_role}_client")
    user = request.getfixturevalue(f"{global_role}_user")
    libraries.set_membership(db, 1, user["id"], library_role)
    db.commit()
    before = _snapshot(db)
    response = _confirm(client, mode="wishlist")
    assert response.status_code == (303 if allowed else 403)
    if not allowed:
        assert _snapshot(db) == before
        return
    issue = db.execute("SELECT item_id FROM periodical_issues").fetchone()[0]
    assert libraries.item_library_id(db, issue) == 1
    assert db.execute("SELECT owned FROM items WHERE id = ?", (issue,)).fetchone()[0] == 0
    assert db.execute("SELECT 1 FROM list_items WHERE item_id = ?", (issue,)).fetchone()


def test_periodical_can_insert_into_an_explicit_editable_library_without_default_write_access(db, viewer_client, viewer_user):
    other = libraries.create_library(db, "Explicit periodical destination")
    libraries.set_membership(db, other["id"], viewer_user["id"], "editor")
    db.commit()
    response = _confirm(viewer_client, library_id=other["id"])
    assert response.status_code == 303
    issue = db.execute("SELECT item_id FROM periodical_issues").fetchone()[0]
    assert libraries.item_library_id(db, issue) == other["id"]


@pytest.mark.parametrize("trashed", [False, True])
def test_periodical_hidden_confirmation_refuses_without_restore_or_disclosure(db, viewer_client, viewer_user, trashed):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    private = libraries.create_library(db, "Hidden periodical identity")
    _issue(db, library_id=private["id"], trashed=trashed)
    db.commit()
    before = _snapshot(db)
    response = _confirm(viewer_client)
    assert response.status_code == 400
    assert response.text == "This identifier is unavailable"
    assert "Secret issue identity" not in response.text
    assert _snapshot(db) == before


def test_periodical_read_only_match_cannot_be_reconfirmed(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    other = libraries.create_library(db, "Read only periodical")
    libraries.set_membership(db, other["id"], viewer_user["id"], "viewer")
    _issue(db, library_id=other["id"], trashed=True)
    db.commit()
    before = _snapshot(db)
    assert _confirm(viewer_client).status_code == 403
    assert _snapshot(db) == before


def test_visible_periodical_match_does_not_mask_competing_hidden_evidence(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    private = libraries.create_library(db, "Competing hidden issue")
    _issue(db)
    _issue(db, library_id=private["id"], trashed=True)
    db.commit()
    before = _snapshot(db)
    response = _confirm(viewer_client)
    assert response.status_code == 400 and response.text == "This identifier is unavailable"
    assert _snapshot(db) == before


def test_periodical_shared_publication_upsert_refuses_hidden_other_issues(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    private = libraries.create_library(db, "Shared publication hidden attachment")
    _issue(db, library_id=private["id"])
    db.commit()
    before = _snapshot(db)
    response = _confirm(viewer_client, raw_barcode=BARCODE[:13] + "07", issue_number="2", publisher="Must not overwrite")
    assert response.status_code == 400
    assert response.text == "This identifier is unavailable"
    assert _snapshot(db) == before


@pytest.mark.parametrize("failure_stage", ["insert", "link"])
def test_late_periodical_value_error_rolls_back_the_whole_operation(db, viewer_client, viewer_user, monkeypatch, failure_stage):
    from app.routers import periodicals

    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    _issue(db)
    db.commit()
    before = _snapshot(db)
    owner = periodicals if failure_stage == "insert" else periodicals.periodical_records
    name = "insert_item" if failure_stage == "insert" else "link_issue"
    actual = getattr(owner, name)

    def fail_after_write(*args, **kwargs):
        actual(*args, **kwargs)
        raise item_write.ItemValueError("Late issue refusal")

    queued = []
    monkeypatch.setattr(owner, name, fail_after_write)
    monkeypatch.setattr(periodicals.cover_queue, "enqueue", lambda *args, **kwargs: queued.append(args))
    response = _confirm(viewer_client, raw_barcode=BARCODE[:13] + "07", issue_number="2",
                        publication_title="Changed shared title", publisher="Changed publisher",
                        cover_url="https://books.google.com/aux.jpg")
    assert response.status_code == 400 and response.text == "Late issue refusal"
    assert _snapshot(db) == before
    assert queued == []


def test_reconfirming_legacy_kids_book_issue_retains_id_type_and_children(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    item, _ = _issue(db, media_type="kids_book", trashed=True)
    user_state.save_state(db, viewer_user["id"], item, personal_notes="Preserved issue notes")
    children = [tuple(row) for row in db.execute("SELECT * FROM periodical_issues")]
    db.commit()
    response = _confirm(viewer_client)
    assert response.status_code == 303 and response.headers["location"] == f"/item/{item}"
    row = db.execute("SELECT media_type, deleted_at FROM items WHERE id = ?", (item,)).fetchone()
    assert row["media_type"] == "kids_book" and row["deleted_at"] is None
    assert [tuple(row) for row in db.execute("SELECT * FROM periodical_issues")] == children
    assert user_state.get_state(db, viewer_user["id"], item)["personal_notes"] == "Preserved issue notes"


def test_periodical_pages_hide_hidden_only_publications_and_raw_counts(db, viewer_client):
    private = libraries.create_library(db, "Invisible publication")
    hidden, publication = _issue(db, library_id=private["id"])
    db.commit()
    page = viewer_client.get("/periodicals")
    assert page.status_code == 200 and page.context["publications"] == []
    detail = viewer_client.get(f"/periodicals/{publication}", follow_redirects=False)
    assert detail.status_code == 303 and detail.headers["location"] == "/periodicals"
    visible = _item(db, "Visible issue", media_type="magazine")
    periodical_records.link_issue(db, item_id=visible, publication_id=publication, issue_number="2")
    db.commit()
    page = viewer_client.get("/periodicals")
    assert page.context["publications"][0]["issue_count"] == 1
    detail = viewer_client.get(f"/periodicals/{publication}")
    assert [row["item_id"] for row in detail.context["issues"]] == [visible]
    assert str(hidden) != str(visible) and "Secret issue identity" not in detail.text


@pytest.mark.parametrize("global_role,library_role,allowed", [
    ("viewer", "editor", True), ("editor", "viewer", False),
])
def test_upcoming_dismissal_uses_library_authority(db, request, global_role, library_role, allowed):
    client = request.getfixturevalue(f"{global_role}_client")
    user = request.getfixturevalue(f"{global_role}_user")
    libraries.set_membership(db, 1, user["id"], library_role)
    _, release = _release(db)
    db.commit()
    response = client.post(f"/api/upcoming/{release}/dismiss")
    assert response.status_code == (200 if allowed else 403)
    assert db.execute("SELECT dismissed FROM upcoming_releases WHERE id = ?", (release,)).fetchone()[0] == int(allowed)


def test_upcoming_filters_hidden_watches_and_hidden_held_keys_before_counts(db, viewer_client):
    private = libraries.create_library(db, "Hidden watch and holding")
    _, visible = _release(db)
    _release(db, "Private Saga", library_id=private["id"], hc_id=702)
    _item(db, "Hidden held announcement", library_id=private["id"], hardcover_book_id=701)
    db.commit()
    response = viewer_client.get("/upcoming")
    assert response.status_code == 200
    assert [row["id"] for row in response.context["releases"]] == [visible]
    assert response.context["releases"][0]["can_edit"] is False
    assert "Private Saga" not in response.text and "Hidden held announcement" not in response.text


@pytest.mark.parametrize("other_role,status", [(None, 404), ("viewer", 403)])
def test_shared_upcoming_dismiss_refuses_hidden_or_read_only_series_effects(db, viewer_client, viewer_user, other_role, status):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    private = libraries.create_library(db, "Shared watch other parent")
    if other_role:
        libraries.set_membership(db, private["id"], viewer_user["id"], other_role)
    _, release = _release(db)
    _item(db, "Shared watch other volume", library_id=private["id"], series_name="Aux Saga")
    db.commit()
    before = _snapshot(db)
    page = viewer_client.get("/upcoming")
    assert page.context["releases"][0]["can_edit"] is False
    assert viewer_client.post(f"/api/upcoming/{release}/dismiss").status_code == status
    assert _snapshot(db) == before


def test_catalogue_editors_are_not_global_duplicates_rebuild_or_share_admins(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    a, b = _item(db, "Aux twin"), _item(db, "Aux twin")
    db.commit()
    before = _snapshot(db)
    for method in ("POST", "DELETE"):
        assert viewer_client.request(method, "/api/duplicates/dismiss", json={"item_ids": [a, b]}).status_code == 403
    assert viewer_client.post("/api/items/connections/rebuild").status_code == 403
    assert viewer_client.post("/api/share", data={"scope": "collection"}).status_code == 403
    assert _snapshot(db) == before
    assert db.execute("SELECT COUNT(*) FROM share_links").fetchone()[0] == 0


def test_duplicates_preflight_all_ids_and_keep_native_bounds_and_refusals(db, admin_client):
    a, b = _item(db, "Dismiss one"), _item(db, "Dismiss two")
    unmapped = _item(db, "Unmapped duplicate target", library_id=None)
    db.commit()
    before = _snapshot(db)
    response = admin_client.post("/api/duplicates/dismiss", json={"item_ids": [a, b, unmapped]})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "One of these records no longer exists — reload the page"}
    assert _snapshot(db) == before
    oversized = admin_client.post("/api/duplicates/dismiss", json={"item_ids": list(range(1, 202))})
    assert oversized.json() == {"ok": False, "message": "Too many records in one group"}
    assert admin_client.post("/api/duplicates/dismiss", json={"item_ids": [a, b]}).json() == {"ok": True, "pairs": 1}
    assert admin_client.request("DELETE", "/api/duplicates/dismiss", json={"item_ids": [a, unmapped]}).json()["ok"] is False
    assert db.execute("SELECT COUNT(*) FROM duplicate_dismissals").fetchone()[0] == 1


def test_caught_late_dismissal_value_error_rolls_back_all_pairs(db, admin_client, monkeypatch):
    from app.services import duplicates

    ids = [_item(db, "Atomic dismissal") for _ in range(3)]
    db.commit()
    before = _snapshot(db)
    actual = duplicates.dismiss

    def refuse_after_pairs(*args, **kwargs):
        actual(*args, **kwargs)
        raise item_write.ItemValueError("Late dismissal refusal")

    monkeypatch.setattr(duplicates, "dismiss", refuse_after_pairs)
    response = admin_client.post("/api/duplicates/dismiss", json={"item_ids": ids})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "Late dismissal refusal"}
    assert _snapshot(db) == before


def test_viewer_token_is_never_promoted_by_admin_identity_or_editor_membership(db, admin_client, admin_user):
    from fastapi import HTTPException
    from app.services.catalogue_security import require_item_role

    token = api_tokens.create(db, admin_user["id"], "Aux viewer cap")
    a, b = _item(db, "Token anchor"), _item(db, "Token target")
    collection = _collection(db)
    db.commit()
    before = _snapshot(db)
    admin_client.cookies.delete("access_token")
    admin_client.headers["Authorization"] = f"Bearer {token['token']}"
    principal = api_tokens.resolve(token["token"])
    assert principal["role"] == "viewer" and principal["account_role"] == "admin"
    with pytest.raises(HTTPException) as refusal:
        require_item_role(db, a, "editor", user=principal)
    assert refusal.value.status_code == 403
    assert admin_client.get("/api/v1/stats").status_code == 200
    # Non-v1 routes do not accept this capability at all. Disable following to
    # distinguish the actual native refusal from the200 login form document.
    admin_client.follow_redirects = False
    response = admin_client.post(f"/api/items/{a}/collections", data={"collection_id": collection})
    assert response.status_code == 303 and response.headers["location"] == "/login"
    for surface in ("fork", "modern"):
        response = _link_request(admin_client, surface, a, b)
        assert response.status_code == 303 and response.headers["location"] == "/login"
    response = _confirm(admin_client)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    response = admin_client.post("/api/duplicates/dismiss", json={"item_ids": [a, b]})
    assert response.status_code == 303 and response.headers["location"] == "/login"
    response = admin_client.post("/api/share", data={"scope": "collection"})
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert admin_client.post("/api/v1/stats").status_code == 401
    assert _snapshot(db) == before


def test_public_share_projects_no_private_state_and_scopes_new_and_legacy_capabilities(db, admin_client, client, viewer_user):
    private = libraries.create_library(db, "Not implicitly public")
    main = _item(db, "PUBLIC MAIN TITLE", notes="SHARED PRIVATE NOTE", reading_status="read", date_finished="1999-03-04")
    _item(db, "PRIVATE LIBRARY TITLE", library_id=private["id"])
    user_state.save_state(db, viewer_user["id"], main, personal_notes="PERSONAL SECRET", rating=5, date_finished="2001-02-03")
    user_state.set_reading_status(db, viewer_user["id"], main, "read")
    db.execute("INSERT INTO share_links (token, scope) VALUES ('legacy-aux-capability', 'collection')")
    db.commit()
    link = _share(admin_client, db)
    assert link["token"].startswith("s1.1.")
    client.cookies.clear()
    for token in (link["token"], "legacy-aux-capability"):
        response = client.get(f"/share/{token}")
        assert response.status_code == 200
        assert response.headers["x-robots-tag"] == "noindex"
        assert [row["title"] for row in response.context["items"]] == ["PUBLIC MAIN TITLE"]
        assert set(response.context["items"][0]) == PUBLIC_FIELDS
        for secret in ("PRIVATE LIBRARY TITLE", "SHARED PRIVATE NOTE", "PERSONAL SECRET", "1999-03-04", "2001-02-03"):
            assert secret not in response.text


def test_explicit_public_library_selection_is_stored_and_not_visitor_dependent(db, admin_client, client, viewer_user):
    from app.auth import create_token

    selected = libraries.create_library(db, "Explicitly shared library")
    other = libraries.create_library(db, "Unselected private library")
    _item(db, "Selected public title", library_id=selected["id"])
    _item(db, "Never selected title", library_id=other["id"])
    _item(db, "Unselected Main title")
    db.commit()
    link = _share(admin_client, db, library_id=selected["id"])
    assert link["token"].startswith(f"s1.{selected['id']}.")
    client.cookies.clear()
    anonymous = client.get(f"/share/{link['token']}")
    client.cookies.set("access_token", create_token(viewer_user["id"], viewer_user["username"], "viewer"))
    authenticated = client.get(f"/share/{link['token']}")
    assert anonymous.status_code == authenticated.status_code == 200
    assert anonymous.context["items"] == authenticated.context["items"]
    assert [row["title"] for row in anonymous.context["items"]] == ["Selected public title"]
    tampered = link["token"].replace(f"s1.{selected['id']}.", f"s1.{other['id']}.", 1)
    assert client.get(f"/share/{tampered}").status_code == 404


def test_public_wishlist_is_shared_acquisition_intent_not_some_users_personal_wishlist(db, admin_client, client, viewer_user):
    shared = _item(db, "Shared acquisition wish", owned=0, wishlisted=True)
    personal = _item(db, "Only personal wish", owned=0)
    user_state.save_state(db, viewer_user["id"], personal, wishlist=1)
    _item(db, "Owned not wished")
    db.commit()
    link = _share(admin_client, db, scope="wishlist")
    client.cookies.clear()
    response = client.get(f"/share/{link['token']}")
    assert response.status_code == 200
    assert [row["title"] for row in response.context["items"]] == ["Shared acquisition wish"]
    assert "Only personal wish" not in response.text and "Owned not wished" not in response.text
    assert shared != personal


def test_public_share_cap_is_applied_after_library_scope_not_to_hidden_candidates(db, admin_client, client):
    private = libraries.create_library(db, "Hidden before cap")
    for number in range(501):
        _item(db, f"A HIDDEN {number:04d}", library_id=private["id"])
        _item(db, f"B PUBLIC {number:04d}")
    db.commit()
    link = _share(admin_client, db)
    client.cookies.clear()
    response = client.get(f"/share/{link['token']}")
    assert response.status_code == 200
    assert len(response.context["items"]) == 500
    assert all(row["title"].startswith("B PUBLIC") for row in response.context["items"])
    assert response.context["items"][-1]["title"] == "B PUBLIC 0499"
    assert "A HIDDEN" not in response.text and "B PUBLIC 0500" not in response.text


def test_share_feature_off_and_unknown_token_are_identical_without_disabling_the_feature_as_policy(db, admin_client, client):
    _item(db, "Feature-gated public item")
    db.commit()
    link = _share(admin_client, db)
    features.set_feature_enabled(db, "share", False)
    db.commit()
    features.invalidate_cache()
    client.cookies.clear()
    known, unknown = client.get(f"/share/{link['token']}"), client.get("/share/no-such-aux-token")
    assert known.status_code == unknown.status_code == 404
    assert known.content == unknown.content == b"Not found"
    assert known.headers["x-robots-tag"] == unknown.headers["x-robots-tag"] == "noindex"


def test_share_creation_refuses_invalid_library_without_storing_a_capability(db, admin_client):
    before = db.execute("SELECT COUNT(*) FROM share_links").fetchone()[0]
    response = admin_client.post("/api/share", data={"scope": "collection", "library_id": 999999})
    assert response.status_code == 404
    assert db.execute("SELECT COUNT(*) FROM share_links").fetchone()[0] == before


def test_auxiliary_writes_retain_the_real_cookie_csrf_boundary(db, admin_client):
    a, b = _item(db, "CSRF anchor"), _item(db, "CSRF target")
    collection = _collection(db)
    _, release = _release(db)
    db.commit()
    before = _snapshot(db)
    del admin_client.headers["X-CSRF-Token"]
    responses = [
        admin_client.post("/api/collections", data={"name": "Must not create"}),
        admin_client.post(f"/api/items/{a}/collections", data={"collection_id": collection}),
        _link_request(admin_client, "fork", a, b),
        _link_request(admin_client, "modern", a, b),
        _confirm(admin_client),
        admin_client.post(f"/api/upcoming/{release}/dismiss"),
        admin_client.post("/api/duplicates/dismiss", json={"item_ids": [a, b]}),
        admin_client.post("/api/share", data={"scope": "collection"}),
    ]
    assert [response.status_code for response in responses] == [403] * len(responses)
    assert _snapshot(db) == before
    assert db.execute("SELECT COUNT(*) FROM share_links").fetchone()[0] == 0


def test_catalogue_route_sources_keep_explicit_native_dependencies_and_write_scopes():
    root = Path(__file__).resolve().parents[1] / "app" / "routers"
    catalogue = {
        "collections": ("create_collection", "update_collection", "delete_collection", "add_item_to_collection", "remove_item_from_collection"),
        "related": ("add_related_media", "remove_related_media"),
        "related_media": ("related_media_link", "related_media_unlink"),
        "periodicals": ("confirm_periodical_issue",),
        "upcoming": ("dismiss_release",),
    }
    for module, names in catalogue.items():
        tree = ast.parse((root / f"{module}.py").read_text())
        functions = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
        for name in names:
            node = functions[name]
            assert any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                       and call.func.id == "require_catalogue_role" and call.args[0].value == "editor"
                       for default in node.args.defaults for call in ast.walk(default)), (module, name)
            assert any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                       and call.func.attr == "_write_scope" for call in ast.walk(node)), (module, name)
    for module in (*catalogue, "duplicates", "share"):
        text = (root / f"{module}.py").read_text()
        assert "upstream_034" not in text and "_remove_route" not in text and "include_router(" not in text
