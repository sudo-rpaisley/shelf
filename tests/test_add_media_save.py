"""Native Add/Wishlist saves from real Add-host selection and review producers."""

import copy
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest

from app import config, database
from app.routers import items_common
from app.services import discogs, discogs_selection, igdb, item_write, lists, music_catalog, music_review, musicbrainz
from app.services import tags as tags_svc
from tests.conftest import _insert_location
from tests.test_add_media_search import (
    BOOK, GAME, ISBN, MBID, MOVIE, MUSIC_FORMATS, Forms, catalogue, clock, credentials,
    error, form, io, no_cookie, page, pairs, post, preview, release, replace, search, snapshot, value,
)
from app.services import provider_result


def prepared(client, family="book", mode="add", **changes):
    media_type = changes.pop("media_type", "book" if family == "manual" else family)
    values = {"mode": mode, "media_type": media_type, "q": "Selected query", **changes}
    if family == "manual":
        searched = search(client, method="manual", **values)
        return form(searched, "add-media-manual")
    searched = search(client, **values)
    reviewed = preview(client, searched)
    return form(reviewed, "add-media-save")


def submit(client, selected, **changes):
    return post(client, selected["attrs"]["action"], replace(pairs(selected), **changes))


def receipt(client, response, user_id):
    assert response.status_code == 303, (response.status_code, response.text[:1500])
    no_cookie(response)
    assert "HX-Redirect" not in response.headers and "Refresh" not in response.headers
    target = urlsplit(response.headers["location"])
    assert target.path == "/add" and target.netloc in ("", "testserver")
    query = parse_qs(target.query, strict_parsing=True)
    assert set(query) == {"receipt"} and len(query["receipt"]) == 1
    token = query["receipt"][0]
    assert token.isascii() and len(token) <= 4096
    music_review.verify(token, user_id=user_id, purpose="shelf-add-receipt-v1")
    got = client.get(response.headers["location"], follow_redirects=False)
    assert got.status_code == 200 and 'data-testid="add-media-receipt"' in got.text
    no_cookie(got)
    actual = got.context["add"]["receipt"]
    assert actual is not None
    return got, actual


def stored(db, item_id):
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row is not None
    return dict(row)


def ownership(db, item_id):
    return stored(db, item_id)["owned"], lists.is_member(db, lists.WISHLIST, item_id)


def assert_row_receipt(actual, row):
    assert actual.item_id == row["id"]
    assert actual.actual_media_type == row["media_type"]
    assert actual.title == row["title"] and actual.authors == row["authors"]
    assert actual.cover_path == row["cover_path"] and actual.source == row["source"]


@pytest.mark.parametrize("media_type", ["book", "audiobook", "ebook", "comic", "manga"])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_bibliographic_native_insert_preserves_mapping_and_initial_ownership(
    editor_client, editor_user, db, catalogue, media_type, mode,
):
    location = _insert_location(db, "Add Shelf")
    db.commit()
    selected = prepared(editor_client, media_type, mode)
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    catalogue.clear()
    response = submit(editor_client, selected, location_id=location, tags="Signed; Science Fiction", reading_status="reading")
    got, actual = receipt(editor_client, response, editor_user["id"])
    row = stored(db, actual.item_id)
    assert actual.status == ("added" if mode == "add" else "wishlisted") and actual.actual_mode == mode
    assert_row_receipt(actual, row)
    assert ownership(db, actual.item_id) == ((1, False) if mode == "add" else (0, True))
    assert row["isbn"] == ISBN and row["isbn10"] == "0441172717" and row["media_type"] == media_type
    for key in ("title", "subtitle", "authors", "publisher", "publish_year", "page_count", "description", "series_name", "series_position", "language"):
        assert row[key] == BOOK[key]
    assert row["location_id"] == location and row["source"] == "openlibrary" and row["cover_path"] is None
    assert row["reading_status"] == "reading"
    assert {tag["name"] for tag in tags_svc.get_item_tags(db, row["id"])} == {"Signed", "Science Fiction"}
    copy_row = db.execute("SELECT location_id FROM item_copies WHERE item_id = ? AND copy_number = 1", (row["id"],)).fetchone()
    assert copy_row is not None and copy_row[0] == location
    assert "Dune &lt;edition&gt;" in got.text and "Dune <edition>" not in got.text
    assert catalogue.calls == [], "Save/receipt must not rediscover provider details or request artwork"


@pytest.mark.parametrize("family", ["dvd", "video_game"])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_game_movie_receipt_describes_stored_native_fields_not_preview_only_identity(
    editor_client, editor_user, db, catalogue, family, mode,
):
    changes = {"platform": "switch"} if family == "video_game" else {}
    selected = prepared(editor_client, family, mode, **changes)
    catalogue.clear()
    got, actual = receipt(editor_client, submit(editor_client, selected, tags="Favourite", reading_status="want_to_read"), editor_user["id"])
    row = stored(db, actual.item_id)
    assert_row_receipt(actual, row)
    assert actual.status == ("added" if mode == "add" else "wishlisted") and actual.actual_mode == mode
    assert ownership(db, actual.item_id) == ((1, False) if mode == "add" else (0, True))
    assert row["authors"] is None and row["isbn"] is None and row["upc"] is None
    assert row["source"] == ("igdb" if family == "video_game" else "tmdb")
    assert row["description"] == (GAME if family == "video_game" else MOVIE)["description"]
    assert row["reading_status"] == "want_to_read" and row["cover_path"] is None
    assert "Preview-only Developer" not in got.text
    assert "igdb_id" not in row and "tmdb_id" not in row
    if family == "video_game":
        assert row["platform"] == "switch" and row["publisher"] == GAME["publisher"] and row["series_name"] == GAME["series_name"]
    assert catalogue.calls == []


@pytest.mark.parametrize("media_type", MUSIC_FORMATS)
@pytest.mark.parametrize("provider", ["musicbrainz", "discogs"])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_music_selected_fields_exact_pressing_ownership_and_native_track_semantics(
    editor_client, editor_user, db, catalogue, media_type, provider, mode,
):
    observation = release(provider, media_type)
    if provider == "musicbrainz":
        catalogue.install(musicbrainz, "search_releases", provider_result.found(provider, [observation]))
        catalogue.install(musicbrainz, "lookup_release", provider_result.found(provider, observation))
    else:
        catalogue.install(discogs, "search_public_releases", provider_result.found(provider, [observation]))
        catalogue.install(discogs, "lookup_public_release", provider_result.found(provider, observation))
    searched = search(editor_client, mode=mode, media_type=media_type, provider=provider, q="Hits Out of Hell", artist="Meat Loaf")
    reviewed = preview(editor_client, searched)
    assert "Provider track fact" in reviewed.text and "EPC 40-26156" in reviewed.text
    assert "/api/music/preview-image" not in reviewed.text and '<img src="https://' not in reviewed.text
    selected = form(reviewed, "add-media-save")
    ticket = value(selected, "ticket")
    payload = music_review.verify(ticket, user_id=editor_user["id"], purpose="shelf-add-music-review-v1")
    assert payload["item"] == 0 and payload["fingerprint"] == "new"
    assert payload["candidate"]["add_media_intent"] == {
        "mode": mode, "media_type": media_type, "method": "title", "query": "Hits Out of Hell",
        "artist": "Meat Loaf", "title_hint": "", "platform": "", "country": "", "music_provider": provider,
    }
    assert payload["candidate"].get("images", []) == []
    assert not any(control.get("name") in {"owned", "image", "item_id"} for control in selected["controls"])
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    catalogue.clear()
    got, actual = receipt(editor_client, submit(editor_client, selected, fields=["title", "authors"], tags="Rock", reading_status=""), editor_user["id"])
    row = stored(db, actual.item_id)
    assert_row_receipt(actual, row)
    assert actual.status == ("added" if mode == "add" else "wishlisted") and actual.actual_mode == mode
    assert row["title"] == "Hits Out of Hell" and row["authors"] == "Meat Loaf" and row["media_type"] == media_type
    assert row["publisher"] is None and row["publish_year"] is None and row["upc"] is None
    assert row["cover_path"] is None and row["reading_status"] is None
    assert ownership(db, row["id"]) == ((1, False) if mode == "add" else (0, True))
    assert [tag["name"] for tag in tags_svc.get_item_tags(db, row["id"])] == ["Rock"]
    native = music_catalog.get_release(db, row["id"])
    if provider == "musicbrainz":
        assert native["musicbrainz_release_id"] == MBID
        assert native["media"][0]["tracks"][0]["title"] == "Provider track fact"
    else:
        assert discogs_selection.get_selected_release_id(db, row["id"]) == 77
        assert native["musicbrainz_release_id"] is None and native["media"] == []
        assert db.execute("SELECT COUNT(*) FROM music_tracks").fetchone()[0] == 0
        assert "canonical" not in got.text.casefold() or "not" in got.text.casefold()
    assert catalogue.calls == []


@pytest.mark.parametrize("media_type", tuple(config.MEDIA_TYPES))
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_manual_every_canonical_format_uses_signed_mode_and_native_funnel(editor_client, editor_user, db, io, media_type, mode):
    selected = prepared(editor_client, "manual", mode, media_type=media_type)
    response = submit(editor_client, selected, title="A manually entered title", authors="A manual creator",
                      publisher="A manual publisher", publish_year="2001", series_name="A series", language="it",
                      identifier_kind="none", identifier="", platform="switch" if media_type == "video_game" else "")
    got, actual = receipt(editor_client, response, editor_user["id"])
    row = stored(db, actual.item_id)
    assert_row_receipt(actual, row)
    assert actual.status == ("added" if mode == "add" else "wishlisted") and actual.actual_mode == mode
    assert row["title"] == "A manually entered title" and row["authors"] == "A manual creator"
    assert row["publisher"] == "A manual publisher" and row["publish_year"] == 2001
    assert row["series_name"] == "A series" and row["language"] == "it" and row["source"] == "manual"
    assert row["isbn"] is None and row["upc"] is None and row["media_type"] == media_type
    assert ownership(db, row["id"]) == ((1, False) if mode == "add" else (0, True)) and io.calls == []


def seed_duplicate(db, family, state):
    media_type = "book" if family == "manual" else family
    title = BOOK["title"] if media_type == "book" else (GAME if family == "video_game" else MOVIE)["title"]
    extra = {"isbn": ISBN} if media_type == "book" else {}
    if family == "video_game":
        extra["platform"] = "switch"
    item_id = item_write.insert_item(db, title=title, authors="Stored local creator", media_type=media_type,
                                     publisher="Keep local publisher", source="manual", reading_status="read",
                                     owned=1 if state == "owned" else 0, wishlisted=state == "wishlist", **extra)
    tags_svc.attach_tags(db, item_id, ["Keep local tag"])
    db.commit()
    return item_id


@pytest.mark.parametrize("family", ["book", "video_game", "dvd", "manual"])
@pytest.mark.parametrize("state", ["owned", "neither", "wishlist"])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_live_duplicate_matrix_only_add_promotes_existing_wishlist_and_never_overwrites(
    editor_client, editor_user, db, catalogue, family, state, mode,
):
    item_id = seed_duplicate(db, family, state)
    before_row = stored(db, item_id)
    before_copies = [tuple(row) for row in db.execute("SELECT * FROM item_copies")]
    selected = prepared(editor_client, family, mode, **({"platform": "switch"} if family == "video_game" else {}))
    changes = {"tags": "Do not apply", "reading_status": "reading"}
    if family == "manual":
        changes.update(title="Do not replace title", identifier_kind="isbn", identifier=ISBN, authors="Do not replace creator")
    catalogue.clear()
    got, actual = receipt(editor_client, submit(editor_client, selected, **changes), editor_user["id"])
    promoted = mode == "add" and state == "wishlist"
    assert actual.status == ("promoted" if promoted else "duplicate") and actual.item_id == item_id
    expected = (1, False) if promoted or state == "owned" else (0, state == "wishlist")
    assert ownership(db, item_id) == expected
    after = stored(db, item_id)
    for key in before_row.keys() - {"owned", "updated_at"}:
        assert after[key] == before_row[key], key
    assert_row_receipt(actual, after)
    assert [tag["name"] for tag in tags_svc.get_item_tags(db, item_id)] == ["Keep local tag"]
    assert [tuple(row) for row in db.execute("SELECT * FROM item_copies")] == before_copies
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1 and catalogue.calls == []


@pytest.mark.parametrize("family", ["book", "manual"])
@pytest.mark.parametrize("state", ["owned", "neither", "wishlist"])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_identifier_trash_restore_preserves_facts_copies_keys_art_and_forward_only_ownership(
    editor_client, editor_user, db, catalogue, family, state, mode,
):
    location = _insert_location(db, "Original location")
    item_id = item_write.insert_item(db, title="Stored restore title", authors="Stored restore author", isbn=ISBN,
                                     media_type="book", location_id=location, source="hardcover", hardcover_book_id=91,
                                     hardcover_edition_id=92, cover_path="covers/kept.jpg", notes="Preserve local notes",
                                     owned=1 if state == "owned" else 0, wishlisted=state == "wishlist", reading_status="read")
    db.execute("INSERT INTO item_copies (item_id, copy_number, location_id, condition, acquisition_source) VALUES (?,2,?,'VG+','Local shop')",
               (item_id, location))
    tags_svc.attach_tags(db, item_id, ["Original"])
    (config.COVERS_DIR / "kept.jpg").write_bytes(b"stored artwork bytes")
    item_write.trash_item(db, item_id)
    db.commit()
    before = stored(db, item_id)
    copies = [tuple(row) for row in db.execute("SELECT * FROM item_copies ORDER BY id")]
    selected = prepared(editor_client, family, mode)
    changes = {"tags": "Restored tag", "reading_status": "reading"}
    if family == "manual":
        changes.update(title="Incoming title must not replace", authors="Incoming creator", identifier_kind="isbn", identifier=ISBN)
    catalogue.clear()
    got, actual = receipt(editor_client, submit(editor_client, selected, **changes), editor_user["id"])
    assert actual.status == "restored" and actual.item_id == item_id
    after = stored(db, item_id)
    assert after["deleted_at"] is None
    for key in before.keys() - {"deleted_at", "owned", "updated_at"}:
        assert after[key] == before[key], key
    assert ownership(db, item_id) == ((1, False) if mode == "add" or state == "owned" else (0, True))
    assert_row_receipt(actual, after)
    assert "Stored restore title" in got.text and "Incoming title must not replace" not in got.text
    assert [tuple(row) for row in db.execute("SELECT * FROM item_copies ORDER BY id")] == copies
    assert {tag["name"] for tag in tags_svc.get_item_tags(db, item_id)} == {"Original", "Restored tag"}
    assert (config.COVERS_DIR / "kept.jpg").read_bytes() == b"stored artwork bytes"
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1 and catalogue.calls == []


@pytest.mark.parametrize("family", ["video_game", "dvd"])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_title_only_game_movie_trash_does_not_invent_identifier_restoration(editor_client, editor_user, db, catalogue, family, mode):
    item_id = seed_duplicate(db, family, "owned")
    item_write.trash_item(db, item_id)
    db.commit()
    before = stored(db, item_id)
    selected = prepared(editor_client, family, mode, **({"platform": "switch"} if family == "video_game" else {}))
    got, actual = receipt(editor_client, submit(editor_client, selected), editor_user["id"])
    assert actual.status == ("added" if mode == "add" else "wishlisted") and actual.item_id != item_id
    assert stored(db, item_id) == before and db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    assert ownership(db, actual.item_id) == ((1, False) if mode == "add" else (0, True))


@pytest.mark.parametrize("provider", ["musicbrainz", "discogs"])
@pytest.mark.parametrize("trashed", [False, True])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_music_exact_release_duplicates_and_trash_refuse_without_restoring_or_promoting(
    editor_client, db, catalogue, provider, trashed, mode,
):
    selected = prepared(editor_client, "cassette", mode, provider=provider)
    item_id = item_write.insert_item(db, title="Existing pressing", media_type="cassette", source=provider,
                                     owned=0, wishlisted=True, cover_path="covers/existing.jpg")
    if provider == "musicbrainz":
        music_catalog.save_release(db, item_id, release())
    else:
        music_catalog.save_release(db, item_id, {"source": "discogs", "media": []})
        discogs_selection.set_selected_release_id(db, item_id, 77)
    if trashed:
        item_write.trash_item(db, item_id)
    db.commit()
    (config.COVERS_DIR / "existing.jpg").write_bytes(b"existing art")
    before = snapshot(db)
    catalogue.clear()
    response = submit(editor_client, selected, fields=["title", "authors"], tags="No mutation")
    error(response, 409, "stale_review")
    assert snapshot(db) == before and catalogue.calls == []
    assert ownership(db, item_id) == (0, True)


@pytest.mark.parametrize("provider", ["musicbrainz", "discogs"])
def test_music_barcode_in_trash_is_not_restored_even_without_a_release_reference(editor_client, db, catalogue, provider):
    selected = prepared(editor_client, "cassette", provider=provider)
    blocker = item_write.insert_item(db, title="Barcode blocker", media_type="cassette", upc="0078073003501", source="manual")
    item_write.trash_item(db, blocker)
    db.commit()
    before = snapshot(db)
    catalogue.clear()
    error(submit(editor_client, selected, fields=["title", "upc"]), 409, "stale_review")
    assert snapshot(db) == before and catalogue.calls == []


def test_manual_identifier_free_submission_is_not_title_deduplicated_or_exactly_once(editor_client, editor_user, db, io):
    selected = prepared(editor_client, "manual")
    ids = []
    for _ in range(2):
        _, actual = receipt(editor_client, submit(editor_client, selected, title="Same title deliberately resubmitted",
                                                   identifier_kind="none", identifier=""), editor_user["id"])
        assert actual.status == "added"
        ids.append(actual.item_id)
    assert ids[0] != ids[1] and db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    assert io.calls == []


def test_prg_receipt_refresh_is_read_only_and_never_replays_post(editor_client, editor_user, db, catalogue):
    selected = prepared(editor_client, "book", "wishlist")
    response = submit(editor_client, selected)
    _, actual = receipt(editor_client, response, editor_user["id"])
    before = snapshot(db)
    catalogue.clear()
    for _ in range(3):
        got = editor_client.get(response.headers["location"])
        assert got.status_code == 200 and got.context["add"]["receipt"].item_id == actual.item_id
        no_cookie(got)
    assert snapshot(db) == before and catalogue.calls == []


@pytest.mark.parametrize("family", ["book", "dvd", "video_game", "manual", "cassette"])
def test_valid_location_is_applied_in_native_insert_and_copy(editor_client, editor_user, db, catalogue, family):
    location = _insert_location(db, "New item location")
    db.commit()
    selected = prepared(editor_client, family)
    changes = {"location_id": location}
    if family == "manual":
        changes.update(title="Located manual", identifier_kind="none", identifier="")
    if family == "cassette":
        changes["fields"] = ["title"]
    _, actual = receipt(editor_client, submit(editor_client, selected, **changes), editor_user["id"])
    assert stored(db, actual.item_id)["location_id"] == location
    assert db.execute("SELECT location_id FROM item_copies WHERE item_id = ? AND copy_number=1", (actual.item_id,)).fetchone()[0] == location


@pytest.mark.parametrize("family", ["book", "dvd", "video_game", "manual", "cassette"])
def test_location_vanished_after_preview_refuses_before_catalogue_write(editor_client, db, catalogue, family):
    location = _insert_location(db, "Vanishing location")
    db.commit()
    selected = prepared(editor_client, family)
    db.execute("DELETE FROM locations WHERE id = ?", (location,))
    db.commit()
    before = snapshot(db)
    catalogue.clear()
    changes = {"location_id": location}
    if family == "manual":
        changes.update(title="Manual location refused", identifier_kind="none", identifier="")
    if family == "cassette":
        changes["fields"] = ["title"]
    error(submit(editor_client, selected, **changes), 422, "invalid_input")
    assert snapshot(db) == before and catalogue.calls == []


@pytest.mark.parametrize("family,restoration", [("book", False), ("dvd", False), ("video_game", False),
    ("manual", False), ("cassette", False), ("book", True), ("manual", True)])
def test_default_tag_failure_rolls_back_whole_native_insert_or_restoration(
    editor_client, db, catalogue, monkeypatch, family, restoration,
):
    if restoration:
        old = item_write.insert_item(db, title="Keep trashed", isbn=ISBN, source="manual", owned=0, wishlisted=True)
        item_write.trash_item(db, old)
        db.commit()
    selected = prepared(editor_client, family)
    before = snapshot(db)
    original, attempted = tags_svc.attach_tags, []

    def fail_after_tag_write(*args, **kwargs):
        original(*args, **kwargs)
        attempted.append(True)
        raise RuntimeError("synthetic tag failure after real tag DML")

    monkeypatch.setattr(tags_svc, "attach_tags", fail_after_tag_write)
    changes = {"tags": "Must roll back", "reading_status": "" if family == "cassette" else "reading"}
    if family == "manual":
        changes.update(title="Manual atomic", identifier_kind="isbn" if restoration else "none", identifier=ISBN if restoration else "")
    if family == "cassette":
        changes["fields"] = ["title"]
    response = submit(editor_client, selected, **changes)
    error(response, 503, "outcome_unknown")
    assert attempted == [True], "The real tag write must be reached; a preflight refusal is not rollback proof"
    assert snapshot(db) == before


@pytest.mark.parametrize("committed", [False, True])
def test_commit_acknowledgement_error_is_unknown_without_automatic_retry(editor_client, db, io, monkeypatch, committed):
    selected = prepared(editor_client, "manual")
    real_execute, real_commit = database._Connection.execute, database._Connection.commit
    writes, commits, touched = [], [], set()

    def execute(conn, sql, *args, **kwargs):
        result = real_execute(conn, sql, *args, **kwargs)
        if sql.lstrip().upper().startswith("INSERT INTO ITEMS "):
            writes.append(sql)
            touched.add(conn)
        return result

    def commit(conn):
        if conn in touched:
            commits.append(True)
            if committed:
                real_commit(conn)
            raise sqlite3.OperationalError("synthetic lost commit acknowledgement")
        return real_commit(conn)

    monkeypatch.setattr(database._Connection, "execute", execute)
    monkeypatch.setattr(database._Connection, "commit", commit)
    response = submit(editor_client, selected, title="Uncertain manual result", identifier_kind="none", identifier="")
    error(response, 503, "outcome_unknown")
    assert len(writes) == 1 and commits == [True]
    assert db.execute("SELECT COUNT(*) FROM items WHERE title='Uncertain manual result'").fetchone()[0] == int(committed)
    assert "No changes were saved" not in response.text and "safe to retry" not in response.text.casefold()
    assert "location" not in response.headers and io.calls == []


def test_postcommit_logging_failure_warns_saved_item_outside_writer_lock(editor_client, editor_user, db, io, monkeypatch):
    selected = prepared(editor_client, "manual", "wishlist")
    calls = []

    def failed_log(*args, **kwargs):
        rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
        try:
            rival.execute("BEGIN IMMEDIATE")
            rows = rival.execute("SELECT id FROM items WHERE title='Saved despite logging failure'").fetchall()
            assert len(rows) == 1, "Log ran before commit or while writer lock was held"
            calls.append(rows[0][0])
            rival.rollback()
        finally:
            rival.close()
        raise RuntimeError("synthetic log-only failure")

    monkeypatch.setattr(items_common, "_log_scan", failed_log)
    got, actual = receipt(editor_client, submit(editor_client, selected, title="Saved despite logging failure",
                                                identifier_kind="none", identifier=""), editor_user["id"])
    assert calls == [actual.item_id] and actual.status == "wishlisted"
    assert got.context["add"]["host_warning"]
    assert actual.refused_code is None and not actual.cover_warning
    assert "unsaved" not in got.text.casefold() and "No changes were saved" not in got.text
    assert ownership(db, actual.item_id) == (0, True) and io.calls == []


@pytest.mark.parametrize("family", ["book", "dvd", "video_game"])
def test_retained_native_add_only_routes_still_leave_wishlist_duplicates_unpromoted(editor_client, db, catalogue, family):
    item_id = seed_duplicate(db, family, "wishlist")
    before = stored(db, item_id)
    if family == "book":
        action, values = "/api/books/add", {"isbn": ISBN, "media_type": "book"}
    elif family == "video_game":
        action, values = "/api/games/add", {"igdb_id": "123", "platform": "switch"}
    else:
        action, values = "/api/dvds/add", {"title": MOVIE["title"], "tmdb_id": "456"}
    response = editor_client.post(action, data=values)
    assert response.status_code == 200 and "Already in collection" in response.text
    assert ownership(db, item_id) == (0, True) and stored(db, item_id) == before


@pytest.mark.parametrize("state", ["owned", "neither", "wishlist"])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_manual_upc_trash_restore_keeps_native_identity_copies_location_keys_and_forward_ownership(
    editor_client, editor_user, db, io, monkeypatch, state, mode,
):
    original_location = _insert_location(db, "Original UPC shelf")
    proposed_location = _insert_location(db, "Must not move restored copies here")
    db.commit()
    initial_mode = "add" if state == "owned" else "wishlist"
    initial = prepared(editor_client, "manual", initial_mode, media_type="cassette")
    first_response = submit(editor_client, initial, title="Original UPC cassette", authors="Original artist",
                            publisher="Original label", publish_year="1984", identifier_kind="barcode",
                            identifier="078073003501", location_id=original_location, tags="Original UPC tag")
    _, first = receipt(editor_client, first_response, editor_user["id"])
    item_id = first.item_id
    assert first.status == ("added" if initial_mode == "add" else "wishlisted")
    assert stored(db, item_id)["upc"] == "0078073003501" and stored(db, item_id)["isbn"] is None
    assert ownership(db, item_id) == ((1, False) if initial_mode == "add" else (0, True))
    if state == "neither":
        item_write.update_item_fields(db, item_id, {"owned": 0, "wishlisted": False})
    item_write.update_item_fields(db, item_id, {"notes": "Keep original UPC notes", "cover_path": "covers/upc-kept.jpg"})
    db.execute("INSERT INTO item_copies (item_id, copy_number, location_id, condition, acquisition_source) "
               "VALUES (?,2,?,'VG+','Original record shop')", (item_id, original_location))
    music_catalog.save_release(db, item_id, release())
    discogs_selection.set_selected_release_id(db, item_id, 77)
    (config.COVERS_DIR / "upc-kept.jpg").write_bytes(b"original UPC artwork bytes")
    item_write.trash_item(db, item_id)
    db.commit()
    before_row = stored(db, item_id)
    assert before_row["deleted_at"] is not None
    before_copies = [tuple(row) for row in db.execute("SELECT * FROM item_copies WHERE item_id=? ORDER BY id", (item_id,))]
    before_music = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
                    for table in ("music_releases", "music_media", "music_tracks", "music_identifiers")}
    selected = prepared(editor_client, "manual", mode, media_type="cassette")
    original_execute, locks = database._Connection.execute, []

    def execute(conn, sql, *args, **kwargs):
        result = original_execute(conn, sql, *args, **kwargs)
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            assert conn.in_transaction
            rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
            try:
                try:
                    rival.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    assert "locked" in str(exc).casefold()
                    locks.append(True)
                else:
                    rival.rollback()
                    locks.append(False)
            finally:
                rival.close()
        return result

    monkeypatch.setattr(database._Connection, "execute", execute)
    io.clear()
    response = submit(editor_client, selected, title="Incoming title must not overwrite UPC restore",
                      authors="Incoming artist", publisher="Incoming publisher", publish_year="2026",
                      identifier_kind="barcode", identifier="0-78073-00350-1", location_id=proposed_location,
                      tags="Restored UPC tag", reading_status="")
    got, actual = receipt(editor_client, response, editor_user["id"])
    after = stored(db, item_id)
    assert locks == [True], "UPC restoration must use the real caller-owned SQLite writer lock"
    assert actual.status == "restored" and actual.item_id == item_id
    assert_restored_receipt_mode_matches_ownership(actual, after)
    if mode == "wishlist" and after["owned"]:
        from dataclasses import replace as replace_record
        with pytest.raises(AssertionError):
            assert_restored_receipt_mode_matches_ownership(replace_record(actual, actual_mode="wishlist"), after)
    assert_row_receipt(actual, after)
    assert after["deleted_at"] is None and after["upc"] == "0078073003501" and after["location_id"] == original_location
    for key in before_row.keys() - {"deleted_at", "owned", "updated_at"}:
        assert after[key] == before_row[key], key
    assert ownership(db, item_id) == ((1, False) if mode == "add" or state == "owned" else (0, True))
    assert [tuple(row) for row in db.execute("SELECT * FROM item_copies WHERE item_id=? ORDER BY id", (item_id,))] == before_copies
    assert {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in before_music} == before_music
    assert {tag["name"] for tag in tags_svc.get_item_tags(db, item_id)} == {"Original UPC tag", "Restored UPC tag"}
    assert (config.COVERS_DIR / "upc-kept.jpg").read_bytes() == b"original UPC artwork bytes"
    assert "Original UPC cassette" in got.text and "Incoming title must not overwrite UPC restore" not in got.text
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1 and io.calls == []


def assert_restored_receipt_mode_matches_ownership(actual, row):
    assert actual.actual_mode == ("add" if row["owned"] else "wishlist")


def assert_second_rendered_choice_saved(actual, row, family, mode):
    expected = {
        "dvd": ("Second selected movie", "Second movie-only description", 2002, "tmdb"),
        "video_game": ("Second selected game", "Second game-only description", 2021, "igdb"),
    }[family]
    assert (row["title"], row["description"], row["publish_year"], row["source"]) == expected
    assert actual.title == expected[0] and actual.source == expected[3]
    assert actual.status == ("added" if mode == "add" else "wishlisted") and actual.actual_mode == mode
    assert row["media_type"] == family and row["authors"] is None
    assert "igdb_id" not in row and "tmdb_id" not in row
    assert_row_receipt(actual, row)


@pytest.mark.parametrize("family", ["dvd", "video_game"])
@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_second_rendered_movie_game_choice_is_saved_with_literal_native_facts_and_receipt(
    editor_client, editor_user, db, catalogue, family, mode,
):
    from dataclasses import replace as replace_record
    from app.services import tmdb

    if family == "dvd":
        catalogue.install(tmdb, "search_movies", provider_result.found("tmdb", [
            {**MOVIE, "tmdb_id": 456, "title": "First unselected movie", "description": "First movie description", "publish_year": 1980},
            {**MOVIE, "tmdb_id": 987, "title": "Second selected movie", "description": "Second movie-only description", "publish_year": 2002},
        ]))
    else:
        catalogue.install(igdb, "search_games", provider_result.found("igdb", [
            {**GAME, "igdb_id": 123, "title": "First unselected game", "description": "First game description", "publish_year": 1989},
            {**GAME, "igdb_id": 789, "title": "Second selected game", "description": "Second game-only description", "publish_year": 2021},
        ]))

        def exact_second(identifier, *args, **kwargs):
            assert identifier == 789
            return {**GAME, "igdb_id": 789, "title": "Second selected game",
                    "description": "Second game-only description", "publish_year": 2021}

        catalogue.install(igdb, "lookup_game", handler=exact_second)
    searched = search(editor_client, media_type=family, mode=mode, q="Choose second edition",
                      **({"platform": "switch"} if family == "video_game" else {}))
    search_module, search_name = (tmdb, "search_movies") if family == "dvd" else (igdb, "search_games")
    assert len(catalogue.calls) == 1 and len(catalogue.seen(search_module, search_name)) == 1
    catalogue.clear()
    reviewed = preview(editor_client, searched, index=1)
    selected = form(reviewed, "add-media-save")
    assert ("Second selected movie" if family == "dvd" else "Second selected game") in reviewed.text
    if family == "dvd":
        assert catalogue.calls == [], "Movie preview must use the exact signed search snapshot, not rediscover its first title"
    else:
        details = catalogue.seen(igdb, "lookup_game")
        assert len(catalogue.calls) == 1 and len(details) == 1 and details[0][0][0] == 789
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    catalogue.clear()
    _, actual = receipt(editor_client, submit(editor_client, selected), editor_user["id"])
    row = stored(db, actual.item_id)
    assert_second_rendered_choice_saved(actual, row, family, mode)
    assert ownership(db, actual.item_id) == ((1, False) if mode == "add" else (0, True))
    assert row["cover_path"] is None and db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert catalogue.calls == [], "Save and receipt GET must not rediscover provider metadata or artwork"
    # Counterexamples copy actual observed native output only for this
    # assertion helper; none is supplied to an auth handler or save route.
    first_title = "First unselected movie" if family == "dvd" else "First unselected game"
    first_description = "First movie description" if family == "dvd" else "First game description"
    first_year = 1980 if family == "dvd" else 1989
    with pytest.raises(AssertionError):
        assert_second_rendered_choice_saved(replace_record(actual, title=first_title), {**row, "title": first_title}, family, mode)
    with pytest.raises(AssertionError):
        assert_second_rendered_choice_saved(actual, {**row, "description": first_description}, family, mode)
    with pytest.raises(AssertionError):
        assert_second_rendered_choice_saved(actual, {**row, "publish_year": first_year}, family, mode)
