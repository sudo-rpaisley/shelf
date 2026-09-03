from pathlib import Path


def replace(path: str, old: str, new: str, count: int = 1) -> None:
    p = Path(path)
    text = p.read_text()
    actual = text.count(old)
    if actual != count:
        raise RuntimeError(f"{path}: expected {count} occurrence(s), found {actual}: {old[:100]!r}")
    p.write_text(text.replace(old, new, count))


Path("app/services/music_artwork.py").write_text(r'''"""Barcode-first sleeve artwork lookup for Shelf music items."""

from __future__ import annotations

import httpx

from app.config import MUSIC_MEDIA_TYPES
from app.services import musicbrainz
from app.services import upc as upc_svc

_PHYSICAL_TYPES = {"vinyl", "cd", "cassette"}


def infer_media_type(release: dict) -> str:
    """Map a compact MusicBrainz release summary to a Shelf music type."""
    text = str(release.get("format_summary") or "").casefold()
    if "vinyl" in text or any(token in text for token in ('7"', '10"', '12"')):
        return "vinyl"
    if "cassette" in text:
        return "cassette"
    if "compact disc" in text or text.strip() == "cd" or " cd" in text:
        return "cd"
    if "digital" in text:
        return "digital_music"
    return "music_other"


def barcode_variants(barcode: str) -> list[str]:
    """Return the scanned value plus its UPC-A/EAN-13 equivalent."""
    scanned = upc_svc.normalize_barcode(barcode)
    canonical = upc_svc.normalize_upc(barcode)
    values = [scanned, canonical]
    if len(canonical) == 13 and canonical.startswith("0"):
        values.append(canonical[1:])
    return list(dict.fromkeys(value for value in values if value))


def _compatible(release: dict, preferred_media_type: str | None) -> bool:
    inferred = infer_media_type(release)
    if preferred_media_type in _PHYSICAL_TYPES:
        return inferred == preferred_media_type
    return inferred in MUSIC_MEDIA_TYPES


async def find_cover_url(
    barcode: str,
    media_type: str,
    client: httpx.AsyncClient,
) -> str | None:
    """Find front artwork for a barcode without guessing across formats.

    A vinyl item only accepts vinyl MusicBrainz candidates, and likewise for
    CD/cassette.  Ambiguous barcodes are fine for artwork: each compatible
    release is tried until Cover Art Archive has an image, but no exact release
    identity is persisted here.
    """
    releases: list[dict] = []
    for variant in barcode_variants(barcode):
        result = await musicbrainz.search_by_barcode(variant, client, limit=20)
        if result.found and result.payload:
            releases = [
                release for release in result.payload
                if isinstance(release, dict) and _compatible(release, media_type)
            ]
            if releases:
                break
        if result.outcome in {"rate_limited", "rejected", "transport_failed"}:
            return None

    for release in releases[:3]:
        release_id = release.get("musicbrainz_release_id")
        if not release_id:
            continue
        art = await musicbrainz.cover_art(release_id, client)
        if not art.found:
            continue
        candidates = art.payload or []
        front = next((candidate for candidate in candidates if candidate.get("front")), None)
        chosen = front or (candidates[0] if candidates else None)
        if chosen and chosen.get("url"):
            return chosen["url"]
    return None
''')

# music.py: browse first, release search under /music/add, plus bulk artwork repair.
replace(
    "app/routers/music.py",
    "from app.services import covers, discogs, music_catalog, musicbrainz\n",
    "from app.services import covers, discogs, music_artwork, music_catalog, musicbrainz\n",
)
replace(
    "app/routers/music.py",
    '''@router.get("/music")\nasync def music_page(\n''',
    '''@router.get("/music")\nasync def music_library(_=Depends(require_role("viewer"))):\n    """Open Music on the owned library; exact-release search is an add action."""\n    return RedirectResponse("/browse?media_family_filter=music", status_code=303)\n\n\n@router.get("/music/add")\nasync def music_page(\n''',
)
replace(
    "app/routers/music.py",
    '        return RedirectResponse("/music", status_code=303)\n',
    '        return RedirectResponse("/music/add", status_code=303)\n',
)
replace(
    "app/routers/music.py",
    '''\n\n@router.post("/api/music/add")\nasync def add_music_release(\n''',
    '''\n\n@router.post("/api/music/repair-artwork")\nasync def repair_music_artwork(_=Depends(require_role("editor"))):\n    """Fill missing covers for a bounded batch of barcode-backed music items."""\n    music_types = tuple(sorted(MUSIC_MEDIA_TYPES))\n    placeholders = ",".join("?" for _ in music_types)\n    with get_db() as db:\n        rows = db.execute(\n            f"""SELECT id, upc, media_type FROM items\n                WHERE media_type IN ({placeholders})\n                  AND upc IS NOT NULL AND TRIM(upc) != ''\n                  AND COALESCE(TRIM(cover_path), '') = ''\n                ORDER BY id LIMIT 15""",\n            music_types,\n        ).fetchall()\n\n    repaired = 0\n    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:\n        for row in rows:\n            cover_url = await music_artwork.find_cover_url(\n                row["upc"], row["media_type"], client\n            )\n            if not cover_url:\n                continue\n            cover_path = await covers._download_to_item(row["id"], cover_url, client)\n            if not cover_path:\n                continue\n            with get_db() as db:\n                cursor = db.execute(\n                    "UPDATE items SET cover_path = ?, updated_at = datetime('now') "\n                    "WHERE id = ? AND COALESCE(TRIM(cover_path), '') = ''",\n                    (cover_path, row["id"]),\n                )\n                repaired += max(cursor.rowcount, 0)\n\n    return RedirectResponse(\n        "/browse?media_family_filter=music"\n        f"&music_artwork_checked={len(rows)}&music_artwork_repaired={repaired}",\n        status_code=303,\n    )\n\n\n@router.post("/api/music/add")\nasync def add_music_release(\n''',
)

# Exact-release page wording and URLs.
replace("app/templates/music.html", "{% block title %}Music — Shelf{% endblock %}", "{% block title %}Add music — Shelf{% endblock %}")
replace("app/templates/music.html", '<h1 class="text-2xl font-bold">Music</h1>', '<h1 class="text-2xl font-bold">Add music</h1>')
replace("app/templates/music.html", 'href="/browse?media_type_filter=vinyl"', 'href="/music"')
replace("app/templates/music.html", '<form method="get" action="/music"', '<form method="get" action="/music/add"')
replace(
    "app/templates/music.html",
    '<a href="/music{% if target_item %}?item_id={{ target_item.id }}{% endif %}" class="text-sm text-shelf-muted hover:text-white">Reset search</a>',
    '<a href="/music/add{% if target_item %}?item_id={{ target_item.id }}{% endif %}" class="text-sm text-shelf-muted hover:text-white">Reset search</a>',
)
replace(
    "app/templates/fragments/music_detail.html",
    '<a href="/music?item_id={{ item.id }}" class="inline-flex px-3 py-2 bg-shelf-accent text-white rounded-lg text-sm hover:bg-shelf-accent2 transition-colors">Find exact release</a>',
    '<a href="/music/add?item_id={{ item.id }}" class="inline-flex px-3 py-2 bg-shelf-accent text-white rounded-lg text-sm hover:bg-shelf-accent2 transition-colors">Find exact release</a>',
)

# Browse becomes the actual Music landing page when filtered to the music family.
replace(
    "app/templates/browse.html",
    '''    <!-- Browse heading -->\n    <div class="flex flex-wrap items-end justify-between gap-3 mb-4">\n        <div>\n            <p class="text-xs uppercase tracking-wider text-shelf-muted mb-1">Library</p>\n            <h1 class="text-2xl font-bold whitespace-nowrap">Browse <span id="collection-count" class="text-shelf-muted text-lg font-normal">({{ filtered_total }})</span></h1>\n        </div>\n        {% if can_select %}\n        <div x-show="selectMode" x-cloak class="flex items-center gap-1">\n            <button @click="selectAll()" data-testid="select-all" class="px-2 py-1 rounded text-xs text-shelf-muted hover:text-shelf-accent2 transition-colors">Select All</button>\n            <button @click="deselectAll()" x-show="selectedIds.length > 0" class="px-2 py-1 rounded text-xs text-shelf-muted hover:text-shelf-accent2 transition-colors">Deselect All</button>\n        </div>\n        {% endif %}\n    </div>\n''',
    '''    <!-- Browse heading -->\n    {% set music_browse = f.media_family_filter == 'music' %}\n    <div class="flex flex-wrap items-end justify-between gap-3 mb-4">\n        <div>\n            <p class="text-xs uppercase tracking-wider text-shelf-muted mb-1">Library</p>\n            <h1 class="text-2xl font-bold whitespace-nowrap">{% if music_browse %}Music{% else %}Browse{% endif %} <span id="collection-count" class="text-shelf-muted text-lg font-normal">({{ filtered_total }})</span></h1>\n            {% if music_browse %}<p class="text-sm text-shelf-muted mt-1">Vinyl, CDs, cassettes and digital music in your library.</p>{% endif %}\n        </div>\n        <div class="flex flex-wrap items-center gap-2">\n            {% if music_browse and can_select %}\n            <form method="post" action="/api/music/repair-artwork">\n                <button type="submit" data-testid="find-music-artwork" class="px-3 py-2 rounded-lg bg-shelf-hover border border-shelf-border text-sm text-shelf-muted hover:text-white hover:border-shelf-accent/50 transition-colors">Find missing artwork</button>\n            </form>\n            <a href="/music/add" data-testid="add-music" class="px-3 py-2 rounded-lg bg-shelf-accent text-white text-sm hover:bg-shelf-accent2 transition-colors">Add music</a>\n            {% endif %}\n            {% if can_select %}\n            <div x-show="selectMode" x-cloak class="flex items-center gap-1">\n                <button @click="selectAll()" data-testid="select-all" class="px-2 py-1 rounded text-xs text-shelf-muted hover:text-shelf-accent2 transition-colors">Select All</button>\n                <button @click="deselectAll()" x-show="selectedIds.length > 0" class="px-2 py-1 rounded text-xs text-shelf-muted hover:text-shelf-accent2 transition-colors">Deselect All</button>\n            </div>\n            {% endif %}\n        </div>\n    </div>\n    {% if music_browse and request.query_params.get('music_artwork_checked') %}\n    <div class="mb-4 rounded-lg border border-shelf-border bg-shelf-card px-4 py-3 text-sm text-shelf-muted" data-testid="music-artwork-result">\n        Artwork search checked {{ request.query_params.get('music_artwork_checked') }} coverless music item{% if request.query_params.get('music_artwork_checked') != '1' %}s{% endif %} and added {{ request.query_params.get('music_artwork_repaired', '0') }} cover{% if request.query_params.get('music_artwork_repaired', '0') != '1' %}s{% endif %}.\n    </div>\n    {% endif %}\n''',
)

# Existing route regressions now exercise the browse-first contract.
replace(
    "tests/test_music.py",
    '''class TestMusicRoutes:\n    def test_viewer_can_open_music_page(self, viewer_client):\n        response = viewer_client.get("/music")\n        assert response.status_code == 200\n        assert "Search exact MusicBrainz releases" in response.text\n\n    def test_search_renders_exact_release_choices(self, viewer_client, monkeypatch):\n''',
    '''class TestMusicRoutes:\n    def test_music_opens_owned_library_first(self, viewer_client):\n        response = viewer_client.get("/music", follow_redirects=False)\n        assert response.status_code == 303\n        assert response.headers["location"] == "/browse?media_family_filter=music"\n\n    def test_add_page_keeps_exact_release_search(self, viewer_client):\n        response = viewer_client.get("/music/add")\n        assert response.status_code == 200\n        assert "Search exact MusicBrainz releases" in response.text\n        assert "Add music" in response.text\n\n    def test_search_renders_exact_release_choices(self, viewer_client, monkeypatch):\n''',
)
replace(
    "tests/test_music.py",
    'response = viewer_client.get("/music?q=dark+side&artist=Pink+Floyd")',
    'response = viewer_client.get("/music/add?q=dark+side&artist=Pink+Floyd")',
)

# Existing enrich test follows the relocated exact-release picker.
p = Path("tests/test_music_enrich.py")
text = p.read_text()
text = text.replace('f"/music?item_id={item_id}"', 'f"/music/add?item_id={item_id}"')
p.write_text(text)

Path("tests/test_music_artwork.py").write_text(r'''"""Browse-first Music and barcode sleeve repair regressions."""

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
''')
