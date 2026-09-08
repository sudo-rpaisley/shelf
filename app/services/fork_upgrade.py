"""One-time bridge from the pre-0.37 sudo-rpaisley fork schema.

The old fork and upstream independently used migration numbers 24 onward for
unrelated work. A database that has run the old fork therefore needs its data
and migration ledger translated before it can safely follow upstream 0.37.

This module deliberately owns no canonical Shelf table definitions. Where a
current table must be recreated, the definition is read from app.database so
schema ownership remains centralised there. The bridge is split into prepare
and finish phases so startup can make legacy tables compatible before
MIGRATION_TABLES runs, then project provider identities after those current
tables exist.
"""

from __future__ import annotations

import json
import sqlite3


_MARKER = "sudo_fork_037_schema_bridge_v1"
_LEDGER_KEY = "sudo_fork_037_migration_ledger_v1"
_OLD_V24_DESCRIPTION = "Add komga_id column"
_UPSTREAM_MAX_VERSION = 31


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(db, table):
        return set()
    return {
        row["name"] if hasattr(row, "keys") else row[1]
        for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _setting(db: sqlite3.Connection, key: str) -> str | None:
    if not _table_exists(db, "settings"):
        return None
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _marker_set(db: sqlite3.Connection) -> bool:
    return _setting(db, _MARKER) == "1"


def is_pre_037_fork_database(db: sqlite3.Connection) -> bool:
    """Return whether this database carries the old fork migration namespace."""
    if _marker_set(db):
        return False
    if not _table_exists(db, "schema_version"):
        return False

    row = db.execute(
        "SELECT description FROM schema_version WHERE version = 24"
    ).fetchone()
    if row and row["description"] == _OLD_V24_DESCRIPTION:
        return True

    # A prepared-but-not-finished retry still belongs to this bridge even if a
    # human has edited the v24 description in between runs.
    if _setting(db, _LEDGER_KEY):
        return True

    # Very early development databases may have acquired the additive holdings
    # schema before their migration description was written. The FK target is
    # an equally strong fingerprint and cannot occur in upstream Shelf.
    if _table_exists(db, "location_nodes") and _table_exists(db, "item_copies"):
        for fk in db.execute("PRAGMA foreign_key_list(item_copies)").fetchall():
            if fk["from"] == "location_id" and fk["table"] == "location_nodes":
                return True
    return False


def _archive_legacy_migration_ledger(db: sqlite3.Connection) -> None:
    """Preserve the fork ledger and free future upstream version numbers."""
    if _setting(db, _LEDGER_KEY):
        # A previous prepare may have committed before a later startup failure.
        db.execute(
            "DELETE FROM schema_version WHERE version > ?",
            (_UPSTREAM_MAX_VERSION,),
        )
        return

    columns = _columns(db, "schema_version")
    applied_expr = "applied_at" if "applied_at" in columns else "NULL AS applied_at"
    rows = db.execute(
        f"SELECT version, description, {applied_expr} FROM schema_version "
        "WHERE version >= 24 ORDER BY version"
    ).fetchall()
    payload = [
        {
            "version": int(row["version"]),
            "description": row["description"],
            "applied_at": row["applied_at"],
        }
        for row in rows
    ]
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_LEDGER_KEY, json.dumps(payload, separators=(",", ":"))),
    )
    # Rows above upstream's current maximum belong to the fork namespace. If
    # left in the central ledger they would cause future upstream migrations to
    # be skipped merely because the number had been reused years earlier.
    db.execute(
        "DELETE FROM schema_version WHERE version > ?",
        (_UPSTREAM_MAX_VERSION,),
    )


def _ensure_upstream_legacy_mapping_table(db: sqlite3.Connection) -> None:
    """Replay current migration 24 from the canonical database definition."""
    from app import database

    sql = next(sql for version, _description, sql in database.MIGRATIONS if version == 24)
    db.execute(sql)


def _ensure_location_columns(db: sqlite3.Connection) -> None:
    columns = _columns(db, "locations")
    if "parent_id" not in columns:
        db.execute(
            "ALTER TABLE locations ADD COLUMN parent_id INTEGER "
            "REFERENCES locations(id) ON DELETE RESTRICT"
        )
    columns = _columns(db, "locations")
    if "label" not in columns:
        db.execute("ALTER TABLE locations ADD COLUMN label TEXT DEFAULT NULL")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_locations_parent "
        "ON locations(parent_id, sort_order)"
    )
    db.execute("UPDATE locations SET label = name WHERE label IS NULL")


def _migrate_location_nodes(db: sqlite3.Connection) -> dict[int, int]:
    """Project the fork's location_nodes tree into upstream locations."""
    _ensure_location_columns(db)
    if not _table_exists(db, "location_nodes"):
        return {}

    rows = db.execute(
        "SELECT id, parent_id, name, sort_order, legacy_location_id "
        "FROM location_nodes ORDER BY id"
    ).fetchall()
    pending = {int(row["id"]): row for row in rows}
    mapping: dict[int, int] = {}
    full_names: dict[int, str] = {}

    while pending:
        progressed = False
        for old_id, node in list(pending.items()):
            old_parent = node["parent_id"]
            if old_parent is not None and int(old_parent) not in mapping:
                continue

            label = str(node["name"] or "").strip()
            if not label:
                raise RuntimeError(f"Legacy location node {old_id} has a blank name")
            parent_id = mapping.get(int(old_parent)) if old_parent is not None else None
            full_name = (
                f"{full_names[int(old_parent)]} / {label}"
                if old_parent is not None
                else label
            )

            target_id = None
            legacy_id = node["legacy_location_id"]
            if legacy_id is not None:
                legacy_row = db.execute(
                    "SELECT id FROM locations WHERE id = ?", (int(legacy_id),)
                ).fetchone()
                if legacy_row:
                    target_id = int(legacy_row["id"])

            if target_id is None:
                existing = db.execute(
                    "SELECT id FROM locations WHERE name = ? COLLATE NOCASE",
                    (full_name,),
                ).fetchone()
                if existing:
                    target_id = int(existing["id"])

            if target_id is None:
                target_id = int(
                    db.execute(
                        "INSERT INTO locations (name, label, parent_id, sort_order) "
                        "VALUES (?, ?, ?, ?)",
                        (full_name, label, parent_id, int(node["sort_order"] or 0)),
                    ).lastrowid
                )
            else:
                db.execute(
                    "UPDATE locations SET name = ?, label = ?, parent_id = ?, "
                    "sort_order = ? WHERE id = ?",
                    (
                        full_name,
                        label,
                        parent_id,
                        int(node["sort_order"] or 0),
                        target_id,
                    ),
                )

            mapping[old_id] = target_id
            full_names[old_id] = full_name
            del pending[old_id]
            progressed = True

        if not progressed:
            unresolved = ", ".join(str(node_id) for node_id in sorted(pending))
            raise RuntimeError(
                "Legacy location tree contains an orphan or cycle; unresolved nodes: "
                + unresolved
            )

    return mapping


def _item_copy_location_target(db: sqlite3.Connection) -> str | None:
    if not _table_exists(db, "item_copies"):
        return None
    for fk in db.execute("PRAGMA foreign_key_list(item_copies)").fetchall():
        if fk["from"] == "location_id":
            return str(fk["table"])
    return None


def _canonical_table_statement(table: str) -> str:
    """Read one current table statement out of database.MIGRATION_TABLES."""
    from app import database

    marker = ("CREATE " + "TABLE IF NOT EXISTS " + table).upper()
    buffer = ""
    for line in database.MIGRATION_TABLES.splitlines(keepends=True):
        buffer += line
        if not sqlite3.complete_statement(buffer):
            continue
        statement = buffer.strip()
        buffer = ""
        significant = "\n".join(
            line for line in statement.splitlines()
            if not line.lstrip().startswith("--")
        ).strip()
        if significant.upper().startswith(marker):
            return significant.rstrip(";")
    raise RuntimeError(f"Current schema definition for {table} was not found")


def _create_current_item_copies(db: sqlite3.Connection) -> None:
    db.execute(_canonical_table_statement("item_copies"))


def _ensure_position_order(db: sqlite3.Connection) -> None:
    if "position_order" not in _columns(db, "item_copies"):
        db.execute(
            "ALTER TABLE item_copies ADD COLUMN position_order INTEGER DEFAULT NULL"
        )


def _create_item_copy_indexes(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_item_copies_item "
        "ON item_copies(item_id, copy_number)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_item_copies_location "
        "ON item_copies(location_id, item_id)"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_item_copies_one_primary "
        "ON item_copies(item_id) WHERE is_primary = 1"
    )


def _rebuild_legacy_item_copies(
    db: sqlite3.Connection,
    location_mapping: dict[int, int],
) -> None:
    rows = [dict(row) for row in db.execute("SELECT * FROM item_copies ORDER BY id")]

    duplicate_primaries = db.execute(
        "SELECT item_id FROM item_copies WHERE is_primary = 1 "
        "GROUP BY item_id HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if duplicate_primaries:
        raise RuntimeError(
            "Legacy item has more than one primary physical copy: "
            f"item {duplicate_primaries['item_id']}"
        )

    for row in rows:
        price = row.get("acquisition_price")
        if price is not None and float(price) < 0:
            raise RuntimeError(
                f"Legacy copy {row['id']} has a negative acquisition price"
            )
        old_location = row.get("location_id")
        if old_location is not None and int(old_location) not in location_mapping:
            raise RuntimeError(
                f"Legacy copy {row['id']} points to unknown location node {old_location}"
            )

    db.execute("ALTER TABLE item_copies RENAME TO item_copies_pre037")
    _create_current_item_copies(db)
    _ensure_position_order(db)

    for row in rows:
        old_location = row.get("location_id")
        new_location = (
            location_mapping[int(old_location)] if old_location is not None else None
        )
        db.execute(
            """INSERT INTO item_copies (
                id, item_id, copy_number, location_id, condition, notes,
                acquired_date, acquisition_source, acquisition_price, provenance,
                copy_barcode, is_primary, created_at, updated_at, position_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                row["item_id"],
                row["copy_number"],
                new_location,
                row.get("condition"),
                row.get("notes"),
                row.get("acquired_date"),
                row.get("acquisition_source"),
                row.get("acquisition_price"),
                row.get("provenance"),
                row.get("copy_barcode"),
                row.get("is_primary", 0),
                row.get("created_at"),
                row.get("updated_at"),
                row.get("position_order"),
            ),
        )

    db.execute("DROP TABLE item_copies_pre037")
    _create_item_copy_indexes(db)


def _sync_catalogue_locations_from_primary_copies(db: sqlite3.Connection) -> None:
    from app.services.item_write import update_item_fields

    rows = db.execute(
        "SELECT item_id, location_id FROM item_copies "
        "WHERE is_primary = 1 ORDER BY item_id, id"
    ).fetchall()
    seen: set[int] = set()
    for row in rows:
        item_id = int(row["item_id"])
        if item_id in seen:
            raise RuntimeError(f"Item {item_id} has more than one primary copy")
        seen.add(item_id)
        update_item_fields(db, item_id, {"location_id": row["location_id"]})


def _ensure_upstream_item_copies(
    db: sqlite3.Connection,
    location_mapping: dict[int, int],
) -> None:
    target = _item_copy_location_target(db)
    if target == "location_nodes":
        _rebuild_legacy_item_copies(db, location_mapping)
    elif not _table_exists(db, "item_copies"):
        _create_current_item_copies(db)
        _ensure_position_order(db)
        _create_item_copy_indexes(db)
    else:
        _ensure_position_order(db)
        _create_item_copy_indexes(db)

    # Reproduce upstream migration 26 for catalogue rows that never passed
    # through the fork's holdings projection.
    db.execute(
        """INSERT INTO item_copies (item_id, copy_number, location_id, is_primary)
           SELECT i.id, 1, i.location_id, 1 FROM items i
           WHERE i.owned = 1 AND i.location_id IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM item_copies c WHERE c.item_id = i.id)"""
    )
    _sync_catalogue_locations_from_primary_copies(db)


def _ensure_periodical_columns(db: sqlite3.Connection) -> None:
    if not _table_exists(db, "periodical_issues"):
        return
    columns = _columns(db, "periodical_issues")
    if "barcode_supplement" not in columns:
        db.execute("ALTER TABLE periodical_issues ADD COLUMN barcode_supplement TEXT")
    columns = _columns(db, "periodical_issues")
    if "barcode_ean" not in columns:
        db.execute("ALTER TABLE periodical_issues ADD COLUMN barcode_ean TEXT")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_periodical_issues_barcode "
        "ON periodical_issues(barcode_ean, barcode_supplement)"
    )


def _komga_kind_map(db: sqlite3.Connection) -> dict[str, str]:
    row = db.execute(
        "SELECT value FROM settings WHERE key = 'komga_library_media_types'"
    ).fetchone()
    if not row or not row["value"]:
        return {}
    try:
        raw = json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for library_id, media_type in raw.items():
        if media_type == "digital_manga":
            out[str(library_id)] = "manga"
        elif media_type == "digital_comic":
            out[str(library_id)] = "comic"
    return out


def _holding_library_id(
    db: sqlite3.Connection, item_id: int, provider: str
) -> str | None:
    if not _table_exists(db, "digital_holdings"):
        return None
    row = db.execute(
        "SELECT library_id FROM digital_holdings "
        "WHERE item_id = ? AND provider = ? ORDER BY id LIMIT 1",
        (item_id, provider),
    ).fetchone()
    if not row or row["library_id"] is None:
        return None
    value = str(row["library_id"]).strip()
    return value or None


def _require_provider_targets(db: sqlite3.Connection) -> None:
    item_columns = _columns(db, "items")
    if "romm_id" in item_columns:
        has_romm = db.execute(
            "SELECT 1 FROM items WHERE romm_id IS NOT NULL AND TRIM(romm_id) != '' LIMIT 1"
        ).fetchone()
        if has_romm and not _table_exists(db, "romm_records"):
            raise RuntimeError("RomM target table is not installed yet")
    if "komga_id" in item_columns:
        has_komga = db.execute(
            "SELECT 1 FROM items WHERE komga_id IS NOT NULL AND TRIM(komga_id) != '' LIMIT 1"
        ).fetchone()
        if has_komga and not _table_exists(db, "komga_records"):
            raise RuntimeError("Komga target table is not installed yet")


def _project_provider_records(db: sqlite3.Connection) -> None:
    from app.services.item_write import update_items_fields

    item_columns = _columns(db, "items")

    if {"romm_id", "romm_platform_id"}.issubset(item_columns) and _table_exists(
        db, "romm_records"
    ):
        select_platform = ", platform" if "platform" in item_columns else ""
        rows = db.execute(
            "SELECT id, romm_id, romm_platform_id" + select_platform + " FROM items "
            "WHERE romm_id IS NOT NULL AND TRIM(romm_id) != ''"
        ).fetchall()
        for row in rows:
            item_id = int(row["id"])
            platform_id = str(row["romm_platform_id"] or "").strip()
            if not platform_id:
                platform_id = _holding_library_id(db, item_id, "romm") or ""
            if not platform_id and "platform" in row.keys():
                platform_id = str(row["platform"] or "").strip()
            if not platform_id:
                raise RuntimeError(
                    f"RomM item {item_id} has no recoverable platform identifier"
                )
            db.execute(
                """INSERT INTO romm_records (romm_id, item_id, platform_id, updated_at)
                   VALUES (?, ?, ?, datetime('now'))
                   ON CONFLICT(romm_id) DO UPDATE SET
                       item_id = excluded.item_id,
                       platform_id = excluded.platform_id,
                       updated_at = datetime('now')""",
                (str(row["romm_id"]), item_id, platform_id),
            )

    if {"komga_id", "komga_library_id", "komga_series_id"}.issubset(
        item_columns
    ) and _table_exists(db, "komga_records"):
        configured_kinds = _komga_kind_map(db)
        rows = db.execute(
            "SELECT id, media_type, komga_id, komga_library_id, komga_series_id "
            "FROM items WHERE komga_id IS NOT NULL AND TRIM(komga_id) != ''"
        ).fetchall()
        for row in rows:
            item_id = int(row["id"])
            library_id = str(row["komga_library_id"] or "").strip()
            if not library_id:
                library_id = _holding_library_id(db, item_id, "komga") or ""
            kind = configured_kinds.get(library_id)
            if kind is None:
                kind = "manga" if row["media_type"] in {"manga", "digital_manga"} else "comic"
            db.execute(
                """INSERT INTO komga_records
                   (komga_id, item_id, library_id, series_id, kind, updated_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(komga_id) DO UPDATE SET
                       item_id = excluded.item_id,
                       library_id = excluded.library_id,
                       series_id = excluded.series_id,
                       kind = excluded.kind,
                       updated_at = datetime('now')""",
                (
                    str(row["komga_id"]),
                    item_id,
                    library_id,
                    str(row["komga_series_id"]) if row["komga_series_id"] is not None else None,
                    kind,
                ),
            )

        comic_ids = [
            int(row["id"])
            for row in db.execute(
                "SELECT id FROM items WHERE media_type = 'digital_comic'"
            ).fetchall()
        ]
        manga_ids = [
            int(row["id"])
            for row in db.execute(
                "SELECT id FROM items WHERE media_type = 'digital_manga'"
            ).fetchall()
        ]
        update_items_fields(db, comic_ids, {"media_type": "comic"})
        update_items_fields(db, manga_ids, {"media_type": "manga"})


def _align_upstream_migration_descriptions(db: sqlite3.Connection) -> None:
    """Make the colliding 24-31 version rows truthful after conversion."""
    descriptions = {
        24: "Add confirmed legacy book barcode mappings",
        25: "Add first-class physical copies",
        26: "Backfill primary copies from legacy locations",
        27: "Add parent relationship to locations",
        28: "Add node label to locations",
        29: "Backfill flat locations as hierarchy roots",
        30: "Index hierarchical location children",
        31: "Add physical copy shelf position",
    }
    for version, description in descriptions.items():
        db.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?) "
            "ON CONFLICT(version) DO UPDATE SET description = excluded.description",
            (version, description),
        )


def prepare_pre_037_fork(db: sqlite3.Connection) -> bool:
    """Make old fork structures safe for Shelf's normal migration-table pass."""
    if not is_pre_037_fork_database(db):
        return False

    db.execute("SAVEPOINT sudo_fork_037_prepare")
    try:
        _archive_legacy_migration_ledger(db)
        _ensure_upstream_legacy_mapping_table(db)
        location_mapping = _migrate_location_nodes(db)
        _ensure_upstream_item_copies(db, location_mapping)
        _ensure_periodical_columns(db)
        db.execute("RELEASE SAVEPOINT sudo_fork_037_prepare")
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT sudo_fork_037_prepare")
        db.execute("RELEASE SAVEPOINT sudo_fork_037_prepare")
        raise
    return True


def finish_pre_037_fork(db: sqlite3.Connection) -> list[str]:
    """Project provider identities and finish the migration-ledger translation."""
    if not is_pre_037_fork_database(db):
        return []

    db.execute("SAVEPOINT sudo_fork_037_finish")
    try:
        _require_provider_targets(db)
        _project_provider_records(db)
        _align_upstream_migration_descriptions(db)
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_MARKER,),
        )
        db.execute("RELEASE SAVEPOINT sudo_fork_037_finish")
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT sudo_fork_037_finish")
        db.execute("RELEASE SAVEPOINT sudo_fork_037_finish")
        raise

    return ["Upgraded pre-0.37 fork schema to Shelf 0.37 compatibility model"]


def upgrade_pre_037_fork(db: sqlite3.Connection) -> list[str]:
    """Direct helper for already-initialised databases and focused tests."""
    if not prepare_pre_037_fork(db):
        return []
    return finish_pre_037_fork(db)
