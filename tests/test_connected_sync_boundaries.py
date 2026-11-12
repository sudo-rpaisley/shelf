"""Final-identity, enumeration and cover boundaries for connected sync."""

import asyncio
import json
import sqlite3

import httpx
import pytest
import respx

from app import config
from app.database import get_db
from app.services import audiobookshelf, cover_sync_policy, komga_records, legacy_sources, metadata_policy
from app.services.item_write import insert_item, trash_item, update_item_fields


ABS = "https://abs.example"
ISBN_X = "9780441172719"
ISBN_Y = "9780441013593"


def _library(library_id="audio"):
    return {"id": library_id, "name": "Audiobooks", "mediaType": "book"}


def _remote(abs_id="one", *, ebook=False, **metadata):
    return {
        "id": abs_id,
        "media": {
            "metadata": {"title": "Remote title", **metadata},
            "numAudioFiles": 0 if ebook else 1,
            "duration": 0 if ebook else 3600,
            "ebookFormat": "epub" if ebook else None,
        },
    }


def _libraries(libraries, **pagination):
    return respx.get(f"{ABS}/api/libraries").respond(200, json={"libraries": libraries, **pagination})


def _items(*items, library_id="audio", **pagination):
    return respx.get(f"{ABS}/api/libraries/{library_id}/items").respond(
        200, json={"results": list(items), **pagination},
    )


def _cover(abs_id="one", content=None):
    return respx.get(f"{ABS}/api/items/{abs_id}/cover").respond(
        404 if content is None else 200, content=content or b"",
    )


def _sync():
    return asyncio.run(audiobookshelf.sync(ABS, "test-token"))


def _seed_abs(db, *, provenance=True, **overrides):
    fields = {
        "title": "Original title", "isbn": ISBN_X, "media_type": "audiobook",
        "description": "Provider description", "duration_mins": 60,
        **overrides,
    }
    item_id = insert_item(
        db, fields, source="audiobookshelf", abs_id="one", abs_library_id="audio",
        cover_path="covers/selected.jpg",
    )
    if provenance:
        metadata_policy.record_initial(db, item_id, "audiobookshelf:one", fields)
    return item_id


def _row(db, item_id):
    return dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())


def _snapshot(db, item_id, source_key, field):
    return json.loads(db.execute(
        "SELECT value_json FROM item_metadata_provenance "
        "WHERE item_id = ? AND source_key = ? AND field_name = ?",
        (item_id, source_key, field),
    ).fetchone()["value_json"])


@pytest.mark.parametrize("baseline", ["cleared", "unknown"])
@respx.mock
def test_abs_remote_identifier_conflict_does_not_refuse_a_preserved_local_identity(db, baseline):
    item_id = _seed_abs(db, provenance=baseline == "cleared")
    local_isbn = None if baseline == "cleared" else ISBN_Y
    update_item_fields(db, item_id, {"isbn": local_isbn, "description": "My description"})
    blocker = insert_item(db, title="Other live edition", isbn=ISBN_X, media_type="audiobook")
    db.commit()
    _libraries([_library()])
    _items(_remote(isbn=ISBN_X, description="Changed provider description"), _remote("healthy"))
    _cover("healthy")

    stats = _sync()

    assert stats == {
        "added": 1, "updated": int(baseline == "cleared"), "unchanged": int(baseline == "unknown"),
        "skipped": 0, "errors": 0,
    }
    row = _row(db, item_id)
    assert row["isbn"] == local_isbn
    assert row["description"] == "My description"
    assert row["title"] == ("Remote title" if baseline == "cleared" else "Original title")
    assert _row(db, blocker)["isbn"] == ISBN_X
    assert db.execute("SELECT id FROM items_live WHERE abs_id = 'healthy'").fetchone() is not None
    assert _snapshot(db, item_id, "audiobookshelf:one", "isbn")["overridden"] is (baseline == "cleared")


@pytest.mark.parametrize("trashed", [False, True])
@pytest.mark.parametrize("identifier,value", [("isbn", ISBN_X), ("upc", "0123456789012")])
@respx.mock
def test_abs_retained_identifier_and_new_type_are_checked_as_one_effective_identity(
    db, monkeypatch, trashed, identifier, value,
):
    from app.log_handler import SQLiteHandler

    fields = {"isbn": None, identifier: value}
    item_id = _seed_abs(db, **fields)
    blocker = insert_item(db, title="Conflicting ebook", media_type="ebook", **fields)
    if trashed:
        trash_item(db, blocker)
    before = _row(db, item_id)
    db.commit()
    monkeypatch.setattr(audiobookshelf.logger, "handlers", [SQLiteHandler()])
    _libraries([_library()])
    # ABS omits ISBN; the stored identifier survives sparse refresh and must
    # still participate in the final (identifier, ebook) collision check.
    _items(_remote(ebook=True), _remote("healthy"))
    _cover("healthy")

    stats = _sync()

    assert stats["added"] == 1 and stats["updated"] == 0 and stats["errors"] == 0
    assert stats["skipped"] == (0 if trashed else 1)
    assert stats.get("in_trash", 0) == (1 if trashed else 0)
    assert _row(db, item_id) == before
    assert _row(db, blocker)[identifier] == value
    assert (_row(db, blocker)["deleted_at"] is not None) is trashed
    assert db.execute("SELECT id FROM items_live WHERE abs_id = 'healthy'").fetchone() is not None
    if not trashed:
        messages = db.execute("SELECT message FROM log_entries WHERE module = ?", (audiobookshelf.__name__,))
        assert any("Skipping ABS item one" in row["message"] for row in messages)


@pytest.mark.parametrize("operation", ["insert", "update"])
@respx.mock
def test_abs_funnel_integrity_collision_is_per_record_and_does_not_abort_the_library(db, monkeypatch, operation):
    item_id = _seed_abs(db) if operation == "update" else None
    db.commit()
    _libraries([_library()])
    _items(_remote(), _remote("healthy"))
    _cover("healthy")
    if operation == "insert":
        original = audiobookshelf.insert_item

        def collide(db, **fields):
            if fields["abs_id"] == "one":
                raise sqlite3.IntegrityError("UNIQUE constraint failed: items.isbn, items.media_type")
            return original(db, **fields)

        monkeypatch.setattr(audiobookshelf, "insert_item", collide)
    else:
        original = audiobookshelf.update_item_fields

        def collide(db, target_id, fields):
            if target_id == item_id:
                raise sqlite3.IntegrityError("UNIQUE constraint failed: items.isbn, items.media_type")
            return original(db, target_id, fields)

        monkeypatch.setattr(audiobookshelf, "update_item_fields", collide)

    assert _sync() == {"added": 1, "updated": 0, "unchanged": 0, "skipped": 1, "errors": 0}
    if item_id is not None:
        assert _row(db, item_id)["title"] == "Original title"
    assert db.execute("SELECT id FROM items_live WHERE abs_id = 'healthy'").fetchone() is not None


def _komga(**overrides):
    return {
        "komga_id": "one", "komga_library_id": "library", "library_kind": "comic",
        "title": "Comic title", "authors": "Author", "isbn": ISBN_X,
        "description": "Provider description", "series_name": "Provider series",
        **overrides,
    }


@pytest.mark.parametrize("trashed", [False, True])
def test_komga_reclassification_writes_only_the_safely_planned_final_identity(db, monkeypatch, trashed):
    item_id = komga_records.persist_candidate(db, _komga())["item_id"]
    blocker = insert_item(db, title="Other manga", isbn=ISBN_X, media_type="manga")
    if trashed:
        trash_item(db, blocker)
    original = komga_records.update_item_fields
    writes = []

    def track(db, target_id, fields):
        writes.append(dict(fields))
        return original(db, target_id, fields)

    monkeypatch.setattr(komga_records, "update_item_fields", track)
    result = komga_records.persist_candidate(
        db, _komga(library_kind="manga", isbn=ISBN_Y, title="Corrected manga title"),
    )

    assert result["item_id"] == item_id and result["action"] == "updated"
    assert len(writes) == 1
    assert writes[0]["media_type"] == "manga" and writes[0]["isbn"] == ISBN_Y
    row = _row(db, item_id)
    assert row["media_type"] == "manga" and row["isbn"] == ISBN_Y
    assert row["title"] == "Corrected manga title"
    assert komga_records.records_for_item(db, item_id)[0]["kind"] == "manga"
    assert _row(db, blocker)["isbn"] == ISBN_X
    assert (_row(db, blocker)["deleted_at"] is not None) is trashed


def test_komga_full_identity_planning_does_not_overwrite_a_local_identifier_to_avoid_collision(db):
    item_id = komga_records.persist_candidate(db, _komga())["item_id"]
    update_item_fields(db, item_id, {"isbn": ISBN_Y, "description": "My description"})
    insert_item(db, title="Other manga", isbn=ISBN_Y, media_type="manga")
    before = _row(db, item_id)
    holding = komga_records.records_for_item(db, item_id)

    with pytest.raises(komga_records.KomgaPersistenceError, match="collide"):
        komga_records.persist_candidate(
            db, _komga(library_kind="manga", isbn=ISBN_X, komga_library_id="other-library"),
        )

    assert _row(db, item_id) == before
    assert komga_records.records_for_item(db, item_id) == holding


@pytest.mark.parametrize("payload", [{}, [], None, {"libraries": None}, {"libraries": {}}, {"libraries": "bad"}])
@respx.mock
def test_abs_malformed_library_envelope_cannot_advance_complete_health(db, monkeypatch, payload):
    now = ["2026-11-12T12:00:00+00:00"]
    monkeypatch.setattr(legacy_sources, "_now", lambda: now[0])
    _libraries([])

    def tracked():
        return asyncio.run(legacy_sources.run_tracked("abs", audiobookshelf.sync, ABS, "test-token"))

    assert tracked() == {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    now[0] = "2026-11-12T13:00:00+00:00"
    respx.get(f"{ABS}/api/libraries").respond(200, content=json.dumps(payload).encode())

    stats = tracked()

    assert stats["errors"] == 1 and stats["complete"] is False
    health = next(row for row in legacy_sources.statuses(db) if row["key"] == "abs")
    assert health["last_sync_at"] == "2026-11-12T12:00:00+00:00"
    assert health["last_attempt_at"] == now[0]
    assert health["last_status"] in {"partial", "error"}
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 0


@pytest.mark.parametrize("bad_library", [
    None, [], {}, {"id": 123}, {"id": "bad", "name": []},
    {"id": "bad", "mediaType": None}, {"id": "bad", "mediaType": "unknown"},
])
@respx.mock
def test_abs_malformed_library_entries_do_not_prevent_healthy_libraries(db, bad_library):
    _libraries([bad_library, _library()])
    _items(_remote("healthy"))
    _cover("healthy")

    stats = _sync()

    assert stats["added"] == 1 and stats["errors"] == 1 and stats["complete"] is False
    assert legacy_sources.result_health(stats) == ("partial", "incomplete")
    assert db.execute("SELECT abs_id FROM items_live").fetchone()[0] == "healthy"


@pytest.mark.parametrize("payload", [{}, [], None, {"results": None}, {"results": {}}, {"results": "bad"}])
@respx.mock
def test_abs_malformed_item_envelopes_do_not_prevent_healthy_libraries(db, payload):
    _libraries([_library("bad"), _library()])
    respx.get(f"{ABS}/api/libraries/bad/items").respond(200, content=json.dumps(payload).encode())
    _items(_remote("healthy"))
    _cover("healthy")

    stats = _sync()

    assert stats["added"] == 1 and stats["errors"] == 1 and stats["complete"] is False
    assert db.execute("SELECT abs_id FROM items_live").fetchone()[0] == "healthy"


@pytest.mark.parametrize("endpoint", ["libraries", "items"])
@respx.mock
def test_abs_invalid_json_is_counted_instead_of_becoming_empty_success(db, endpoint):
    if endpoint == "libraries":
        route = respx.get(f"{ABS}/api/libraries")
    else:
        _libraries([_library("bad"), _library()])
        route = respx.get(f"{ABS}/api/libraries/bad/items")
        _items(_remote("healthy"))
        _cover("healthy")
    route.respond(200, content=b"{not-json")

    stats = _sync()

    assert stats["errors"] == 1 and stats["complete"] is False
    assert stats["added"] == (0 if endpoint == "libraries" else 1)


@pytest.mark.parametrize("bad_item", [
    None, [], {}, {"media": None}, {"media": {"metadata": []}},
    _remote(publishedYear=[]),
    _remote(publishedYear=str(2**64)),
    {"id": "bad", "media": {"metadata": {"title": "Bad"}, "duration": "invalid"}},
    {"id": "bad", "media": {"metadata": {"title": "Bad"}, "duration": 2**64}},
    {"id": "bad", "media": {"metadata": {"title": "Bad"}, "numAudioFiles": 1.5}},
    _remote(title=[]), _remote(authorName=[]),
])
@respx.mock
def test_abs_malformed_item_values_are_per_record_failures(db, bad_item):
    _libraries([_library()])
    _items(bad_item, _remote("healthy"))
    _cover("healthy")

    stats = _sync()

    assert stats["added"] == 1 and stats["errors"] == 1 and stats["complete"] is False
    assert db.execute("SELECT abs_id FROM items_live").fetchone()[0] == "healthy"


@pytest.mark.parametrize("pagination", [
    {"total": 2}, {"total": 0}, {"total": "1"}, {"limit": 1}, {"limit": "1"},
    {"page": 1}, {"hasMore": True}, {"hasNextPage": True}, {"totalPages": 2}, {"nextPage": 1},
])
@respx.mock
def test_abs_unenumerated_pages_or_invalid_pagination_are_partial_not_complete(db, pagination):
    _libraries([_library("paged"), _library()])
    _items(_remote("partial"), library_id="paged", **pagination)
    _items(_remote("healthy"))
    _cover("partial")
    _cover("healthy")

    stats = _sync()

    assert stats["added"] == 2 and stats["errors"] == 1 and stats["complete"] is False
    assert legacy_sources.result_health(stats) == ("partial", "incomplete")
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 2


@pytest.mark.parametrize("empty_endpoint", ["libraries", "items"])
@respx.mock
def test_abs_explicit_empty_collections_are_complete_success(db, empty_endpoint):
    _libraries([] if empty_endpoint == "libraries" else [_library()])
    if empty_endpoint == "items":
        _items(total=0, limit=10000, page=0)
    stats = _sync()
    assert stats == {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    assert legacy_sources.result_health(stats) == ("ok", None)


def test_abs_exact_page_cap_without_completion_evidence_is_not_complete():
    assert audiobookshelf._listing_is_partial({}, 10000, 10000) is True
    assert audiobookshelf._listing_is_partial({"total": 10000, "page": 0}, 10000, 10000) is False


@respx.mock
def test_abs_incomplete_library_listing_still_imports_known_healthy_libraries(db):
    _libraries([_library()], total=2)
    _items(_remote("healthy"), total=1, limit=1, page=0)
    _cover("healthy")
    stats = _sync()
    assert stats["added"] == 1 and stats["errors"] == 1 and stats["complete"] is False


@respx.mock
def test_abs_deliberately_removed_cover_stays_absent_even_with_dismissal_reset(db, admin_client):
    first_cover = b"\xff\xd8\xff" + b"original cover" * 100
    _libraries([_library()])
    _items(_remote())
    cover_route = _cover(content=first_cover)
    assert _sync()["added"] == 1
    item_id = db.execute("SELECT id FROM items_live").fetchone()[0]
    path = config.COVERS_DIR / f"{item_id}.jpg"
    assert path.read_bytes() == first_cover
    assert admin_client.post(f"/api/items/{item_id}/cover-remove").status_code == 200
    assert _row(db, item_id)["cover_review_dismissed"] == 0
    cover_route.respond(200, content=b"\xff\xd8\xff" + b"replacement cover" * 100)

    for _ in range(2):
        assert _sync()["unchanged"] == 1
        assert _row(db, item_id)["cover_path"] is None
        assert path.read_bytes() == first_cover
        assert cover_route.call_count == 1
    # Removing an orphaned file later must not revoke the saved local decision.
    path.unlink()
    assert _sync()["unchanged"] == 1
    assert _row(db, item_id)["cover_path"] is None
    assert not path.exists()
    assert cover_route.call_count == 1
    # Removal is authoritative in the independent artwork decision table. A
    # blocked sync need not rewrite the old provider observation to enforce it.
    assert cover_sync_policy.decision(db, item_id) is True


@pytest.mark.parametrize("dismiss_during_download", [False, True])
@respx.mock
def test_abs_dismissed_missing_cover_is_not_filled_before_or_after_download(db, dismiss_during_download):
    item_id = _seed_abs(db, isbn=None)
    update_item_fields(db, item_id, {
        "cover_path": None, "cover_review_dismissed": 0 if dismiss_during_download else 1,
    })
    db.commit()
    _libraries([_library()])
    _items(_remote(title="Original title", description="Provider description"))
    route = _cover()

    def dismiss(request):
        with get_db() as writer:
            update_item_fields(writer, item_id, {"cover_review_dismissed": 1})
        return httpx.Response(200, content=b"\xff\xd8\xff" + b"unwanted cover" * 100)

    route.mock(side_effect=dismiss)
    stats = _sync()

    assert stats["unchanged"] == 1 and stats["errors"] == 0
    assert _row(db, item_id)["cover_path"] is None
    assert _row(db, item_id)["cover_review_dismissed"] == 1
    assert not (config.COVERS_DIR / f"{item_id}.jpg").exists()
    assert route.call_count == (1 if dismiss_during_download else 0)
