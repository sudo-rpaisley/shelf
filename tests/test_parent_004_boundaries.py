"""Source-authored integration regressions; execute only after the C004 freeze."""

from html import unescape
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.conftest import _insert_item, _seed_personal_state, _insert_personal_history


def _dump(db):
    return list(db.iterdump())


def _domains(db):
    db.execute("INSERT INTO locations(id,name) VALUES (10,'Flat A'), (20,'Flat B')")
    db.execute("INSERT INTO location_nodes(id,name,legacy_location_id) "
               "VALUES (100,'Node A',10), (10,'Node B',20)")
    db.execute("INSERT INTO location_nodes(id,parent_id,name) VALUES (101,100,'Precise shelf')")


def _options(html, element_id):
    control = re.search(r'<select\b[^>]*\bid="' + re.escape(element_id) + r'"[^>]*>(.*?)</select>', html, re.S)
    assert control is not None, element_id
    return {value: unescape(re.sub(r"<[^>]+>", "", label).strip()) for value, label in
            re.findall(r'<option\b[^>]*value="([^"]*)"[^>]*>(.*?)</option>', control[1], re.S) if value}


def test_scan_and_settings_keep_node_and_flat_controls_distinct(admin_client, db):
    _domains(db)
    db.commit()
    before = _dump(db)
    scan = admin_client.get("/scan")
    assert scan.status_code == 200
    assert _options(scan.text, "location") == {"10": "Flat A", "20": "Flat B"}
    assert _options(scan.text, "inventory-location") == {
        "100": "Node A", "101": "Node A › Precise shelf", "10": "Node B",
    }
    settings = admin_client.get("/settings")
    assert settings.status_code == 200
    assert {row["id"]: row["name"] for row in settings.context["locations"]} == {10: "Flat A", 20: "Flat B"}
    assert {row["id"]: row["name"] for row in settings.context["location_nodes"]} == {
        100: "Node A", 101: "Node A / Precise shelf", 10: "Node B",
    }
    assert _dump(db) == before


def test_home_projects_shared_wishlist_and_uses_core_music_fallback(editor_client, editor_user, db):
    from app import features
    from app.services import libraries

    member = _insert_item(db, title="Shared acquisition", isbn=None, owned=0, wishlisted=True)
    personal = _insert_item(db, title="Personal only", isbn=None, owned=0,
                            personal_user_id=editor_user["id"], personal_state={"wishlist": 1})
    bare = _insert_item(db, title="Neither", isbn=None, owned=0)
    private = libraries.create_library(db, "Hidden Home library")["id"]
    hidden = _insert_item(db, title="HIDDEN HOME", isbn=None, _library_id=private)
    features.set_feature_enabled(db, "music", False)
    db.commit()
    response = editor_client.get("/")
    assert response.status_code == 200
    recent = {row["id"]: row["wishlisted"] for row in response.context["recent_items"]}
    assert recent == {member: 1, personal: 0, bare: 0}
    assert hidden not in recent and "HIDDEN HOME" not in response.text
    music = next(row for row in response.context["families"] if row["key"] == "music")
    assert music["href"] == "/browse?media_family_filter=music"
    features.set_feature_enabled(db, "music", True)
    db.commit()
    enabled = editor_client.get("/")
    assert next(row for row in enabled.context["families"] if row["key"] == "music")["href"] == "/music"


@pytest.mark.parametrize("kind", ["missing", "hidden", "trash"])
def test_native_edit_get_unavailable_targets_are_the_same_307(editor_client, db, kind):
    from app.services import libraries, item_write

    target = 999999
    if kind != "missing":
        library = libraries.create_library(db, "Hidden edit GET")["id"] if kind == "hidden" else 1
        target = _insert_item(db, title="Do not expose this edit target", isbn=None, _library_id=library)
        if kind == "trash":
            item_write.trash_item(db, target)
    db.commit()
    before = _dump(db)
    response = editor_client.get(f"/item/{target}/edit", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/browse"
    assert "Do not expose" not in response.text
    assert _dump(db) == before


def test_game_destination_authority_refuses_before_igdb(editor_client, db, monkeypatch):
    from app.routers import items_catalog
    from app.services import libraries

    private = libraries.create_library(db, "Not an IGDB destination")["id"]
    db.commit()
    lookup = AsyncMock(side_effect=AssertionError("unavailable destination must precede provider work"))
    monkeypatch.setattr(items_catalog.igdb, "lookup_game", lookup)
    before = _dump(db)
    response = editor_client.post("/api/games/add", data={"igdb_id": "42", "library_id": private})
    assert response.status_code == 404
    lookup.assert_not_awaited()
    assert _dump(db) == before


def test_audnexus_hidden_and_missing_have_the_same_generic_toast(editor_client, db, monkeypatch):
    from app.routers import items_catalog
    from app.services import libraries

    private = libraries.create_library(db, "Hidden Audible library")["id"]
    hidden = _insert_item(db, title="Private Audible target", isbn=None, _library_id=private,
                          media_type="audiobook", asin="B012345678")
    db.commit()
    lookup = AsyncMock(side_effect=AssertionError("hidden and missing must not reach Audible"))
    monkeypatch.setattr(items_catalog.audnexus, "lookup", lookup)
    before = _dump(db)
    responses = [editor_client.post(f"/api/items/{target}/fill-from-audible") for target in (hidden, 999999)]
    assert [response.status_code for response in responses] == [404, 404]
    assert responses[0].content == responses[1].content == b""
    assert responses[0].headers["hx-trigger"] == responses[1].headers["hx-trigger"]
    assert "Item not found" in responses[0].headers["hx-trigger"]
    lookup.assert_not_awaited()
    assert _dump(db) == before


@pytest.mark.parametrize("trashed", [False, True])
@pytest.mark.parametrize("copies", [0, 1])
def test_explicit_admin_recovery_changes_only_mapping(admin_client, admin_user, db, trashed, copies):
    from app.services import item_copies, item_write, libraries

    target = _insert_item(db, title='<script>orphan recovery</script>', isbn=None, _library_id=None)
    if copies:
        item_copies.add_copy(db, target, {"copy_barcode": "RECOVERY-COPY", "condition": "good"})
    _seed_personal_state(db, admin_user["id"], target, rating=4, personal_notes="Retain private identity")
    _insert_personal_history(db, admin_user["id"], target, date_finished="2026-11-01")
    if trashed:
        item_write.trash_item(db, target)
    destination = libraries.create_library(db, "Recovered library")["id"]
    db.commit()
    before = _dump(db)
    assert not libraries.has_item_role(db, admin_user, target, "viewer")
    assert libraries.can_recover_item(db, admin_user, target)
    assert admin_client.get(f"/item/{target}", follow_redirects=False).status_code == 307
    page = admin_client.get("/settings/library-recovery")
    assert page.status_code == 200
    assert '<script>orphan recovery</script>' not in page.text
    assert '&lt;script&gt;orphan recovery&lt;/script&gt;' in page.text
    assert f'data-recovery-item="{target}"' in page.text
    assert _dump(db) == before
    response = admin_client.post(f"/api/libraries/recovery/items/{target}", data={"library_id": destination},
                                 follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/settings/library-recovery"
    assert libraries.item_library_id(db, target) == destination
    after = _dump(db)
    added = [line for line in after if line not in before]
    removed = [line for line in before if line not in after]
    created = db.execute("SELECT quote(created_at) FROM library_items WHERE item_id=?", (target,)).fetchone()[0]
    assert created != "NULL"
    assert added == [f'INSERT INTO "library_items" VALUES({target},{destination},{created});']
    assert removed == []
    assert not libraries.can_recover_item(db, admin_user, target)
    repeated = admin_client.post(f"/api/libraries/recovery/items/{target}", data={"library_id": 1})
    assert repeated.status_code == 409 and _dump(db) == after


@pytest.mark.parametrize("kind", ["bearer", "viewer", "demoted", "version", "deleted", "other-actor"])
def test_recovery_never_promotes_a_captured_principal(admin_user, editor_user, db, kind):
    from fastapi import HTTPException
    from app.services import libraries, catalogue_security

    target = _insert_item(db, title="Unmapped capability target", isbn=None, _library_id=None)
    principal = {**admin_user, "token_version": 1}
    if kind == "bearer":
        from app.services import api_tokens

        issued = api_tokens.create(db, admin_user["id"], "Recovery cannot elevate me")
        db.commit()
        principal = api_tokens.resolve(issued["token"])
        assert principal["role"] == "viewer" and principal["account_role"] == "admin"
    elif kind == "viewer":
        principal.update(role="viewer", account_role="admin")
    elif kind == "demoted":
        db.execute("UPDATE users SET role='viewer' WHERE id=?", (admin_user["id"],))
    elif kind == "version":
        db.execute("UPDATE users SET token_version=token_version+1 WHERE id=?", (admin_user["id"],))
    elif kind == "deleted":
        db.execute("DELETE FROM users WHERE id=?", (admin_user["id"],))
    db.commit()
    before = _dump(db)
    token = (catalogue_security.set_actor(editor_user) if kind == "other-actor" else
             catalogue_security.set_actor(principal) if kind == "bearer" else None)
    try:
        assert libraries.can_recover_item(db, principal, target) is False
        if kind == "bearer":
            assert libraries.can_recover_item(db, {**principal, "role": "admin"}, target) is False
        with pytest.raises(HTTPException) as refusal:
            libraries.recover_item(db, principal, target, 1)
        assert refusal.value.status_code == 403
    finally:
        if token is not None:
            catalogue_security.reset_actor(token)
    assert _dump(db) == before


@pytest.mark.parametrize("fixture", ["viewer_client", "editor_client"])
def test_recovery_http_rejects_non_admin_without_disclosure(request, db, fixture):
    client = request.getfixturevalue(fixture)
    target = _insert_item(db, title="SECRET UNMAPPED", isbn=None, _library_id=None)
    db.commit()
    before = _dump(db)
    response = client.post(f"/api/libraries/recovery/items/{target}", data={"library_id": 1})
    assert response.status_code == 403
    assert "SECRET UNMAPPED" not in response.text
    assert _dump(db) == before


def test_trusted_trash_updates_reindex_without_lifecycle_or_copy_effects(admin_client, db):
    from app.services import item_write, item_copies

    dead = item_write.insert_item(db, title="Trash metadata", authors="Before", isbn=None)
    live = item_write.insert_item(db, title="Live metadata", authors="Before", isbn=None)
    item_write.trash_item(db, dead, at="2026-11-01 12:00:00")
    copies = [tuple(row) for row in db.execute("SELECT * FROM item_copies ORDER BY id")]
    assert len(copies) == 2
    item_write.update_item_fields(db, dead, {"authors": "After Single"})
    assert db.execute("SELECT authors FROM items WHERE id=?", (dead,)).fetchone()[0] == "After Single"
    item_write.update_items_fields(db, [dead, live, 999999], {"authors": "After Bulk"})
    assert [row[0] for row in db.execute("SELECT authors FROM items ORDER BY id")] == ["After Bulk", "After Bulk"]
    for target in (dead, live):
        assert [row[0] for row in db.execute(
            "SELECT a.name_key FROM item_authors ia JOIN authors a ON a.id=ia.author_id WHERE ia.item_id=?", (target,)
        )] == ["after bulk"]
    assert db.execute("SELECT deleted_at FROM items WHERE id=?", (dead,)).fetchone()[0] == "2026-11-01 12:00:00"
    assert [tuple(row) for row in db.execute("SELECT * FROM item_copies ORDER BY id")] == copies
    assert item_copies.copies_for_item(db, dead) == []
    db.commit()
    before = _dump(db)
    response = admin_client.post(f"/api/items/{dead}/edit", data={"title": "Forbidden HTTP edit"})
    assert response.status_code == 404
    assert _dump(db) == before
    with pytest.raises(ValueError):
        item_write.update_item_fields(db, dead, {"deleted_at": None})
    assert _dump(db) == before


def test_legacy_missing_report_uses_scoped_native_node_census_and_never_writes(editor_client, editor_user, db):
    from app.services import item_copies, item_write, libraries

    _domains(db)
    item = _insert_item(db, title='<script>visible missing</script>', authors='A & B', isbn=None, location_id=10)
    first = item_copies.add_copy(db, item, {"location_id": 101})
    second = item_copies.add_copy(db, item, {"location_id": 101})
    # A live copy elsewhere suppresses the stale FLAT seam fallback at this node.
    elsewhere = _insert_item(db, title="Wrong shelf", isbn=None, location_id=10)
    item_copies.add_copy(db, elsewhere, {"location_id": 10})
    zero = _insert_item(db, title="Copy-less mapped root", isbn=None, location_id=10)
    hidden_library = libraries.create_library(db, "Hidden inventory")["id"]
    hidden = _insert_item(db, title="PRIVATE MISSING", isbn=None, _library_id=hidden_library)
    item_copies.add_copy(db, hidden, {"location_id": 101})
    dead = _insert_item(db, title="TRASH MISSING", isbn=None)
    item_copies.add_copy(db, dead, {"location_id": 101})
    item_write.trash_item(db, dead)
    db.commit()
    before = _dump(db)
    response = editor_client.post("/api/inventory/missing", data={"location_id": "101", "scanned_ids": "junk,-1"})
    assert response.status_code == 200
    assert [row["item_id"] for row in response.context["missing_items"]] == [item]
    assert response.text.count(f'data-missing-item="{item}"') == 1
    assert '<script>visible missing</script>' not in response.text
    assert '&lt;script&gt;visible missing&lt;/script&gt;' in response.text
    assert 'A &amp; B' in response.text and 'PRIVATE MISSING' not in response.text and 'TRASH MISSING' not in response.text
    accounted = editor_client.post("/api/inventory/missing", data={"location_id": "101", "scanned_ids": str(item)})
    assert accounted.context["missing_items"] == []
    assert "accounted for" in accounted.text
    root = editor_client.post("/api/inventory/missing", data={"location_id": "100"})
    assert [row["item_id"] for row in root.context["missing_items"]] == [zero]
    for location in ("99999", "not-a-node"):
        missing = editor_client.post("/api/inventory/missing", data={"location_id": location})
        assert missing.status_code == 404 and missing.text == "Location not found"
    assert db.execute("SELECT COUNT(*) FROM item_copies WHERE id IN (?,?)", (first, second)).fetchone()[0] == 2
    assert _dump(db) == before


@pytest.mark.parametrize("rebind", [False, True])
def test_store_cover_uses_real_owned_stage_and_parent_binding(admin_client, admin_user, db, monkeypatch, rebind):
    from app import config
    from app.routers import items_common
    from app.services import covers, libraries

    destination = libraries.create_library(db, "Moved while Store downloads")["id"]
    db.commit()
    metadata = {"title": "Store staged item", "authors": "Real Author"}
    monkeypatch.setattr(items_common, "_lookup_metadata", AsyncMock(return_value=(metadata, "openlibrary", {}, False)))
    real_stage = items_common.CoverStage
    real_store = items_common.store_item_cover
    stages, downloads, publications = [], [], []

    def store_bound(connection, item_id, cover_path, **kwargs):
        principal = dict(connection.execute(
            "SELECT id,username,role,display_name,token_version,language FROM users WHERE id=?",
            (admin_user["id"],),
        ).fetchone())
        assert kwargs["user"] == principal
        assert kwargs["library_ids"] == {item_id: 1}
        publications.append(item_id)
        return real_store(connection, item_id, cover_path, **kwargs)

    monkeypatch.setattr(items_common, "store_item_cover", store_bound)

    class RecordedStage(real_stage):
        def __init__(self, item_id):
            super().__init__(item_id)
            stages.append(self)

    monkeypatch.setattr(items_common, "CoverStage", RecordedStage)
    image = (Path(__file__).parent / "fixtures/intake/eleven_books.jpg").read_bytes()
    assert covers.validate_uploaded_cover(image)

    async def download(download_id, isbn, cover_url, cover_id, client, **kwargs):
        from app.database import get_db

        stage = stages[0]
        assert download_id == stage.download_id and stage.item_id > 0
        assert (isbn, cover_url, cover_id, kwargs) == ("9780441013593", None, None, {"hardcover_cover_url": None})
        downloads.append(download_id)
        config.COVERS_DIR.mkdir(parents=True, exist_ok=True)
        stage.path.write_bytes(image)
        if rebind:
            with get_db() as connection:
                libraries.assign_item(connection, stage.item_id, destination)
            (config.COVERS_DIR / f"{stage.item_id}.jpg").write_bytes(b"retained final bytes")
        return f"covers/{download_id}.jpg"

    monkeypatch.setattr(covers, "download_cover", download)
    response = admin_client.post("/api/store/queue", json={"isbns": ["9780441013593"]})
    assert len(stages) == 1 and downloads == [stages[0].download_id]
    stage = stages[0]
    assert not stage.path.exists()
    row = db.execute("SELECT id, cover_path FROM items WHERE id=?", (stage.item_id,)).fetchone()
    assert row is not None
    final = config.COVERS_DIR / f"{stage.item_id}.jpg"
    assert list(config.COVERS_DIR.iterdir()) == [final]
    if rebind:
        assert response.status_code == 404
        assert publications == []
        assert row["cover_path"] is None and final.read_bytes() == b"retained final bytes"
        assert libraries.item_library_id(db, stage.item_id) == destination
    else:
        assert response.status_code == 200 and response.json()["results"][0]["status"] == "wishlisted"
        assert publications == [stage.item_id]
        assert row["cover_path"] == f"covers/{stage.item_id}.jpg" and final.read_bytes() == image
        assert libraries.item_library_id(db, stage.item_id) == 1
