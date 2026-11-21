"""A title-only UPC music scan can be matched to an exact release in place."""

from app.services import music_catalog, musicbrainz, provider_result
from app.services.item_write import insert_item
from tests.conftest import _insert_location


RELEASE_ID = "33333333-3333-3333-3333-333333333333"
GROUP_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
BARCODE = "012345678905"


def _copy_state(db, item_id):
    # The normal legacy projection may touch updated_at even for an unchanged
    # location. Compare every identity/placement/collector field, not its clock.
    return [{key: row[key] for key in row.keys() if key != "updated_at"}
            for row in db.execute("SELECT * FROM item_copies WHERE item_id = ? ORDER BY id", (item_id,))]


def _release():
    return {
        "title": "Rumours",
        "artist_credit": "Fleetwood Mac",
        "musicbrainz_release_id": RELEASE_ID,
        "musicbrainz_release_group_id": GROUP_ID,
        "release_type": "Album",
        "release_status": "Official",
        "release_date": "1977-02-04",
        "first_release_date": "1977-02-04",
        "country": "US",
        "label": "Warner Bros. Records",
        "catalog_number": "BSK 3010",
        "barcode": BARCODE,
        "packaging": "Cardboard/Paper Sleeve",
        "media_count": 1,
        "format_summary": "12\" Vinyl",
        "source": "musicbrainz",
        "media": [{
            "position": 1,
            "format": "12\" Vinyl",
            "title": None,
            "track_count": 1,
            "tracks": [{
                "position": 1,
                "number": "A1",
                "title": "Second Hand News",
                "artist_credit": "Fleetwood Mac",
                "duration_ms": 176000,
                "musicbrainz_recording_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            }],
        }],
    }


def test_existing_scanned_item_prefills_release_search_from_barcode(
    editor_client, db, monkeypatch
):
    item_id = insert_item(
        db,
        title="Fleetwood Mac Rumours Vinyl LP",
        media_type="vinyl",
        upc="0012345678905",  # Shelf's canonical EAN-13 storage shape
        source="upc",
    )
    copies_before = _copy_state(db, item_id)
    assert len(copies_before) == 1
    db.commit()

    async def fake_search(query, client, **kwargs):
        assert query == ""
        assert kwargs["barcode"] == "0012345678905"
        summary = dict(_release())
        summary.pop("media")
        return provider_result.found("musicbrainz", [summary])

    monkeypatch.setattr(musicbrainz, "search_releases", fake_search)
    response = editor_client.get(f"/music/add?item_id={item_id}")

    assert response.status_code == 200
    assert "Match scanned item" in response.text
    assert f'name="target_item_id" value="{item_id}"' in response.text
    assert "Use this exact release" in response.text
    assert _copy_state(db, item_id) == copies_before


def test_exact_release_enriches_scan_without_replacing_owned_copy(
    editor_client, db, monkeypatch
):
    location_id = _insert_location(db, "Record Shelf")
    item_id = insert_item(
        db,
        title="Fleetwood Mac - Rumours - Vinyl LP",
        media_type="vinyl",
        upc="0012345678905",
        location_id=location_id,
        owned=1,
        source="upc",
    )
    copies_before = _copy_state(db, item_id)
    assert len(copies_before) == 1
    db.commit()

    async def fake_lookup(release_id, client):
        assert release_id == RELEASE_ID
        return provider_result.found("musicbrainz", _release())

    async def fake_art(release_id, client):
        return provider_result.found("coverartarchive", [])

    monkeypatch.setattr(musicbrainz, "lookup_release", fake_lookup)
    monkeypatch.setattr(musicbrainz, "cover_art", fake_art)

    response = editor_client.post(
        "/api/music/add",
        data={
            "release_id": RELEASE_ID,
            "target_item_id": str(item_id),
            "media_type": "vinyl",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/item/{item_id}?from=music"

    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    assert item["title"] == "Rumours"
    assert item["authors"] == "Fleetwood Mac"
    assert item["publisher"] == "Warner Bros. Records"
    assert item["location_id"] == location_id
    assert item["owned"] == 1
    # The exact code physically scanned stays authoritative on the Shelf item.
    assert item["upc"] == "0012345678905"
    assert _copy_state(db, item_id) == copies_before

    release = music_catalog.get_release(db, item_id)
    assert release["musicbrainz_release_id"] == RELEASE_ID
    assert release["catalog_number"] == "BSK 3010"
    assert release["media"][0]["tracks"][0]["number"] == "A1"
