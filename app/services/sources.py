"""Connected-library instances, safe snapshots and conservative import sync.

No remote writes, title matching, ownership changes or automatic restores. The
short lease claim and final compare-and-swap are separate write transactions;
network I/O and credential decryption happen with no SQLite write lock (G3/G18).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx

from app import crypto
from app.config import MEDIA_TYPES
from app.database import get_db
from app.services import item_write, metadata_policy
from app.services.isbn import canonical_isbn_pair
from app.services.source_connectors import PROVIDERS, get_provider
from app.services.source_connectors.base import (
    Catalogue, ConnectorError, SourceRecord, contains_secret, credential_secrets, safe_code, safe_url, validate_root_url,
)

MAX_RECORDS = 5000
MAX_INSTANCES = 100
SYNC_SECONDS = 120
LEASE_SECONDS = 180
_COUNTS = ("fetched", "added", "linked", "updated", "unavailable", "in_trash", "skipped")
_DESCRIPTIVE = frozenset({
    "description", "subtitle", "publisher", "published_date", "language",
    "series_name", "series_position", "duration", "year", "genres",
    "publish_year", "duration_seconds", "studios", "jellyfin_type",
    "pages", "page_count",
})
_PROVIDER_ID_NAMES = {
    name.casefold(): name for name in (
        "Imdb", "Tmdb", "Tvdb", "MusicBrainzAlbum", "MusicBrainzRelease",
        "MusicBrainzReleaseGroup", "MusicBrainzArtist", "MusicBrainzAlbumArtist",
        "MusicBrainzTrack", "MusicBrainzRecording", "MusicBrainzReleaseTrack",
        "Isbn", "Isbn13", "Isbn10", "Asin",
    )
}
_UUID_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_TEXT_FIELDS = {"description": 8000, "subtitle": 2000, "publisher": 1000,
                "language": 100, "series_name": 1000}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _string(value, maximum: int, *, required=False) -> str:
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(c) == 0 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or (required and not value.strip())):
        raise ConnectorError("invalid_config")
    return value.strip()


def _options(provider: str, options) -> dict:
    if isinstance(options, str):
        if len(options) > 8192:
            raise ConnectorError("invalid_options")
        try:
            options = json.loads(options)
        except (ValueError, RecursionError):
            raise ConnectorError("invalid_options") from None
    if not isinstance(options, dict):
        raise ConnectorError("invalid_options")
    allowed = {"media_type", "max_pages", "max_entries", "max_depth"} if provider == "opds" else {
        "library_ids", "user_id",
    } if provider == "jellyfin" else set()
    if set(options) - allowed:
        raise ConnectorError("invalid_options")
    for key, value in options.items():
        if key in {"max_pages", "max_entries", "max_depth", "page_size"}:
            ceiling = {"max_pages": 50, "max_entries": MAX_RECORDS, "max_depth": 4, "page_size": 500}[key]
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ConnectorError("invalid_options")
        elif key == "media_type":
            if not isinstance(value, str) or value not in {"book", "ebook", "audiobook", "comic", "manga"}:
                raise ConnectorError("invalid_options")
        elif key in {"library_ids", "media_types"}:
            if (not isinstance(value, list) or len(value) > 100
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in value)):
                raise ConnectorError("invalid_options")
            if key == "media_types" and any(v not in MEDIA_TYPES for v in value):
                raise ConnectorError("invalid_options")
        elif key == "user_id":
            _string(value, 200, required=True)
    return dict(options)


def _scope_options(provider: str, options: dict) -> dict:
    """Compare effective scopes/limits, not JSON spelling or library ordering."""
    if provider == "opds":
        return {"media_type": "ebook", "max_pages": 20, "max_entries": 2000,
                "max_depth": 2, **options}
    if provider == "jellyfin":
        return {"user_id": options.get("user_id"),
                "library_ids": sorted(set(options.get("library_ids", [])))}
    return {}


def _encrypted_credentials(provider: str, credentials: dict | None) -> str:
    if credentials is None:
        credentials = {}
    if not isinstance(credentials, dict) or set(credentials) - set(get_provider(provider).credential_fields):
        raise ConnectorError("invalid_credentials")
    if any(not isinstance(v, str) or len(v) > 4096
           or any(ord(c) == 0 or 0xD800 <= ord(c) <= 0xDFFF for c in v) for v in credentials.values()):
        raise ConnectorError("invalid_credentials")
    if provider == "opds" and (":" in credentials.get("username", "") or len(credentials.get("username", "")) > 512):
        raise ConnectorError("invalid_credentials")
    values = {k: v for k, v in credentials.items() if v}
    return crypto.encrypt_value(json.dumps(values), crypto.get_encryption_key()) if values else ""


def _credentials(source: dict) -> dict:
    encrypted = source.get("credentials_enc") or ""
    if not encrypted:
        return {}
    # May log a key-mismatch warning: deliberately outside a write transaction.
    value = crypto.decrypt_value(encrypted, crypto.get_encryption_key(), key_name="source_credentials")
    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        raise ConnectorError("credentials_unavailable") from None
    if not isinstance(result, dict):
        raise ConnectorError("credentials_unavailable")
    return result


def _summary(raw: str | dict) -> dict:
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    summary = {key: data[key] for key in _COUNTS if type(data.get(key)) is int and 0 <= data[key] <= 10**9}
    summary["warnings"] = list(dict.fromkeys(safe_code(c) for c in data.get("warnings", [])[:20])) if isinstance(data.get("warnings"), list) else []
    return summary


def _dto(row) -> dict:
    provider = PROVIDERS.get(row["provider"])
    return {
        "id": row["id"], "provider": row["provider"] if provider else "unknown",
        "provider_label": provider.label if provider else "Unknown",
        "name": row["name"], "enabled": bool(row["enabled"]),
        "syncable": bool(provider and provider.syncable),
        "manual_links": bool(provider and provider.manual_links),
        "last_status": row["last_status"] if row["last_status"] in {"never", "ok", "partial", "error", "syncing"} else "error",
        "last_error": safe_code(row["last_error"]) if row["last_error"] else None,
        "last_attempt_at": row["last_attempt_at"], "last_sync_at": row["last_sync_at"],
        "summary": _summary(row["last_summary_json"]),
    }


def list_instances(db) -> list[dict]:
    """Viewer-safe overview: no roots, options, usernames, tokens or ciphertext."""
    rows = db.execute(
        "SELECT id, provider, name, enabled, last_status, last_error, "
        "last_attempt_at, last_sync_at, last_summary_json FROM source_instances ORDER BY name, id"
    ).fetchall()
    return [_dto(row) for row in rows]


def get_instance(db, source_id: int) -> dict | None:
    """Administrator configuration DTO. Credentials are represented only by bool."""
    row = db.execute("SELECT * FROM source_instances WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        return None
    result = _dto(row)
    try:
        options = _options(row["provider"], row["options_json"])
    except ConnectorError:
        options = {}
    result.update(base_url=safe_url(row["base_url"]) or "", browser_url=safe_url(row["browser_url"]) or "",
                  options=options, options_json=json.dumps(options, ensure_ascii=False),
                  credentials_saved=bool(row["credentials_enc"]), config_revision=row["config_revision"])
    return result


def list_records(db, source_id: int, *, limit: int = 2000) -> list[dict]:
    """Remote snapshots of live catalogue rows only; never expose metadata blobs."""
    rows = db.execute(
        "SELECT r.id, r.source_id, r.item_id, r.external_id, r.library_id, "
        "r.title, r.media_type, r.format, r.open_url, r.availability, r.last_seen_at, "
        "i.title AS item_title FROM source_records r JOIN items_live i ON i.id = r.item_id "
        "WHERE r.source_id = ? ORDER BY r.title, r.id LIMIT ?", (source_id, min(max(limit, 1), 5000)),
    ).fetchall()
    result = []
    for row in rows:
        record = dict(row)
        record["open_url"] = safe_url(record["open_url"])
        result.append(record)
    return result


def list_item_records(db, item_id: int) -> list[dict]:
    """Safe item-page availability cards from enabled instances only."""
    rows = db.execute(
        "SELECT r.source_id, r.format, r.open_url, r.availability, r.last_seen_at, "
        "s.name, s.provider, s.last_sync_at FROM source_records r "
        "JOIN source_instances s ON s.id = r.source_id JOIN items_live i ON i.id = r.item_id "
        "WHERE r.item_id = ? AND s.enabled = 1 ORDER BY s.name, r.id", (item_id,),
    ).fetchall()
    return [{**dict(row), "open_url": safe_url(row["open_url"])} for row in rows]


def create_instance(db, *, provider: str, name: str, base_url: str = "", browser_url: str = "",
                    credentials: dict | None = None, options=None, enabled: bool = True) -> int:
    spec = get_provider(provider)
    name = _string(name, 200, required=True)
    root = validate_root_url(base_url, optional=not spec.syncable)
    browser = validate_root_url(browser_url, optional=True)
    parsed_options = _options(provider, {} if options is None else options)
    encrypted = _encrypted_credentials(provider, credentials)
    if type(enabled) is not bool:
        raise ConnectorError("invalid_config")
    if db.execute("SELECT COUNT(*) FROM source_instances").fetchone()[0] >= MAX_INSTANCES:
        raise ConnectorError("limit_reached")
    cursor = db.execute(
        "INSERT INTO source_instances (provider, name, base_url, browser_url, credentials_enc, options_json, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (provider, name, root, browser, encrypted, json.dumps(parsed_options), int(enabled)),
    )
    return cursor.lastrowid


def save_settings(db, source_id: int, *, name: str, base_url: str = "", browser_url: str = "",
                  credentials: dict | None = None, clear_credentials: bool = False,
                  options=None, enabled: bool = True) -> None:
    """Replace configuration under the caller's write lock.

    Blank credential fields preserve the saved secret unless explicitly cleared.
    Any explicit nonblank replacement invalidates cached health (even a repeat:
    decryption must not log under this lock). Roots, effective scope/limits and
    browser roots do likewise for syncable sources. New server identities should
    use a NEW INSTANCE: editing a root intentionally keeps external-ID mappings.
    Manual links are independent URLs, so re-enabling them never requires sync.
    """
    row = db.execute("SELECT * FROM source_instances WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise ConnectorError("not_found")
    spec = get_provider(row["provider"])
    name = _string(name, 200, required=True)
    root = validate_root_url(base_url, optional=not spec.syncable)
    browser = validate_root_url(browser_url, optional=True)
    parsed_options = _options(spec.id, {} if options is None else options)
    if type(enabled) is not bool or type(clear_credentials) is not bool:
        raise ConnectorError("invalid_config")
    encrypted = "" if clear_credentials else row["credentials_enc"]
    if credentials is not None:
        replacement = _encrypted_credentials(spec.id, credentials)
        if replacement:
            encrypted = replacement
    try:
        old_scope = _scope_options(spec.id, _options(spec.id, row["options_json"]))
    except ConnectorError:
        old_scope = None
    material = spec.syncable and (
        root != row["base_url"] or browser != (row["browser_url"] or "")
        or _scope_options(spec.id, parsed_options) != old_scope
        or (encrypted or "") != (row["credentials_enc"] or "")
    )
    now = _now()
    db.execute(
        "UPDATE source_instances SET name = ?, base_url = ?, browser_url = ?, credentials_enc = ?, "
        "options_json = ?, enabled = ?, config_revision = config_revision + 1, sync_token = NULL, "
        "last_status = CASE WHEN ? OR last_status = 'syncing' THEN 'never' ELSE last_status END, "
        "last_error = CASE WHEN ? OR last_status = 'syncing' THEN NULL ELSE last_error END, "
        "last_sync_at = CASE WHEN ? THEN NULL ELSE last_sync_at END, "
        "last_attempt_at = CASE WHEN ? THEN NULL ELSE last_attempt_at END, "
        "last_summary_json = CASE WHEN ? THEN '{}' ELSE last_summary_json END, updated_at = ? WHERE id = ?",
        (name, root, browser, encrypted, json.dumps(parsed_options), int(enabled),
         *([int(material)] * 5), now, source_id),
    )
    if material:
        db.execute("UPDATE source_records SET availability = 'unknown', updated_at = ? "
                   "WHERE source_id = ? AND availability != 'unknown'", (now, source_id))


def disable_instance(db, source_id: int) -> None:
    cursor = db.execute(
        "UPDATE source_instances SET enabled = 0, config_revision = config_revision + 1, "
        "sync_token = NULL, last_status = CASE WHEN last_status = 'syncing' THEN 'never' ELSE last_status END, "
        "updated_at = ? WHERE id = ?", (_now(), source_id),
    )
    if not cursor.rowcount:
        raise ConnectorError("not_found")


def _read_source(source_id: int) -> dict:
    with get_db() as db:
        row = db.execute("SELECT * FROM source_instances WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise ConnectorError("not_found")
    source = dict(row)
    source["options"] = _options(source["provider"], source["options_json"])
    return source


async def _fetch(source: dict, credentials: dict) -> Catalogue:
    async with asyncio.timeout(SYNC_SECONDS):
        catalogue = await get_provider(source["provider"]).fetch_catalogue(source, credentials)
    if not isinstance(catalogue, Catalogue) or not isinstance(catalogue.records, list):
        raise ConnectorError("malformed_response")
    return catalogue


async def test_instance(source_id: int) -> dict:
    """Read remote catalogue without importing or changing sync timestamps."""
    try:
        source = _read_source(source_id)
        if not source["enabled"]:
            raise ConnectorError("disabled")
        if not get_provider(source["provider"]).syncable:
            raise ConnectorError("manual_only")
        catalogue = await _fetch(source, _credentials(source))
        return {"ok": True, "status": "ok" if catalogue.complete else "partial",
                "count": min(len(catalogue.records), MAX_RECORDS)}
    except (ConnectorError, TimeoutError, httpx.HTTPError) as exc:
        return {"ok": False, "code": _exception_code(exc)}
    except Exception:
        return {"ok": False, "code": "unexpected_error"}


def _exception_code(exc) -> str:
    if isinstance(exc, ConnectorError):
        return safe_code(exc.code)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return "unreachable"
    return "unexpected_error"


def _claim(source_id: int) -> dict:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM source_instances WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise ConnectorError("not_found")
        if not row["enabled"]:
            raise ConnectorError("disabled")
        if not get_provider(row["provider"]).syncable:
            raise ConnectorError("manual_only")
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        now = _now()
        if row["sync_token"] and row["last_attempt_at"] and cutoff < row["last_attempt_at"] <= now:
            raise ConnectorError("busy")
        token, now = uuid.uuid4().hex, _now()
        db.execute(
            "UPDATE source_instances SET sync_token = ?, last_status = 'syncing', "
            "last_attempt_at = ?, last_error = NULL, updated_at = ? WHERE id = ?",
            (token, now, now, source_id),
        )
        source = dict(row)
        source["sync_token"] = token
    return source


def _same_config(row, source: dict) -> bool:
    return bool(row and row["enabled"] and row["config_revision"] == source["config_revision"]
                and row["sync_token"] == source["sync_token"])


def _failure(source: dict, code: str) -> dict:
    code = safe_code(code)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM source_instances WHERE id = ?", (source["id"],)).fetchone()
        if not _same_config(row, source):
            return {"ok": False, "code": "stale_config"}
        db.execute(
            "UPDATE source_instances SET sync_token = NULL, last_status = 'error', last_error = ?, "
            "updated_at = ? WHERE id = ?", (code, _now(), source["id"]),
        )
    return {"ok": False, "status": "error", "code": code}


def _record(record: SourceRecord, secrets: list[str]) -> SourceRecord:
    if not isinstance(record, SourceRecord):
        raise ConnectorError("invalid_record")
    if (not isinstance(record.external_id, str) or not record.external_id.strip()
            or len(record.external_id) > 512
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in record.external_id)
            or record.media_type not in MEDIA_TYPES):
        raise ConnectorError("invalid_record")
    title, authors = _string(record.title, 2000, required=True), _string(record.authors, 4096)
    isbn = None
    if isinstance(record.isbn, str) and re.fullmatch(r"[0-9Xx -]+", record.isbn):
        pair = canonical_isbn_pair(record.isbn)
        isbn = pair[0] if pair else None
    metadata = {}
    if isinstance(record.metadata, dict):
        for key in _DESCRIPTIVE:
            value = record.metadata.get(key)
            if isinstance(value, str):
                metadata[key] = "".join(c for c in value[:8000] if ord(c) != 0 and not 0xD800 <= ord(c) <= 0xDFFF)
            elif type(value) in {int, float} and abs(value) < 10**9:
                metadata[key] = value
            elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                metadata[key] = ["".join(c for c in v[:200] if ord(c) != 0 and not 0xD800 <= ord(c) <= 0xDFFF)
                                 for v in value[:50]]
        identifiers = _provider_ids(record.metadata.get("provider_ids"))
        if identifiers:
            metadata["provider_ids"] = identifiers
    open_url = safe_url(record.open_url)
    # Do not persist reflected secrets in apparently descriptive fields/links,
    # even when the server URL/HTML-encodes them. Check values before JSON can
    # escape a quote in a password and hide it from a plain substring test.
    candidates = [record.external_id, title, authors, open_url, record.library_id, record.format,
                  record.isbn, isbn]
    for value in metadata.values():
        candidates.extend(value.values() if isinstance(value, dict) else value if isinstance(value, list) else [value])
    raw_ids = record.metadata.get("provider_ids") if isinstance(record.metadata, dict) else None
    if isinstance(raw_ids, dict) and len(raw_ids) <= 64:
        candidates.extend(v for k, v in raw_ids.items() if isinstance(k, str)
                          and k.casefold().replace("_", "").replace("-", "") in _PROVIDER_ID_NAMES)
    if any(contains_secret(candidate, secrets) for candidate in candidates):
        raise ConnectorError("invalid_record")
    return replace(record, title=title, authors=authors, isbn=isbn, open_url=open_url,
                   library_id=_string(record.library_id, 200) if record.library_id is not None else None,
                   format=_string(record.format, 100) if record.format is not None else None, metadata=metadata)


def _provider_ids(raw) -> dict[str, str]:
    """Keep known, shaped identifiers only, never arbitrary nested provider JSON."""
    if not isinstance(raw, dict) or len(raw) > 64:
        return {}
    result = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str) or len(value) > 128:
            continue
        name = _PROVIDER_ID_NAMES.get(key.casefold().replace("_", "").replace("-", ""))
        if not name:
            continue
        if name.startswith("Isbn"):
            if not re.fullmatch(r"[0-9Xx -]+", value):
                continue
            pair = canonical_isbn_pair(value)
            if pair is None:
                continue
            if name == "Isbn10":
                value = pair[1]
                if value is None:
                    continue
            else:
                value = pair[0]
        elif name in {"Tmdb", "Tvdb"}:
            if not re.fullmatch(r"[0-9]{1,12}", value):
                continue
        elif name == "Imdb":
            if not re.fullmatch(r"tt[0-9]{1,12}", value):
                continue
        elif name.startswith("MusicBrainz"):
            if not _UUID_ID.fullmatch(value):
                continue
        elif name == "Asin":
            if not re.fullmatch(r"[A-Za-z0-9]{10}", value):
                continue
            value = value.upper()
        result[name] = value
    return result


def metadata_source_key(source_id: int, external_id: str) -> str:
    """Stable instance + opaque identity, safe for review URLs and <= 256 chars.

    Hashing retains exact identity (including Unicode/reserved characters) without
    copying credential-looking IDs or URL-shaped IDs into the review surface.
    """
    return f"source:{source_id}:{hashlib.sha256(external_id.encode('utf-8')).hexdigest()}"


def _number(value, maximum: int, *, integral=True):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value):
        value = int(value)
    if (type(value) not in {int, float} or not math.isfinite(value)
            or not 0 <= value <= maximum or (integral and int(value) != value)):
        return None
    return int(value) if integral else value


def _catalogue_fields(record: SourceRecord) -> dict:
    """Validated descriptive projection; identities and personal state stay out."""
    fields = {"title": record.title, "authors": record.authors[:2000] or None}
    metadata = record.metadata
    for name, limit in _TEXT_FIELDS.items():
        value = metadata.get(name)
        if isinstance(value, str):
            value = "".join(c for c in value[:limit] if
                            (ord(c) >= 32 or c in "\t\r\n") and not 127 <= ord(c) <= 159).strip()
            fields[name] = value or None
    for key in ("publish_year", "year"):
        year = _number(metadata.get(key), 9999)
        if year:
            fields["publish_year"] = year
            break
    if "publish_year" not in fields and isinstance(metadata.get("published_date"), str):
        date = metadata["published_date"]
        year = _number(date, 9999)
        if not year:
            try:
                year = datetime.fromisoformat(date.replace("Z", "+00:00")).year
            except ValueError:
                year = None
        if year:
            fields["publish_year"] = year
    for target, keys, maximum, integral in (
        ("page_count", ("page_count", "pages"), 1000000, True),
        ("series_position", ("series_position",), 1000000, False),
        ("duration_mins", ("duration_seconds", "duration"), 60000000, False),
    ):
        for key in keys:
            value = _number(metadata.get(key), maximum, integral=integral)
            if value is not None:
                fields[target] = int(value // 60) if target == "duration_mins" else value
                break
    return fields


def _prepare(catalogue: Catalogue, credentials: dict) -> Catalogue:
    warnings = [safe_code(c) for c in catalogue.warnings[:20]] if isinstance(catalogue.warnings, list) else ["incomplete"]
    if len(catalogue.records) > MAX_RECORDS:
        warnings.append("limit_reached")
    records, seen = [], set()
    secrets = credential_secrets(credentials)
    for raw in catalogue.records[:MAX_RECORDS]:
        try:
            record = _record(raw, secrets)
        except (ConnectorError, TypeError, ValueError):
            warnings.append("invalid_record")
            continue
        if record.external_id in seen:
            warnings.append("duplicate_record")
            continue
        seen.add(record.external_id)
        records.append(record)
    return Catalogue(records, catalogue.complete is True and not warnings, list(dict.fromkeys(warnings)))


def _snapshot(db, source_id: int, item_id: int, record: SourceRecord, now: str):
    db.execute(
        "INSERT INTO source_records (source_id, item_id, external_id, library_id, title, media_type, "
        "format, open_url, availability, last_seen_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'available', ?, ?) "
        "ON CONFLICT(source_id, external_id) DO UPDATE SET library_id = excluded.library_id, "
        "title = excluded.title, media_type = excluded.media_type, format = excluded.format, "
        "open_url = excluded.open_url, availability = 'available', last_seen_at = excluded.last_seen_at, "
        "metadata_json = excluded.metadata_json, updated_at = excluded.last_seen_at",
        (source_id, item_id, record.external_id, record.library_id, record.title, record.media_type,
         record.format, record.open_url, now, json.dumps(record.metadata, ensure_ascii=False)),
    )


def _persist_record(db, source: dict, record: SourceRecord, now: str) -> str:
    # The identity lives in source_records, not items. A LEFT JOIN to the live
    # view sees an attachment to Trash without reading the physical item table.
    attached = db.execute(
        "SELECT r.item_id, i.id AS live_id FROM source_records r "
        "LEFT JOIN items_live i ON i.id = r.item_id WHERE r.source_id = ? AND r.external_id = ?",
        (source["id"], record.external_id),
    ).fetchone()
    fields = _catalogue_fields(record)
    action = "updated"
    if attached:
        if attached["live_id"] is None:
            return "in_trash"
        item_id = attached["item_id"]
    else:
        matches = []
        if record.isbn:
            pair = canonical_isbn_pair(record.isbn)
            matches = db.execute(
                "SELECT id FROM items_live WHERE media_type = ? AND "
                "(isbn IN (?, ?) OR isbn10 IN (?, ?)) LIMIT 3",
                (record.media_type, pair[0], pair[1], pair[0], pair[1]),
            ).fetchall()
        if len(matches) > 1:
            raise ConnectorError("ambiguous_isbn")
        if matches:
            item_id, action = matches[0]["id"], "linked"
        else:
            item_id = item_write.insert_item(db, {
                **fields, "media_type": record.media_type, "isbn": record.isbn,
                "source": "connected_library", "owned": 0, "wishlisted": False,
            }, restore_trashed=False)
            action = "added"
    source_key = metadata_source_key(source["id"], record.external_id)
    # Subtitle is a creation-only projection until the shared policy supports it.
    # Identifiers/media type are observed for review but NEVER refreshed here.
    descriptive = {name: value for name, value in fields.items() if name in metadata_policy.METADATA_FIELDS}
    observed = {**descriptive, "isbn": record.isbn, "media_type": record.media_type}
    if action == "added":
        metadata_policy.record_initial(db, item_id, source_key, observed)
    else:
        updates = metadata_policy.metadata_updates(db, item_id, source_key, descriptive)
        if updates:
            item_write.update_item_fields(db, item_id, updates)
        metadata_policy.record_observation(db, item_id, source_key, observed, applied_fields=updates)
    _snapshot(db, source["id"], item_id, record, now)
    return action


def _apply(source: dict, catalogue: Catalogue) -> dict:
    counts = {key: 0 for key in _COUNTS}
    counts["fetched"] = len(catalogue.records)
    warnings = list(catalogue.warnings)
    seen, now = set(), _now()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM source_instances WHERE id = ?", (source["id"],)).fetchone()
        if not _same_config(row, source):
            return {"ok": False, "code": "stale_config"}
        for record in catalogue.records:
            seen.add(record.external_id)
            # Invalid provider metadata must not leave half an item/provenance
            # row behind or poison other valid entries in this same catalogue.
            db.execute("SAVEPOINT source_entry")
            try:
                action = _persist_record(db, source, record, now)
            except item_write.IdentifierInTrash:
                db.execute("ROLLBACK TO source_entry")
                counts["in_trash"] += 1
            except (ConnectorError, ValueError, sqlite3.IntegrityError, sqlite3.DataError, OverflowError) as exc:
                db.execute("ROLLBACK TO source_entry")
                counts["skipped"] += 1
                warnings.append(exc.code if isinstance(exc, ConnectorError) else "invalid_record")
            else:
                counts[action] += 1
            finally:
                db.execute("RELEASE source_entry")
        complete = catalogue.complete and not warnings
        if complete:
            missing = db.execute(
                "SELECT r.id, r.external_id FROM source_records r JOIN items_live i ON i.id = r.item_id "
                "WHERE r.source_id = ? AND r.availability != 'unavailable'", (source["id"],),
            ).fetchall()
            for record in missing:
                if record["external_id"] not in seen:
                    db.execute("UPDATE source_records SET availability = 'unavailable', updated_at = ? WHERE id = ?",
                               (now, record["id"]))
                    counts["unavailable"] += 1
        status = "ok" if complete else "partial"
        summary = {**counts, "warnings": list(dict.fromkeys(warnings))[:20]}
        # A partial traversal is not a successful enumeration. Preserve the last
        # complete success separately from last_attempt_at and the partial report.
        db.execute(
            "UPDATE source_instances SET sync_token = NULL, last_status = ?, last_error = NULL, "
            "last_sync_at = CASE WHEN ? THEN ? ELSE last_sync_at END, last_summary_json = ?, "
            "updated_at = ? WHERE id = ?",
            (status, int(complete), now, json.dumps(summary), now, source["id"]),
        )
    return {"ok": True, "status": status, "summary": summary}


async def sync_instance(source_id: int) -> dict:
    try:
        source = _claim(source_id)
    except ConnectorError as exc:
        return {"ok": False, "code": exc.code}
    try:
        source["options"] = _options(source["provider"], source["options_json"])
        credentials = _credentials(source)
        catalogue = _prepare(await _fetch(source, credentials), credentials)
        return _apply(source, catalogue)
    except asyncio.CancelledError:
        _failure(source, "interrupted")
        raise
    except Exception as exc:
        return _failure(source, _exception_code(exc))


def attach_record(db, source_id: int, *, item_id: int, external_id: str, open_url: str,
                  library_id: str | None = None, format: str | None = None) -> int:
    """Administrator-confirmed availability link; only the manual provider."""
    source = db.execute("SELECT * FROM source_instances WHERE id = ?", (source_id,)).fetchone()
    if source is None:
        raise ConnectorError("not_found")
    if not source["enabled"]:
        raise ConnectorError("disabled")
    if not get_provider(source["provider"]).manual_links:
        raise ConnectorError("manual_only")
    item = db.execute("SELECT id, title, media_type FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise ConnectorError("not_found")
    url = safe_url(open_url)
    if not url:
        raise ConnectorError("invalid_url")
    record = _record(SourceRecord(external_id, item["title"], item["media_type"],
                                  library_id=library_id, format=format, open_url=url), [])
    attached = db.execute("SELECT id, item_id FROM source_records WHERE source_id = ? AND external_id = ?",
                          (source_id, external_id)).fetchone()
    if attached and attached["item_id"] != item_id:
        raise ConnectorError("record_conflict")
    _snapshot(db, source_id, item_id, record, _now())
    return db.execute("SELECT id FROM source_records WHERE source_id = ? AND external_id = ?",
                      (source_id, external_id)).fetchone()[0]
