"""Private, explicit repeat sessions, journal and annual completion goals.

No inference from manual personal status, provider progress or reading_log, and
no writes to any of them. Dates are user-entered calendar dates (not UTC instants).
Only finished sessions whose end date is in the goal year count. A distinct-item
goal counts catalogue ids, not works/format stacks; Trash is hidden/excluded until
restored. Merging keeps every session but can reduce the distinct-item count.

Mutation callers take BEGIN IMMEDIATE *before* any guard. This module never
commits, logs, opens a second connection or calls a provider. Session/journal
corrections use optimistic versions and append full revisions; starting another
cycle always inserts a separate session. Bounded, paginated reads need no cache.
"""

import json
import re
import secrets
from datetime import date

from app import i18n


PAGE_SIZE = 20
MAX_ID = 2**63 - 1
ACTIVITIES = {
    "read": i18n.N_("Read"), "listen": i18n.N_("Listen"),
    "watch": i18n.N_("Watch"), "play": i18n.N_("Play"),
}
STATUSES = {
    "in_progress": i18n.N_("In progress"), "paused": i18n.N_("Paused"),
    "finished": i18n.N_("Finished"), "abandoned": i18n.N_("Abandoned"),
    "voided": i18n.N_("Entered in error (not counted)"),
}
COUNT_RULES = {
    "sessions": i18n.N_("Completed sessions (repeats count)"),
    "distinct_items": i18n.N_("Distinct catalogue items (repeats count once)"),
}
_SESSION_FIELDS = ("activity", "status", "started_on", "ended_on")
_JOURNAL_FIELDS = ("entry_on", "body")


class NotFound(LookupError):
    """Absent, hidden or owned by a different account: one public outcome."""


class Conflict(ValueError):
    """The form is stale, already used differently, or history is unreadable."""


def _text(form, name: str, default: str = "") -> str:
    if hasattr(form, "getlist") and len(form.getlist(name)) > 1:
        raise ValueError(i18n.gettext("Submit each reading life field only once"))
    value = form.get(name, default)
    if not isinstance(value, str):
        raise ValueError(i18n.gettext("Reading life fields must be text"))
    return value


def _integer(value: str, minimum: int, maximum: int) -> int:
    if not re.fullmatch(r"[0-9]{1,19}", value) or not minimum <= int(value) <= maximum:
        raise ValueError(i18n.gettext("Choose a valid whole number"))
    return int(value)


def _date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError(i18n.gettext("Use a valid calendar date in YYYY-MM-DD format"))
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(i18n.gettext("Use a valid calendar date in YYYY-MM-DD format")) from None
    if parsed > date.today():
        raise ValueError(i18n.gettext("Session and journal dates cannot be in the future"))
    return value


def _key(form) -> str:
    value = _text(form, "submission_key")
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError(i18n.gettext("Reload this page before submitting a new entry"))
    return value


def parse_session(form, *, update: bool = False) -> dict:
    activity, status = _text(form, "activity"), _text(form, "status")
    if activity not in ACTIVITIES or status not in STATUSES:
        raise ValueError(i18n.gettext("Choose a valid session activity and status"))
    started = _date(_text(form, "started_on"))
    ended = _text(form, "ended_on")
    ended = _date(ended) if ended else None
    if status in ("finished", "abandoned"):
        if ended is None:
            raise ValueError(i18n.gettext("Finished and abandoned sessions need an end date"))
    elif ended is not None:
        raise ValueError(i18n.gettext("Only finished or abandoned sessions have an end date"))
    if ended is not None and ended < started:
        raise ValueError(i18n.gettext("The end date cannot be before the start date"))
    result = {"activity": activity, "status": status, "started_on": started, "ended_on": ended}
    result["version" if update else "submission_key"] = (
        _integer(_text(form, "version"), 1, MAX_ID) if update else _key(form)
    )
    return result


def parse_journal(form, *, update: bool = False) -> dict:
    body = _text(form, "body")
    if not body.strip() or len(body) > 10000 or "\x00" in body:
        raise ValueError(i18n.gettext("Write a journal entry of 1 to 10000 characters, without null characters"))
    result = {"entry_on": _date(_text(form, "entry_on")), "body": body}
    if update:
        result["version"] = _integer(_text(form, "version"), 1, MAX_ID)
    else:
        session_id = _text(form, "session_id")
        result["session_id"] = _integer(session_id, 1, MAX_ID) if session_id else None
        result["submission_key"] = _key(form)
    return result


def parse_goal(form, *, update: bool = False) -> dict:
    result = {"target": _integer(_text(form, "target"), 1, 10000)}
    if update:
        result["version"] = parse_version(form)
    else:
        activity, rule = _text(form, "activity"), _text(form, "count_rule")
        if activity not in ("all", *ACTIVITIES) or rule not in COUNT_RULES:
            raise ValueError(i18n.gettext("Choose a valid goal activity and counting rule"))
        result.update(year=_integer(_text(form, "year"), 1, 9999), activity=activity, count_rule=rule)
    return result


def parse_version(form) -> int:
    return _integer(_text(form, "version"), 1, MAX_ID)


def form_values(form, names) -> dict:
    """Keep recoverable text on native-form errors, never files or huge input."""
    return {name: value if isinstance(value := form.get(name, ""), str) and len(value) <= 10000 else ""
            for name in names}


def labels() -> dict:
    return {"reading_activities": ACTIVITIES, "reading_statuses": STATUSES,
            "reading_count_rules": COUNT_RULES, "reading_today": date.today().isoformat()}


def new_session_form(media_type: str) -> dict:
    activity = ("listen" if media_type in ("audiobook", "cd", "vinyl", "cassette", "digital_music")
                else "watch" if media_type in ("dvd", "digital_video", "tv_series")
                else "play" if media_type == "video_game" else "read")
    return {"activity": activity, "status": "in_progress", "started_on": date.today().isoformat(),
            "ended_on": "", "submission_key": secrets.token_hex(16)}


def new_journal_form(session_id: int | None = None) -> dict:
    return {"entry_on": date.today().isoformat(), "body": "", "session_id": session_id or "",
            "submission_key": secrets.token_hex(16)}


def item_for(db, item_id: int) -> dict:
    row = db.execute("SELECT id, title, media_type FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise NotFound
    return dict(row)


def session_for(db, user_id: int, session_id: int) -> dict:
    row = db.execute(
        "SELECT s.*, i.title, i.media_type FROM reading_sessions s "
        "JOIN items_live i ON i.id = s.item_id WHERE s.user_id = ? AND s.id = ?",
        (user_id, session_id),
    ).fetchone()
    if row is None:
        raise NotFound
    return dict(row)


def journal_for(db, user_id: int, entry_id: int) -> dict:
    row = db.execute(
        "SELECT j.*, i.title, i.media_type FROM reading_journal j "
        "JOIN items_live i ON i.id = j.item_id WHERE j.user_id = ? AND j.id = ?",
        (user_id, entry_id),
    ).fetchone()
    if row is None:
        raise NotFound
    return dict(row)


def _history(row: dict) -> list[dict]:
    try:
        history = json.loads(row["history_json"])
        if not isinstance(history, list) or not history or not all(isinstance(r, dict) for r in history):
            raise ValueError
        return history
    except (ValueError, TypeError, RecursionError):
        raise Conflict(i18n.gettext("The saved revision history cannot be read. No changes were made.")) from None


def history_page(row: dict, page: int = 1) -> dict:
    try:
        history = list(reversed(_history(row)))
    except Conflict as exc:
        return {"rows": [], "page": page, "has_next": False, "error": str(exc)}
    start = (page - 1) * PAGE_SIZE
    return {"rows": history[start:start + PAGE_SIZE], "page": page,
            "has_next": len(history) > start + PAGE_SIZE, "error": None}


def _revision(values: dict, fields, version: int, now: str) -> dict:
    return {**{name: values[name] for name in fields}, "version": version, "recorded_at": now}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _now(db) -> str:
    return db.execute("SELECT datetime('now')").fetchone()[0]


def _same_version(previous: dict, version: int) -> None:
    if previous["version"] != version:
        raise Conflict(i18n.gettext("This record changed after you opened the form. Reload the latest version before saving."))


def create_session(db, user_id: int, item_id: int, values: dict) -> int:
    """Insert a new independent cycle; a repeated submission never adds a cycle."""
    item_for(db, item_id)
    previous = db.execute(
        "SELECT * FROM reading_sessions WHERE user_id = ? AND submission_key = ?",
        (user_id, values["submission_key"]),
    ).fetchone()
    if previous:
        original = _history(dict(previous))[0]
        if previous["item_id"] == item_id and all(original.get(k) == values[k] for k in _SESSION_FIELDS):
            return previous["id"]
        raise Conflict(i18n.gettext("This form was already used. Reload before creating another entry."))
    now = _now(db)
    return db.execute(
        "INSERT INTO reading_sessions "
        "(user_id, item_id, activity, status, started_on, ended_on, submission_key, history_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, item_id, *(values[k] for k in _SESSION_FIELDS), values["submission_key"],
         _json([_revision(values, _SESSION_FIELDS, 1, now)]), now, now),
    ).lastrowid


def update_session(db, user_id: int, session_id: int, values: dict) -> int:
    previous = session_for(db, user_id, session_id)
    _same_version(previous, values["version"])
    if all(previous[k] == values[k] for k in _SESSION_FIELDS):
        return session_id
    history, now = _history(previous), _now(db)
    version = previous["version"] + 1
    history.append(_revision(values, _SESSION_FIELDS, version, now))
    db.execute(
        "UPDATE reading_sessions SET activity = ?, status = ?, started_on = ?, ended_on = ?, "
        "version = ?, history_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (*(values[k] for k in _SESSION_FIELDS), version, _json(history), now, session_id, user_id),
    )
    return session_id


def create_journal(db, user_id: int, item_id: int, values: dict) -> int:
    item_for(db, item_id)
    if values["session_id"] is not None:
        session = session_for(db, user_id, values["session_id"])
        if session["item_id"] != item_id:
            raise NotFound
    previous = db.execute(
        "SELECT * FROM reading_journal WHERE user_id = ? AND submission_key = ?",
        (user_id, values["submission_key"]),
    ).fetchone()
    if previous:
        original = _history(dict(previous))[0]
        if (previous["item_id"] == item_id and previous["session_id"] == values["session_id"]
                and all(original.get(k) == values[k] for k in _JOURNAL_FIELDS)):
            return previous["id"]
        raise Conflict(i18n.gettext("This form was already used. Reload before creating another entry."))
    now = _now(db)
    return db.execute(
        "INSERT INTO reading_journal "
        "(user_id, item_id, session_id, entry_on, body, submission_key, history_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, item_id, values["session_id"], values["entry_on"], values["body"], values["submission_key"],
         _json([_revision(values, _JOURNAL_FIELDS, 1, now)]), now, now),
    ).lastrowid


def update_journal(db, user_id: int, entry_id: int, values: dict) -> int:
    previous = journal_for(db, user_id, entry_id)
    _same_version(previous, values["version"])
    if all(previous[k] == values[k] for k in _JOURNAL_FIELDS):
        return entry_id
    history, now = _history(previous), _now(db)
    version = previous["version"] + 1
    history.append(_revision(values, _JOURNAL_FIELDS, version, now))
    db.execute(
        "UPDATE reading_journal SET entry_on = ?, body = ?, version = ?, history_json = ?, updated_at = ? "
        "WHERE id = ? AND user_id = ?",
        (values["entry_on"], values["body"], version, _json(history), now, entry_id, user_id),
    )
    return entry_id


def _list_page(db, user_id: int, *, journal: bool = False, item_id: int | None = None,
               session_id: int | None = None, page: int = 1) -> dict:
    # Table and order are internal constants, never request-supplied SQL.
    table, ordered = ("reading_journal", "entry_on") if journal else ("reading_sessions", "started_on")
    condition, params = "r.user_id = ?", [user_id]
    if item_id is not None:
        condition += " AND r.item_id = ?"
        params.append(item_id)
    if session_id is not None:
        assert journal
        condition += " AND r.session_id = ?"
        params.append(session_id)
    # List pages do not load revision blobs. Use the record page for history.
    fields = ("r.session_id, r.entry_on, r.body" if journal
              else "r.activity, r.status, r.started_on, r.ended_on")
    rows = [dict(row) for row in db.execute(
        f"SELECT r.id, r.item_id, r.version, {fields}, i.title, i.media_type "
        f"FROM {table} r JOIN items_live i ON i.id = r.item_id WHERE {condition} "
        f"ORDER BY r.{ordered} DESC, r.id DESC LIMIT ? OFFSET ?",
        (*params, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE),
    ).fetchall()]
    return {"rows": rows[:PAGE_SIZE], "page": page, "has_next": len(rows) > PAGE_SIZE}


def sessions_page(db, user_id: int, *, item_id: int | None = None, page: int = 1) -> dict:
    return _list_page(db, user_id, item_id=item_id, page=page)


def journal_page(db, user_id: int, *, item_id: int | None = None,
                 session_id: int | None = None, page: int = 1) -> dict:
    return _list_page(db, user_id, journal=True, item_id=item_id, session_id=session_id, page=page)


def goals_for(db, user_id: int, year: int) -> list[dict]:
    # Aggregate once for the year, not once per goal, and never hydrate catalogue.
    counts = {r["activity"]: dict(r) for r in db.execute(
        "SELECT s.activity, COUNT(*) AS sessions, COUNT(DISTINCT s.item_id) AS distinct_items "
        "FROM reading_sessions s JOIN items_live i ON i.id = s.item_id "
        "WHERE s.user_id = ? AND s.status = 'finished' AND s.ended_on BETWEEN ? AND ? GROUP BY s.activity",
        (user_id, f"{year:04d}-01-01", f"{year:04d}-12-31"),
    ).fetchall()}
    all_counts = db.execute(
        "SELECT COUNT(*) AS sessions, COUNT(DISTINCT s.item_id) AS distinct_items "
        "FROM reading_sessions s JOIN items_live i ON i.id = s.item_id "
        "WHERE s.user_id = ? AND s.status = 'finished' AND s.ended_on BETWEEN ? AND ?",
        (user_id, f"{year:04d}-01-01", f"{year:04d}-12-31"),
    ).fetchone()
    counts["all"] = dict(all_counts)
    goals = []
    for row in db.execute(
        "SELECT * FROM reading_goals WHERE user_id = ? AND year = ? ORDER BY activity, count_rule, id",
        (user_id, year),
    ).fetchall():
        goal = dict(row)
        goal["completed"] = counts.get(goal["activity"], {}).get(goal["count_rule"], 0)
        goals.append(goal)
    return goals


def create_goal(db, user_id: int, values: dict) -> int:
    if db.execute(
        "SELECT id FROM reading_goals WHERE user_id = ? AND year = ? AND activity = ? AND count_rule = ?",
        (user_id, values["year"], values["activity"], values["count_rule"]),
    ).fetchone():
        raise Conflict(i18n.gettext("This goal already exists. Edit its target instead."))
    return db.execute(
        "INSERT INTO reading_goals (user_id, year, activity, count_rule, target) VALUES (?, ?, ?, ?, ?)",
        (user_id, values["year"], values["activity"], values["count_rule"], values["target"]),
    ).lastrowid


def goal_for(db, user_id: int, goal_id: int) -> dict:
    row = db.execute("SELECT * FROM reading_goals WHERE user_id = ? AND id = ?", (user_id, goal_id)).fetchone()
    if row is None:
        raise NotFound
    return dict(row)


def update_goal(db, user_id: int, goal_id: int, values: dict) -> int:
    previous = goal_for(db, user_id, goal_id)
    _same_version(previous, values["version"])
    if previous["target"] != values["target"]:
        db.execute("UPDATE reading_goals SET target = ?, version = version + 1, updated_at = datetime('now') "
                   "WHERE id = ? AND user_id = ?", (values["target"], goal_id, user_id))
    return previous["year"]


def remove_goal(db, user_id: int, goal_id: int, version: int) -> int:
    previous = goal_for(db, user_id, goal_id)
    _same_version(previous, version)
    db.execute("DELETE FROM reading_goals WHERE id = ? AND user_id = ?", (goal_id, user_id))
    return previous["year"]


def reparent(db, keep_id: int, other_id: int) -> None:
    """Merge keeps all cycles/revisions; there is no per-item session uniqueness.

    The composite session FK updates session-linked journal items too. Move the
    remaining item-level entries explicitly. Goals are account/year-local and
    are computed again, never copied or incremented during merge.
    """
    if keep_id != other_id:
        db.execute("UPDATE reading_sessions SET item_id = ? WHERE item_id = ?", (keep_id, other_id))
        db.execute("UPDATE reading_journal SET item_id = ? WHERE item_id = ?", (keep_id, other_id))
