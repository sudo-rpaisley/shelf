"""Optional provider-series enrichment layered around existing media syncs.

The core Audiobookshelf, Komga and RomM importers remain responsible for item
cataloguing. This module adds series/franchise metadata without making a richer
provider endpoint a hard dependency of a successful item sync.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import quote

import httpx

from app.database import get_db
from app.services import provider_series
from app.services import series_memberships as series_memberships_svc

def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def _minutes_from_seconds(value) -> int | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return int(seconds / 60)


async def sync_audiobookshelf_series(abs_url: str, token: str) -> int:
    """Refresh series identities/totals for ABS libraries represented in Shelf."""
    with get_db() as db:
        library_ids = [
            str(row["abs_library_id"])
            for row in db.execute(
                "SELECT DISTINCT abs_library_id FROM items_live "
                "WHERE abs_id IS NOT NULL AND abs_library_id IS NOT NULL"
            ).fetchall()
            if row["abs_library_id"]
        ]
    if not library_ids:
        return 0

    saved = 0
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        for library_id in library_ids:
            try:
                response = await client.get(
                    f"{abs_url}/api/libraries/{quote(library_id, safe='')}/series",
                    headers=headers,
                    params={"limit": 10000, "page": 0},
                )
            except Exception:
                # Enrichment must not turn a healthy item sync into a failure.
                continue
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            rows = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                continue
            with get_db() as db:
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    provider_id = _clean(row.get("id"))
                    name = _clean(row.get("name"))
                    if not provider_id or not name:
                        continue
                    books = row.get("books")
                    total_items = len(books) if isinstance(books, list) else _int(row.get("numBooks"))
                    provider_series.upsert(
                        db,
                        provider="audiobookshelf",
                        provider_series_id=provider_id,
                        series_name=name,
                        total_items=total_items,
                        total_duration_mins=_minutes_from_seconds(row.get("totalDuration")),
                        metadata={"library_id": library_id},
                    )
                    saved += 1
    return saved


async def _fetch_komga_series_one(
    client: httpx.AsyncClient,
    komga_url: str,
    api_key: str,
    series_id: str,
) -> dict | None:
    try:
        response = await client.get(
            f"{komga_url}/api/v1/series/{quote(series_id, safe='')}",
            headers={"X-API-Key": api_key, "Accept": "application/json"},
        )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def sync_komga_series(komga_url: str, api_key: str) -> int:
    """Refresh Komga series summaries and descriptive metadata by stable ID."""
    with get_db() as db:
        ids = [
            str(row["komga_series_id"])
            for row in db.execute(
                "SELECT DISTINCT kr.series_id AS komga_series_id FROM komga_records kr "
                "JOIN items_live i ON i.id = kr.item_id WHERE kr.series_id IS NOT NULL "
                "UNION SELECT DISTINCT komga_series_id FROM items_live "
                "WHERE komga_id IS NOT NULL AND komga_series_id IS NOT NULL"
            ).fetchall()
            if row["komga_series_id"]
        ]
    if not ids:
        return 0

    semaphore = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=30) as client:
        async def fetch(series_id: str):
            async with semaphore:
                return series_id, await _fetch_komga_series_one(
                    client, komga_url, api_key, series_id
                )

        fetched = await asyncio.gather(*(fetch(series_id) for series_id in ids))

    saved = 0
    with get_db() as db:
        for series_id, data in fetched:
            if not data:
                continue
            metadata = data.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            name = _clean(data.get("name") or metadata.get("title"))
            if not name:
                # Older/newer Komga shapes can omit the top-level name. The
                # local item relation is still a safe stable-ID fallback.
                local = db.execute(
                    "SELECT DISTINCT i.series_name FROM items_live i WHERE i.series_name IS NOT NULL "
                    "AND (i.komga_series_id = ? OR EXISTS (SELECT 1 FROM komga_records kr "
                    "WHERE kr.item_id = i.id AND kr.series_id = ?))",
                    (series_id, series_id),
                ).fetchall()
                name = _clean(local[0]["series_name"]) if len(local) == 1 else None
            if not name:
                continue
            total_items = (
                _int(metadata.get("totalBookCount"))
                or _int(data.get("bookCount"))
                or _int(data.get("booksCount"))
            )
            provider_series.upsert(
                db,
                provider="komga",
                provider_series_id=series_id,
                series_name=name,
                description=metadata.get("summary") or data.get("summary"),
                publisher=metadata.get("publisher"),
                status=metadata.get("status"),
                age_rating=metadata.get("ageRating"),
                total_items=total_items,
                metadata={
                    "library_id": data.get("libraryId"),
                    "reading_direction": metadata.get("readingDirection"),
                    "language": metadata.get("language"),
                },
            )
            saved += 1
    return saved


def _named_provider_entries(value, *, kind: str, prefix: str) -> list[dict]:
    """Normalise RomM/IGDB collection and franchise shapes defensively."""
    if value in (None, "", []):
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    out: list[dict] = []
    seen: set[str] = set()
    for entry in values:
        if isinstance(entry, dict):
            name = _clean(entry.get("name") or entry.get("title") or entry.get("value"))
            raw_id = entry.get("id") or entry.get("igdb_id") or entry.get("slug")
            metadata = entry
        else:
            name = _clean(entry)
            raw_id = None
            metadata = {"value": entry}
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        if raw_id not in (None, ""):
            provider_id = f"{prefix}:{raw_id}"
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", key).strip("-") or "unnamed"
            provider_id = f"{prefix}:name:{slug}"
        out.append({
            "name": name,
            "provider_id": provider_id,
            "kind": kind,
            "metadata": metadata,
        })
    return out


def romm_groupings(metadata: dict) -> list[dict]:
    """Return unordered game collections first, then broader franchises."""
    if not isinstance(metadata, dict):
        return []
    rows = _named_provider_entries(
        metadata.get("collections"), kind="series", prefix="collection"
    )
    franchise_value = metadata.get("franchises")
    if not franchise_value:
        franchise_value = metadata.get("franchise") or metadata.get("family")
    rows.extend(
        _named_provider_entries(
            franchise_value, kind="franchise", prefix="franchise"
        )
    )
    return rows


def enrich_romm_batch(db, roms: list[dict]) -> int:
    """Attach RomM/IGDB collections and franchises as unordered memberships."""
    saved = 0
    for rom in roms:
        if not isinstance(rom, dict):
            continue
        romm_id = _clean(rom.get("id"))
        metadata = rom.get("metadatum") or {}
        groupings = romm_groupings(metadata)
        if not romm_id or not groupings:
            continue
        items = db.execute(
            "SELECT i.id FROM items_live i JOIN romm_records rr ON rr.item_id = i.id WHERE rr.romm_id = ? "
            "UNION SELECT id FROM items_live WHERE romm_id = ?", (romm_id, romm_id),
        ).fetchall()
        item = items[0] if len(items) == 1 else None
        if not item:
            continue
        # No position is supplied. Continue/Up Next intentionally considers
        # only ordered memberships, so franchises can group games without
        # inventing a play order.
        series_memberships_svc.add_metadata_memberships(
            db,
            item["id"],
            [{"name": row["name"], "position": None} for row in groupings],
        )
        for row in groupings:
            provider_series.upsert(
                db,
                provider="romm",
                provider_series_id=row["provider_id"],
                series_name=row["name"],
                kind=row["kind"],
                metadata=row["metadata"],
            )
            saved += 1
    return saved


def install() -> None:
    """Explicit parent composition: callbacks, never import-time monkey patches.

    app.main calls this once after composing routers. Repeating the composition
    is harmless and never wraps an already wrapped writer/scheduler.
    """
    from app.services import audiobookshelf, komga_sync, romm_records

    audiobookshelf.set_series_enricher(sync_audiobookshelf_series)
    komga_sync.set_series_enricher(sync_komga_series)
    romm_records.set_series_enricher(enrich_romm_batch)
