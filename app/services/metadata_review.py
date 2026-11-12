"""Local-only, explicit review of saved provider metadata observations.

The review owns no schema and never infers provider ownership from ``source``.
Only an existing provenance row can be resolved. Its fingerprint covers the
exact saved snapshot, observation time and current field value, not truncated
display text. Call ``resolve_field`` under the caller's BEGIN IMMEDIATE; it
neither commits nor logs, and uses only that caller's connection.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import sqlite3
from datetime import datetime, timezone

from app import i18n
from app.services import item_write
from app.services.source_connectors import PROVIDERS


MAX_ROWS = 80
MAX_SOURCE_KEY_CHARS = 256
MAX_SNAPSHOT_CHARS = 131072
MAX_DISPLAY_CHARS = 1200
RESOLVABLE_FIELDS = frozenset({
    "title", "authors", "narrator", "description", "publisher", "publish_year",
    "page_count", "duration_mins", "language", "series_name", "series_position",
})
FIELD_LABELS = {
    "title": i18n.N_("Title"), "authors": i18n.N_("Authors"),
    "narrator": i18n.N_("Narrator"), "description": i18n.N_("Description"),
    "publisher": i18n.N_("Publisher"), "publish_year": i18n.N_("Publication year"),
    "page_count": i18n.N_("Page count"), "duration_mins": i18n.N_("Duration (minutes)"),
    "language": i18n.N_("Language"), "series_name": i18n.N_("Series"),
    "series_position": i18n.N_("Series position"),
    "isbn": i18n.N_("ISBN"), "isbn10": i18n.N_("ISBN-10"),
    "asin": i18n.N_("ASIN"), "upc": i18n.N_("UPC"),
    "media_type": i18n.N_("Media type"), "platform": i18n.N_("Platform"),
}
_TEXT_LIMITS = {
    "title": 2000, "authors": 2000, "narrator": 2000, "description": 20000,
    "publisher": 1000, "language": 100, "series_name": 1000,
    "isbn": 100, "isbn10": 100, "asin": 100, "upc": 100,
    "media_type": 100, "platform": 200,
}
_INTEGER_BOUNDS = {"publish_year": (1, 9999), "page_count": (0, 1000000),
                   "duration_mins": (0, 1000000)}
_LEGACY_LABELS = {"audiobookshelf": "Audiobookshelf", "komga": "Komga", "romm": "RomM"}
_LEGACY_KEY = re.compile(r"(audiobookshelf|komga|romm):[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_INSTANCE_KEY = re.compile(r"source:([1-9][0-9]{0,18})(?::[A-Za-z0-9][A-Za-z0-9._-]{0,199})?\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


class ReviewError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        self.status_code = status_code
        super().__init__(message)


def _source_key(value) -> bool:
    """Canonical opaque ids only: never URLs, whitespace, credentials or aliases."""
    if not isinstance(value, str) or len(value) > MAX_SOURCE_KEY_CHARS:
        return False
    if _LEGACY_KEY.fullmatch(value):
        return True
    match = _INSTANCE_KEY.fullmatch(value)
    return bool(match and int(match[1]) <= 2**63 - 1)


def _source_label(db, source_key: str) -> str:
    provider = source_key.split(":", 1)[0]
    if provider in _LEGACY_LABELS:
        return _LEGACY_LABELS[provider]
    # Only a provider enum is read. Instance names, roots, options and encrypted
    # credentials are deliberately not selected or copied into the page context.
    source_id = int(source_key.split(":")[1])
    source = db.execute("SELECT provider FROM source_instances WHERE id = ?", (source_id,)).fetchone()
    spec = PROVIDERS.get(source["provider"]) if source else None
    return spec.label if spec else i18n.N_("Unknown source")


def _plain_text(value: str) -> bool:
    return not any((ord(c) < 32 and c not in "\t\r\n") or 127 <= ord(c) <= 159
                   or 0xD800 <= ord(c) <= 0xDFFF for c in value)


def _valid_value(field: str, value, *, following: bool = False) -> bool:
    if value is None:
        return not (following and field == "title")
    if field in _TEXT_LIMITS:
        return (isinstance(value, str) and len(value) <= _TEXT_LIMITS[field]
                and _plain_text(value) and (not following or field != "title" or bool(value.strip())))
    if field in _INTEGER_BOUNDS:
        low, high = _INTEGER_BOUNDS[field]
        return type(value) is int and low <= value <= high
    if field == "series_position":
        return type(value) in (int, float) and 0 <= value <= 1000000 and math.isfinite(value)
    return False


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate snapshot key")
        result[key] = value
    return result


def _not_a_number(value):
    raise ValueError("Non-finite snapshot value")


def _snapshot(raw, field: str) -> dict | None:
    if not isinstance(raw, str) or not 0 < len(raw) <= MAX_SNAPSHOT_CHARS:
        return None
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_not_a_number)
    except (ValueError, TypeError, RecursionError):
        return None
    if (not isinstance(value, dict) or "observed" not in value
            or set(value) - {"observed", "local", "applied", "overridden", "follow_source"}
            or any(type(value[key]) is not bool for key in ("overridden", "follow_source") if key in value)):
        return None
    if not all(_valid_value(field, value[key]) for key in ("observed", "local", "applied") if key in value):
        return None
    return value


def _timestamp(value) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def _fingerprint(item_id: int, row, current) -> str:
    payload = ["metadata-review-v1", item_id, row["source_key"], row["field_name"],
               current, row["value_json"], row["observed_at"]]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _display(value) -> str:
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= MAX_DISPLAY_CHARS else text[:MAX_DISPLAY_CHARS] + "…"


def context_for(db, item_id: int) -> dict | None:
    """Bounded item-page/fragment context, or None for missing/trashed items.

    Unknown fields, unsafe source keys and malformed/unknown snapshot formats
    are ignored, never treated as evidence of ownership or rendered as blobs.
    GET and direct template includes use exactly the same read-only extraction.
    """
    item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return None
    fields = tuple(FIELD_LABELS)
    marks = ", ".join("?" for _ in fields)
    rows = db.execute(
        "SELECT source_key, field_name, "
        "CASE WHEN length(value_json) <= ? THEN value_json END AS value_json, "
        "CASE WHEN length(observed_at) <= 64 THEN observed_at END AS observed_at "
        "FROM item_metadata_provenance WHERE item_id = ? AND length(source_key) BETWEEN 1 AND ? "
        f"AND field_name IN ({marks}) "
        "ORDER BY source_key, field_name LIMIT ?",
        (MAX_SNAPSHOT_CHARS, item_id, MAX_SOURCE_KEY_CHARS, *fields, MAX_ROWS + 1),
    ).fetchall()
    result, labels = [], {}
    for row in rows[:MAX_ROWS]:
        field, key = row["field_name"], row["source_key"]
        if field not in FIELD_LABELS or not _source_key(key):
            continue
        snapshot = _snapshot(row["value_json"], field)
        current = item[field]
        if snapshot is None or not _valid_value(field, current):
            continue
        # Do not hash a substituted/truncated observation time as if it were
        # the exact saved row; an oversized corrupt timestamp is unreviewable.
        if row["observed_at"] is None:
            continue
        overridden = snapshot.get("overridden", False) or any(
            current != snapshot[name] for name in ("local", "applied") if name in snapshot
        )
        if key not in labels:
            labels[key] = _source_label(db, key)
        result.append({
            "source_key": key, "source_label": labels[key], "field_name": field,
            "field_label": FIELD_LABELS[field],
            "current_display": _display(current), "observed_display": _display(snapshot["observed"]),
            "observed_at": _timestamp(row["observed_at"]),
            "different": current != snapshot["observed"], "user_overridden": bool(overridden),
            "provider_owned": not overridden and "applied" in snapshot and current == snapshot["applied"],
            "resolvable": field in RESOLVABLE_FIELDS,
            "can_follow": field in RESOLVABLE_FIELDS and _valid_value(field, snapshot["observed"], following=True),
            "fingerprint": _fingerprint(item_id, row, current),
        })
    return {"item": dict(item), "metadata_rows": result, "metadata_limited": len(rows) > MAX_ROWS}


def parse_form(form) -> dict[str, str]:
    required = {"source_key", "field_name", "decision", "fingerprint"}
    if set(form) - required - {"_csrf"}:
        raise ReviewError(i18n.gettext("Submit only the metadata review form; source values cannot be supplied"))
    values = {}
    for key in required:
        if len(form.getlist(key)) != 1 or not isinstance(form.get(key), str):
            raise ReviewError(i18n.gettext("Submit each metadata review field exactly once"))
        values[key] = form[key]
    return values


def resolve_field(db, item_id: int, source_key: str, field_name: str,
                  decision: str, fingerprint: str) -> None:
    """Resolve ONE existing field under the caller's write lock (G3/G18).

    No source key is adopted and no other source's baseline is changed. The
    source observation time stays intact: a user decision is not a new fetch.
    The caller must let errors escape its transaction so all writes roll back.
    """
    if not _source_key(source_key):
        raise ReviewError(i18n.gettext("Choose a valid saved metadata source"))
    if field_name not in RESOLVABLE_FIELDS:
        raise ReviewError(i18n.gettext("This field is read-only in metadata review; edit it on the item instead"))
    if decision not in {"keep_local", "follow_source"}:
        raise ReviewError(i18n.gettext("Choose Keep local or Follow source"))
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        raise ReviewError(i18n.gettext("Reload Metadata sources before resolving this field"))
    item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise ReviewError(i18n.gettext("Item not found"), 404)
    row = db.execute(
        "SELECT source_key, field_name, value_json, observed_at FROM item_metadata_provenance "
        "WHERE item_id = ? AND source_key = ? AND field_name = ?",
        (item_id, source_key, field_name),
    ).fetchone()
    if row is None:
        raise ReviewError(i18n.gettext("This saved source observation no longer exists; reload Metadata sources"), 409)
    snapshot = _snapshot(row["value_json"], field_name)
    current = item[field_name]
    if (snapshot is None or not _valid_value(field_name, current)
            or not isinstance(row["observed_at"], str) or len(row["observed_at"]) > 64):
        raise ReviewError(i18n.gettext("This saved source value is not safe to resolve; edit the item directly"))
    if not hmac.compare_digest(fingerprint, _fingerprint(item_id, row, current)):
        raise ReviewError(i18n.gettext("The local value or source observation changed; reload Metadata sources and review again"), 409)
    saved = {"observed": snapshot["observed"], "local": current, "overridden": True}
    if decision == "follow_source":
        if not _valid_value(field_name, snapshot["observed"], following=True):
            raise ReviewError(i18n.gettext("The source must provide a non-empty title before you can follow it"))
        try:
            item_write.update_item_fields(db, item_id, {field_name: snapshot["observed"]})
        except (item_write.ItemValueError, sqlite3.IntegrityError):
            raise ReviewError(i18n.gettext("This source value cannot be stored; edit the item directly instead")) from None
        stored = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()[field_name]
        saved.update(local=stored, applied=stored, overridden=False, follow_source=True)
    db.execute(
        "UPDATE item_metadata_provenance SET value_json = ? "
        "WHERE item_id = ? AND source_key = ? AND field_name = ?",
        (json.dumps(saved, ensure_ascii=False, allow_nan=False), item_id, source_key, field_name),
    )
