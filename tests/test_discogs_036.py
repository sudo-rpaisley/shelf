from unittest.mock import AsyncMock, patch

from app.routers import discogs as discogs_router
from app.services import discogs, discogs_catalog, music_catalog, provider_result


def _music_item(db):
    item_id = db.execute(
        "INSERT INTO items (title, authors, media_type, owned, upc) VALUES (?, ?, 'vinyl', 1, ?)",
        ("Kind of Blue", "Miles Davis", "0012345678905"),
    ).lastrowid
    music_catalog.save_release(db, item_id, {
        "title": "Kind of Blue",
        "artist_credit": "Miles Davis",
        "musicbrainz_release_id": "11111111-1111-1111-1111-111111111111",
        "musicbrainz_release_group_id": "22222222-2222-2222-2222-222222222222",
        "release_date": "1959-08-17",
        "label": "Columbia",
        "catalog_number": "CL 1355",
        "format_summary": "12\" Vinyl",
        "media": [],
        "source": "musicbrainz",
    })
    return item_id


def test_discogs_normalises_pressing_identifiers_without_changing_artist_suffix():
    release = discogs.normalise_release({
        "id": 123,
        "master_id": 456,
        "title": "Kind of Blue",
        "artists": [{"name": "Miles Davis (2)"}],
        "labels": [{"name": "Columbia", "catno": "CL 1355"}],
        "formats": [{"name": "Vinyl", "descriptions": ["LP", "Album"]}],
        "genres": ["Jazz"],
        "styles": ["Modal"],
        "identifiers": [
            {"type": "Matrix / Runout", "value": "XLP47324-1A", "description": "Side A"},
            {"type": "Pressing Plant ID", "value": "P"},
        ],
        "uri": "/release/123-Kind-Of-Blue",
    })
    assert release["discogs_release_id"] == 123
    assert release["discogs_master_id"] == 456
    assert release["artist_credit"] == "Miles Davis"
    assert release["format_summary"] == "Vinyl · LP · Album"
    assert release["identifiers"][0]["identifier_type"] == "matrix_runout"
    assert release["discogs_url"].startswith("https://www.discogs.com/")


def test_discogs_enrichment_is_separate_from_musicbrainz_identity(db):
    item_id = _music_item(db)
    before = music_catalog.get_release(db, item_id)

    discogs_catalog.save_enrichment(db, item_id, {
        "discogs_release_id": 123,
        "discogs_master_id": 456,
        "label": "Columbia",
        "catalog_number": "CL 1355",
        "format_summary": "Vinyl · LP",
        "genres": ["Jazz"],
        "styles": ["Modal"],
        "notes": "Six-eye label",
        "discogs_url": "https://www.discogs.com/release/123",
        "identifiers": [{
            "identifier_type": "matrix_runout",
            "value": "XLP47324-1A",
            "description": "Side A",
        }],
    })

    after = music_catalog.get_release(db, item_id)
    enrichment = discogs_catalog.get_enrichment(db, item_id)
    assert after["musicbrainz_release_id"] == before["musicbrainz_release_id"]
    assert after["musicbrainz_release_group_id"] == before["musicbrainz_release_group_id"]
    assert enrichment["discogs_release_id"] == 123
    assert enrichment["identifiers"][0]["value"] == "XLP47324-1A"


def test_removing_discogs_match_leaves_music_release(db):
    item_id = _music_item(db)
    discogs_catalog.save_enrichment(db, item_id, {
        "discogs_release_id": 123,
        "genres": [], "styles": [], "identifiers": [],
    })
    assert discogs_catalog.clear_enrichment(db, item_id)
    assert discogs_catalog.get_enrichment(db, item_id) is None
    assert music_catalog.get_release(db, item_id)["musicbrainz_release_id"]


def test_saved_discogs_token_is_encrypted(admin_client, db, monkeypatch):
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    response = admin_client.post(
        "/settings/discogs", data={"token": "secret-discogs-token"}, follow_redirects=False
    )
    assert response.status_code == 303
    raw = db.execute("SELECT value FROM settings WHERE key = 'discogs_token'").fetchone()["value"]
    assert raw != "secret-discogs-token"
    assert raw.startswith("gAAAAA")
    assert discogs_router._stored_token(db) == "secret-discogs-token"


def test_environment_discogs_token_wins(db, monkeypatch):
    monkeypatch.setenv("DISCOGS_TOKEN", "env-token")
    discogs_router._save_token(db, "database-token")
    assert discogs_router._stored_token(db) == "env-token"
    assert discogs_router._token_source(db) == "environment"


def test_select_pressing_persists_exact_release(admin_client, db, monkeypatch):
    item_id = _music_item(db)
    monkeypatch.setenv("DISCOGS_TOKEN", "token")
    chosen = provider_result.found("discogs", {
        "discogs_release_id": 321,
        "discogs_master_id": 654,
        "label": "Columbia",
        "catalog_number": "CL 1355",
        "format_summary": "Vinyl · LP",
        "genres": ["Jazz"],
        "styles": ["Modal"],
        "notes": None,
        "identifiers": [],
        "discogs_url": "https://www.discogs.com/release/321",
    })
    with patch(
        "app.routers.discogs.discogs.lookup_release",
        new=AsyncMock(return_value=chosen),
    ):
        response = admin_client.post(
            f"/api/discogs/items/{item_id}/select",
            data={"release_id": "321"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert discogs_catalog.get_enrichment(db, item_id)["discogs_release_id"] == 321


def test_discogs_card_does_not_expose_saved_token(admin_client, db, monkeypatch):
    item_id = _music_item(db)
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    discogs_router._save_token(db, "never-render-this-token")
    db.commit()
    response = admin_client.get(f"/api/discogs/items/{item_id}/card")
    assert response.status_code == 200
    assert "never-render-this-token" not in response.text
    assert "Discogs pressing" in response.text
