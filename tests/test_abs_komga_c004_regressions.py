"""Finite C004 connector contracts; all HTTP/provider traffic is locally mocked."""

from contextlib import contextmanager
from io import BytesIO
import json

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image

from app.database import get_db, get_setting
from app.features import set_feature_enabled
from app.services import (
    audiobookshelf, catalogue_security, covers, holdings, item_write, komga_books,
    komga_records, komga_sync, media_groups, sync_jobs,
)
from tests.conftest import _insert_item

KOMGA = "https://komga.example"
ABS = "https://abs.example"
TABLES = (
    "items", "komga_records", "digital_holdings", "library_items", "item_links",
    "item_series", "item_copies", "user_item_state", "user_reading_log",
    "reading_log", "scan_log",
)


def _snapshot(db):
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in TABLES}


def _jpeg(colour="navy"):
    output = BytesIO()
    Image.new("RGB", (24, 32), colour).save(output, format="JPEG")
    return output.getvalue()


def _candidate(book_id="book-1", **overrides):
    candidate = {"komga_id": book_id, "komga_library_id": "excluded",
                 "library_kind": "comic", "title": f"Komga {book_id}"}
    candidate.update(overrides)
    return candidate


def _seed(db, provider, external_id="record-1", *, source=None):
    if provider == "komga":
        if source is None:
            return komga_records.persist_candidate(db, _candidate(external_id))["item_id"]
        fields = {"media_type": "digital_comic", "komga_id": external_id,
                  "komga_library_id": "excluded"}
    else:
        fields = {"media_type": "audiobook", "abs_id": external_id,
                  "abs_library_id": "excluded"}
        if source is None:
            return audiobookshelf.persist_candidate(db, {
                **fields, "title": f"Audiobookshelf {external_id}",
            })["item_id"]
    return item_write.insert_item(db, title=f"Manual {external_id}", source=source, **fields)


async def _cover(client, provider, item_id, external_id):
    if provider == "komga":
        return await komga_sync._ingest_cover(client, KOMGA, "test-key", item_id, external_id)
    return await audiobookshelf._ingest_cover(client, ABS, "test-token", item_id, external_id)


def _thumbnail_url(provider, external_id="record-1"):
    return (f"{KOMGA}/api/v1/books/{external_id}/thumbnail" if provider == "komga"
            else f"{ABS}/api/items/{external_id}/cover")


def test_abs_strict_origins_exclude_each_other_without_narrowing_default_groups(db):
    audio = _insert_item(db, title="Shared Work", isbn=None, media_type="audiobook")
    ebook = _insert_item(db, title="Shared Work", isbn=None, media_type="ebook")
    db.commit()
    assert audiobookshelf.auto_link([audio, ebook]) == 0
    assert db.execute("SELECT COUNT(*) FROM item_links").fetchone()[0] == 0
    # The optional origin policy must not silently narrow the native grouping API.
    assert media_groups.auto_link_item(db, audio) == 1
    assert media_groups.related_ids(db, audio) == [ebook]


def test_default_grouping_still_connects_same_format_editions(db):
    first = _insert_item(db, title="Shared Work", isbn=None, media_type="audiobook")
    second = _insert_item(db, title="Shared Work", isbn=None, media_type="audiobook")
    assert media_groups.auto_link_item(db, first) == 1
    assert media_groups.related_ids(db, first) == [second]


@pytest.mark.asyncio
async def test_unselected_comic_and_manga_names_do_not_authorize_import(db, respx_mock):
    komga_sync.save_configuration(url=KOMGA, api_key="test-key")
    respx_mock.get(f"{KOMGA}/api/v1/libraries").respond(200, json=[
        {"id": "comic", "name": "Comics"}, {"id": "manga", "name": "Manga"},
    ])
    before = _snapshot(db)
    stats = await komga_sync.sync()
    assert stats == {"created": 0, "adopted": 0, "updated": 0, "unchanged": 0,
                     "skipped": 2, "in_trash": 0, "errors": 0, "covers": 0,
                     "cover_errors": 0, "added": 0}
    assert _snapshot(db) == before
    assert [call.request.url.path for call in respx_mock.calls] == ["/api/v1/libraries"]


@pytest.mark.asyncio
async def test_incomplete_library_never_persists_partial_books_and_sibling_continues(db, respx_mock):
    komga_sync.save_configuration(url=KOMGA, api_key="test-key")
    komga_sync.save_library_selection([
        {"id": "bad", "included": True, "kind": "comic"},
        {"id": "good", "included": True, "kind": "comic"},
    ])
    respx_mock.get(f"{KOMGA}/api/v1/libraries").respond(200, json=[
        {"id": "bad", "name": "Bad"}, {"id": "good", "name": "Good"},
    ])
    respx_mock.post(f"{KOMGA}/api/v1/books/list").mock(side_effect=[
        httpx.Response(200, json={"content": [{"id": "partial", "name": "Partial"}], "last": False}),
        httpx.Response(200, json={"content": [{"id": "partial", "name": "Partial"}], "last": True}),
        httpx.Response(200, json={"content": [{"id": "complete", "name": "Complete"}], "last": True}),
    ])
    respx_mock.get(f"{KOMGA}/api/v1/books/complete/thumbnail").respond(404)
    progress = []

    async def callback(current, total, title, status):
        progress.append((current, total, title, status))

    stats = await komga_sync.sync(on_progress=callback)
    assert stats == {"created": 1, "adopted": 0, "updated": 0, "unchanged": 0,
                     "skipped": 0, "in_trash": 0, "errors": 1, "covers": 0,
                     "cover_errors": 0, "added": 1}
    assert [tuple(row) for row in db.execute("SELECT title, komga_id FROM items")] == [("Complete", "complete")]
    assert progress == [(1, 1, "Complete", "created")]
    assert not any("/partial/" in call.request.url.path for call in respx_mock.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("pages", [
    [{"content": [{"id": "b1", "name": "Partial"}], "last": True, "totalPages": 2}],
    [{"content": [], "last": True, "totalPages": 2}],
    [{"content": [{"id": "b1", "name": "Partial"}], "last": False, "totalPages": 1}],
    [{"content": [], "last": False, "totalPages": 0}],
    [
        {"content": [{"id": "b1"}], "last": False, "totalPages": 2},
        {"content": [{"id": "b2"}], "last": True, "totalPages": 1},
    ],
    [
        {"content": [{"id": "b1"}], "last": False, "totalPages": 2},
        {"content": [{"id": "b2"}], "last": True, "totalPages": 3},
    ],
    [
        {"content": [{"id": "b1"}], "last": False, "totalPages": 2},
        {"content": [{"id": "b2"}], "last": False, "totalPages": 2},
    ],
    [
        {"content": [{"id": "b1"}], "last": False, "totalPages": 3},
        {"content": [{"id": "b2"}], "last": True},
    ],
    [
        {"content": [{"id": "b1"}], "last": False, "totalPages": 3},
        {"content": [{"id": "b2"}], "last": True, "totalPages": 2},
    ],
])
async def test_komga_discovery_refuses_inconsistent_terminal_markers(respx_mock, pages):
    listing = respx_mock.post(f"{KOMGA}/api/v1/books/list").mock(side_effect=[
        httpx.Response(200, json=page) for page in pages
    ])
    async with httpx.AsyncClient() as client:
        with pytest.raises(komga_books.KomgaIncompleteDiscoveryError):
            await komga_books.fetch_library_books(client, KOMGA, "test-key", "bad")
    assert listing.call_count == len(pages)
    assert [call.request.url.params["page"] for call in listing.calls] == [str(page) for page in range(len(pages))]


@pytest.mark.asyncio
@pytest.mark.parametrize("total_pages", [-1, 0, True, False, "2", 2.0, None, [], {}])
async def test_komga_discovery_refuses_invalid_published_counts(respx_mock, total_pages):
    listing = respx_mock.post(f"{KOMGA}/api/v1/books/list").respond(200, json={
        "content": [{"id": "b1", "name": "Partial"}], "last": True, "totalPages": total_pages,
    })
    async with httpx.AsyncClient() as client:
        with pytest.raises(komga_books.KomgaIncompleteDiscoveryError, match="page count"):
            await komga_books.fetch_library_books(client, KOMGA, "test-key", "bad")
    assert listing.call_count == 1
    assert listing.calls[0].request.url.params["page"] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("last", [None, 0, 1, "true", "false"])
async def test_komga_discovery_refuses_untyped_completion_markers(respx_mock, last):
    listing = respx_mock.post(f"{KOMGA}/api/v1/books/list").respond(200, json={
        "content": [{"id": "b1", "name": "Partial"}], "last": last, "totalPages": 1,
    })
    async with httpx.AsyncClient() as client:
        with pytest.raises(komga_books.KomgaIncompleteDiscoveryError, match="invalid completion marker"):
            await komga_books.fetch_library_books(client, KOMGA, "test-key", "bad")
    assert listing.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("pages,expected_ids", [
    ([{"content": [{"id": "b1"}], "last": True, "totalPages": 1}], ["b1"]),
    ([{"content": [{"id": "b1"}], "last": True}], ["b1"]),
    ([{"content": [], "last": True, "totalPages": 0}], []),
    ([{"content": [], "last": True, "totalPages": 1}], []),
    ([{"content": [], "totalPages": 0}], []),
    ([
        {"content": [{"id": "b1"}], "last": False, "totalPages": 2},
        {"content": [{"id": "b2"}], "last": True, "totalPages": 2},
    ], ["b1", "b2"]),
    ([
        {"content": [{"id": "b1"}], "last": False},
        {"content": [{"id": "b2"}], "last": True},
    ], ["b1", "b2"]),
    ([
        {"content": [{"id": "b1"}], "totalPages": 2},
        {"content": [{"id": "b2"}], "totalPages": 2},
    ], ["b1", "b2"]),
    ([
        {"content": [{"id": "b1"}], "last": False, "totalPages": 2},
        {"content": [{"id": "b2"}], "last": True},
    ], ["b1", "b2"]),
    ([
        {"content": [{"id": "b1"}], "last": False},
        {"content": [{"id": "b2"}], "last": True, "totalPages": 2},
    ], ["b1", "b2"]),
])
async def test_komga_discovery_accepts_consistent_final_pages(respx_mock, pages, expected_ids):
    listing = respx_mock.post(f"{KOMGA}/api/v1/books/list").mock(side_effect=[
        httpx.Response(200, json=page) for page in pages
    ])
    async with httpx.AsyncClient() as client:
        books = await komga_books.fetch_library_books(client, KOMGA, "test-key", "good")
    assert [book["id"] for book in books] == expected_ids
    assert listing.call_count == len(pages)
    assert [call.request.url.params["page"] for call in listing.calls] == [str(page) for page in range(len(pages))]


@pytest.mark.asyncio
async def test_premature_terminal_library_has_no_effects_and_healthy_sibling_really_imports(db, respx_mock):
    komga_sync.save_configuration(url=KOMGA, api_key="test-key")
    komga_sync.save_library_selection([
        {"id": "bad", "included": True, "kind": "comic"},
        {"id": "good", "included": True, "kind": "comic"},
    ])
    respx_mock.get(f"{KOMGA}/api/v1/libraries").respond(200, json=[
        {"id": "bad", "name": "Bad"}, {"id": "good", "name": "Good"},
    ])
    # The actual review reproducer: last=true cannot override an unseen page.
    listing = respx_mock.post(f"{KOMGA}/api/v1/books/list").mock(side_effect=[
        httpx.Response(200, json={"content": [{"id": "b1", "name": "Partial"}], "last": True, "totalPages": 2}),
        httpx.Response(200, json={"content": [{"id": "complete", "name": "Complete"}], "last": True, "totalPages": 1}),
    ])
    image = _jpeg()
    thumbnail = respx_mock.get(f"{KOMGA}/api/v1/books/complete/thumbnail").respond(
        200, content=image, headers={"Content-Type": "image/jpeg"},
    )
    progress = []

    async def callback(current, total, title, status):
        progress.append((current, total, title, status))

    stats = await komga_sync.sync(on_progress=callback)
    assert stats == {"created": 1, "adopted": 0, "updated": 0, "unchanged": 0,
                     "skipped": 0, "in_trash": 0, "errors": 1, "covers": 1,
                     "cover_errors": 0, "added": 1}
    rows = db.execute("SELECT id, title, komga_id, komga_library_id, media_type, source, cover_path FROM items").fetchall()
    assert len(rows) == 1
    item_id = rows[0]["id"]
    assert tuple(rows[0]) == (item_id, "Complete", "complete", "good", "digital_comic", "komga", f"covers/{item_id}.jpg")
    assert [tuple(row) for row in db.execute("SELECT komga_id, item_id, library_id, kind FROM komga_records")] == [
        ("complete", item_id, "good", "comic"),
    ]
    assert [tuple(row) for row in db.execute("SELECT item_id, provider, external_id, library_id FROM digital_holdings")] == [
        (item_id, "komga", "complete", "good"),
    ]
    assert progress == [(1, 1, "Complete", "created")]
    assert listing.call_count == 2
    assert [(json.loads(call.request.content)["condition"]["allOf"][0]["libraryId"]["value"],
             call.request.url.params["page"]) for call in listing.calls] == [("bad", "0"), ("good", "0")]
    assert thumbnail.call_count == 1
    assert [call.request.url.path for call in respx_mock.calls] == [
        "/api/v1/libraries", "/api/v1/books/list", "/api/v1/books/list", "/api/v1/books/complete/thumbnail",
    ]
    assert (covers.COVERS_DIR / f"{item_id}.jpg").read_bytes() == image
    assert sorted(path.name for path in covers.COVERS_DIR.iterdir()) == [f"{item_id}.jpg"]


@pytest.mark.parametrize("provider", ["abs", "komga"])
@pytest.mark.parametrize("client_fixture,status", [
    ("admin_client", 303), ("editor_client", 403), ("viewer_client", 403),
])
def test_schedule_configuration_off_never_runs_provider_work(
    db, request, respx_mock, provider, client_fixture, status,
):
    client = request.getfixturevalue(client_fixture)
    feature = "abs_sync" if provider == "abs" else "komga"
    key = "abs_sync_interval" if provider == "abs" else "komga_sync_interval"
    name = "audiobookshelf" if provider == "abs" else "komga"
    set_feature_enabled(db, feature, False)
    db.commit()
    before = _snapshot(db)
    job_before = sync_jobs.get_status(name)
    response = client.post(f"/api/sync/{name}/schedule", data={"interval": "daily"}, follow_redirects=False)
    assert response.status_code == status
    assert get_setting(db, key) == ("daily" if status == 303 else "")
    assert _snapshot(db) == before
    assert sync_jobs.get_status(name) == job_before
    assert len(respx_mock.calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["abs", "komga"])
async def test_disabled_provider_sync_has_no_outbound_or_catalogue_effects(db, respx_mock, provider):
    set_feature_enabled(db, "abs_sync" if provider == "abs" else "komga", False)
    db.commit()
    before = _snapshot(db)
    if provider == "abs":
        result = await audiobookshelf.sync(ABS, "test-token")
        assert result == {"error": "Audiobookshelf sync is disabled"}
    else:
        result = await komga_sync.sync(url=KOMGA, api_key="test-key")
        assert result == {"error": "Komga is disabled"}
    assert _snapshot(db) == before
    assert len(respx_mock.calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,error", [
    ("abs", item_write.ItemValueError), ("komga", komga_sync.KomgaCoverError),
])
@pytest.mark.parametrize("mutation", ["trash", "identity", "library", "cover"])
async def test_cover_rechecks_binding_after_await_and_preserves_final_bytes(
    db, respx_mock, provider, error, mutation,
):
    item_id = _seed(db, provider)
    db.commit()
    original = _jpeg("red")
    concurrent = _jpeg("green")
    dest = covers.COVERS_DIR / f"{item_id}.jpg"
    dest.write_bytes(original)
    after_mutation = []

    async def change_target(request):
        with get_db() as connection, item_write._write_scope(connection):
            if mutation == "trash":
                item_write.trash_item(connection, item_id)
            elif mutation == "identity":
                if provider == "komga":
                    komga_records.detach_record(connection, "record-1")
                else:
                    item_write.update_item_fields(connection, item_id, {"abs_id": None, "abs_library_id": None})
                    connection.execute("DELETE FROM digital_holdings WHERE item_id = ? AND provider = 'audiobookshelf'", (item_id,))
                    holdings.sync_item(connection, item_id)
            elif mutation == "library":
                library_id = connection.execute("INSERT INTO libraries (name) VALUES ('Moved')").lastrowid
                connection.execute("UPDATE library_items SET library_id = ? WHERE item_id = ?", (library_id, item_id))
            else:
                with covers.CoverStage(item_id) as other:
                    path = covers.save_uploaded_cover(other.download_id, concurrent)
                    covers.publish_staged_cover(
                        connection, other, path,
                        authorize=lambda conn: catalogue_security.require_item_role(conn, item_id, "editor"),
                        store=lambda conn, published: item_write.update_item_fields(conn, item_id, {"cover_path": published}),
                    )
        with get_db() as connection:
            after_mutation.append(_snapshot(connection))
        return httpx.Response(200, content=_jpeg(), headers={"Content-Type": "image/jpeg"})

    route = respx_mock.get(_thumbnail_url(provider)).mock(side_effect=change_target)
    async with httpx.AsyncClient() as client:
        with pytest.raises(error):
            await _cover(client, provider, item_id, "record-1")
    assert route.call_count == 1
    assert _snapshot(db) == after_mutation[0]
    assert dest.read_bytes() == (concurrent if mutation == "cover" else original)
    assert sorted(path.name for path in covers.COVERS_DIR.iterdir()) == [dest.name]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["abs", "komga"])
@pytest.mark.parametrize("revocation", ["membership", "account"])
async def test_cover_rechecks_real_editor_authority_after_await(
    db, editor_user, respx_mock, provider, revocation,
):
    item_id = _seed(db, provider)
    db.commit()
    original = _jpeg("red")
    dest = covers.COVERS_DIR / f"{item_id}.jpg"
    dest.write_bytes(original)
    after_revocation = []

    async def revoke(request):
        with get_db() as connection:
            if revocation == "membership":
                connection.execute("UPDATE library_memberships SET role = 'viewer' WHERE user_id = ?", (editor_user["id"],))
            else:
                connection.execute("UPDATE users SET role = 'viewer' WHERE id = ?", (editor_user["id"],))
        with get_db() as connection:
            after_revocation.append(_snapshot(connection))
        return httpx.Response(200, content=_jpeg(), headers={"Content-Type": "image/jpeg"})

    route = respx_mock.get(_thumbnail_url(provider)).mock(side_effect=revoke)
    token = catalogue_security.set_actor(editor_user)
    try:
        async with httpx.AsyncClient() as client:
            if revocation == "membership":
                with pytest.raises(HTTPException) as exc:
                    await _cover(client, provider, item_id, "record-1")
                assert exc.value.status_code == 403
            else:
                error = item_write.ItemValueError if provider == "abs" else komga_sync.KomgaCoverError
                with pytest.raises(error, match="authority is no longer available"):
                    await _cover(client, provider, item_id, "record-1")
    finally:
        catalogue_security.reset_actor(token)
    assert route.call_count == 1
    assert _snapshot(db) == after_revocation[0]
    assert dest.read_bytes() == original
    assert sorted(path.name for path in covers.COVERS_DIR.iterdir()) == [dest.name]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["abs", "komga"])
async def test_cover_publication_recovers_bytes_when_outer_transaction_rolls_back(
    db, monkeypatch, respx_mock, provider,
):
    item_id = _seed(db, provider)
    db.commit()
    original = _jpeg("red")
    dest = covers.COVERS_DIR / f"{item_id}.jpg"
    dest.write_bytes(original)
    before = _snapshot(db)
    module = audiobookshelf if provider == "abs" else komga_sync
    reached = []

    class ForcedOuterRollback(RuntimeError):
        pass

    @contextmanager
    def rollback_publication():
        with get_db() as connection:
            yield connection
            row = connection.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()
            if row["cover_path"]:
                reached.append(row["cover_path"])
                assert dest.read_bytes() == _jpeg()
                raise ForcedOuterRollback("after publication, before outer commit")

    monkeypatch.setattr(module, "get_db", rollback_publication)
    route = respx_mock.get(_thumbnail_url(provider)).respond(200, content=_jpeg())
    async with httpx.AsyncClient() as client:
        with pytest.raises(ForcedOuterRollback):
            await _cover(client, provider, item_id, "record-1")
    assert route.call_count == 1
    assert reached == [f"covers/{item_id}.jpg"]
    assert _snapshot(db) == before
    assert dest.read_bytes() == original
    assert sorted(path.name for path in covers.COVERS_DIR.iterdir()) == [dest.name]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["abs", "komga"])
async def test_real_cover_bytes_are_published_and_existing_cover_is_not_refetched(db, respx_mock, provider):
    item_id = _seed(db, provider)
    db.commit()
    image = _jpeg()
    route = respx_mock.get(_thumbnail_url(provider)).respond(200, content=image, headers={"Content-Type": "image/jpeg"})
    async with httpx.AsyncClient() as client:
        assert await _cover(client, provider, item_id, "record-1") is True
        assert await _cover(client, provider, item_id, "record-1") is False
    assert route.call_count == 1
    assert db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()[0] == f"covers/{item_id}.jpg"
    assert (covers.COVERS_DIR / f"{item_id}.jpg").read_bytes() == image
    assert sorted(path.name for path in covers.COVERS_DIR.iterdir()) == [f"{item_id}.jpg"]


@pytest.mark.parametrize("source", ["manual", "komga"])
def test_exact_historical_physical_attachment_keeps_edition_copies_and_history(db, source):
    item_id = _insert_item(
        db, title="Historical Physical", isbn="9781582406725", media_type="comic",
        source=source, authors="Local Author", description="Local description",
        komga_id="record-1", komga_library_id="excluded",
    )
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, is_primary, copy_barcode, position_order, condition, notes) "
        "VALUES (?, 1, 1, 'historical-copy', 17, 'good', 'keep copy notes')", (item_id,),
    )
    db.execute("INSERT INTO reading_log (item_id, status, notes) VALUES (?, 'read', 'keep history')", (item_id,))
    db.execute("INSERT INTO scan_log (isbn, media_type, result, item_id) VALUES ('9781582406725', 'comic', 'added', ?)", (item_id,))
    before = _snapshot(db)
    result = komga_records.persist_candidate(db, _candidate(
        "record-1", title="Provider title", isbn="1582406723", authors="Provider author",
        description="Provider description",
    ))
    assert result == {"item_id": item_id, "action": "unchanged", "adopted": source == "manual"}
    row = db.execute("SELECT title, isbn, media_type, source, authors, description, komga_id FROM items").fetchone()
    assert tuple(row) == ("Historical Physical", "9781582406725", "comic", source,
                          "Local Author", "Local description", "record-1")
    after = _snapshot(db)
    for table in ("item_copies", "reading_log", "scan_log", "library_items", "user_item_state", "user_reading_log"):
        assert after[table] == before[table]
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert [tuple(row) for row in db.execute("SELECT item_id, komga_id, kind FROM komga_records")] == [(item_id, "record-1", "comic")]


@pytest.mark.parametrize("provider", ["abs", "komga"])
@pytest.mark.parametrize("retention", ["same_provider", "other_provider"])
def test_cleanup_trashes_once_detaches_adopted_and_protects_retained_holdings(
    db, admin_client, respx_mock, provider, retention,
):
    owned = _seed(db, provider, "owned")
    adopted = _seed(db, provider, "adopted", source="manual")
    retained = _seed(db, provider, "retained")
    provider_name = "komga" if provider == "komga" else "audiobookshelf"
    other_provider = provider_name if retention == "same_provider" else (
        "audiobookshelf" if provider == "komga" else "komga")
    # Captured plural-attachment history: not an ISBN/title-based takeover.
    connection = db.execute(
        "INSERT INTO digital_holdings (item_id, provider, external_id, library_id) VALUES (?, ?, 'kept-id', 'kept')",
        (retained, other_provider),
    )
    holding_id = connection.lastrowid
    if other_provider == "komga":
        db.execute("INSERT INTO komga_records (komga_id, item_id, library_id, kind) VALUES ('kept-id', ?, 'kept', 'comic')", (retained,))
    for item_id in (owned, adopted, retained):
        db.execute("INSERT INTO scan_log (isbn, media_type, result, item_id) VALUES (?, 'audiobook', 'added', ?)", (str(item_id), item_id))
        db.execute("INSERT INTO reading_log (item_id, status, notes) VALUES (?, 'read', 'retain history')", (item_id,))
    db.execute("INSERT INTO settings (key, value) VALUES (?, '[\"excluded\"]')", (
        "abs_excluded_libraries" if provider == "abs" else "komga_excluded_libraries",
    ))
    if provider == "abs":
        db.execute("INSERT INTO settings (key, value) VALUES ('abs_url', ?)", (ABS,))
        db.execute("INSERT INTO settings (key, value) VALUES ('abs_token', 'test-token')")
        respx_mock.get(f"{ABS}/api/libraries/excluded/items").respond(200, json={"results": [
            {"id": external_id} for external_id in ("owned", "adopted", "retained")
        ]})
    db.commit()
    holding_query = ("SELECT id, item_id, provider, external_id, library_id, format, source_url, created_at "
                     "FROM digital_holdings WHERE id = ?")
    holding = tuple(db.execute(holding_query, (holding_id,)).fetchone())
    before = _snapshot(db)
    endpoint = f"/api/sync/{provider_name}/libraries/cleanup"
    response = admin_client.post(endpoint)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": 1, "detached": 2}
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 3
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (owned,)).fetchone()[0] is not None
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (adopted,)).fetchone()[0] is None
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (retained,)).fetchone()[0] is None
    # Projection may stamp updated_at, but cannot replace the holding or erase
    # its exact identity/format/source/creation history.
    assert tuple(db.execute(holding_query, (holding_id,)).fetchone()) == holding
    assert db.execute("SELECT source FROM items WHERE id = ?", (adopted,)).fetchone()[0] == "manual"
    after = _snapshot(db)
    for table in ("reading_log", "scan_log", "user_item_state", "user_reading_log", "item_copies"):
        assert after[table] == before[table]
    again = admin_client.post(endpoint)
    assert again.status_code == 200
    assert again.json() == {"ok": True, "deleted": 0, "detached": 0}
    assert _snapshot(db) == after


@pytest.mark.parametrize("provider", ["abs", "komga"])
def test_cleanup_busy_is_409_with_no_outbound_or_writes(db, admin_client, respx_mock, provider):
    _seed(db, provider)
    db.commit()
    before = _snapshot(db)
    module = audiobookshelf if provider == "abs" else komga_sync
    provider_name = "audiobookshelf" if provider == "abs" else "komga"
    assert module._sync_lock.acquire(blocking=False)
    try:
        response = admin_client.post(f"/api/sync/{provider_name}/libraries/cleanup")
    finally:
        module._sync_lock.release()
    assert response.status_code == 409
    label = "Audiobookshelf" if provider == "abs" else "Komga"
    assert response.json() == {"ok": False, "message": f"{label} sync is running"}
    assert _snapshot(db) == before
    assert len(respx_mock.calls) == 0


def test_manual_digital_reclassification_endpoint_refuses_before_settings_or_sidecars(db, admin_client):
    item_id = _seed(db, "komga", source="manual")
    komga_records.persist_candidate(db, _candidate("record-1"))
    db.commit()
    before = _snapshot(db)
    settings = [tuple(row) for row in db.execute("SELECT * FROM settings ORDER BY key")]
    response = admin_client.post("/api/sync/komga/libraries", json={
        "excluded": [], "media_types": {"excluded": "digital_manga"},
    })
    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "Komga reclassification would change a manually catalogued item"}
    assert _snapshot(db) == before
    assert [tuple(row) for row in db.execute("SELECT * FROM settings ORDER BY key")] == settings
    assert db.execute("SELECT media_type FROM items WHERE id = ?", (item_id,)).fetchone()[0] == "digital_comic"
