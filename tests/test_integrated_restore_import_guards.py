"""Integrated import/restore boundaries; no provider/network fixtures.

Run after the parent freezes the candidate. These are new obligations, not
replacements for the two parents' archive, CSV, lifecycle or backup suites.
"""

import io
import json
import sqlite3
import zipfile

import pytest

from tests.conftest import _insert_item, _insert_personal_history, _seed_personal_state


def _post_csv(client, content, mode="skip", **fields):
    response = client.post(
        "/api/import/csv",
        files={"file": ("rows.csv", content.encode(), "text/csv")},
        data={"mode": mode, **fields},
    )
    assert response.status_code == 200
    return response.json()


def _archive_bytes(library, version=3):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": "shelf-archive", "version": version}))
        archive.writestr("library.json", json.dumps(library))
    return output.getvalue()


def _post_archive(client, library, mode="skip", version=3):
    response = client.post(
        "/api/import/archive",
        files={"file": ("collection.zip", _archive_bytes(library, version), "application/zip")},
        data={"mode": mode},
    )
    assert response.status_code == 200
    return response.json()


def _dump(db):
    return "\n".join(db.iterdump())


@pytest.fixture
def destination_restore_keys():
    """Key initialization is setup, NEVER a side effect of the restore route."""
    from app.auth import get_secret_key
    from app.crypto import get_encryption_key

    return get_encryption_key(), get_secret_key()


def test_csv_failed_row_rolls_back_projections_and_allows_same_key_retry(admin_client, db, monkeypatch):
    from app.routers import items_csv

    attach = items_csv.tags_svc.attach_tags
    calls = 0

    def fail_after_tags(conn, item_id, tags):
        nonlocal calls
        attach(conn, item_id, tags)
        calls += 1
        if calls == 1:
            raise RuntimeError("injected failure after tag mutation")

    monkeypatch.setattr(items_csv.tags_svc, "attach_tags", fail_after_tags)
    row = "Atomic row,,9780441013593,book,Atomic shelf,Atomic tag\n"
    result = _post_csv(admin_client, "title,authors,isbn,media_type,location,tags\n" + row + row)
    assert (result["imported"], result["skipped"], result["error_count"]) == (1, 0, 1)
    assert result["locations_created"] == ["Atomic shelf"]
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM item_tags").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM locations WHERE name='Atomic shelf'").fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_csv_failed_update_reverts_restore_and_earlier_metadata(admin_client, db, monkeypatch):
    from app.routers import items_csv
    from app.services import item_write

    item_id = _insert_item(db, title="Still trashed", isbn="9780441013593", publisher="Original")
    item_write.trash_item(db, item_id, at="2026-01-02 03:04:05")
    db.commit()
    before = _dump(db)
    update = items_csv.csv_import.update_from_row

    def fail_after_update(conn, *args, **kwargs):
        update(conn, *args, **kwargs)
        raise RuntimeError("injected failure after restored metadata")

    monkeypatch.setattr(items_csv.csv_import, "update_from_row", fail_after_update)
    result = _post_csv(
        admin_client,
        "title,isbn,media_type,publisher,location\nIncoming,9780441013593,book,Changed,Failed shelf\n",
        mode="update",
    )
    assert (result["imported"], result["restored"], result["error_count"]) == (0, 0, 1)
    assert result["locations_created"] == []
    assert _dump(db) == before


def test_tracker_wishlist_and_status_belong_only_to_importer(admin_client, admin_user, viewer_user, db):
    result = _post_csv(
        admin_client,
        "Title,ISBN/UID,Format,Read Status,Owned?\nPersonal tracker,9780441013593,paperback,to-read,No\n",
        to_read_wishlist="1",
    )
    assert result["errors"] == []
    item = db.execute("SELECT id, reading_status, date_finished FROM items").fetchone()
    assert item["reading_status"] is None and item["date_finished"] is None
    personal = db.execute(
        "SELECT reading_status, wishlist FROM user_item_state WHERE user_id=? AND item_id=?",
        (admin_user["id"], item["id"]),
    ).fetchone()
    assert tuple(personal) == ("want_to_read", 1)
    assert db.execute("SELECT 1 FROM list_items WHERE item_id=?", (item["id"],)).fetchone() is None
    assert db.execute("SELECT 1 FROM user_item_state WHERE user_id=?", (viewer_user["id"],)).fetchone() is None


@pytest.mark.parametrize("deleted", [False, True])
def test_csv_legacy_identity_is_not_a_book_twin(admin_client, db, deleted):
    from app.services import item_write

    legacy = _insert_item(db, title="Legacy", isbn="9780441013593", media_type="kids_book")
    modern = _insert_item(db, title="Modern", isbn="9780441013593", media_type="book", publisher="Keep")
    if deleted:
        item_write.trash_item(db, legacy)
    db.commit()
    result = _post_csv(
        admin_client, "title,isbn,media_type,publisher\nLegacy,9780441013593,kids_book,Updated\n", mode="update",
    )
    assert result["errors"] == []
    assert result["restored"] == int(deleted)
    assert db.execute("SELECT media_type, publisher, deleted_at FROM items WHERE id=?", (legacy,)).fetchone()[:] == (
        "kids_book", "Updated", None,
    )
    assert db.execute("SELECT title, publisher FROM items WHERE id=?", (modern,)).fetchone()[:] == ("Modern", "Keep")


def test_archive_export_uses_actor_state_even_for_trash_and_excludes_hidden_history(db, editor_user, viewer_user):
    from app.services import archive, catalogue_security, item_write, libraries

    visible = _insert_item(db, title="Visible trash", isbn=None, reading_status="read")
    private_library = libraries.create_library(db, "Private archive library")["id"]
    hidden = _insert_item(db, title="Hidden archive title", isbn=None, _library_id=private_library)
    _seed_personal_state(db, editor_user["id"], visible, reading_status="reading", personal_notes="mine")
    _seed_personal_state(db, viewer_user["id"], visible, reading_status="read", personal_notes="not mine")
    _insert_personal_history(db, editor_user["id"], visible, notes="own history")
    _insert_personal_history(db, viewer_user["id"], visible, notes="another user's history")
    _insert_personal_history(db, editor_user["id"], hidden, notes="hidden item history")
    item_write.trash_item(db, visible)
    db.commit()
    token = catalogue_security.set_actor(editor_user)
    try:
        items, id_map, _covers = archive._build_items(db)
        history = archive._fetch_reading_log(db, id_map)
    finally:
        catalogue_security.reset_actor(token)
    assert [item["title"] for item in items] == ["Visible trash"]
    assert items[0]["reading_status"] == "reading"
    assert items[0]["personal_state"]["personal_notes"] == "mine"
    assert [row["notes"] for row in history] == ["own history"]


def test_csv_export_filters_physical_trash_by_library(editor_client, editor_user, viewer_client, viewer_user, db):
    from app.auth import create_token
    from app.services import item_write, libraries

    visible = _insert_item(db, title="CSV visible trash", isbn=None)
    private_library = libraries.create_library(db, "CSV hidden library")["id"]
    _insert_item(db, title="CSV hidden live", isbn=None, _library_id=private_library)
    hidden_trash = _insert_item(db, title="CSV hidden trash", isbn=None, _library_id=private_library)
    readonly_library = libraries.create_library(db, "CSV read-only library")["id"]
    libraries.set_membership(db, readonly_library, editor_user["id"], "viewer")
    _insert_item(db, title="CSV read-only live", isbn=None, _library_id=readonly_library)
    readonly_trash = _insert_item(db, title="CSV read-only trash", isbn=None, _library_id=readonly_library)
    item_write.trash_item(db, visible)
    item_write.trash_item(db, hidden_trash)
    item_write.trash_item(db, readonly_trash)
    db.commit()
    # These named fixtures deliberately share the same native client. Select
    # each real session explicitly rather than accidentally exporting as viewer.
    editor_client.cookies.set("access_token", create_token(
        editor_user["id"], editor_user["username"], editor_user["role"], editor_user["display_name"],
    ))
    response = editor_client.get("/api/export/csv")
    assert response.status_code == 200
    assert "CSV visible trash" in response.text
    assert "CSV hidden" not in response.text
    assert "CSV read-only live" in response.text
    assert "CSV read-only trash" not in response.text
    viewer_client.cookies.set("access_token", create_token(
        viewer_user["id"], viewer_user["username"], viewer_user["role"], viewer_user["display_name"],
    ))
    viewer_export = viewer_client.get("/api/export/csv")
    assert viewer_export.status_code == 200
    assert "CSV visible trash" not in viewer_export.text


def test_csv_export_uses_library_editor_authority_not_global_viewer_role(viewer_client, viewer_user, db):
    from app.services import item_write, libraries

    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    readonly = libraries.create_library(db, "Viewer's read-only export library")["id"]
    libraries.set_membership(db, readonly, viewer_user["id"], "viewer")
    allowed = _insert_item(db, title="Editable-library trashed export", isbn=None)
    denied = _insert_item(db, title="Read-only-library trashed export", isbn=None, _library_id=readonly)
    item_write.trash_item(db, allowed)
    item_write.trash_item(db, denied)
    db.commit()
    response = viewer_client.get("/api/export/csv")
    assert response.status_code == 200
    assert "Editable-library trashed export" in response.text
    assert "Read-only-library trashed export" not in response.text


@pytest.mark.parametrize("deleted", [False, True])
def test_archive_legacy_update_or_restore_preserves_type_and_copy_identity(admin_client, db, deleted):
    from app.services import item_copies, item_write

    legacy = _insert_item(db, title="Legacy archive", isbn="9780441013593", media_type="kids_book", publisher="Original")
    modern = _insert_item(db, title="Distinct book twin", isbn="9780441013593", media_type="book", publisher="Keep")
    copy_id = item_copies.insert_copy(db, {
        "item_id": legacy, "copy_number": 3, "is_primary": 1, "copy_barcode": "legacy-stable-copy",
    })
    if deleted:
        item_write.trash_item(db, legacy)
    db.commit()
    result = _post_archive(admin_client, {"items": [{
        "id": 1, "title": "Legacy incoming", "isbn": "9780441013593", "media_type": "kids_book",
        "publisher": "Updated", "owned": 1, "copies": [],
    }]}, mode="update")
    assert result["errors"] == []
    assert result["restored"] == int(deleted)
    row = db.execute("SELECT media_type,publisher,deleted_at FROM items WHERE id=?", (legacy,)).fetchone()
    assert row[:] == ("kids_book", "Original" if deleted else "Updated", None)
    assert db.execute("SELECT publisher FROM items WHERE id=?", (modern,)).fetchone()[0] == "Keep"
    assert db.execute("SELECT item_id,copy_number,copy_barcode FROM item_copies WHERE id=?", (copy_id,)).fetchone()[:] == (
        legacy, 3, "legacy-stable-copy",
    )


def test_archive_copy_export_and_import_preserve_node_path_not_flat_integer(admin_client, db):
    from app.services import archive, item_copies, location_tree, locations

    db.execute("INSERT INTO locations(id,name) VALUES(71,'Flat binding')")
    db.execute("INSERT INTO location_nodes(id,name) VALUES(71,'Unrelated integer twin')")
    root = location_tree.legacy_location_to_node(db, 71, create=True)
    child = locations.create_location(db, "Precise child", parent_id=root, sort_order=9)
    source = _insert_item(db, title="Source node item", isbn=None, location_id=71)
    copy_id = item_copies.insert_copy(db, {
        "item_id": source, "copy_number": 4, "location_id": child, "is_primary": 1,
        "copy_barcode": "source-copy", "position_order": 17, "missing_since": "2026-01-01",
        "acquired_date": "2025-02-03", "acquisition_source": "Local shop", "provenance": "First owner",
    })
    exported = archive._copies_by_item(db)[source][0]
    assert exported["location"] == "Flat binding / Precise child"
    assert exported["source_copy_id"] == copy_id
    assert [part["name"] for part in exported["location_path"]] == ["Flat binding", "Precise child"]
    assert "missing_since" not in exported
    exported["copy_barcode"] = "incoming-copy"
    db.commit()
    result = _post_archive(admin_client, {"items": [{
        "id": 1, "title": "Imported node item", "media_type": "book", "owned": 1,
        "location": "Flat binding", "copies": [exported],
    }]})
    assert result["errors"] == []
    incoming = db.execute("SELECT id FROM items WHERE title='Imported node item'").fetchone()[0]
    copy = db.execute("SELECT * FROM item_copies WHERE item_id=?", (incoming,)).fetchone()
    assert (copy["location_id"], copy["copy_number"], copy["position_order"]) == (child, 4, 17)
    assert (copy["missing_since"], copy["acquisition_source"], copy["provenance"]) == (
        None, "Local shop", "First owner",
    )
    assert db.execute("SELECT location_id, missing_since FROM item_copies WHERE id=?", (copy_id,)).fetchone()[:] == (
        child, "2026-01-01",
    )


def test_old_archive_copy_flat_location_explicitly_converts_to_node(admin_client, db):
    from app.services import item_write

    db.execute("INSERT INTO locations(id,name) VALUES(81,'Old archive shelf')")
    db.execute("INSERT INTO location_nodes(id,name) VALUES(81,'Unrelated old integer')")
    db.commit()
    result = _post_archive(admin_client, {"items": [{
        "id": 1, "title": "Old copy format", "media_type": "book", "owned": 1,
        "copies": [{"copy_number": 1, "is_primary": 1, "location": "Old archive shelf", "position_order": 17}],
    }]}, version=2)
    assert result["errors"] == []
    row = db.execute(
        "SELECT c.id,c.item_id,c.location_id,c.position_order,n.legacy_location_id FROM item_copies c "
        "JOIN location_nodes n ON n.id=c.location_id JOIN items i ON i.id=c.item_id "
        "WHERE i.title='Old copy format'",
    ).fetchone()
    assert row["location_id"] != 81
    assert row["legacy_location_id"] == 81
    assert row["position_order"] == 17
    assert db.execute("SELECT location_id FROM items WHERE id=?", (row["item_id"],)).fetchone()[0] == 81
    item_write.update_item_fields(db, row["item_id"], {"publisher": "Metadata only"})
    after = db.execute("SELECT id,location_id,position_order FROM item_copies WHERE item_id=?", (row["item_id"],)).fetchall()
    assert [tuple(copy) for copy in after] == [(row["id"], row["location_id"], 17)]
    assert db.execute("SELECT location_id FROM items WHERE id=?", (row["item_id"],)).fetchone()[0] == 81


def test_archive_copy_failure_rolls_back_item_tags_and_locations(admin_client, db, monkeypatch):
    from app.services import archive

    insert = archive.item_copies.insert_copy

    def fail_second(conn, fields, **kwargs):
        value = insert(conn, fields, **kwargs)
        if fields["copy_number"] == 2:
            raise RuntimeError("injected second-copy failure")
        return value

    monkeypatch.setattr(archive.item_copies, "insert_copy", fail_second)
    before = _dump(db)
    result = _post_archive(admin_client, {"items": [{
        "id": 1, "title": "Rollback archive item", "media_type": "book", "owned": 1,
        "location": "Rollback archive shelf", "tags": ["Rollback archive tag"],
        "copies": [{"copy_number": 1, "is_primary": 1}, {"copy_number": 2}],
    }]})
    assert result["imported"] == 0 and len(result["errors"]) == 1
    assert _dump(db) == before


def test_archive_history_is_owned_by_importer_not_legacy_global_seed(admin_client, admin_user, viewer_user, db):
    result = _post_archive(admin_client, {
        "items": [{"id": 1, "title": "Private archive import", "media_type": "book", "owned": 1,
                   "reading_status": "read", "personal_state": {"wishlist": 1, "rating": 4}}],
        "reading_log": [{"item_id": 1, "status": "read", "notes": "Imported personal completion"}],
    })
    assert result["errors"] == []
    assert db.execute("SELECT reading_status FROM items").fetchone()[0] is None
    assert db.execute("SELECT COUNT(*) FROM reading_log").fetchone()[0] == 0
    history = db.execute("SELECT user_id,notes FROM user_reading_log").fetchall()
    assert [tuple(row) for row in history] == [(admin_user["id"], "Imported personal completion")]
    assert db.execute("SELECT 1 FROM user_item_state WHERE user_id=?", (viewer_user["id"],)).fetchone() is None


def test_archive_authorizes_all_targets_before_first_mutation(db, editor_user):
    from types import SimpleNamespace
    from app.services import archive, catalogue_security, libraries

    visible = _insert_item(db, title="Visible preflight", isbn="9780441013593", publisher="Original")
    private_library = libraries.create_library(db, "Archive preflight private")["id"]
    hidden = _insert_item(db, title="Undisclosed blocker", isbn="9780553283686", _library_id=private_library)
    db.commit()
    reader = SimpleNamespace(library={"items": [
        {"id": 1, "title": "Visible incoming", "isbn": "9780441013593", "publisher": "Changed"},
        {"id": 2, "title": "User supplied", "isbn": "9780553283686"},
    ], "tags": [{"name": "Must not be seeded"}]}, cover_names=set())
    plan = {"mode": "update", "items": [
        {"ref": 1, "verdict": "update"}, {"ref": 2, "verdict": "update"},
    ]}
    before = _dump(db)
    token = catalogue_security.set_actor(editor_user)
    try:
        with pytest.raises(archive.ArchiveError, match="unavailable or not editable") as refused:
            archive.apply_plan(db, reader, plan)
    finally:
        catalogue_security.reset_actor(token)
    assert "Undisclosed" not in str(refused.value)
    assert str(hidden) not in str(refused.value)
    assert _dump(db) == before
    assert db.execute("SELECT publisher FROM items WHERE id=?", (visible,)).fetchone()[0] == "Original"


def test_archive_bearer_viewer_cap_cannot_be_lifted_by_admin_identity(db, admin_user):
    from types import SimpleNamespace
    from app.services import archive, catalogue_security

    item_id = _insert_item(db, title="Bearer cap target", isbn="9780441013593", publisher="Original")
    db.commit()
    reader = SimpleNamespace(library={"items": [{"id": 1, "title": "Bearer cap target",
        "isbn": "9780441013593", "publisher": "Changed"}]}, cover_names=set())
    principal = {**admin_user, "role": "viewer", "account_role": "admin", "api_token_id": 12345}
    token = catalogue_security.set_actor(principal)
    try:
        plan = archive.plan_archive(db, reader, mode="update")
        with pytest.raises(archive.ArchiveError, match="unavailable or not editable"):
            archive.apply_plan(db, reader, plan)
    finally:
        catalogue_security.reset_actor(token)
    assert db.execute("SELECT publisher FROM items WHERE id=?", (item_id,)).fetchone()[0] == "Original"


@pytest.mark.parametrize("stored_media", ["kids_book", "book"])
def test_archive_alias_classification_never_uses_hidden_legacy_or_canonical_identity(db, editor_user, stored_media):
    from types import SimpleNamespace
    from app.services import archive, catalogue_security, libraries

    private = libraries.create_library(db, "Hidden alias identity")["id"]
    visible = _insert_item(db, title="Visible alias batch", isbn="9780553283686", publisher="Original")
    hidden = _insert_item(db, title="PRIVATE ALIAS BLOCKER", isbn="9780441013593",
                          media_type=stored_media, _library_id=private)
    db.commit()
    before = _dump(db)
    reader = SimpleNamespace(library={"items": [
        {"id": "public-ref", "title": "Visible incoming", "isbn": "9780553283686", "publisher": "Changed"},
        {"id": "alias-ref", "title": "Submitted alias", "isbn": "9780441013593", "media_type": "kids_book"},
    ], "tags": [{"name": "Must not be seeded by hidden alias batch"}]}, cover_names=set())
    token = catalogue_security.set_actor(editor_user)
    try:
        plan = archive.plan_archive(db, reader, mode="update")
        # Planning reports per-record errors, never a hidden target/verdict.
        assert plan["items"] == [{"ref": "public-ref", "title": "Visible incoming",
                                  "verdict": "update", "basis": "isbn", "cover": "none", "deleted": False}]
        assert plan["summary"]["errors"] == [
            "Archive item alias-ref: Archive item is unavailable or not editable",
        ]
        assert str(hidden) not in " ".join(plan["summary"]["errors"])
        assert "PRIVATE ALIAS BLOCKER" not in json.dumps(plan)
        assert _dump(db) == before
        # A selected hidden record must also fail the COMPLETE apply preflight,
        # before the earlier visible update or global tag can mutate anything.
        selected = {"mode": "update", "items": [
            {"ref": "public-ref", "verdict": "update"}, {"ref": "alias-ref", "verdict": "update"},
        ]}
        with pytest.raises(archive.ArchiveError, match="unavailable or not editable") as refusal:
            archive.apply_plan(db, reader, selected)
    finally:
        catalogue_security.reset_actor(token)
    assert "PRIVATE ALIAS BLOCKER" not in str(refusal.value)
    assert str(hidden) not in str(refusal.value)
    assert _dump(db) == before
    assert db.execute("SELECT publisher FROM items WHERE id=?", (visible,)).fetchone()[0] == "Original"


@pytest.mark.parametrize("fault", ["unknown_history", "mixed_history", "view", "trigger"])
def test_restore_refusal_keeps_active_database_byte_logically_unchanged(
    admin_client, db, tmp_path, fault, destination_restore_keys,
):
    backup = admin_client.get("/api/settings/backup")
    assert backup.status_code == 200
    uploaded = tmp_path / "refused.db"
    uploaded.write_bytes(backup.content)
    with sqlite3.connect(uploaded) as staged:
        if fault == "unknown_history":
            staged.execute("INSERT INTO schema_version(version,description) VALUES(999999,'Unknown origin')")
        elif fault == "mixed_history":
            staged.execute("UPDATE schema_version SET description='Foreign published identity' WHERE version=24")
        elif fault == "view":
            staged.execute("CREATE VIEW hostile_view AS SELECT * FROM users")
        else:
            staged.execute("CREATE TRIGGER hostile_trigger AFTER UPDATE ON users BEGIN DELETE FROM items; END")
    before = _dump(db)
    response = admin_client.post(
        "/api/settings/restore", files={"file": ("refused.db", uploaded.read_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert _dump(db) == before


def test_staged_migration_failure_cannot_replace_active_or_invalidate_sessions(
    admin_client, db, monkeypatch, destination_restore_keys,
):
    from app import database

    backup = admin_client.get("/api/settings/backup")
    assert backup.status_code == 200
    before = _dump(db)

    def failing_runner(staged):
        staged.execute("UPDATE users SET token_version=token_version+500")
        raise database.SchemaLineageError("injected staged migration refusal")

    monkeypatch.setattr(database, "validate_and_migrate_staged_connection", failing_runner)
    response = admin_client.post(
        "/api/settings/restore", files={"file": ("backup.db", backup.content, "application/octet-stream")},
    )
    assert response.json()["ok"] is False
    assert _dump(db) == before


def test_restore_invalidates_cookie_and_api_tokens_before_live_copy(
    admin_client, admin_user, db, monkeypatch, destination_restore_keys,
):
    from app.auth import TOKEN_VERSION_HIGH_WATER
    from app.routers import settings
    from app.services import api_tokens

    api_tokens.create(db, admin_user["id"], "synthetic test capability")
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (TOKEN_VERSION_HIGH_WATER, "700"))
    db.commit()
    backup = admin_client.get("/api/settings/backup")
    assert backup.status_code == 200
    _insert_item(db, title="After snapshot marker", isbn=None)
    db.commit()
    invalidate = settings._invalidate_restored_tokens
    observed = []

    def inspect_stage(staged, live_max):
        from app.database import get_db

        value = invalidate(staged, live_max)
        observed.append(staged.execute("SELECT MIN(token_version) FROM users").fetchone()[0])
        assert staged.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 0
        # These MUST still be live when invalidation finishes in staging. Moving
        # the invalidation after the copy makes this assertion fail.
        with get_db() as live:
            assert live.execute("SELECT 1 FROM items WHERE title='After snapshot marker'").fetchone() is not None
            assert live.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 1
        return value

    monkeypatch.setattr(settings, "_invalidate_restored_tokens", inspect_stage)
    response = admin_client.post(
        "/api/settings/restore", files={"file": ("backup.db", backup.content, "application/octet-stream")},
    )
    assert response.json()["ok"] is True, response.json()
    assert observed == [701]
    assert db.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 0
    assert db.execute("SELECT 1 FROM items WHERE title='After snapshot marker'").fetchone() is None
    assert list(settings.DATA_DIR.glob("shelf_pre_restore_*.db"))


def test_restore_refuses_concurrent_cookie_generation_collision_before_commit(
    admin_client, db, monkeypatch, destination_restore_keys,
):
    from app.routers import settings

    backup = admin_client.get("/api/settings/backup")
    assert backup.status_code == 200
    marker = _insert_item(db, title="Must survive restore race refusal", isbn=None)
    db.commit()
    invalidate = settings._invalidate_restored_tokens
    simulated = []

    def concurrent_password_change(staged, live_max):
        from app.database import get_db

        invalidate(staged, live_max)
        generation = staged.execute("SELECT MIN(token_version) FROM users").fetchone()[0]
        # Simulate the previously accepted read/copy window: a password change
        # issues a cookie carrying exactly the staged restore generation.
        with get_db() as live:
            live.execute("UPDATE users SET token_version=?", (generation,))
            simulated.append(_dump(live))

    monkeypatch.setattr(settings, "_invalidate_restored_tokens", concurrent_password_change)
    response = admin_client.post(
        "/api/settings/restore", files={"file": ("backup.db", backup.content, "application/octet-stream")},
    )
    assert response.json()["ok"] is False
    assert "sessions changed" in response.json()["message"]
    assert db.execute("SELECT title FROM items WHERE id=?", (marker,)).fetchone()[0] == "Must survive restore race refusal"
    assert _dump(db) == simulated[0]
