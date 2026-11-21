"""Fork security/history contracts composed with native Trash/write funnels.

These exercise real central schema/services, not patched routers, inheritance
seeds or a reduced application. Run only after the parent freezes the candidate.
"""

import sqlite3
from contextlib import contextmanager
from contextvars import copy_context

import pytest
from fastapi import HTTPException

from app.auth import create_token
from app.database import get_db
from app.services import api_tokens, catalogue_security as security
from app.services import item_copies, item_merge, item_write, libraries, lists, trash, user_state


def _user(db, name="lane-user", role="viewer"):
    cur = db.execute(
        "INSERT INTO users (username, password, role) VALUES (?, 'not-a-login', ?)",
        (name, role),
    )
    return {"id": cur.lastrowid, "username": name, "role": role}


def _item(db, title="Lane item", **fields):
    return item_write.insert_item(db, title=title, source="security-regression", **fields)


@contextmanager
def _acting(db, user):
    token = security.set_actor(user)
    try:
        security.install_catalogue_views(db)
        yield
    finally:
        security.reset_actor(token)
        security.install_catalogue_views(db)


def _snapshot(db):
    return tuple(db.iterdump())


def _request_as(client, user, method, url, **kwargs):
    """Exercise real canonical session/ASGI dispatch for each named principal."""
    client.cookies.set("access_token", create_token(
        user["id"], user["username"], user["role"],
        user.get("display_name"), user.get("token_version", 1),
    ))
    return client.request(method, url, **kwargs)


def _complementary_release_items(db):
    keep = _item(db, "Collector keeper", media_type="cd")
    source = _item(db, "Identified source", media_type="cd")
    db.execute(
        "INSERT INTO music_releases (item_id, edition_notes, media_condition, media_count) "
        "VALUES (?, 'Collector numbered copy', 'good', 0)", (keep,),
    )
    db.execute(
        "INSERT INTO music_releases (item_id, musicbrainz_release_id, artist_credit, catalog_number) "
        "VALUES (?, 'complementary-release-identity', 'Source Artist', 'SOURCE-001')", (source,),
    )
    return keep, source


def test_live_views_intersect_library_liveness_and_actor_private_state(db):
    first = _user(db)
    other = _user(db, "another")
    private = libraries.create_library(db, "Lane private")
    libraries.set_membership(db, 1, first["id"], "viewer")
    visible = _item(db, "Visible", reading_status="read", date_finished="2001-01-01")
    hidden = _item(db, "Hidden", library_id=private["id"])
    deleted = _item(db, "Deleted")
    item_write.trash_item(db, deleted)
    user_state.save_state(db, other["id"], visible, reading_status="reading")
    with _acting(db, first):
        rows = db.execute("SELECT * FROM items_live ORDER BY id").fetchall()
        assert [row["id"] for row in rows] == [visible]
        assert rows[0]["reading_status"] is None
        assert rows[0]["date_finished"] is None
        assert {row[0] for row in db.execute("SELECT item_id FROM copies_live")} == {visible}
    assert {row[0] for row in db.execute("SELECT id FROM items_live")} == {visible, hidden}
    assert security.current_actor() is None


def test_missing_security_schema_refuses_instead_of_unscoping():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO items VALUES (1)")
    token = security.set_actor({"id": 1, "role": "admin"})
    try:
        with pytest.raises(sqlite3.OperationalError, match="library_items"):
            security.install_catalogue_views(conn)
        assert conn.execute("SELECT * FROM items_live").fetchall() == []
    finally:
        security.reset_actor(token)
        conn.close()


def test_copied_http_context_expires_closed_not_as_trusted_service():
    token = security.set_actor({"id": 1, "role": "admin"})
    inherited = copy_context()
    security.reset_actor(token)
    assert security.current_actor() is None
    with pytest.raises(RuntimeError, match="request scope has ended"):
        inherited.run(security.current_actor)


def test_library_authority_is_not_the_legacy_global_role_ceiling(db):
    viewer = _user(db, "global-viewer", "viewer")
    editor = _user(db, "global-editor", "editor")
    item = _item(db)
    libraries.set_membership(db, 1, viewer["id"], "editor")
    libraries.set_membership(db, 1, editor["id"], "viewer")
    with _acting(db, viewer):
        item_write.update_item_fields(db, item, {"title": "Library editor wrote this"})
    with _acting(db, editor):
        with pytest.raises(HTTPException) as exc:
            item_write.update_item_fields(db, item, {"title": "Not allowed"})
        assert exc.value.status_code == 403
    assert db.execute("SELECT title FROM items WHERE id = ?", (item,)).fetchone()[0] == "Library editor wrote this"


def test_missing_mapping_denies_even_account_admin(db):
    admin = _user(db, role="admin")
    item = _item(db)
    db.execute("DELETE FROM library_items WHERE item_id = ?", (item,))
    with _acting(db, admin):
        assert db.execute("SELECT 1 FROM items_live WHERE id = ?", (item,)).fetchone() is None
        with pytest.raises(HTTPException) as exc:
            security.require_item_role(db, item, "editor", include_deleted=True)
        assert exc.value.status_code == 404


def test_token_admin_read_visibility_is_separate_from_viewer_cap(db):
    admin = _user(db, role="admin")
    private = libraries.create_library(db, "Token admin read scope")
    item = _item(db, library_id=private["id"])
    minted = api_tokens.create(db, admin["id"], "read only")
    db.commit()
    principal = api_tokens.resolve(minted["token"])
    assert principal["role"] == "viewer"
    assert principal["account_role"] == "admin"
    with _acting(db, principal):
        assert db.execute("SELECT id FROM items_live").fetchone()[0] == item
        with pytest.raises(HTTPException) as exc:
            item_write.trash_item(db, item)
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            user_state.save_state(db, admin["id"], item, wishlist=1)
        assert exc.value.status_code == 403


@pytest.mark.parametrize("trashed", [False, True])
@pytest.mark.parametrize("identifier", [{"isbn": "9780000000118"}, {"upc": "012569803121"}])
def test_hidden_identifier_collisions_never_disclose_restore_or_reassign(db, trashed, identifier):
    actor = _user(db)
    libraries.set_membership(db, 1, actor["id"], "editor")
    private = libraries.create_library(db, "Hidden identifier owner")
    hidden = _item(db, "Never disclose this title", library_id=private["id"], **identifier)
    if trashed:
        item_write.trash_item(db, hidden)
    with _acting(db, actor):
        before = _snapshot(db)
        with pytest.raises(item_write.IdentifierUnavailable) as exc:
            _item(db, "Attempted duplicate", library_id=1, **identifier)
        assert str(exc.value) == "This identifier is unavailable"
        assert not hasattr(exc.value, "item_id")
        assert not hasattr(exc.value, "title")
        assert _snapshot(db) == before


def test_media_type_only_collision_and_mixed_bulk_refuse_atomically(db):
    actor = _user(db)
    libraries.set_membership(db, 1, actor["id"], "editor")
    private = libraries.create_library(db, "Type collision")
    _item(db, "Secret type twin", library_id=private["id"], isbn="9780000000118")
    target = _item(db, "Target", media_type="audiobook", isbn="9780000000118")
    second = _item(db, "Second", media_type="audiobook")
    with _acting(db, actor):
        before = _snapshot(db)
        with pytest.raises(item_write.IdentifierUnavailable):
            item_write.update_items_fields(db, [second, target], {"media_type": "book"})
        assert _snapshot(db) == before


def test_projection_and_private_refusal_roll_back_metadata_in_catching_importer(db):
    actor = _user(db)
    libraries.set_membership(db, 1, actor["id"], "editor")
    item = _item(db, "Must remain")
    user_state.save_state(db, actor["id"], item, progress_value=5, progress_total=10)
    with _acting(db, actor):
        before = _snapshot(db)
        with pytest.raises(ValueError, match="exceed"):
            item_write.update_item_fields(db, item, {"title": "Must roll back", "progress_value": 11})
        assert _snapshot(db) == before
    before = _snapshot(db)
    with pytest.raises(LookupError, match="Library not found"):
        _item(db, "No partial insert", library_id=999999)
    assert _snapshot(db) == before


def test_personal_wishlist_and_reading_do_not_modify_shared_acquisition_partition(db):
    actor = _user(db)
    other = _user(db, "private-other")
    libraries.set_membership(db, 1, actor["id"], "editor")
    item = _item(db, owned=1)
    db.execute("UPDATE items SET updated_at = '2000-01-01' WHERE id = ?", (item,))
    stored = tuple(db.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone())
    with _acting(db, actor):
        item_write.update_item_fields(db, item, {"reading_status": "read", "wishlist": 1, "personal_notes": "Only mine"})
        assert db.execute("SELECT reading_status FROM items_live WHERE id = ?", (item,)).fetchone()[0] == "read"
        with pytest.raises(HTTPException) as exc:
            user_state.save_state(db, other["id"], item, rating=5)
        assert exc.value.status_code == 403
        with pytest.raises(item_write.InvalidWishlisted):
            item_write.update_item_fields(db, item, {"wishlisted": True})
    assert tuple(db.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone()) == stored
    assert user_state.get_state(db, actor["id"], item)["wishlist"] == 1
    assert user_state.get_state(db, other["id"], item)["personal_notes"] is None
    assert not lists.is_member(db, lists.WISHLIST, item)


def test_http_sole_user_never_inherits_legacy_history(db):
    actor = _user(db)
    libraries.set_membership(db, 1, actor["id"], "viewer")
    item = _item(db)
    db.execute("INSERT INTO reading_log (item_id, status, date_finished) VALUES (?, 'read', '2001-01-01')", (item,))
    with _acting(db, actor):
        assert user_state.get_reading_history(db, actor["id"], item) == []


def test_native_trash_restore_preserves_ids_copies_private_history_and_ownership(db):
    actor = _user(db)
    item = _item(db, "Kept title", isbn="9780000000118", owned=0, wishlisted=True)
    user_state.set_reading_status(db, actor["id"], item, "read")
    children = [tuple(r) for r in db.execute("SELECT * FROM user_reading_log")]
    assert item_write.trash_item(db, item)
    assert not item_write.trash_item(db, item)
    assert db.execute("SELECT id FROM items WHERE id = ?", (item,)).fetchone()
    again = _item(db, "Provider title must not overwrite", isbn="9780000000118", owned=1)
    assert again == item and item_write.was_restored(again)
    assert db.execute("SELECT title, owned FROM items WHERE id = ?", (item,)).fetchone()["title"] == "Kept title"
    assert [tuple(r) for r in db.execute("SELECT * FROM user_reading_log")] == children
    assert not lists.is_member(db, lists.WISHLIST, item)
    assert db.execute("SELECT 1 FROM copies_live WHERE item_id = ?", (item,)).fetchone()
    with pytest.raises(ValueError, match="database assigns"):
        item_write.update_item_fields(db, item, {"deleted_at": "2000-01-01"})


def test_owned_item_trash_hides_but_never_rewrites_its_physical_copies(db):
    item = _item(db, "Physical retention")
    before = [tuple(r) for r in db.execute("SELECT * FROM item_copies WHERE item_id = ?", (item,))]
    assert before
    assert item_write.trash_item(db, item)
    assert db.execute("SELECT 1 FROM copies_live WHERE item_id = ?", (item,)).fetchone() is None
    assert [tuple(r) for r in db.execute("SELECT * FROM item_copies WHERE item_id = ?", (item,))] == before
    assert item_write.restore_item(db, item)
    assert [tuple(r) for r in db.execute("SELECT * FROM item_copies WHERE item_id = ?", (item,))] == before


def test_initial_and_mixed_bulk_acquisition_do_not_recreate_routine_zero_copy_state(db):
    routine = _item(db, "Initial physical copy", owned=1)
    assert db.execute("SELECT COUNT(*) FROM item_copies WHERE item_id = ?", (routine,)).fetchone()[0] == 1
    item_copies.delete_copies_for_item(db, routine)  # Explicit archive zero-copy replay.
    item_write.update_item_fields(db, routine, {"owned": 1, "location_id": None, "title": "Routine refresh"})
    assert db.execute("SELECT COUNT(*) FROM item_copies WHERE item_id = ?", (routine,)).fetchone()[0] == 0

    actor = _user(db, "acquisition-private-owner")
    acquired = _item(db, "Real acquisition", owned=0, wishlisted=True)
    user_state.save_state(db, actor["id"], acquired, wishlist=1)
    assert db.execute("SELECT COUNT(*) FROM item_copies WHERE item_id = ?", (acquired,)).fetchone()[0] == 0
    item_write.update_items_fields(db, [routine, acquired], {"owned": "1", "location_id": None})
    assert db.execute("SELECT COUNT(*) FROM item_copies WHERE item_id = ?", (routine,)).fetchone()[0] == 0
    copies = db.execute("SELECT id, location_id FROM item_copies WHERE item_id = ?", (acquired,)).fetchall()
    assert len(copies) == 1 and copies[0]["location_id"] is None
    assert not lists.is_member(db, lists.WISHLIST, acquired)
    assert user_state.get_state(db, actor["id"], acquired)["wishlist"] == 1


@pytest.mark.parametrize("trashed", [False, True])
def test_explicit_single_acquisition_preserves_existing_copy_history_without_extra_copy(db, trashed):
    item = _item(db, "Retained acquisition history", owned=1)
    copy_id = db.execute("SELECT id FROM item_copies WHERE item_id = ?", (item,)).fetchone()[0]
    item_copies.update_copy(db, copy_id, {"copy_barcode": "ACQUISITION-HISTORY", "provenance": "Retained original"})
    if trashed:
        item_copies.trash_copy(db, copy_id, replay=True)  # Validated archive replay, not HTTP removal.
    item_write.update_item_fields(db, item, {"owned": 0})
    before = db.execute("SELECT id, deleted_at, copy_barcode, provenance FROM item_copies WHERE item_id = ?", (item,)).fetchall()
    item_write.update_item_fields(db, item, {"owned": 1})
    after = db.execute("SELECT id, deleted_at, copy_barcode, provenance FROM item_copies WHERE item_id = ?", (item,)).fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert len(after) == 1 and after[0]["id"] == copy_id


def test_merge_reparents_source_copies_before_owned_keeper_promotion_without_placeholder(db):
    keep = _item(db, "Unowned keeper", owned=0, wishlisted=True)
    source = _item(db, "Owned source", owned=1)
    copy_id = db.execute("SELECT id FROM item_copies WHERE item_id = ?", (source,)).fetchone()[0]
    item_copies.update_copy(db, copy_id, {"copy_barcode": "MERGE-REAL-COPY", "provenance": "Actual source copy"})
    item_merge.reparent_children(db, keep, source)
    assert [row[0] for row in db.execute("SELECT id FROM item_copies ORDER BY id")] == [copy_id]
    assert db.execute("SELECT item_id FROM item_copies WHERE id = ?", (copy_id,)).fetchone()[0] == keep
    db.execute("DELETE FROM items WHERE id = ?", (source,))
    item_write.update_item_fields(db, keep, {"owned": 1})
    copies = db.execute("SELECT id, copy_barcode, provenance FROM item_copies WHERE item_id = ?", (keep,)).fetchall()
    assert [tuple(row) for row in copies] == [(copy_id, "MERGE-REAL-COPY", "Actual source copy")]
    assert not lists.is_member(db, lists.WISHLIST, keep)


def test_restore_never_moves_a_visible_identifier_into_requested_other_library(db):
    original_library = libraries.create_library(db, "Restore identity library")
    item = _item(db, "Original identity", library_id=original_library["id"], isbn="9780000000118")
    item_write.trash_item(db, item)
    before = _snapshot(db)
    with pytest.raises(item_write.ItemValueError, match="requested library"):
        _item(db, "No reassignment", library_id=1, isbn="9780000000118")
    assert _snapshot(db) == before


def test_trusted_service_can_update_and_reindex_trash_without_restore_or_history_changes(db):
    """Trusted maintenance is not HTTP editing and must not restore its parent."""
    owner = _user(db, "maintenance-private-owner")
    library = libraries.create_library(db, "Maintenance identity library")
    item = _item(db, "Trashed index", authors="Original Author", library_id=library["id"])
    copy_id = db.execute("SELECT id FROM item_copies WHERE item_id = ?", (item,)).fetchone()[0]
    item_copies.update_copy(db, copy_id, {"copy_barcode": "MAINTENANCE-KEPT", "provenance": "Original acquisition"})
    user_state.save_state(db, owner["id"], item, personal_notes="Private retained maintenance note", wishlist=1)
    user_state.set_reading_status(db, owner["id"], item, "read")
    assert item_write.trash_item(db, item, at="2026-01-01 00:00:00")
    db.execute("UPDATE items SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (item,))
    before = dict(db.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone())
    retained = {
        table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in ("library_items", "item_copies", "user_item_state", "user_reading_log", "reading_log")
    }
    assert retained["item_copies"] and retained["user_item_state"] and retained["user_reading_log"]
    assert db.execute("SELECT id FROM items_live WHERE id = ?", (item,)).fetchone() is None
    assert db.execute("SELECT id FROM copies_live WHERE item_id = ?", (item,)).fetchone() is None
    assert [tuple(row) for row in db.execute(
        "SELECT a.name, a.name_key, ia.role, ia.position FROM item_authors ia "
        "JOIN authors a ON a.id = ia.author_id WHERE ia.item_id = ? ORDER BY ia.position", (item,),
    )] == [("Original Author", "original author", None, 0)]
    assert security.current_actor() is None

    item_write.update_item_fields(db, item, {"title": "Maintained in Trash", "authors": "Updated Author"})

    after = dict(db.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone())
    assert after == {**before, "title": "Maintained in Trash", "authors": "Updated Author", "updated_at": after["updated_at"]}
    assert after["updated_at"] != before["updated_at"]
    assert after["id"] == item and after["deleted_at"] == "2026-01-01 00:00:00"
    assert libraries.item_library_id(db, item) == library["id"]
    assert [tuple(row) for row in db.execute(
        "SELECT a.name, a.name_key, ia.role, ia.position FROM item_authors ia "
        "JOIN authors a ON a.id = ia.author_id WHERE ia.item_id = ? ORDER BY ia.position", (item,),
    )] == [("Updated Author", "updated author", None, 0)]
    assert db.execute("SELECT id FROM authors WHERE name_key = 'original author'").fetchone() is None
    for table, rows in retained.items():
        assert [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")] == rows, table
    assert db.execute("SELECT id FROM items_live WHERE id = ?", (item,)).fetchone() is None
    assert db.execute("SELECT id FROM copies_live WHERE item_id = ?", (item,)).fetchone() is None


def test_http_edit_of_trashed_row_refuses_without_database_changes(db, admin_client, admin_user):
    from app.routers import items

    item = _item(db, "HTTP Trash must not be edited", authors="Original HTTP Author")
    user_state.save_state(db, admin_user["id"], item, personal_notes="HTTP private history retained")
    user_state.set_reading_status(db, admin_user["id"], item, "read")
    assert item_write.trash_item(db, item)
    db.commit()
    routes = [route for route in admin_client.app.routes
              if route.path == "/api/items/{item_id}" and "POST" in getattr(route, "methods", ())]
    assert len(routes) == 1 and routes[0].endpoint is items.update_item
    before = _snapshot(db)

    response = admin_client.post(
        f"/api/items/{item}", data={"title": "Forbidden HTTP edit", "authors": "Not reindexed"}, follow_redirects=False,
    )

    assert response.status_code == 404 and response.text == "Not found"
    assert "HTTP Trash must not be edited" not in response.text
    assert _snapshot(db) == before


def test_merge_retains_all_users_history_collection_series_connector_and_release_children(db):
    first = _user(db)
    second = _user(db, "history-second")
    keep = _item(db, "Keeper")
    other = _item(db, "Absorbed")
    user_state.save_state(db, first["id"], other, personal_notes="Private retained")
    user_state.set_reading_status(db, second["id"], other, "read")
    history_id = db.execute("SELECT id FROM user_reading_log WHERE item_id = ?", (other,)).fetchone()[0]
    copy_ids = {r[0] for r in db.execute("SELECT id FROM item_copies WHERE item_id = ?", (other,))}
    collection = db.execute("INSERT INTO collections (name) VALUES ('Retention collection')").lastrowid
    db.execute("INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)", (collection, other))
    db.execute("INSERT INTO item_series (item_id, series_name, position) VALUES (?, 'Secondary saga', 2)", (other,))
    db.execute("INSERT INTO digital_holdings (item_id, provider, external_id) VALUES (?, 'lane-provider', 'plural-identity')", (other,))
    db.execute("INSERT INTO komga_records (komga_id, item_id, library_id, kind) VALUES ('lane-komga', ?, 'native-lib', 'comic')", (other,))
    publication = db.execute("INSERT INTO periodical_publications (title) VALUES ('Retention journal')").lastrowid
    db.execute("INSERT INTO periodical_issues (item_id, publication_id, issue_number) VALUES (?, ?, '7')", (other, publication))
    db.execute("INSERT INTO music_releases (item_id, musicbrainz_release_id) VALUES (?, 'lane-release')", (other,))
    medium = db.execute("INSERT INTO music_media (item_id, position, title) VALUES (?, 1, 'Disc')", (other,)).lastrowid
    track = db.execute("INSERT INTO music_tracks (medium_id, position, title) VALUES (?, 1, 'Retained track')", (medium,)).lastrowid
    item_merge.reparent_children(db, keep, other)
    db.execute("DELETE FROM items WHERE id = ?", (other,))
    assert user_state.get_state(db, first["id"], keep)["personal_notes"] == "Private retained"
    assert user_state.get_state(db, second["id"], keep)["reading_status"] == "read"
    assert db.execute("SELECT item_id FROM user_reading_log WHERE id = ?", (history_id,)).fetchone()[0] == keep
    assert copy_ids <= {r[0] for r in db.execute("SELECT id FROM item_copies WHERE item_id = ?", (keep,))}
    for table in ("collection_items", "item_series", "digital_holdings", "komga_records", "periodical_issues", "music_releases", "music_media"):
        assert db.execute(f"SELECT 1 FROM {table} WHERE item_id = ?", (keep,)).fetchone(), table
    assert db.execute("SELECT medium_id FROM music_tracks WHERE id = ?", (track,)).fetchone()[0] == medium
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("domain", ["personal", "library", "series", "periodical", "release"])
def test_conflicting_merge_identities_refuse_without_any_child_loss(db, domain):
    keep = _item(db, "Kept conflict")
    other = _item(db, "Other conflict")
    if domain == "personal":
        user = _user(db)
        user_state.save_state(db, user["id"], keep, personal_notes="First private value")
        user_state.save_state(db, user["id"], other, personal_notes="Conflicting private value")
    elif domain == "library":
        private = libraries.create_library(db, "Cannot conflate")
        libraries.assign_item(db, other, private["id"])
    elif domain == "series":
        db.execute("INSERT INTO item_series (item_id, series_name, position) VALUES (?, 'Same saga', 1)", (keep,))
        db.execute("INSERT INTO item_series (item_id, series_name, position) VALUES (?, 'Same saga', 2)", (other,))
    elif domain == "periodical":
        pub = db.execute("INSERT INTO periodical_publications (title) VALUES ('Different issues')").lastrowid
        for item, issue in ((keep, "1"), (other, "2")):
            db.execute("INSERT INTO periodical_issues (item_id, publication_id, issue_number) VALUES (?, ?, ?)", (item, pub, issue))
    else:
        for item, release in ((keep, "release-one"), (other, "release-two")):
            db.execute("INSERT INTO music_releases (item_id, musicbrainz_release_id) VALUES (?, ?)", (item, release))
    before = _snapshot(db)
    with pytest.raises(item_write.ItemValueError, match="Cannot merge conflicting") as exc:
        item_merge.reparent_children(db, keep, other)
    assert "private value" not in str(exc.value)
    assert _snapshot(db) == before


def test_trash_listing_counts_restore_and_admin_only_purge_are_library_scoped(db):
    actor = _user(db, role="editor")
    libraries.set_membership(db, 1, actor["id"], "editor")
    private = libraries.create_library(db, "Hidden Trash")
    visible = _item(db, "Visible Trash")
    hidden = _item(db, "Secret Trash", library_id=private["id"])
    for item in (visible, hidden):
        item_write.trash_item(db, item, at="2000-01-01")
    with _acting(db, actor):
        listing = trash.listing(db, expired_only=False, days=180)
        assert [r["id"] for r in listing["items"]] == [visible]
        assert trash.expired_count(db) == 1
        with pytest.raises(HTTPException) as exc:
            item_write.restore_item(db, hidden)
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            trash.purge_item(db, visible)
        assert exc.value.status_code == 403
    admin = _user(db, "purge-admin", "admin")
    with _acting(db, admin):
        assert trash.purge_item(db, visible)
        assert not trash.purge_item(db, visible)
    assert db.execute("SELECT id FROM items WHERE id = ?", (hidden,)).fetchone()


def test_parent_get_db_installs_request_views_on_new_connections(db):
    actor = _user(db)
    libraries.set_membership(db, 1, actor["id"], "viewer")
    visible = _item(db, "Connection visible")
    private = libraries.create_library(db, "Connection hidden")
    _item(db, "Connection secret", library_id=private["id"])
    db.commit()
    token = security.set_actor(actor)
    try:
        with get_db() as request_db:
            assert [r[0] for r in request_db.execute("SELECT id FROM items_live")] == [visible]
    finally:
        security.reset_actor(token)


def test_http_library_editor_with_global_viewer_can_edit_delete_and_restore(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    item = _item(db, "HTTP edit")
    user_state.set_reading_status(db, viewer_user["id"], item, "read")
    history_id = db.execute("SELECT id FROM user_reading_log WHERE item_id = ?", (item,)).fetchone()[0]
    db.commit()
    response = viewer_client.post(f"/api/items/{item}", data={"title": "HTTP library-authorized edit"}, follow_redirects=False)
    assert response.status_code == 303
    assert db.execute("SELECT title FROM items WHERE id = ?", (item,)).fetchone()[0] == "HTTP library-authorized edit"
    assert viewer_client.delete(f"/api/items/{item}").status_code == 200
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (item,)).fetchone()[0] is not None
    assert db.execute("SELECT item_id FROM user_reading_log WHERE id = ?", (history_id,)).fetchone()[0] == item
    assert viewer_client.post(f"/api/trash/items/{item}/restore").status_code == 200
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (item,)).fetchone()[0] is None
    assert viewer_client.delete(f"/api/trash/items/{item}").status_code == 403


def test_http_global_editor_cannot_write_or_restore_hidden_library(db, editor_client, editor_user):
    libraries.set_membership(db, 1, editor_user["id"], "editor")
    private = libraries.create_library(db, "HTTP boundary")
    hidden = _item(db, "HTTP secret title", library_id=private["id"])
    item_write.trash_item(db, hidden)
    db.commit()
    response = editor_client.post(f"/api/trash/items/{hidden}/restore")
    assert response.status_code == 404
    assert "HTTP secret title" not in response.text
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (hidden,)).fetchone()[0] is not None
    response = editor_client.delete(f"/api/items/{hidden}")
    assert response.status_code == 404
    assert "HTTP secret title" not in response.text


def test_http_merge_refusal_preserves_private_history_and_both_identities(db, admin_client):
    user = _user(db, "HTTP-private-owner")
    keep = _item(db, "HTTP keep")
    other = _item(db, "HTTP other")
    for item, note in ((keep, "PRIVATE FIRST"), (other, "PRIVATE SECOND")):
        user_state.save_state(db, user["id"], item, personal_notes=note)
        user_state.set_reading_status(db, user["id"], item, "read")
    states = [tuple(r) for r in db.execute("SELECT * FROM user_item_state ORDER BY item_id")]
    history = [tuple(r) for r in db.execute("SELECT * FROM user_reading_log ORDER BY id")]
    db.commit()
    response = admin_client.post("/api/items/merge", json={"keep_id": keep, "merge_ids": [other]})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "PRIVATE FIRST" not in response.text and "PRIVATE SECOND" not in response.text
    assert [tuple(r) for r in db.execute("SELECT * FROM user_item_state ORDER BY item_id")] == states
    assert [tuple(r) for r in db.execute("SELECT * FROM user_reading_log ORDER BY id")] == history
    assert {r[0] for r in db.execute("SELECT id FROM items")} == {keep, other}


def test_canonical_complementary_release_transfers_unique_identity_and_retains_collector_history(db):
    keep, source = _complementary_release_items(db)
    owner = _user(db, "release-private-owner")
    user_state.save_state(db, owner["id"], source, personal_notes="Original source private notes")
    user_state.set_reading_status(db, owner["id"], source, "read")
    history_id = db.execute("SELECT id FROM user_reading_log WHERE item_id = ?", (source,)).fetchone()[0]
    medium = db.execute("INSERT INTO music_media (item_id, position) VALUES (?, 1)", (source,)).lastrowid
    track = db.execute("INSERT INTO music_tracks (medium_id, position, title) VALUES (?, 1, 'Retained track')", (medium,)).lastrowid
    copies = {row[0] for row in db.execute("SELECT id FROM item_copies")}
    # Negative control on the REAL central schema, not the old reduced fixture:
    # the source still owns this global UNIQUE slot before the merge.
    with pytest.raises(sqlite3.IntegrityError, match="musicbrainz_release_id"):
        db.execute(
            "UPDATE music_releases SET musicbrainz_release_id = 'complementary-release-identity' WHERE item_id = ?",
            (keep,),
        )
    item_merge.reparent_children(db, keep, source)
    db.execute("DELETE FROM items WHERE id = ?", (source,))
    release = db.execute("SELECT * FROM music_releases WHERE item_id = ?", (keep,)).fetchone()
    assert release["musicbrainz_release_id"] == "complementary-release-identity"
    assert (release["edition_notes"], release["media_condition"], release["media_count"]) == ("Collector numbered copy", "good", 0)
    assert (release["artist_credit"], release["catalog_number"]) == ("Source Artist", "SOURCE-001")
    assert db.execute("SELECT 1 FROM music_releases WHERE item_id = ?", (source,)).fetchone() is None
    assert user_state.get_state(db, owner["id"], keep)["personal_notes"] == "Original source private notes"
    assert db.execute("SELECT item_id FROM user_reading_log WHERE id = ?", (history_id,)).fetchone()[0] == keep
    assert db.execute("SELECT item_id FROM music_media WHERE id = ?", (medium,)).fetchone()[0] == keep
    assert db.execute("SELECT medium_id FROM music_tracks WHERE id = ?", (track,)).fetchone()[0] == medium
    assert {row[0] for row in db.execute("SELECT id FROM item_copies")} == copies
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_canonical_later_release_merge_refusal_restores_unique_identity_and_all_private_children(db):
    keep, source = _complementary_release_items(db)
    owner = _user(db, "late-release-private-owner")
    user_state.save_state(db, owner["id"], source, personal_notes="Private source retained on rollback")
    user_state.set_reading_status(db, owner["id"], source, "read")
    for item, description in ((keep, "Keeper identifier metadata"), (source, "Unequal source metadata")):
        db.execute(
            "INSERT INTO music_identifiers (item_id, identifier_type, value, description) VALUES (?, 'catalog', 'SAME-KEY', ?)",
            (item, description),
        )
    before = _snapshot(db)
    # music_identifiers is processed AFTER the complementary release transfer.
    with pytest.raises(item_write.ItemValueError, match="Cannot merge conflicting music_identifiers") as exc:
        item_merge.reparent_children(db, keep, source)
    assert "Private source" not in str(exc.value) and "Unequal source" not in str(exc.value)
    assert _snapshot(db) == before
    assert db.execute("SELECT musicbrainz_release_id FROM music_releases WHERE item_id = ?", (keep,)).fetchone()[0] is None
    assert db.execute("SELECT musicbrainz_release_id FROM music_releases WHERE item_id = ?", (source,)).fetchone()[0] == "complementary-release-identity"
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_purge_captures_only_missing_scan_library_and_never_reassigns_original_scan_user(db):
    owner = _user(db, "scan-original-owner")
    admin = _user(db, "scan-purging-admin", "admin")
    original = libraries.create_library(db, "Original scan library")
    item = _item(db, "Later Main Library assignment")
    saved = db.execute(
        "INSERT INTO scan_log (isbn, media_type, result, item_id, mode, security_library_id, security_user_id) "
        "VALUES ('SAVED-SCAN', 'book', 'duplicate', ?, 'add', ?, ?)",
        (item, original["id"], owner["id"]),
    ).lastrowid
    bound = db.execute(
        "INSERT INTO scan_log (isbn, media_type, result, item_id, mode) VALUES ('BOUND-UNKNOWN-USER', 'book', 'duplicate', ?, 'add')",
        (item,),
    ).lastrowid
    orphan = db.execute(
        "INSERT INTO scan_log (isbn, media_type, result, mode) VALUES ('UNKNOWN-ORPHAN', 'book', 'error', 'add')"
    ).lastrowid
    raw = [tuple(row) for row in db.execute("SELECT id, isbn, result, mode, created_at FROM scan_log ORDER BY id")]
    item_write.trash_item(db, item)
    with _acting(db, admin):
        assert trash.purge_item(db, item)
    assert [tuple(row) for row in db.execute("SELECT id, isbn, result, mode, created_at FROM scan_log ORDER BY id")] == raw
    scopes = {row["id"]: tuple(row)[1:] for row in db.execute("SELECT id, item_id, security_library_id, security_user_id FROM scan_log")}
    assert scopes[saved] == (None, original["id"], owner["id"])
    assert scopes[bound] == (None, 1, None)
    assert scopes[orphan] == (None, None, None)
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_http_private_scan_history_stays_scoped_before_trash_during_trash_and_after_admin_purge(db, client, admin_user):
    from app.services import scan_history

    owner = _user(db, "http-private-scanner", "editor")
    peer = _user(db, "http-other-library-editor", "editor")
    colleague = _user(db, "http-authorized-library-reader", "viewer")
    private = libraries.create_library(db, "HTTP private scan history")
    libraries.set_membership(db, private["id"], owner["id"], "editor")
    libraries.set_membership(db, private["id"], colleague["id"], "editor")
    libraries.set_membership(db, 1, peer["id"], "editor")
    code = "9780000000118"
    title = "PRIVATE SCANNED TITLE"
    item = _item(db, title, isbn=code, library_id=private["id"])
    # A centrally initialized database uses the real writer. Historical logs
    # receive this same bound-only snapshot during operations119–121; separate
    # old-manifest migration tests exercise that transition without lazy repair.
    scan_history.record_scan(db, "BOUND-PRIVATE-LEGACY", "book", "duplicate", item)
    legacy = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    orphan = db.execute("INSERT INTO scan_log (isbn, media_type, result, mode) VALUES ('UNKNOWN-PRIVATE-ORPHAN', 'book', 'error', 'add')").lastrowid
    db.commit()
    scan = _request_as(client, owner, "POST", "/api/scan", data={"isbn": code, "media_type": "book", "mode": "add"})
    assert scan.status_code == 200 and scan.context["status"] == "duplicate"
    recorded = db.execute("SELECT * FROM scan_log WHERE isbn = ? ORDER BY id DESC LIMIT 1", (code,)).fetchone()
    scan_id = recorded["id"]
    assert recorded["security_library_id"] == private["id"]
    assert recorded["security_user_id"] == owner["id"]
    raw = [tuple(row) for row in db.execute("SELECT id, isbn, result, mode, created_at FROM scan_log ORDER BY id")]

    def assert_history_visibility():
        hidden = _request_as(client, peer, "GET", "/api/recent-scans?mode=add")
        assert hidden.status_code == 200
        assert not {scan_id, legacy, orphan} & {row["id"] for row in hidden.context["recent_scans"]}
        assert all(value not in hidden.text for value in (code, title, "BOUND-PRIVATE-LEGACY", "UNKNOWN-PRIVATE-ORPHAN"))
        for authorized in (owner, colleague):
            visible = _request_as(client, authorized, "GET", "/api/recent-scans?mode=add")
            assert visible.status_code == 200
            ids = {row["id"] for row in visible.context["recent_scans"]}
            assert {scan_id, legacy} <= ids and orphan not in ids
            assert "UNKNOWN-PRIVATE-ORPHAN" not in visible.text

    assert_history_visibility()
    assert _request_as(client, owner, "DELETE", f"/api/items/{item}").status_code == 200
    assert_history_visibility()
    # Model a legacy bound log without its library snapshot. Purge must capture
    # the binding before unlinking, while preserving the ORIGINAL scan user.
    db.execute("UPDATE scan_log SET security_library_id = NULL WHERE id = ?", (scan_id,))
    db.commit()
    assert _request_as(client, admin_user, "DELETE", f"/api/trash/items/{item}").status_code == 200
    assert db.execute("SELECT 1 FROM items WHERE id = ?", (item,)).fetchone() is None
    scopes = {row["id"]: tuple(row)[1:] for row in db.execute("SELECT id, item_id, security_library_id, security_user_id FROM scan_log")}
    assert scopes[scan_id] == (None, private["id"], owner["id"])
    assert scopes[legacy] == (None, private["id"], None)
    assert scopes[orphan] == (None, None, None)
    assert [tuple(row) for row in db.execute("SELECT id, isbn, result, mode, created_at FROM scan_log ORDER BY id")] == raw
    assert_history_visibility()


def test_http_merge_promotes_unowned_keeper_and_retains_exact_source_copy_ids_without_placeholder(db, admin_client):
    keep = _item(db, "HTTP unowned physical keeper", owned=0, wishlisted=True)
    source = _item(db, "HTTP owned physical source", owned=1)
    copy_id = db.execute("SELECT id FROM item_copies WHERE item_id = ?", (source,)).fetchone()[0]
    item_copies.update_copy(db, copy_id, {"copy_barcode": "HTTP-MERGE-ORIGINAL", "provenance": "Original physical acquisition"})
    db.commit()
    response = admin_client.post("/api/items/merge", json={"keep_id": keep, "merge_ids": [source]})
    assert response.status_code == 200 and response.json() == {"ok": True, "merged": 1}
    assert db.execute("SELECT owned FROM items WHERE id = ?", (keep,)).fetchone()[0] == 1
    assert db.execute("SELECT 1 FROM items WHERE id = ?", (source,)).fetchone() is None
    copies = db.execute("SELECT id, item_id, copy_barcode, provenance FROM item_copies ORDER BY id").fetchall()
    assert [tuple(row) for row in copies] == [(copy_id, keep, "HTTP-MERGE-ORIGINAL", "Original physical acquisition")]
    assert not lists.is_member(db, lists.WISHLIST, keep)
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_http_later_batch_merge_conflict_rolls_back_promotion_unique_release_and_every_target_generically(db, admin_client):
    keep = _item(db, "HTTP batch keeper", owned=0, wishlisted=True)
    first = _item(db, "HTTP first owned source", owned=1)
    second = _item(db, "HTTP second conflicting source", owned=1)
    owner = _user(db, "HTTP-batch-private-owner")
    for item, note in ((keep, "KEEPER PRIVATE VALUE"), (second, "SOURCE PRIVATE CONFLICT")):
        user_state.save_state(db, owner["id"], item, personal_notes=note)
        user_state.set_reading_status(db, owner["id"], item, "read")
    db.execute("INSERT INTO music_releases (item_id, edition_notes, media_count) VALUES (?, 'Retained collector note', 0)", (keep,))
    db.execute("INSERT INTO music_releases (item_id, musicbrainz_release_id) VALUES (?, 'http-batch-release')", (first,))
    tables = ("items", "library_items", "item_copies", "user_item_state", "user_reading_log", "music_releases", "list_items", "scan_log")
    before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}
    db.commit()
    response = admin_client.post("/api/items/merge", json={"keep_id": keep, "merge_ids": [first, second]})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "Merge conflicts with existing catalogue data", "merged": 0}
    assert "KEEPER PRIVATE VALUE" not in response.text and "SOURCE PRIVATE CONFLICT" not in response.text
    for table in tables:
        assert [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")] == before[table], table
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
