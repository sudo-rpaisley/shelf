"""Per-user media state layered on Shelf's shared catalogue.

Catalogue metadata, physical copies, locations, provider holdings and lending
remain shared. This module stores the state that belongs to one signed-in
person: consumption status and progress, rating, wishlist/favourite flags,
private notes and personal reading history.

The schema is owned centrally by ``app/database.py`` (migrations 32-40).  This
service deliberately contains no schema creation or migration registration so
it follows Shelf's single schema-owner rule.
"""

from __future__ import annotations

from collections import Counter
from datetime import date


VALID_READING_STATUSES = {None, "want_to_read", "reading", "read"}


def _item_exists(db, item_id: int) -> bool:
    return db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone() is not None


def get_state(db, user_id: int, item_id: int) -> dict | None:
    """Return exactly one user's state; a missing row stays blank.

    Missing personal state must never fall back to another user's values or to
    the old shared ``items.reading_status`` fields. Those legacy fields are
    copied once, for users that already exist at migration time, by the central
    migrations instead.
    """
    if not _item_exists(db, item_id):
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
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            raise ValueError("Rating must be between 1 and 5") from None
        if rating < 1 or rating > 5:
            raise ValueError("Rating must be between 1 and 5")
        state["rating"] = rating

    for field in ("wishlist", "favourite"):
        value = state.get(field, 0)
        if isinstance(value, bool):
            value = int(value)
        else:
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{field.title()} must be 0 or 1") from None
        if value not in (0, 1):
            raise ValueError(f"{field.title()} must be 0 or 1")
        state[field] = value

    progress_value = state.get("progress_value")
    progress_total = state.get("progress_total")
    if progress_value is not None:
        try:
            progress_value = float(progress_value)
        except (TypeError, ValueError):
            raise ValueError("Progress must be a number") from None
        if progress_value < 0:
            raise ValueError("Progress cannot be negative")
        state["progress_value"] = progress_value
    if progress_total is not None:
        try:
            progress_total = float(progress_total)
        except (TypeError, ValueError):
            raise ValueError("Progress total must be a number") from None
        if progress_total <= 0:
            raise ValueError("Progress total must be greater than zero")
        state["progress_total"] = progress_total
    if (
        progress_value is not None
        and progress_total is not None
        and progress_value > progress_total
    ):
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


def save_state(db, user_id: int, item_id: int, **changes) -> dict:
    """Persist selected personal fields for one user and catalogue item."""
    state = get_state(db, user_id, item_id)
    if state is None:
        raise LookupError("Item not found")

    allowed = {
        "reading_status",
        "date_started",
        "date_finished",
        "rating",
        "wishlist",
        "favourite",
        "personal_notes",
        "progress_value",
        "progress_total",
        "progress_unit",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"Unsupported personal state field: {sorted(unknown)[0]}")

    for key, value in changes.items():
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


def set_reading_status(db, user_id: int, item_id: int, status: str | None) -> dict:
    """Set one user's status and maintain that user's dates/history."""
    status = status or None
    if status not in VALID_READING_STATUSES:
        raise ValueError("Invalid reading status")

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
    """Return one user's history without leaking another household member's.

    For a genuinely single-user installation only, later rows written by old
    integrations to the legacy shared ``reading_log`` remain visible during
    the transition. Rows already copied into personal history are removed as a
    multiset so they are not shown twice. As soon as a second account exists,
    the shared legacy log is no longer consulted.
    """
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
    if user_count == 1:
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
    if media_type in {
        "audiobook",
        "vinyl",
        "cassette",
        "cd",
        "digital_music",
        "music_other",
    }:
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
            "read": "Completed",
        }
    return {
        "heading": "Reading Status",
        "want_to_read": "Want to Read",
        "reading": "Reading",
        "read": "Read",
    }
