"""Persistence for optional Discogs pressing enrichment.

The table is deliberately separate from ``music_releases``: MusicBrainz owns
canonical release identity, while Discogs records one user-selected pressing
and its replaceable collector metadata.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


_SCHEMA = """
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
"""


def ensure_schema(db) -> None:
    db.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def save_enrichment(db, item_id: int, release: dict) -> None:
    ensure_schema(db)
    try:
        release_id = int(release.get("discogs_release_id"))
    except (TypeError, ValueError):
        raise ValueError("a Discogs release ID is required") from None
    if release_id <= 0:
        raise ValueError("a Discogs release ID is required")

    # Enrichment is only valid for an item that has canonical music metadata.
    if not db.execute(
        "SELECT 1 FROM music_releases WHERE item_id = ?", (item_id,)
    ).fetchone():
        raise ValueError("MusicBrainz release must be attached first")

    db.execute(
        """INSERT INTO music_discogs (
               item_id, discogs_release_id, discogs_master_id, label,
               catalog_number, format_summary, genres_json, styles_json,
               notes, discogs_url, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET
               discogs_release_id = excluded.discogs_release_id,
               discogs_master_id = excluded.discogs_master_id,
               label = excluded.label,
               catalog_number = excluded.catalog_number,
               format_summary = excluded.format_summary,
               genres_json = excluded.genres_json,
               styles_json = excluded.styles_json,
               notes = excluded.notes,
               discogs_url = excluded.discogs_url,
               updated_at = excluded.updated_at""",
        (
            item_id,
            release_id,
            release.get("discogs_master_id"),
            release.get("label"),
            release.get("catalog_number"),
            release.get("format_summary"),
            json.dumps(release.get("genres") or [], ensure_ascii=False),
            json.dumps(release.get("styles") or [], ensure_ascii=False),
            release.get("notes"),
            release.get("discogs_url"),
            _now(),
        ),
    )
    db.execute("DELETE FROM music_discogs_identifiers WHERE item_id = ?", (item_id,))
    for identifier in release.get("identifiers") or []:
        if not isinstance(identifier, dict):
            continue
        kind = str(identifier.get("identifier_type") or "").strip()
        value = str(identifier.get("value") or "").strip()
        if not kind or not value:
            continue
        db.execute(
            """INSERT OR IGNORE INTO music_discogs_identifiers
               (item_id, identifier_type, value, description)
               VALUES (?, ?, ?, ?)""",
            (item_id, kind, value, str(identifier.get("description") or "").strip() or None),
        )


def get_enrichment(db, item_id: int) -> dict | None:
    ensure_schema(db)
    row = db.execute("SELECT * FROM music_discogs WHERE item_id = ?", (item_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    for source, target in (("genres_json", "genres"), ("styles_json", "styles")):
        try:
            result[target] = json.loads(result.get(source) or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            result[target] = []
    result["identifiers"] = [
        dict(row) for row in db.execute(
            "SELECT identifier_type, value, description FROM music_discogs_identifiers "
            "WHERE item_id = ? ORDER BY identifier_type COLLATE NOCASE, value COLLATE NOCASE",
            (item_id,),
        ).fetchall()
    ]
    return result


def clear_enrichment(db, item_id: int) -> bool:
    ensure_schema(db)
    cursor = db.execute("DELETE FROM music_discogs WHERE item_id = ?", (item_id,))
    # FK cascade handles identifiers, but explicit delete also supports test DBs
    # that deliberately run with foreign keys disabled.
    db.execute("DELETE FROM music_discogs_identifiers WHERE item_id = ?", (item_id,))
    return cursor.rowcount > 0
