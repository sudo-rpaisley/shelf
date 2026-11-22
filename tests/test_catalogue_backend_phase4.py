"""Bounded phase4 catalogue contracts; execute only after all writers freeze.

Real HTTP guards/writers/stages are exercised. Provider doubles write actual
bytes into the captured real stage, never a fabricated final cover path.
"""

import io
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image

from app import config
from app.database import get_db
from app.routers import items, items_common, items_synopses, tags as tag_routes
from app.services import api_tokens, catalogue_security, covers, libraries, tags, user_state
from tests.conftest import _create_user, _insert_borrower, _insert_item


def _snapshot(db):
    return list(db.iterdump())


def _files():
    return {p.name: p.read_bytes() for p in config.COVERS_DIR.iterdir()}


def _image(color="blue"):
    output = io.BytesIO()
    Image.new("RGB", (24, 36), color).save(output, format="JPEG")
    content = output.getvalue()
    assert len(content) > 100 and covers.validate_uploaded_cover(content)
    return content


def _row(db, item_id):
    return dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())


def _bulk(client, action, ids):
    if action == "merge":
        return client.post("/api/items/merge", json={"keep_id": ids[0], "merge_ids": ids[1:]})
    if action == "tags":
        return client.post("/api/items/bulk-tags", json={"item_ids": ids, "add": ["phase4"]})
    updates = {"language": "ja"} if action == "language" else {"media_type": "ebook"}
    return client.post("/api/items/bulk-update", json={"item_ids": ids, "updates": updates})


@pytest.mark.parametrize("action", ["bulk", "language", "merge", "tags"])
def test_global_viewer_library_editor_can_mutate_complete_selection(viewer_client, viewer_user, db, action):
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    ids = [_insert_item(db, title=f"Editable {i}", isbn=None, language="en") for i in range(3)]
    db.commit()
    assert db.execute("SELECT role FROM users WHERE id = ?", (viewer_user["id"],)).fetchone()[0] == "viewer"
    response = _bulk(viewer_client, action, ids)
    assert response.status_code == 200
    if action == "merge":
        assert response.json() == {"ok": True, "merged": 2}
        assert [r[0] for r in db.execute("SELECT id FROM items ORDER BY id")] == [ids[0]]
    elif action == "tags":
        assert response.json() == {"ok": True, "updated": 3, "message": "Added phase4 to 3 item(s)"}
        assert [r[0] for r in db.execute("SELECT item_id FROM item_tags ORDER BY item_id")] == ids
    else:
        assert response.json() == {"ok": True, "updated": 3}
        field, value = ("language", "ja") if action == "language" else ("media_type", "ebook")
        assert [r[0] for r in db.execute(f"SELECT {field} FROM items ORDER BY id")] == [value] * 3
    assert 'data-testid="bulk-language"' in viewer_client.get("/browse").text


@pytest.mark.parametrize("action", ["bulk", "language", "merge", "tags"])
@pytest.mark.parametrize("blocker", ["read_only", "no_grant", "missing"])
def test_mixed_selection_refuses_every_write_despite_global_editor(editor_client, editor_user, db, action, blocker):
    other = libraries.create_library(db, "Separate target boundary")
    if blocker == "read_only":
        libraries.set_membership(db, other["id"], editor_user["id"], "viewer")
    first = _insert_item(db, title="Allowed first", isbn=None, language="en")
    denied = _insert_item(db, title="PRIVATE BLOCKER", isbn=None, _library_id=other["id"])
    last = _insert_item(db, title="Allowed last", isbn=None, language="en")
    db.commit()
    ids = [first, 999999 if blocker == "missing" else denied, last]
    before = _snapshot(db)
    response = _bulk(editor_client, action, ids)
    expected_status = 403 if blocker == "read_only" else (400 if action == "tags" else 404)
    assert response.status_code == expected_status
    if action == "tags" and blocker != "read_only":
        assert response.json() == {"ok": False, "message": "1 selected item(s) no longer exist"}
    assert "PRIVATE BLOCKER" not in response.text
    assert _snapshot(db) == before


@pytest.mark.parametrize("action", ["bulk", "language", "merge", "tags"])
def test_complete_target_preflight_is_locked_and_precedes_first_mutation(editor_client, db, monkeypatch, action):
    ids = [_insert_item(db, title=f"Lock target {i}", isbn=None) for i in range(3)]
    db.commit()
    before = _snapshot(db)
    module = tag_routes if action == "tags" else items
    original = module.require_item_role
    seen = []

    def require(conn, item_id, role, **kwargs):
        assert conn.in_transaction
        assert _snapshot(conn) == before
        with sqlite3.connect(config.DATABASE_PATH, timeout=0) as rival:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                rival.execute("BEGIN IMMEDIATE")
        seen.append(item_id)
        return original(conn, item_id, role, **kwargs)

    monkeypatch.setattr(module, "require_item_role", require)
    response = _bulk(editor_client, action, ids)
    assert response.status_code == 200 and response.json()["ok"] is True
    assert seen == ids


@pytest.mark.parametrize("role", ["admin", "viewer"])
@pytest.mark.parametrize("action", ["bulk", "language", "merge", "tags"])
def test_real_bearer_never_lifts_library_editor_or_account_admin_cap(client, db, role, action):
    actor = _create_user(f"bearer-{role}", "password123", "Token account", role, library_role="editor")
    ids = [_insert_item(db, title=f"Bearer target {i}", isbn=None) for i in range(3)]
    created = api_tokens.create(db, actor["id"], "phase4 bearer")
    db.commit()
    headers = {"Authorization": f"Bearer {created['token']}"}
    assert client.get("/api/v1/stats", headers=headers).status_code == 200
    principal = api_tokens.resolve(created["token"])
    assert principal["role"] == "viewer" and principal["account_role"] == role
    assert libraries.has_item_role(db, principal, ids[0], "viewer")
    with pytest.raises(HTTPException) as denied:
        catalogue_security.require_item_role(db, ids[0], "editor", user=principal)
    assert denied.value.status_code == 403
    before = _snapshot(db)
    # G117: tokens authenticate v1 only. Retain the exact outside-v1 protocol,
    # rather than following a login document and mistaking it for a success.
    if action == "merge":
        path, body = "/api/items/merge", {"keep_id": ids[0], "merge_ids": ids[1:]}
    elif action == "tags":
        path, body = "/api/items/bulk-tags", {"item_ids": ids, "add": ["forbidden"]}
    else:
        path, body = "/api/items/bulk-update", {"item_ids": ids, "updates": {"language": "ja"}}
    response = client.post(path, json=body, headers=headers, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert _snapshot(db) == before


def test_bulk_tags_counts_complete_unavailable_selection_without_subset_write(editor_client, db, monkeypatch):
    private = libraries.create_library(db, "Hidden tags")
    first = _insert_item(db, title="Visible first", isbn=None)
    hidden = _insert_item(db, title="HIDDEN TAG TITLE", isbn=None, _library_id=private["id"])
    trash = _insert_item(db, title="Trashed tag parent", isbn=None, deleted_at="2026-01-01")
    last = _insert_item(db, title="Visible last", isbn=None)
    db.commit()
    ids = [first, hidden, 999999, trash, hidden, last, 999999]
    seen = []
    original = tag_routes.require_item_role

    def require(conn, item_id, role, **kwargs):
        assert conn.in_transaction
        seen.append(item_id)
        return original(conn, item_id, role, **kwargs)

    mutate = Mock(side_effect=AssertionError("no bulk tag mutation before whole preflight"))
    monkeypatch.setattr(tag_routes, "require_item_role", require)
    monkeypatch.setattr(tag_routes.tags_svc, "bulk_tag", mutate)
    before = _snapshot(db)
    response = _bulk(editor_client, "tags", ids)
    assert response.status_code == 400
    assert response.json() == {"ok": False, "message": "3 selected item(s) no longer exist"}
    assert seen == list(dict.fromkeys(ids))
    mutate.assert_not_called()
    assert _snapshot(db) == before


@pytest.mark.parametrize("state", ["unmapped", "trash"])
def test_native_admin_actual_count_never_ignores_extant_unavailable_rows(admin_client, db, state):
    valid = _insert_item(db, title="Allowed target", isbn=None, language="en")
    blocker = _insert_item(db, title="EXTANT UNAVAILABLE", isbn=None,
                           _library_id=None if state == "unmapped" else 1,
                           deleted_at="2026-01-01" if state == "trash" else None)
    db.commit()
    before = _snapshot(db)
    response = _bulk(admin_client, "language", [valid, 999999, blocker])
    assert response.status_code == 404
    assert "EXTANT UNAVAILABLE" not in response.text
    assert _snapshot(db) == before


def _single(client, action, item_id, borrower_id):
    if action == "edit":
        return client.post(f"/api/items/{item_id}", data={"title": "Must not save"},
                           files={"cover": ("cover.jpg", _image(), "image/jpeg")})
    if action == "edit_choice":
        return client.post(f"/api/items/{item_id}", data={"title": "Must not save", **CHOICE})
    if action == "delete":
        return client.delete(f"/api/items/{item_id}")
    if action == "checkout":
        return client.post(f"/api/items/{item_id}/checkout", data={"borrower_id": str(borrower_id)})
    if action == "remove_tag":
        return client.delete(f"/api/items/{item_id}/tags/999999")
    return client.post(f"/api/items/{item_id}/fetch-synopsis")


@pytest.mark.parametrize("action", ["edit", "edit_choice", "delete", "checkout", "remove_tag", "synopsis"])
@pytest.mark.parametrize("state", ["missing", "hidden", "unmapped", "read_only"])
def test_target_boundary_keeps_native_envelope_and_never_translates_403(
    editor_client, editor_user, db, monkeypatch, action, state
):
    private = libraries.create_library(db, "Single-target boundary")
    if state == "read_only":
        libraries.set_membership(db, private["id"], editor_user["id"], "viewer")
    target = _insert_item(db, title="PRIVATE SINGLE TITLE", isbn=None,
                          _library_id=None if state == "unmapped" else private["id"])
    borrower = _insert_borrower(db)
    db.commit()
    if state == "missing":
        target = 999999
    fetch = AsyncMock()
    write = Mock(wraps=covers.write_uploaded_cover)
    download = AsyncMock()
    monkeypatch.setattr(items_synopses.synopsis_svc, "fetch_description", fetch)
    monkeypatch.setattr(covers, "write_uploaded_cover", write)
    monkeypatch.setattr(covers, "_download_to_item", download)
    before, before_files = _snapshot(db), _files()
    response = _single(editor_client, action, target, borrower)
    if state == "read_only":
        assert response.status_code == 403
        assert response.json() == {"detail": "Forbidden"}
    else:
        assert response.status_code == 404
        if action in ("edit", "edit_choice", "remove_tag"):
            assert response.text == ("Item not found" if action == "remove_tag" else "Not found")
            assert response.headers["content-type"].startswith("text/plain")
        else:
            assert response.json() == {"ok": False, "message": "Item not found"}
    fetch.assert_not_awaited()
    write.assert_not_called()
    download.assert_not_awaited()
    assert "PRIVATE SINGLE TITLE" not in response.text
    assert _snapshot(db) == before and _files() == before_files


def test_visible_repeated_delete_remains_a_native_successful_noop(editor_client, db):
    target = _insert_item(db, title="Reversible delete", isbn=None)
    tags.attach_tags(db, target, ["retained for restore"])
    db.commit()
    assert editor_client.delete(f"/api/items/{target}").json() == {"ok": True}
    before = _snapshot(db)
    response = editor_client.delete(f"/api/items/{target}")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert _snapshot(db) == before


@pytest.mark.parametrize("field,value,message", [
    ("title", "", "Title is required"),
    ("publish_year", "not-a-year", "Invalid publish year"),
    ("manual_value", "not-a-price", "Invalid manual value"),
    ("media_type", "laserdisc", "Invalid media type"),
    ("owned", "2", "Owned must be 0 or 1"),
    ("owned", "two", "Owned must be 0 or 1"),
    ("location_id", "999999", "Location not found"),
    ("isbn", "9780441172710", "Invalid ISBN"),
])
def test_partial_edit_diagnostics_are_field_aware_and_have_no_effects(editor_client, db, monkeypatch, field, value, message):
    target = _insert_item(db, title="Keep metadata", isbn=None)
    db.commit()
    write = Mock(wraps=covers.write_uploaded_cover)
    monkeypatch.setattr(covers, "write_uploaded_cover", write)
    before, before_files = _snapshot(db), _files()
    response = editor_client.post(f"/api/items/{target}",
                                  data={"notes": "Do not save", "reading_status": "read", field: value},
                                  files={"cover": ("cover.jpg", _image(), "image/jpeg")})
    assert response.status_code == 400 and response.text == message
    write.assert_not_called()
    assert _snapshot(db) == before and _files() == before_files


def test_full_form_retains_separate_303_banner_refusal(editor_client, db):
    target = _insert_item(db, title="Keep full form", isbn=None)
    db.commit()
    before = _snapshot(db)
    response = editor_client.post(f"/api/items/{target}",
                                  data={"title": "Dirty title", "media_type": "book", "owned": "1",
                                        "publish_year": "not-a-year"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/item/{target}/edit?error=invalid_number"
    assert _snapshot(db) == before


CHOICE_URL = "https://covers.openlibrary.org/b/id/123-L.jpg"
CHOICE = {"cover_choice_url": CHOICE_URL,
          "cover_choice_edition_isbn": "0441013597", "cover_choice_edition_language": "en"}


def _capture_stage(monkeypatch):
    real = items_common.CoverStage
    stages = []

    def capture(item_id):
        stage = real(item_id)
        stages.append(stage)
        return stage

    monkeypatch.setattr(items_common, "CoverStage", capture)
    return stages


def test_save_consumes_real_pending_choice_with_dirty_fields_and_actor_private_state(
    editor_client, editor_user, db, monkeypatch
):
    other = _create_user("other-editor-state", "password123", "Other reader", "viewer", library_role="viewer")
    target = _insert_item(db, title="Before Save", isbn="9780441172719", manual_value=5,
                          cover_path="covers/original.jpg", cover_edition_isbn="9780547928227",
                          cover_edition_language="en")
    user_state.save_state(db, editor_user["id"], target, reading_status="reading", rating=4)
    user_state.save_state(db, other["id"], target, reading_status="read", rating=1)
    db.execute("UPDATE items SET cover_path = ? WHERE id = ?", (f"covers/{target}.jpg", target))
    db.commit()
    (config.COVERS_DIR / f"{target}.jpg").write_bytes(_image("green"))
    original_state = dict(user_state.get_state(db, other["id"], target))
    staged_bytes = _image("red")
    stages = _capture_stage(monkeypatch)
    real_store = items_common.store_item_cover
    stores = []
    principals, clients = [], []

    def store(conn, item_id, path, **kwargs):
        assert conn.in_transaction
        assert _row(conn, target)["title"] == "Dirty title survives"
        assert user_state.get_state(conn, editor_user["id"], target)["reading_status"] == "read"
        stores.append((item_id, path, kwargs))
        return real_store(conn, item_id, path, **kwargs)

    async def download(download_id, url, client):
        stage, = stages
        assert stage.item_id == target and download_id == stage.download_id and download_id < 0
        assert url == CHOICE_URL and isinstance(client, httpx.AsyncClient)
        assert catalogue_security.current_actor()["id"] == editor_user["id"]
        principals.append(dict(catalogue_security.current_actor()))
        clients.append(client)
        with get_db() as conn:
            assert _row(conn, target)["title"] == "Before Save"
            assert user_state.get_state(conn, editor_user["id"], target)["reading_status"] == "reading"
        stage.path.write_bytes(staged_bytes)
        return f"covers/{download_id}.jpg"

    monkeypatch.setattr(items_common, "store_item_cover", store)
    lookup = AsyncMock(side_effect=download)
    monkeypatch.setattr(covers, "_download_to_item", lookup)
    response = editor_client.post(f"/api/items/{target}", data={
        "title": "Dirty title survives", "manual_value": "12.5", "reading_status": "read", **CHOICE,
    }, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/item/{target}"
    lookup.assert_awaited_once()
    assert lookup.await_args.args == (stages[0].download_id, CHOICE_URL, clients[0])
    assert lookup.await_args.kwargs == {}
    row = _row(db, target)
    assert (row["title"], row["manual_value"], row["isbn"]) == ("Dirty title survives", 12.5, "9780441172719")
    assert (row["cover_path"], row["cover_edition_isbn"], row["cover_edition_language"]) == (
        f"covers/{target}.jpg", "9780441013593", "en")
    state = user_state.get_state(db, editor_user["id"], target)
    assert state["reading_status"] == "read" and state["rating"] == 4
    assert dict(user_state.get_state(db, other["id"], target)) == original_state
    assert len(stores) == 1
    item_id, path, kwargs = stores[0]
    assert (item_id, path) == (target, f"covers/{target}.jpg")
    assert kwargs == {"edition": ("9780441013593", "en"),
                      "user": principals[0], "library_ids": {target: 1}}
    assert _files() == {f"{target}.jpg": staged_bytes}
    assert not stages[0].path.exists()


@pytest.mark.parametrize("refusal,status,message", [
    ("download", 400, "Cover could not be downloaded"),
    ("validation", 400, "Invalid publish year"),
    ("conflict", 409, "Update conflicts with existing catalogue data"),
])
def test_pending_choice_refusal_keeps_all_dirty_fields_and_files_unchanged(
    editor_client, editor_user, db, monkeypatch, refusal, status, message
):
    target = _insert_item(db, title="Keep dirty state", isbn="9780156027601", cover_path="covers/original.jpg")
    _insert_item(db, title="Existing ISBN", isbn="9780441172719")
    user_state.save_state(db, editor_user["id"], target, reading_status="reading", rating=3)
    db.execute("UPDATE items SET cover_path = ? WHERE id = ?", (f"covers/{target}.jpg", target))
    db.commit()
    old = config.COVERS_DIR / f"{target}.jpg"
    old.write_bytes(_image("green"))
    stages = _capture_stage(monkeypatch)

    async def download(download_id, url, client):
        stage, = stages
        assert download_id == stage.download_id and url == CHOICE_URL
        stage.path.write_bytes(_image("red"))
        return None if refusal == "download" else f"covers/{download_id}.jpg"

    monkeypatch.setattr(covers, "_download_to_item", download)
    before, before_files = _snapshot(db), _files()
    data = {"title": "Must not save", "reading_status": "read", "manual_value": "20", **CHOICE}
    if refusal == "validation":
        data["publish_year"] = "bad-year"
    elif refusal == "conflict":
        data["isbn"] = "9780441172719"
    response = editor_client.post(f"/api/items/{target}", data=data, follow_redirects=False)
    assert response.status_code == status and response.text == message
    assert _snapshot(db) == before and _files() == before_files
    assert len(stages) == 1 and not stages[0].path.exists()


def test_pending_choice_publication_callback_failure_rolls_back_whole_save(editor_client, editor_user, db, monkeypatch):
    target = _insert_item(db, title="Keep before publication", isbn=None)
    db.execute("UPDATE items SET cover_path = ? WHERE id = ?", (f"covers/{target}.jpg", target))
    user_state.save_state(db, editor_user["id"], target, reading_status="reading", rating=4)
    db.commit()
    (config.COVERS_DIR / f"{target}.jpg").write_bytes(_image("green"))
    stages = _capture_stage(monkeypatch)
    original_store = items_common.store_item_cover
    stores = []

    async def download(download_id, url, client):
        stage, = stages
        assert download_id == stage.download_id and url == CHOICE_URL
        stage.path.write_bytes(_image("red"))
        return f"covers/{download_id}.jpg"

    def refuse(conn, item_id, path, **kwargs):
        assert _row(conn, target)["title"] == "Dirty title"
        assert user_state.get_state(conn, editor_user["id"], target)["reading_status"] == "read"
        original_store(conn, item_id, path, **kwargs)
        stores.append(item_id)
        raise RuntimeError("Publication callback refused")

    monkeypatch.setattr(covers, "_download_to_item", download)
    monkeypatch.setattr(items_common, "store_item_cover", refuse)
    before, before_files = _snapshot(db), _files()
    with pytest.raises(RuntimeError, match="Publication callback refused"):
        editor_client.post(f"/api/items/{target}", data={"title": "Dirty title", "reading_status": "read", **CHOICE})
    assert stores == [target]
    assert _snapshot(db) == before and _files() == before_files
    assert len(stages) == 1 and not stages[0].path.exists()


@pytest.mark.parametrize("change,status", [
    ("move", 404), ("revoke_library", 403), ("revoke_account", 403), ("revoke_session", 403), ("trash", 404),
])
def test_pending_choice_rechecks_exact_parent_and_principal_after_provider_await(
    editor_client, editor_user, db, monkeypatch, change, status
):
    other = libraries.create_library(db, "Another editable parent")
    libraries.set_membership(db, other["id"], editor_user["id"], "editor")
    target = _insert_item(db, title="Keep after race", isbn=None, cover_path="covers/original.jpg")
    user_state.save_state(db, editor_user["id"], target, reading_status="reading", rating=4)
    db.execute("UPDATE items SET cover_path = ? WHERE id = ?", (f"covers/{target}.jpg", target))
    db.commit()
    (config.COVERS_DIR / f"{target}.jpg").write_bytes(_image("green"))
    stages = _capture_stage(monkeypatch)
    after_rival = []

    async def download(download_id, url, client):
        stage, = stages
        assert download_id == stage.download_id and url == CHOICE_URL
        stage.path.write_bytes(_image("red"))
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if change == "move":
                conn.execute("UPDATE library_items SET library_id = ? WHERE item_id = ?", (other["id"], target))
            elif change == "revoke_library":
                libraries.set_membership(conn, 1, editor_user["id"], "viewer")
            elif change == "revoke_account":
                conn.execute("UPDATE users SET role = 'viewer' WHERE id = ?", (editor_user["id"],))
            elif change == "revoke_session":
                conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (editor_user["id"],))
            else:
                conn.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (target,))
        with get_db() as conn:
            after_rival.extend(_snapshot(conn))
        return f"covers/{download_id}.jpg"

    monkeypatch.setattr(covers, "_download_to_item", download)
    before_files = _files()
    response = editor_client.post(f"/api/items/{target}", data={
        "title": "Must not save", "reading_status": "read", "manual_value": "20", **CHOICE,
    }, follow_redirects=False)
    assert response.status_code == status
    if status == 404:
        assert response.text == "Not found"
    else:
        assert response.json() == {"detail": "Forbidden"}
    assert _snapshot(db) == after_rival and _files() == before_files
    assert len(stages) == 1 and not stages[0].path.exists()


@pytest.mark.parametrize("source", ["upload", "manual_url", "edition_without_url"])
def test_pending_cover_source_ambiguity_is_an_explicit_no_effect_refusal(editor_client, db, monkeypatch, source):
    target = _insert_item(db, title="Keep ambiguous Save", isbn=None)
    db.commit()
    data, files = {"title": "Must not save", **CHOICE}, None
    if source == "upload":
        files = {"cover": ("cover.jpg", _image(), "image/jpeg")}
    elif source == "manual_url":
        data["url"] = "https://example.com/manual-cover.jpg"
    else:
        data["cover_choice_url"] = ""
    download = AsyncMock()
    monkeypatch.setattr(covers, "_download_to_item", download)
    before = _snapshot(db)
    response = editor_client.post(f"/api/items/{target}", data=data, files=files)
    assert response.status_code == 400
    assert response.text == ("Choose a cover before its edition" if source == "edition_without_url" else "Choose one cover source")
    download.assert_not_awaited()
    assert _snapshot(db) == before and _files() == {}


def test_unchanged_cover_keeps_provenance_and_real_upload_replacement_clears_it(editor_client, db, monkeypatch):
    target = _insert_item(db, title="Cover provenance", isbn=None, cover_path="covers/original.jpg",
                          cover_edition_isbn="9780441013593", cover_edition_language="en")
    db.execute("UPDATE items SET cover_path = ? WHERE id = ?", (f"covers/{target}.jpg", target))
    db.commit()
    (config.COVERS_DIR / f"{target}.jpg").write_bytes(_image("green"))
    response = editor_client.post(f"/api/items/{target}", data={"title": "Ordinary Save"}, follow_redirects=False)
    assert response.status_code == 303
    row = _row(db, target)
    assert (row["cover_path"], row["cover_edition_isbn"], row["cover_edition_language"]) == (
        f"covers/{target}.jpg", "9780441013593", "en")
    uploaded = _image("red")
    sql_connections = []
    update = items.update_item_fields
    def observe_update(conn, *args, **kwargs):
        from app.database import _Connection

        assert isinstance(conn, _Connection) and conn.in_transaction
        sql_connections.append(conn)
        return update(conn, *args, **kwargs)
    monkeypatch.setattr(items, "update_item_fields", observe_update)
    write = Mock(wraps=covers.write_uploaded_cover)
    monkeypatch.setattr(covers, "write_uploaded_cover", write)
    response = editor_client.post(f"/api/items/{target}", data={"title": "Uploaded replacement"},
                                  files={"cover": ("cover.jpg", uploaded, "image/jpeg")}, follow_redirects=False)
    assert response.status_code == 303
    assert len(sql_connections) == 1
    write.assert_called_once_with(target, uploaded, db=sql_connections[0])
    row = _row(db, target)
    assert (row["cover_path"], row["cover_edition_isbn"], row["cover_edition_language"]) == (f"covers/{target}.jpg", None, None)
    assert _files() == {f"{target}.jpg": uploaded}


def test_synopsis_routes_are_composed_once_with_real_new_module_endpoints():
    from app.main import app

    expected = {
        ("POST", "/api/items/{item_id}/fetch-synopsis"): items_synopses.fetch_synopsis,
        ("GET", "/api/synopses/backfill/stream"): items_synopses.backfill_synopses_stream,
    }
    for (method, path), endpoint in expected.items():
        matches = [route for route in app.routes if getattr(route, "path", None) == path
                   and method in getattr(route, "methods", ())]
        assert len(matches) == 1 and matches[0].endpoint is endpoint
    assert not hasattr(items, "synopsis_svc") and not hasattr(items, "fetch_synopsis")
    root = Path(__file__).resolve().parents[1]
    assert len((root / "app/routers/items.py").read_text().splitlines()) < 1600
