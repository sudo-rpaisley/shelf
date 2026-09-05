"""RomM sync mapping, paging, covers, platform selection and linking."""
import asyncio
import json

import httpx
import respx

from app.services import covers
from app.services import romm as romm_service
from app.services.romm import PAGE_SIZE, get_excluded_platforms, sync
from tests.conftest import _insert_item

ROMM = "http://romm.example"
TOKEN = "rmm_test_token"


def _set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.execute("COMMIT")


def _platform(pid=1, slug="snes", name="Super Nintendo", igdb_id=19, count=1):
    return {"id": pid, "slug": slug, "fs_slug": slug, "name": name,
            "display_name": name, "custom_name": None, "igdb_id": igdb_id,
            "rom_count": count}


def _rom(rid=101, title="Chrono Trigger", platform_id=1,
         platform_slug="snes",
         cover="/assets/romm/resources/snes/cover/chrono.jpg"):
    return {
        "id": rid,
        "platform_id": platform_id,
        "platform_slug": platform_slug,
        "platform_fs_slug": platform_slug,
        "platform_display_name": "Super Nintendo",
        "name": title,
        "fs_name_no_tags": title,
        "fs_name_no_ext": title,
        "summary": "A time-travelling RPG.",
        "metadatum": {
            "publishers": ["Square"], "developers": ["Square"],
            "franchises": [], "collections": [],
            "first_release_date": 795571200,
        },
        "path_cover_large": cover,
        "path_cover_small": None,
        "url_cover": None,
    }


def _page(*items, total=None, offset=0):
    return httpx.Response(200, json={
        "items": list(items), "total": len(items) if total is None else total,
        "limit": PAGE_SIZE, "offset": offset, "char_index": {},
        "rom_id_index": [], "filter_values": {},
    })


def _mock_library(platform=None, rom=None):
    platform = platform or _platform()
    rom = rom or _rom()
    respx.get(f"{ROMM}/api/platforms").mock(
        return_value=httpx.Response(200, json=[platform]))
    return respx.get(f"{ROMM}/api/roms").mock(return_value=_page(rom))


@respx.mock
def test_maps_romm_rom_to_digital_game(db):
    listing = _mock_library()
    cover = respx.get(f"{ROMM}/assets/romm/resources/snes/cover/chrono.jpg").mock(
        return_value=httpx.Response(404))
    stats = asyncio.run(sync(ROMM, TOKEN))
    assert stats["added"] == 1
    row = db.execute(
        """SELECT title, media_type, platform, publisher, publish_year,
                  description, romm_id, romm_platform_id, source FROM items"""
    ).fetchone()
    assert dict(row) == {
        "title": "Chrono Trigger", "media_type": "digital_game",
        "platform": "snes", "publisher": "Square", "publish_year": 1995,
        "description": "A time-travelling RPG.", "romm_id": "101",
        "romm_platform_id": "1", "source": "romm",
    }
    req = listing.calls[0].request
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert req.url.params["platform_ids"] == "1"
    assert "platform_id" not in req.url.params
    assert req.url.params["order_by"] == "id"
    assert req.url.params["with_total"] == "true"
    assert req.url.params["limit"] == str(PAGE_SIZE)
    assert req.url.params["group_by_meta_id"] == "true"
    assert req.url.params["with_char_index"] == "false"
    assert cover.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    icon_map = json.loads(
        db.execute(
            "SELECT value FROM settings WHERE key='romm_platform_icon_slugs'"
        ).fetchone()["value"]
    )
    assert icon_map == {"snes": "snes"}


@respx.mock
def test_imports_romm_cover_with_real_content_type(db):
    _mock_library()
    image = b"RIFF" + b"x" * 1200
    respx.get(f"{ROMM}/assets/romm/resources/snes/cover/chrono.jpg").mock(
        return_value=httpx.Response(200, content=image,
                                    headers={"Content-Type": "image/webp"}))
    stats = asyncio.run(sync(ROMM, TOKEN))
    row = db.execute("SELECT id, cover_path FROM items").fetchone()
    assert stats["covers"] == 1
    assert row["cover_path"] == f"covers/{row['id']}.webp"
    assert (covers.COVERS_DIR / f"{row['id']}.webp").read_bytes() == image


@respx.mock
def test_external_cover_never_receives_romm_token(db):
    rom = _rom(cover=None)
    rom["url_cover"] = "https://images.example/chrono.jpg"
    _mock_library(rom=rom)
    external = respx.get("https://images.example/chrono.jpg").mock(
        return_value=httpx.Response(404))
    asyncio.run(sync(ROMM, TOKEN))
    assert "Authorization" not in external.calls[0].request.headers


@respx.mock
def test_excluded_platform_is_not_fetched(db):
    _set_setting(db, "romm_excluded_platforms", json.dumps(["1"]))
    respx.get(f"{ROMM}/api/platforms").mock(
        return_value=httpx.Response(200, json=[_platform()]))
    route = respx.get(f"{ROMM}/api/roms").mock(return_value=_page(_rom()))
    stats = asyncio.run(sync(ROMM, TOKEN))
    assert stats["added"] == 0
    assert route.call_count == 0
    assert get_excluded_platforms() == {"1"}


@respx.mock
def test_second_identical_sync_is_unchanged(db):
    _mock_library()
    respx.get(f"{ROMM}/assets/romm/resources/snes/cover/chrono.jpg").mock(
        return_value=httpx.Response(404))
    first = asyncio.run(sync(ROMM, TOKEN))
    before = db.execute("SELECT updated_at FROM items").fetchone()["updated_at"]
    second = asyncio.run(sync(ROMM, TOKEN))
    after = db.execute("SELECT updated_at FROM items").fetchone()["updated_at"]
    assert first["added"] == 1
    assert second["unchanged"] == 1
    assert before == after


@respx.mock
def test_links_digital_game_to_physical_copy_on_title_and_platform(db):
    physical_id = _insert_item(db, title="Chrono Trigger", isbn=None,
                               media_type="video_game", platform="snes")
    db.execute("COMMIT")
    _mock_library()
    respx.get(f"{ROMM}/assets/romm/resources/snes/cover/chrono.jpg").mock(
        return_value=httpx.Response(404))
    asyncio.run(sync(ROMM, TOKEN))
    digital = db.execute("SELECT id FROM items WHERE media_type='digital_game'").fetchone()["id"]
    link = db.execute("SELECT item_a_id, item_b_id FROM item_links").fetchone()
    assert {link["item_a_id"], link["item_b_id"]} == {physical_id, digital}


@respx.mock
def test_same_title_different_platform_is_grouped(db):
    physical_id = _insert_item(db, title="Chrono Trigger", isbn=None,
                               media_type="video_game", platform="ps1")
    db.execute("COMMIT")
    _mock_library()
    respx.get(f"{ROMM}/assets/romm/resources/snes/cover/chrono.jpg").mock(
        return_value=httpx.Response(404))
    asyncio.run(sync(ROMM, TOKEN))
    digital_id = db.execute(
        "SELECT id FROM items WHERE media_type='digital_game'"
    ).fetchone()["id"]
    link = db.execute("SELECT item_a_id, item_b_id FROM item_links").fetchone()
    assert {link["item_a_id"], link["item_b_id"]} == {physical_id, digital_id}


@respx.mock
def test_unknown_romm_platform_is_created_in_shelf(db):
    platform = _platform(pid=44, slug="pc-engine-cd", name="PC Engine CD",
                         igdb_id=None)
    rom = _rom(rid=202, title="Gate of Thunder", platform_id=44,
               platform_slug="pc-engine-cd", cover=None)
    _mock_library(platform, rom)
    asyncio.run(sync(ROMM, TOKEN))
    row = db.execute("SELECT platform FROM items WHERE romm_id='202'").fetchone()
    assert row["platform"] == "pcenginecd"
    saved = db.execute("SELECT name FROM game_platforms WHERE slug='pcenginecd'").fetchone()
    assert saved["name"] == "PC Engine CD"


@respx.mock
def test_paginates_romm_platform(db):
    respx.get(f"{ROMM}/api/platforms").mock(
        return_value=httpx.Response(200, json=[_platform(count=PAGE_SIZE + 1)]))
    first = [_rom(rid=i + 1, title=f"Game {i + 1}", cover=None)
             for i in range(PAGE_SIZE)]
    second = [_rom(rid=PAGE_SIZE + 1, title="Last Game", cover=None)]
    route = respx.get(f"{ROMM}/api/roms").mock(side_effect=[
        _page(*first, total=PAGE_SIZE + 1, offset=0),
        _page(*second, total=PAGE_SIZE + 1, offset=PAGE_SIZE),
    ])
    stats = asyncio.run(sync(ROMM, TOKEN))
    assert stats["added"] == PAGE_SIZE + 1
    assert route.call_count == 2
    assert route.calls[1].request.url.params["offset"] == str(PAGE_SIZE)
    assert route.calls[0].request.url.params["with_total"] == "true"
    assert route.calls[1].request.url.params["with_total"] == "false"


@respx.mock
def test_transient_page_timeout_is_retried_without_dropping_platform(db, monkeypatch):
    monkeypatch.setattr(romm_service, "PAGE_RETRY_BACKOFF", 0)
    respx.get(f"{ROMM}/api/platforms").mock(
        return_value=httpx.Response(200, json=[_platform(count=PAGE_SIZE + 1)]))
    first = [_rom(rid=i + 1, title=f"Game {i + 1}", cover=None)
             for i in range(PAGE_SIZE)]
    last = _rom(rid=PAGE_SIZE + 1, title="Last Game", cover=None)
    route = respx.get(f"{ROMM}/api/roms").mock(side_effect=[
        _page(*first, total=PAGE_SIZE + 1, offset=0),
        httpx.ReadTimeout("temporary RomM timeout"),
        _page(last, total=PAGE_SIZE + 1, offset=PAGE_SIZE),
    ])

    stats = asyncio.run(sync(ROMM, TOKEN))

    assert stats["added"] == PAGE_SIZE + 1
    assert stats["incomplete_platforms"] == 0
    assert route.call_count == 3


@respx.mock
def test_exhausted_page_retries_preserve_partial_platform(db, monkeypatch):
    monkeypatch.setattr(romm_service, "PAGE_RETRY_BACKOFF", 0)
    respx.get(f"{ROMM}/api/platforms").mock(
        return_value=httpx.Response(200, json=[_platform(count=PAGE_SIZE + 50)]))
    first = [_rom(rid=i + 1, title=f"Game {i + 1}", cover=None)
             for i in range(PAGE_SIZE)]
    route = respx.get(f"{ROMM}/api/roms").mock(side_effect=[
        _page(*first, total=PAGE_SIZE + 50, offset=0),
        *[httpx.ReadTimeout("RomM stayed unavailable") for _ in range(romm_service.PAGE_RETRIES + 1)],
    ])

    stats = asyncio.run(sync(ROMM, TOKEN))

    assert stats["added"] == PAGE_SIZE
    assert stats["errors"] == 1
    assert stats["incomplete_platforms"] == 1
    assert route.call_count == romm_service.PAGE_RETRIES + 2


@respx.mock
def test_streaming_progress_reports_imports_without_reset(db):
    respx.get(f"{ROMM}/api/platforms").mock(
        return_value=httpx.Response(200, json=[_platform(count=PAGE_SIZE + 1)]))
    first = [_rom(rid=i + 1, title=f"Game {i + 1}", cover=None)
             for i in range(PAGE_SIZE)]
    last = _rom(rid=PAGE_SIZE + 1, title="Last Game", cover=None)
    respx.get(f"{ROMM}/api/roms").mock(side_effect=[
        _page(*first, total=PAGE_SIZE + 1, offset=0),
        _page(last, total=PAGE_SIZE + 1, offset=PAGE_SIZE),
    ])
    progress = []

    async def on_progress(current, total, title, status):
        progress.append((current, total, title, status))

    stats = asyncio.run(sync(ROMM, TOKEN, on_progress=on_progress))

    assert stats["added"] == PAGE_SIZE + 1
    assert any(
        current == 0 and title.startswith("Fetching ") and status == "discovering"
        for current, total, title, status in progress
    )
    added = [entry for entry in progress if entry[3] == "added"]
    assert added[0][0] == 1
    assert added[-1][0] == PAGE_SIZE + 1
    assert all(entry[1] >= entry[0] for entry in progress)


@respx.mock
def test_first_page_is_imported_before_second_page_is_requested(db):
    respx.get(f"{ROMM}/api/platforms").mock(
        return_value=httpx.Response(200, json=[_platform(count=PAGE_SIZE + 1)]))
    first = [_rom(rid=i + 1, title=f"Game {i + 1}", cover=None)
             for i in range(PAGE_SIZE)]
    last = _rom(rid=PAGE_SIZE + 1, title="Last Game", cover=None)

    def responder(request):
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return _page(*first, total=PAGE_SIZE + 1, offset=0)
        imported = db.execute(
            "SELECT COUNT(*) AS n FROM items WHERE source='romm'"
        ).fetchone()["n"]
        assert imported == PAGE_SIZE
        return _page(last, total=PAGE_SIZE + 1, offset=PAGE_SIZE)

    route = respx.get(f"{ROMM}/api/roms").mock(side_effect=responder)
    stats = asyncio.run(sync(ROMM, TOKEN))

    assert stats["added"] == PAGE_SIZE + 1
    assert route.call_count == 2
