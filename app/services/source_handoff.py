"""Account-owned, credential-free browser handoff, not playback or a proxy.

Only live format-linked editions with an available cached holding are eligible.
Every click resolves again: disabled/failed/missing sources cannot retain a
preferred redirect. No network probe is implied by a cached availability row.
"""

from app.services import availability
from app.services.source_connectors.base import safe_url


ACTIONS = ("Read", "Listen", "Watch", "Play")


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
    for row in result["rows"]:
        row["eligible"] = bool(row["state"] == "available" and row["action"] in ACTIONS
                               and safe_url(row["url"]))
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


def resolve(db, user_id: int, item_id: int, action: str, *, source_key: str | None = None) -> dict | None:
    """Resolve again on navigation; return display data, never credentials."""
    if action not in ACTIONS:
        return None
    result = context(db, user_id, item_id)
    if result is None:
        return None
    if source_key is not None:
        # A row's explicit "open this source" is still checked at click time,
        # but must not silently open some other server if this one went away.
        return next((row for row in result["rows"] if row["eligible"]
                     and row["action"] == action and row["source_key"] == source_key), None)
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
