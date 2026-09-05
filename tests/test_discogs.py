"""Discogs collector-enrichment regression tests."""

from app.database import _run_migrations, get_setting
from app.services import discogs, music_catalog, provider_result
from app.services.item_write import insert_item


MBID = "33333333-3333-3333-3333-333333333333"
GROUP_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _music_release():
    return {
        "title": "Test Album",
        "artist_credit": "Test Artist",
        "musicbrainz_release_id": MBID,
        "musicbrainz_release_group_id": GROUP_ID,
        "release_type": "Album",
        "release_status": "Official",
        "release_date": "1981-01-01",
        "first_release_date": "1981-01-01",
        "country": "GB",
        "label": "Canonical Label",
        "catalog_number": "CAT 1",
        "packaging": "Cardboard/Paper Sleeve",
        "media_count": 0,
        "format_summary": "12\" Vinyl",
        "source": "musicbrainz",
        "media": [],
    }


def _discogs_release(release_id=12345):
    return {
        "title": "Test Album",
        "artist_credit": "Test Artist",
        "discogs_release_id": release_id,
        "discogs_master_id": 54321,
        "release_date": "1981",
        "country": "UK",
        "label": "Collector Label",
        "catalog_number": "CAT-1-A",
        "format_summary": "Vinyl · LP · Album · Reissue",
        "genres": ["Rock"],
        "styles": ["New Wave"],
        "notes": "Pressed at Example Plant.",
        "identifiers": [
            {"identifier_type": "matrix_runout", "value": "CAT A-1", "description": "Side A"},
            {"identifier_type": "pressing_plant_id", "value": "PLANT-X", "description": None},
        ],
        "tracks": [],
        "discogs_url": f"https://www.discogs.com/release/{release_id}",
        "source": "discogs",
    }


def _seed(db):
    item_id = insert_item(db, title="Test Album", authors="Test Artist", media_type="vinyl")
    music_catalog.save_release(db, item_id, _music_release())
    return item_id


class TestDiscogsMigrationRecovery:
    def test_baked_discogs_column_self_heals_missing_version_row(self, db):
        # Reproduce a restored/constructed DB where the complete table schema
        # is present but migration 31's bookkeeping row is missing.
        db.execute("DELETE FROM schema_version WHERE version = 31")
        db.commit()

        _run_migrations(db)

        assert db.execute(
            "SELECT 1 FROM schema_version WHERE version = 31"
        ).fetchone() is not None


class TestDiscogsNormalisation:
    def test_exact_release_extracts_pressing_identifiers_and_track_numbers(self):
        raw = {
            "id": 12345,
            "master_id": 54321,
            "title": "Test Album",
            "artists": [{"name": "Test Artist (2)"}],
            "released": "1981-06-00",
            "country": "UK",
            "labels": [{"name": "Collector Label", "catno": "CAT-1-A"}],
            "formats": [{"name": "Vinyl", "qty": "1", "descriptions": ["LP", "Album", "Reissue"]}],
            "genres": ["Rock"],
            "styles": ["New Wave"],
            "identifiers": [
                {"type": "Matrix / Runout", "value": "CAT A-1", "description": "Side A"},
                {"type": "Pressing Plant ID", "value": "PLANT-X"},
            ],
            "tracklist": [{"position": "A1", "title": "Opening", "duration": "3:21", "type_": "track"}],
            "uri": "/release/12345-Test-Artist-Test-Album",
        }
        result = discogs.normalise_release(raw)
        assert result["discogs_release_id"] == 12345
        assert result["discogs_master_id"] == 54321
        assert result["artist_credit"] == "Test Artist"
        assert "LP" in result["format_summary"]
        assert result["identifiers"][0]["identifier_type"] == "matrix_runout"
        assert result["tracks"][0]["number"] == "A1"
        assert result["tracks"][0]["duration_ms"] == 201000


class TestDiscogsPersistence:
    def test_enrichment_keeps_musicbrainz_identity_and_manual_identifiers(self, db):
        item_id = _seed(db)
        music_catalog.add_identifier(db, item_id, "matrix_runout", "MANUAL-1", "My note")
        assert music_catalog.save_discogs_enrichment(db, item_id, _discogs_release())

        row = db.execute("SELECT * FROM music_releases WHERE item_id = ?", (item_id,)).fetchone()
        assert row["musicbrainz_release_id"] == MBID
        assert row["discogs_release_id"] == 12345
        assert row["discogs_master_id"] == 54321
        assert row["discogs_catalog_number"] == "CAT-1-A"

        hydrated = music_catalog.get_release(db, item_id)
        assert hydrated["discogs_fresh"] is True
        assert hydrated["discogs_genres"] == ["Rock"]
        assert [x["value"] for x in hydrated["identifiers"]] == ["MANUAL-1"]
        assert {x["value"] for x in hydrated["discogs_identifiers"]} == {"CAT A-1", "PLANT-X"}

    def test_refresh_replaces_only_discogs_owned_identifiers(self, db):
        item_id = _seed(db)
        music_catalog.add_identifier(db, item_id, "matrix_runout", "MANUAL-1")
        music_catalog.save_discogs_enrichment(db, item_id, _discogs_release())
        changed = _discogs_release()
        changed["identifiers"] = [{"identifier_type": "matrix_runout", "value": "CAT A-2", "description": None}]
        music_catalog.save_discogs_enrichment(db, item_id, changed)

        rows = db.execute(
            "SELECT value, source FROM music_identifiers WHERE item_id = ? ORDER BY value", (item_id,)
        ).fetchall()
        assert [(r["value"], r["source"]) for r in rows] == [
            ("CAT A-2", "discogs"), ("MANUAL-1", "manual")
        ]

    def test_stale_discogs_cache_is_not_exposed_for_display(self, db):
        item_id = _seed(db)
        music_catalog.save_discogs_enrichment(db, item_id, _discogs_release())
        db.execute(
            "UPDATE music_releases SET discogs_updated_at = '2000-01-01T00:00:00+00:00' WHERE item_id = ?",
            (item_id,),
        )
        release = music_catalog.get_release(db, item_id)
        assert release["discogs_fresh"] is False
        assert release["discogs_identifiers"] == []

    def test_clear_discogs_match_preserves_manual_data(self, db):
        item_id = _seed(db)
        music_catalog.add_identifier(db, item_id, "matrix_runout", "MANUAL-1")
        music_catalog.save_discogs_enrichment(db, item_id, _discogs_release())
        assert music_catalog.clear_discogs_enrichment(db, item_id)
        row = db.execute("SELECT discogs_release_id FROM music_releases WHERE item_id = ?", (item_id,)).fetchone()
        assert row["discogs_release_id"] is None
        ids = db.execute("SELECT value, source FROM music_identifiers WHERE item_id = ?", (item_id,)).fetchall()
        assert [(r["value"], r["source"]) for r in ids] == [("MANUAL-1", "manual")]


class TestDiscogsSettings:
    def test_discogs_token_is_env_overridable(self, db, monkeypatch):
        monkeypatch.setenv("DISCOGS_TOKEN", "env-discogs-token")
        assert get_setting(db, "discogs_token") == "env-discogs-token"

    def test_settings_renders_write_only_discogs_token(self, admin_client):
        html = admin_client.get("/settings").text
        assert 'name="discogs_token"' in html
        assert 'data-testid="discogs-settings-card"' in html

    def test_settings_saves_discogs_token_encrypted(self, admin_client, db):
        response = admin_client.post("/api/settings", data={"discogs_token": "secret-token"}, follow_redirects=False)
        assert response.status_code == 303
        raw = db.execute("SELECT value FROM settings WHERE key = 'discogs_token'").fetchone()["value"]
        assert raw != "secret-token"
        assert get_setting(db, "discogs_token") == "secret-token"

    def test_discogs_test_button_uses_typed_token(self, admin_client, monkeypatch):
        async def fake_test(token, client):
            assert token == "typed-token"
            return provider_result.found("discogs", {"username": "tester"})
        monkeypatch.setattr(discogs, "test_connection", fake_test)
        response = admin_client.post(
            "/api/settings/discogs/test", data={"discogs_token": "typed-token"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"].endswith("discogs_test=ok")


class TestDiscogsRoutes:
    def test_match_page_prefers_barcode_and_renders_exact_releases(self, editor_client, admin_client, db, monkeypatch):
        admin_client.post("/api/settings", data={"discogs_token": "token"}, follow_redirects=False)
        item_id = _seed(db)
        db.execute("UPDATE items SET upc = '0123456789012' WHERE id = ?", (item_id,))
        db.commit()

        async def fake_search(query, client, **kwargs):
            assert query == ""
            assert kwargs["barcode"] == "0123456789012"
            return provider_result.found("discogs", [_discogs_release() | {"barcodes": ["0123456789012"]}])
        monkeypatch.setattr(discogs, "search_releases", fake_search)

        response = editor_client.get(f"/music/item/{item_id}/discogs")
        assert response.status_code == 200
        assert "CAT-1-A" in response.text
        assert "Use this pressing" in response.text
        assert "Data provided by Discogs." in response.text

    def test_attach_discogs_release_persists_exact_pressing(self, editor_client, admin_client, db, monkeypatch):
        admin_client.post("/api/settings", data={"discogs_token": "token"}, follow_redirects=False)
        item_id = _seed(db)
        db.commit()

        async def fake_lookup(release_id, client, *, token):
            assert release_id == 12345
            assert token == "token"
            return provider_result.found("discogs", _discogs_release())
        monkeypatch.setattr(discogs, "lookup_release", fake_lookup)

        response = editor_client.post(
            f"/api/music/items/{item_id}/discogs",
            data={"release_id": "12345"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        row = db.execute("SELECT discogs_release_id FROM music_releases WHERE item_id = ?", (item_id,)).fetchone()
        assert row["discogs_release_id"] == 12345

        detail = editor_client.get(f"/api/music/items/{item_id}/detail")
        assert "Data provided by Discogs." in detail.text
        assert "CAT A-1" in detail.text
