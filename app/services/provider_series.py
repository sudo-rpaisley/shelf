"""Provider-owned metadata attached to Shelf series identities.

Shelf keeps series membership and manual metadata locally. Connected services
may enrich that identity with stable provider IDs and descriptive fields, but
provider sync never overwrites ``series_meta``: a synopsis entered in Shelf (or
already stored there by an existing enrichment path) remains authoritative.
"""

from __future__ import annotations

import json


_CREATE_PROVIDER_SERIES = """CREATE TABLE IF NOT EXISTS series_provider_meta (
    provider              TEXT NOT NULL,
    provider_series_id    TEXT NOT NULL,
    series_name           TEXT NOT NULL COLLATE NOCASE,
    kind                  TEXT NOT NULL DEFAULT 'series'
                          CHECK(kind IN ('series','franchise')),
    description           TEXT,
    publisher             TEXT,
    status                TEXT,
    age_rating            TEXT,
    total_items           INTEGER,
    total_duration_mins   INTEGER,
    metadata_json         TEXT,
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (provider, provider_series_id)
)"""

_PROVIDER_SERIES_MIGRATIONS = (
    (65, "Add connected-service series metadata", _CREATE_PROVIDER_SERIES),
    (
        66,
        "Index connected-service series names",
        "CREATE INDEX IF NOT EXISTS idx_series_provider_meta_name "
        "ON series_provider_meta(series_name COLLATE NOCASE)",
    ),
)


def _register_migrations() -> None:
    from app import database

    existing = {version for version, _description, _sql in database.MIGRATIONS}
    pending = tuple(m for m in _PROVIDER_SERIES_MIGRATIONS if m[0] not in existing)
    if pending:
        database.MIGRATIONS = tuple(database.MIGRATIONS) + pending


_register_migrations()


def ensure_schema(db) -> None:
    db.execute(_CREATE_PROVIDER_SERIES)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_series_provider_meta_name "
        "ON series_provider_meta(series_name COLLATE NOCASE)"
    )


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer(value) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def upsert(
    db,
    *,
    provider: str,
    provider_series_id: str,
    series_name: str,
    kind: str = "series",
    description=None,
    publisher=None,
    status=None,
    age_rating=None,
    total_items=None,
    total_duration_mins=None,
    metadata=None,
) -> None:
    """Store one provider's current view without touching Shelf-owned metadata."""
    ensure_schema(db)
    provider = _clean(provider)
    provider_series_id = _clean(provider_series_id)
    series_name = _clean(series_name)
    if not provider or not provider_series_id or not series_name:
        return
    if kind not in ("series", "franchise"):
        kind = "series"

    metadata_json = None
    if metadata is not None:
        try:
            metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            metadata_json = None

    db.execute(
        """INSERT INTO series_provider_meta (
               provider, provider_series_id, series_name, kind,
               description, publisher, status, age_rating,
               total_items, total_duration_mins, metadata_json, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(provider, provider_series_id) DO UPDATE SET
               series_name = excluded.series_name,
               kind = excluded.kind,
               description = excluded.description,
               publisher = excluded.publisher,
               status = excluded.status,
               age_rating = excluded.age_rating,
               total_items = excluded.total_items,
               total_duration_mins = excluded.total_duration_mins,
               metadata_json = excluded.metadata_json,
               updated_at = datetime('now')""",
        (
            provider,
            provider_series_id,
            series_name,
            kind,
            _clean(description),
            _clean(publisher),
            _clean(status),
            _clean(age_rating),
            _integer(total_items),
            _integer(total_duration_mins),
            metadata_json,
        ),
    )


def for_series(
    db,
    series_name: str,
    *,
    provider: str | None = None,
    provider_series_id: str | None = None,
) -> list[dict]:
    """Return provider records for a Shelf series, optionally by stable ID."""
    ensure_schema(db)
    clauses = ["series_name = ? COLLATE NOCASE"]
    params: list[object] = [series_name]
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if provider_series_id:
        clauses.append("provider_series_id = ?")
        params.append(str(provider_series_id))
    rows = db.execute(
        "SELECT provider, provider_series_id, series_name, kind, description, "
        "publisher, status, age_rating, total_items, total_duration_mins, "
        "metadata_json, updated_at FROM series_provider_meta WHERE "
        + " AND ".join(clauses)
        + " ORDER BY CASE provider WHEN 'komga' THEN 1 "
          "WHEN 'audiobookshelf' THEN 2 WHEN 'romm' THEN 3 ELSE 9 END, provider",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def preferred_description(rows: list[dict], media_type: str | None = None) -> tuple[str | None, str | None]:
    """Choose a provider fallback; callers still give Shelf metadata priority."""
    preferred = {
        "digital_comic": "komga",
        "digital_manga": "komga",
        "comic": "komga",
        "manga": "komga",
        "audiobook": "audiobookshelf",
        "ebook": "audiobookshelf",
        "video_game": "romm",
        "digital_game": "romm",
    }.get(media_type)
    ordered = sorted(rows, key=lambda row: row["provider"] != preferred) if preferred else rows
    for row in ordered:
        text = _clean(row.get("description"))
        if text:
            return text, row.get("provider")
    return None, None
