from unittest.mock import AsyncMock, patch

from app.database import get_setting
from app.services import discogs, discogs_catalog, music_catalog, provider_result


def _music_item(db):
    item_id = db.execute(
        "INSERT INTO items (title, authors, media_type, owned, upc) "
        "VALUES (?, ?, 'vinyl', 1, ?)",
        ("Kind of Blue", "Miles Davis", "0012345678905"),
    ).lastrowid
    music_catalog.save_release(
        db,
        item_id,
        {
            "title": "Kind of Blue",
            "artist_credit": "Miles Davis",
            "musicbrainz_release_id": "11111111-1111-1111-1111-111111111111",
            "musicbrainz_release_group_id": "22222222-2222-2222-2222-222222222222",
            "release_date": "1959-08-17",
            "label": "Columbia",
            "catalog_number": "CL 1355",
            "format_summary": '12" Vinyl',
            "media": [],
            "source": "musicbrainz",
        },
    )
    return item_id


def _pressing(release_id=123):
    return {
        "discogs_release_id": release_id,
        "discogs_master_id": 456,
        "label": "Columbia",
        "catalog_number": "CL 1355",
        "format_summary": "Vinyl · LP",
        "genres": ["Jazz"],
        "styles": ["Modal"],
        "notes": "Six-eye label",
        "discogs_url": f"https://www.discogs.com/release/{release_id}",
        "identifiers": [
            {
                "identifier_type": "matrix_runout",
                "value": "XLP47324-1A",
                "description": "Side A",
            }
        ],
    }


def test_discogs_tables_are_bootstrapped_centrally(db):
    tables = {
        row["name"]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE 'music_discogs%'"
        ).fetchall()
    }
    assert tables == {"music_discogs", "music_discogs_identifiers"}


def test_discogs_normalises_exact_pressing_identifiers():
    release = discogs.normalise_release(
        {
            "id": 123,
            "master_id": 456,
            "title": "Kind of Blue",
            "artists": [{"name": "Miles Davis (2)"}],
            "labels": [{"name": "Columbia", "catno": "CL 1355"}],
            "formats": [{"name": "Vinyl", "descriptions": ["LP", "Album"]}],
            "genres": ["Jazz"],
            "styles": ["Modal"],
            "identifiers": [
                {
                    "type": "Matrix / Runout",
                    "value": "XLP47324-1A",
                    "description": "Side A",
                },
                {"type": "Pressing Plant ID", "value": "P"},
            ],
            "uri": "/release/123-Kind-Of-Blue",
        }
    )
    assert release["discogs_release_id"] == 123
    assert release["discogs_master_id"] == 456
    assert release["artist_credit"] == "Miles Davis"
    assert release["format_summary"] == "Vinyl · LP · Album"
    assert release["identifiers"][0]["identifier_type"] == "matrix_runout"
    assert release["discogs_url"].startswith("https://www.discogs.com/")


def test_discogs_enrichment_cannot_replace_musicbrainz_identity(db):
    item_id = _music_item(db)
    before = music_catalog.get_release(db, item_id)
    discogs_catalog.save_enrichment(db, item_id, _pressing())
    after = music_catalog.get_release(db, item_id)
    enrichment = discogs_catalog.get_enrichment(db, item_id)
    assert after["musicbrainz_release_id"] == before["musicbrainz_release_id"]
    assert after["musicbrainz_release_group_id"] == before[
        "musicbrainz_release_group_id"
    ]
    assert enrichment["discogs_release_id"] == 123
    assert enrichment["identifiers"][0]["value"] == "XLP47324-1A"


def test_removing_discogs_match_leaves_music_release(db):
    item_id = _music_item(db)
    discogs_catalog.save_enrichment(db, item_id, _pressing())
    assert discogs_catalog.clear_enrichment(db, item_id)
    assert discogs_catalog.get_enrichment(db, item_id) is None
    assert music_catalog.get_release(db, item_id)["musicbrainz_release_id"]


def test_saved_discogs_token_uses_standard_encrypted_settings(
    admin_client, db, monkeypatch
):
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    response = admin_client.post(
        "/api/settings",
        data={"discogs_token": "secret-discogs-token"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    raw = db.execute(
        "SELECT value FROM settings WHERE key = 'discogs_token'"
    ).fetchone()["value"]
    assert raw != "secret-discogs-token"
    assert raw.startswith("gAAAAA")
    assert get_setting(db, "discogs_token") == "secret-discogs-token"


def test_environment_discogs_token_wins(db, monkeypatch):
    monkeypatch.setenv("DISCOGS_TOKEN", "env-token")
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('discogs_token', 'database-token')"
    )
    assert get_setting(db, "discogs_token") == "env-token"


def test_settings_page_exposes_discogs_without_secret_value(
    admin_client, db, monkeypatch
):
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    admin_client.post(
        "/api/settings", data={"discogs_token": "never-render-this-token"}
    )
    response = admin_client.get("/settings")
    assert response.status_code == 200
    assert "Discogs" in response.text
    assert "never-render-this-token" not in response.text
    assert "DISCOGS_TOKEN" in response.text


def test_select_pressing_persists_exact_release(admin_client, db, monkeypatch):
    item_id = _music_item(db)
    db.commit()
    monkeypatch.setenv("DISCOGS_TOKEN", "token")
    chosen = provider_result.found("discogs", _pressing(321))
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


def test_discogs_card_is_readable_but_viewer_cannot_mutate(
    viewer_client, db, monkeypatch
):
    item_id = _music_item(db)
    discogs_catalog.save_enrichment(db, item_id, _pressing())
    db.commit()
    monkeypatch.setenv("DISCOGS_TOKEN", "token")
    card = viewer_client.get(f"/api/discogs/items/{item_id}/card")
    remove = viewer_client.post(
        f"/api/discogs/items/{item_id}/remove", follow_redirects=False
    )
    assert card.status_code == 200
    assert "Discogs pressing" in card.text
    assert remove.status_code == 403
    assert discogs_catalog.get_enrichment(db, item_id) is not None


def test_discogs_routes_are_registered_through_main():
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/api/discogs/test" in paths
    assert "/api/discogs/items/{item_id}/card" in paths
    assert "/music/item/{item_id}/discogs" in paths
