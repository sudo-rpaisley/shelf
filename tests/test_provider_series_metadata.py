"""Connected-service series metadata and ordering-safety regressions."""

import asyncio

import httpx
import respx

from app.services import libraries, provider_series, provider_series_sync
from tests.conftest import _insert_item


ABS = "http://abs-series.example"
KOMGA = "http://komga-series.example"


def test_provider_metadata_upsert_is_separate_from_shelf_series_meta(db):
    db.execute(
        "INSERT INTO series_meta (name, description, source) VALUES (?, ?, ?)",
        ("Example Saga", "Shelf's synopsis", "manual"),
    )
    provider_series.upsert(
        db,
        provider="komga",
        provider_series_id="series_1",
        series_name="Example Saga",
        description="Komga's synopsis",
        publisher="Example Press",
        total_items=12,
    )
    provider_series.upsert(
        db,
        provider="komga",
        provider_series_id="series_1",
        series_name="Example Saga",
        description="Updated provider synopsis",
        publisher="Example Press",
        total_items=13,
    )

    local = db.execute(
        "SELECT description, source FROM series_meta WHERE name = 'Example Saga'"
    ).fetchone()
    connected = provider_series.for_series(db, "Example Saga")
    assert dict(local) == {"description": "Shelf's synopsis", "source": "manual"}
    assert len(connected) == 1
    assert connected[0]["description"] == "Updated provider synopsis"
    assert connected[0]["total_items"] == 13


@respx.mock
def test_audiobookshelf_series_endpoint_supplies_stable_id_totals_and_duration(db):
    _insert_item(
        db,
        title="Audio One",
        isbn=None,
        media_type="audiobook",
        abs_id="li_1",
        abs_library_id="lib_audio",
        series_name="Audio Saga",
        series_position=1,
    )
    db.commit()
    route = respx.get(f"{ABS}/api/libraries/lib_audio/series").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "ser_audio",
                        "name": "Audio Saga",
                        "books": [{"id": "li_1"}, {"id": "li_2"}],
                        "totalDuration": 10800,
                    }
                ]
            },
        )
    )

    saved = asyncio.run(provider_series_sync.sync_audiobookshelf_series(ABS, "token"))

    assert saved == 1
    row = provider_series.for_series(db, "Audio Saga")[0]
    assert row["provider"] == "audiobookshelf"
    assert row["provider_series_id"] == "ser_audio"
    assert row["total_items"] == 2
    assert row["total_duration_mins"] == 180
    assert route.calls[0].request.headers["Authorization"] == "Bearer token"
    assert route.calls[0].request.url.params["limit"] == "10000"


@respx.mock
def test_komga_series_endpoint_supplies_rich_series_metadata(db):
    _insert_item(
        db,
        title="Comic One",
        isbn=None,
        media_type="digital_comic",
        komga_id="book_1",
        komga_series_id="series_1",
        series_name="Comic Saga",
        series_position=1,
    )
    db.commit()
    respx.get(f"{KOMGA}/api/v1/series/series_1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "series_1",
                "name": "Comic Saga",
                "libraryId": "lib_comics",
                "bookCount": 8,
                "metadata": {
                    "summary": "A provider supplied series synopsis.",
                    "publisher": "Panel Press",
                    "status": "ONGOING",
                    "ageRating": 16,
                    "totalBookCount": 12,
                    "readingDirection": "LEFT_TO_RIGHT",
                    "language": "en",
                },
            },
        )
    )

    saved = asyncio.run(provider_series_sync.sync_komga_series(KOMGA, "key"))

    assert saved == 1
    row = provider_series.for_series(db, "Comic Saga")[0]
    assert row["provider"] == "komga"
    assert row["description"] == "A provider supplied series synopsis."
    assert row["publisher"] == "Panel Press"
    assert row["status"] == "ONGOING"
    assert row["age_rating"] == "16"
    assert row["total_items"] == 12


def test_romm_collections_and_franchises_are_unordered_series_memberships(db):
    item_id = _insert_item(
        db,
        title="Example Game",
        isbn=None,
        media_type="digital_game",
        romm_id="101",
    )
    db.commit()
    rom = {
        "id": 101,
        "metadatum": {
            "collections": [{"id": 55, "name": "Example Trilogy"}],
            "franchises": [{"id": 77, "name": "Example Universe"}],
        },
    }

    saved = provider_series_sync.enrich_romm_batch(db, [rom])

    assert saved == 2
    memberships = db.execute(
        "SELECT series_name, position FROM item_series WHERE item_id = ? "
        "ORDER BY series_name",
        (item_id,),
    ).fetchall()
    assert [(row["series_name"], row["position"]) for row in memberships] == [
        ("Example Trilogy", None),
        ("Example Universe", None),
    ]
    trilogy = provider_series.for_series(db, "Example Trilogy")[0]
    universe = provider_series.for_series(db, "Example Universe")[0]
    assert trilogy["kind"] == "series"
    assert trilogy["provider_series_id"] == "collection:55"
    assert universe["kind"] == "franchise"
    assert universe["provider_series_id"] == "franchise:77"


def test_series_detail_uses_provider_synopsis_only_as_fallback(db, admin_client):
    _insert_item(
        db,
        title="Provider Volume One",
        isbn="9780000024001",
        media_type="digital_manga",
        source="komga",
        series_name="Provider Saga",
        series_position=1,
        komga_id="komga_provider_volume_1",
        komga_series_id="komga_provider_saga",
    )
    provider_series.upsert(
        db,
        provider="komga",
        provider_series_id="komga_provider_saga",
        series_name="Provider Saga",
        description="Komga fallback synopsis",
        publisher="Manga House",
        status="ONGOING",
        total_items=20,
    )
    db.execute(
        "INSERT INTO series_meta (name, description, source) VALUES (?, ?, ?)",
        ("Provider Saga", "Shelf manual synopsis", "manual"),
    )
    db.commit()

    manual = admin_client.get(
        "/series/detail",
        params={
            "name": "Provider Saga",
            "media_type": "digital_manga",
            "komga_series_id": "komga_provider_saga",
        },
    )
    assert manual.status_code == 200
    assert "Shelf manual synopsis" in manual.text
    assert "Komga fallback synopsis" not in manual.text
    assert "Connected series details" in manual.text
    assert "Manga House" in manual.text
    assert "ONGOING" in manual.text

    db.execute("DELETE FROM series_meta WHERE name = 'Provider Saga'")
    db.commit()
    fallback = admin_client.get(
        "/series/detail",
        params={
            "name": "Provider Saga",
            "media_type": "digital_manga",
            "komga_series_id": "komga_provider_saga",
        },
    )
    assert fallback.status_code == 200
    assert "Komga fallback synopsis" in fallback.text
    assert "Series synopsis from Komga" in fallback.text


def test_secondary_romm_franchise_opens_as_a_series_detail(db, viewer_client):
    _insert_item(
        db,
        title="Franchise Game",
        isbn=None,
        media_type="digital_game",
        romm_id="201",
    )
    provider_series_sync.enrich_romm_batch(
        db,
        [
            {
                "id": 201,
                "metadatum": {
                    "collections": [{"id": 91, "name": "Game Trilogy"}],
                    "franchises": [{"id": 92, "name": "Game Universe"}],
                },
            }
        ],
    )
    db.commit()

    response = viewer_client.get(
        "/series/detail", params={"name": "Game Universe"}
    )

    assert response.status_code == 200
    assert "Game Universe" in response.text
    assert "Franchise Game" in response.text
    assert "Connected series details" in response.text
    assert "Franchise" in response.text


def test_hidden_provider_series_metadata_does_not_leak_by_shared_name(
    db, viewer_client
):
    hidden_library = libraries.create_library(db, "Hidden provider library")
    _insert_item(
        db,
        title="Visible Shared Name",
        isbn="9780000024010",
        media_type="book",
        series_name="Shared Provider Name",
        series_position=1,
    )
    _insert_item(
        db,
        title="Hidden Audio Shared Name",
        isbn=None,
        media_type="audiobook",
        series_name="Shared Provider Name",
        series_position=2,
        abs_id="hidden_abs_item",
        abs_library_id="hidden_abs_library",
        _library_id=hidden_library["id"],
    )
    provider_series.upsert(
        db,
        provider="audiobookshelf",
        provider_series_id="hidden_abs_series",
        series_name="Shared Provider Name",
        description="SECRET PROVIDER SYNOPSIS",
        total_items=99,
        metadata={"library_id": "hidden_abs_library"},
    )
    db.commit()

    response = viewer_client.get(
        "/series/detail", params={"name": "Shared Provider Name"}
    )

    assert response.status_code == 200
    assert "Visible Shared Name" in response.text
    assert "Hidden Audio Shared Name" not in response.text
    assert "SECRET PROVIDER SYNOPSIS" not in response.text
    assert "Provider total:</span> 99" not in response.text
