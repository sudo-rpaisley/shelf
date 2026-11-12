"""Account-local plans, progress and history, never catalogue/provider state.

Readers use ``items_live`` so Trash stays hidden. Mutation callers must take
``BEGIN IMMEDIATE`` before calling ``save`` or ``reset``; each rechecks the
live item under that lock. ``reparent`` participates in the merge caller's
transaction. Nothing here commits, logs, or calls an external provider.
"""

import json
import re

from app import i18n


MAX_NOTES = 5000
STATUS_LABELS = {
    "planned": i18n.N_("Planned"),
    "in_progress": i18n.N_("In progress"),
    "finished": i18n.N_("Finished"),
    "paused": i18n.N_("Paused"),
    "abandoned": i18n.N_("Abandoned"),
}
_FIELDS = (
    "status", "progress", "rating", "favourite", "on_list", "notes",
    "started_at", "finished_at", "updated_at", "last_event_id",
)
_FORM_FIELDS = _FIELDS[:6]


def _defaults(user_id: int, item_id: int) -> dict:
    return {
        "user_id": user_id, "item_id": item_id, "status": None,
        "progress": None, "rating": None, "favourite": 0, "on_list": 0,
        "notes": "", "started_at": None, "finished_at": None,
        "updated_at": None, "last_event_id": 0,
    }


def state_for(db, user_id: int, item_id: int) -> dict:
    """Return this user's state, or fresh defaults (also for a hidden item)."""
    row = db.execute(
        "SELECT s.* FROM user_item_state s JOIN items_live i ON i.id = s.item_id "
        "WHERE s.user_id = ? AND s.item_id = ?",
        (user_id, item_id),
    ).fetchone()
    return dict(row) if row else _defaults(user_id, item_id)


def _events(db, user_id: int, *, item_id: int | None = None, limit: int = 20) -> list[dict]:
    condition = " AND e.item_id = ?" if item_id is not None else ""
    params = [user_id]
    if item_id is not None:
        params.append(item_id)
    params.append(limit)
    rows = db.execute(
        "SELECT e.*, i.title, i.authors, i.media_type, i.cover_path "
        "FROM user_item_events e JOIN items_live i ON i.id = e.item_id "
        f"WHERE e.user_id = ?{condition} "
        "ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
        params,
    ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        try:
            snapshot = json.loads(event["snapshot_json"])
        except (TypeError, ValueError):
            snapshot = {}
        event["snapshot"] = snapshot if isinstance(snapshot, dict) else {}
        events.append(event)
    return events


def personal_context(db, user_id: int, item_id: int) -> dict | None:
    """Item/fragment context, or None when the item is absent or in Trash."""
    item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return None
    return {
        "item": dict(item),
        "personal": state_for(db, user_id, item_id),
        "personal_events": _events(db, user_id, item_id=item_id),
        "personal_status_labels": STATUS_LABELS,
    }


def dashboard(db, user_id: int, limit: int = 12) -> dict:
    """The Activity context; item lists carry catalogue display fields + own state.

    Up Next is deliberately explicit: on My List or personally Planned, not
    inferred from household ownership, legacy reading status or provider data.
    """
    limit = max(0, min(int(limit), 50))

    def items(condition):
        return [dict(row) for row in db.execute(
            "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, "
            "s.status, s.progress, s.rating, s.favourite, s.on_list, s.notes, "
            "s.started_at, s.finished_at, s.updated_at "
            "FROM user_item_state s JOIN items_live i ON i.id = s.item_id "
            f"WHERE s.user_id = ? AND ({condition}) "
            "ORDER BY s.updated_at DESC, i.id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()]

    return {
        "continue_items": items("s.status = 'in_progress'"),
        "up_next_items": items("s.on_list = 1 OR s.status = 'planned'"),
        "favourite_items": items("s.favourite = 1"),
        "personal_events": _events(db, user_id, limit=limit),
        "personal_status_labels": STATUS_LABELS,
    }


def parse_form(form) -> dict:
    """Validate only the six writable fields, never an input-supplied identity.

    No float coercion: fractions, exponents, signs, NaN, infinity, files and
    repeated fields are refused rather than silently truncated or guessed.
    Empty numeric/status fields clear their values; absent checkboxes are off.
    """
    values = {}
    for name in _FORM_FIELDS:
        if hasattr(form, "getlist") and len(form.getlist(name)) > 1:
            raise ValueError(i18n.gettext("Submit each personal field only once"))
        value = form.get(name, "")
        if not isinstance(value, str):
            raise ValueError(i18n.gettext("Personal fields must be text"))
        values[name] = value

    status = values["status"] or None
    if status is not None and status not in STATUS_LABELS:
        raise ValueError(i18n.gettext("Choose a valid personal status"))

    def integer(name, minimum, maximum):
        value = values[name]
        if value == "":
            return None
        if not re.fullmatch(r"[0-9]{1,3}", value) or not minimum <= int(value) <= maximum:
            raise ValueError(
                i18n.gettext("%(field)s must be a whole number from %(min)s to %(max)s")
                % {"field": i18n.gettext("Progress") if name == "progress" else i18n.gettext("Rating"),
                   "min": minimum, "max": maximum}
            )
        return int(value)

    def checkbox(name):
        value = values[name]
        if value in ("", "0", "off", "false"):
            return 0
        if value in ("1", "on", "true"):
            return 1
        raise ValueError(i18n.gettext("Choose a valid personal checkbox value"))

    if len(values["notes"]) > MAX_NOTES:
        raise ValueError(i18n.gettext("Keep personal notes to 5000 characters or fewer"))
    if "\x00" in values["notes"]:
        raise ValueError(i18n.gettext("Personal notes cannot contain null characters"))
    return {
        "status": status,
        "progress": integer("progress", 0, 100),
        "rating": integer("rating", 1, 5),
        "favourite": checkbox("favourite"),
        "on_list": checkbox("on_list"),
        "notes": values["notes"],
    }


def _store(db, state: dict) -> None:
    columns = ("user_id", "item_id", *_FIELDS)
    assignments = ", ".join(f"{name} = excluded.{name}" for name in _FIELDS)
    db.execute(
        f"INSERT INTO user_item_state ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT(user_id, item_id) DO UPDATE SET {assignments}",
        [state[name] for name in columns],
    )


def _record(db, user_id: int, item_id: int, event_type: str, snapshot: dict) -> int:
    return db.execute(
        "INSERT INTO user_item_events (user_id, item_id, event_type, snapshot_json) "
        "VALUES (?, ?, ?, ?)",
        (user_id, item_id, event_type, json.dumps(snapshot, ensure_ascii=False)),
    ).lastrowid


def save(db, user_id: int, item_id: int, values: dict) -> dict | None:
    """Replace own form state under the caller's write lock; None means 404.

    ``values`` must come from ``parse_form``. Dates and recency are server-owned.
    Finishing sets progress to 100; reopening clears the finished timestamp.
    Only an actual change creates history and advances ``last_event_id``.
    """
    if db.execute("SELECT id FROM items_live WHERE id = ?", (item_id,)).fetchone() is None:
        return None
    previous = state_for(db, user_id, item_id)
    state = {**previous, **{name: values[name] for name in _FORM_FIELDS}}
    now = db.execute("SELECT datetime('now')").fetchone()[0]
    if state["status"] == "in_progress" and not state["started_at"]:
        state["started_at"] = now
    if state["status"] == "finished":
        state["progress"] = 100
        state["finished_at"] = state["finished_at"] or now
    else:
        state["finished_at"] = None
    if all(state[name] == previous[name] for name in _FIELDS
           if name not in ("updated_at", "last_event_id")):
        return previous
    state["updated_at"] = now
    # Record and state are written in the caller's transaction. History is not
    # a recency index: resets/merges can move or add later events without any
    # new personal save. Only this event becomes the state's recency identity.
    snapshot = {name: value for name, value in state.items() if name != "last_event_id"}
    state["last_event_id"] = _record(db, user_id, item_id, "updated", snapshot)
    _store(db, state)
    return state


def reset(db, user_id: int, item_id: int) -> bool:
    """Clear only this user's state, retaining its snapshot in reset history."""
    if db.execute("SELECT id FROM items_live WHERE id = ?", (item_id,)).fetchone() is None:
        return False
    previous = state_for(db, user_id, item_id)
    deleted = db.execute(
        "DELETE FROM user_item_state WHERE user_id = ? AND item_id = ?",
        (user_id, item_id),
    ).rowcount
    if deleted:
        _record(db, user_id, item_id, "reset", previous)
    return True


def reparent(db, keep_id: int, other_id: int) -> None:
    """Preserve every user's state/events when duplicate items are merged.

    The most recently updated row wins scalar/date fields as one tuple. Equal
    timestamps use the state's own last_event_id, with keep_id winning exact
    ties (including unknown legacy identities, 0). Combined/reset/merge history
    never promotes an older state; the winning state's identity is preserved.
    Favourite/list membership are unions; distinct notes are joined newest
    first. If their combination exceeds the form's 5000-character bound, keep
    the newest nonempty note in the editable state and preserve BOTH complete
    originals in the visible merged-history snapshot. No note is truncated.
    Original state timestamps survive, so merging a third duplicate still
    chooses the actually latest progress rather than the time of this merge.
    """
    if keep_id == other_id:
        return
    rows = db.execute(
        "SELECT * FROM user_item_state WHERE item_id = ? ORDER BY user_id", (other_id,),
    ).fetchall()
    for row in rows:
        other = dict(row)
        kept = db.execute(
            "SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?",
            (other["user_id"], keep_id),
        ).fetchone()
        if kept is None:
            db.execute(
                "UPDATE user_item_state SET item_id = ? WHERE user_id = ? AND item_id = ?",
                (keep_id, other["user_id"], other_id),
            )
            continue
        kept = dict(kept)

        def order(state):
            return state["updated_at"] or "", state["last_event_id"]

        newest, older = (other, kept) if order(other) > order(kept) else (kept, other)
        notes = list(dict.fromkeys(s["notes"] for s in (newest, older) if s["notes"]))
        combined = "\n\n".join(notes)
        merged = {
            **newest, "item_id": keep_id,
            "favourite": int(bool(kept["favourite"] or other["favourite"])),
            "on_list": int(bool(kept["on_list"] or other["on_list"])),
            "notes": combined if len(combined) <= MAX_NOTES else notes[0],
        }
        _store(db, merged)
        db.execute(
            "DELETE FROM user_item_state WHERE user_id = ? AND item_id = ?",
            (other["user_id"], other_id),
        )
        _record(db, merged["user_id"], keep_id, "merged", {
            **merged, "merged_states": [kept, other],
        })
    db.execute("UPDATE user_item_events SET item_id = ? WHERE item_id = ?", (keep_id, other_id))
