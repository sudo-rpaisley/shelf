"""Native scanner protocols plus fork private-state/legacy identity seams.

These exercise real request authorization and writers. Only provider/network
boundaries are stubbed. Run after the integration candidate is frozen.
"""

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import _insert_borrower, _insert_item, _insert_location, _insert_node_location


ISBN = "9780590435062"
OTHER_ISBN = "9780439435062"
UPC5 = "07807300350143506"


def _found():
    from app.services import provider_result

    metadata = {"title": "Verified scanner book", "authors": "Test author"}
    return metadata, "openlibrary", {}, provider_result.found("openlibrary", metadata)


def _submit(client, surface, *, mode="wishlist", **extra):
    if surface == "store":
        return client.post("/api/store/queue", json={"isbns": [ISBN]})
    data = {"isbn": ISBN, "mode": mode, **extra}
    if surface == "manual":
        data["title"] = "A submitted title must not replace a restored title"
    return client.post("/api/items/manual" if surface == "manual" else "/api/scan", data=data)


def _status(response, surface):
    return response.json()["results"][0]["status"] if surface == "store" else response.context["status"]


def test_scan_wrapper_forwards_every_modern_parameter(editor_client, db):
    from app.routers import items

    location_id = _insert_location(db)
    db.commit()

    async def paused(request, *args):
        return request.app.state.templates.TemplateResponse(
            request, "fragments/scan_result.html", {"status": "error", "isbn": "paused"},
        )

    inner = AsyncMock(side_effect=paused)
    with patch.object(items, "_scan_isbn_inner", inner):
        response = editor_client.post("/api/scan", data={
            "isbn": UPC5, "media_type": "book", "location_id": str(location_id),
            "platform": "PC", "mode": "inventory", "borrower_id": "9",
            "legacy_confirm_isbn13": ISBN, "legacy_supplement": "43506",
            "tags": "carry me", "reading_status": "reading", "audit_id": "00017",
        })

    assert response.status_code == 200
    assert inner.await_count == 1
    assert inner.await_args.args[1:] == (
        UPC5, "book", location_id, "PC", "inventory", 9, ISBN, "43506", "carry me", "reading", 17,
    )
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0


def test_confirmed_legacy_scan_resolves_once_and_reuses_its_metadata(editor_client, editor_user, db):
    from app.services import legacy_book, user_state

    location_id = _insert_location(db)
    db.commit()
    metadata, source, hc_ids, cascade = _found()
    match = legacy_book.LegacyBookMatch(ISBN, metadata, source, hc_ids, cascade)
    other = legacy_book.LegacyBookMatch(OTHER_ISBN, {"title": "Other verified candidate"}, source, {}, cascade)
    resolution = legacy_book.LegacyBookResolution(
        "ambiguous", candidates=(ISBN, OTHER_ISBN), matches=(match, other),
    )
    verify = AsyncMock(return_value=resolution)
    lookup = AsyncMock(side_effect=AssertionError("The verified metadata must be reused"))
    with patch("app.routers.items._verify_legacy_book_barcode", verify), \
         patch("app.routers.items_common._lookup_metadata", lookup), \
         patch("app.routers.items.cover_queue.enqueue"):
        response = editor_client.post("/api/scan", data={
            "isbn": UPC5, "legacy_confirm_isbn13": ISBN, "mode": "wishlist",
            "location_id": str(location_id), "tags": "retained", "reading_status": "reading",
        })

    assert response.status_code == 200
    assert response.context["status"] == "wishlisted"
    verify.assert_awaited_once_with(UPC5)
    lookup.assert_not_awaited()
    row = db.execute("SELECT id, title, media_type, location_id, reading_status FROM items").fetchone()
    assert (row["title"], row["media_type"], row["location_id"], row["reading_status"]) == (
        metadata["title"], "book", location_id, None,
    )
    state = user_state.get_state(db, editor_user["id"], row["id"])
    assert (state["wishlist"], state["reading_status"]) == (1, "reading")
    assert db.execute(
        "SELECT t.name FROM tags t JOIN item_tags it ON it.tag_id = t.id WHERE it.item_id = ?", (row["id"],),
    ).fetchone()[0] == "retained"


@pytest.mark.parametrize("surface", ["scan", "manual", "store"])
@pytest.mark.parametrize("media_type", ["book", "kids_book"])
@pytest.mark.parametrize("deleted_at", [None, "2026-01-01 00:00:00"])
def test_hidden_isbn_refuses_without_500_disclosure_restore_or_private_write(
    editor_client, editor_user, db, surface, media_type, deleted_at,
):
    from app.services import libraries

    hidden_library = libraries.create_library(db, "Restricted scans")
    item_id = _insert_item(
        db, title="Undisclosed ISBN title", isbn=ISBN, media_type=media_type,
        _library_id=hidden_library["id"], deleted_at=deleted_at, owned=0,
    )
    db.commit()
    lookup = AsyncMock(side_effect=AssertionError("A hidden collision must stop before lookup"))
    with patch("app.routers.items_common._lookup_metadata", lookup):
        response = _submit(editor_client, surface)

    assert response.status_code == 200
    assert _status(response, surface) == ("invalid" if surface == "store" else "error")
    assert "This identifier is unavailable" in response.text
    assert "Undisclosed ISBN title" not in response.text
    assert "Restricted scans" not in response.text
    lookup.assert_not_awaited()
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (item_id,)).fetchone()[0] == deleted_at
    assert db.execute("SELECT library_id FROM library_items WHERE item_id = ?", (item_id,)).fetchone()[0] == hidden_library["id"]
    assert db.execute("SELECT COUNT(*) FROM user_item_state WHERE user_id = ?", (editor_user["id"],)).fetchone()[0] == 0


@pytest.mark.parametrize("surface", ["scan", "manual", "store"])
def test_existing_legacy_book_keeps_its_id_type_and_metadata(editor_client, db, surface):
    item_id = _insert_item(db, title="Original legacy book", isbn=ISBN, media_type="kids_book")
    db.commit()
    lookup = AsyncMock(side_effect=AssertionError("An exact legacy ISBN is already known"))
    with patch("app.routers.items_common._lookup_metadata", lookup):
        response = _submit(editor_client, surface)

    assert response.status_code == 200
    assert _status(response, surface) == "duplicate"
    lookup.assert_not_awaited()
    assert [tuple(row) for row in db.execute("SELECT id, title, media_type FROM items")] == [
        (item_id, "Original legacy book", "kids_book"),
    ]


@pytest.mark.parametrize("surface", ["scan", "manual", "store"])
def test_visible_old_new_isbn_twins_refuse_without_guessing(editor_client, db, surface):
    _insert_item(db, title="Old twin", isbn=ISBN, media_type="kids_book")
    _insert_item(db, title="New twin", isbn=ISBN, media_type="book")
    db.commit()
    response = _submit(editor_client, surface)

    assert response.status_code == 200
    assert _status(response, surface) == ("invalid" if surface == "store" else "error")
    assert "Old twin" not in response.text
    assert "New twin" not in response.text
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM user_item_state").fetchone()[0] == 0


@pytest.mark.parametrize("surface", ["scan", "manual", "store"])
def test_legacy_restore_uses_native_lifecycle_and_private_wishlist(
    editor_client, editor_user, viewer_user, db, surface,
):
    from app.services import item_write, lists, user_state

    item_id = _insert_item(db, title="Stored legacy title", isbn=ISBN, media_type="kids_book", owned=0)
    item_write.trash_item(db, item_id)
    db.commit()
    response = _submit(editor_client, surface, tags="restored tag", reading_status="reading")

    assert response.status_code == 200
    assert _status(response, surface) == "restored"
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    row = db.execute("SELECT id, title, media_type, deleted_at, reading_status FROM items").fetchone()
    assert tuple(row) == (item_id, "Stored legacy title", "kids_book", None, None)
    assert lists.is_member(db, lists.WISHLIST, item_id)
    assert user_state.get_state(db, editor_user["id"], item_id)["wishlist"] == 1
    assert user_state.get_state(db, viewer_user["id"], item_id)["persisted"] is False
    if surface != "store":
        assert user_state.get_state(db, editor_user["id"], item_id)["reading_status"] == "reading"
        assert db.execute("SELECT COUNT(*) FROM item_tags WHERE item_id = ?", (item_id,)).fetchone()[0] == 1


def test_error_card_with_item_id_does_not_become_a_personal_wish(editor_client, editor_user, db):
    from app.services import user_state

    item_id = _insert_item(db, isbn=ISBN, owned=0)
    db.commit()

    async def refuse(request, *args):
        return request.app.state.templates.TemplateResponse(
            request, "fragments/scan_result.html", {"status": "error", "item_id": item_id, "isbn": ISBN},
        )

    with patch("app.routers.items._scan_isbn_inner", new=AsyncMock(side_effect=refuse)):
        response = _submit(editor_client, "scan")
    assert response.status_code == 200
    assert user_state.get_state(db, editor_user["id"], item_id)["persisted"] is False


@pytest.mark.parametrize("media_type,code,word", [
    ("book", ISBN, "read"), ("kids_book", ISBN, "read"),
    ("dvd", "012569803121", "watched"), ("video_game", "012569803121", "played"),
])
def test_quick_rate_uses_native_media_language_and_only_the_actors_state(
    editor_client, editor_user, viewer_user, db, media_type, code, word,
):
    from app.services import upc, user_state

    is_book = media_type in {"book", "kids_book"}
    item_id = _insert_item(
        db, isbn=code if is_book else None, upc=None if is_book else upc.normalize_upc(code),
        media_type=media_type, reading_status="want_to_read",
    )
    db.commit()
    response = editor_client.post("/api/scan", data={"isbn": code, "mode": "quick_rate"})

    assert response.status_code == 200
    assert response.context["status"] == "marked_read"
    assert response.context["media_type"] == media_type
    assert response.context["message"] == f"Marked as {word}"
    assert "HX-Trigger" not in response.headers
    assert user_state.get_state(db, editor_user["id"], item_id)["reading_status"] == "read"
    assert len(user_state.get_reading_history(db, editor_user["id"], item_id)) == 1
    assert user_state.get_state(db, viewer_user["id"], item_id)["persisted"] is False
    assert db.execute("SELECT reading_status FROM items WHERE id = ?", (item_id,)).fetchone()[0] == "want_to_read"
    assert db.execute("SELECT COUNT(*) FROM reading_log").fetchone()[0] == 0


def test_quick_rate_in_trash_preserves_status_history_and_refusal_protocol(editor_client, editor_user, db):
    from app.services import user_state

    item_id = _insert_item(db, isbn=ISBN, deleted_at="2026-01-01 00:00:00")
    db.commit()
    response = editor_client.post("/api/scan", data={"isbn": ISBN, "mode": "quick_rate"})

    assert response.status_code == 200
    assert response.context["status"] == "in_trash"
    assert "HX-Trigger" not in response.headers
    assert user_state.get_state(db, editor_user["id"], item_id)["persisted"] is False
    assert user_state.get_reading_history(db, editor_user["id"], item_id) == []


def test_native_lend_return_cards_keep_statuses_and_log_after_the_write(editor_client, db):
    item_id = _insert_item(db, isbn=ISBN)
    borrower_id = _insert_borrower(db, "Scanner borrower")
    db.commit()

    def scan(mode):
        response = editor_client.post("/api/scan", data={"isbn": ISBN, "mode": mode, "borrower_id": str(borrower_id)})
        assert response.status_code == 200
        assert "HX-Trigger" not in response.headers
        return response.context["status"]

    assert scan("lend") == "checked_out"
    assert scan("lend") == "already_checked_out"
    assert scan("return") == "returned"
    assert scan("return") == "not_checked_out"
    assert db.execute("SELECT COUNT(*) FROM checkouts WHERE item_id = ?", (item_id,)).fetchone()[0] == 1
    assert db.execute("SELECT checked_in FROM checkouts WHERE item_id = ?", (item_id,)).fetchone()[0] is not None
    assert {row[0] for row in db.execute("SELECT result FROM scan_log WHERE item_id = ?", (item_id,))} == {
        "checked_out", "already_checked_out", "returned", "not_checked_out",
    }


def test_library_editor_with_global_viewer_role_can_scan_but_not_administer(viewer_client, viewer_user, db):
    from app.services import libraries

    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    _insert_item(db, isbn=ISBN)
    db.commit()
    response = viewer_client.post("/api/scan", data={"isbn": ISBN})
    assert response.status_code == 200
    assert response.context["status"] == "duplicate"
    assert viewer_client.post("/api/igdb/test-key", json={}).status_code == 403


def test_recent_scans_omit_hidden_codes_but_keep_visible_trash_logs(editor_client, db):
    from app.services import libraries, scan_history

    hidden_library = libraries.create_library(db, "Private scan logs")
    visible = _insert_item(db, isbn=None)
    trashed = _insert_item(db, isbn=None, deleted_at="2026-01-01 00:00:00")
    hidden = _insert_item(db, isbn=None, _library_id=hidden_library["id"])
    for item_id, raw in ((visible, "VISIBLE-CODE"), (trashed, "TRASH-CODE"), (hidden, "PRIVATE-CODE")):
        scan_history.record_scan(db, raw, "book", "duplicate", item_id)
    db.execute("INSERT INTO scan_log (isbn, media_type, result, mode) VALUES ('UNANCHORED-CODE', 'book', 'error', 'add')")
    db.commit()
    response = editor_client.get("/api/recent-scans")

    assert response.status_code == 200
    assert {row["item_id"] for row in response.context["recent_scans"]} == {visible, trashed}
    assert "PRIVATE-CODE" not in response.text
    assert "UNANCHORED-CODE" not in response.text


def test_shelf_fill_confirmation_keeps_node_and_all_scan_defaults(editor_client, db):
    from app.services import legacy_book

    node_id = _insert_node_location(db, "Confirmation shelf")
    db.commit()
    metadata, source, hc_ids, cascade = _found()
    matches = tuple(legacy_book.LegacyBookMatch(code, metadata, source, hc_ids, cascade) for code in (ISBN, OTHER_ISBN))
    with patch("app.routers.items._verify_legacy_book_barcode", new=AsyncMock(return_value=
               legacy_book.LegacyBookResolution("ambiguous", matches=matches))):
        response = editor_client.post("/api/shelf-fill/scan", data={
            "isbn": UPC5, "location_node_id": str(node_id), "platform": "PC",
            "tags": "carry me", "reading_status": "reading",
        })

    assert response.status_code == 200
    context = response.context
    assert context["status"] == "legacy_ambiguous"
    assert context["scan_action"] == "/api/shelf-fill/scan"
    # The explicit continuation is a NODE, not an unrelated flat location ID.
    assert context["location_id"] is None
    assert context["location_node_id"] == node_id
    assert context["shelf_fill_origin"] == "shelf_fill"
    assert (context["platform"], context["tags"], context["reading_status"]) == ("PC", "carry me", "reading")
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_store_cover_download_cannot_overwrite_after_edit_access_is_revoked(editor_client, editor_user, db):
    from pathlib import Path

    import httpx

    from app.database import get_db
    from app.routers import items_common
    from app.services import covers, lists, user_state

    real_stage = items_common.CoverStage
    stages = []
    revoked = []
    snapshots = []
    image = (Path(__file__).parent / "fixtures/intake/eleven_books.jpg").read_bytes()
    assert covers.validate_uploaded_cover(image)

    class RecordedStage(real_stage):
        def __init__(self, item_id):
            super().__init__(item_id)
            stages.append(self)

    async def revoke_during_download(staged_id, isbn, cover_url, cover_id, client, **kwargs):
        stage = stages[0]
        assert staged_id == stage.download_id and staged_id < 0
        assert (isbn, cover_url, cover_id, kwargs) == (ISBN, None, None, {"hardcover_cover_url": None})
        pending = covers.stage_uploaded_cover(stage, image, content_type="image/jpeg")
        assert pending == f"covers/{staged_id}.jpg" and stage.owns(pending)
        assert stage.path_for(".jpg").read_bytes() == image
        with get_db() as connection:
            assert connection is not db
            changed = connection.execute(
                "UPDATE library_memberships SET role = 'viewer' WHERE library_id = 1 AND user_id = ?",
                (editor_user["id"],),
            )
            assert changed.rowcount == 1
        with get_db() as observed:
            assert observed.execute(
                "SELECT role FROM library_memberships WHERE library_id = 1 AND user_id = ?", (editor_user["id"],),
            ).fetchone()[0] == "viewer"
            # Store committed its item/shared/private wishlist before downloading.
            # Denial must preserve that state AFTER the intentional revocation.
            snapshots.append(tuple(observed.iterdump()))
        revoked.append(editor_user["id"])
        return pending

    download = AsyncMock(side_effect=revoke_during_download)
    with patch.object(items_common, "CoverStage", RecordedStage), \
         patch("app.routers.items_common._lookup_metadata", new=AsyncMock(return_value=_found())), \
         patch("app.routers.store.covers.download_cover", new=download):
        response = _submit(editor_client, "store")

    download.assert_awaited_once()
    assert revoked == [editor_user["id"]] and len(snapshots) == 1
    assert len(stages) == 1 and isinstance(stages[0], real_stage)
    stage = stages[0]
    provider_client = download.await_args.args[4]
    assert isinstance(provider_client, httpx.AsyncClient) and provider_client.is_closed
    download.assert_awaited_once_with(stage.download_id, ISBN, None, None, provider_client, hardcover_cover_url=None)
    assert response.status_code == 403
    item_id = db.execute("SELECT id FROM items WHERE isbn = ?", (ISBN,)).fetchone()[0]
    assert item_id == stage.item_id
    assert not (covers.COVERS_DIR / f"{item_id}.jpg").exists()
    for suffix in stage.SUFFIXES:
        assert not stage.path_for(suffix).exists() and not stage.path_for(suffix).is_symlink()
    assert list(covers.COVERS_DIR.iterdir()) == []
    assert db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()[0] is None
    assert lists.is_member(db, lists.WISHLIST, item_id)
    assert user_state.get_state(db, editor_user["id"], item_id)["wishlist"] == 1
    assert tuple(db.iterdump()) == snapshots[0]
