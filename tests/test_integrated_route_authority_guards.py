"""Real HTTP/library/write-funnel regressions for the integrated route lane.

Run only after the parent freezes all source writers. Providers are stubbed;
these tests do not substitute endpoint calls for FastAPI dependency dispatch.
"""

import json
import sqlite3
from unittest.mock import AsyncMock

import pytest


def _user(db, name="route-actor", role="viewer", library_role="editor"):
    from app.services import libraries

    cur = db.execute(
        "INSERT INTO users (username, password, role) VALUES (?, 'not-a-login', ?)",
        (name, role),
    )
    user = {"id": cur.lastrowid, "username": name, "role": role, "display_name": None}
    if library_role:
        libraries.set_membership(db, 1, user["id"], library_role)
    return user


def _login(client, db, user):
    from app.auth import create_token

    db.commit()
    client.cookies.set("access_token", create_token(
        user["id"], user["username"], user["role"], user["display_name"]))


def _item(db, title="Route item", **fields):
    from app.services import item_write

    return item_write.insert_item(db, title=title, source="route-security-test", **fields)


def _legacy_kids_item(db, title="Retained kids item", **fields):
    """Materialize a real retained fork row, not a canonicalized new input."""
    item_id = _item(db, title, media_type="book", **fields)
    db.execute("UPDATE items SET media_type = 'kids_book' WHERE id = ?", (item_id,))
    return item_id


def _snapshot(db):
    tables = ("items", "item_copies", "library_items", "tags", "item_tags",
              "lists", "list_items", "user_item_state", "user_reading_log")
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in tables}


def _events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


@pytest.mark.parametrize("path,data", [
    ("/api/items/{id}/cover-remove", {}),
    ("/api/covers/review/{id}/dismiss", {}),
    ("/api/items/{id}/tags", {"name": "library-editor"}),
    ("/api/valuate/{id}", {}),
    ("/api/items/{id}/fill-from-audible", {}),
    ("/api/hardcover/push/{id}", {}),
])
def test_global_viewer_library_editor_can_reach_catalogue_mutations(client, db, path, data):
    actor = _user(db)
    target = _item(db)  # No ISBN/ASIN/token: these cases cannot contact providers.
    _login(client, db, actor)
    response = client.post(path.format(id=target), data=data)
    assert response.status_code == 200


@pytest.mark.parametrize("path,data", [
    ("/api/items/{id}/cover-remove", {}),
    ("/api/covers/review/{id}/dismiss", {}),
    ("/api/items/{id}/tags", {"name": "forbidden"}),
    ("/api/valuate/{id}", {}),
    ("/api/items/{id}/fill-from-audible", {}),
    ("/api/hardcover/push/{id}", {}),
])
def test_global_editor_cannot_write_a_viewer_library(client, db, path, data):
    from app.services import libraries

    actor = _user(db, role="editor", library_role="viewer")
    editable = libraries.create_library(db, "Another editable library")
    libraries.set_membership(db, editable["id"], actor["id"], "editor")
    target = _item(db)
    _login(client, db, actor)
    before = _snapshot(db)
    response = client.post(path.format(id=target), data=data)
    assert response.status_code == 403
    assert _snapshot(db) == before


@pytest.mark.parametrize("trashed", [False, True])
@pytest.mark.parametrize("verb", ["select", "url", "upload", "remove", "retry", "dismiss", "tag", "price", "audible", "push"])
def test_hidden_target_refuses_without_file_provider_or_catalogue_effect(client, db, monkeypatch, trashed, verb):
    from app.services import audnexus, covers, hardcover, isbndb, item_write, libraries, manual_cover

    actor = _user(db)
    private = libraries.create_library(db, "Hidden route library")
    target = _item(db, "DO NOT DISCLOSE THIS ROUTE TITLE", library_id=private["id"],
                   isbn="9780441172719", asin="B00EXAMPLE", media_type="audiobook")
    if trashed:
        item_write.trash_item(db, target)
    probes = []
    for module, attribute in ((covers, "_download_to_item"), (covers, "download_cover"),
                              (manual_cover, "download"), (audnexus, "lookup"),
                              (hardcover, "push_item_to_hardcover"), (isbndb, "lookup_price")):
        probe = AsyncMock()
        monkeypatch.setattr(module, attribute, probe)
        probes.append(probe)
    upload = AsyncMock()
    monkeypatch.setattr(covers, "save_uploaded_cover", upload)
    _login(client, db, actor)
    before = _snapshot(db)
    paths = {"select": f"/api/items/{target}/cover-select", "url": f"/api/items/{target}/cover-url",
             "upload": f"/api/items/{target}/cover-upload", "remove": f"/api/items/{target}/cover-remove",
             "retry": f"/api/items/{target}/retry-cover", "dismiss": f"/api/covers/review/{target}/dismiss",
             "tag": f"/api/items/{target}/tags", "price": f"/api/valuate/{target}",
             "audible": f"/api/items/{target}/fill-from-audible", "push": f"/api/hardcover/push/{target}"}
    response = client.post(paths[verb], data={"url": "https://covers.openlibrary.org/b/id/1-L.jpg", "name": "blocked"})
    assert response.status_code == 404
    assert "DO NOT DISCLOSE" not in response.text
    assert "Trash" not in response.text
    assert _snapshot(db) == before
    for probe in probes:
        probe.assert_not_called()
    upload.assert_not_called()


def test_bulk_tags_authorizes_every_selected_id_before_first_write(client, db, monkeypatch):
    from app import config
    from app.routers import tags

    actor = _user(db)
    targets = [_item(db, "First"), _item(db, "Second")]
    _login(client, db, actor)
    original = tags.require_item_role
    seen = []

    def checked(conn, item_id, role, **kwargs):
        assert conn.in_transaction
        with sqlite3.connect(config.DATABASE_PATH, timeout=0) as rival:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                rival.execute("BEGIN IMMEDIATE")
        assert conn.execute("SELECT COUNT(*) FROM item_tags").fetchone()[0] == 0
        seen.append(item_id)
        return original(conn, item_id, role, **kwargs)

    monkeypatch.setattr(tags, "require_item_role", checked)
    response = client.post("/api/items/bulk-tags", json={"item_ids": targets, "add": ["all-authorized"]})
    assert response.status_code == 200
    assert response.json()["updated"] == 2
    assert seen == targets


def test_bulk_hidden_target_is_atomic_not_successful_subset(client, db):
    from app.services import libraries

    actor = _user(db)
    visible = _item(db, "Allowed first target")
    private = libraries.create_library(db, "Hidden bulk target")
    hidden = _item(db, "HIDDEN BULK TITLE", library_id=private["id"])
    _login(client, db, actor)
    before = _snapshot(db)
    response = client.post("/api/items/bulk-tags", json={"item_ids": [visible, hidden], "add": ["must-not-exist"]})
    assert response.status_code == 400
    assert response.json() == {"ok": False, "message": "1 selected item(s) no longer exist"}
    assert "HIDDEN BULK TITLE" not in response.text
    assert _snapshot(db) == before


@pytest.mark.parametrize("change,status", [("move", 404), ("revoke", 403)])
def test_cover_download_is_staged_and_parent_bound_across_provider_wait(client, db, monkeypatch, change, status):
    from app import config
    from app.database import get_db
    from app.services import covers, libraries

    actor = _user(db)
    other = libraries.create_library(db, "Other editable parent")
    libraries.set_membership(db, other["id"], actor["id"], "editor")
    target = _item(db, cover_path="covers/original.jpg")
    existing = config.COVERS_DIR / f"{target}.jpg"
    existing.write_bytes(b"unchanged real image")
    _login(client, db, actor)

    async def download(download_id, url, http):
        assert download_id < 0 and download_id != target
        (config.COVERS_DIR / f"{download_id}.jpg").write_bytes(b"new staged image")
        with get_db() as rival:
            if change == "move":
                rival.execute("UPDATE library_items SET library_id = ? WHERE item_id = ?", (other["id"], target))
            else:
                rival.execute("UPDATE library_memberships SET role = 'viewer' WHERE library_id = 1 AND user_id = ?", (actor["id"],))
        return f"covers/{download_id}.jpg"

    monkeypatch.setattr(covers, "_download_to_item", download)
    response = client.post(f"/api/items/{target}/cover-select", data={"url": "https://covers.openlibrary.org/b/id/1-L.jpg"})
    assert response.status_code == status
    assert existing.read_bytes() == b"unchanged real image"
    assert list(config.COVERS_DIR.iterdir()) == [existing]
    assert db.execute("SELECT cover_path FROM items WHERE id = ?", (target,)).fetchone()[0] == "covers/original.jpg"


def test_successful_staged_cover_uses_authoritative_writer_and_preserves_catalogue_fields(client, db, monkeypatch):
    from app import config
    from app.services import covers, item_write

    actor = _user(db)
    target = _item(db, "Keep stored title", isbn="9780441172719", reading_status="read")
    _login(client, db, actor)
    original = item_write.update_item_fields
    writes = []

    def tracked(conn, item_id, fields):
        writes.append((item_id, dict(fields)))
        return original(conn, item_id, fields)

    async def download(download_id, url, http):
        assert download_id < 0
        (config.COVERS_DIR / f"{download_id}.jpg").write_bytes(b"verified fake image")
        return f"covers/{download_id}.jpg"

    # items_common calls its established module-bound writer export.
    from app.routers import items_common
    monkeypatch.setattr(items_common, "update_item_fields", tracked)
    monkeypatch.setattr(covers, "_download_to_item", download)
    response = client.post(f"/api/items/{target}/cover-select", data={
        "url": "https://covers.openlibrary.org/b/id/1-L.jpg",
        "edition_isbn": "9780441013593", "edition_language": "en",
    })
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == f"/item/{target}"
    assert writes == [(target, {"cover_path": f"covers/{target}.jpg",
                                "cover_edition_isbn": "9780441013593", "cover_edition_language": "en"})]
    assert (config.COVERS_DIR / f"{target}.jpg").read_bytes() == b"verified fake image"
    row = db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone()
    assert row["title"] == "Keep stored title"
    assert row["isbn"] == "9780441172719" and row["reading_status"] == "read"


@pytest.mark.parametrize("feature,path,body", [
    ("valuation", "/api/valuate/1", {}),
    ("hardcover", "/api/hardcover/add-to-shelf", {"title": "Disabled"}),
    ("hardcover", "/api/hardcover/import/stream", None),
    ("intake", "/api/intake/confirm", {"books": [{"title": "Disabled"}]}),
])
def test_feature_dependencies_still_refuse_before_work(client, db, monkeypatch, feature, path, body):
    from app.features import set_feature_enabled
    from app.services import hardcover, isbndb, openlibrary

    actor = _user(db)
    _login(client, db, actor)
    set_feature_enabled(db, feature, False)
    db.commit()
    probes = [AsyncMock(), AsyncMock(), AsyncMock()]
    monkeypatch.setattr(hardcover, "get_user_id", probes[0])
    monkeypatch.setattr(isbndb, "lookup_price", probes[1])
    monkeypatch.setattr(openlibrary, "search_by_title_author", probes[2])
    before = _snapshot(db)
    response = client.get(path) if body is None else client.post(path, json=body)
    assert response.status_code == 403
    assert _snapshot(db) == before
    for probe in probes:
        probe.assert_not_called()


def test_disabled_hardcover_eventsource_retains_sse_error_protocol(client, db, monkeypatch):
    from app.features import set_feature_enabled
    from app.services import hardcover

    actor = _user(db)
    set_feature_enabled(db, "hardcover", False)
    _login(client, db, actor)
    probe = AsyncMock()
    monkeypatch.setattr(hardcover, "get_user_id", probe)
    response = client.get("/api/hardcover/import/stream", headers={"Accept": "text/event-stream"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _events(response) == [{"type": "error", "message": "Hardcover is turned off on this Shelf."}]
    probe.assert_not_called()


@pytest.mark.parametrize("path,body", [
    ("/api/items/{id}/tags", {"name": "cannot-write"}),
    ("/api/items/{id}/cover-remove", {}),
    ("/api/valuate/{id}", {}),
    ("/api/hardcover/add-to-shelf", {"title": "Cannot insert"}),
    ("/api/intake/confirm", {"books": [{"title": "Cannot insert"}]}),
])
def test_account_admin_bearer_stays_viewer_at_catalogue_routes(client, db, path, body):
    from fastapi import HTTPException
    from app.services import api_tokens, catalogue_security, libraries

    admin = _user(db, role="admin")
    private = libraries.create_library(db, "Bearer-readable admin library")
    target = _item(db, library_id=private["id"])
    token = api_tokens.create(db, admin["id"], "route regression")
    db.commit()
    headers = {"Authorization": f"Bearer {token['token']}"}
    assert client.get("/api/v1/stats", headers=headers).status_code == 200
    outside = client.get(f"/api/items/{target}/cover-status", headers=headers, follow_redirects=False)
    assert outside.status_code == 303 and outside.headers["location"] == "/login"
    principal = api_tokens.resolve(token["token"])
    assert principal["role"] == "viewer" and principal["account_role"] == "admin"
    with pytest.raises(HTTPException) as refusal:
        catalogue_security.require_item_role(db, target, "editor", user=principal)
    assert refusal.value.status_code == 403
    before = _snapshot(db)
    # Bearers deliberately authenticate ONLY /api/v1 (the retained G117
    # contract). Inspect the native refusal, not a followed200 login document.
    response = client.post(path.format(id=target), data=body, headers=headers, follow_redirects=False) if "intake" not in path and "add-to-shelf" not in path else client.post(path, json=body, headers=headers, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.post("/api/v1/stats", headers=headers).status_code == 401
    assert _snapshot(db) == before


def test_csrf_is_not_bypassed_by_new_catalogue_dependencies(client, db):
    actor = _user(db)
    target = _item(db)
    _login(client, db, actor)
    before = _snapshot(db)
    response = client.post(f"/api/items/{target}/cover-remove", headers={"X-CSRF-Token": "incorrect"})
    assert response.status_code == 403
    assert _snapshot(db) == before


@pytest.mark.parametrize("trashed", [False, True])
def test_hardcover_hidden_identifier_collision_is_generic_not_restore_or_500(client, db, trashed):
    from app.services import item_write, libraries

    actor = _user(db)
    private = libraries.create_library(db, "Hidden Hardcover identity")
    hidden = _item(db, "PRIVATE HARDCOVER TITLE", library_id=private["id"], hardcover_book_id=801)
    if trashed:
        item_write.trash_item(db, hidden)
    _login(client, db, actor)
    before = _snapshot(db)
    response = client.post("/api/hardcover/add-to-shelf", json={"title": "Provider title", "hardcover_book_id": 801})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "This identifier is unavailable"}
    assert _snapshot(db) == before


def test_hardcover_import_all_target_preflight_prevents_partial_update(client, db, monkeypatch):
    from app.database import set_setting
    from app.services import hardcover, libraries

    actor = _user(db)
    first = _item(db, "Visible import", hardcover_book_id=811)
    private = libraries.create_library(db, "Hidden import match")
    _item(db, "PRIVATE IMPORT MATCH", library_id=private["id"], hardcover_book_id=812)
    set_setting(db, "hardcover_token", "unit-test-hardcover")
    _login(client, db, actor)
    monkeypatch.setattr(hardcover, "get_user_id", AsyncMock(return_value=900))
    monkeypatch.setattr(hardcover, "get_user_books", AsyncMock(return_value=[
        {"title": "Provider first", "hardcover_book_id": 811, "publisher": "Must not be saved"},
        {"title": "Provider second", "hardcover_book_id": 812},
    ]))
    before = _snapshot(db)
    response = client.get("/api/hardcover/import/stream")
    assert response.status_code == 200
    events = _events(response)
    assert events[0]["status"] == "loading"
    assert events[-1] == {"type": "error", "message": "This identifier is unavailable"}
    assert not any(event["type"] == "done" for event in events)
    assert "PRIVATE IMPORT MATCH" not in response.text
    assert db.execute("SELECT publisher FROM items WHERE id = ?", (first,)).fetchone()[0] is None
    assert _snapshot(db) == before


def test_hardcover_background_import_has_explicit_account_and_preserves_legacy_identity(client, db, monkeypatch):
    from app.database import set_setting
    from app.routers import hardcover as routes
    from app.services import catalogue_security, hardcover, user_state

    actor = _user(db)
    other = _user(db, "other-person")
    target = _legacy_kids_item(db, "Retained kids identity", hardcover_book_id=821,
                   hardcover_user_book_id=99, reading_status="want_to_read", cover_path="covers/kept.jpg")
    user_state.save_state(db, other["id"], target, reading_status="read")
    set_setting(db, "hardcover_token", "unit-test-hardcover")
    _login(client, db, actor)
    monkeypatch.setattr(hardcover, "get_user_id", AsyncMock(return_value=900))
    monkeypatch.setattr(hardcover, "get_user_books", AsyncMock(return_value=[{
        "title": "Provider title", "hardcover_book_id": 821, "hardcover_user_book_id": 901,
        "reading_status": "reading", "publisher": "New provider metadata",
    }]))
    original = routes._import_single_book_metadata
    seen = []

    def inspect(*args, **kwargs):
        seen.append((catalogue_security.current_actor(), kwargs["user"]["id"], kwargs["expected_item_id"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(routes, "_import_single_book_metadata", inspect)
    response = client.get("/api/hardcover/import/stream")
    assert _events(response)[-1]["updated"] == 1
    assert seen == [(None, actor["id"], target)]
    row = db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone()
    assert row["media_type"] == "kids_book"
    assert row["hardcover_user_book_id"] == 99
    assert row["reading_status"] == "want_to_read"
    assert row["publisher"] == "New provider metadata"
    assert user_state.get_state(db, actor["id"], target)["reading_status"] == "reading"
    assert user_state.get_state(db, other["id"], target)["reading_status"] == "read"
    assert db.execute("SELECT value FROM settings WHERE key = ?", (routes._private_link_key(actor["id"], target),)).fetchone()[0] == "901"
    assert db.execute("SELECT value FROM settings WHERE key = ?", (routes._private_link_key(other["id"], target),)).fetchone() is None


def test_hardcover_background_export_is_scoped_and_uses_only_actor_private_state(client, db, monkeypatch):
    from app.database import set_setting
    from app.services import catalogue_security, hardcover, libraries, user_state

    actor = _user(db)
    target = _item(db, "Export allowed", isbn="9780441172719", reading_status="read", hardcover_user_book_id=99)
    user_state.save_state(db, actor["id"], target, reading_status="reading")
    private = libraries.create_library(db, "Do not export")
    _item(db, "Hidden export", isbn="9780441013593", library_id=private["id"])
    set_setting(db, "hardcover_token", "unit-test-hardcover")
    _login(client, db, actor)
    seen = []

    async def push(token, item, http):
        seen.append((catalogue_security.current_actor(), item["id"], item["reading_status"], item["hardcover_user_book_id"]))
        return {"ok": True, "status": "added", "hardcover_book_id": 831, "hardcover_user_book_id": 902}

    monkeypatch.setattr(hardcover, "push_item_to_hardcover", push)
    response = client.get("/api/hardcover/export/stream")
    assert _events(response)[-1]["total"] == 1
    assert seen == [(None, target, "reading", None)]
    assert db.execute("SELECT hardcover_user_book_id FROM items WHERE id = ?", (target,)).fetchone()[0] == 99


def test_intake_hidden_second_identifier_refuses_before_first_insert(client, db, monkeypatch):
    from app.services import libraries, openlibrary

    actor = _user(db)
    private = libraries.create_library(db, "Hidden intake ISBN")
    _item(db, "PRIVATE INTAKE RECORD", isbn="9780441172719", library_id=private["id"])
    _login(client, db, actor)
    monkeypatch.setattr(openlibrary, "search_by_title_author", AsyncMock(return_value=[]))
    before = _snapshot(db)
    response = client.post("/api/intake/confirm", json={"books": [
        {"title": "First new book"}, {"title": "Second provider title", "isbn": "9780441172719"},
    ]})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "This identifier is unavailable", "added": [], "skipped": []}
    assert _snapshot(db) == before


@pytest.mark.parametrize("trashed", [False, True])
def test_book_provider_add_hidden_legacy_isbn_is_generic_and_has_no_effect(client, db, monkeypatch, trashed):
    from app.routers import items_common
    from app.services import item_write, libraries

    actor = _user(db)
    private = libraries.create_library(db, "Hidden legacy ISBN")
    hidden = _legacy_kids_item(db, "PRIVATE LEGACY TITLE", isbn="9780441172719", library_id=private["id"])
    if trashed:
        item_write.trash_item(db, hidden)
    _login(client, db, actor)
    monkeypatch.setattr(items_common, "_lookup_metadata", AsyncMock(return_value=(
        {"title": "Provider title"}, "openlibrary", {}, None)))
    before = _snapshot(db)
    response = client.post("/api/books/add", data={"isbn": "9780441172719", "media_type": "book"})
    assert response.status_code == 200
    assert "This identifier is unavailable" in response.text
    assert "PRIVATE LEGACY TITLE" not in response.text
    assert "Trash" not in response.text
    assert _snapshot(db) == before


@pytest.mark.parametrize("trashed", [False, True])
def test_book_provider_add_retains_existing_kids_book_type_and_id(client, db, monkeypatch, trashed):
    from app.routers import items_common
    from app.services import item_write

    actor = _user(db)
    target = _legacy_kids_item(db, "Retain this stored title", isbn="9780441172719", cover_path="covers/kept.jpg")
    if trashed:
        item_write.trash_item(db, target)
    _login(client, db, actor)
    lookup = AsyncMock(return_value=({"title": "Different provider title"}, "openlibrary", {}, None))
    monkeypatch.setattr(items_common, "_lookup_metadata", lookup)
    response = client.post("/api/books/add", data={"isbn": "9780441172719", "media_type": "book"})
    assert response.status_code == 200
    row = db.execute("SELECT id, media_type, title, deleted_at FROM items WHERE id = ?", (target,)).fetchone()
    assert tuple(row) == (target, "kids_book", "Retain this stored title", None)
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    if trashed:
        lookup.assert_awaited_once()
    else:
        lookup.assert_not_called()


def test_hardcover_neither_promotion_keeps_personal_wishlist_separate(client, db):
    from app.services import lists, user_state

    actor = _user(db)
    other = _user(db, "other-wishlist-person")
    target = _item(db, owned=0, wishlisted=False, hardcover_book_id=841)
    user_state.save_state(db, actor["id"], target, wishlist=1, reading_status="reading")
    user_state.save_state(db, other["id"], target, wishlist=0, reading_status="read")
    _login(client, db, actor)
    response = client.post("/api/hardcover/add-to-shelf", json={"title": "Provider title", "hardcover_book_id": 841})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "Added to wishlist", "item_id": target}
    assert lists.is_member(db, lists.WISHLIST, target)
    assert db.execute("SELECT owned FROM items WHERE id = ?", (target,)).fetchone()[0] == 0
    assert user_state.get_state(db, actor["id"], target)["wishlist"] == 1
    assert user_state.get_state(db, actor["id"], target)["reading_status"] == "reading"
    assert user_state.get_state(db, other["id"], target)["wishlist"] == 0
    assert user_state.get_state(db, other["id"], target)["reading_status"] == "read"


def test_valuation_worker_uses_only_preflighted_ids_in_empty_context(client, db, monkeypatch):
    from app.database import set_setting
    from app.services import catalogue_security, isbndb, libraries

    actor = _user(db)
    target = _item(db, isbn="9780441172719")
    private = libraries.create_library(db, "Do not price")
    hidden = _item(db, "Hidden price", isbn="9780441013593", library_id=private["id"])
    set_setting(db, "isbndb_api_key", "unit-test-price-key")
    _login(client, db, actor)
    seen = []

    async def lookup(isbn, key, http, cache):
        seen.append((catalogue_security.current_actor(), isbn))
        return {"price": 12.5}

    monkeypatch.setattr(isbndb, "lookup_price", lookup)
    monkeypatch.setattr(isbndb, "parse_price", lambda data: data["price"])
    monkeypatch.setattr(isbndb, "_load_cache", dict)
    monkeypatch.setattr(isbndb, "_save_cache", lambda cache: None)
    response = client.get("/api/valuate/stream")
    assert _events(response)[-1]["priced"] == 1
    assert seen == [(None, "9780441172719")]
    assert db.execute("SELECT estimated_value FROM items WHERE id = ?", (target,)).fetchone()[0] == 12.5
    assert db.execute("SELECT estimated_value FROM items WHERE id = ?", (hidden,)).fetchone()[0] is None
    assert db.execute("SELECT COUNT(*) FROM valuation_history").fetchone()[0] == 0


def test_import_cover_queue_keeps_library_binding_for_retained_kids_job(db, monkeypatch):
    import asyncio
    import httpx

    from app.routers import items_common
    from app.services import cover_queue, covers, libraries

    actor = _user(db)
    other = libraries.create_library(db, "Queue's new editable parent")
    libraries.set_membership(db, other["id"], actor["id"], "editor")
    target = _legacy_kids_item(db, isbn="9780441172719")
    db.commit()
    asyncio.run(items_common._enrich_import_covers([target], user=actor, library_ids={target: 1}))
    assert cover_queue.stats()["queued"] == 1
    libraries.assign_item(db, target, other["id"])
    db.commit()
    download = AsyncMock()
    monkeypatch.setattr(covers, "download_cover", download)

    async def process():
        async with httpx.AsyncClient() as http:
            return await cover_queue.process_one(http)

    assert asyncio.run(process()) is False
    download.assert_not_called()
    assert db.execute("SELECT cover_path, media_type FROM items WHERE id = ?", (target,)).fetchone()[:] == (None, "kids_book")
