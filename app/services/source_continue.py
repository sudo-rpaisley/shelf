"""Account-owned native Continue cache; never modifies personal/catalogue state.

GET context/handoff reads are local-only. Explicit POSTs and separately consented
schedules verify self identity and replace one bounded snapshot atomically. The
existing progress import account schema, baselines and history are independent.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote

from app import config, crypto, media_presentation
from app.database import get_db
from app.services import continue_connectors as connectors
from app.services.media_cards import build_media_card
from app.services.source_connectors.base import ConnectorError, contains_secret, safe_url, validate_root_url
from app.services.source_progress import (
    ProgressError, SCHEDULE_INTERVALS, _actor, _client, _enabled, _hash, _identity, _json, _now, _token,
)


CONSENT_VERSION = "native_continue_v1"
SCHEDULE_CONSENT_VERSION = "scheduled_continue_v1"
MAX_CONNECTIONS = 64
SYNC_SECONDS = 60
LEASE_SECONDS = 90
STALE_SECONDS = 24 * 3600
PROVIDERS = {"jellyfin": "Jellyfin", "abs": "Audiobookshelf", "komga": "Komga", "romm": "RomM"}


def _read(db, key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _policy(db, key, *, mapping=False, numeric=False):
    try:
        value = json.loads(_read(db, key, "{}" if mapping else "[]"))
        if mapping:
            if not isinstance(value, dict) or len(value) > 1000:
                raise ValueError
            for library_id, kind in value.items():
                _identity(library_id)
                if kind not in {"comic", "manga"}:
                    raise ValueError
        else:
            if not isinstance(value, list) or len(value) > 1000:
                raise ValueError
            if numeric:
                # Existing RomM selections may contain integer IDs. Normalize
                # only actual positive provider integers/canonical digit text,
                # not bool/float/blank values into an accidentally open scope.
                normalized = []
                for entry in value:
                    if type(entry) is int and 0 < entry <= 2**63 - 1:
                        entry = str(entry)
                    if (not isinstance(entry, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", entry)
                            or int(entry) > 2**63 - 1):
                        raise ValueError
                    normalized.append(entry)
                value = sorted(set(normalized))
            else:
                value = sorted({_identity(entry) for entry in value})
        return value
    except (ValueError, TypeError, RecursionError, ProgressError):
        raise ProgressError("relink_required") from None


def _source(db, key):
    """Whitelist only roots/policy/health. No catalogue credential column/read."""
    if not _enabled(db, "connected_library"):
        raise ProgressError("disabled")
    if key in {"abs", "komga", "romm"}:
        if not _enabled(db, "abs_sync" if key == "abs" else key):
            raise ProgressError("disabled")
        root = _read(db, f"{key}_url")
        if key == "abs":
            root = config.get_setting_value("abs_url", root)
        excluded = _policy(db, f"{key}_excluded_" + ("platforms" if key == "romm" else "libraries"), numeric=key == "romm")
        kinds = _policy(db, "komga_library_kinds", mapping=True) if key == "komga" else {}
        source = {"key": key, "provider": key, "name": PROVIDERS[key], "root": root,
                  "browser": _read(db, f"{key}_public_url") or root, "excluded": excluded, "kinds": kinds,
                  "revision": _hash([excluded, kinds])}
        if _read(db, f"connection.{key}.last_status") in {"error", "partial"}:
            raise ProgressError("unavailable")
    elif isinstance(key, str) and re.fullmatch(r"source:[1-9][0-9]{0,18}", key):
        source_id = int(key.partition(":")[2])
        if source_id > 2**63 - 1:
            raise ProgressError("not_found")
        row = db.execute("SELECT id, name, provider, base_url, browser_url, enabled, config_revision, "
                         "options_json, last_status, last_error FROM source_instances WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise ProgressError("not_found")
        if row["provider"] != "jellyfin":
            raise ProgressError("unsupported")
        if not row["enabled"]:
            raise ProgressError("disabled")
        if row["last_status"] in {"error", "partial"} or row["last_error"]:
            raise ProgressError("unavailable")
        try:
            libraries = json.loads(row["options_json"]).get("library_ids", [])
            if not isinstance(libraries, list) or len(libraries) > 100:
                raise ValueError
            libraries = list(dict.fromkeys(_identity(value) for value in libraries))
        except (ValueError, TypeError, AttributeError, RecursionError, ProgressError):
            raise ProgressError("relink_required") from None
        source = {"key": key, "id": source_id, "provider": "jellyfin", "name": row["name"],
                  "root": row["base_url"], "browser": row["browser_url"] or row["base_url"],
                  "revision": row["config_revision"], "libraries": libraries}
    else:
        raise ProgressError("unsupported")
    try:
        for field in ("root", "browser"):
            source[field] = validate_root_url(source[field]).rstrip("/")
            if not safe_url(source[field]):
                raise ConnectorError("invalid_url")
    except ConnectorError:
        raise ProgressError("relink_required") from None
    source["fingerprint"] = _hash([key, source["provider"], source["root"], source["browser"], source["revision"]])
    return source


def sources(db):
    result = []
    for key in [f"source:{row[0]}" for row in db.execute("SELECT id FROM source_instances ORDER BY id LIMIT 100")] + ["abs", "komga", "romm"]:
        try:
            source = _source(db, key)
        except ProgressError:
            continue
        result.append({"key": key, "name": source["name"], "provider": source["provider"]})
    return result


def _check_cancelled():
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


def _link_generation(db, user_id, key):
    generation = secrets.token_hex(32)
    db.execute("INSERT INTO user_continue_link_state (user_id, source_key, generation) VALUES (?, ?, ?) "
               "ON CONFLICT(user_id, source_key) DO UPDATE SET generation = excluded.generation",
               (user_id, key, generation))
    return generation


def _ownership(db, user_id, key, remote_user_id):
    for table in ("user_continue_connections", "user_source_accounts"):
        row = db.execute(f"SELECT user_id FROM {table} WHERE source_key = ? AND remote_user_id = ?",
                         (key, remote_user_id)).fetchone()
        if row and row[0] != user_id:
            raise ProgressError("account_in_use")


async def link(user_id, source_key, *, token, remote_user_id, consent, actor=None):
    if consent != CONSENT_VERSION:
        raise ProgressError("consent_required")
    token, expected = _token(token), _identity(remote_user_id)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        actor = _actor(db, user_id, actor)
        source = _source(db, source_key)
        if any(contains_secret(value, [token]) for value in (source["root"], source["browser"], expected)):
            raise ProgressError("invalid_token")
        if (db.execute("SELECT COUNT(*) FROM user_continue_connections WHERE user_id = ?", (user_id,)).fetchone()[0] >= MAX_CONNECTIONS
                and not db.execute("SELECT id FROM user_continue_connections WHERE user_id = ? AND source_key = ?", (user_id, source_key)).fetchone()):
            raise ProgressError("too_large")
        generation = _link_generation(db, user_id, source_key)
    def checkpoint():
        _check_cancelled()
        with get_db() as db:
            _actor(db, user_id, actor)
            row = db.execute("SELECT generation FROM user_continue_link_state WHERE user_id = ? AND source_key = ?", (user_id, source_key)).fetchone()
            if not row or row[0] != generation:
                raise ProgressError("stale_link")
            if _source(db, source_key)["fingerprint"] != source["fingerprint"]:
                raise ProgressError("stale_config")
    async with _client() as client:
        actual, _ = await connectors.who(connectors.Reader(client, source, token, checkpoint))
    checkpoint()
    if actual != expected:
        raise ProgressError("identity_mismatch")
    encrypted = crypto.encrypt_value(token, crypto.get_encryption_key())
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _actor(db, user_id, actor)
        row = db.execute("SELECT generation FROM user_continue_link_state WHERE user_id = ? AND source_key = ?", (user_id, source_key)).fetchone()
        if not row or row[0] != generation:
            raise ProgressError("stale_link")
        if _source(db, source_key)["fingerprint"] != source["fingerprint"]:
            raise ProgressError("stale_config")
        _ownership(db, user_id, source_key, actual)
        if (db.execute("SELECT COUNT(*) FROM user_continue_connections WHERE user_id = ?", (user_id,)).fetchone()[0] >= MAX_CONNECTIONS
                and not db.execute("SELECT id FROM user_continue_connections WHERE user_id = ? AND source_key = ?", (user_id, source_key)).fetchone()):
            raise ProgressError("too_large")
        # A replacement removes only this native cache/automatic consent. No
        # personal state, imported-progress baselines/history or IDs are changed.
        db.execute("DELETE FROM user_continue_connections WHERE user_id = ? AND source_key = ?", (user_id, source_key))
        cursor = db.execute(
            "INSERT INTO user_continue_connections (user_id, source_key, provider, remote_user_id, credentials_enc, "
            "root_fingerprint, revision, consent_version, consented_at, schedule_revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, source_key, source["provider"], actual, encrypted, source["fingerprint"], secrets.token_hex(32),
             CONSENT_VERSION, _now(), secrets.token_hex(32)),
        )
        return cursor.lastrowid


def _connection(db, user_id, connection_id):
    row = db.execute("SELECT * FROM user_continue_connections WHERE id = ? AND user_id = ?", (connection_id, user_id)).fetchone()
    if row is None:
        raise ProgressError("not_found")
    source = _source(db, row["source_key"])
    if source["fingerprint"] != row["root_fingerprint"] or source["provider"] != row["provider"]:
        raise ProgressError("relink_required")
    if row["consent_version"] != CONSENT_VERSION:
        raise ProgressError("consent_required")
    return dict(row), source


def disconnect(db, user_id, connection_id, *, actor=None):
    _actor(db, user_id, actor)
    row = db.execute("SELECT source_key FROM user_continue_connections WHERE id = ? AND user_id = ?", (connection_id, user_id)).fetchone()
    if row is None:
        raise ProgressError("not_found")
    _link_generation(db, user_id, row[0])
    db.execute("DELETE FROM user_continue_connections WHERE id = ? AND user_id = ?", (connection_id, user_id))


def _schedule_actor(user_id, row):
    return {"id": user_id, "username": row["actor_username"], "role": row["actor_role"], "token_version": row["actor_token_version"]}


def set_schedule(db, user_id, connection_id, *, interval_minutes, consent="", actor=None):
    actor = _actor(db, user_id, actor)
    if type(interval_minutes) is not int or interval_minutes not in (0, *SCHEDULE_INTERVALS):
        raise ProgressError("invalid_form")
    if not db.execute("SELECT id FROM user_continue_connections WHERE id = ? AND user_id = ?", (connection_id, user_id)).fetchone():
        raise ProgressError("not_found")
    if interval_minutes:
        if consent != SCHEDULE_CONSENT_VERSION:
            raise ProgressError("consent_required")
        _connection(db, user_id, connection_id)
    db.execute(
        "UPDATE user_continue_connections SET interval_minutes = ?, schedule_revision = ?, schedule_consent = ?, "
        "schedule_consented_at = ?, actor_username = ?, actor_role = ?, actor_token_version = ?, next_due_at = ?, "
        "lease_token = NULL, lease_expires_at = NULL, last_status = CASE WHEN last_status = 'running' THEN 'interrupted' ELSE last_status END "
        "WHERE id = ? AND user_id = ?",
        (interval_minutes, secrets.token_hex(32), SCHEDULE_CONSENT_VERSION if interval_minutes else None,
         _now() if interval_minutes else None, actor["username"], actor["role"], actor["token_version"],
         _now() if interval_minutes else None, connection_id, user_id),
    )


def _claim(user_id, connection_id, actor, automatic):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row, source = _connection(db, user_id, connection_id)
        if automatic:
            actor = _schedule_actor(user_id, row)
            if (not row["interval_minutes"] or row["schedule_consent"] != SCHEDULE_CONSENT_VERSION
                    or not row["next_due_at"] or row["next_due_at"] > _now()):
                raise ProgressError("stale_schedule")
        actor = _actor(db, user_id, actor)
        now = _now()
        if row["lease_token"] and (row["lease_expires_at"] or "") > now:
            raise ProgressError("busy")
        if row["last_attempt_at"] and (datetime.fromisoformat(now) - datetime.fromisoformat(row["last_attempt_at"])).total_seconds() < 5:
            raise ProgressError("busy")
        lease = secrets.token_hex(32)
        expires = (datetime.fromisoformat(now) + timedelta(seconds=LEASE_SECONDS)).isoformat(timespec="microseconds")
        due = (datetime.fromisoformat(now) + timedelta(minutes=row["interval_minutes"])).isoformat(timespec="microseconds") if row["interval_minutes"] else None
        db.execute("UPDATE user_continue_connections SET lease_token = ?, lease_expires_at = ?, last_attempt_at = ?, "
                   "next_due_at = ?, last_status = 'running', last_error = NULL WHERE id = ?", (lease, expires, now, due, connection_id))
        return row, source, actor, lease


def _check(db, row, source, actor, lease):
    _check_cancelled()
    _actor(db, row["user_id"], actor)
    current, live = _connection(db, row["user_id"], row["id"])
    if (current["revision"] != row["revision"] or current["schedule_revision"] != row["schedule_revision"]
            or current["lease_token"] != lease or not current["lease_expires_at"] or current["lease_expires_at"] <= _now()):
        raise ProgressError("stale_schedule")
    if live["fingerprint"] != source["fingerprint"]:
        raise ProgressError("stale_config")


def _failure(row, lease, error):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE user_continue_connections SET lease_token = NULL, lease_expires_at = NULL, "
                   "last_status = 'error', last_error = ? WHERE id = ? AND revision = ? AND lease_token = ?",
                   (error, row["id"], row["revision"], lease))


async def refresh(user_id, connection_id, *, actor=None, automatic=False):
    row, source, actor, lease = _claim(user_id, connection_id, actor, automatic)
    def checkpoint():
        with get_db() as db:
            _check(db, row, source, actor, lease)
    try:
        async with asyncio.timeout(SYNC_SECONDS):
            checkpoint()
            if not crypto.is_fernet_token(row["credentials_enc"]):
                raise ProgressError("credentials_unavailable")
            token = crypto.decrypt_value(row["credentials_enc"], crypto.get_encryption_key(), key_name="continue_user_token")
            if not token:
                raise ProgressError("credentials_unavailable")
            token = _token(token)
            if any(contains_secret(value, [token]) for value in (source["root"], source["browser"], row["remote_user_id"])):
                raise ProgressError("invalid_token")
            async with _client() as client:
                result = await connectors.fetch(connectors.Reader(client, source, token, checkpoint), row["remote_user_id"])
            with get_db() as db:
                db.execute("BEGIN IMMEDIATE")
                _check(db, row, source, actor, lease)
                db.execute("UPDATE user_continue_connections SET entries_json = ?, truncated = ?, last_success_at = ?, "
                           "last_status = 'ok', last_error = NULL, lease_token = NULL, lease_expires_at = NULL WHERE id = ?",
                           (_json(result["entries"]), int(result["truncated"]), _now(), connection_id))
        return {"ok": True, "count": len(result["entries"]), "truncated": result["truncated"]}
    except asyncio.CancelledError as cancelled:
        try:
            _failure(row, lease, "interrupted")
        finally:
            raise cancelled
    except TimeoutError:
        _failure(row, lease, "timeout")
        raise ProgressError("timeout") from None
    except ProgressError as exc:
        _failure(row, lease, exc.code)
        raise
    except Exception:
        _failure(row, lease, "unexpected_error")
        raise ProgressError("unexpected_error") from None


async def run_scheduled_pass(*, max_seconds=20):
    """At most three cache refreshes, no retries; shared periodic worker only."""
    if type(max_seconds) not in {int, float} or not 0 < max_seconds <= 20:
        raise ValueError("Invalid Continue work budget")
    with get_db() as db:
        if not _enabled(db, "connected_library"):
            return {"attempted": 0, "errors": 0}
        rows = [dict(row) for row in db.execute(
            "SELECT c.id, c.user_id, c.interval_minutes, c.schedule_revision FROM user_continue_connections c JOIN users u ON u.id = c.user_id "
            "WHERE c.interval_minutes > 0 AND c.schedule_consent = ? AND c.next_due_at <= ? "
            "AND (c.lease_token IS NULL OR COALESCE(c.lease_expires_at, '') <= ?) "
            "AND u.username = c.actor_username AND u.role = c.actor_role AND u.token_version = c.actor_token_version "
            "AND u.role IN ('viewer', 'editor', 'admin') ORDER BY c.next_due_at, c.id LIMIT 3",
            (SCHEDULE_CONSENT_VERSION, _now(), _now()),
        )]
    counts = {"attempted": 0, "errors": 0}
    try:
        async with asyncio.timeout(max_seconds):
            for row in rows:
                counts["attempted"] += 1
                try:
                    await refresh(row["user_id"], row["id"], automatic=True)
                except ProgressError:
                    counts["errors"] += 1
                    # Ineligible sources never hot-loop: postpone even when a
                    # lease was not claimable. Do not revive deleted consent.
                    with get_db() as db:
                        db.execute("BEGIN IMMEDIATE")
                        db.execute("UPDATE user_continue_connections SET next_due_at = ? WHERE id = ? "
                                   "AND next_due_at <= ? AND lease_token IS NULL AND schedule_revision = ?",
                                   ((datetime.fromisoformat(_now()) + timedelta(minutes=row["interval_minutes"])).isoformat(timespec="microseconds"),
                                    row["id"], _now(), row["schedule_revision"]))
    except TimeoutError:
        counts["errors"] += 1
    return counts


def _cached_entries(row):
    try:
        values = json.loads(row["entries_json"])
        if not isinstance(values, list) or len(values) > connectors.MAX_ENTRIES or any(not isinstance(value, dict) for value in values):
            raise ValueError
        return values
    except (ValueError, TypeError, RecursionError):
        return []


def _match(db, source, entry):
    """Exact whole-edition bindings only; Trash is suppressed, not remote-only."""
    provider, external_id = source["provider"], entry["external_id"]
    if entry["kind"] in {"episode", "track"}:
        # ABS episode IDs are separate, but their exact parent library item can
        # still be in Trash. Never use its art/status as episode progress.
        hidden = provider == "abs" and db.execute("SELECT 1 FROM items WHERE abs_id = ? AND deleted_at IS NOT NULL LIMIT 1", (external_id,)).fetchone()
        return None, bool(hidden)
    if provider == "jellyfin":
        rows = db.execute("SELECT i.*, r.library_id AS binding_scope, r.availability AS binding_availability FROM source_records r JOIN items i ON i.id = r.item_id "
                          "WHERE r.source_id = ? AND r.external_id = ? AND r.media_type = ? LIMIT 2",
                          (source["id"], external_id, entry["media_type"])).fetchall()
    elif provider == "abs":
        rows = db.execute("SELECT i.*, i.abs_library_id AS binding_scope FROM items i WHERE abs_id = ? LIMIT 2", (external_id,)).fetchall()
    else:
        # Identifiers here are owned constants, never request/provider text.
        table, column, scope = ("romm_records", "romm_id", "platform_id") if provider == "romm" else ("komga_records", "komga_id", "library_id")
        rows = db.execute(f"SELECT i.*, r.{scope} AS binding_scope FROM {table} r JOIN items i ON i.id = r.item_id WHERE r.{column} = ? LIMIT 2", (external_id,)).fetchall()
    if any(row["deleted_at"] is not None for row in rows):
        return None, True
    if provider == "jellyfin" and any(row["binding_availability"] != "available" for row in rows):
        return None, True
    if len(rows) != 1:
        return None, False
    row = rows[0]
    if row["media_type"] != entry["media_type"]:
        return None, False
    if entry["library_id"] is not None and row["binding_scope"] != entry["library_id"]:
        if provider == "abs" and row["binding_scope"] is None:
            # The native shelf proves its own membership, not a missing legacy
            # catalogue stamp. Show remote-only, without borrowing that cover.
            return None, False
        return None, True
    return dict(row), False


def _url(source, entry):
    base, value = source["browser"], quote(entry["external_id"], safe="")
    suffix = {"jellyfin": f"/web/index.html#!/details?id={value}", "abs": f"/item/{value}",
              "komga": f"/book/{value}", "romm": f"/rom/{value}"}[source["provider"]]
    return safe_url(base + suffix)


def _card(db, row, source, entry):
    item, suppressed = _match(db, source, entry)
    if suppressed or not _url(source, entry):
        return None
    cover = build_media_card(item).cover_path if item else None
    key = _hash(entry)
    art = {"title": entry["title"], "creator_text": entry["creator"], "media_type": entry["media_type"],
           "media_label": config.MEDIA_TYPES.get(entry["media_type"], "Media"), "cover_path": cover,
           "artwork": media_presentation.artwork_for(entry["media_type"])}
    return {**entry, "key": key, "provider": PROVIDERS[source["provider"]], "source_name": source["name"],
            "connection_id": row["id"], "catalogue_id": item["id"] if item else None, "art": art,
            "open_href": f"/api/source-continue/connections/{row['id']}/entries/{key}/open",
            "observed_at": row["last_success_at"], "stale": row["last_status"] != "ok",
            "position_label": _duration(entry["position"]) if entry["unit"] == "seconds" and entry["position"] is not None else entry["position"],
            "total_label": _duration(entry["total"]) if entry["unit"] == "seconds" and entry["total"] is not None else entry["total"]}


def _duration(value):
    seconds = int(value)
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def context(db, user_id, *, limit=12):
    """Local reads only, even for unavailable connections. Never return tokens."""
    if type(limit) is not int or not 1 <= limit <= 96:
        raise ValueError("Invalid Continue preview limit")
    connections, entries = [], []
    now = datetime.fromisoformat(_now())
    for raw in db.execute("SELECT * FROM user_continue_connections WHERE user_id = ? ORDER BY id LIMIT ?", (user_id, MAX_CONNECTIONS)):
        row = dict(raw)
        info = {key: row[key] for key in ("id", "source_key", "remote_user_id", "consented_at", "last_attempt_at", "last_success_at", "last_status", "last_error", "truncated", "interval_minutes", "next_due_at")}
        info.update(name=PROVIDERS[row["provider"]], available=False, authorization_current=True)
        if row["last_status"] == "running" and (not row["lease_token"] or (row["lease_expires_at"] or "") <= _now()):
            info["last_status"] = "interrupted"
        if row["interval_minutes"]:
            try:
                _actor(db, user_id, _schedule_actor(user_id, row))
            except ProgressError:
                info["authorization_current"] = False
        old = not row["last_success_at"] or (now - datetime.fromisoformat(row["last_success_at"])).total_seconds() > STALE_SECONDS
        info["stale"] = old or info["last_status"] != "ok"
        try:
            _, source = _connection(db, user_id, row["id"])
            info.update(name=source["name"], available=True)
            for entry in _cached_entries(row):
                card = _card(db, row, source, entry)
                if card:
                    card["stale"] = info["stale"]
                    entries.append(card)
        except (ProgressError, KeyError, TypeError, ValueError):
            # Invalidated roots/scopes/disabled features never retain redirects
            # or become remote-only cards. Connection notice remains visible.
            pass
        connections.append(info)
    entries.sort(key=lambda entry: entry["sort_at"] or "", reverse=True)
    return {"connections": connections, "entries": entries[:limit], "has_more": len(entries) > limit,
            "catalogue_ids": [entry["catalogue_id"] for entry in entries[:limit] if entry["catalogue_id"] is not None],
            "linkable": sources(db), "consent_version": CONSENT_VERSION,
            "schedule_consent_version": SCHEDULE_CONSENT_VERSION, "schedule_intervals": SCHEDULE_INTERVALS}


def resolve(db, user_id, connection_id, entry_key):
    row, source = _connection(db, user_id, connection_id)
    for entry in _cached_entries(row):
        if _hash(entry) == entry_key and _card(db, row, source, entry) is not None:
            return _url(source, entry)
    raise ProgressError("not_found")
