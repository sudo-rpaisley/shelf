"""Browse-first Music and barcode sleeve repair regressions."""

from app.services import covers, music_artwork, musicbrainz, provider_result
from app.services.item_write import insert_item

BARCODE = "012345678905"
RELEASE_ID = "77777777-7777-7777-7777-777777777777"


def _vinyl_summary():
    return {
        "title": "Anamnesis",
        "artist_credit": "Example Artist",
        "musicbrainz_release_id": RELEASE_ID,
        "format_summary": '12" Vinyl',
    }


def test_music_browse_has_add_and_repair_actions(editor_client):
    response = editor_client.get("/browse?media_family_filter=music")
    assert response.status_code == 200
    assert "Music" in response.text
    assert 'data-testid="add-music"' in response.text
    assert 'data-testid="find-music-artwork"' in response.text


def test_cover_lookup_keeps_vinyl_on_vinyl(monkeypatch):
    import asyncio
    import httpx

    async def search(barcode, client, limit=20):
        return provider_result.found("musicbrainz", [
            {**_vinyl_summary(), "musicbrainz_release_id": "cd-release", "format_summary": "CD"},
            _vinyl_summary(),
        ])

    async def art(release_id, client):
        assert release_id == RELEASE_ID
        return provider_result.found("coverartarchive", [{
            "url": "https://coverartarchive.org/release/example/front.jpg",
            "front": True,
        }])

    monkeypatch.setattr(musicbrainz, "search_by_barcode", search)
    monkeypatch.setattr(musicbrainz, "cover_art", art)
    async def run():
        async with httpx.AsyncClient() as client:
            return await music_artwork.find_cover_url(BARCODE, "vinyl", client)
    assert asyncio.run(run()).endswith("front.jpg")


def test_repair_fills_existing_coverless_music(editor_client, db, monkeypatch):
    item_id = insert_item(
        db, title="Anamnesis", media_type="vinyl", upc=BARCODE, source="upc"
    )
    db.commit()

    async def find_cover(barcode, media_type, client):
        assert barcode
        assert media_type == "vinyl"
        return "https://coverartarchive.org/release/example/front.jpg"

    async def download(download_item_id, url, client):
        assert download_item_id == item_id
        return f"covers/{item_id}.jpg"

    monkeypatch.setattr(music_artwork, "find_cover_url", find_cover)
    monkeypatch.setattr(covers, "_download_to_item", download)

    response = editor_client.post("/api/music/repair-artwork", follow_redirects=False)
    assert response.status_code == 303
    assert "media_family_filter=music" in response.headers["location"]
    assert "music_artwork_repaired=1" in response.headers["location"]
    row = db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["cover_path"] == f"covers/{item_id}.jpg"


def test_repair_never_overwrites_an_existing_cover(editor_client, db, monkeypatch):
    item_id = insert_item(
        db, title="Keep Me", media_type="vinyl", upc=BARCODE, source="upc",
        cover_path="covers/manual.jpg",
    )
    db.commit()

    async def should_not_run(*args, **kwargs):
        raise AssertionError("covered items must not be looked up")

    monkeypatch.setattr(music_artwork, "find_cover_url", should_not_run)
    response = editor_client.post("/api/music/repair-artwork", follow_redirects=False)
    assert response.status_code == 303
    assert "music_artwork_checked=0" in response.headers["location"]
    row = db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()
    assert row["cover_path"] == "covers/manual.jpg"
