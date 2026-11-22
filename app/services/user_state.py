"""Per-user state layered on top of Shelf's shared catalogue.

Catalogue metadata, holdings, locations and lending remain shared. This module
stores the parts that belong to a person: consumption status/progress, rating,
personal wishlist/favourite flags and private notes.

Existing users receive a one-time snapshot of the old shared reading status,
wishlist and reading history through migrations 55-56. Users created after the
upgrade start with clean personal state; missing personal rows never inherit
another account's legacy values.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from math import isfinite
from functools import wraps


VALID_READING_STATUSES = {None, "want_to_read", "reading", "read"}
PERSONAL_FIELDS = frozenset({
    "reading_status", "date_started", "date_finished", "rating", "wishlist",
    "favourite", "personal_notes", "progress_value", "progress_total", "progress_unit",
})

from app.database import ensure_user_state_schema as ensure_schema


def __getattr__(name):
    if name == "_USER_STATE_MIGRATIONS":
        from app.database import MIGRATIONS

        return tuple(m for m in MIGRATIONS if 48 <= m[0] <= 56)
    raise AttributeError(name)


def _personal_write(function):
    @wraps(function)
    def wrapped(db, *args, **kwargs):
        from app.services.item_write import _write_scope

        with _write_scope(db):
            return function(db, *args, **kwargs)
    return wrapped


def _authorize(db, user_id: int, item_id: int, *, write=False) -> None:
    from fastapi import HTTPException
    from app.services.catalogue_security import current_actor, require_item_role

    actor = current_actor()
    if actor is not None:
        if int(actor["id"]) != int(user_id):
            raise HTTPException(403, "Forbidden")
        if write and actor.get("api_token_id"):
            raise HTTPException(403, "Forbidden")
        # Personal state needs library *read* access, not catalogue edit access.
        require_item_role(db, item_id, "viewer")


def _item_baseline(db, item_id: int):
    return db.execute(
        "SELECT id FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()


def get_state(db, user_id: int, item_id: int) -> dict | None:
    """Return exactly one user's state; missing personal state stays blank."""
    ensure_schema(db)
    _authorize(db, user_id, item_id)
    item = _item_baseline(db, item_id)
    if not item:
        return None

    row = db.execute(
        "SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?",
        (user_id, item_id),
    ).fetchone()
    if row:
        state = dict(row)
        state["persisted"] = True
        return state

    return {
        "user_id": user_id,
        "item_id": item_id,
        "reading_status": None,
        "date_started": None,
        "date_finished": None,
        "rating": None,
        "wishlist": 0,
        "favourite": 0,
        "personal_notes": None,
        "progress_value": None,
        "progress_total": None,
        "progress_unit": None,
        "created_at": None,
        "updated_at": None,
        "persisted": False,
    }


def _normalise_state_values(state: dict) -> dict:
    status = state.get("reading_status")
    if status not in VALID_READING_STATUSES:
        raise ValueError("Invalid reading status")

    rating = state.get("rating")
    if rating is not None:
        rating = int(rating)
        if rating < 1 or rating > 5:
            raise ValueError("Rating must be between 1 and 5")
        state["rating"] = rating

    for field in ("wishlist", "favourite"):
        value = state.get(field, 0)
        if isinstance(value, bool):
            value = int(value)
        else:
            value = int(value)
        if value not in (0, 1):
            raise ValueError(f"{field.title()} must be 0 or 1")
        state[field] = value

    progress_value = state.get("progress_value")
    progress_total = state.get("progress_total")
    if progress_value is not None:
        progress_value = float(progress_value)
        if not isfinite(progress_value) or progress_value < 0:
            raise ValueError("Progress cannot be negative")
        state["progress_value"] = progress_value
    if progress_total is not None:
        progress_total = float(progress_total)
        if not isfinite(progress_total) or progress_total <= 0:
            raise ValueError("Progress total must be greater than zero")
        state["progress_total"] = progress_total
    if progress_value is not None and progress_total is not None and progress_value > progress_total:
        raise ValueError("Progress cannot exceed the total")

    notes = state.get("personal_notes")
    if notes is not None:
        notes = str(notes).strip()
        if len(notes) > 10000:
            raise ValueError("Personal notes are too long")
        state["personal_notes"] = notes or None

    unit = state.get("progress_unit")
    if unit is not None:
        unit = str(unit).strip()
        if len(unit) > 32:
            raise ValueError("Progress unit is too long")
        state["progress_unit"] = unit or None

    return state


def validate_state_changes(changes: dict, state: dict | None = None) -> dict:
    """Validate a partial personal write without changing any table."""
    unknown = set(changes) - PERSONAL_FIELDS
    if unknown:
        raise ValueError(f"Unsupported personal state field: {sorted(unknown)[0]}")
    merged = dict(state or {})
    merged.update(changes)
    normalized = _normalise_state_values(merged)
    return {name: normalized[name] for name in changes}


@_personal_write
def save_state(db, user_id: int, item_id: int, **changes) -> dict:
    """Persist selected fields for one user and one catalogue item."""
    _authorize(db, user_id, item_id, write=True)
    state = get_state(db, user_id, item_id)
    if state is None:
        raise LookupError("Item not found")

    for key, value in validate_state_changes(changes, state).items():
        state[key] = value
    state = _normalise_state_values(state)

    db.execute(
        """INSERT INTO user_item_state (
            user_id, item_id, reading_status, date_started, date_finished,
            rating, wishlist, favourite, personal_notes,
            progress_value, progress_total, progress_unit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, item_id) DO UPDATE SET
            reading_status = excluded.reading_status,
            date_started = excluded.date_started,
            date_finished = excluded.date_finished,
            rating = excluded.rating,
            wishlist = excluded.wishlist,
            favourite = excluded.favourite,
            personal_notes = excluded.personal_notes,
            progress_value = excluded.progress_value,
            progress_total = excluded.progress_total,
            progress_unit = excluded.progress_unit,
            updated_at = datetime('now')""",
        (
            user_id,
            item_id,
            state["reading_status"],
            state["date_started"],
            state["date_finished"],
            state["rating"],
            state["wishlist"],
            state["favourite"],
            state["personal_notes"],
            state["progress_value"],
            state["progress_total"],
            state["progress_unit"],
        ),
    )
    saved = get_state(db, user_id, item_id)
    assert saved is not None
    return saved


@_personal_write
def set_reading_status(db, user_id: int, item_id: int, status: str | None) -> dict:
    """Set the acting user's status and maintain their dates/history."""
    _authorize(db, user_id, item_id, write=True)
    status = status or None
    if status not in VALID_READING_STATUSES:
        raise ValueError(f"Invalid reading status: {status!r}")

    state = get_state(db, user_id, item_id)
    if state is None:
        raise LookupError("Item not found")

    today = date.today().isoformat()
    changes: dict = {"reading_status": status}

    if status == "reading":
        if not state.get("date_started"):
            changes["date_started"] = today
        changes["date_finished"] = None
    elif status == "read":
        started = state.get("date_started") or today
        changes["date_started"] = started
        changes["date_finished"] = today
        db.execute(
            "INSERT INTO user_reading_log "
            "(user_id, item_id, status, date_started, date_finished) "
            "VALUES (?, ?, 'read', ?, ?)",
            (user_id, item_id, started, today),
        )
    elif status is None:
        changes["date_started"] = None
        changes["date_finished"] = None

    return save_state(db, user_id, item_id, **changes)


def _history_key(row) -> tuple:
    return (
        row["status"],
        row["date_started"],
        row["date_finished"],
        row["notes"],
    )


def get_reading_history(db, user_id: int, item_id: int) -> list[dict]:
    """Return the acting user's history without leaking household activity.

    Migrations copy legacy ``reading_log`` rows into personal history. For a
    trusted non-request, single-user service context only, we also surface later
    legacy rows from old integrations during the transition. HTTP always reads
    the actor's explicit history: a newly created sole account must not inherit
    a previous account's shared activity. Matching migrated rows are removed as
    a multiset so the same completion is not displayed twice.
    """
    ensure_schema(db)
    _authorize(db, user_id, item_id)
    personal_rows = db.execute(
        "SELECT id, status, date_started, date_finished, notes "
        "FROM user_reading_log WHERE user_id = ? AND item_id = ?",
        (user_id, item_id),
    ).fetchall()

    result = [
        {
            "id": row["id"],
            "status": row["status"],
            "date_started": row["date_started"],
            "date_finished": row["date_finished"],
            "source": "personal",
        }
        for row in personal_rows
    ]

    user_count = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    from app.services.catalogue_security import current_actor

    if user_count == 1 and current_actor() is None:
        matched = Counter(_history_key(row) for row in personal_rows)
        legacy_rows = db.execute(
            "SELECT id, status, date_started, date_finished, notes "
            "FROM reading_log WHERE item_id = ?",
            (item_id,),
        ).fetchall()
        for row in legacy_rows:
            key = _history_key(row)
            if matched[key] > 0:
                matched[key] -= 1
                continue
            result.append(
                {
                    "id": row["id"],
                    "status": row["status"],
                    "date_started": row["date_started"],
                    "date_finished": row["date_finished"],
                    "source": "legacy",
                }
            )

    result.sort(
        key=lambda row: (row.get("date_finished") or "", row["id"]),
        reverse=True,
    )
    return result


def seed_wishlist_for_user(db, user_id: int, item_id: int) -> dict:
    """Mark a newly catalogued not-owned item as wanted by the acting user."""
    return save_state(db, user_id, item_id, wishlist=1)


def status_labels(media_type: str) -> dict[str, str]:
    """Human labels for the same stored status values across media families."""
    if media_type == "audiobook" or media_type in {"vinyl", "cassette", "cd", "digital_music", "music_other"}:
        return {
            "heading": "Listening Status",
            "want_to_read": "Want to Listen",
            "reading": "Listening",
            "read": "Listened",
        }
    if media_type == "dvd":
        return {
            "heading": "Watching Status",
            "want_to_read": "Want to Watch",
            "reading": "Watching",
            "read": "Watched",
        }
    if media_type in {"video_game", "digital_game"}:
        return {
            "heading": "Playing Status",
            "want_to_read": "Want to Play",
            "reading": "Playing",
            "read": "Played",
        }
    return {
        "heading": "Reading Status",
        "want_to_read": "Want to Read",
        "reading": "Reading",
        "read": "Read",
    }
