"""Future synthetic music-cover regressions; authored, not executed here.

Provider/image boundaries are substituted; SQL, snapshots, publication and native
forms remain real. Synthetic fault cases are not native durability/DNS proof.
"""

from contextlib import contextmanager
import json
from uuid import uuid4

import pytest

from app import config
from app.database import get_db, set_setting
from app.routers import items_common, items_covers
from app.services import cover_queue, cover_sync_policy, discogs, discogs_selection, item_write
from app.services import music_artwork, music_auto_covers, music_catalog, musicbrainz, provider_result
from tests.test_add_media_search import form, io, pairs, post, preview, release, search, snapshot  # noqa: F401


MBID = "11111111-1111-4111-8111-111111111111"
GROUP = "22222222-2222-4222-8222-222222222222"
FRONT = "https://coverartarchive.org/automatic-front.jpg"
DISCOGS_FRONT = "https://i.discogs.com/automatic-front.jpg"
KINDS = ("vinyl", "cd", "cassette", "digital_music")


def _seed(db, *, kind="cd", mbid=None, group=None, discogs_id=None, authors="Artist", **changes):
    item_id = item_write.insert_item(db, title="Album", authors=authors, media_type=kind, source="manual", **changes)
    if mbid or group or discogs_id:
        music_catalog.save_release(db, item_id, {"musicbrainz_release_id": mbid, "musicbrainz_release_group_id": group,
                                                "artist_credit": authors, "source": "musicbrainz" if mbid else "discogs"})
    if discogs_id:
        discogs_selection.set_selected_release_id(db, item_id, discogs_id)
    db.commit()
    return item_id


def _invariants(db):
    rows, _ = snapshot(db)
    rows.pop("items")
    ignored = {"cover_path", "cover_edition_isbn", "cover_edition_language", "updated_at"}
    rows["items_except_cover_fields"] = [tuple((key, row[key]) for key in row.keys() if key not in ignored)
                                         for row in db.execute("SELECT * FROM items ORDER BY id")]
    return rows


def _stage(item_id):
    name = f"{item_id}-{uuid4().hex}.jpg"
    config.COVERS_DIR.mkdir(parents=True, exist_ok=True)
    (config.COVERS_DIR / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"synthetic-image" * 20)
    return "covers/" + name


def _images(io):
    async def download(item_id, url):
        assert url in {FRONT, DISCOGS_FRONT}
        # A second writer can acquire its lock while image work runs.
        with get_db() as other:
            other.execute("BEGIN IMMEDIATE")
        return _stage(item_id)
    io.install(music_artwork, "download", handler=download)


def _discogs(identity, *, master=99, title="Album", artist="Artist"):
    return provider_result.found("discogs", {"discogs_release_id": identity, "discogs_master_id": master,
        "title": title, "artist_credit": artist, "images": [{"url": DISCOGS_FRONT, "label": "Primary"}]})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_exact_release_front_is_filled_without_changing_identity_metadata_or_history(db, io, kind):
    item_id = _seed(db, kind=kind, mbid=MBID, group=GROUP)
    before = _invariants(db)
    io.install(musicbrainz, "cover_art", provider_result.found("coverartarchive", [
        {"url": "https://coverartarchive.org/back.jpg", "front": False}, {"url": FRONT, "front": True}]))
    _images(io)
    path = await items_common.resolve_missing_cover(item_id, object(), hints={"cover_url": "https://openlibrary.org/wrong-book.jpg"})
    assert path and db.execute("SELECT cover_path, isbn, upc FROM items WHERE id = ?", (item_id,)).fetchone()["cover_path"] == path
    assert (config.COVERS_DIR / path.split("/")[-1]).exists()
    assert _invariants(db) == before
    assert len(io.seen(musicbrainz, "cover_art")) == 1
    assert io.seen(discogs, "lookup_public_release") == []


@pytest.mark.asyncio
async def test_saved_discogs_release_supplies_primary_art_without_a_token(db, io):
    item_id = _seed(db, discogs_id=77)
    before = _invariants(db)
    io.install(discogs, "lookup_public_release", _discogs(77))
    _images(io)
    assert await music_auto_covers.resolve(item_id, object())
    assert io.seen(discogs, "lookup_public_release")[0][0][0] == 77
    assert io.seen(discogs, "lookup_public_release")[0][1]["token"] == ""
    assert io.seen(discogs, "lookup_release") == []
    assert _invariants(db) == before


@pytest.mark.asyncio
async def test_legacy_release_request_cannot_fetch_art_for_a_replaced_binding(db, io):
    item_id = _seed(db, mbid=MBID)
    before = snapshot(db)
    assert await music_auto_covers.resolve(item_id, object(), expected_release_id=GROUP) is None
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.asyncio
async def test_legacy_music_helper_uses_the_shared_front_only_fill_guard(db, io):
    from app.routers import music

    item_id = _seed(db, mbid=MBID)
    before = _invariants(db)
    io.install(musicbrainz, "cover_art", provider_result.found("coverartarchive", [
        {"url": "https://coverartarchive.org/back.jpg", "front": False}, {"url": FRONT, "front": True}]))
    _images(io)
    await music._apply_release_artwork(item_id, MBID)
    assert db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()[0]
    assert io.seen(music_artwork, "release_candidates") == []
    assert _invariants(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("protection", ["chosen", "removed", "dismissed", "trash", "disabled"])
async def test_protected_music_is_never_queued_or_contacted(db, io, protection):
    item_id = _seed(db, mbid=MBID)
    if protection == "chosen":
        db.execute("UPDATE items SET cover_path = 'covers/kept.jpg' WHERE id = ?", (item_id,))
    elif protection == "removed":
        cover_sync_policy.block(db, item_id)
    elif protection == "dismissed":
        db.execute("UPDATE items SET cover_review_dismissed = 1 WHERE id = ?", (item_id,))
    elif protection == "trash":
        db.execute("UPDATE items SET deleted_at = '2026-01-01' WHERE id = ?", (item_id,))
    else:
        set_setting(db, "feature.music", "0")
    db.commit()
    before = snapshot(db)
    assert music_auto_covers.enqueue_missing(item_id) is False
    assert await music_auto_covers.resolve(item_id, object()) is None
    assert io.calls == [] and snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["chosen", "removed", "dismissed", "rematched", "title", "trash", "disabled", "credentials"])
async def test_concurrent_changes_refuse_publication_and_remove_only_this_unique_staged_file(db, io, change):
    item_id = _seed(db, mbid=MBID)
    sentinel = config.COVERS_DIR / "kept-sentinel.jpg"
    sentinel.write_bytes(b"keep this file")
    staged = []
    io.install(musicbrainz, "cover_art", provider_result.found("coverartarchive", [{"url": FRONT, "front": True}]))

    async def download(item, url):
        path = _stage(item)
        staged.append(path)
        with get_db() as other:
            other.execute("BEGIN IMMEDIATE")
            if change == "chosen":
                other.execute("UPDATE items SET cover_path = 'covers/chosen.jpg' WHERE id = ?", (item,))
            elif change == "removed":
                cover_sync_policy.block(other, item)
            elif change == "dismissed":
                other.execute("UPDATE items SET cover_review_dismissed = 1 WHERE id = ?", (item,))
            elif change == "rematched":
                other.execute("UPDATE music_releases SET musicbrainz_release_id = ? WHERE item_id = ?", (GROUP, item))
            elif change == "title":
                item_write.update_item_fields(other, item, {"title": "A corrected title"})
            elif change == "trash":
                other.execute("UPDATE items SET deleted_at = '2026-01-01' WHERE id = ?", (item,))
            else:
                set_setting(other, "feature.music" if change == "disabled" else "discogs_token", "0" if change == "disabled" else "changed-synthetic-token")
        return path

    io.install(music_artwork, "download", handler=download)
    assert await music_auto_covers.resolve(item_id, object()) is None
    assert len(staged) == 1 and not (config.COVERS_DIR / staged[0].split("/")[-1]).exists()
    assert sentinel.read_bytes() == b"keep this file"
    assert db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()[0] == ("covers/chosen.jpg" if change == "chosen" else None)


def test_synthetic_fault_after_real_commit_keeps_potentially_referenced_artwork(db, monkeypatch):
    item_id = _seed(db, mbid=MBID)
    with get_db() as reader:
        reader.execute("BEGIN")
        expected = music_auto_covers._state(reader, item_id)[1]
    path = _stage(item_id)

    @contextmanager
    def uncertain_acknowledgement():
        with get_db() as writer:
            yield writer
        raise OSError("Synthetic acknowledgement fault after commit, not a native durability test")

    monkeypatch.setattr(music_auto_covers, "get_db", uncertain_acknowledgement)
    with pytest.raises(OSError, match="Synthetic acknowledgement"):
        music_auto_covers._publish(item_id, path, expected)
    assert (config.COVERS_DIR / path.split("/")[-1]).exists()
    assert db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()[0] == path


@pytest.mark.asyncio
async def test_manual_album_fallback_uses_one_matching_group_without_assigning_release_ids(db, io):
    item_id = _seed(db)
    before = _invariants(db)
    io.install(musicbrainz, "search_releases", provider_result.found("musicbrainz", [
        {"musicbrainz_release_id": MBID, "musicbrainz_release_group_id": GROUP, "title": "Album", "artist_credit": "Artist"}]))
    io.install(discogs, "search_public_releases", provider_result.found("discogs", []))
    io.install(musicbrainz, "cover_art", provider_result.found("coverartarchive", [{"url": FRONT, "front": True}]))
    _images(io)
    assert await music_auto_covers.resolve(item_id, object())
    assert io.seen(musicbrainz, "cover_art")[0][0][0] == GROUP
    assert io.seen(musicbrainz, "cover_art")[0][1] == {"group": True}
    assert _invariants(db) == before
    assert db.execute("SELECT COUNT(*) FROM music_releases WHERE item_id = ?", (item_id,)).fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("masters,found", [([99, 99], True), ([99, 100], False), ([99, None], False)])
async def test_discogs_album_art_requires_structured_artist_title_and_nonconflicting_masters(db, io, masters, found):
    item_id = _seed(db)
    before = _invariants(db)
    io.install(musicbrainz, "search_releases", provider_result.no_match("musicbrainz", status=200))
    candidates = [{"discogs_release_id": index + 1, "title": "Untrusted composite display", "artist_credit": None}
                  for index in range(len(masters))]
    io.install(discogs, "search_public_releases", provider_result.found("discogs", candidates))

    async def details(identity, client, **kwargs):
        return _discogs(identity, master=masters[identity - 1])
    io.install(discogs, "lookup_public_release", handler=details)
    _images(io)
    path = await music_auto_covers.resolve(item_id, object())
    assert bool(path) is found
    assert len(io.seen(discogs, "lookup_public_release")) == len(masters)
    assert _invariants(db) == before


@pytest.mark.asyncio
async def test_conflicting_musicbrainz_albums_do_not_pick_a_first_result(db, io):
    item_id = _seed(db)
    io.install(musicbrainz, "search_releases", provider_result.found("musicbrainz", [
        {"musicbrainz_release_id": MBID, "musicbrainz_release_group_id": group, "title": "Album", "artist_credit": "Artist"}
        for group in (GROUP, MBID)]))
    io.install(discogs, "search_public_releases", provider_result.found("discogs", []))
    assert await music_auto_covers.resolve(item_id, object()) is None
    assert io.seen(musicbrainz, "cover_art") == [] and io.seen(music_artwork, "download") == []


@pytest.mark.asyncio
async def test_album_detail_work_is_bounded_and_back_or_untrusted_images_are_not_selected(db, io):
    item_id = _seed(db)
    io.install(musicbrainz, "search_releases", provider_result.no_match("musicbrainz", status=200))
    io.install(discogs, "search_public_releases", provider_result.found("discogs", [
        {"discogs_release_id": index + 1, "title": "Composite"} for index in range(20)]))
    async def details(identity, client, **kwargs):
        return _discogs(identity, title="Not the album")
    io.install(discogs, "lookup_public_release", handler=details)
    assert await music_auto_covers.resolve(item_id, object()) is None
    assert len(io.seen(discogs, "lookup_public_release")) == 3
    assert music_auto_covers._fronts(provider_result.found("coverartarchive", [
        {"url": FRONT, "front": False}, {"url": "https://covers.openlibrary.org/wrong.jpg", "front": True},
        {"url": "https://user:secret@coverartarchive.org/front.jpg", "front": True}])) == []


def test_queue_type_extension_keeps_intakes_book_classification_unchanged(db):
    music = [_seed(db, kind=kind) for kind in KINDS]
    book = item_write.insert_item(db, title="A book", media_type="book")
    dvd = item_write.insert_item(db, title="A film", media_type="dvd")
    db.commit()
    assert not set(KINDS) & set(cover_queue.COVER_REQUEUE_MEDIA_TYPES)
    assert set(KINDS) <= set(cover_queue.AUTO_COVER_MEDIA_TYPES)
    assert cover_queue.filter_cover_eligible([dvd, music[2], book, music[0]]) == [music[2], book, music[0]]


def test_restart_recovery_is_recent_only_but_explicit_backfill_includes_older_music(db):
    recent, old, blocked = _seed(db), _seed(db), _seed(db)
    db.execute("UPDATE items SET created_at = '2001-01-01' WHERE id = ?", (old,))
    cover_sync_policy.block(db, blocked)
    dismissed_book = item_write.insert_item(db, title="Dismissed book", media_type="book")
    db.execute("UPDATE items SET cover_review_dismissed = 1 WHERE id = ?", (dismissed_book,))
    db.commit()
    assert cover_queue.requeue_recent_missing() == 1
    assert cover_queue._get_queue().get_nowait().item_id == recent
    assert {row["id"] for row in items_covers._missing_cover_items(db)} == {recent, old, dismissed_book}


@pytest.mark.parametrize("streaming", [False, True])
def test_existing_admin_retry_sweeps_coverless_music_of_any_age_without_book_fallback(admin_client, db, io, streaming):
    targets = [_seed(db, kind=kind, discogs_id=index + 10) for index, kind in enumerate(KINDS)]
    db.executemany("UPDATE items SET created_at = '2001-01-01' WHERE id = ?", [(item,) for item in targets])
    blocked = _seed(db, discogs_id=80)
    cover_sync_policy.block(db, blocked)
    dismissed = _seed(db, discogs_id=81)
    db.execute("UPDATE items SET cover_review_dismissed = 1 WHERE id = ?", (dismissed,))
    db.commit()
    before = _invariants(db)
    async def details(identity, client, **kwargs):
        return _discogs(identity)
    io.install(discogs, "lookup_public_release", handler=details)
    _images(io)
    response = admin_client.get("/api/covers/bulk-retry/stream") if streaming else admin_client.post("/api/covers/bulk-retry")
    assert response.status_code == 200
    if streaming:
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert events[-1] == {"type": "done", "success": 4, "failed": 0, "total": 4}
        assert len([event for event in events if event["type"] == "progress"]) == 4
    else:
        assert response.json() == {"success": 4, "failed": 0, "total": 4}
    assert all(db.execute("SELECT cover_path FROM items WHERE id = ?", (item,)).fetchone()[0] for item in targets)
    assert all(db.execute("SELECT cover_path FROM items WHERE id = ?", (item,)).fetchone()[0] is None for item in (blocked, dismissed))
    assert _invariants(db) == before


def test_single_music_retry_does_not_require_an_isbn(editor_client, db, io):
    item_id = _seed(db, discogs_id=77)
    io.install(discogs, "lookup_public_release", _discogs(77))
    _images(io)
    response = editor_client.post(f"/api/items/{item_id}/retry-cover")
    assert response.status_code == 200 and response.json()["ok"]
    assert db.execute("SELECT isbn FROM items WHERE id = ?", (item_id,)).fetchone()[0] is None


def test_add_music_queues_only_after_the_catalogue_and_exact_binding_commit(editor_client, db, io, monkeypatch):
    io.install(discogs, "search_public_releases", provider_result.found("discogs", [release("discogs", "cd")]))
    io.install(discogs, "lookup_public_release", provider_result.found("discogs", release("discogs", "cd")))
    queued = []
    def enqueue(item_id):
        with get_db() as observer:
            assert observer.execute("SELECT id FROM items_live WHERE id = ?", (item_id,)).fetchone()
            assert discogs_selection.get_selected_release_id(observer, item_id) == 77
        queued.append(item_id)
    monkeypatch.setattr(cover_queue, "enqueue", enqueue)
    searched = search(editor_client, provider="discogs", media_type="cd", q="Album")
    reviewed = preview(editor_client, searched)
    assert queued == []
    save = form(reviewed, "add-media-save")
    response = post(editor_client, save["attrs"]["action"], pairs(save))
    assert response.status_code == 303 and len(queued) == 1
    assert io.seen(music_artwork, "download") == []
    assert db.execute("SELECT cover_path FROM items WHERE id = ?", (queued[0],)).fetchone()[0] is None


def test_viewer_cannot_start_retry_or_reenable_and_queue_failure_does_not_undo_a_commit(viewer_client, db, monkeypatch):
    item_id = _seed(db, discogs_id=77)
    assert viewer_client.post(f"/api/items/{item_id}/retry-cover").status_code == 403
    assert viewer_client.post("/api/covers/bulk-retry").status_code == 403
    assert viewer_client.post(f"/api/items/{item_id}/cover-sync/allow").status_code == 403
    def broken(*args):
        raise RuntimeError("Synthetic optional queue fault")
    monkeypatch.setattr(cover_queue, "enqueue", broken)
    assert music_auto_covers.enqueue_missing(item_id) is False
    assert db.execute("SELECT id FROM items_live WHERE id = ?", (item_id,)).fetchone()
