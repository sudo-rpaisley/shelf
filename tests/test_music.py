"""First-class music catalogue regression tests."""

from app.config import MEDIA_TYPES, MUSIC_MEDIA_TYPES
from app.services import music_catalog, musicbrainz, provider_result
from app.services.item_write import insert_item


_SAMPLE_RELEASE = {
    "title": "The Dark Side of the Moon",
    "artist_credit": "Pink Floyd",
    "musicbrainz_release_id": "11111111-1111-1111-1111-111111111111",
    "musicbrainz_release_group_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "release_type": "Album",
    "release_status": "Official",
    "release_date": "1973-03-23",
    "first_release_date": "1973-03-01",
    "country": "GB",
    "label": "Harvest",
    "catalog_number": "SHVL 804",
    "barcode": None,
    "packaging": "Gatefold Cover",
    "media_count": 1,
    "format_summary": "12\" Vinyl",
    "source": "musicbrainz",
    "media": [
        {
            "position": 1,
            "format": "12\" Vinyl",
            "title": None,
            "track_count": 2,
            "tracks": [
                {
                    "position": 1,
                    "number": "A1",
                    "title": "Speak to Me",
                    "artist_credit": "Pink Floyd",
                    "duration_ms": 65000,
                    "musicbrainz_recording_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                },
                {
                    "position": 2,
                    "number": "B1",
                    "title": "Money",
                    "artist_credit": "Pink Floyd",
                    "duration_ms": 382000,
                    "musicbrainz_recording_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                },
            ],
        }
    ],
}


class TestMusicMediaTypes:
    def test_every_music_type_is_a_public_media_type(self):
        assert MUSIC_MEDIA_TYPES == {
            "vinyl", "cassette", "cd", "digital_music", "music_other"
        }
        assert MUSIC_MEDIA_TYPES <= MEDIA_TYPES.keys()


class TestMusicSchema:
    def test_music_tables_exist_on_a_fresh_database(self, db):
        names = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "music_releases", "music_media", "music_tracks", "music_identifiers"
        } <= names

    def test_track_number_is_text_for_vinyl_side_numbers(self, db):
        columns = {
            row["name"]: row["type"]
            for row in db.execute("PRAGMA table_info(music_tracks)").fetchall()
        }
        assert columns["number"].upper() == "TEXT"


class TestMusicBrainzNormalisation:
    def test_exact_release_keeps_media_and_side_numbering(self):
        raw = {
            "id": _SAMPLE_RELEASE["musicbrainz_release_id"],
            "title": _SAMPLE_RELEASE["title"],
            "artist-credit": [
                {"name": "Pink Floyd", "artist": {"name": "Pink Floyd"}, "joinphrase": ""}
            ],
            "status": "Official",
            "date": "1973-03-23",
            "country": "GB",
            "packaging": "Gatefold Cover",
            "release-group": {
                "id": _SAMPLE_RELEASE["musicbrainz_release_group_id"],
                "primary-type": "Album",
                "first-release-date": "1973-03-01",
            },
            "label-info": [
                {"catalog-number": "SHVL 804", "label": {"name": "Harvest"}}
            ],
            "media": [
                {
                    "position": 1,
                    "format": "12\" Vinyl",
                    "track-count": 2,
                    "tracks": [
                        {
                            "position": 1,
                            "number": "A1",
                            "title": "Speak to Me",
                            "length": 65000,
                            "recording": {"id": "rec-a", "title": "Speak to Me"},
                        },
                        {
                            "position": 2,
                            "number": "B1",
                            "title": "Money",
                            "length": 382000,
                            "recording": {"id": "rec-b", "title": "Money"},
                        },
                    ],
                }
            ],
        }
        release = musicbrainz.normalise_release(raw)
        assert release["musicbrainz_release_id"] == raw["id"]
        assert release["musicbrainz_release_group_id"] == raw["release-group"]["id"]
        assert release["artist_credit"] == "Pink Floyd"
        assert release["catalog_number"] == "SHVL 804"
        assert release["media"][0]["tracks"][0]["number"] == "A1"
        assert release["media"][0]["tracks"][1]["number"] == "B1"


class TestMusicPersistence:
    def test_release_media_tracks_and_identifiers_round_trip(self, db):
        item_id = insert_item(
            db,
            title=_SAMPLE_RELEASE["title"],
            authors="Pink Floyd",
            media_type="vinyl",
            source="musicbrainz",
        )
        music_catalog.save_release(db, item_id, _SAMPLE_RELEASE)
        music_catalog.add_identifier(
            db, item_id, "matrix_runout", "SHVL 804 A-2", "Side A"
        )

        release = music_catalog.get_release(db, item_id)
        assert release["catalog_number"] == "SHVL 804"
        assert release["media"][0]["format"] == "12\" Vinyl"
        assert [t["number"] for t in release["media"][0]["tracks"]] == ["A1", "B1"]
        assert release["identifiers"][0]["value"] == "SHVL 804 A-2"

    def test_same_release_group_auto_links_different_formats(self, db):
        vinyl_id = insert_item(db, title="Album", media_type="vinyl")
        cd_id = insert_item(db, title="Album", media_type="cd")

        vinyl = dict(_SAMPLE_RELEASE)
        cd = dict(_SAMPLE_RELEASE)
        cd["musicbrainz_release_id"] = "22222222-2222-2222-2222-222222222222"
        cd["format_summary"] = "CD"
        cd["media"] = []

        music_catalog.save_release(db, vinyl_id, vinyl)
        music_catalog.save_release(db, cd_id, cd)

        link = db.execute(
            "SELECT * FROM item_links WHERE item_a_id = ? AND item_b_id = ?",
            tuple(sorted((vinyl_id, cd_id))),
        ).fetchone()
        assert link is not None
        assert link["link_type"] == "format"

    def test_provider_refresh_preserves_copy_specific_notes(self, db):
        item_id = insert_item(db, title="Album", media_type="vinyl")
        music_catalog.save_release(db, item_id, _SAMPLE_RELEASE)
        music_catalog.update_copy_details(
            db,
            item_id,
            edition_notes="Red translucent pressing",
            media_condition="VG+",
            packaging_condition="VG",
            condition_notes="Small sleeve crease",
        )

        refreshed = dict(_SAMPLE_RELEASE)
        refreshed["label"] = "Pink Floyd Records"
        music_catalog.save_release(db, item_id, refreshed)
        release = music_catalog.get_release(db, item_id)

        assert release["label"] == "Pink Floyd Records"
        assert release["edition_notes"] == "Red translucent pressing"
        assert release["media_condition"] == "VG+"
        assert release["condition_notes"] == "Small sleeve crease"


class TestMusicRoutes:
    def test_viewer_can_open_music_page(self, viewer_client):
        response = viewer_client.get("/music")
        assert response.status_code == 200
        assert "Search exact MusicBrainz releases" in response.text

    def test_search_renders_exact_release_choices(self, viewer_client, monkeypatch):
        async def fake_search(*args, **kwargs):
            result = dict(_SAMPLE_RELEASE)
            result.pop("media", None)
            return provider_result.found("musicbrainz", [result])

        monkeypatch.setattr(musicbrainz, "search_releases", fake_search)
        response = viewer_client.get("/music?q=dark+side&artist=Pink+Floyd")
        assert response.status_code == 200
        assert "The Dark Side of the Moon" in response.text
        assert "SHVL 804" in response.text
        assert "Vinyl" in response.text

    def test_editor_adds_exact_release_and_track_tree(self, editor_client, db, monkeypatch):
        async def fake_lookup(release_id, client):
            assert release_id == _SAMPLE_RELEASE["musicbrainz_release_id"]
            return provider_result.found("musicbrainz", dict(_SAMPLE_RELEASE))

        async def fake_art(release_id, client):
            return provider_result.found("coverartarchive", [])

        monkeypatch.setattr(musicbrainz, "lookup_release", fake_lookup)
        monkeypatch.setattr(musicbrainz, "cover_art", fake_art)

        response = editor_client.post(
            "/api/music/add",
            data={
                "release_id": _SAMPLE_RELEASE["musicbrainz_release_id"],
                "media_type": "vinyl",
                "owned": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        item = db.execute(
            "SELECT * FROM items WHERE title = ? AND media_type = 'vinyl'",
            (_SAMPLE_RELEASE["title"],),
        ).fetchone()
        assert item is not None
        assert item["authors"] == "Pink Floyd"
        release = music_catalog.get_release(db, item["id"])
        assert release["musicbrainz_release_id"] == _SAMPLE_RELEASE["musicbrainz_release_id"]
        assert release["media"][0]["tracks"][1]["number"] == "B1"

    def test_music_item_detail_does_not_render_reading_status(self, viewer_client, db):
        item_id = insert_item(db, title="Album", authors="Artist", media_type="vinyl")
        music_catalog.save_release(db, item_id, _SAMPLE_RELEASE)
        db.commit()
        response = viewer_client.get(f"/item/{item_id}?from=music")
        assert response.status_code == 200
        assert "Reading Status" not in response.text
        assert f'/api/music/items/{item_id}/detail' in response.text

    def test_music_detail_fragment_renders_tracks(self, viewer_client, db):
        item_id = insert_item(db, title="Album", authors="Pink Floyd", media_type="vinyl")
        music_catalog.save_release(db, item_id, _SAMPLE_RELEASE)
        db.commit()
        response = viewer_client.get(f"/api/music/items/{item_id}/detail")
        assert response.status_code == 200
        assert "SHVL 804" in response.text
        assert "Speak to Me" in response.text
        assert "B1" in response.text
