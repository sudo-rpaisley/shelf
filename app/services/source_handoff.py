"""Account-owned, credential-free browser handoff, not playback or a proxy.

Only live format-linked editions with an available cached holding are eligible.
Every click resolves again: disabled/failed/missing sources cannot retain a
preferred redirect. No network probe is implied by a cached availability row.
"""

import hashlib
import json
import re
import sqlite3
from collections import defaultdict, deque

from app.services import availability
from app.services.source_connectors.base import safe_url


ACTIONS = ("Read", "Listen", "Watch", "Play")


def _bind_rows(db, rows):
    """Enrich only our DTO, leaving availability's public read-only DTO stable.

    A row action identifies a record, not just a source. IDs are hashed with
    binding/URL/revision facts; no opaque remote ID or credential enters a URL.
    Matching cached display rows against current records fails closed if a
    concurrent refresh removed/changed them between the two read queries.
    """
    records = defaultdict(deque)
    ids = sorted({row["item_id"] for row in rows})
    size = min(400, db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    for start in range(0, len(ids), size):
        chunk = ids[start:start + size]
        for record in db.execute(
            "SELECT r.id, r.item_id, r.source_id, r.external_id, r.library_id, r.media_type, r.open_url, "
            "r.availability, i.media_type AS item_media_type, s.base_url, s.browser_url, s.config_revision, "
            "s.enabled, s.last_status, s.last_error FROM source_records r "
            "JOIN source_instances s ON s.id = r.source_id JOIN items_live i ON i.id = r.item_id "
            f"WHERE r.item_id IN ({','.join('?' for _ in chunk)}) ORDER BY r.id", chunk,
        ):
            url = availability._safe_url(record["open_url"], base=record["browser_url"] or record["base_url"])
            key = (record["item_id"], f"source:{record['source_id']}", url)
            records[key].append({
                "identity": [record[name] for name in ("id", "external_id", "library_id", "media_type", "config_revision")],
                "eligible": record["availability"] == "available" and record["enabled"]
                            and str(record["last_status"] or "").casefold() not in availability._FAILED_STATUSES
                            and not record["last_error"],
                "action": availability._ACTIONS.get(record["media_type"])
                          or availability._ACTIONS.get(record["item_media_type"], "Open"),
                "media_type": record["item_media_type"],
            })
    for row in rows:
        key = (row["item_id"], row["source_key"], row["url"])
        identity = None
        eligible = True
        if row["source_key"].startswith("source:"):
            if records[key]:
                record = records[key].popleft()
                identity = record["identity"]
                eligible = (record["eligible"] and row["action"] == record["action"]
                            and row["media_type"] == record["media_type"])
        else:
            # Existing singleton builders encode the remote identity in the
            # browser path. Changing it changes this binding, not a fallback.
            identity = ["legacy"]
        row["binding"] = hashlib.sha256(json.dumps(
            [*key, row["action"], identity], separators=(",", ":"), ensure_ascii=True,
        ).encode()).hexdigest() if identity is not None else None
        row["eligible"] = bool(eligible and row["binding"] and row["state"] == "available"
                               and row["action"] in ACTIONS and safe_url(row["url"]))


def context(db, user_id: int, item_id: int) -> dict | None:
    result = availability.available_here(db, item_id)
    if result is None:
        return None
    preferences = {row["action"]: row["source_key"] for row in db.execute(
        "SELECT action, source_key FROM user_source_preferences WHERE user_id = ? AND item_id = ?",
        (user_id, item_id),
    )}
    # The connector URL boundary is stricter than the historical availability
    # DTO. Never silently strip credentials into an apparently working link.
    _bind_rows(db, result["rows"])
    actions = []
    for action in ACTIONS:
        choices = [row for row in result["rows"] if row["eligible"] and row["action"] == action]
        preferred = preferences.get(action)
        selected = next((row for row in choices if row["source_key"] == preferred), None)
        fallback = bool(preferred and selected is None)
        selected = selected or (choices[0] if choices else None)
        if selected or preferred:
            actions.append({"action": action, "choices": choices, "selected": selected,
                            "preferred": preferred, "fallback": fallback})
    result["source_handoff"] = {"actions": actions}
    return result


def resolve(db, user_id: int, item_id: int, action: str, *, source_key: str | None = None,
            binding: str | None = None) -> dict | None:
    """Resolve again on navigation; return display data, never credentials."""
    if action not in ACTIONS:
        return None
    result = context(db, user_id, item_id)
    if result is None:
        return None
    if binding is not None:
        if not isinstance(binding, str) or re.fullmatch(r"[a-f0-9]{64}", binding, re.ASCII) is None:
            return None
        return next((row for row in result["rows"] if row["eligible"] and row["item_id"] == item_id
                     and row["action"] == action and row["binding"] == binding
                     and (source_key is None or row["source_key"] == source_key)), None)
    if source_key is not None:
        # A row's explicit "open this source" is still checked at click time,
        # but must not silently open some other server if this one went away.
        choices = [row for row in result["rows"] if row["eligible"] and row["item_id"] == item_id
                   and row["action"] == action and row["source_key"] == source_key]
        return choices[0] if len(choices) == 1 else None
    return next((entry["selected"] for entry in result["source_handoff"]["actions"]
                 if entry["action"] == action), None)


def set_preference(db, user_id: int, item_id: int, action: str, source_key: str) -> None:
    """Caller holds BEGIN IMMEDIATE; identities never come from form fields."""
    result = context(db, user_id, item_id)
    if result is None:
        raise ValueError("Item not found")
    if action not in ACTIONS:
        raise ValueError("Choose a supported action")
    if source_key == "":
        db.execute("DELETE FROM user_source_preferences WHERE user_id = ? AND item_id = ? AND action = ?",
                   (user_id, item_id, action))
        return
    if not any(row["eligible"] and row["action"] == action and row["source_key"] == source_key
               for row in result["rows"]):
        existing = db.execute(
            "SELECT source_key FROM user_source_preferences WHERE user_id = ? AND item_id = ? AND action = ?",
            (user_id, item_id, action),
        ).fetchone()
        if existing and existing[0] == source_key:
            return  # Re-saving the displayed unavailable preference keeps it.
        raise ValueError("Choose an available source for this action")
    db.execute(
        "INSERT INTO user_source_preferences (user_id, item_id, action, source_key) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, item_id, action) DO UPDATE SET source_key = excluded.source_key",
        (user_id, item_id, action, source_key),
    )


def reparent(db, keep_id: int, other_id: int) -> None:
    """Merge hook: retain kept preferences, fill absent ones from the duplicate."""
    if keep_id == other_id:
        return
    db.execute(
        "INSERT OR IGNORE INTO user_source_preferences (user_id, item_id, action, source_key) "
        "SELECT user_id, ?, action, source_key FROM user_source_preferences WHERE item_id = ?",
        (keep_id, other_id),
    )
    db.execute("DELETE FROM user_source_preferences WHERE item_id = ?", (other_id,))
