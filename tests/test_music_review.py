"""Real catalogue writes from signed, selective and stale-safe music reviews."""

import html
import re
import time
from unittest.mock import AsyncMock

import jwt
import pytest

from app import auth
from app.config import JWT_ALGORITHM
from app.services import manual_cover, music_catalog, music_review, musicbrainz, provider_result, shop_metadata
from tests.conftest import _insert_item

MBID = "11111111-1111-4111-8111-111111111111"
RELEASE = {"musicbrainz_release_id": MBID, "title": "Matched album", "artist_credit": "The Artist", "label": "The Label",
           "release_date": "2026-10-01", "catalog_number": "CAT-123", "format_summary": "Vinyl", "source": "musicbrainz",
           "media": [{"position": 1, "format": "Vinyl", "tracks": [{"position": 1, "number": "A1", "title": "First track"}]}]}


def _seed(db, **values):
    item_id = _insert_item(db, title="My local album", authors="Local artist", media_type="vinyl", isbn=None,
                           cover_path="covers/chosen.jpg", notes="Do not lose my notes", **values)
    db.commit()
    return item_id


def _candidate(kind="musicbrainz", **values):
    return {"kind": kind, "source_url": f"https://musicbrainz.org/release/{MBID}" if kind == "musicbrainz" else "https://shop.example.test/album",
            "release": dict(RELEASE), "fields": music_review.fields_for(RELEASE), "images": [], **values}


def _ticket(db, user, item_id, candidate=None):
    return music_review.sign(candidate or _candidate(), user_id=user["id"], item_id=item_id,
                             fingerprint=music_review.fingerprint(db, item_id))


def _inputs(response, name):
    return [html.unescape(value) for value in re.findall(rf'name="{name}" value="([^"]*)"', response.text)]


def test_preview_reads_full_metadata_without_changing_item(editor_client, editor_user, db, monkeypatch):
    item_id = _seed(db)
    before = dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
    monkeypatch.setattr(musicbrainz, "lookup_release", AsyncMock(return_value=provider_result.found("musicbrainz", RELEASE)))
    monkeypatch.setattr(musicbrainz, "cover_art", AsyncMock(return_value=provider_result.found("coverartarchive", [{"url": "https://coverartarchive.org/front.jpg"}])))
    response = editor_client.post("/api/music/preview", data={"item_id": item_id, "url": f"https://musicbrainz.org/release/{MBID}"})
    assert response.status_code == 200
    assert "Matched album" in response.text and "First track" in response.text and "CAT-123" in response.text
    assert dict(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()) == before
    assert not db.execute("SELECT * FROM music_releases WHERE item_id = ?", (item_id,)).fetchone()
    ticket = _inputs(response, "ticket")[0]
    assert music_review.verify(ticket, user_id=editor_user["id"])["item"] == item_id
    assert 'name="fields" value="title" checked' not in response.text
    assert 'name="image" value="-1" checked' in response.text
    assert '<img src="https://' not in response.text


def test_apply_installs_exact_release_and_only_checked_fields(editor_client, editor_user, db):
    item_id = _seed(db)
    ticket = _ticket(db, editor_user, item_id)
    response = editor_client.post("/api/music/apply-match", data={"ticket": ticket, "item_id": item_id, "fields": ["publisher", "publish_year"], "image": "-1"},
                                  follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/music/item/{item_id}"
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert item["title"] == "My local album" and item["authors"] == "Local artist"
    assert item["publisher"] == "The Label" and item["publish_year"] == 2026
    assert item["cover_path"] == "covers/chosen.jpg" and item["notes"] == "Do not lose my notes"
    release = music_catalog.get_release(db, item_id)
    assert release["musicbrainz_release_id"] == MBID and release["media"][0]["tracks"][0]["number"] == "A1"


@pytest.mark.parametrize("mutation", ["tampered", "other_user", "expired", "image_token", "wrong_item"])
def test_reviews_are_user_item_purpose_and_expiry_bound(editor_client, editor_user, db, mutation):
    item_id = _seed(db)
    ticket = _ticket(db, editor_user, item_id)
    if mutation == "tampered":
        ticket = ticket[:-5] + "abcde"
    elif mutation == "other_user":
        ticket = _ticket(db, {"id": editor_user["id"] + 1}, item_id)
    elif mutation == "image_token":
        ticket = music_review.sign(_candidate(), user_id=editor_user["id"], item_id=item_id,
                                   fingerprint=music_review.fingerprint(db, item_id), purpose="shelf-music-image-v1")
    elif mutation == "expired":
        payload = jwt.decode(ticket, music_review._key(), algorithms=[JWT_ALGORITHM])
        payload["exp"] = int(time.time()) - 1
        ticket = jwt.encode(payload, music_review._key(), algorithm=JWT_ALGORITHM)
    response = editor_client.post("/api/music/apply-match", data={"ticket": ticket, "item_id": item_id + 1 if mutation == "wrong_item" else item_id, "fields": "title"})
    assert response.status_code == 200 and 'role="alert"' in response.text
    assert db.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()[0] == "My local album"
    assert not db.execute("SELECT * FROM music_releases").fetchone()


@pytest.mark.parametrize("change", ["edit", "trash"])
def test_stale_or_trashed_item_is_rejected_before_downloading(editor_client, editor_user, db, monkeypatch, change):
    item_id = _seed(db)
    ticket = _ticket(db, editor_user, item_id, _candidate(images=["https://images.example.test/front.jpg"]))
    db.execute("UPDATE items SET title = 'A concurrent edit' WHERE id = ?" if change == "edit" else "UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (item_id,))
    db.commit()
    download = AsyncMock(side_effect=AssertionError("Stale review must not download"))
    monkeypatch.setattr(manual_cover, "download", download)
    response = editor_client.post("/api/music/apply-match", data={"ticket": ticket, "item_id": item_id, "fields": "title", "image": "0"})
    assert 'role="alert"' in response.text
    download.assert_not_awaited()


def test_edit_during_download_preserves_original_cover_and_cleans_staging(editor_client, editor_user, db, monkeypatch):
    from app import config
    item_id = _seed(db)
    (config.COVERS_DIR / "chosen.jpg").write_bytes(b"original")
    ticket = _ticket(db, editor_user, item_id, _candidate(images=["https://images.example.test/front.jpg"]))
    async def download(*args, **kwargs):
        from app.database import get_db
        with get_db() as other:
            other.execute("UPDATE items SET title = 'Concurrent edit' WHERE id = ?", (item_id,))
        (config.COVERS_DIR / "staging.jpg").write_bytes(b"new image")
        return "covers/staging.jpg"
    monkeypatch.setattr(manual_cover, "download", download)
    response = editor_client.post("/api/music/apply-match", data={"ticket": ticket, "item_id": item_id, "fields": "title", "image": "0"})
    assert "changed while" in response.text
    assert (config.COVERS_DIR / "chosen.jpg").read_bytes() == b"original"
    assert not (config.COVERS_DIR / "staging.jpg").exists()
    assert db.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()[0] == "Concurrent edit"


def test_shop_review_keeps_existing_exact_release_and_tracks(editor_client, editor_user, db):
    item_id = _seed(db)
    music_catalog.save_release(db, item_id, RELEASE)
    db.commit()
    candidate = _candidate("shop", fields={"description": "Shop description"}, release={"source": "shop", "media": []})
    ticket = _ticket(db, editor_user, item_id, candidate)
    response = editor_client.post("/api/music/apply-match", data={"ticket": ticket, "item_id": item_id, "fields": "description"}, follow_redirects=False)
    assert response.status_code == 303
    assert music_catalog.get_release(db, item_id)["media"][0]["tracks"][0]["title"] == "First track"
    assert db.execute("SELECT description FROM items WHERE id = ?", (item_id,)).fetchone()[0] == "Shop description"
    assert db.execute("SELECT value FROM music_identifiers WHERE item_id = ? AND identifier_type = 'shop_url'", (item_id,)).fetchone()[0] == candidate["source_url"]


def test_new_shop_release_is_explicitly_added_with_validated_fields(editor_client, editor_user, db):
    candidate = _candidate("shop", fields={"title": "A shop album", "authors": "An Artist"}, release={**RELEASE, "musicbrainz_release_id": None, "source": "shop"})
    ticket = _ticket(db, editor_user, 0, candidate)
    response = editor_client.post("/api/music/apply-match", data={"ticket": ticket, "fields": ["title", "authors"], "media_type": "vinyl", "owned": "0"}, follow_redirects=False)
    assert response.status_code == 303
    row = db.execute("SELECT * FROM items WHERE title = 'A shop album'").fetchone()
    assert row["media_type"] == "vinyl" and row["owned"] == 0
    assert music_catalog.get_release(db, row["id"])["metadata_source"] == "shop"


def test_selecting_artwork_does_not_change_matched_release(editor_client, db, monkeypatch):
    from app.services import music_artwork
    item_id = _seed(db)
    music_catalog.save_release(db, item_id, RELEASE)
    db.commit()
    monkeypatch.setattr(music_artwork, "download", AsyncMock(return_value="covers/new-version.jpg"))
    response = editor_client.post(f"/api/items/{item_id}/cover-select", data={"url": "https://coverartarchive.org/front.jpg", "return_to": "music"})
    assert response.headers["HX-Redirect"] == f"/music/item/{item_id}"
    assert music_catalog.get_release(db, item_id)["musicbrainz_release_id"] == MBID
    assert db.execute("SELECT title, cover_path FROM items WHERE id = ?", (item_id,)).fetchone()["cover_path"] == "covers/new-version.jpg"


@pytest.mark.parametrize("endpoint", ["/api/music/preview", "/api/music/apply-match"])
def test_new_mutations_require_editor(viewer_client, endpoint):
    assert viewer_client.post(endpoint, data={}).status_code == 403


@pytest.mark.parametrize("endpoint", ["/api/music/preview", "/api/music/apply-match"])
@pytest.mark.parametrize("trailing_slash", [False, True])
@pytest.mark.parametrize("header", [False, True])
def test_plain_and_htmx_review_bodies_are_bounded(editor_client, endpoint, trailing_slash, header):
    from app.http_body import MUSIC_REVIEW_FORM_BYTES
    if not header:
        editor_client.headers.pop("X-CSRF-Token")
    response = editor_client.post(endpoint + ("/" if trailing_slash else ""), content=b"url=" + b"x" * (MUSIC_REVIEW_FORM_BYTES + 1),
                                  headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=True)
    assert response.status_code in (200, 413)
    assert "too large" in response.text


def test_shop_preview_does_not_persist_unreviewed_observations(editor_client, db, monkeypatch):
    item_id = _seed(db)
    monkeypatch.setattr(shop_metadata, "preview", AsyncMock(return_value=[_candidate("shop")]))
    response = editor_client.post("/api/music/preview", data={"item_id": item_id, "url": "https://shop.example.test/album"})
    assert "Shop-provided details" in response.text
    assert db.execute("SELECT COUNT(*) FROM music_releases").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM music_identifiers").fetchone()[0] == 0


@pytest.mark.parametrize("selected", [True, False])
def test_image_choice_applies_atomically_or_changes_nothing(editor_client, editor_user, db, monkeypatch, selected):
    from app import config
    from app.services import cover_sync_policy
    item_id = _seed(db)
    ticket = _ticket(db, editor_user, item_id, _candidate(images=["https://coverartarchive.org/front.jpg"]))
    (config.COVERS_DIR / 'versioned.jpg').write_bytes(b'image')
    download = AsyncMock(return_value="covers/versioned.jpg" if selected else None)
    monkeypatch.setattr(manual_cover, "download", download)
    response = editor_client.post("/api/music/apply-match", data={"ticket": ticket, "item_id": item_id, "fields": "title", "image": "0"}, follow_redirects=False)
    row = db.execute("SELECT title, cover_path FROM items WHERE id = ?", (item_id,)).fetchone()
    if selected:
        assert response.status_code == 303
        assert tuple(row) == ('Matched album', 'covers/versioned.jpg')
        assert cover_sync_policy.blocked(db, item_id)
    else:
        assert "Nothing was changed" in response.text
        assert tuple(row) == ('My local album', 'covers/chosen.jpg')
        assert music_catalog.get_release(db, item_id) is None
    download.assert_awaited_once_with(item_id, "https://coverartarchive.org/front.jpg", unique=True, caa=True)


@pytest.mark.parametrize("invalid", ["purpose", "user", "index"])
def test_preview_images_require_their_own_user_bound_token(editor_client, editor_user, db, monkeypatch, invalid):
    item_id = _seed(db)
    ticket = music_review.sign({"kind": "shop", "images": ["https://images.example.test/front.jpg"]},
        user_id=editor_user["id"] + (invalid == "user"), item_id=item_id, fingerprint=music_review.fingerprint(db, item_id),
        purpose="shelf-music-review-v1" if invalid == "purpose" else "shelf-music-image-v1")
    download = AsyncMock(side_effect=AssertionError("Unauthorized image request downloaded content"))
    monkeypatch.setattr(manual_cover, "download", download)
    response = editor_client.get("/api/music/preview-image", params={"ticket": ticket, "index": 9 if invalid == "index" else 0})
    assert response.status_code == 404
    download.assert_not_awaited()


def test_preview_image_is_same_origin_temporary_and_nonmutating(editor_client, editor_user, db, monkeypatch):
    from app import config
    item_id = _seed(db)
    ticket = music_review.sign({"kind": "shop", "images": ["https://images.example.test/front.jpg"]},
        user_id=editor_user["id"], item_id=item_id, fingerprint=music_review.fingerprint(db, item_id), purpose="shelf-music-image-v1")
    content = b'\x89PNG\r\n\x1a\n' + b'0' * 200
    async def download(*args, **kwargs):
        (config.COVERS_DIR / 'preview-stage.jpg').write_bytes(content)
        return 'covers/preview-stage.jpg'
    monkeypatch.setattr(manual_cover, "download", download)
    response = editor_client.get("/api/music/preview-image", params={"ticket": ticket, "index": 0})
    assert response.content == content and response.headers['content-type'] == 'image/png'
    assert response.headers['cache-control'].startswith('private')
    assert not (config.COVERS_DIR / 'preview-stage.jpg').exists()
    assert db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()[0] == 'covers/chosen.jpg'


def test_review_tickets_cannot_be_used_as_login_tokens(editor_user, db):
    assert auth.decode_token(_ticket(db, editor_user, 0)) is None


def test_exact_release_collision_rolls_back_everything(editor_client, editor_user, db):
    item_id = _seed(db)
    other_id = _insert_item(db, title="Already matched", media_type="vinyl", isbn=None)
    music_catalog.save_release(db, other_id, RELEASE)
    db.commit()
    response = editor_client.post('/api/music/apply-match', data={'ticket': _ticket(db, editor_user, item_id), 'item_id': item_id, 'fields': 'title'})
    assert 'No changes were saved' in response.text
    assert db.execute('SELECT title FROM items WHERE id = ?', (item_id,)).fetchone()[0] == 'My local album'
    assert music_catalog.get_release(db, item_id) is None
    assert music_catalog.get_release(db, other_id)['musicbrainz_release_id'] == MBID


def test_apply_preserves_copies_memberships_and_private_history(editor_client, editor_user, db):
    from app.services import personal_library
    item_id = _seed(db, owned=1)
    db.execute("INSERT INTO item_copies (item_id, copy_number, condition, acquisition_source) VALUES (?, 1, 'VG+', 'Record shop')", (item_id,))
    personal_library.save(db, editor_user['id'], item_id, personal_library.parse_form({'notes': 'My private listening diary', 'rating': '5'}))
    db.commit()
    tables = ('item_copies', 'list_items', 'user_item_state', 'user_item_events')
    before = {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in tables}
    response = editor_client.post('/api/music/apply-match', data={'ticket': _ticket(db, editor_user, item_id), 'item_id': item_id, 'fields': 'authors'}, follow_redirects=False)
    assert response.status_code == 303
    assert {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in tables} == before


@pytest.mark.parametrize('path,method', [('/api/music/match', 'get'), ('/api/music/preview', 'post'),
    ('/api/music/preview-image?ticket=x&index=0', 'get'), ('/api/music/apply-match', 'post')])
def test_music_feature_gates_all_new_endpoints(editor_client, path, method):
    from tests.test_features_gate import off
    off('music')
    assert getattr(editor_client, method)(path).status_code == 403


def test_generic_music_artwork_becomes_manual_only_when_feature_is_off(editor_client, db, monkeypatch):
    from tests.test_features_gate import off
    item_id = _seed(db)
    off('music')
    lookup = AsyncMock(side_effect=AssertionError('Disabled music feature contacted provider'))
    monkeypatch.setattr(musicbrainz, 'search_releases', lookup)
    response = editor_client.get(f'/api/items/{item_id}/cover-search?gallery=1')
    assert response.status_code == 200 and 'Music matching is turned off' in response.text
    assert 'data-testid="cover-upload"' in response.text
    lookup.assert_not_awaited()


def test_discogs_link_remembers_only_pressing_and_keeps_musicbrainz(editor_client, editor_user, db, monkeypatch):
    from app.database import set_setting
    from app.services import discogs, discogs_selection
    item_id = _seed(db)
    music_catalog.save_release(db, item_id, RELEASE)
    set_setting(db, 'discogs_token', 'discogs-fixture-token')
    db.commit()
    observation = discogs.normalise_release({'id': 123, 'title': 'Discogs edition', 'tracklist': [{'title': 'Provider-only track', 'position': 'A1'}]})
    monkeypatch.setattr(discogs, 'lookup_release', AsyncMock(return_value=provider_result.found('discogs', observation)))
    response = editor_client.post('/api/music/preview', data={'item_id': item_id, 'url': 'https://www.discogs.com/release/123-Album'})
    assert 'Provider-only track' in response.text and 'Data provided by Discogs' in response.text
    ticket = _inputs(response, 'ticket')[0]
    applied = editor_client.post('/api/music/apply-match', data={'ticket': ticket, 'item_id': item_id}, follow_redirects=False)
    assert applied.status_code == 303
    assert discogs_selection.get_selected_release_id(db, item_id) == 123
    release = music_catalog.get_release(db, item_id)
    assert release['musicbrainz_release_id'] == MBID and release['media'][0]['tracks'][0]['title'] == 'First track'


def test_sparse_shop_preview_cannot_erase_previously_collected_tracks(editor_client, editor_user, db):
    item_id = _seed(db)
    music_catalog.save_release(db, item_id, {**RELEASE, 'musicbrainz_release_id': None, 'source': 'shop'})
    db.commit()
    candidate = _candidate('shop', release={'source': 'shop'}, fields={'description': 'Extra shop notes'})
    response = editor_client.post('/api/music/apply-match', data={'ticket': _ticket(db, editor_user, item_id, candidate), 'item_id': item_id, 'fields': 'description'}, follow_redirects=False)
    assert response.status_code == 303
    release = music_catalog.get_release(db, item_id)
    assert release['metadata_source'] == 'shop' and release['catalog_number'] == 'CAT-123'
    assert release['media'][0]['tracks'][0]['title'] == 'First track'
