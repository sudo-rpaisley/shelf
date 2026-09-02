from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parent.parent


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text()
    if old not in text:
        raise SystemExit(f"patch anchor missing in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1))


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(content).lstrip())


# --- config / credential plumbing -----------------------------------------
replace_once(
    "app/config.py",
    '    "coverartarchive.org": 1.0,\n    # EXPLORER',
    '    "coverartarchive.org": 1.0,\n'
    '    # Discogs publishes request ceilings through response headers. Shelf\n'
    '    # does not need bursty collector lookups, so one request/second is a\n'
    '    # deliberately conservative client-side pace.\n'
    '    "api.discogs.com": 1.0,\n'
    '    # EXPLORER',
)
replace_once(
    "app/config.py",
    '    "igdb_client_secret": "IGDB_CLIENT_SECRET",\n}',
    '    "igdb_client_secret": "IGDB_CLIENT_SECRET",\n'
    '    "discogs_token": "DISCOGS_TOKEN",\n}',
)
replace_once(
    "app/crypto.py",
    '        "igdb_client_secret",\n        # An ntfy topic URL',
    '        "igdb_client_secret",\n'
    '        "discogs_token",\n'
    '        # An ntfy topic URL',
)

# --- schema ---------------------------------------------------------------
replace_once(
    "app/database.py",
    '    (30, "Index RomM item IDs", "CREATE INDEX IF NOT EXISTS idx_items_romm_id ON items(romm_id)"),\n)',
    '''    (30, "Index RomM item IDs", "CREATE INDEX IF NOT EXISTS idx_items_romm_id ON items(romm_id)"),
    (31, "Add Discogs master ID", "ALTER TABLE music_releases ADD COLUMN discogs_master_id INTEGER DEFAULT NULL"),
    (32, "Add Discogs label", "ALTER TABLE music_releases ADD COLUMN discogs_label TEXT DEFAULT NULL"),
    (33, "Add Discogs catalogue number", "ALTER TABLE music_releases ADD COLUMN discogs_catalog_number TEXT DEFAULT NULL"),
    (34, "Add Discogs format summary", "ALTER TABLE music_releases ADD COLUMN discogs_format_summary TEXT DEFAULT NULL"),
    (35, "Add Discogs genres", "ALTER TABLE music_releases ADD COLUMN discogs_genres TEXT DEFAULT NULL"),
    (36, "Add Discogs styles", "ALTER TABLE music_releases ADD COLUMN discogs_styles TEXT DEFAULT NULL"),
    (37, "Add Discogs notes", "ALTER TABLE music_releases ADD COLUMN discogs_notes TEXT DEFAULT NULL"),
    (38, "Add Discogs cache timestamp", "ALTER TABLE music_releases ADD COLUMN discogs_updated_at TEXT DEFAULT NULL"),
    (39, "Add music identifier source", "ALTER TABLE music_identifiers ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"),
)''',
)
replace_once(
    "app/database.py",
    '''    discogs_release_id            INTEGER,
    release_type                  TEXT,''',
    '''    discogs_release_id            INTEGER,
    discogs_master_id             INTEGER,
    discogs_label                 TEXT,
    discogs_catalog_number        TEXT,
    discogs_format_summary        TEXT,
    discogs_genres                TEXT,
    discogs_styles                TEXT,
    discogs_notes                 TEXT,
    discogs_updated_at            TEXT,
    release_type                  TEXT,''',
)
replace_once(
    "app/database.py",
    'CREATE INDEX IF NOT EXISTS idx_music_releases_discogs ON music_releases(discogs_release_id);',
    'CREATE INDEX IF NOT EXISTS idx_music_releases_discogs ON music_releases(discogs_release_id);\n'
    'CREATE INDEX IF NOT EXISTS idx_music_releases_discogs_master ON music_releases(discogs_master_id);',
)
replace_once(
    "app/database.py",
    '''    description      TEXT,
    UNIQUE(item_id, identifier_type, value)''',
    '''    description      TEXT,
    source           TEXT NOT NULL DEFAULT 'manual',
    UNIQUE(item_id, identifier_type, value)''',
)

# --- settings -------------------------------------------------------------
replace_once(
    "app/routers/settings.py",
    '    "igdb_client_secret",\n)',
    '    "igdb_client_secret",\n    "discogs_token",\n)',
)
replace_once(
    "app/routers/settings.py",
    '@router.post("/vision")\nasync def update_vision_settings(',
    '''@router.post("/discogs/test")
async def test_discogs_token(discogs_token: str = Form("")):
    """Test a typed Discogs token, falling back to the configured token."""
    import httpx

    from app.config import HTTP_TIMEOUT
    from app.services import discogs

    token = discogs_token.strip()
    if not token:
        with get_db() as db:
            token = get_setting(db, "discogs_token")
    if not token:
        code = "missing"
    else:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            result = await discogs.test_connection(token, client)
        code = {
            "found": "ok",
            "rejected": "rejected",
            "rate_limited": "rate_limited",
            "transport_failed": "unavailable",
            "no_match": "invalid",
            "no_credential": "missing",
        }.get(result.outcome, "invalid")
    return RedirectResponse(url=f"/settings?discogs_test={code}", status_code=303)


@router.post("/vision")
async def update_vision_settings(''',
)
replace_once(
    "app/templates/fragments/settings/integrations.html",
    '{% include "fragments/settings/romm.html" %}\n\n    <!-- Hardcover -->',
    '{% include "fragments/settings/romm.html" %}\n\n'
    '    {% include "fragments/settings/discogs.html" %}\n\n'
    '    <!-- Hardcover -->',
)
write(
    "app/templates/fragments/settings/discogs.html",
    r'''
<!-- Discogs collector enrichment -->
<div class="bg-shelf-card rounded-xl border border-shelf-border p-6" data-testid="discogs-settings-card">
    <div class="flex items-center justify-between mb-4">
        <div>
            <h2 class="text-lg font-semibold">Discogs</h2>
            <p class="text-xs text-shelf-muted mt-1">Optional collector metadata for exact music pressings.</p>
        </div>
        <a href="https://www.discogs.com/settings/developers" target="_blank" rel="noopener"
           class="text-xs text-shelf-accent2 hover:text-shelf-accent transition-colors">Create API token</a>
    </div>

    {% set discogs_test = request.query_params.get('discogs_test') %}
    {% if discogs_test in ('ok', 'missing', 'rejected', 'rate_limited', 'unavailable', 'invalid') %}
    <div class="mb-3 text-xs {% if discogs_test == 'ok' %}text-shelf-success{% else %}text-shelf-error{% endif %}" data-testid="discogs-test-result">
        {% if discogs_test == 'ok' %}Discogs connection successful.
        {% elif discogs_test == 'missing' %}Enter or configure a Discogs API token first.
        {% elif discogs_test == 'rejected' %}Discogs rejected that token.
        {% elif discogs_test == 'rate_limited' %}Discogs is rate-limiting requests; try again later.
        {% elif discogs_test == 'unavailable' %}Discogs could not be reached.
        {% else %}Discogs did not return a valid identity for that token.
        {% endif %}
    </div>
    {% endif %}

    <form action="/api/settings" method="post" class="space-y-3">
        <div>
            <label for="discogs_token" class="block text-sm font-medium text-shelf-muted mb-1">Personal access token</label>
            <input type="password" id="discogs_token" name="discogs_token" autocomplete="new-password"
                   placeholder="{% if secrets_saved['discogs_token'] %}Saved — leave blank to keep{% elif secrets_present['discogs_token'] %}Configured by DISCOGS_TOKEN{% else %}Discogs developer token{% endif %}"
                   class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">
            <p class="text-xs text-shelf-muted mt-1">Stored encrypted at rest. You can alternatively set <span class="font-mono">DISCOGS_TOKEN</span>.</p>
        </div>
        {% if secrets_saved['discogs_token'] %}
        <label class="flex items-center gap-2 text-xs text-shelf-muted">
            <input type="checkbox" name="clear_discogs_token" class="rounded border-shelf-border"> Remove saved token
        </label>
        {% endif %}
        <div class="flex items-center gap-2">
            <button type="submit" class="px-4 py-2 bg-shelf-accent text-white rounded-lg text-sm hover:bg-shelf-accent2 transition-colors">Save</button>
            <button type="submit" formaction="/api/settings/discogs/test"
                    class="px-4 py-2 bg-shelf-hover text-shelf-text rounded-lg text-sm hover:bg-shelf-border transition-colors">Test</button>
        </div>
    </form>
</div>
''',
)

# --- music persistence ----------------------------------------------------
replace_once(
    "app/services/music_catalog.py",
    'from datetime import datetime, timezone\n',
    'import json\nfrom datetime import datetime, timedelta, timezone\n',
)
replace_once(
    "app/services/music_catalog.py",
    '''def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
''',
    '''def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _discogs_is_fresh(value: str | None) -> bool:
    """Discogs API data older than six hours must not be displayed."""
    if not value:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - stamp <= timedelta(hours=6)
''',
)
replace_once(
    "app/services/music_catalog.py",
    '\n\ndef _link_release_group_siblings(db, item_id: int, release_group_id: str | None) -> None:',
    r'''


def save_discogs_enrichment(db, item_id: int, release: dict) -> bool:
    """Cache one selected Discogs Release without changing MusicBrainz identity.

    Discogs-sourced identifiers are replaceable provider cache. Manually entered
    matrix/runout data is source='manual' and therefore survives every refresh.
    """
    try:
        release_id = int(release.get("discogs_release_id"))
    except (TypeError, ValueError):
        raise ValueError("a Discogs release ID is required")
    if release_id <= 0:
        raise ValueError("a Discogs release ID is required")

    cursor = db.execute(
        """
        UPDATE music_releases SET
            discogs_release_id = ?, discogs_master_id = ?, discogs_label = ?,
            discogs_catalog_number = ?, discogs_format_summary = ?,
            discogs_genres = ?, discogs_styles = ?, discogs_notes = ?,
            discogs_updated_at = ?
        WHERE item_id = ?
        """,
        (
            release_id,
            release.get("discogs_master_id"),
            release.get("label"),
            release.get("catalog_number"),
            release.get("format_summary"),
            json.dumps(release.get("genres") or [], ensure_ascii=False),
            json.dumps(release.get("styles") or [], ensure_ascii=False),
            release.get("notes"),
            _now(),
            item_id,
        ),
    )
    if cursor.rowcount == 0:
        return False

    db.execute(
        "DELETE FROM music_identifiers WHERE item_id = ? AND source = 'discogs'",
        (item_id,),
    )
    for identifier in release.get("identifiers") or []:
        if not isinstance(identifier, dict):
            continue
        try:
            add_identifier(
                db,
                item_id,
                identifier.get("identifier_type") or "other",
                identifier.get("value") or "",
                identifier.get("description"),
                source="discogs",
            )
        except ValueError:
            continue
    return True


def clear_discogs_enrichment(db, item_id: int) -> bool:
    """Remove the selected Discogs pressing and all provider-owned cache."""
    cursor = db.execute(
        """
        UPDATE music_releases SET
            discogs_release_id = NULL, discogs_master_id = NULL,
            discogs_label = NULL, discogs_catalog_number = NULL,
            discogs_format_summary = NULL, discogs_genres = NULL,
            discogs_styles = NULL, discogs_notes = NULL,
            discogs_updated_at = NULL
        WHERE item_id = ?
        """,
        (item_id,),
    )
    db.execute(
        "DELETE FROM music_identifiers WHERE item_id = ? AND source = 'discogs'",
        (item_id,),
    )
    return cursor.rowcount > 0


def _link_release_group_siblings(db, item_id: int, release_group_id: str | None) -> None:''',
)
replace_once(
    "app/services/music_catalog.py",
    '''    release["identifiers"] = [
        dict(identifier)
        for identifier in db.execute(
            "SELECT * FROM music_identifiers WHERE item_id = ? "
            "ORDER BY identifier_type COLLATE NOCASE, value COLLATE NOCASE",
            (item_id,),
        ).fetchall()
    ]
    return release
''',
    '''    for field in ("discogs_genres", "discogs_styles"):
        raw = release.get(field)
        try:
            release[field] = json.loads(raw) if raw else []
        except (TypeError, ValueError, json.JSONDecodeError):
            release[field] = []
    release["discogs_fresh"] = _discogs_is_fresh(release.get("discogs_updated_at"))

    identifiers = [
        dict(identifier)
        for identifier in db.execute(
            "SELECT * FROM music_identifiers WHERE item_id = ? "
            "ORDER BY identifier_type COLLATE NOCASE, value COLLATE NOCASE",
            (item_id,),
        ).fetchall()
    ]
    release["identifiers"] = [
        identifier for identifier in identifiers if identifier.get("source") != "discogs"
    ]
    release["discogs_identifiers"] = (
        [identifier for identifier in identifiers if identifier.get("source") == "discogs"]
        if release["discogs_fresh"] else []
    )
    return release
''',
)
replace_once(
    "app/services/music_catalog.py",
    '''def add_identifier(
    db, item_id: int, identifier_type: str, value: str, description: str | None = None
) -> None:
    identifier_type = (identifier_type or "").strip()
    value = (value or "").strip()
    if not identifier_type or not value:
        raise ValueError("identifier type and value are required")
    db.execute(
        "INSERT OR IGNORE INTO music_identifiers "
        "(item_id, identifier_type, value, description) VALUES (?, ?, ?, ?)",
        (item_id, identifier_type, value, (description or "").strip() or None),
    )
''',
    '''def add_identifier(
    db,
    item_id: int,
    identifier_type: str,
    value: str,
    description: str | None = None,
    *,
    source: str = "manual",
) -> None:
    identifier_type = (identifier_type or "").strip()
    value = (value or "").strip()
    source = (source or "manual").strip() or "manual"
    if not identifier_type or not value:
        raise ValueError("identifier type and value are required")
    db.execute(
        "INSERT OR IGNORE INTO music_identifiers "
        "(item_id, identifier_type, value, description, source) VALUES (?, ?, ?, ?, ?)",
        (item_id, identifier_type, value, (description or "").strip() or None, source),
    )
''',
)

# --- item-level Discogs matching -----------------------------------------
replace_once(
    "app/routers/music.py",
    'from app.database import get_db\nfrom app.services import covers, music_catalog, musicbrainz\n',
    'from app.database import get_db, get_setting\n'
    'from app.services import covers, discogs, music_catalog, musicbrainz\n',
)
replace_once(
    "app/routers/music.py",
    '''def _music_item(db, item_id: int | None):
''',
    '''def _discogs_error(result) -> str | None:
    if result is None or result.found:
        return None
    return {
        "no_credential": "Configure a Discogs API token in Settings first.",
        "rate_limited": "Discogs is rate-limiting requests. Try again shortly.",
        "transport_failed": "Discogs could not be reached.",
        "rejected": "Discogs rejected the configured token.",
        "no_match": "No matching Discogs releases were found.",
    }.get(result.outcome, "Discogs search failed.")


def _music_item(db, item_id: int | None):
''',
)
with (ROOT / "app/routers/music.py").open("a") as handle:
    handle.write(dedent(r'''


@router.get("/music/item/{item_id}/discogs")
async def match_discogs_release(
    request: Request,
    item_id: int,
    q: str = Query(""),
    artist: str = Query(""),
    barcode: str = Query(""),
    catalog_number: str = Query(""),
    _=Depends(require_role("editor")),
):
    """Pick the exact Discogs Release that represents this physical pressing."""
    q = q.strip()[:200]
    artist = artist.strip()[:200]
    barcode = upc_svc.normalize_barcode(barcode)[:32]
    catalog_number = catalog_number.strip()[:100]

    with get_db() as db:
        item = _music_item(db, item_id)
        release = music_catalog.get_release(db, item_id) if item else None
        token = get_setting(db, "discogs_token")
    if not item:
        return RedirectResponse("/music", status_code=303)
    if not release:
        return RedirectResponse(f"/music?item_id={item_id}", status_code=303)

    if not (q or artist or barcode or catalog_number):
        q = item["title"] or ""
        artist = release.get("artist_credit") or item["authors"] or ""
        barcode = item["upc"] or ""
        catalog_number = release.get("catalog_number") or ""

    results: list[dict] = []
    error = None
    if not token:
        error = "Configure a Discogs API token in Settings before matching a pressing."
    else:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            if barcode:
                result = await discogs.search_releases("", client, token=token, barcode=barcode, limit=30)
            elif catalog_number:
                result = await discogs.search_releases(
                    "", client, token=token, artist=artist or None,
                    catalog_number=catalog_number, limit=30,
                )
            else:
                result = await discogs.search_releases(
                    q, client, token=token, artist=artist or None, limit=30,
                )
        if result.found:
            results = result.payload or []
        else:
            error = _discogs_error(result)

    return request.app.state.templates.TemplateResponse(
        request,
        "discogs_match.html",
        {
            "item": item,
            "release": release,
            "results": results,
            "error": error,
            "discogs_configured": bool(token),
            "q": q,
            "artist": artist,
            "barcode": barcode,
            "catalog_number": catalog_number,
        },
    )


@router.post("/api/music/items/{item_id}/discogs")
async def attach_discogs_release(
    item_id: int,
    release_id: int = Form(...),
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        item = _music_item(db, item_id)
        current = music_catalog.get_release(db, item_id) if item else None
        token = get_setting(db, "discogs_token")
    if not item or not current:
        return HTMLResponse("This item needs an exact MusicBrainz release first", status_code=400)
    if not token:
        return HTMLResponse("Discogs API token is not configured", status_code=400)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await discogs.lookup_release(release_id, client, token=token)
    if not result.found:
        return HTMLResponse(_discogs_error(result) or "Discogs release lookup failed", status_code=502)

    with get_db() as db:
        if not music_catalog.save_discogs_enrichment(db, item_id, result.payload):
            return HTMLResponse("Music release no longer exists", status_code=404)
    return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)


@router.post("/api/music/items/{item_id}/discogs/refresh")
async def refresh_discogs_release(
    item_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        row = db.execute(
            "SELECT discogs_release_id FROM music_releases WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        token = get_setting(db, "discogs_token")
    if not row or not row["discogs_release_id"]:
        return HTMLResponse("This copy has no Discogs release match", status_code=400)
    if not token:
        return HTMLResponse("Discogs API token is not configured", status_code=400)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await discogs.lookup_release(row["discogs_release_id"], client, token=token)
    if not result.found:
        return HTMLResponse(_discogs_error(result) or "Discogs refresh failed", status_code=502)
    with get_db() as db:
        music_catalog.save_discogs_enrichment(db, item_id, result.payload)
    return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)


@router.post("/api/music/items/{item_id}/discogs/clear")
async def clear_discogs_release(
    item_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        music_catalog.clear_discogs_enrichment(db, item_id)
    return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)
'''))

write(
    "app/templates/discogs_match.html",
    r'''
{% extends "base.html" %}
{% block title %}Match Discogs release — Shelf{% endblock %}
{% block content %}
<div class="max-w-4xl mx-auto">
    <div class="flex items-center justify-between gap-3 mb-6">
        <div>
            <a href="/item/{{ item.id }}?from=music" class="text-sm text-shelf-accent2 hover:text-shelf-accent">&larr; Back to item</a>
            <h1 class="text-2xl font-bold mt-2">Find the exact Discogs pressing</h1>
            <p class="text-sm text-shelf-muted mt-1">{{ release.artist_credit or item.authors }} — {{ item.title }}</p>
        </div>
        <a href="/settings" class="text-xs px-3 py-2 bg-shelf-hover text-shelf-muted rounded hover:text-white transition-colors">Discogs settings</a>
    </div>

    <form method="get" action="/music/item/{{ item.id }}/discogs" class="bg-shelf-card border border-shelf-border rounded-xl p-5 mb-6">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div><label class="block text-xs text-shelf-muted mb-1">Title</label><input name="q" value="{{ q }}" class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-shelf-text"></div>
            <div><label class="block text-xs text-shelf-muted mb-1">Artist</label><input name="artist" value="{{ artist }}" class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-shelf-text"></div>
            <div><label class="block text-xs text-shelf-muted mb-1">Barcode</label><input name="barcode" value="{{ barcode }}" class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-shelf-text font-mono"></div>
            <div><label class="block text-xs text-shelf-muted mb-1">Catalogue number</label><input name="catalog_number" value="{{ catalog_number }}" class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-shelf-text font-mono"></div>
        </div>
        <button type="submit" class="mt-4 px-4 py-2 bg-shelf-accent text-white rounded-lg text-sm hover:bg-shelf-accent2 transition-colors">Search Discogs</button>
    </form>

    {% if error %}
    <div class="bg-shelf-warning/10 border border-shelf-warning/30 rounded-lg p-4 text-sm mb-5" data-testid="discogs-search-error">
        <p>{{ error }}</p>
        {% if not discogs_configured %}<a href="/settings" class="text-shelf-accent2 hover:text-shelf-accent">Configure Discogs in Settings</a>{% endif %}
    </div>
    {% endif %}

    {% if results %}
    <div class="space-y-3" data-testid="discogs-results">
        {% for result in results %}
        <div class="bg-shelf-card border border-shelf-border rounded-xl p-4">
            <div class="flex flex-col sm:flex-row sm:items-start justify-between gap-4">
                <div class="min-w-0">
                    <h2 class="font-semibold">{{ result.title }}</h2>
                    <div class="text-sm text-shelf-muted mt-1 flex flex-wrap gap-x-3 gap-y-1">
                        {% if result.release_date %}<span>{{ result.release_date }}</span>{% endif %}
                        {% if result.country %}<span>{{ result.country }}</span>{% endif %}
                        {% if result.label %}<span>{{ result.label }}</span>{% endif %}
                        {% if result.catalog_number %}<span class="font-mono">{{ result.catalog_number }}</span>{% endif %}
                    </div>
                    {% if result.format_summary %}<p class="text-xs text-shelf-muted mt-2">{{ result.format_summary }}</p>{% endif %}
                    {% if result.barcodes %}<p class="text-xs text-shelf-muted mt-1">Barcode: <span class="font-mono">{{ result.barcodes|join(', ') }}</span></p>{% endif %}
                    {% if result.discogs_url %}<a href="{{ result.discogs_url }}" target="_blank" rel="noopener" class="inline-block text-xs text-shelf-accent2 hover:text-shelf-accent mt-2">Data provided by Discogs.</a>{% endif %}
                </div>
                <form method="post" action="/api/music/items/{{ item.id }}/discogs" class="shrink-0">
                    <input type="hidden" name="release_id" value="{{ result.discogs_release_id }}">
                    <button type="submit" class="px-4 py-2 bg-shelf-accent text-white rounded-lg text-sm hover:bg-shelf-accent2 transition-colors">Use this pressing</button>
                </form>
            </div>
        </div>
        {% endfor %}
    </div>
    {% endif %}
</div>
{% endblock %}
''',
)

# Discogs detail block — source data is shown only while the cache is fresh.
replace_once(
    "app/templates/fragments/music_detail.html",
    '''    {% if release.media %}
    <div class="space-y-5">''',
    r'''    {% if release.discogs_release_id %}
    <div class="mb-5 bg-shelf-bg rounded-lg p-4" data-testid="discogs-release-detail">
        <div class="flex items-start justify-between gap-3">
            <div>
                <h3 class="text-xs font-semibold uppercase tracking-wider text-shelf-muted mb-2">Discogs pressing</h3>
                {% if release.discogs_fresh %}
                <div class="text-sm space-y-1">
                    {% if release.discogs_label %}<div><span class="text-shelf-muted">Label:</span> {{ release.discogs_label }}</div>{% endif %}
                    {% if release.discogs_catalog_number %}<div><span class="text-shelf-muted">Catalogue #:</span> <span class="font-mono">{{ release.discogs_catalog_number }}</span></div>{% endif %}
                    {% if release.discogs_format_summary %}<div><span class="text-shelf-muted">Exact format:</span> {{ release.discogs_format_summary }}</div>{% endif %}
                    {% if release.discogs_genres %}<div><span class="text-shelf-muted">Genres:</span> {{ release.discogs_genres|join(', ') }}</div>{% endif %}
                    {% if release.discogs_styles %}<div><span class="text-shelf-muted">Styles:</span> {{ release.discogs_styles|join(', ') }}</div>{% endif %}
                </div>
                {% if release.discogs_identifiers %}
                <div class="flex flex-wrap gap-2 mt-3">
                    {% for identifier in release.discogs_identifiers %}
                    <span class="px-2 py-1 bg-shelf-hover rounded text-xs"><span class="text-shelf-muted">{{ identifier.identifier_type|replace('_', ' ')|title }}:</span> <span class="font-mono">{{ identifier.value }}</span>{% if identifier.description %} · {{ identifier.description }}{% endif %}</span>
                    {% endfor %}
                </div>
                {% endif %}
                {% if release.discogs_notes %}<p class="text-xs text-shelf-muted mt-3 whitespace-pre-line">{{ release.discogs_notes }}</p>{% endif %}
                {% else %}
                <p class="text-sm text-shelf-muted">Cached Discogs data is older than six hours and is hidden until refreshed.</p>
                {% endif %}
                <p class="mt-3 text-xs"><a class="text-shelf-accent2 hover:text-shelf-accent" target="_blank" rel="noopener" href="https://www.discogs.com/release/{{ release.discogs_release_id }}">Data provided by Discogs.</a> <span class="text-shelf-muted">Release #{{ release.discogs_release_id }}</span></p>
                {% if release.discogs_master_id %}<p class="text-xs text-shelf-muted mt-1">Master: <a class="text-shelf-accent2 hover:text-shelf-accent" target="_blank" rel="noopener" href="https://www.discogs.com/master/{{ release.discogs_master_id }}">{{ release.discogs_master_id }}</a></p>{% endif %}
            </div>
            {% if user and user.role in ('admin', 'editor') %}
            <div class="flex flex-col gap-2 shrink-0">
                <form method="post" action="/api/music/items/{{ item.id }}/discogs/refresh"><button type="submit" class="text-xs px-3 py-1.5 bg-shelf-hover text-shelf-muted rounded hover:text-white transition-colors">Refresh Discogs</button></form>
                <a href="/music/item/{{ item.id }}/discogs" class="text-xs px-3 py-1.5 bg-shelf-hover text-shelf-muted rounded hover:text-white transition-colors text-center">Change match</a>
                <form method="post" action="/api/music/items/{{ item.id }}/discogs/clear" data-confirm="Remove this Discogs pressing match?"><button type="submit" class="text-xs px-3 py-1.5 bg-shelf-error/20 text-shelf-error rounded hover:bg-shelf-error/30 transition-colors w-full">Remove match</button></form>
            </div>
            {% endif %}
        </div>
    </div>
    {% elif user and user.role in ('admin', 'editor') %}
    <div class="mb-5"><a href="/music/item/{{ item.id }}/discogs" class="inline-flex px-3 py-2 bg-shelf-hover text-shelf-text rounded-lg text-sm hover:bg-shelf-border transition-colors">Find Discogs pressing</a></div>
    {% endif %}

    {% if release.media %}
    <div class="space-y-5">''',
)

# --- focused regressions --------------------------------------------------
write(
    "tests/test_discogs.py",
    r'''
"""Discogs collector-enrichment regression tests."""

from app.database import get_setting
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
''',
)

print("Discogs integration patch applied")
