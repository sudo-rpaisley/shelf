"""Explicit, account-owned, pull-only progress for Jellyfin and Audiobookshelf.

Remote contract (no catalogue/server credentials are read):
* Jellyfin user access token: GET Users/Me -> Id; then
  GET Users/{that Id}/Items/{bound Id}. Only Movie/AudioBook UserData.Played,
  PlaybackPositionTicks, LastPlayedDate and intrinsic RunTimeTicks are used.
  LastPlayedDate is a playback clock, NOT a general modification timestamp:
  missing/non-advancing clocks (notably mark-unplayed) require review.
* ABS user token: GET api/me -> id; GET api/me/progress/{bound libraryItemId}
  -> libraryItemId, episodeId, currentTime, duration, isFinished, lastUpdate
  (Unix milliseconds). If userId is supplied it must match the verified self;
  ABS versions omitting it are scoped by the authenticated /me endpoint.
  Only whole audiobook progress is supported.
  With any library exclusions, a valid stamped abs_library_id is mandatory:
  unknown identity cannot prove membership is allowed. No per-item remote
  lookup guesses membership. Legacy NULL stamps are accepted ONLY when the
  validated exclusion list is empty (the explicit all-libraries policy).
  Non-NULL library stamps and exclusion entries must be valid opaque IDs;
  blank/malformed values are never normalized into an inclusion or empty scope.

Every link requires the owner to supply their own token, confirm the resolved
remote user ID, and consent to pull_v1. Within one configured source, a remote
account cannot be linked to two Shelf users. Tokens are encrypted server-side;
no token is returned, logged,
put in a URL, or used for playback/downloads. There is no remote writeback.
Automatic pulls need SEPARATE scheduled_pull_v1 consent on each connection.
run_scheduled_pass/next_due are bounded integration hooks, not a new daemon.

Every pull operates on one exact binding. Scheduled batches enumerate only
already catalogued local bindings; there is no remote enumeration/absence reset.
Only the exact bound, live edition changes: never a format sibling, page/time
conversion, album/series/episode aggregation, rating, notes or catalogue state.
Changed local state/reset since the last accepted baseline requires review;
on first import any divergent existing state also requires review. Old/equal or
missing remote clocks cannot silently replace state. Explicit review is bound
to the owner, source root, binding, observation and local event identity and is
rechecked under BEGIN IMMEDIATE. Replays create no duplicate history.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from app import config, crypto
from app.auth import ROLE_LEVELS
from app.database import get_db
from app.services import personal_library
from app.services.source_connectors.base import ConnectorError, contains_secret, validate_root_url


CONSENT_VERSION = "pull_v1"
SCHEDULE_CONSENT_VERSION = "scheduled_pull_v1"
SCHEDULE_INTERVALS = (15, 60, 360, 1440)
MAX_SCHEDULE_ACCOUNTS = 3
MAX_SCHEDULE_ITEMS = 20
SCHEDULE_LEASE_SECONDS = 120
SCHEDULE_BATCH_GAP_SECONDS = 60
REQUEST_TIMEOUT = 20.0
MAX_RESPONSE_BYTES = 1024 * 1024
_ERRORS = frozenset({
    "not_found", "disabled", "unsupported", "invalid_form", "consent_required",
    "invalid_token", "identity_mismatch", "account_in_use", "credentials_unavailable",
    "relink_required", "stale_review", "stale_config", "auth_failed", "unsafe_redirect",
    "http_error", "timeout", "unreachable", "malformed_response", "too_large",
    "ambiguous_binding", "actor_changed", "stale_link", "stale_schedule", "unavailable",
    "interrupted", "unexpected_error",
})


class ProgressError(Exception):
    def __init__(self, code: str):
        self.code = code if code in _ERRORS else "http_error"
        super().__init__(self.code)


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _timestamp(value, *, milliseconds=False) -> str | None:
    if value is None:
        return None
    try:
        if milliseconds:
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError
            date = datetime.fromtimestamp(value / 1000, timezone.utc)
        else:
            if not isinstance(value, str) or len(value) > 64:
                raise ValueError
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError
            date = date.astimezone(timezone.utc)
        if date > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError
        return date.isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError, OSError):
        raise ProgressError("malformed_response") from None


def _identity(value) -> str:
    if (not isinstance(value, str) or not value or len(value) > 200 or value != value.strip()
            or value in {".", ".."} or any(ord(c) < 33 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ProgressError("malformed_response")
    return value


def _token(value) -> str:
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(not 33 <= ord(c) <= 126 for c in value)):
        raise ProgressError("invalid_token")
    return value


def _enabled(db, key: str) -> bool:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (f"feature.{key}",)).fetchone()
    return row is None or row[0] != "0"


def _valid_abs_library_id(value) -> bool:
    try:
        _identity(value)
    except ProgressError:
        return False
    return True


def _abs_library_in_scope(library_id, excluded: list[str]) -> bool:
    # NULL is the legacy "unstamped" state, not evidence of an included library.
    # Only an all-libraries policy makes that unknown membership irrelevant.
    if library_id is None:
        return not excluded
    return _valid_abs_library_id(library_id) and library_id not in excluded


def _actor(db, user_id: int, expected: dict | None = None) -> dict:
    """Revalidate session/consent authority using the caller's connection/lock.

    HTTP callers pass the entry-session version and expiry, not a newly resolved
    post-await identity. Trusted local/scheduled callers snapshot the user row.
    No password/token/key lookup, logging or second connection occurs here.
    """
    row = db.execute("SELECT username, role, token_version FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or ROLE_LEVELS.get(row["role"], 0) < ROLE_LEVELS["viewer"]:
        raise ProgressError("actor_changed")
    current = {"id": user_id, **dict(row)}
    if expected is not None:
        expiry = expected.get("expires_at")
        if (any(current[key] != expected.get(key) for key in current)
                or (expiry is not None and (type(expiry) not in {int, float}
                    or not 0 < expiry <= 253402300799 or not math.isfinite(expiry)
                    or expiry <= datetime.now(timezone.utc).timestamp()))):
            raise ProgressError("actor_changed")
        current["expires_at"] = expiry
    return current


def _source(db, key: str) -> dict:
    """Read only roots, not source credentials, options.user_id or legacy keys."""
    if not _enabled(db, "connected_library"):
        raise ProgressError("disabled")
    if key == "abs":
        if not _enabled(db, "abs_sync"):
            raise ProgressError("disabled")
        row = db.execute("SELECT value FROM settings WHERE key = 'abs_url'").fetchone()
        root = config.get_setting_value("abs_url", row[0] if row else "")
        settings = dict(db.execute("SELECT key, value FROM settings WHERE key IN "
                                   "('abs_excluded_libraries', 'connection.abs.last_status')").fetchall())
        try:
            excluded = json.loads(settings.get("abs_excluded_libraries") or "[]")
            if (not isinstance(excluded, list) or len(excluded) > 1000
                    or any(not _valid_abs_library_id(value) for value in excluded)):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise ProgressError("relink_required") from None
        source = {"key": "abs", "provider": "abs", "name": "Audiobookshelf", "root": root,
                  "excluded": excluded, "revision": _hash(sorted(set(excluded)))}
        if settings.get("connection.abs.last_status") in {"error", "partial"}:
            raise ProgressError("unavailable")
    elif isinstance(key, str) and re.fullmatch(r"source:[1-9][0-9]{0,18}", key):
        number = int(key.partition(":")[2])
        if number > 2**63 - 1:
            raise ProgressError("not_found")
        row = db.execute("SELECT id, name, provider, base_url, enabled, config_revision, "
                         "options_json, last_status, last_error FROM source_instances WHERE id = ?",
                         (number,)).fetchone()
        if row is None:
            raise ProgressError("not_found")
        if row["provider"] != "jellyfin":
            raise ProgressError("unsupported")
        if not row["enabled"]:
            raise ProgressError("disabled")
        if row["last_status"] in {"error", "partial"} or row["last_error"]:
            raise ProgressError("unavailable")
        try:
            options = json.loads(row["options_json"])
            libraries = options.get("library_ids", [])
            if (not isinstance(libraries, list) or len(libraries) > 100
                    or any(not isinstance(v, str) or not v for v in libraries)):
                raise ValueError
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise ProgressError("relink_required") from None
        source = {"key": key, "provider": "jellyfin", "name": row["name"], "root": row["base_url"],
                  "id": number, "revision": row["config_revision"], "libraries": libraries}
    else:
        raise ProgressError("unsupported")
    try:
        source["root"] = validate_root_url(source["root"]).rstrip("/")
    except ConnectorError:
        raise ProgressError("relink_required") from None
    source["fingerprint"] = _hash([source["key"], source["provider"], source["root"], source["revision"]])
    return source


def linkable_sources(db) -> list[dict]:
    keys = [f"source:{row[0]}" for row in db.execute(
        "SELECT id FROM source_instances WHERE provider = 'jellyfin' AND enabled = 1 ORDER BY name, id",
    )] + ["abs"]
    result = []
    for key in keys:
        try:
            source = _source(db, key)
        except ProgressError:
            continue
        result.append({"key": key, "name": source["name"], "provider": source["provider"]})
    return result


async def _get(client, source: dict, token: str, path: str) -> dict:
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    headers.update({"X-Emby-Token": token} if source["provider"] == "jellyfin"
                   else {"Authorization": f"Bearer {token}"})
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            async with client.stream("GET", f"{source['root']}/{path}", headers=headers) as response:
                status = response.status_code
                if status in {401, 403}:
                    raise ProgressError("auth_failed")
                if 300 <= status < 400:
                    raise ProgressError("unsafe_redirect")
                if status == 404:
                    raise ProgressError("not_found")
                if status != 200:
                    raise ProgressError("http_error")
                if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                    raise ProgressError("malformed_response")
                length = response.headers.get("content-length", "")
                if length.isdecimal() and int(length) > MAX_RESPONSE_BYTES:
                    raise ProgressError("too_large")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ProgressError("too_large")
                    body.extend(chunk)
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (TimeoutError, httpx.TimeoutException):
        raise ProgressError("timeout") from None
    except (httpx.HTTPError, OSError):
        raise ProgressError("unreachable") from None
    except (ValueError, UnicodeError, RecursionError):
        raise ProgressError("malformed_response") from None


def _client():
    return httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=5),
                             follow_redirects=False, trust_env=False)


async def _who(client, source: dict, token: str) -> str:
    payload = await _get(client, source, token, "Users/Me" if source["provider"] == "jellyfin" else "api/me")
    identity = _identity(payload.get("Id" if source["provider"] == "jellyfin" else "id"))
    if contains_secret(identity, [token]):
        raise ProgressError("identity_mismatch")
    return identity


async def link_account(user_id: int, source_key: str, *, token: str,
                       remote_user_id: str, consent: str, actor: dict | None = None) -> int:
    """Identity-check a separately supplied user token; never fall back to a key."""
    if consent != CONSENT_VERSION:
        raise ProgressError("consent_required")
    token = _token(token)
    expected = _identity(remote_user_id)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        actor = _actor(db, user_id, actor)
        source = _source(db, source_key)
        if contains_secret(source["root"], [token]):
            raise ProgressError("invalid_token")
        generation = secrets.token_hex(32)
        db.execute(
            "INSERT INTO user_source_link_state (user_id, source_key, generation) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, source_key) DO UPDATE SET generation = excluded.generation",
            (user_id, source_key, generation),
        )
    async with _client() as client:
        actual = await _who(client, source, token)
    if actual != expected:
        raise ProgressError("identity_mismatch")
    encrypted = crypto.encrypt_value(token, crypto.get_encryption_key())
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _actor(db, user_id, actor)
        if _source(db, source_key)["fingerprint"] != source["fingerprint"]:
            raise ProgressError("stale_config")
        claimed = db.execute("SELECT generation FROM user_source_link_state WHERE user_id = ? AND source_key = ?",
                             (user_id, source_key)).fetchone()
        if claimed is None or claimed[0] != generation:
            raise ProgressError("stale_link")
        other = db.execute("SELECT user_id FROM user_source_accounts WHERE source_key = ? AND remote_user_id = ?",
                           (source_key, actual)).fetchone()
        if other and other[0] != user_id:
            raise ProgressError("account_in_use")
        db.execute(
            "INSERT INTO user_source_accounts (user_id, source_key, provider, remote_user_id, credentials_enc, "
            "root_fingerprint, revision, consent_version, consented_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, source_key) DO UPDATE SET provider = excluded.provider, "
            "remote_user_id = excluded.remote_user_id, credentials_enc = excluded.credentials_enc, "
            "root_fingerprint = excluded.root_fingerprint, revision = excluded.revision, "
            "consent_version = excluded.consent_version, consented_at = excluded.consented_at",
            (user_id, source_key, source["provider"], actual, encrypted, source["fingerprint"],
             secrets.token_hex(16), CONSENT_VERSION, _now()),
        )
        account_id = db.execute("SELECT id FROM user_source_accounts WHERE user_id = ? AND source_key = ?",
                                (user_id, source_key)).fetchone()[0]
        # Relinking is a new consent boundary, never reuse a pending review or
        # an old account's baseline. Accepted personal state/history survive.
        db.execute("DELETE FROM user_source_progress WHERE account_id = ?", (account_id,))
        db.execute("DELETE FROM user_source_schedules WHERE account_id = ?", (account_id,))
    return account_id


def _account(db, user_id: int, account_id: int) -> tuple[dict, dict]:
    row = db.execute("SELECT * FROM user_source_accounts WHERE id = ? AND user_id = ?",
                     (account_id, user_id)).fetchone()
    if row is None:
        raise ProgressError("not_found")
    account = dict(row)
    source = _source(db, account["source_key"])
    if source["fingerprint"] != account["root_fingerprint"] or source["provider"] != account["provider"]:
        raise ProgressError("relink_required")
    if account["consent_version"] != CONSENT_VERSION:
        raise ProgressError("consent_required")
    return account, source


def disconnect(db, user_id: int, account_id: int, *, actor: dict | None = None) -> None:
    """Caller holds BEGIN IMMEDIATE. Forget token/baselines, retain local history."""
    _actor(db, user_id, actor)
    account = db.execute("SELECT source_key FROM user_source_accounts WHERE id = ? AND user_id = ?",
                         (account_id, user_id)).fetchone()
    if account is None:
        raise ProgressError("not_found")
    db.execute(
        "INSERT INTO user_source_link_state (user_id, source_key, generation) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, source_key) DO UPDATE SET generation = excluded.generation",
        (user_id, account[0], secrets.token_hex(32)),
    )
    db.execute("DELETE FROM user_source_accounts WHERE id = ? AND user_id = ?", (account_id, user_id))


def _binding(db, account: dict, source: dict, item_id: int) -> dict:
    item = db.execute("SELECT id, media_type, abs_id, abs_library_id FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise ProgressError("not_found")
    if source["provider"] == "abs":
        if item["media_type"] != "audiobook" or not item["abs_id"]:
            raise ProgressError("unsupported")
        if not _abs_library_in_scope(item["abs_library_id"], source["excluded"]):
            raise ProgressError("not_found")
        if db.execute("SELECT COUNT(*) FROM items_live WHERE abs_id = ?", (item["abs_id"],)).fetchone()[0] != 1:
            raise ProgressError("ambiguous_binding")
        external_id = _identity(item["abs_id"])
        record_id = None
        library_id = item["abs_library_id"]
    else:
        if item["media_type"] not in {"digital_video", "audiobook"}:
            raise ProgressError("unsupported")
        rows = db.execute(
            "SELECT id, external_id, library_id FROM source_records WHERE source_id = ? AND item_id = ? "
            "AND media_type = ? AND availability = 'available' ORDER BY id LIMIT 2",
            (source["id"], item_id, item["media_type"]),
        ).fetchall()
        if not rows:
            raise ProgressError("not_found")
        if len(rows) != 1:
            raise ProgressError("ambiguous_binding")
        external_id, record_id = _identity(rows[0]["external_id"]), rows[0]["id"]
        library_id = rows[0]["library_id"]
        if source["libraries"] and library_id not in source["libraries"]:
            raise ProgressError("not_found")
    return {"external_id": external_id, "media_type": item["media_type"],
            "hash": _hash([source["fingerprint"], item_id, item["media_type"], external_id, record_id, library_id])}


def _number(value, maximum=10**12) -> float | int:
    if (type(value) not in {int, float} or not 0 <= value <= maximum
            or (type(value) is float and not math.isfinite(value))):
        raise ProgressError("malformed_response")
    return value


def _normalise(source: dict, binding: dict, remote_user_id: str, data: dict) -> dict:
    if source["provider"] == "jellyfin":
        expected_type = "Movie" if binding["media_type"] == "digital_video" else "AudioBook"
        if data.get("Id") != binding["external_id"] or data.get("Type") != expected_type:
            raise ProgressError("malformed_response")
        values = data.get("UserData")
        if not isinstance(values, dict):
            raise ProgressError("malformed_response")
        finished = values.get("Played")
        position, total = values.get("PlaybackPositionTicks"), data.get("RunTimeTicks")
        if type(position) is not int or type(total) is not int:
            raise ProgressError("malformed_response")
        position, total = _number(position, 2**63 - 1), _number(total, 2**63 - 1)
        clock = _timestamp(values.get("LastPlayedDate"))
        divisor = 10_000_000
    else:
        if (data.get("libraryItemId") != binding["external_id"]
                or ("userId" in data and data["userId"] != remote_user_id)
                or data.get("episodeId") not in (None, "")):
            raise ProgressError("malformed_response")
        finished = data.get("isFinished")
        position, total = _number(data.get("currentTime")), _number(data.get("duration"))
        if "progress" in data:
            _number(data["progress"], 1)  # never use an ebook/page fraction as audio time
        clock = _timestamp(data.get("lastUpdate"), milliseconds=True)
        divisor = 1
    if type(finished) is not bool or total <= 0 or position > total:
        raise ProgressError("malformed_response")
    # Completion is explicit, not inferred from a rounded percentage or EOF.
    progress = 100 if finished else min(99, int(position * 100 // total))
    return {"status": "finished" if finished else "in_progress" if position > 0 else "planned",
            "progress": progress, "unit": "seconds", "position": position / divisor,
            "total": total / divisor, "updated_at": clock}


def _local(db, user_id: int, item_id: int) -> tuple[dict, str, bool]:
    state = personal_library.state_for(db, user_id, item_id)
    latest = db.execute("SELECT COALESCE(MAX(id), 0) FROM user_item_events WHERE user_id = ? AND item_id = ?",
                        (user_id, item_id)).fetchone()[0]
    marker = _hash([state["status"], state["progress"], state["updated_at"], state["last_event_id"], latest])
    return state, marker, bool(latest or state["updated_at"])


def _manual_clock(db, user_id: int, item_id: int, state: dict) -> str | None:
    row = db.execute(
        "SELECT created_at FROM user_item_events WHERE user_id = ? AND item_id = ? "
        "AND event_type != 'source_progress' ORDER BY id DESC LIMIT 1", (user_id, item_id),
    ).fetchone()
    value = row[0] if row else state["updated_at"] if not state["last_event_id"] else None
    if not value:
        return None
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError):
        # Unknown local recency is not permission to replace it unattended.
        return "9999"


def _reason(previous, remote: dict, state: dict, marker: str, has_history: bool,
            manual_clock: str | None) -> str | None:
    baseline = json.loads(previous["baseline_remote_json"]) if previous and previous["baseline_remote_json"] else None
    if baseline:
        if _json(remote) == _json(baseline):
            return "unchanged"
        if not remote["updated_at"] or not baseline["updated_at"]:
            return "unknown_clock"
        if remote["updated_at"] <= baseline["updated_at"]:
            return "non_advancing_clock"
        if marker != previous["baseline_local_marker"]:
            return "local_changed"
    elif has_history:
        if (state["status"], state["progress"]) != (remote["status"], remote["progress"]):
            return "existing_local_state"
    if not remote["updated_at"]:
        return "unknown_clock"
    if manual_clock and remote["updated_at"] <= manual_clock:
        return "local_newer"
    return None


def _accept(db, user_id: int, account: dict, observation_id: int, remote: dict, *, decision: str) -> None:
    row = db.execute("SELECT * FROM user_source_progress WHERE id = ?", (observation_id,)).fetchone()
    before, marker, _ = _local(db, user_id, row["item_id"])
    event_key = _hash([account["revision"], row["item_id"], remote, marker, decision])
    if decision == "accepted":
        personal_library.apply_source_progress(
            db, user_id, row["item_id"], status=remote["status"], progress=remote["progress"],
            provenance={"source_key": account["source_key"], "provider": account["provider"], **remote},
        )
    after, current_marker, _ = _local(db, user_id, row["item_id"])
    db.execute(
        "INSERT OR IGNORE INTO user_source_progress_events "
        "(user_id, item_id, source_key, decision, snapshot_json, event_key) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, row["item_id"], account["source_key"], decision,
         _json({"remote": remote, "before": {key: before[key] for key in ("status", "progress")},
                "after": {key: after[key] for key in ("status", "progress")}}), event_key),
    )
    db.execute(
        "UPDATE user_source_progress SET baseline_remote_json = ?, baseline_local_marker = ?, "
        "review_token = NULL, review_local_marker = NULL, reason = NULL WHERE id = ?",
        (_json(remote), current_marker, observation_id),
    )


async def pull_item(user_id: int, account_id: int, item_id: int, *, actor: dict | None = None,
                    _lease: dict | None = None) -> dict:
    """Explicit per-item pull; safe newer observations apply, conflicts preview."""
    with get_db() as db:
        actor = _actor(db, user_id, actor)
        if _lease is not None:
            _check_schedule(db, user_id, account_id, _lease)
        account, source = _account(db, user_id, account_id)
        binding = _binding(db, account, source, item_id)
    # Decryption may log on key mismatch, so it must stay outside the lock.
    if not crypto.is_fernet_token(account["credentials_enc"]):
        raise ProgressError("credentials_unavailable")
    token = crypto.decrypt_value(account["credentials_enc"], crypto.get_encryption_key(), key_name="user_source_token")
    if not token:
        raise ProgressError("credentials_unavailable")
    token = _token(token)
    if any(contains_secret(value, [token]) for value in (source["root"], binding["external_id"], account["remote_user_id"])):
        raise ProgressError("malformed_response")
    async with _client() as client:
        if await _who(client, source, token) != account["remote_user_id"]:
            raise ProgressError("identity_mismatch")
        with get_db() as db:
            _check_pull(db, user_id, account, binding, item_id, actor, _lease)
        path = (f"Users/{quote(account['remote_user_id'], safe='')}/Items/{quote(binding['external_id'], safe='')}"
                if source["provider"] == "jellyfin" else f"api/me/progress/{quote(binding['external_id'], safe='')}")
        remote = _normalise(source, binding, account["remote_user_id"], await _get(client, source, token, path))
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _check_pull(db, user_id, account, binding, item_id, actor, _lease)
        previous = db.execute("SELECT * FROM user_source_progress WHERE account_id = ? AND item_id = ?",
                              (account_id, item_id)).fetchone()
        if previous and previous["binding_hash"] != binding["hash"]:
            # A reviewed binding repair is not permission to inherit progress.
            db.execute("DELETE FROM user_source_progress WHERE id = ?", (previous["id"],))
            previous = None
        state, marker, has_history = _local(db, user_id, item_id)
        reason = _reason(previous, remote, state, marker, has_history, _manual_clock(db, user_id, item_id, state))
        unchanged_preview = bool(previous and previous["remote_json"] == _json(remote)
                                 and previous["review_local_marker"] == marker and previous["review_token"])
        review_token = previous["review_token"] if unchanged_preview else secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO user_source_progress (account_id, item_id, binding_hash, remote_json, "
            "review_token, review_local_marker, reason, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, item_id) DO UPDATE SET remote_json = excluded.remote_json, "
            "review_token = excluded.review_token, review_local_marker = excluded.review_local_marker, "
            "reason = excluded.reason, observed_at = excluded.observed_at",
            (account_id, item_id, binding["hash"], _json(remote), review_token, marker, reason, _now()),
        )
        observation_id = db.execute("SELECT id FROM user_source_progress WHERE account_id = ? AND item_id = ?",
                                    (account_id, item_id)).fetchone()[0]
        if reason == "unchanged":
            db.execute("UPDATE user_source_progress SET review_token = NULL, review_local_marker = NULL, reason = NULL WHERE id = ?",
                       (observation_id,))
            outcome = "unchanged"
        elif reason:
            outcome = "conflict"
        else:
            _accept(db, user_id, current, observation_id, remote, decision="accepted")
            outcome = "accepted"
    return {"ok": True, "outcome": outcome, "observation_id": observation_id}


def _check_pull(db, user_id, account, binding, item_id, actor, lease):
    _actor(db, user_id, actor)
    if lease is not None:
        _check_schedule(db, user_id, account["id"], lease)
    current, source = _account(db, user_id, account["id"])
    if current["revision"] != account["revision"] or _binding(db, current, source, item_id) != binding:
        raise ProgressError("stale_config")
    return current


def review(db, user_id: int, observation_id: int, *, token: str, decision: str,
           actor: dict | None = None) -> int:
    """Caller holds BEGIN IMMEDIATE. One observation, not a general overwrite."""
    _actor(db, user_id, actor)
    if not isinstance(decision, str) or decision not in {"accepted", "kept_local"}:
        raise ProgressError("invalid_form")
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token, flags=re.ASCII) is None:
        raise ProgressError("stale_review")
    row = db.execute(
        "SELECT p.* FROM user_source_progress p JOIN user_source_accounts a ON a.id = p.account_id "
        "JOIN items_live i ON i.id = p.item_id WHERE p.id = ? AND a.user_id = ?",
        (observation_id, user_id),
    ).fetchone()
    if row is None:
        raise ProgressError("not_found")
    if (not isinstance(row["review_token"], str)
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", row["review_token"], flags=re.ASCII) is None
            or not secrets.compare_digest(token, row["review_token"])):
        raise ProgressError("stale_review")
    account, source = _account(db, user_id, row["account_id"])
    if _binding(db, account, source, row["item_id"])["hash"] != row["binding_hash"]:
        raise ProgressError("stale_review")
    if _local(db, user_id, row["item_id"])[1] != row["review_local_marker"]:
        raise ProgressError("stale_review")
    _accept(db, user_id, account, row["id"], json.loads(row["remote_json"]), decision=decision)
    return row["item_id"]


def set_schedule(db, user_id: int, account_id: int, *, interval_minutes: int,
                 consent: str = "", actor: dict | None = None) -> None:
    """Owner opt-in under BEGIN IMMEDIATE; zero disables/cancels in-flight work.

    Scheduled authority is independent of the web session's expiry, but remains
    bound to its consenting user's username/role/token_version. Password/role/
    session-version changes pause the schedule until the owner renews consent.
    """
    actor = _actor(db, user_id, actor)
    if type(interval_minutes) is not int or interval_minutes not in (0, *SCHEDULE_INTERVALS):
        raise ProgressError("invalid_form")
    if not db.execute("SELECT id FROM user_source_accounts WHERE id = ? AND user_id = ?",
                      (account_id, user_id)).fetchone():
        raise ProgressError("not_found")
    if interval_minutes == 0:
        db.execute("DELETE FROM user_source_schedules WHERE account_id = ?", (account_id,))
        return
    if consent != SCHEDULE_CONSENT_VERSION:
        raise ProgressError("consent_required")
    _account(db, user_id, account_id)
    # Replacing the row deliberately rotates the revision and drops any lease.
    # Old work cannot apply after disable -> enable, even at the same interval.
    db.execute("DELETE FROM user_source_schedules WHERE account_id = ?", (account_id,))
    db.execute(
        "INSERT INTO user_source_schedules (account_id, revision, interval_minutes, consent_version, "
        "consented_at, actor_username, actor_role, actor_token_version, next_due_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, secrets.token_hex(32), interval_minutes, SCHEDULE_CONSENT_VERSION, _now(),
         actor["username"], actor["role"], actor["token_version"], _now()),
    )


def _schedule_actor(user_id: int, row) -> dict:
    return {"id": user_id, "username": row["actor_username"], "role": row["actor_role"],
            "token_version": row["actor_token_version"]}


def _check_schedule(db, user_id: int, account_id: int, lease: dict) -> None:
    parent = lease.get("_parent_task")
    if parent is not None and parent.cancelling():
        # The pull is shielded only so its HTTP cleanup can drain. It must not
        # apply a response after its scheduling task has requested shutdown,
        # even if a transport suppresses the child's cancellation.
        raise asyncio.CancelledError
    row = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account_id,)).fetchone()
    if (not row or row["revision"] != lease["revision"] or row["lease_token"] != lease["token"]
            or not row["lease_expires_at"] or row["lease_expires_at"] <= _now()
            or row["consent_version"] != SCHEDULE_CONSENT_VERSION):
        raise ProgressError("stale_schedule")
    _actor(db, user_id, _schedule_actor(user_id, row))


def _eligible_schedules(db, *, due: str | None = None):
    """Read-only scheduler census; no token decryption or external requests."""
    if not _enabled(db, "connected_library"):
        return ()
    condition = ""
    parameters = [SCHEDULE_CONSENT_VERSION, int(_enabled(db, "abs_sync"))]
    if due is not None:
        condition = " AND sc.next_due_at <= ? AND (sc.lease_token IS NULL OR COALESCE(sc.lease_expires_at, '') <= ?)"
        parameters.extend((due, due))
    return db.execute(
        "SELECT sc.*, a.user_id FROM user_source_schedules sc "
        "JOIN user_source_accounts a ON a.id = sc.account_id JOIN users u ON u.id = a.user_id "
        "LEFT JOIN source_instances si ON a.source_key = 'source:' || si.id "
        "WHERE sc.consent_version = ? AND u.username = sc.actor_username AND u.role = sc.actor_role "
        "AND u.token_version = sc.actor_token_version AND u.role IN ('viewer', 'editor', 'admin') "
        "AND ((a.provider = 'abs' AND ? = 1) OR (a.provider = 'jellyfin' AND si.enabled = 1 "
        "AND si.provider = 'jellyfin' AND si.last_status NOT IN ('error', 'partial') "
        "AND COALESCE(si.last_error, '') = ''))" + condition
        + " ORDER BY sc.next_due_at, sc.account_id" + (" LIMIT 1" if due is not None else ""), parameters,
    )


def next_due() -> str | None:
    """Earliest UTC wake-up for parent wiring, or None when no eligible consent.

    A running job wakes at its lease expiry, not its old due timestamp; expired
    leases can be recovered after a process crash without an in-memory lock.
    """
    with get_db() as db:
        return min((max(row["next_due_at"], row["lease_expires_at"] or "") if row["lease_token"]
                    else row["next_due_at"] for row in _eligible_schedules(db)), default=None)


def _claim_schedule():
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        now = _now()
        for row in _eligible_schedules(db, due=now):
            if row["next_due_at"] > now or (row["lease_token"] and row["lease_expires_at"] and row["lease_expires_at"] > now):
                continue
            token = secrets.token_hex(32)
            expires = (datetime.fromisoformat(now) + timedelta(seconds=SCHEDULE_LEASE_SECONDS)).isoformat(timespec="microseconds")
            db.execute("UPDATE user_source_schedules SET lease_token = ?, lease_expires_at = ?, "
                       "last_attempt_at = ?, last_status = 'running', last_error = NULL WHERE account_id = ?",
                       (token, expires, now, row["account_id"]))
            return dict(row), {"revision": row["revision"], "token": token}
    return None


def _scheduled_items(db, account: dict, source: dict, after: int, limit: int) -> list[int]:
    # SQL bounds the work before fetching. The exact scope/uniqueness/eligibility
    # is rechecked by pull_item before HTTP and again after every await.
    if source["provider"] == "abs":
        scope, parameters = "", [after]
        if source["excluded"]:
            # Filter the common legacy/blank/excluded cases BEFORE LIMIT so
            # they cannot consume every bounded batch ahead of allowed items.
            # Binding validation still rejects all malformed stamps before HTTP.
            # json_each keeps a large exclusion list to one bound parameter.
            scope = (" AND abs_library_id IS NOT NULL AND trim(abs_library_id) != '' "
                     "AND abs_library_id NOT IN (SELECT value FROM json_each(?))")
            parameters.append(_json(source["excluded"]))
        parameters.append(limit)
        return [row[0] for row in db.execute(
            "SELECT id FROM items_live WHERE id > ? AND media_type = 'audiobook' "
            "AND abs_id IS NOT NULL AND abs_id != ''" + scope + " ORDER BY id LIMIT ?", parameters,
        )]
    return [row[0] for row in db.execute(
        "SELECT DISTINCT i.id FROM source_records r JOIN items_live i ON i.id = r.item_id "
        "WHERE r.source_id = ? AND i.id > ? AND i.media_type IN ('digital_video', 'audiobook') "
        "AND r.media_type = i.media_type AND r.availability = 'available' ORDER BY i.id LIMIT ?",
        (source["id"], after, limit),
    )]


def _finish_schedule(row: dict, lease: dict, counts: dict, *, cursor: int, more: bool,
                     error: str | None = None, interrupted=False) -> None:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (row["account_id"],)).fetchone()
        if (not current or current["revision"] != lease["revision"] or current["lease_token"] != lease["token"]
                or not current["lease_expires_at"] or current["lease_expires_at"] <= _now()):
            return  # Relink/disconnect/disable/new consent won. Never resurrect it.
        now = _now()
        seconds = SCHEDULE_BATCH_GAP_SECONDS if more and not error else row["interval_minutes"] * 60
        due = (datetime.fromisoformat(now) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")
        status = "interrupted" if interrupted else "partial" if counts["errors"] or counts["skipped"] else "ok"
        if error and not counts["accepted"] and not counts["conflict"] and not counts["unchanged"]:
            status = "error" if not interrupted else "interrupted"
        db.execute(
            "UPDATE user_source_schedules SET lease_token = NULL, lease_expires_at = NULL, "
            "next_due_at = ?, cursor_item_id = ?, last_finished_at = ?, last_status = ?, "
            "last_error = ?, last_summary_json = ? WHERE account_id = ?",
            (due, cursor if more else 0, now, status, error, _json(counts), row["account_id"]),
        )


async def _scheduled_pull(user_id: int, account_id: int, item_id: int, actor: dict, lease: dict) -> dict:
    """Capture parent cancellation before a child's fallible HTTP finalizers.

    Shield the drain, not the work: cancellation fences apply immediately via
    the runtime parent pointer, and the child is cancelled once. Repeated parent
    cancellation cannot abandon cleanup or replace the first cancellation.
    """
    owned = {**lease, "_parent_task": asyncio.current_task()}
    pull = asyncio.create_task(pull_item(user_id, account_id, item_id, actor=actor, _lease=owned))
    try:
        return await asyncio.shield(pull)
    except asyncio.CancelledError as cancelled:
        try:
            if not pull.done() and not pull.cancelling():
                pull.cancel()
            drained = asyncio.gather(pull, return_exceptions=True)
            while not drained.done():
                try:
                    await asyncio.shield(drained)
                except asyncio.CancelledError:
                    pass
        finally:
            raise cancelled


async def run_scheduled_pass(*, max_accounts: int = MAX_SCHEDULE_ACCOUNTS,
                             max_items: int = MAX_SCHEDULE_ITEMS, max_seconds: float = 60) -> dict:
    """Bounded parent-runner hook, NOT a daemon and never remote writeback.

    Only separately opted-in accounts run. At most 3 accounts/20 item attempts/
    60 seconds of asynchronous work per call (short SQLite sections retain the
    normal DB timeout); durable cursors continue large collections
    in later batches (minimum 60-second gap). Conflicts remain owner-reviewed.
    Source failures stop that connection's batch and wait its normal interval.
    Disabling consent, lease loss, source changes and actor revocation cancel
    in-flight writes through the SAME pull_item CAS as manual requests.
    """
    if (type(max_accounts) is not int or not 1 <= max_accounts <= MAX_SCHEDULE_ACCOUNTS
            or type(max_items) is not int or not 1 <= max_items <= MAX_SCHEDULE_ITEMS
            or type(max_seconds) not in {int, float} or not 0 < max_seconds <= 60):
        raise ValueError("Invalid scheduled-pull work budget")
    totals = {"accounts": 0, "attempted": 0, "accepted": 0, "conflict": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    deadline = asyncio.get_running_loop().time() + max_seconds
    while totals["accounts"] < max_accounts and totals["attempted"] < max_items:
        remaining_time = deadline - asyncio.get_running_loop().time()
        if remaining_time <= 0:
            break
        claimed = _claim_schedule()
        if claimed is None:
            break
        row, lease = claimed
        totals["accounts"] += 1
        counts = {key: 0 for key in ("attempted", "accepted", "conflict", "unchanged", "skipped", "errors")}
        cursor, more, error, interrupted = row["cursor_item_id"], False, None, False
        try:
            async with asyncio.timeout(max(0, deadline - asyncio.get_running_loop().time())):
                actor = _schedule_actor(row["user_id"], row)
                with get_db() as db:
                    _actor(db, row["user_id"], actor)
                    account, source = _account(db, row["user_id"], row["account_id"])
                    budget = max_items - totals["attempted"]
                    items = _scheduled_items(db, account, source, cursor, budget + 1)
                more = len(items) > budget
                for item_id in items[:budget]:
                    counts["attempted"] += 1
                    try:
                        result = await _scheduled_pull(row["user_id"], row["account_id"], item_id, actor, lease)
                    except ProgressError as exc:
                        if exc.code not in {"not_found", "unsupported", "ambiguous_binding"}:
                            raise
                        counts["skipped"] += 1
                    else:
                        counts[result["outcome"]] += 1
                    cursor = item_id
        except asyncio.CancelledError as cancelled:
            counts["errors"] += 1
            try:
                # The item task/HTTP cleanup has drained. If this ACK cannot
                # commit, retain its owned lease for expiry recovery, but NEVER
                # let any finalizer failure turn shutdown into an ordinary pass
                # error that a periodic runner catches and retries.
                _finish_schedule(row, lease, counts, cursor=cursor, more=True, error="interrupted", interrupted=True)
            finally:
                raise cancelled
        except TimeoutError:
            counts["errors"] += 1
            error, interrupted, more = "timeout", True, True
        except ProgressError as exc:
            counts["errors"] += 1
            error, more = exc.code, True
        except Exception:
            counts["errors"] += 1
            error, more = "unexpected_error", True
        _finish_schedule(row, lease, counts, cursor=cursor, more=more, error=error, interrupted=interrupted)
        for key in counts:
            totals[key] += counts[key]
        if interrupted:
            break
    return {"ok": totals["errors"] == 0, **totals, "next_due_at": next_due()}


def context(db, user_id: int, *, item_id: int | None = None,
            review_before: int | None = None, history_before: int | None = None,
            scheduler_active: bool = False) -> dict:
    """Own-account UI DTO, never encrypted/plain tokens or arbitrary remote text."""
    accounts = []
    for row in db.execute("SELECT id, source_key, provider, remote_user_id, consented_at FROM user_source_accounts WHERE user_id = ? ORDER BY id",
                          (user_id,)):
        account = dict(row)
        account.update(name="Jellyfin" if row["provider"] == "jellyfin" else "Audiobookshelf", available=False, pullable=False)
        try:
            raw, source = _account(db, user_id, row["id"])
            account.update(name=source["name"], available=True)
            if item_id is not None:
                _binding(db, raw, source, item_id)
                account["pullable"] = True
        except ProgressError:
            pass
        schedule = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (row["id"],)).fetchone()
        account["schedule"] = {"interval_minutes": 0, "authorization_current": True}
        if schedule:
            account["schedule"] = {key: schedule[key] for key in (
                "interval_minutes", "consented_at", "next_due_at", "last_attempt_at", "last_finished_at", "last_status", "last_error",
            )}
            try:
                _actor(db, user_id, _schedule_actor(user_id, schedule))
                account["schedule"]["authorization_current"] = True
            except ProgressError:
                account["schedule"]["authorization_current"] = False
        accounts.append(account)
    condition, params = (" AND p.item_id = ?", [user_id, item_id]) if item_id is not None else ("", [user_id])
    if review_before is not None:
        condition += " AND p.id < ?"
        params.append(review_before)
    pending = []
    for row in db.execute(
        "SELECT p.id, p.item_id, p.account_id, p.remote_json, p.reason, p.review_token, i.title "
        "FROM user_source_progress p JOIN user_source_accounts a ON a.id = p.account_id "
        "JOIN items_live i ON i.id = p.item_id WHERE a.user_id = ? AND p.review_token IS NOT NULL" + condition + " ORDER BY p.id DESC LIMIT 101", params,
    ):
        entry = dict(row)
        entry["remote"] = json.loads(entry.pop("remote_json"))
        entry["local"] = personal_library.state_for(db, user_id, entry["item_id"])
        pending.append(entry)
    condition, params = (" AND e.item_id = ?", [user_id, item_id]) if item_id is not None else ("", [user_id])
    if history_before is not None:
        condition += " AND e.id < ?"
        params.append(history_before)
    history = []
    for row in db.execute(
        "SELECT e.id, e.item_id, e.source_key, e.decision, e.snapshot_json, e.created_at, i.title "
        "FROM user_source_progress_events e JOIN items_live i ON i.id = e.item_id WHERE e.user_id = ?" + condition + " ORDER BY e.id DESC LIMIT 31", params,
    ):
        event = dict(row)
        event["snapshot"] = json.loads(event.pop("snapshot_json"))
        history.append(event)
    return {"accounts": accounts, "linkable": linkable_sources(db), "pending": pending[:100], "history": history[:30],
            "next_review_id": pending[99]["id"] if len(pending) > 100 else None,
            "next_history_id": history[29]["id"] if len(history) > 30 else None,
            "consent_version": CONSENT_VERSION, "item_id": item_id,
            "schedule_consent_version": SCHEDULE_CONSENT_VERSION, "schedule_intervals": SCHEDULE_INTERVALS,
            "scheduler_active": scheduler_active,
            "capabilities": {"pull": True, "remote_write": False, "bidirectional": False, "scheduled": True}}


def reparent(db, keep_id: int, other_id: int) -> None:
    """Merge hook: retain audit history, invalidate moved bindings/baselines."""
    if keep_id == other_id:
        return
    db.execute("UPDATE user_source_progress_events SET item_id = ? WHERE item_id = ?", (keep_id, other_id))
    db.execute("DELETE FROM user_source_progress WHERE item_id IN (?, ?)", (keep_id, other_id))
