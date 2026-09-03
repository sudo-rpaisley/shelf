from pathlib import Path


def replace(path: str, old: str, new: str, expected: int = 1) -> None:
    p = Path(path)
    text = p.read_text()
    actual = text.count(old)
    if actual != expected:
        raise RuntimeError(f"{path}: expected {expected} occurrence(s), found {actual}: {old[:120]!r}")
    p.write_text(text.replace(old, new, expected))


# The main patch deliberately reaches this point before stopping because the
# generic and game scan paths both contain the same cover comment. Insert only
# into the first (generic UPC) path; music_match does not exist in the game path.
p = Path("app/routers/items_common.py")
text = p.read_text()
marker = "    # Download cover\n    cover_path = None\n"
if text.count(marker) != 2:
    raise RuntimeError(f"expected two cover markers, found {text.count(marker)}")
insert = '''    # Persist an exact MusicBrainz release only when the barcode resolver\n    # found one unambiguous format-compatible candidate.\n    if music_match and music_match.get("release"):\n        with get_db() as db:\n            music_catalog.save_release(db, item_id, music_match["release"])\n\n    # Download cover\n    cover_path = None\n'''
p.write_text(text.replace(marker, insert, 1))

replace(
    "app/services/scan_outcome.py",
    '''    "upcitemdb": "UPC Item DB",\n''',
    '''    "upcitemdb": "UPC Item DB",\n    "musicbrainz": "MusicBrainz",\n''',
)

replace(
    "tests/test_music.py",
    '''    def test_viewer_can_open_music_page(self, viewer_client):\n        response = viewer_client.get("/music")\n        assert response.status_code == 200\n        assert "Search exact MusicBrainz releases" in response.text\n\n    def test_search_renders_exact_release_choices(self, viewer_client, monkeypatch):\n''',
    '''    def test_music_destination_opens_the_library_first(self, viewer_client):\n        response = viewer_client.get("/music", follow_redirects=False)\n        assert response.status_code == 303\n        assert response.headers["location"] == "/browse?media_family_filter=music"\n\n    def test_exact_release_search_is_a_separate_add_page(self, viewer_client):\n        response = viewer_client.get("/music/add")\n        assert response.status_code == 200\n        assert "Add music" in response.text\n        assert "Search exact MusicBrainz releases" in response.text\n\n    def test_search_renders_exact_release_choices(self, viewer_client, monkeypatch):\n''',
)
replace(
    "tests/test_music.py",
    'response = viewer_client.get("/music?q=dark+side&artist=Pink+Floyd")',
    'response = viewer_client.get("/music/add?q=dark+side&artist=Pink+Floyd")',
)
replace(
    "tests/test_music_enrich.py",
    'response = editor_client.get(f"/music?item_id={item_id}")',
    'response = editor_client.get(f"/music/add?item_id={item_id}")',
)

Path("tests/test_music_scan_artwork.py").write_text(r'''"""Music UPC scans use MusicBrainz for format, metadata and sleeve artwork."""

from app.services import covers, musicbrainz, provider_result, upcitemdb
from app.services.item_write import insert_item

BARCODE = "012345678905"
RELEASE_ID = "44444444-4444-4444-4444-444444444444"
GROUP_ID = "55555555-5555-5555-5555-555555555555"


def _summary():
    return {
        "title": "Anamnesis",
        "artist_credit": "Example Artist",
        "musicbrainz_release_id": RELEASE_ID,
        "musicbrainz_release_group_id": GROUP_ID,
        "release_type": "Album",
        "release_status": "Official",
        "release_date": "2020-09-01",
        "country": "GB",
        "label": "Example Records",
        "catalog_number": "EX-001",
        "barcode": BARCODE,
        "packaging": "Cardboard/Paper Sleeve",
        "media_count": 1,
        "format_summary": '12" Vinyl',
    }


def _release():
    release = dict(_summary())
    release.update({
        "first_release_date": "2020-09-01",
        "source": "musicbrainz",
        "media": [{
            "position": 1,
            "format": '12" Vinyl',
            "title": None,
            "track_count": 1,
            "tracks": [{
                "position": 1,
                "number": "A1",
                "title": "Track One",
                "artist_credit": "Example Artist",
                "duration_ms": 180000,
                "musicbrainz_recording_id": "66666666-6666-6666-6666-666666666666",
            }],
        }],
    })
    return release


def _install_musicbrainz(monkeypatch):
    async def search_barcode(barcode, client, limit=15):
        assert barcode in {BARCODE, "0" + BARCODE}
        return provider_result.found("musicbrainz", [_summary()])

    async def lookup_release(release_id, client):
        assert release_id == RELEASE_ID
        return provider_result.found("musicbrainz", _release())

    async def cover_art(release_id, client):
        assert release_id == RELEASE_ID
        return provider_result.found("coverartarchive", [{
            "url": "https://coverartarchive.org/release/example/front.jpg",
            "front": True,
        }])

    monkeypatch.setattr(musicbrainz, "search_by_barcode", search_barcode)
    monkeypatch.setattr(musicbrainz, "lookup_release", lookup_release)
    monkeypatch.setattr(musicbrainz, "cover_art", cover_art)


def test_weak_upc_dvd_guess_is_rescued_by_musicbrainz(
    editor_client, db, monkeypatch
):
    async def product_lookup(upc, client):
        return provider_result.found("upcitemdb", {
            "title": "Anamnesis",
            "category": None,
            "brand": None,
            "images": [],
        })

    async def download(item_id, url, client):
        return f"covers/{item_id}.jpg"

    monkeypatch.setattr(upcitemdb, "lookup", product_lookup)
    monkeypatch.setattr(covers, "_download_to_item", download)
    _install_musicbrainz(monkeypatch)

    response = editor_client.post(
        "/api/scan", data={"isbn": BARCODE, "media_type": "auto"}
    )
    assert response.status_code == 200

    item = db.execute("SELECT * FROM items WHERE upc IS NOT NULL").fetchone()
    assert item["media_type"] == "vinyl"
    assert item["source"] == "musicbrainz"
    assert item["title"] == "Anamnesis"
    assert item["authors"] == "Example Artist"
    assert item["publisher"] == "Example Records"
    assert item["cover_path"] == f"covers/{item['id']}.jpg"
    release = db.execute(
        "SELECT musicbrainz_release_id FROM music_releases WHERE item_id = ?",
        (item["id"],),
    ).fetchone()
    assert release["musicbrainz_release_id"] == RELEASE_ID


def test_existing_coverless_vinyl_can_repair_artwork(
    editor_client, db, monkeypatch
):
    item_id = insert_item(
        db,
        title="Anamnesis",
        media_type="vinyl",
        upc="0" + BARCODE,
        source="upc",
    )
    db.commit()

    async def download(download_item_id, url, client):
        assert download_item_id == item_id
        return f"covers/{item_id}.jpg"

    monkeypatch.setattr(covers, "_download_to_item", download)
    _install_musicbrainz(monkeypatch)

    response = editor_client.post(
        "/api/music/repair-artwork", follow_redirects=False
    )
    assert response.status_code == 303
    assert "media_family_filter=music" in response.headers["location"]
    assert "music_artwork_repaired=1" in response.headers["location"]

    item = db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()
    assert item["cover_path"] == f"covers/{item_id}.jpg"


def test_music_browse_exposes_add_and_repair_actions(editor_client):
    response = editor_client.get("/browse?media_family_filter=music")
    assert response.status_code == 200
    assert ">Music <span id=\"collection-count\"" in response.text
    assert 'href="/music/add"' in response.text
    assert 'action="/api/music/repair-artwork"' in response.text
''')
