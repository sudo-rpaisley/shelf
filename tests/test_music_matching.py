"""Music-specific matching and artwork, with no live provider requests."""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import MUSIC_MEDIA_TYPES
from app.services import covers, music_artwork, music_matching, musicbrainz, provider_result

MBID = "11111111-1111-4111-8111-111111111111"
GROUP = "22222222-2222-4222-8222-222222222222"


@pytest.mark.parametrize("value", [MBID, f"https://musicbrainz.org/release/{MBID}", f"https://www.musicbrainz.org/release/{MBID}/"])
def test_accepts_only_exact_release_identifiers(value):
    assert music_matching.release_id(value) == MBID


@pytest.mark.parametrize("value", ["", "../../secret", "x" * 36, "{123}",
    f"https://musicbrainz.org/release-group/{MBID}", f"http://musicbrainz.org/release/{MBID}",
    f"https://musicbrainz.org.evil.test/release/{MBID}", f"https://user:pass@musicbrainz.org/release/{MBID}",
    f"https://musicbrainz.org:8443/release/{MBID}", f"https://musicbrainz.org/release/{MBID}?token=secret"])
def test_rejects_nonrelease_or_unsafe_identifiers(value):
    assert music_matching.release_id(value) is None


def test_ranking_explains_identifier_matches_and_format_conflicts():
    results = music_matching.rank([
        {"title": "Other album", "artist_credit": "Other artist", "barcode": "012345678905", "format_summary": "CD"},
        {"title": "My Album", "artist_credit": "Björk", "catalog_number": "ABC-12", "format_summary": '12" Vinyl', "country": "GB"},
    ], title="My album", artist="Bjork", barcode="0012345678905", catalog_number="abc 12", format="vinyl", country="GB")
    assert "barcode" in results[0]["match_reasons"]
    assert "format" in results[0]["match_conflicts"]
    assert set(results[1]["match_reasons"]) == {"title", "artist", "catalog_number", "format", "country"}


@pytest.mark.asyncio
async def test_identifier_search_does_not_and_an_unreliable_local_title(monkeypatch):
    search = AsyncMock(return_value=provider_result.found("musicbrainz", [{"title": "Correct title", "barcode": "012345678905", "format_summary": "Vinyl"}]))
    monkeypatch.setattr(musicbrainz, "search_releases", search)
    result = await music_matching.search(object(), title="Wrong local title", artist="Unknown", barcode="012345678905", format="vinyl")
    assert result.found and result.payload[0]["match_reasons"] == ["barcode", "format"]
    assert search.await_args.args[0] == ""
    assert search.await_args.kwargs["barcode"] == "012345678905"
    assert "artist" not in search.await_args.kwargs


@pytest.mark.asyncio
async def test_zero_padded_barcode_and_title_fallback_are_bounded(monkeypatch):
    search = AsyncMock(side_effect=[provider_result.found("musicbrainz", []), provider_result.found("musicbrainz", []),
                                   provider_result.found("musicbrainz", [{"title": "Album", "barcode": "999999999999", "format_summary": "Vinyl"}])])
    monkeypatch.setattr(musicbrainz, "search_releases", search)
    result = await music_matching.search(object(), title="Album", barcode="012345678905")
    assert search.await_count == 3
    assert search.await_args_list[1].kwargs["barcode"] == "0012345678905"
    assert "barcode" in result.payload[0]["match_conflicts"]
    assert "barcode" not in result.payload[0]["match_reasons"]


@pytest.mark.asyncio
async def test_lucene_literals_and_format_filters_are_escaped(monkeypatch):
    fetch = AsyncMock(return_value=httpx.Response(200, json={"releases": []}))
    monkeypatch.setattr(musicbrainz.outbound, "fetch", fetch)
    await musicbrainz.search_releases('A "B" / C', object(), artist="AC/DC", media_type="vinyl", country="GB")
    query = fetch.await_args.kwargs["params"]["query"]
    assert 'release:"A \\"B\\" \\/ C"' == query.split(" AND ")[0]
    assert 'artist:"AC\\/DC"' in query
    assert "format:(Vinyl OR 7vinyl OR 10vinyl OR 12vinyl)" in query
    assert 'country:"GB"' in query


@pytest.mark.asyncio
async def test_caa_listing_redirect_and_http_art_urls_work(monkeypatch):
    fetch = AsyncMock(side_effect=[
        httpx.Response(307, headers={"Location": f"http://archive.org/download/mbid-{MBID}/index.json"}),
        httpx.Response(200, json={"images": [{"image": f"http://coverartarchive.org/release/{MBID}/1.jpg", "front": True,
                                            "types": ["Front"], "thumbnails": {"500": f"http://coverartarchive.org/release/{MBID}/1-500.jpg"}}]}),
    ])
    monkeypatch.setattr(musicbrainz.outbound, "fetch", fetch)
    result = await musicbrainz.cover_art(MBID, object())
    assert result.found and result.payload[0]["url"].startswith("https://")
    assert result.payload[0]["thumbnail"].startswith("https://")
    assert fetch.await_args_list[1].args[2] == f"https://archive.org/download/mbid-{MBID}/index.json"
    assert all(call.kwargs["follow_redirects"] is False for call in fetch.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["http://127.0.0.1/secret", "https://evil.test/art", "https://archive.org/download/audio/file", "https://user:pass@coverartarchive.org/x"])
async def test_caa_redirects_are_validated_before_following(monkeypatch, location):
    fetch = AsyncMock(return_value=httpx.Response(307, headers={"Location": location}))
    monkeypatch.setattr(musicbrainz.outbound, "fetch", fetch)
    result = await musicbrainz.cover_art(MBID, object())
    assert result.outcome == "transport_failed"
    assert fetch.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", sorted(MUSIC_MEDIA_TYPES))
async def test_every_music_format_searches_music_not_books(monkeypatch, media_type):
    books = AsyncMock(side_effect=AssertionError("Music must not inherit book covers"))
    monkeypatch.setattr(covers, "search_book_covers", books)
    art = AsyncMock(return_value=provider_result.found("coverartarchive", [{"url": f"https://coverartarchive.org/release/{MBID}/1.jpg", "thumbnail": "https://coverartarchive.org/a.jpg", "front": True, "types": ["Front"], "source": "Cover Art Archive"}]))
    monkeypatch.setattr(musicbrainz, "cover_art", art)
    result = await covers.search_covers({"id": 1, "media_type": media_type, "title": "Album", "musicbrainz_release_id": MBID}, "Album", object(), creds={})
    assert result.found and result.payload[0]["source"] == "Cover Art Archive"
    books.assert_not_awaited()
    assert covers.required_credentials(media_type) == ()


@pytest.mark.asyncio
async def test_group_artwork_is_labelled_as_another_edition(monkeypatch):
    art = AsyncMock(side_effect=[provider_result.found("coverartarchive", []), provider_result.found("coverartarchive", [{"url": "https://coverartarchive.org/front.jpg", "front": True}])])
    monkeypatch.setattr(musicbrainz, "cover_art", art)
    result = await music_artwork.search({"title": "Album", "musicbrainz_release_id": MBID, "musicbrainz_release_group_id": GROUP}, "Album", object(), creds={})
    assert "another edition" in result.payload[0]["label"]
    assert art.await_args.kwargs == {"group": True}


@pytest.mark.asyncio
async def test_catalogue_number_is_tried_before_title_fallback(monkeypatch):
    lookup = AsyncMock(side_effect=[provider_result.found("musicbrainz", []), provider_result.found("musicbrainz", []),
                                   provider_result.found("musicbrainz", [{"title": "Exact pressing", "catalog_number": "CAT-123"}])])
    monkeypatch.setattr(musicbrainz, "search_releases", lookup)
    result = await music_matching.search(object(), title="Unreliable local title", barcode="012345678905", catalog_number="CAT-123")
    assert result.found and result.payload[0]["match_reasons"] == ["catalog_number"]
    assert lookup.await_count == 3 and lookup.await_args.kwargs["catalog_number"] == "CAT-123"
    assert lookup.await_args.args[0] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rate_limited", "transport_failed", "rejected"])
async def test_provider_failure_does_not_trigger_misleading_fallbacks(monkeypatch, failure):
    lookup = AsyncMock(return_value=getattr(provider_result, failure)("musicbrainz", **({"status": 400} if failure == 'rejected' else {})))
    monkeypatch.setattr(musicbrainz, "search_releases", lookup)
    result = await music_matching.search(object(), title="Album", barcode="012345678905", catalog_number="CAT-123")
    assert result.outcome == failure and lookup.await_count == 1


def test_discogs_tracklist_preserves_sides_and_skips_headings():
    from app.services import discogs
    release = discogs.normalise_release({"id": 12, "tracklist": [
        {"title": "Side A", "type_": "heading"},
        {"position": "A1", "title": "First track", "duration": "3:45"},
        {"type_": "index", "title": "Suite", "sub_tracks": [{"position": "B1a", "title": "Part one", "duration": "1:02:03"}]},
    ], "images": [{"uri": "https://i.discogs.com/front.jpeg", "uri150": "https://127.0.0.1/thumbnail.jpg", "type": "primary"},
                  {"uri": "https://i.discogs.com.evil.test/front.jpeg"}]})
    tracks = release["media"][0]["tracks"]
    assert [track["number"] for track in tracks] == ["A1", "B1a"]
    assert [track["duration_ms"] for track in tracks] == [225000, 3723000]
    assert len(release["images"]) == 1
    assert release['images'][0]['thumbnail'] == 'https://i.discogs.com/front.jpeg'


@pytest.mark.asyncio
async def test_manual_music_cover_downloads_use_new_filenames(monkeypatch):
    from app import config
    from app.services import manual_cover
    content = b'\x89PNG\r\n\x1a\n' + b'0' * 200
    monkeypatch.setattr(manual_cover, "_fetch_once", AsyncMock(return_value=(200, None, content)))
    first = await music_artwork.download(42, f"https://coverartarchive.org/release/{MBID}/front.jpg")
    second = await music_artwork.download(42, f"https://coverartarchive.org/release/{MBID}/back.jpg")
    assert first != second and first.startswith("covers/42-") and second.startswith("covers/42-")
    assert (config.COVERS_DIR / first.split('/')[-1]).read_bytes() == content


@pytest.mark.parametrize('kind,allowed', [(b'WEBP', True), (b'WAVE', False), (b'AVI ', False)])
def test_riff_audio_and_video_are_not_artwork(kind, allowed):
    assert covers.validate_uploaded_cover(b'RIFF' + b'\0' * 4 + kind + b'\0' * 200) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["chosen", "removed", "rematched"])
async def test_automatic_artwork_cannot_overwrite_a_concurrent_choice(db, monkeypatch, change):
    from app import config
    from app.routers import music
    from app.services import cover_sync_policy, music_catalog
    from tests.conftest import _insert_item
    item_id = _insert_item(db, title="Album", media_type="vinyl", isbn=None)
    music_catalog.save_release(db, item_id, {"musicbrainz_release_id": MBID, "source": "musicbrainz"})
    db.commit()
    monkeypatch.setattr(musicbrainz, "cover_art", AsyncMock(return_value=provider_result.found("coverartarchive", [{"url": "https://coverartarchive.org/front.jpg", "front": True}])))
    async def download(*args):
        from app.database import get_db
        with get_db() as other:
            if change == "chosen":
                other.execute("UPDATE items SET cover_path = 'covers/my-choice.jpg' WHERE id = ?", (item_id,))
            elif change == "removed":
                cover_sync_policy.block(other, item_id)
            else:
                other.execute("UPDATE music_releases SET musicbrainz_release_id = ? WHERE item_id = ?", (GROUP, item_id))
        (config.COVERS_DIR / 'automatic-stage.jpg').write_bytes(b'image')
        return 'covers/automatic-stage.jpg'
    monkeypatch.setattr(music_artwork, "download", download)
    await music._apply_release_artwork(item_id, MBID)
    assert not (config.COVERS_DIR / 'automatic-stage.jpg').exists()
    assert db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()[0] == ('covers/my-choice.jpg' if change == 'chosen' else None)
