from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"expected patch anchor not found in {path}: {old!r}")
    p.write_text(text.replace(old, new, 1))


replace(
    "app/config.py",
    '    "igdb_client_secret": "IGDB_CLIENT_SECRET",\n}',
    '    "igdb_client_secret": "IGDB_CLIENT_SECRET",\n    "discogs_token": "DISCOGS_TOKEN",\n}',
)
replace(
    "app/crypto.py",
    '        "igdb_client_secret",\n        # An ntfy topic URL',
    '        "igdb_client_secret",\n        "discogs_token",\n        # An ntfy topic URL',
)
replace(
    "app/routers/settings.py",
    '    "igdb_client_secret",\n)',
    '    "igdb_client_secret",\n    "discogs_token",\n)',
)
replace(
    "app/database.py",
    'CREATE INDEX IF NOT EXISTS idx_music_identifiers_value\n    ON music_identifiers(value COLLATE NOCASE);\n"""',
    '''CREATE INDEX IF NOT EXISTS idx_music_identifiers_value
    ON music_identifiers(value COLLATE NOCASE);

-- Optional Discogs exact-pressing enrichment
CREATE TABLE IF NOT EXISTS music_discogs (
    item_id             INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    discogs_release_id  INTEGER NOT NULL,
    discogs_master_id   INTEGER,
    label               TEXT,
    catalog_number      TEXT,
    format_summary      TEXT,
    genres_json         TEXT,
    styles_json         TEXT,
    notes               TEXT,
    discogs_url         TEXT,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_music_discogs_release
    ON music_discogs(discogs_release_id);

CREATE TABLE IF NOT EXISTS music_discogs_identifiers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id          INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    identifier_type  TEXT NOT NULL,
    value            TEXT NOT NULL,
    description      TEXT,
    UNIQUE(item_id, identifier_type, value)
);
CREATE INDEX IF NOT EXISTS idx_music_discogs_identifiers_item
    ON music_discogs_identifiers(item_id);
"""''',
)
replace(
    "app/main.py",
    'archive, shelf_fill, romm, komga, periodicals, music\n',
    'archive, shelf_fill, romm, komga, periodicals, music, discogs\n',
)
replace(
    "app/main.py",
    'app.include_router(periodicals.router)',
    'app.include_router(periodicals.router)\napp.include_router(discogs.router)',
)
replace(
    "app/templates/settings.html",
    '        {% include "fragments/settings/komga.html" %}\n',
    '        {% include "fragments/settings/komga.html" %}\n        {% include "fragments/settings/discogs.html" %}\n',
)
replace(
    "app/templates/music_item.html",
    '        {% if release.musicbrainz_release_id %}',
    '''        <div hx-get="/api/discogs/items/{{ item.id }}/card" hx-trigger="load" hx-swap="outerHTML">
            <section class="mt-4 pt-4 border-t border-shelf-border text-sm text-shelf-muted">Loading optional Discogs pressing data…</section>
        </div>

        {% if release.musicbrainz_release_id %}''',
)
