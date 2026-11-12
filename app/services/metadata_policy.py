"""Conservative, per-source three-way refresh of catalogue metadata.

``value_json`` contains the latest ``observed`` remote value and, only when
known, the last provider-``applied`` value. ``local`` records the actual stored
value after each observation. A change from that snapshot is a durable local
``overridden`` decision, including an explicit clear to NULL or blank text.
Observing a manual/legacy field (even one equal to the remote value) does not
grant ownership of it, and observing an override never claims it back.

Call inside the caller's transaction. Nothing here commits, opens a second
connection, installs schema, or logs (G3). The read/decision/write sequence
must hold the write lock (G18); ``begin_write`` acquires it when necessary.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from app.services import item_write


# Personal state, covers, tags, source/holding identities and locations are
# deliberately outside descriptive refresh, even when a provider supplies them.
METADATA_FIELDS = frozenset({
    "title", "authors", "narrator", "isbn", "asin", "upc", "series_name",
    "series_position", "publisher", "publish_year", "description",
    "duration_mins", "page_count", "language", "media_type", "platform",
})


def begin_write(db) -> None:
    """Lock before any guard read, without committing the caller's work."""
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _locally_overridden(current: Any, baseline: Mapping[str, Any]) -> bool:
    # Keep an override after recording its new local value, even if it is
    # empty or later happens to match the remote value. Older snapshots did
    # not record `local`; their known `applied` value still detects a clear.
    return bool(baseline.get("overridden")) or any(
        current != baseline[key] for key in ("local", "applied") if key in baseline
    )


def _check_fields(source_key: str, fields: Mapping[str, Any]) -> None:
    if not isinstance(source_key, str) or not source_key.strip():
        raise ValueError("Metadata provenance requires a source key")
    unknown = set(fields) - METADATA_FIELDS
    if unknown:
        raise ValueError(f"Not refreshable catalogue metadata: {sorted(unknown)}")


def _item(db, item_id: int):
    row = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ValueError("Live catalogue item not found")
    return row


def _snapshots(db, item_id: int, source_key: str) -> dict[str, dict[str, Any]]:
    rows = db.execute(
        "SELECT field_name, value_json FROM item_metadata_provenance "
        "WHERE item_id = ? AND source_key = ?",
        (item_id, source_key),
    ).fetchall()
    result = {}
    for row in rows:
        try:
            value = json.loads(row["value_json"])
        except (TypeError, ValueError):
            continue
        # An unknown/old representation is not evidence that this source
        # applied the current local value. Fail closed, without logging.
        if isinstance(value, dict) and "observed" in value:
            result[row["field_name"]] = value
    return result


def _save_snapshot(db, item_id: int, source_key: str, name: str, snapshot: dict) -> None:
    db.execute(
        "INSERT INTO item_metadata_provenance "
        "(item_id, source_key, field_name, value_json) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(item_id, source_key, field_name) DO UPDATE SET "
        "value_json = excluded.value_json, observed_at = datetime('now')",
        (item_id, source_key, name, json.dumps(snapshot, ensure_ascii=False)),
    )


def cover_fill_allowed(db, item_id: int, source_key: str, *, retained_file: bool = False,
                       explicit_allow: bool = False) -> bool:
    """Observe a local cover decision under the lock; covers remain fill-only.

    The Remove Cover endpoint resets dismissal but keeps its file. Persist
    that evidence (or a change from a known cover) so later orphan cleanup
    cannot silently undo the removal. This is NOT a provider metadata refresh:
    ``cover_path`` stays outside METADATA_FIELDS and is never overwritten here.
    Call again after downloading, before touching either the file or item.
    """
    _check_fields(source_key, {})
    begin_write(db)
    row = _item(db, item_id)
    current = row["cover_path"]
    baseline = _snapshots(db, item_id, source_key).get("cover_path", {})
    overridden = (bool(row["cover_review_dismissed"]) or (not explicit_allow and (
        _locally_overridden(current, baseline) or (_empty(current) and retained_file))))
    snapshot = {"observed": baseline.get("observed"), "local": current, "overridden": overridden}
    if not overridden and "applied" in baseline and current == baseline["applied"]:
        snapshot["applied"] = baseline["applied"]
    _save_snapshot(db, item_id, source_key, "cover_path", snapshot)
    return _empty(current) and not overridden


def record_cover_applied(db, item_id: int, source_key: str) -> None:
    """Record an automatic cover fill only AFTER its guarded write succeeded."""
    _check_fields(source_key, {})
    begin_write(db)
    path = _item(db, item_id)["cover_path"]
    _save_snapshot(db, item_id, source_key, "cover_path", {
        "observed": path, "applied": path, "local": path, "overridden": False,
    })


def metadata_updates(
    db, item_id: int, source_key: str, fields: Mapping[str, Any], *,
    fill_only: bool = False,
) -> dict[str, Any]:
    """Plan changes; call ``record_observation`` only after they succeed.

    Empty provider values never erase metadata. A missing baseline preserves
    every nonempty local value, regardless of the item's original ``source``.
    Only genuinely missing fields may be filled: a known local change or a
    recorded override prevents filling even an empty field on later syncs.
    ``fill_only`` keeps stricter adapters' adopted-manual-item contract.
    """
    _check_fields(source_key, fields)
    begin_write(db)
    row = _item(db, item_id)
    previous = _snapshots(db, item_id, source_key)
    updates = {}
    for name, incoming in fields.items():
        if _empty(incoming):
            continue
        current = row[name]
        baseline = previous.get(name, {})
        if _locally_overridden(current, baseline):
            continue
        provider_owned = "applied" in baseline and current == baseline["applied"]
        follows = not fill_only or baseline.get("follow_source") is True
        if (_empty(current) or (follows and provider_owned)) and current != incoming:
            updates[name] = incoming
    return updates


def record_observation(
    db, item_id: int, source_key: str, fields: Mapping[str, Any], *,
    applied_fields: Iterable[str] = (),
) -> None:
    """Save remote snapshots, distinguishing observed from actually applied.

    Pass only fields successfully written by this provider as ``applied_fields``.
    Read their values back from SQLite so INTEGER/REAL affinity or the write
    funnel's canonicalisation cannot manufacture a false local edit. Changes
    from a known local/applied snapshot are durable overrides, even when a
    person clears a field that was only observed on adoption, never applied.
    Only a successful provider application resets that field's override flag.
    """
    _check_fields(source_key, fields)
    applied = set(applied_fields)
    if not applied <= fields.keys():
        raise ValueError("Applied metadata fields must be part of the observation")
    begin_write(db)
    row = _item(db, item_id)
    previous = _snapshots(db, item_id, source_key)
    for name, incoming in fields.items():
        baseline = previous.get(name, {})
        overridden = name not in applied and _locally_overridden(row[name], baseline)
        snapshot = {"observed": incoming, "local": row[name], "overridden": overridden}
        if not overridden and baseline.get("follow_source") is True:
            snapshot["follow_source"] = True
        if name in applied:
            snapshot["applied"] = row[name]
        elif not overridden and "applied" in baseline and row[name] == baseline["applied"]:
            snapshot["applied"] = baseline["applied"]
        _save_snapshot(db, item_id, source_key, name, snapshot)


def record_initial(db, item_id: int, source_key: str, fields: Mapping[str, Any]) -> None:
    """Record values just used to CREATE a provider item, never an adopted one."""
    record_observation(db, item_id, source_key, fields, applied_fields=fields)


def refresh_metadata(
    db, item_id: int, source_key: str, fields: Mapping[str, Any], *,
    fill_only: bool = False,
) -> dict[str, Any]:
    """Apply safe changes and record the observation on the same connection.

    Returns changed fields. For adapters that also write holding identities,
    use ``metadata_updates`` + the write funnel + ``record_observation`` to
    validate/write everything before recording a successful observation.
    """
    updates = metadata_updates(db, item_id, source_key, fields, fill_only=fill_only)
    if updates:
        item_write.update_item_fields(db, item_id, updates)
    record_observation(db, item_id, source_key, fields, applied_fields=updates)
    return updates
