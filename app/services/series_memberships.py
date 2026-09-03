"""Shared helpers for Shelf's many-to-many item series memberships.

``items.series_name`` / ``series_position`` remain the compatibility primary
series because older imports, filters and integrations still write that pair.
``item_series`` is the richer representation used by the item-detail rows and
the global Series page.  These helpers keep the two representations in sync
and let metadata providers add additional series without overwriting
user-managed secondary memberships.
"""

from __future__ import annotations

from collections.abc import Mapping


MAX_SERIES_NAME = 1000

_ITEM_SERIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS item_series (
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    series_name  TEXT NOT NULL COLLATE NOCASE,
    position     REAL,
    is_primary   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_id, series_name)
)
"""


def ensure_schema(db) -> None:
    db.execute(_ITEM_SERIES_SCHEMA)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_item_series_name "
        "ON item_series(series_name COLLATE NOCASE)"
    )


def _position(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalise(memberships) -> list[dict]:
    """Normalise provider/manual series values into ``name``/``position`` rows.

    Providers are not consistent about shape: Hardcover uses ``name`` and
    ``position``; Audiobookshelf uses ``series`` and ``sequence``; some call
    sites still only know the legacy string.  Unknown or malformed rows are
    ignored rather than preventing the parent item from being saved.
    """
    if memberships is None:
        return []
    if isinstance(memberships, (str, Mapping)):
        memberships = [memberships]

    rows: list[dict] = []
    by_name: dict[str, dict] = {}
    for raw in memberships:
        if isinstance(raw, str):
            name = raw.strip()
            position = None
        elif isinstance(raw, Mapping):
            name = str(
                raw.get("name")
                or raw.get("series_name")
                or raw.get("series")
                or ""
            ).strip()
            position = _position(
                raw.get("position")
                if raw.get("position") not in (None, "")
                else raw.get("sequence")
            )
        else:
            continue

        if not name or len(name) > MAX_SERIES_NAME:
            continue
        key = name.casefold()
        existing = by_name.get(key)
        if existing:
            if existing["position"] is None and position is not None:
                existing["position"] = position
            continue
        row = {"name": name, "position": position}
        by_name[key] = row
        rows.append(row)
    return rows


def metadata_memberships(metadata: Mapping | None) -> list[dict]:
    """Return every explicit series carried by a metadata dict.

    The legacy pair is always represented, even when a newer provider also
    supplied ``series_memberships``. This keeps callers that only populate the
    old fields fully compatible while allowing richer providers to supply more
    than one series.
    """
    if not metadata:
        return []
    rows = normalise(metadata.get("series_memberships"))
    legacy_name = str(metadata.get("series_name") or "").strip()
    if legacy_name:
        legacy = {
            "name": legacy_name,
            "position": _position(metadata.get("series_position")),
        }
        existing = next((r for r in rows if r["name"].casefold() == legacy_name.casefold()), None)
        if existing:
            if existing["position"] is None:
                existing["position"] = legacy["position"]
            rows.remove(existing)
            rows.insert(0, existing)
        else:
            rows.insert(0, legacy)
    return rows


def sync_legacy_item(db, item_id: int) -> bool:
    """Reconcile one item's compatibility primary into ``item_series``.

    Returns ``True`` only when a membership row had to be created. Secondary
    memberships are never removed.
    """
    ensure_schema(db)
    item = db.execute(
        "SELECT series_name, series_position FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if not item:
        return False

    name = str(item["series_name"] or "").strip()
    if name:
        db.execute(
            "DELETE FROM item_series WHERE item_id = ? AND is_primary = 1 "
            "AND series_name != ? COLLATE NOCASE",
            (item_id, name),
        )
    else:
        db.execute(
            "DELETE FROM item_series WHERE item_id = ? AND is_primary = 1",
            (item_id,),
        )
        return False

    db.execute("UPDATE item_series SET is_primary = 0 WHERE item_id = ?", (item_id,))

    existed = db.execute(
        "SELECT 1 FROM item_series WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
        (item_id, name),
    ).fetchone()
    db.execute(
        "INSERT INTO item_series (item_id, series_name, position, is_primary) "
        "VALUES (?, ?, ?, 1) "
        "ON CONFLICT(item_id, series_name) DO UPDATE SET "
        "position = excluded.position, is_primary = 1",
        (item_id, name, item["series_position"]),
    )
    return not bool(existed)


def sync_all_legacy(db) -> dict:
    """Reconcile every legacy series pair and return a compact summary."""
    ensure_schema(db)
    before = db.execute("SELECT COUNT(*) FROM item_series").fetchone()[0]

    # The item row is authoritative for *which* membership is primary. Drop
    # a stale former primary when that compatibility field changed, preserve true
    # secondary memberships, then upsert every current legacy pair.
    db.execute(
        "DELETE FROM item_series WHERE is_primary = 1 AND NOT EXISTS ("
        "SELECT 1 FROM items i WHERE i.id = item_series.item_id "
        "AND i.series_name IS NOT NULL AND TRIM(i.series_name) != '' "
        "AND TRIM(i.series_name) = item_series.series_name COLLATE NOCASE"
        ")"
    )
    db.execute("UPDATE item_series SET is_primary = 0")
    db.execute(
        "INSERT INTO item_series (item_id, series_name, position, is_primary) "
        "SELECT id, TRIM(series_name), series_position, 1 FROM items "
        "WHERE series_name IS NOT NULL AND TRIM(series_name) != '' "
        "ON CONFLICT(item_id, series_name) DO UPDATE SET "
        "position = excluded.position, is_primary = 1"
    )

    after = db.execute("SELECT COUNT(*) FROM item_series").fetchone()[0]
    series_count = db.execute(
        "SELECT COUNT(*) FROM (SELECT series_name FROM item_series "
        "GROUP BY series_name COLLATE NOCASE)"
    ).fetchone()[0]
    item_count = db.execute(
        "SELECT COUNT(DISTINCT item_id) FROM item_series"
    ).fetchone()[0]
    return {
        "created": max(0, after - before),
        "memberships": after,
        "series": series_count,
        "items": item_count,
    }


def add_metadata_memberships(db, item_id: int, memberships) -> int:
    """Add explicit provider series to an item without deleting manual ones.

    If the item has no compatibility primary yet, the first provider series
    becomes primary. Existing non-null positions are preserved so a metadata
    refresh cannot silently replace a sequence the user has edited by hand.
    Returns the number of newly created membership rows.
    """
    rows = normalise(memberships)
    ensure_schema(db)
    item = db.execute(
        "SELECT series_name, series_position FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if not item:
        return 0

    legacy_name = str(item["series_name"] or "").strip()
    legacy_position = item["series_position"]
    if not legacy_name and rows:
        legacy_name = rows[0]["name"]
        legacy_position = rows[0]["position"]
        db.execute(
            "UPDATE items SET series_name = ?, series_position = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (legacy_name, legacy_position, item_id),
        )

    created = int(sync_legacy_item(db, item_id))
    for row in rows:
        name = row["name"]
        incoming_position = row["position"]
        existing = db.execute(
            "SELECT position, is_primary FROM item_series "
            "WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
            (item_id, name),
        ).fetchone()
        is_primary = bool(legacy_name and name.casefold() == legacy_name.casefold())

        if existing:
            position = existing["position"] if existing["position"] is not None else incoming_position
            db.execute(
                "UPDATE item_series SET position = ?, is_primary = CASE "
                "WHEN ? THEN 1 ELSE is_primary END "
                "WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
                (position, int(is_primary), item_id, name),
            )
        else:
            db.execute(
                "INSERT INTO item_series (item_id, series_name, position, is_primary) "
                "VALUES (?, ?, ?, ?)",
                (item_id, name, incoming_position, int(is_primary)),
            )
            created += 1

        if is_primary and legacy_position is None and incoming_position is not None:
            legacy_position = incoming_position
            db.execute(
                "UPDATE items SET series_position = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (incoming_position, item_id),
            )
            db.execute(
                "UPDATE item_series SET position = ? WHERE item_id = ? "
                "AND series_name = ? COLLATE NOCASE",
                (incoming_position, item_id, name),
            )

    return created
