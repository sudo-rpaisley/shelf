from pathlib import Path


def replace(path: str, old: str, new: str, expected: int = 1) -> None:
    p = Path(path)
    text = p.read_text()
    actual = text.count(old)
    if actual != expected:
        raise RuntimeError(f"{path}: expected {expected} occurrence(s), found {actual}: {old[:120]!r}")
    p.write_text(text.replace(old, new, expected))


# ---------------------------------------------------------------------------
# Shared music type inference
# ---------------------------------------------------------------------------
replace(
    "app/services/music_catalog.py",
    '''def _now() -> str:\n    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()\n\n\n''',
    '''def _now() -> str:\n    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()\n\n\ndef infer_media_type(release: dict) -> str:\n    """Map MusicBrainz medium formats to Shelf's music media types."""\n    media = release.get("media") or []\n    formats = [\n        str(medium.get("format") or "").casefold()\n        for medium in media\n        if isinstance(medium, dict)\n    ]\n    summary = str(release.get("format_summary") or "").casefold()\n    if not formats and summary:\n        formats = [summary]\n    for fmt in formats:\n        if "vinyl" in fmt or fmt in {'7"', '10"', '12"'}:\n            return "vinyl"\n        if "cassette" in fmt:\n            return "cassette"\n        if fmt == "cd" or "compact disc" in fmt:\n            return "cd"\n        if "digital" in fmt:\n            return "digital_music"\n    return "music_other"\n\n\n''',
)

# ---------------------------------------------------------------------------
# Barcode-first MusicBrainz resolver used by scan and repair flows
# ---------------------------------------------------------------------------
Path("app/services/music_scan.py").write_text(r'''"""Barcode-first metadata and artwork matching for physical music.

UPC Item DB is useful for a retail title, but it is not a music catalogue and
usually supplies no usable sleeve artwork.  MusicBrainz is already Shelf's
canonical release identity source, so scans use its exact barcode index before
falling back to the generic UPC title-only path.  Ambiguous barcodes never get
an exact pressing identity silently: only a single format-compatible result is
persisted as a MusicBrainz Release.
"""

from __future__ import annotations

import httpx

from app.config import MUSIC_MEDIA_TYPES
from app.services import music_catalog, musicbrainz, provider_result
from app.services import upc as upc_svc

_SPECIFIC_PHYSICAL_TYPES = {"vinyl", "cd", "cassette"}


def _year(value: str | None) -> int | None:
    text = (value or "").strip()
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None


def _barcode_variants(barcode: str) -> list[str]:
    """Try the scanned code and its UPC-A/EAN-13 equivalent once each."""
    scanned = upc_svc.normalize_barcode(barcode)
    canonical = upc_svc.normalize_upc(barcode)
    values = [scanned, canonical]
    if len(canonical) == 13 and canonical.startswith("0"):
        values.append(canonical[1:])
    return list(dict.fromkeys(value for value in values if value))


def _common_value(candidates: list[dict], key: str):
    values = {candidate.get(key) for candidate in candidates if candidate.get(key)}
    return next(iter(values)) if len(values) == 1 else None


async def _search_variants(barcode: str, client: httpx.AsyncClient):
    attempts = []
    for variant in _barcode_variants(barcode):
        result = await musicbrainz.search_by_barcode(variant, client, limit=20)
        if result.found and result.payload:
            return result, variant
        if result.found:
            result = provider_result.no_match("musicbrainz")
        attempts.append(result)
        if result.outcome in ("rate_limited", "rejected", "transport_failed"):
            return result, variant
    return provider_result.combine(attempts, provider="musicbrainz"), None


async def _cover_url(release_ids: list[str], client: httpx.AsyncClient) -> str | None:
    """Find front artwork without assuming the first barcode match is exact."""
    for release_id in release_ids[:3]:
        art = await musicbrainz.cover_art(release_id, client)
        if not art.found:
            continue
        candidates = art.payload or []
        front = next((candidate for candidate in candidates if candidate.get("front")), None)
        chosen = front or (candidates[0] if candidates else None)
        if chosen and chosen.get("url"):
            return chosen["url"]
    return None


async def lookup_barcode(
    barcode: str,
    client: httpx.AsyncClient,
    *,
    preferred_media_type: str | None = None,
    load_release: bool = True,
    include_cover: bool = True,
) -> provider_result.ProviderResult:
    """Return safe music metadata for an exact UPC/EAN barcode.

    A specific Vinyl/CD/Cassette preference filters the provider results.  If
    several compatible releases remain, Shelf can still use their common
    title/artist and artwork, but it does not claim one exact pressing.  A
    single candidate is looked up fully so media and tracks can be persisted.
    """
    result, matched_barcode = await _search_variants(barcode, client)
    if not result.found or not result.payload:
        return result if not result.found else provider_result.no_match("musicbrainz")

    typed = [
        (candidate, music_catalog.infer_media_type(candidate))
        for candidate in result.payload
        if isinstance(candidate, dict) and candidate.get("musicbrainz_release_id")
    ]
    if not typed:
        return provider_result.no_match("musicbrainz")

    preferred = preferred_media_type if preferred_media_type in _SPECIFIC_PHYSICAL_TYPES else None
    candidates = typed
    if preferred:
        candidates = [entry for entry in typed if entry[1] == preferred]
        if not candidates:
            # An exact barcode hit for a different format is not permission to
            # rewrite an explicitly identified Vinyl/CD/Cassette scan.
            return provider_result.no_match("musicbrainz")

    inferred_types = {media_type for _, media_type in candidates}
    if preferred:
        media_type = preferred
    elif len(inferred_types) == 1:
        media_type = next(iter(inferred_types))
    else:
        media_type = "music_other"
    if media_type not in MUSIC_MEDIA_TYPES:
        media_type = "music_other"

    summaries = [candidate for candidate, _ in candidates]
    exact_summary = summaries[0] if len(summaries) == 1 else None
    release = None
    metadata_source = exact_summary or summaries[0]

    if exact_summary and load_release:
        detail = await musicbrainz.lookup_release(
            exact_summary["musicbrainz_release_id"], client
        )
        if detail.found:
            release = detail.payload
            metadata_source = release

    title = _common_value(summaries, "title") or metadata_source.get("title") or "Untitled"
    artist = _common_value(summaries, "artist_credit") or metadata_source.get("artist_credit")
    release_ids = [
        summary["musicbrainz_release_id"]
        for summary in summaries
        if summary.get("musicbrainz_release_id")
    ]
    cover_url = await _cover_url(release_ids, client) if include_cover else None

    payload = {
        "media_type": media_type,
        "title": title,
        "authors": artist,
        "publisher": metadata_source.get("label"),
        "publish_year": _year(metadata_source.get("release_date"))
            or _year(metadata_source.get("first_release_date")),
        "cover_url": cover_url,
        "release": release,
        "release_ids": release_ids,
        "matched_barcode": matched_barcode,
        "ambiguous": len(summaries) > 1,
    }
    return provider_result.found("musicbrainz", payload)
''')

# ---------------------------------------------------------------------------
# Music routes: browse first, exact-release add page second, repair old covers
# ---------------------------------------------------------------------------
replace(
    "app/routers/music.py",
    "from app.services import covers, discogs, music_catalog, musicbrainz\n",
    "from app.services import covers, discogs, music_catalog, musicbrainz, music_scan\n",
)
replace(
    "app/routers/music.py",
    '''def _infer_media_type(release: dict) -> str:\n    """Map MusicBrainz's exact medium format to Shelf's owned-item family."""\n    media = release.get("media") or []\n    formats = [\n        str(m.get("format") or "").casefold()\n        for m in media\n        if isinstance(m, dict)\n    ]\n    summary = str(release.get("format_summary") or "").casefold()\n    if not formats and summary:\n        formats = [summary]\n    for fmt in formats:\n        if "vinyl" in fmt or fmt in {"7\\\"", "10\\\"", "12\\\""}:\n            return "vinyl"\n        if "cassette" in fmt:\n            return "cassette"\n        if fmt == "cd" or "compact disc" in fmt:\n            return "cd"\n        if "digital" in fmt:\n            return "digital_music"\n    return "music_other"\n\n\n''',
    '',
)
replace(
    "app/routers/music.py",
    '''@router.get("/music")\nasync def music_page(\n''',
    '''@router.get("/music")\nasync def music_library(_=Depends(require_role("viewer"))):\n    """Music opens on the user's library; release search is an add action."""\n    return RedirectResponse("/browse?media_family_filter=music", status_code=303)\n\n\n@router.get("/music/add")\nasync def music_page(\n''',
)
replace(
    "app/routers/music.py",
    'release["shelf_media_type"] = _infer_media_type(release)',
    'release["shelf_media_type"] = music_catalog.infer_media_type(release)',
)
replace(
    "app/routers/music.py",
    'media_type = _infer_media_type(release)',
    'media_type = music_catalog.infer_media_type(release)',
)
replace(
    "app/routers/music.py",
    'return RedirectResponse("/music", status_code=303)\n\n    try:',
    'return RedirectResponse("/music/add", status_code=303)\n\n    try:',
)
replace(
    "app/routers/music.py",
    'return RedirectResponse(f"/music?item_id={item_id}", status_code=303)',
    'return RedirectResponse(f"/music/add?item_id={item_id}", status_code=303)',
)

insert_after = '''async def _apply_release_artwork(item_id: int, release_id: str) -> None:\n'''
# Add the repair route after the artwork helper by anchoring on the next API route.
replace(
    "app/routers/music.py",
    '''\n\n@router.post("/api/music/add")\nasync def add_music_release(\n''',
    '''\n\n@router.post("/api/music/repair-artwork")\nasync def repair_music_artwork(\n    _=Depends(require_role("editor")),\n):\n    """Repair a bounded batch of coverless barcode-backed music items."""\n    music_types = tuple(sorted(MUSIC_MEDIA_TYPES))\n    placeholders = ",".join("?" for _ in music_types)\n    with get_db() as db:\n        rows = db.execute(\n            f"""SELECT id, upc, media_type FROM items\n                WHERE media_type IN ({placeholders})\n                  AND upc IS NOT NULL AND TRIM(upc) != ''\n                  AND COALESCE(TRIM(cover_path), '') = ''\n                ORDER BY id LIMIT 25""",\n            music_types,\n        ).fetchall()\n\n    repaired = 0\n    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:\n        for row in rows:\n            preferred = row["media_type"] if row["media_type"] in {"vinyl", "cd", "cassette"} else None\n            result = await music_scan.lookup_barcode(\n                row["upc"], client, preferred_media_type=preferred,\n                load_release=False, include_cover=True,\n            )\n            if not result.found or not result.payload.get("cover_url"):\n                continue\n            cover_path = await covers._download_to_item(\n                row["id"], result.payload["cover_url"], client\n            )\n            if not cover_path:\n                continue\n            with get_db() as db:\n                cursor = db.execute(\n                    "UPDATE items SET cover_path = ?, updated_at = datetime('now') "\n                    "WHERE id = ? AND COALESCE(TRIM(cover_path), '') = ''",\n                    (cover_path, row["id"]),\n                )\n                repaired += max(cursor.rowcount, 0)\n\n    return RedirectResponse(\n        "/browse?media_family_filter=music"\n        f"&music_artwork_repaired={repaired}&music_artwork_checked={len(rows)}",\n        status_code=303,\n    )\n\n\n@router.post("/api/music/add")\nasync def add_music_release(\n''',
)

# ---------------------------------------------------------------------------
# Music add/search template and item detail links
# ---------------------------------------------------------------------------
replace("app/templates/music.html", "{% block title %}Music — Shelf{% endblock %}", "{% block title %}Add music — Shelf{% endblock %}")
replace("app/templates/music.html", '<h1 class="text-2xl font-bold">Music</h1>', '<h1 class="text-2xl font-bold">Add music</h1>')
replace(
    "app/templates/music.html",
    '<a href="/browse?media_type_filter=vinyl" class="text-sm px-3 py-2 rounded-lg bg-shelf-hover text-shelf-muted hover:text-white transition-colors">Browse music</a>',
    '<a href="/music" class="text-sm px-3 py-2 rounded-lg bg-shelf-hover text-shelf-muted hover:text-white transition-colors">Browse music</a>',
)
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

# ---------------------------------------------------------------------------
# Browse makes Music feel like a library destination and exposes its actions
# ---------------------------------------------------------------------------
replace(
    "app/templates/browse.html",
    '''    <!-- Browse heading -->\n    <div class="flex flex-wrap items-end justify-between gap-3 mb-4">\n        <div>\n            <p class="text-xs uppercase tracking-wider text-shelf-muted mb-1">Library</p>\n            <h1 class="text-2xl font-bold whitespace-nowrap">Browse <span id="collection-count" class="text-shelf-muted text-lg font-normal">({{ filtered_total }})</span></h1>\n        </div>\n        {% if can_select %}\n        <div x-show="selectMode" x-cloak class="flex items-center gap-1">\n            <button @click="selectAll()" data-testid="select-all" class="px-2 py-1 rounded text-xs text-shelf-muted hover:text-shelf-accent2 transition-colors">Select All</button>\n            <button @click="deselectAll()" x-show="selectedIds.length > 0" class="px-2 py-1 rounded text-xs text-shelf-muted hover:text-shelf-accent2 transition-colors">Deselect All</button>\n        </div>\n        {% endif %}\n    </div>\n''',
    '''    <!-- Browse heading -->\n    {% set music_browse = f.media_family_filter == 'music' %}\n    <div class="flex flex-wrap items-end justify-between gap-3 mb-4">\n        <div>\n            <p class="text-xs uppercase tracking-wider text-shelf-muted mb-1">Library</p>\n            <h1 class="text-2xl font-bold whitespace-nowrap">{% if music_browse %}Music{% else %}Browse{% endif %} <span id="collection-count" class="text-shelf-muted text-lg font-normal">({{ filtered_total }})</span></h1>\n            {% if music_browse %}<p class="text-sm text-shelf-muted mt-1">Vinyl, CDs, cassettes and digital music in your library.</p>{% endif %}\n        </div>\n        <div class="flex flex-wrap items-center gap-2">\n            {% if music_browse and can_select %}\n            <form method="post" action="/api/music/repair-artwork">\n                <button type="submit" class="px-3 py-2 rounded-lg bg-shelf-hover border border-shelf-border text-sm text-shelf-muted hover:text-white hover:border-shelf-accent/50 transition-colors">Find missing artwork</button>\n            </form>\n            <a href="/music/add" class="px-3 py-2 rounded-lg bg-shelf-accent text-white text-sm hover:bg-shelf-accent2 transition-colors">Add music</a>\n            {% endif %}\n            {% if can_select %}\n            <div x-show="selectMode" x-cloak class="flex items-center gap-1">\n                <button @click="selectAll()" data-testid="select-all" class="px-2 py-1 rounded text-xs text-shelf-muted hover:text-shelf-accent2 transition-colors">Select All</button>\n                <button @click="deselectAll()" x-show="selectedIds.length > 0" class="px-2 py-1 rounded text-xs text-shelf-muted hover:text-shelf-accent2 transition-colors">Deselect All</button>\n            </div>\n            {% endif %}\n        </div>\n    </div>\n    {% if music_browse and request.query_params.get('music_artwork_checked') %}\n    <div class="mb-4 rounded-lg border border-shelf-border bg-shelf-card px-4 py-3 text-sm text-shelf-muted">\n        Artwork search checked {{ request.query_params.get('music_artwork_checked') }} coverless music item{% if request.query_params.get('music_artwork_checked') != '1' %}s{% endif %} and added {{ request.query_params.get('music_artwork_repaired', '0') }} cover{% if request.query_params.get('music_artwork_repaired', '0') != '1' %}s{% endif %}.\n    </div>\n    {% endif %}\n''',
)

# ---------------------------------------------------------------------------
# UPC scan: MusicBrainz is a real music provider and can rescue weak DVD guesses
# ---------------------------------------------------------------------------
replace(
    "app/routers/items_common.py",
    "from app.config import HTTP_TIMEOUT, MEDIA_TYPES\n",
    "from app.config import HTTP_TIMEOUT, MEDIA_TYPES, MUSIC_MEDIA_TYPES\n",
)
replace(
    "app/routers/items_common.py",
    "from app.services import igdb, scan_outcome, tmdb, upcitemdb\n",
    "from app.services import igdb, music_catalog, music_scan, scan_outcome, tmdb, upcitemdb\n",
)
replace(
    "app/routers/items_common.py",
    '''    media_type = detection.media_type\n    detect_reason = detection.reason\n    detect_overrode = media_type != hint\n\n    # Video games: the record above, then IGDB for metadata.\n''',
    '''    media_type = detection.media_type\n    detect_reason = detection.reason\n    detect_overrode = media_type != hint\n\n    # MusicBrainz is both an enrichment source for known music and a second\n    # opinion for an automatic UPC that otherwise has no format signal.  That\n    # latter case is how a bare LP retail title used to fall through to DVD.\n    music_result = None\n    music_match = None\n    should_try_music = (\n        media_type in MUSIC_MEDIA_TYPES\n        or (hint == "auto" and detection.signal == "none")\n    )\n    if should_try_music:\n        preferred_music_type = media_type if media_type in MUSIC_MEDIA_TYPES else None\n        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:\n            music_result = await music_scan.lookup_barcode(\n                upc_norm, client, preferred_media_type=preferred_music_type\n            )\n        if music_result.found:\n            music_match = music_result.payload\n            media_type = music_match["media_type"]\n            detect_reason = (\n                f"MusicBrainz matched this barcode to {MEDIA_TYPES.get(media_type, media_type)}."\n            )\n            detect_overrode = media_type != hint\n\n    # Video games: the record above, then IGDB for metadata.\n''',
)
replace(
    "app/routers/items_common.py",
    '''    # A resolved media type with no metadata provider is filed under its\n    # cleaned title with no outbound request at all. Before this, a CD was\n    # searched on The Movie Database — a real request to a film provider for a\n    # music disc — and the card then named TMDb, which is #44.\n    no_metadata_provider = media_type not in UPC_METADATA_PROVIDERS\n''',
    '''    # Music has its own barcode provider. Other unsupported media remain\n    # title-only rather than being sent to an unrelated film provider.\n    is_music = media_type in MUSIC_MEDIA_TYPES\n    no_metadata_provider = media_type not in UPC_METADATA_PROVIDERS and not is_music\n''',
)
replace(
    "app/routers/items_common.py",
    '''    metadata = None\n    # Seeded, not left unset: the ladder below runs only when a key is\n    # configured and the type has a provider, so this is what the card\n    # projects from when it does not.\n    tmdb_result = provider_result.no_credential("tmdb")\n    # A 200 can still carry a missing, blank or format-only title ("[DVD]"),\n    # which normalises to no queries at all. That is a not_found, not an index\n    # error on queries[0].\n    queries: list[str] = upcitemdb.search_queries((product or {}).get("title") or "")\n    if queries and tmdb_key and not no_metadata_provider and not is_hardware:\n''',
    '''    metadata = None\n    # Seeded, not left unset: the film ladder below runs only when a key is\n    # configured. Music carries its own ProviderResult from the barcode lookup.\n    tmdb_result = provider_result.no_credential("tmdb")\n    # A 200 can still carry a missing, blank or format-only title ("[DVD]").\n    queries: list[str] = upcitemdb.search_queries((product or {}).get("title") or "")\n    if is_music and music_match:\n        metadata = {\n            "title": music_match["title"],\n            "authors": music_match.get("authors"),\n            "publisher": music_match.get("publisher"),\n            "description": None,\n            "publish_year": music_match.get("publish_year"),\n            "cover_url": music_match.get("cover_url"),\n        }\n        # MusicBrainz can rescue a barcode that UPC Item DB does not know.\n        if not queries:\n            queries = [metadata["title"]]\n    elif queries and tmdb_key and not no_metadata_provider and not is_hardware:\n''',
)
replace(
    "app/routers/items_common.py",
    '''    enrich_status = "no_lookup" if is_hardware else scan_outcome.enrich_status(\n        tmdb_result, has_provider=not no_metadata_provider\n    )\n    # `None` so no arm can interpolate a provider name that does not exist:\n    # the template's `no_provider` arm names none, and this router cannot\n    # reach an arm that does. A hardware scan names nobody, one step stronger.\n    enrich_provider = None if (no_metadata_provider or is_hardware) else "TMDb"\n\n    # Provenance, decided before the placeholder below overwrites `metadata`.\n''',
    '''    if is_hardware:\n        enrich_status = "no_lookup"\n        enrich_provider = None\n    elif is_music:\n        music_result = music_result or provider_result.no_match("musicbrainz")\n        enrich_status = scan_outcome.enrich_status(music_result)\n        enrich_provider = "MusicBrainz"\n    else:\n        enrich_status = scan_outcome.enrich_status(\n            tmdb_result, has_provider=not no_metadata_provider\n        )\n        enrich_provider = None if no_metadata_provider else "TMDb"\n\n    # Provenance, decided before the placeholder below overwrites `metadata`.\n''',
)
replace(
    "app/routers/items_common.py",
    '''    source = "tmdb" if metadata else "upc"\n''',
    '''    source = (\n        "musicbrainz" if is_music and metadata\n        else "tmdb" if metadata\n        else "upc"\n    )\n''',
)
replace(
    "app/routers/items_common.py",
    '''        metadata = {\n            "title": queries[0], "description": None,\n            "publish_year": None, "cover_url": None,\n        }\n''',
    '''        metadata = {\n            "title": queries[0], "authors": None, "publisher": None,\n            "description": None, "publish_year": None, "cover_url": None,\n        }\n''',
)
replace(
    "app/routers/items_common.py",
    '''                    title=metadata["title"],\n                    description=metadata.get("description"),\n                    media_type=media_type,\n                    publish_year=metadata.get("publish_year"),\n''',
    '''                    title=metadata["title"],\n                    authors=metadata.get("authors"),\n                    description=metadata.get("description"),\n                    media_type=media_type,\n                    publisher=metadata.get("publisher"),\n                    publish_year=metadata.get("publish_year"),\n''',
)
replace(
    "app/routers/items_common.py",
    '''    # Download cover\n    cover_path = None\n''',
    '''    # Persist an exact MusicBrainz release only when the barcode resolver\n    # found one unambiguous format-compatible candidate.\n    if music_match and music_match.get("release"):\n        with get_db() as db:\n            music_catalog.save_release(db, item_id, music_match["release"])\n\n    # Download cover\n    cover_path = None\n''',
)
replace(
    "app/services/scan_outcome.py",
    '''    "upcitemdb": "UPC Item DB",\n''',
    '''    "upcitemdb": "UPC Item DB",\n    "musicbrainz": "MusicBrainz",\n''',
)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
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
        # No format words/category: legacy detection would fall back to DVD.
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
