"""The single exact-ID RomM writer, coordinating all compatibility projections."""

from __future__ import annotations

from typing import Any

from app.services import holdings, libraries
from app.services.catalogue_security import require_item_role
from app.services.item_write import _write_scope, insert_item, trash_item, update_item_fields

_series_enricher = None


def set_series_enricher(callback) -> None:
    global _series_enricher
    _series_enricher = callback


class RomMPersistenceError(ValueError):
    """A RomM candidate cannot be represented safely."""


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def ensure_platform(db, slug: str, name: str | None = None) -> str:
    clean_slug = _clean(slug)
    if not clean_slug:
        raise RomMPersistenceError("RomM candidate has no evidenced platform")
    row = db.execute("SELECT slug FROM game_platforms WHERE slug = ?", (clean_slug,)).fetchone()
    if row is None:
        if not _clean(name):
            raise RomMPersistenceError("Unknown RomM platform needs an explicit provider name")
        db.execute("INSERT INTO game_platforms (slug, name) VALUES (?, ?)", (clean_slug, _clean(name)))
    return clean_slug


def _attachment(db, romm_id: str):
    # Physical identity reads include Trash/hidden rows to prevent duplicate
    # creation. All normal readers still use the actor-aware live view.
    rows = db.execute(
        "SELECT id FROM items WHERE romm_id = ? UNION "
        "SELECT item_id FROM romm_records WHERE romm_id = ? UNION "
        "SELECT item_id FROM digital_holdings WHERE provider = 'romm' AND external_id = ?",
        (romm_id, romm_id, romm_id),
    ).fetchall()
    if len(rows) > 1:
        raise RomMPersistenceError("RomM identity has conflicting catalogue attachments")
    if not rows:
        return None
    item = db.execute("SELECT * FROM items WHERE id = ?", (rows[0][0],)).fetchone()
    if item is None:
        raise RomMPersistenceError("RomM identity has an incomplete catalogue attachment")
    if item["deleted_at"] is None:
        require_item_role(db, item["id"], "editor")
    return item


def _existing_record(db, romm_id: str):
    return db.execute(
        "SELECT rr.*, i.source, i.deleted_at FROM romm_records rr JOIN items i ON i.id = rr.item_id "
        "WHERE rr.romm_id = ?", (romm_id,),
    ).fetchone()


def persist_candidate(db, candidate: dict[str, Any]) -> dict[str, Any]:
    with _write_scope(db):
        return _persist_candidate(db, candidate)


def _upsert_record(db, romm_id, item_id, platform_id) -> None:
    db.execute(
        "INSERT INTO romm_records (romm_id, item_id, platform_id) VALUES (?, ?, ?) "
        "ON CONFLICT(romm_id) DO UPDATE SET platform_id = excluded.platform_id, updated_at = datetime('now')",
        (romm_id, item_id, platform_id),
    )


def _persist_candidate(db, candidate: dict[str, Any]) -> dict[str, Any]:
    romm_id, platform_id, title = (_clean(candidate.get(key)) for key in ("romm_id", "romm_platform_id", "title"))
    if not romm_id or not platform_id or not title:
        raise RomMPersistenceError("RomM candidate is missing id, platform id or title")
    existing = _attachment(db, romm_id)
    if existing is not None and existing["deleted_at"] is not None:
        return {"item_id": existing["id"], "action": "in_trash"}
    # Match/Trash decision precedes even an ancillary platform registry write.
    platform = ensure_platform(db, str(candidate.get("platform") or ""), _clean(candidate.get("platform_name")))
    fields = {
        "title": title, "media_type": "digital_game", "platform": platform,
        "publisher": _clean(candidate.get("publisher")), "publish_year": candidate.get("publish_year"),
        "description": _clean(candidate.get("description")),
    }
    if existing is None:
        item_id = insert_item(db, {
            **fields, "romm_id": romm_id, "romm_platform_id": platform_id, "source": "romm", "owned": 1,
        }, restore_trashed=False)
        action = "created"
        _upsert_record(db, romm_id, item_id, platform_id)
    else:
        item_id = existing["id"]
        # Preexisting physical/manual exact attachments are retained, not taken
        # over or moved by a migration. Only fill missing descriptive metadata.
        if existing["source"] == "romm" and existing["media_type"] == "digital_game":
            platforms = {row["platform_id"] for row in db.execute(
                "SELECT platform_id FROM romm_records WHERE item_id = ? AND romm_id != ?", (item_id, romm_id),
            ).fetchall()}
            if platforms and platforms != {platform_id}:
                raise RomMPersistenceError("Plural RomM holdings have incompatible platforms")
            desired = {key: value for key, value in fields.items() if value is not None}
        else:
            desired = {key: value for key, value in fields.items()
                       if key in {"publisher", "publish_year", "description"}
                       and existing[key] in (None, "") and value is not None}
        other_holding = db.execute(
            "SELECT 1 FROM romm_records WHERE item_id = ? AND romm_id != ? UNION "
            "SELECT 1 FROM digital_holdings WHERE item_id = ? AND provider = 'romm' AND external_id != ? LIMIT 1",
            (item_id, romm_id, item_id, romm_id),
        ).fetchone()
        if existing["romm_id"] == romm_id or (not existing["romm_id"] and not other_holding):
            desired.update(romm_id=romm_id, romm_platform_id=platform_id)
        desired = {key: value for key, value in desired.items() if existing[key] != value}
        _upsert_record(db, romm_id, item_id, platform_id)
        if desired:
            update_item_fields(db, item_id, desired)
        action = "updated" if desired else "unchanged"
    if _series_enricher is not None:
        _series_enricher(db, [{"id": romm_id, "metadatum": candidate.get("metadatum") or {}}])
    holdings.sync_item(db, item_id)
    return {"item_id": item_id, "action": action}


def records_for_item(db, item_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(
        "SELECT rr.romm_id, rr.platform_id FROM romm_records rr JOIN items_live i ON i.id = rr.item_id "
        "WHERE rr.item_id = ? ORDER BY rr.romm_id", (item_id,),
    ).fetchall()]


def cover_binding(db, item_id: int, romm_id: str | None = None,
                  platform_id: str | None = None) -> tuple | None:
    """Snapshot exact availability, not a title match or an unscoped item ID.

    The cover orchestrator calls this before network I/O and again under its
    final write lock. Every projection participates, so deleting or rebinding a
    sidecar cannot leave an in-flight cover authorized by a stale inline ID.
    """
    item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if item is None or item["cover_path"]:
        return None
    require_item_role(db, item_id, "editor")
    sidecars = tuple(tuple(row) for row in db.execute(
        "SELECT romm_id, platform_id FROM romm_records WHERE item_id = ? ORDER BY romm_id", (item_id,),
    ))
    digital = tuple(tuple(row) for row in db.execute(
        "SELECT external_id, library_id FROM digital_holdings WHERE item_id = ? AND provider = 'romm' "
        "ORDER BY external_id", (item_id,),
    ))
    inline = (_clean(item["romm_id"]), _clean(item["romm_platform_id"]))
    pairs = {(_clean(identity), _clean(platform)) for identity, platform in (inline, *sidecars, *digital)
             if _clean(identity)}
    selected = _clean(romm_id) or inline[0]
    if selected is None and len({identity for identity, _ in pairs}) == 1:
        selected = next(iter(pairs))[0]
    platforms = {platform for identity, platform in pairs if identity == selected}
    requested_platform = _clean(platform_id)
    if (not selected or not platforms or len(platforms - {None}) != 1
            or (requested_platform and platforms - {None} != {requested_platform})):
        return None
    attachment = _attachment(db, selected)
    if attachment is None or attachment["id"] != item_id:
        return None
    return (selected, requested_platform or next(iter(platforms - {None})),
            libraries.item_library_id(db, item_id), item["source"], item["media_type"], item["platform"],
            inline, sidecars, digital)


def detach_record(db, romm_id: str) -> None:
    with _write_scope(db):
        _detach_record(db, romm_id)


def _detach_record(db, romm_id: str) -> None:
    item = _attachment(db, str(romm_id))
    if item is None:
        return
    require_item_role(db, item["id"], "editor", include_deleted=True)
    db.execute("DELETE FROM romm_records WHERE romm_id = ?", (str(romm_id),))
    if item["romm_id"] == str(romm_id):
        update_item_fields(db, item["id"], {"romm_id": None, "romm_platform_id": None})
    db.execute("DELETE FROM digital_holdings WHERE item_id = ? AND provider = 'romm' AND external_id = ?",
               (item["id"], str(romm_id)))
    holdings.sync_item(db, item["id"])


def cleanup_excluded_platforms(db, excluded: set[str]) -> dict[str, int]:
    """Reversibly remove excluded availability, never an item's other holdings.

    All projections and authority are read under the same write lock. A provider-
    owned item with no remaining holding moves to Trash once, retaining its IDs
    and child history. Adopted items and items still held elsewhere only detach
    the excluded RomM identities through the canonical adapter above.
    """
    with _write_scope(db):
        records = db.execute(
            "SELECT rr.item_id, rr.romm_id, rr.platform_id FROM romm_records rr "
            "JOIN items_live i ON i.id = rr.item_id UNION "
            "SELECT id, romm_id, romm_platform_id FROM items_live WHERE romm_id IS NOT NULL UNION "
            "SELECT dh.item_id, dh.external_id, dh.library_id FROM digital_holdings dh "
            "JOIN items_live i ON i.id = dh.item_id WHERE dh.provider = 'romm'"
        ).fetchall()
        identities: dict[tuple[int, str], set[str | None]] = {}
        for row in records:
            romm_id = _clean(row["romm_id"])
            if romm_id:
                identities.setdefault((row["item_id"], romm_id), set()).add(_clean(row["platform_id"]))
        targets: dict[int, list[str]] = {}
        items = {}
        for (item_id, romm_id), platforms in identities.items():
            if not platforms.intersection(excluded):
                continue
            if len(platforms - {None}) > 1:
                raise RomMPersistenceError("RomM identity has conflicting platform attachments")
            item = _attachment(db, romm_id)
            if item is None or item["id"] != item_id:
                raise RomMPersistenceError("RomM identity has conflicting catalogue attachments")
            require_item_role(db, item_id, "editor")
            items[item_id] = item
            targets.setdefault(item_id, []).append(romm_id)

        deleted = detached = 0
        for item_id, selected in targets.items():
            item = items[item_id]
            retained = any(
                other_item == item_id and romm_id not in selected
                for other_item, romm_id in identities
            ) or db.execute(
                "SELECT 1 FROM digital_holdings WHERE item_id = ? AND provider != 'romm' UNION "
                "SELECT 1 FROM copies_live WHERE item_id = ? UNION "
                "SELECT 1 FROM komga_records WHERE item_id = ? LIMIT 1",
                (item_id, item_id, item_id),
            ).fetchone() is not None or bool(item["abs_id"] or item["komga_id"])
            if item["source"] == "romm" and not retained:
                deleted += int(trash_item(db, item_id))
            else:
                for romm_id in selected:
                    _detach_record(db, romm_id)
                    detached += 1
        return {"deleted": deleted, "detached": detached}
