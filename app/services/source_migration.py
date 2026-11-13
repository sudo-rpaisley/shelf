"""Reviewed, local-only replacement and repair of machine source bindings.

Preview stores only a short-lived review plan, never changes catalogue data.
Apply/retire/rollback take the writer lock *before* re-reading the fingerprint.
No credentials, connector calls, item/copy writes or title matching live here.
The existing sync contract already follows source_records.item_id, so changing
that binding persists across sync without a special-case adapter hook.

Reattachment does NOT transfer metadata authority. Descriptive observations
are retained but frozen on both sides, including explicit blanks; old applied
and follow_source claims are not restored by rollback. Newer authority claims
block a changed-binding rollback; binding-no-op rollback leaves them intact.
Covers and other source keys are untouched. A binding-no-op review changes
neither binding versions nor metadata authority. Metadata sources remains the
explicit way to follow a correct source again after inspecting local values.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone

from app import features
from app.services import availability, metadata_policy, sources
from app.services.isbn import canonical_isbn_pair
from app.services.source_connectors import PROVIDERS
from app.services.source_connectors.base import safe_url

PAGE_SIZE = 50
STREAM_BATCH = 64
MAX_MAPPINGS = 100
PREVIEW_MINUTES = 15
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_ID = re.compile(r"[0-9a-f]{32}\Z")
_IDENTITIES = frozenset({
    "Imdb", "Tmdb", "Tvdb", "Asin", "MusicBrainzAlbum", "MusicBrainzRelease",
    "MusicBrainzReleaseGroup",
})
_SOURCE_COLUMNS = (
    "id, provider, name, enabled, config_revision, sync_token, last_attempt_at, "
    "last_sync_at, last_status, last_error, last_summary_json, updated_at"
)
_OPENING_REASONS = {
    "available": "Cached opening URL; no live connection check.",
    "missing_link": "No saved opening URL. This record provides no opening action.",
    "unsafe_link": "The saved opening URL is unsafe and is not displayed.",
    "incompatible_media": "Source and catalogue media types differ; no compatible opening action is promised.",
    "source_disabled": "Source is disabled; its opening action is unavailable.",
    "source_unconfirmed": "Source sync is running, incomplete or failed; the cached opening action is not confirmed.",
    "unavailable": "The remote record is unavailable.",
    "unknown": "The remote record availability is unknown.",
    "not_live": "The catalogue entry is missing or in Trash.",
}


class MigrationError(ValueError):
    """A static, non-reflecting error safe for either HTML or JSON."""

    def __init__(self, code: str, message: str, status_code: int = 422):
        self.code, self.status_code = code, status_code
        super().__init__(message)


def _conflict():
    raise MigrationError("stale_preview", "The bindings, settings or local values changed. Preview and review again.", 409)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def positive_id(value) -> int:
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19:
        value = int(value)
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise MigrationError("invalid_id", "Choose a valid source, record or catalogue item ID.")
    return value


def _lock(db) -> None:
    # Services own the lock, but never commit their caller's transaction.
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")


def _source(db, source_id: int) -> dict:
    row = db.execute(f"SELECT {_SOURCE_COLUMNS} FROM source_instances WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise MigrationError("not_found", "Source not found.", 404)
    provider = PROVIDERS.get(row["provider"])
    if not provider or not provider.syncable:
        raise MigrationError("manual_only", "Only machine-synced instances use this workflow. Manual and legacy sources are unchanged.")
    if row["sync_token"]:
        raise MigrationError("busy", "Finish or retry the source sync before reviewing bindings.", 409)
    return dict(row)


def authorize_mutation(db, actor: dict) -> None:
    """Fresh request authority on the same locked connection as the mutation.

    This is the capability seam for the coordinator's household authorization,
    not a maintenance bypass. Never re-decode a JWT or open another connection
    here: that could log or accept a replacement session after a body await.
    """
    if not db.in_transaction:
        raise RuntimeError("Source migration authorization requires the write lock")
    current = db.execute("SELECT username, role, token_version FROM users WHERE id = ?", (actor.get("id"),)).fetchone()
    expiry = actor.get("expires_at")
    if (current is None or type(expiry) not in (int, float) or not math.isfinite(expiry)
            or expiry <= _now().timestamp() or current["token_version"] != actor.get("token_version")
            or not isinstance(actor.get("username"), str)
            or current["username"].casefold() != actor["username"].casefold()):
        raise MigrationError("actor_changed", "Your session changed or expired during this request. Sign in and review again; nothing was saved.", 401)
    if actor.get("role") != "admin" or current["role"] != "admin":
        raise MigrationError("actor_changed", "Administrator permission changed during this request. Nothing was saved.", 403)
    flag = db.execute("SELECT value FROM settings WHERE key = ?", (features.setting_key("connected_library"),)).fetchone()
    if flag is not None and flag["value"] == "0":
        raise MigrationError("feature_disabled", "Connected library was disabled during this request. Nothing was saved.", 403)


def _iter_inventory(db, source_id: int):
    """All rows, in indexed stable order, with bounded Python hydration.

    The unique (source_id, external_id) index supplies ordering without sorting
    whole metadata blobs. The pure SQL function makes the indexed provenance
    join exact without an N+1 query or a persistent derived-identity table.
    """
    db.create_function("shelf_migration_key", 2, sources.metadata_source_key, deterministic=True)
    cursor = db.execute(
        "SELECT r.*, COALESCE(v.revision, 0) AS migration_revision, i.id AS migration_live_id, "
        "p.value_json AS migration_isbn FROM source_records r "
        "LEFT JOIN source_binding_versions v ON v.record_id = r.id "
        "LEFT JOIN items_live i ON i.id = r.item_id "
        "LEFT JOIN item_metadata_provenance p ON p.item_id = r.item_id "
        "AND p.source_key = shelf_migration_key(r.source_id, r.external_id) AND p.field_name = 'isbn' "
        "WHERE r.source_id = ? ORDER BY r.external_id", (source_id,),
    )
    try:
        while rows := cursor.fetchmany(STREAM_BATCH):
            for row in rows:
                yield dict(row)
    finally:
        cursor.close()


def _digest(rows) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_json(row).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def record_page(db, source_id: int, *, after: int = 0, before: int | None = None,
                record_id: int | None = None, external_id: str | None = None) -> dict:
    source_id = positive_id(source_id)
    _source(db, source_id)
    if type(after) is not int or not 0 <= after <= 2**63 - 1:
        raise MigrationError("invalid_id", "Choose a valid record page cursor.")
    if before is not None:
        before = positive_id(before)
    conditions, args = ["source_id = ?"], [source_id]
    if record_id is not None:
        conditions.append("id = ?")
        args.append(positive_id(record_id))
    if external_id is not None:
        if not isinstance(external_id, str) or not 1 <= len(external_id) <= 512:
            raise MigrationError("invalid_id", "Enter one exact external record ID, at most 512 characters.")
        conditions.append("external_id = ?")
        args.append(external_id)
    if before is not None:
        conditions.append("id < ?")
        args.append(before)
    elif after:
        conditions.append("id > ?")
        args.append(after)
    order = "DESC" if before is not None else "ASC"
    rows = db.execute("SELECT * FROM source_records WHERE " + " AND ".join(conditions)
                      + f" ORDER BY id {order} LIMIT ?", (*args, PAGE_SIZE + 1)).fetchall()
    rows = rows[:PAGE_SIZE]
    if before is not None:
        rows.reverse()
    records = [_display_record(db, dict(row)) for row in rows]
    total = db.execute("SELECT COUNT(*) FROM source_records WHERE source_id = ?", (source_id,)).fetchone()[0]
    filtered = record_id is not None or external_id is not None
    previous = records[0]["id"] if records and not filtered and db.execute(
        "SELECT 1 FROM source_records WHERE source_id = ? AND id < ? LIMIT 1", (source_id, records[0]["id"]),
    ).fetchone() else None
    following = records[-1]["id"] if records and not filtered and db.execute(
        "SELECT 1 FROM source_records WHERE source_id = ? AND id > ? LIMIT 1", (source_id, records[-1]["id"]),
    ).fetchone() else None
    return {"records": records, "total": total, "previous_before": previous, "next_after": following,
            "after": after, "before": before, "filtered": filtered, "page_size": PAGE_SIZE}


def _object(raw) -> dict:
    if not isinstance(raw, str) or len(raw) > 131072:
        return {}
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_number)
    except (ValueError, TypeError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _unique_object(pairs):
    value = {}
    for key, entry in pairs:
        if key in value:
            raise ValueError
        value[key] = entry
    return value


def _invalid_number(value):
    raise ValueError


def _observations(db, record: dict, item_id: int | None = None) -> dict:
    key = sources.metadata_source_key(record["source_id"], record["external_id"])
    rows = db.execute(
        "SELECT field_name, value_json FROM item_metadata_provenance WHERE item_id = ? AND source_key = ?",
        (record["item_id"] if item_id is None else item_id, key),
    ).fetchall()
    return {row["field_name"]: _object(row["value_json"]).get("observed") for row in rows}


def _identifiers(db, record: dict) -> dict[str, list[str]]:
    """Only remote observations, never an edited local item's identifiers/title."""
    raw = _object(record["metadata_json"]).get("provider_ids")
    cleaned = sources._provider_ids(raw)
    identities: dict[str, set[str]] = {}
    for name, value in cleaned.items():
        if name.startswith("Isbn"):
            identities.setdefault("ISBN", set()).add(canonical_isbn_pair(value)[0])
        elif name in _IDENTITIES:
            identities.setdefault(name, set()).add(value.lower() if name.startswith("MusicBrainz") else value)
    isbn = (_object(record["migration_isbn"]).get("observed") if "migration_isbn" in record
            else _observations(db, record).get("isbn"))
    if isinstance(isbn, str) and re.fullmatch(r"[0-9Xx -]{1,100}", isbn):
        pair = canonical_isbn_pair(isbn)
        if pair:
            identities.setdefault("ISBN", set()).add(pair[0])
    return {key: sorted(values) for key, values in sorted(identities.items())}


def _evidence(old: dict, new: dict) -> tuple[list[dict], list[str]]:
    shared, conflicts = [], []
    for key in sorted(old["identifiers"].keys() & new["identifiers"].keys()):
        a, b = set(old["identifiers"][key]), set(new["identifiers"][key])
        shared.extend({"kind": key, "value": value} for value in sorted(a & b))
        if a != b:
            conflicts.append(f"Different {key} observations")
    if old["media_type"] != new["media_type"]:
        conflicts.append("Different source media types")
    return shared, conflicts


def _display_record(db, record: dict) -> dict:
    item = db.execute("SELECT id, title, media_type FROM items_live WHERE id = ?", (record["item_id"],)).fetchone()
    return {
        "id": record["id"], "source_id": record["source_id"], "item_id": record["item_id"],
        "external_id": record["external_id"], "title": record["title"][:2000],
        "media_type": record["media_type"], "availability": record["availability"],
        "item_title": item["title"][:2000] if item else None, "live": item is not None,
        "identifiers": _identifiers(db, record),
        "opening": _opening(db, record, record["item_id"]),
    }


def _opening(db, record: dict, item_id: int, *, disabled=False) -> dict:
    """Conservative cached projection, not a connection test or credential read.

    URLs come only from the saved record and the connector's credential-free
    allowlist, never raw source configuration. Share the existing action labels
    but do not call availability_for_items: its legacy adapters may decrypt
    configuration, which must not happen in a migration's write transaction.
    """
    source = db.execute(
        "SELECT enabled, last_status, last_error, sync_token FROM source_instances WHERE id = ?",
        (record["source_id"],),
    ).fetchone()
    item = db.execute("SELECT media_type FROM items_live WHERE id = ?", (item_id,)).fetchone()
    url = safe_url(record["open_url"])
    state = record["availability"]
    if item is None:
        reason = "not_live"
    elif disabled or source is None or not source["enabled"]:
        reason = "source_disabled"
        state = "unavailable" if state == "unavailable" else "unknown"
    elif record["media_type"] != item["media_type"]:
        reason = "incompatible_media"
    elif not record["open_url"]:
        reason = "missing_link"
    elif not url:
        reason = "unsafe_link"
    elif state != "available":
        reason = "unavailable" if state == "unavailable" else "unknown"
    elif source["sync_token"] or source["last_error"] or source["last_status"] in {"error", "partial", "syncing"}:
        reason, state = "source_unconfirmed", "unknown"
    else:
        reason = "available"
    return {"record_id": record["id"], "source_id": record["source_id"], "item_id": item_id,
            "label": availability._ACTIONS.get(record["media_type"], "Open"), "url": url,
            "usable": reason == "available", "availability": state,
            "reason_code": reason, "reason": _OPENING_REASONS[reason]}


def _opening_projection(db, mapping: dict, *, after=False, retired=False) -> list[dict]:
    result = []
    if "old_record_id" in mapping:
        old = db.execute("SELECT * FROM source_records WHERE id = ?", (mapping["old_record_id"],)).fetchone()
        if old:
            result.append(_opening(db, dict(old), mapping["after_item_id"], disabled=retired))
    record = db.execute("SELECT * FROM source_records WHERE id = ?", (mapping["record_id"],)).fetchone()
    if record:
        result.append(_opening(db, dict(record), mapping["after_item_id"] if after else mapping["before_item_id"]))
    return result


def _coverage(db, plan: dict) -> dict:
    result = {"selected": len(plan["mappings"])}
    for side, field in (("old", "old_record_id"), ("new", "record_id")):
        source_id = plan[side + "_source_id"]
        total = db.execute("SELECT COUNT(*) FROM source_records WHERE source_id = ?", (source_id,)).fetchone()[0]
        ids = sorted({mapping[field] for mapping in plan["mappings"] if field in mapping})
        selected = db.execute("SELECT COUNT(*) FROM source_records WHERE source_id = ? AND id IN ("
                              + ",".join("?" for _ in ids) + ")", (source_id, *ids)).fetchone()[0] if ids else 0
        result[side + "_records"], result["unmapped_" + side] = total, total - selected
    return result


def _inventory_fingerprint(db, plan: dict) -> str:
    """Coverage/identity/action inventory, excluding mere observation timestamps.

    Counts alone miss a same-sized replacement set. Include actual record IDs,
    bindings, remote identities, media and links. Configuration changes also
    require a fresh complete review, but no roots or credentials are selected.
    """
    inventory = []
    for source_id in sorted({plan["old_source_id"], plan["new_source_id"]}):
        source = _source(db, source_id)
        def records():
            for record in _iter_inventory(db, source_id):
                yield {key: value for key, value in record.items() if key not in {
                    "last_seen_at", "created_at", "updated_at", "migration_revision", "migration_isbn",
                }} | {"identifiers": _identifiers(db, record)}
        inventory.append([[source[key] for key in ("id", "provider", "enabled", "config_revision")], _digest(records())])
    return _hash(["complete-inventory-v2", inventory])


def _check_reviewed_inventory(db, plan: dict, result: dict) -> None:
    if result.get("inventory_fingerprint") != _inventory_fingerprint(db, plan):
        raise MigrationError("coverage_changed", "Source inventory or configuration changed after apply. Start a fresh complete replacement preview before retiring the old source.", 409)


def _identity_keys(record: dict):
    return ((record["media_type"], kind, value) for kind, values in record["identifiers"].items() for value in values)


def _suggestions(db, old_source_id: int, new_source_id: int, new_records: list[dict]) -> list[dict]:
    """Match only this bounded selection, checking ambiguity across BOTH scopes.

    Memory is O(page identities), not O(catalogue size). A collision anywhere,
    including after the visible page, prevents an automatic suggestion.
    """
    new_by_id = {record["id"]: record for record in new_records if record["live"]}
    index = {}
    for record in new_by_id.values():
        for key in _identity_keys(record):
            index.setdefault(key, set()).add(record["id"])
    if not index:
        return []
    possible = {record_id: [] for record_id in new_by_id}
    for raw in _iter_inventory(db, old_source_id):
        if raw["migration_live_id"] is None:
            continue
        old = {"id": raw["id"], "media_type": raw["media_type"], "identifiers": _identifiers(db, raw)}
        matched = {new_id for key in _identity_keys(old) for new_id in index.get(key, ())}
        for new_id in matched:
            if len(possible[new_id]) < 2:
                possible[new_id].append(old)
    unique = {new_id: choices[0] for new_id, choices in possible.items() if len(choices) == 1}
    reverse, counts = {}, {}
    for old in unique.values():
        counts.setdefault(old["id"], set())
        for key in _identity_keys(old):
            reverse.setdefault(key, set()).add(old["id"])
    if not reverse:
        return []
    for raw in _iter_inventory(db, new_source_id):
        if raw["migration_live_id"] is None:
            continue
        record = {"media_type": raw["media_type"], "identifiers": _identifiers(db, raw)}
        matched = {old_id for key in _identity_keys(record) for old_id in reverse.get(key, ())}
        for old_id in matched:
            if len(counts[old_id]) < 2:
                counts[old_id].add(raw["id"])
    result = []
    for new_id, old in unique.items():
        evidence, conflicts = _evidence(old, new_by_id[new_id])
        if counts[old["id"]] == {new_id} and evidence and not conflicts:
            result.append({"old_record_id": old["id"], "new_record_id": new_id, "evidence": evidence})
    return result


def workspace(db, old_source_id: int | None = None, new_source_id: int | None = None, *,
              old_after=0, new_after=0, old_before=None, new_before=None,
              old_record_id=None, new_record_id=None, old_external_id=None, new_external_id=None) -> dict:
    """Bounded read-only pages and exact identity suggestions; never title search."""
    instances = [row for row in sources.list_instances(db) if row["syncable"]]
    result = {"sources": instances, "old_records": [], "new_records": [], "suggestions": [],
              "old_source_id": old_source_id, "new_source_id": new_source_id}
    if old_source_id is None or new_source_id is None:
        return result
    old_source_id, new_source_id = positive_id(old_source_id), positive_id(new_source_id)
    if old_source_id == new_source_id:
        raise MigrationError("same_source", "Choose different old and replacement instances; use binding repair within one instance.")
    _source(db, old_source_id)
    _source(db, new_source_id)
    old = record_page(db, old_source_id, after=old_after, before=old_before, record_id=old_record_id, external_id=old_external_id)
    new = record_page(db, new_source_id, after=new_after, before=new_before, record_id=new_record_id, external_id=new_external_id)
    result.update(old_records=old["records"], new_records=new["records"], old_page=old, new_page=new,
                  suggestions=_suggestions(db, old_source_id, new_source_id, new["records"]))
    return result


def repair_records(db, source_id: int, *, after=0, before=None, record_id=None, external_id=None) -> dict:
    """Repair lookup returns a page WITH total/cursors, never an unlabeled prefix."""
    return record_page(db, source_id, after=after, before=before, record_id=record_id, external_id=external_id)


def _version(db, record_id: int) -> int:
    row = db.execute("SELECT revision FROM source_binding_versions WHERE record_id = ?", (record_id,)).fetchone()
    return row[0] if row else 0


def _state(db, plan: dict) -> dict:
    """Exact state for stale checks; only its digest is persisted or returned."""
    source_ids = sorted({plan["old_source_id"], plan["new_source_id"]})
    source_rows = [_source(db, source_id) for source_id in source_ids]
    inventory = [[source_id, _digest(_iter_inventory(db, source_id))] for source_id in source_ids]
    item_ids = set()
    for mapping in plan["mappings"]:
        record = db.execute("SELECT item_id FROM source_records WHERE id = ?", (mapping["record_id"],)).fetchone()
        if record is None:
            _conflict()
        item_ids.update((record["item_id"], mapping["before_item_id"], mapping["after_item_id"]))
        if mapping.get("old_record_id") is not None and db.execute(
            "SELECT 1 FROM source_records WHERE id = ?", (mapping["old_record_id"],),
        ).fetchone() is None:
            _conflict()
    items, provenance = [], []
    for item_id in sorted(item_ids):
        row = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise MigrationError("not_found", "Every affected catalogue entry must be live. Restore it before reviewing bindings.", 409)
        items.append(dict(row))
        provenance.append([item_id, _digest(dict(row) for row in db.execute(
            "SELECT * FROM item_metadata_provenance WHERE item_id = ? ORDER BY source_key, field_name", (item_id,),
        ))])
    return {"sources": source_rows, "complete_inventory": inventory, "items": items, "provenance": provenance}


def _fingerprint(db, plan: dict, *, purpose="apply", plan_id="") -> str:
    return _hash(["source-migration-v2-streamed", purpose, plan_id, plan, _state(db, plan)])


def preview_replacement(db, *, actor_id: int, old_source_id: int, new_source_id: int,
                        mappings: list[dict], manual_reviewed: bool = False, retire_old: bool = False) -> dict:
    _lock(db)
    actor_id = positive_id(actor_id)
    old_source_id, new_source_id = positive_id(old_source_id), positive_id(new_source_id)
    if type(manual_reviewed) is not bool or type(retire_old) is not bool:
        raise MigrationError("invalid_form", "Review the source replacement options.")
    if old_source_id == new_source_id:
        raise MigrationError("same_source", "Choose different old and replacement instances; use binding repair within one instance.")
    _source(db, old_source_id)
    if not _source(db, new_source_id)["enabled"]:
        raise MigrationError("disabled", "Enable and sync the replacement instance before reviewing its records.")
    if not isinstance(mappings, list) or not 1 <= len(mappings) <= MAX_MAPPINGS:
        raise MigrationError("invalid_mappings", "Review between 1 and 100 old/new record pairs at a time.")
    old, new, pairs = {}, {}, []
    for pair in mappings:
        if not isinstance(pair, dict) or set(pair) != {"old_record_id", "new_record_id"}:
            raise MigrationError("invalid_mappings", "Supply only reviewed old/new record IDs, not source metadata.")
        old_id, new_id = positive_id(pair["old_record_id"]), positive_id(pair["new_record_id"])
        pairs.append((old_id, new_id))
        for source_id, record_id, selected in ((old_source_id, old_id, old), (new_source_id, new_id, new)):
            row = db.execute("SELECT * FROM source_records WHERE source_id = ? AND id = ?", (source_id, record_id)).fetchone()
            if row is None:
                raise MigrationError("not_found", "Choose live records belonging to the selected old and replacement sources.", 404)
            selected[record_id] = _display_record(db, dict(row))
    suggested = {(pair["old_record_id"], pair["new_record_id"])
                 for pair in _suggestions(db, old_source_id, new_source_id, list(new.values()))} if not manual_reviewed else set()
    selected, seen_old, seen_new = [], set(), set()
    for old_id, new_id in pairs:
        if old_id in seen_old or new_id in seen_new:
            raise MigrationError("ambiguous_mapping", "Each old and new record may appear only once per review.")
        seen_old.add(old_id)
        seen_new.add(new_id)
        if old_id not in old or new_id not in new or not old[old_id]["live"] or not new[new_id]["live"]:
            raise MigrationError("not_found", "Choose live records belonging to the selected old and replacement sources.", 404)
        evidence, conflicts = _evidence(old[old_id], new[new_id])
        if (old_id, new_id) not in suggested and not manual_reviewed:
            raise MigrationError("review_required", "No unique conflict-free identifier match. Explicitly confirm manual review of the old/new IDs.")
        if manual_reviewed:
            evidence.append({"kind": "reviewed_record_ids", "old_record_id": old_id, "new_record_id": new_id})
        selected.append({
            "record_id": new_id, "old_record_id": old_id,
            "before_item_id": new[new_id]["item_id"], "after_item_id": old[old_id]["item_id"],
            "evidence": evidence, "conflicts": conflicts,
            "old_version": _version(db, old_id), "before_version": _version(db, new_id),
        })
    plan = {"kind": "replacement", "old_source_id": old_source_id, "new_source_id": new_source_id,
            "retire_old": retire_old, "mappings": selected}
    plan["coverage"] = _coverage(db, plan)
    return _store_preview(db, actor_id, plan)


def preview_repair(db, *, actor_id: int, source_id: int, record_id: int, item_id: int,
                   manual_reviewed: bool = False) -> dict:
    _lock(db)
    source_id, record_id, item_id = positive_id(source_id), positive_id(record_id), positive_id(item_id)
    _source(db, source_id)
    if manual_reviewed is not True:
        raise MigrationError("review_required", "Confirm that you reviewed the machine record ID and the correct local catalogue item ID.")
    record = db.execute("SELECT * FROM source_records WHERE source_id = ? AND id = ?", (source_id, record_id)).fetchone()
    target = db.execute("SELECT id, media_type FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if record is None or target is None:
        raise MigrationError("not_found", "Choose an existing source record and a live catalogue entry.", 404)
    if record["item_id"] == item_id:
        raise MigrationError("unchanged", "That record is already bound to this catalogue entry.")
    conflicts = ["Different source and local media types"] if record["media_type"] != target["media_type"] else []
    plan = {"kind": "repair", "old_source_id": source_id, "new_source_id": source_id, "retire_old": False,
            "mappings": [{"record_id": record_id, "before_item_id": record["item_id"], "after_item_id": item_id,
                          "before_version": _version(db, record_id), "conflicts": conflicts,
                          "evidence": [{"kind": "reviewed_record_and_item_ids", "record_id": record_id, "item_id": item_id}]}]}
    return _store_preview(db, positive_id(actor_id), plan)


def _store_preview(db, actor_id: int, plan: dict) -> dict:
    for mapping in plan["mappings"]:
        mapping["openings_before"] = _opening_projection(db, mapping)
        mapping["openings_after"] = _opening_projection(db, mapping, after=True, retired=plan["retire_old"])
        if plan["kind"] == "replacement":
            mapping["openings_after_retirement"] = _opening_projection(db, mapping, after=True, retired=True)
    fingerprint = _fingerprint(db, plan)
    plan_id, now = uuid.uuid4().hex, _now()
    db.execute(
        "INSERT INTO source_migration_plans (id, actor_id, plan_json, fingerprint, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (plan_id, actor_id, _json(plan), fingerprint, _time(now), _time(now + timedelta(minutes=PREVIEW_MINUTES))),
    )
    return {"id": plan_id, "fingerprint": fingerprint, "state": "preview", "plan": plan,
            "expires_at": _time(now + timedelta(minutes=PREVIEW_MINUTES))}


def _load(db, plan_id: str, actor_id: int) -> tuple[dict, dict]:
    if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
        raise MigrationError("not_found", "Review plan not found.", 404)
    row = db.execute("SELECT * FROM source_migration_plans WHERE id = ? AND actor_id = ?",
                     (plan_id, positive_id(actor_id))).fetchone()
    if row is None:
        raise MigrationError("not_found", "Review plan not found for this administrator.", 404)
    return dict(row), json.loads(row["plan_json"])


def _check_fingerprint(submitted, expected) -> None:
    if not isinstance(submitted, str) or not _HEX.fullmatch(submitted) or not hmac.compare_digest(submitted, expected):
        _conflict()


def _confirm(reviewed) -> None:
    if reviewed is not True:
        raise MigrationError("review_required", "Explicitly confirm the reviewed bindings and metadata-authority warning.")


def _has_opening_review(plan: dict) -> bool:
    fields = {"openings_before", "openings_after"}
    if plan["kind"] == "replacement":
        fields.add("openings_after_retirement")
    return all(fields <= mapping.keys() for mapping in plan["mappings"])


def _freeze_authority(db, record: dict, item_id: int, observed: dict) -> None:
    """No copied applied baseline, no automatic fills of reviewed local blanks.

    Cover provenance is deliberately excluded: its dismissal/removal decisions
    and timestamps are not binding-repair data. Other source keys stay intact.
    """
    row = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        _conflict()
    key = sources.metadata_source_key(record["source_id"], record["external_id"])
    existing = _observations(db, record, item_id)
    for field in sorted(metadata_policy.METADATA_FIELDS):
        snapshot = {"observed": existing.get(field, observed.get(field)), "local": row[field], "overridden": True}
        db.execute(
            "INSERT INTO item_metadata_provenance (item_id, source_key, field_name, value_json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(item_id, source_key, field_name) DO UPDATE SET value_json = excluded.value_json",
            (item_id, key, field, _json(snapshot)),
        )


def _rebind(db, record: dict, item_id: int, plan_id: str) -> int:
    observed = _observations(db, record)
    _freeze_authority(db, record, record["item_id"], observed)
    _freeze_authority(db, record, item_id, observed)
    # Stable (source_id, external_id) is retained. Future sync looks this up.
    db.execute("UPDATE source_records SET item_id = ?, updated_at = ? WHERE id = ?", (item_id, _time(_now()), record["id"]))
    db.execute(
        "INSERT INTO source_binding_versions (record_id, revision, last_plan_id) VALUES (?, 1, ?) "
        "ON CONFLICT(record_id) DO UPDATE SET revision = revision + 1, last_plan_id = excluded.last_plan_id",
        (record["id"], plan_id),
    )
    return _version(db, record["id"])


def _bump_sources(db, plan: dict, *, retirement=False, restore_enabled=None) -> dict:
    revisions = {}
    for source_id in sorted({plan["old_source_id"], plan["new_source_id"]}):
        source = _source(db, source_id)
        enabled = source["enabled"]
        if source_id == plan["old_source_id"]:
            if retirement:
                enabled = 0
            elif restore_enabled is not None:
                enabled = restore_enabled
        db.execute("UPDATE source_instances SET enabled = ?, config_revision = config_revision + 1, "
                   "sync_token = NULL, updated_at = ? WHERE id = ?", (enabled, _time(_now()), source_id))
        revisions[str(source_id)] = source["config_revision"] + 1
    return revisions


def apply(db, plan_id: str, *, actor_id: int, fingerprint: str, reviewed: bool = False) -> dict:
    _lock(db)
    _confirm(reviewed)
    journal, plan = _load(db, plan_id, actor_id)
    _check_fingerprint(fingerprint, journal["fingerprint"])
    if journal["state"] == "applied":
        return {"ok": True, "id": plan_id, "state": "applied", "replayed": True}
    if journal["state"] != "preview":
        _conflict()
    if journal["expires_at"] <= _time(_now()):
        raise MigrationError("expired", "This preview expired. Create and review a fresh preview.", 409)
    if not _has_opening_review(plan):
        raise MigrationError("review_required", "This older preview did not record opening actions. Start a fresh preview and review the before/after opening actions before applying.", 409)
    _check_fingerprint(fingerprint, _fingerprint(db, plan))
    old_enabled = _source(db, plan["old_source_id"])["enabled"]
    versions = {}
    for mapping in plan["mappings"]:
        record = dict(db.execute("SELECT * FROM source_records WHERE id = ?", (mapping["record_id"],)).fetchone())
        if mapping["before_item_id"] == mapping["after_item_id"]:
            # A fresh inventory review is not a new binding/authority decision.
            # Keep the actual version so real intervening changes still fence
            # this receipt, without superseding an earlier repair needlessly.
            versions[str(record["id"])] = _version(db, record["id"])
            continue
        versions[str(record["id"])] = _rebind(db, record, mapping["after_item_id"], plan_id)
        if "old_record_id" in mapping:
            old = dict(db.execute("SELECT * FROM source_records WHERE id = ?", (mapping["old_record_id"],)).fetchone())
            _freeze_authority(db, old, old["item_id"], _observations(db, old))
    revisions = _bump_sources(db, plan, retirement=plan["retire_old"])
    result = {"versions": versions, "revisions": revisions, "old_enabled": old_enabled, "retired": plan["retire_old"]}
    result["inventory_fingerprint"] = _inventory_fingerprint(db, plan)
    db.execute("UPDATE source_migration_plans SET state = 'applied', result_json = ?, applied_at = ? WHERE id = ?",
               (_json(result), _time(_now()), plan_id))
    return {"ok": True, "id": plan_id, "state": "applied", "replayed": False}


def _check_owned_bindings(db, plan: dict, result: dict) -> None:
    for mapping in plan["mappings"]:
        record = db.execute("SELECT item_id FROM source_records WHERE id = ?", (mapping["record_id"],)).fetchone()
        if (record is None or record["item_id"] != mapping["after_item_id"]
                or _version(db, mapping["record_id"]) != result["versions"][str(mapping["record_id"])]):
            _conflict()
        if "old_record_id" in mapping:
            old = db.execute("SELECT item_id FROM source_records WHERE id = ?", (mapping["old_record_id"],)).fetchone()
            if old is None or old["item_id"] != mapping["after_item_id"] or _version(db, mapping["old_record_id"]) != mapping["old_version"]:
                _conflict()


def _check_rollback_authority(db, plan: dict) -> None:
    """A refreshed receipt cannot authorize erasing a newer Follow decision.

    A real rebind froze its affected fields, so a later applied/follow claim is a
    superseding authority decision, not migration-owned state. No-op bindings
    are excluded: rollback will not rewrite their provenance at all.
    """
    fields = sorted(metadata_policy.METADATA_FIELDS)
    marks = ",".join("?" for _ in fields)
    for mapping in plan["mappings"]:
        if mapping["before_item_id"] == mapping["after_item_id"]:
            continue
        record = db.execute("SELECT source_id, external_id FROM source_records WHERE id = ?", (mapping["record_id"],)).fetchone()
        key = sources.metadata_source_key(record["source_id"], record["external_id"])
        for item_id in (mapping["before_item_id"], mapping["after_item_id"]):
            rows = db.execute(
                f"SELECT value_json FROM item_metadata_provenance WHERE item_id = ? AND source_key = ? AND field_name IN ({marks})",
                (item_id, key, *fields),
            ).fetchall()
            snapshots = [_object(row["value_json"]) for row in rows]
            if len(rows) != len(fields) or any("observed" not in value or "applied" in value or value.get("follow_source") for value in snapshots):
                raise MigrationError("authority_changed", "A newer metadata authority decision supersedes this rollback. Keep it, or start a fresh explicit binding review; rollback cannot reset Follow source or applied decisions.", 409)


def detail(db, plan_id: str, *, actor_id: int) -> dict:
    """Safe receipt and fresh rollback/retirement fingerprints; never fetches."""
    journal, plan = _load(db, plan_id, actor_id)
    result = json.loads(journal["result_json"])
    context = {"id": plan_id, "plan": plan, "state": journal["state"], "fingerprint": journal["fingerprint"],
               "created_at": journal["created_at"], "expires_at": journal["expires_at"],
               "applied_at": journal["applied_at"], "rolled_back_at": journal["rolled_back_at"],
               "retired": result.get("retired", False), "rows": [], "conflict": False,
               "rollback_blocked": False, "coverage_changed": False}
    if plan["kind"] == "replacement":
        context["coverage"] = _coverage(db, plan)
    context["source_names"] = {source["id"]: source["name"] for source in sources.list_instances(db)
                               if source["id"] in {plan["old_source_id"], plan["new_source_id"]}}
    for mapping in plan["mappings"]:
        record = db.execute("SELECT * FROM source_records WHERE id = ?", (mapping["record_id"],)).fetchone()
        old = db.execute("SELECT * FROM source_records WHERE id = ?", (mapping.get("old_record_id"),)).fetchone()
        items = {}
        for key in ("before_item_id", "after_item_id"):
            item = db.execute("SELECT id, title, media_type FROM items_live WHERE id = ?", (mapping[key],)).fetchone()
            items[key] = dict(item) if item else None
        current_records = [dict(row) for row in (old, record) if row is not None]
        record_display = _display_record(db, dict(record)) if record else None
        old_display = _display_record(db, dict(old)) if old else None
        context["rows"].append({**mapping, **items, "record": record_display, "old_record": old_display,
                                "openings_current": [value["opening"] for value in (old_display, record_display) if value],
                                "openings_retire_now": [_opening(db, row, row["item_id"], disabled=row["source_id"] == plan["old_source_id"])
                                                        for row in current_records] if plan["kind"] == "replacement" else []})
    try:
        if journal["state"] == "preview":
            context["conflict"] = (not _has_opening_review(plan) or journal["expires_at"] <= _time(_now())
                                   or _fingerprint(db, plan) != journal["fingerprint"])
        elif journal["state"] == "applied":
            _check_owned_bindings(db, plan, result)
            if result["retired"] and _source(db, plan["old_source_id"])["config_revision"] != result["revisions"][str(plan["old_source_id"])]:
                _conflict()
            try:
                _check_rollback_authority(db, plan)
            except MigrationError as error:
                context["rollback_blocked"], context["rollback_notice"] = True, str(error)
            else:
                context["rollback_fingerprint"] = _fingerprint(db, plan, purpose="rollback", plan_id=plan_id)
            if plan["kind"] == "replacement" and not result["retired"]:
                try:
                    _check_reviewed_inventory(db, plan, result)
                except MigrationError as error:
                    context["coverage_changed"], context["coverage_notice"] = True, str(error)
                else:
                    context["retire_fingerprint"] = _fingerprint(db, plan, purpose="retire", plan_id=plan_id)
    except MigrationError:
        context["conflict"] = True
    return context


def retire(db, plan_id: str, *, actor_id: int, fingerprint: str, reviewed: bool = False) -> dict:
    _lock(db)
    _confirm(reviewed)
    journal, plan = _load(db, plan_id, actor_id)
    result = json.loads(journal["result_json"])
    if journal["state"] != "applied" or plan["kind"] != "replacement":
        _conflict()
    if result["retired"]:
        _check_fingerprint(fingerprint, result.get("retire_fingerprint", ""))
        return {"ok": True, "id": plan_id, "state": "applied", "replayed": True}
    _check_owned_bindings(db, plan, result)
    _check_reviewed_inventory(db, plan, result)
    _check_fingerprint(fingerprint, _fingerprint(db, plan, purpose="retire", plan_id=plan_id))
    # Only disable: credentials, snapshots and catalogue objects stay in place.
    source = _source(db, plan["old_source_id"])
    db.execute("UPDATE source_instances SET enabled = 0, config_revision = config_revision + 1, "
               "sync_token = NULL, updated_at = ? WHERE id = ?", (_time(_now()), source["id"]))
    result.update(retired=True, retire_fingerprint=fingerprint, old_enabled=source["enabled"])
    result["revisions"][str(source["id"])] = source["config_revision"] + 1
    db.execute("UPDATE source_migration_plans SET result_json = ? WHERE id = ?", (_json(result), plan_id))
    return {"ok": True, "id": plan_id, "state": "applied", "replayed": False}


def rollback(db, plan_id: str, *, actor_id: int, fingerprint: str, reviewed: bool = False) -> dict:
    _lock(db)
    _confirm(reviewed)
    journal, plan = _load(db, plan_id, actor_id)
    result = json.loads(journal["result_json"])
    if journal["state"] == "rolled_back":
        _check_fingerprint(fingerprint, result.get("rollback_fingerprint", ""))
        return {"ok": True, "id": plan_id, "state": "rolled_back", "replayed": True}
    if journal["state"] != "applied":
        _conflict()
    _check_owned_bindings(db, plan, result)
    _check_rollback_authority(db, plan)
    _check_fingerprint(fingerprint, _fingerprint(db, plan, purpose="rollback", plan_id=plan_id))
    if result["retired"] and _source(db, plan["old_source_id"])["config_revision"] != result["revisions"][str(plan["old_source_id"])]:
        _conflict()
    for mapping in plan["mappings"]:
        if mapping["before_item_id"] == mapping["after_item_id"]:
            # An ISBN-adopted replacement can be a binding no-op. Rewinding it
            # must not freeze metadata again or erase a later explicit choice.
            continue
        record = dict(db.execute("SELECT * FROM source_records WHERE id = ?", (mapping["record_id"],)).fetchone())
        _rebind(db, record, mapping["before_item_id"], plan_id)
    _bump_sources(db, plan, restore_enabled=result["old_enabled"] if result["retired"] else None)
    result["rollback_fingerprint"] = fingerprint
    db.execute("UPDATE source_migration_plans SET state = 'rolled_back', result_json = ?, rolled_back_at = ? WHERE id = ?",
               (_json(result), _time(_now()), plan_id))
    return {"ok": True, "id": plan_id, "state": "rolled_back", "replayed": False}
