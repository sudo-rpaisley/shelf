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

Every link requires the owner to supply their own token, confirm the resolved
remote user ID, and consent to pull_v1. Within one configured source, a remote
account cannot be linked to two Shelf users. Tokens are encrypted server-side;
no token is returned, logged,
put in a URL, or used for playback/downloads. There is no writeback or scheduler.

Pulls are deliberately per-item. No enumeration or absence/reset inference.
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
from app.database import get_db
from app.services import personal_library
from app.services.source_connectors.base import ConnectorError, contains_secret, validate_root_url


CONSENT_VERSION = "pull_v1"
REQUEST_TIMEOUT = 20.0
MAX_RESPONSE_BYTES = 1024 * 1024
_ERRORS = frozenset({
    "not_found", "disabled", "unsupported", "invalid_form", "consent_required",
    "invalid_token", "identity_mismatch", "account_in_use", "credentials_unavailable",
    "relink_required", "stale_review", "stale_config", "auth_failed", "unsafe_redirect",
    "http_error", "timeout", "unreachable", "malformed_response", "too_large",
    "ambiguous_binding",
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


def _source(db, key: str) -> dict:
    """Read only roots, not source credentials, options.user_id or legacy keys."""
    if not _enabled(db, "connected_library"):
        raise ProgressError("disabled")
    if key == "abs":
        if not _enabled(db, "abs_sync"):
            raise ProgressError("disabled")
        row = db.execute("SELECT value FROM settings WHERE key = 'abs_url'").fetchone()
        root = config.get_setting_value("abs_url", row[0] if row else "")
        source = {"key": "abs", "provider": "abs", "name": "Audiobookshelf", "root": root}
    elif isinstance(key, str) and re.fullmatch(r"source:[1-9][0-9]{0,18}", key):
        number = int(key.partition(":")[2])
        if number > 2**63 - 1:
            raise ProgressError("not_found")
        row = db.execute("SELECT id, name, provider, base_url, enabled FROM source_instances WHERE id = ?",
                         (number,)).fetchone()
        if row is None:
            raise ProgressError("not_found")
        if row["provider"] != "jellyfin":
            raise ProgressError("unsupported")
        if not row["enabled"]:
            raise ProgressError("disabled")
        source = {"key": key, "provider": "jellyfin", "name": row["name"], "root": row["base_url"], "id": number}
    else:
        raise ProgressError("unsupported")
    try:
        source["root"] = validate_root_url(source["root"]).rstrip("/")
    except ConnectorError:
        raise ProgressError("relink_required") from None
    source["fingerprint"] = _hash([source["key"], source["provider"], source["root"]])
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
                       remote_user_id: str, consent: str) -> int:
    """Identity-check a separately supplied user token; never fall back to a key."""
    if consent != CONSENT_VERSION:
        raise ProgressError("consent_required")
    token = _token(token)
    expected = _identity(remote_user_id)
    with get_db() as db:
        source = _source(db, source_key)
    if contains_secret(source["root"], [token]):
        raise ProgressError("invalid_token")
    async with _client() as client:
        actual = await _who(client, source, token)
    if actual != expected:
        raise ProgressError("identity_mismatch")
    encrypted = crypto.encrypt_value(token, crypto.get_encryption_key())
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if _source(db, source_key)["fingerprint"] != source["fingerprint"]:
            raise ProgressError("stale_config")
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


def disconnect(db, user_id: int, account_id: int) -> None:
    """Caller holds BEGIN IMMEDIATE. Forget token/baselines, retain local history."""
    if not db.execute("DELETE FROM user_source_accounts WHERE id = ? AND user_id = ?", (account_id, user_id)).rowcount:
        raise ProgressError("not_found")


def _binding(db, account: dict, source: dict, item_id: int) -> dict:
    item = db.execute("SELECT id, media_type, abs_id FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise ProgressError("not_found")
    if source["provider"] == "abs":
        if item["media_type"] != "audiobook" or not item["abs_id"]:
            raise ProgressError("unsupported")
        if db.execute("SELECT COUNT(*) FROM items_live WHERE abs_id = ?", (item["abs_id"],)).fetchone()[0] != 1:
            raise ProgressError("ambiguous_binding")
        external_id = _identity(item["abs_id"])
        record_id = None
    else:
        if item["media_type"] not in {"digital_video", "audiobook"}:
            raise ProgressError("unsupported")
        rows = db.execute(
            "SELECT id, external_id FROM source_records WHERE source_id = ? AND item_id = ? "
            "AND media_type = ? AND availability != 'unavailable' ORDER BY id LIMIT 2",
            (source["id"], item_id, item["media_type"]),
        ).fetchall()
        if not rows:
            raise ProgressError("not_found")
        if len(rows) != 1:
            raise ProgressError("ambiguous_binding")
        external_id, record_id = _identity(rows[0]["external_id"]), rows[0]["id"]
    return {"external_id": external_id, "media_type": item["media_type"],
            "hash": _hash([source["key"], item_id, item["media_type"], external_id, record_id])}


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


async def pull_item(user_id: int, account_id: int, item_id: int) -> dict:
    """Explicit per-item pull; safe newer observations apply, conflicts preview."""
    with get_db() as db:
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
        path = (f"Users/{quote(account['remote_user_id'], safe='')}/Items/{quote(binding['external_id'], safe='')}"
                if source["provider"] == "jellyfin" else f"api/me/progress/{quote(binding['external_id'], safe='')}")
        remote = _normalise(source, binding, account["remote_user_id"], await _get(client, source, token, path))
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        current, current_source = _account(db, user_id, account_id)
        if current["revision"] != account["revision"] or _binding(db, current, current_source, item_id) != binding:
            raise ProgressError("stale_config")
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


def review(db, user_id: int, observation_id: int, *, token: str, decision: str) -> int:
    """Caller holds BEGIN IMMEDIATE. One observation, not a general overwrite."""
    if decision not in {"accepted", "kept_local"} or not isinstance(token, str) or len(token) > 100:
        raise ProgressError("invalid_form")
    row = db.execute(
        "SELECT p.* FROM user_source_progress p JOIN user_source_accounts a ON a.id = p.account_id "
        "JOIN items_live i ON i.id = p.item_id WHERE p.id = ? AND a.user_id = ?",
        (observation_id, user_id),
    ).fetchone()
    if row is None:
        raise ProgressError("not_found")
    if not row["review_token"] or not secrets.compare_digest(token, row["review_token"]):
        raise ProgressError("stale_review")
    account, source = _account(db, user_id, row["account_id"])
    if _binding(db, account, source, row["item_id"])["hash"] != row["binding_hash"]:
        raise ProgressError("stale_review")
    if _local(db, user_id, row["item_id"])[1] != row["review_local_marker"]:
        raise ProgressError("stale_review")
    _accept(db, user_id, account, row["id"], json.loads(row["remote_json"]), decision=decision)
    return row["item_id"]


def context(db, user_id: int, *, item_id: int | None = None,
            review_before: int | None = None, history_before: int | None = None) -> dict:
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
            "capabilities": {"pull": True, "remote_write": False, "bidirectional": False, "scheduled": False}}


def reparent(db, keep_id: int, other_id: int) -> None:
    """Merge hook: retain audit history, invalidate moved bindings/baselines."""
    if keep_id == other_id:
        return
    db.execute("UPDATE user_source_progress_events SET item_id = ? WHERE item_id = ?", (keep_id, other_id))
    db.execute("DELETE FROM user_source_progress WHERE item_id IN (?, ?)", (keep_id, other_id))
