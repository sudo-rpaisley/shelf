"""Per-user state layered on top of Shelf's shared catalogue.

Catalogue metadata, holdings, locations and lending remain shared. This module
stores the parts that belong to a person: consumption status/progress, rating,
personal wishlist/favourite flags and private notes.

Older Shelf versions stored reading status/history and wishlist intent on the
shared catalogue item. Migrations 55-56 take a one-time snapshot of that legacy
state for every user that exists during upgrade. After that point a missing
personal row means "no personal state" rather than "inherit somebody else's
state". This is especially important for users created later through OIDC or
ordinary local user management.
"""

from __future__ import annotations

from datetime import date


VALID_READING_STATUSES = {None, "want_to_read", "reading", "read"}

_CREATE_USER_ITEM_STATE = """CREATE TABLE IF NOT EXISTS user_item_state (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    reading_status  TEXT CHECK(reading_status IS NULL OR reading_status IN ('want_to_read','reading','read')),
    date_started    TEXT,
    date_finished   TEXT,
    rating          INTEGER CHECK(rating IS NULL OR (rating >= 1 AND rating <= 5)),
    wishlist        INTEGER NOT NULL DEFAULT 0 CHECK(wishlist IN (0,1)),
    favourite       INTEGER NOT NULL DEFAULT 0 CHECK(favourite IN (0,1)),
    personal_notes  TEXT,
    progress_value  REAL,
    progress_total  REAL,
    progress_unit   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, item_id)
)"""

_CREATE_USER_READING_LOG = """CREATE TABLE IF NOT EXISTS user_reading_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    status        TEXT NOT NULL,
    date_started  TEXT,
    date_finished TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
)"""

_SCHEMA_STATEMENTS = (
    _CREATE_USER_ITEM_STATE,
    "CREATE INDEX IF NOT EXISTS idx_user_item_state_item ON user_item_state(item_id)",
    "CREATE INDEX IF NOT EXISTS idx_user_item_state_status ON user_item_state(user_id, reading_status)",
    "CREATE INDEX IF NOT EXISTS idx_user_item_state_wishlist ON user_item_state(user_id, wishlist)",
    "CREATE INDEX IF NOT EXISTS idx_user_item_state_favourite ON user_item_state(user_id, favourite)",
    _CREATE_USER_READING_LOG,
    "CREATE INDEX IF NOT EXISTS idx_user_reading_log_user_item ON user_reading_log(user_id, item_id)",
)

# OIDC's in-flight branch owns 46-47. Starting this independent feature at 48
# keeps both branches merge-order independent: a database may legitimately have
# 48+ recorded before 46-47 and Shelf's migration runner will still apply the
# missing lower-numbered migrations later.
_USER_STATE_MIGRATIONS = (
    (48, "Add per-user item state table", _CREATE_USER_ITEM_STATE),
    (49, "Index per-user item state by item", "CREATE INDEX IF NOT EXISTS idx_user_item_state_item ON user_item_state(item_id)"),
    (50, "Index per-user consumption status", "CREATE INDEX IF NOT EXISTS idx_user_item_state_status ON user_item_state(user_id, reading_status)"),
    (51, "Index per-user wishlist", "CREATE INDEX IF NOT EXISTS idx_user_item_state_wishlist ON user_item_state(user_id, wishlist)"),
    (52, "Index per-user favourites", "CREATE INDEX IF NOT EXISTS idx_user_item_state_favourite ON user_item_state(user_id, favourite)"),
    (53, "Add per-user reading history table", _CREATE_USER_READING_LOG),
    (54, "Index per-user reading history", "CREATE INDEX IF NOT EXISTS idx_user_reading_log_user_item ON user_reading_log(user_id, item_id)"),
    (
        55,
        "Snapshot legacy shared activity for existing users",
        """INSERT OR IGNORE INTO user_item_state
               (user_id, item_id, reading_status, date_started, date_finished, wishlist)
             SELECT u.id,
                    i.id,
                    i.reading_status,
                    i.date_started,
                    i.date_finished,
                    CASE WHEN i.owned = 0 THEN 1 ELSE 0 END
               FROM users u CROSS JOIN items i
              WHERE i.reading_status IS NOT NULL
                 OR i.date_started IS NOT NULL
                 OR i.date_finished IS NOT NULL
                 OR i.owned = 0""",
    ),
    (
        56,
        "Snapshot legacy reading history for existing users",
        """INSERT INTO user_reading_log
               (user_id, item_id, status, date_started, date_finished, notes, created_at)
             SELECT u.id,
                    rl.item_id,
                    rl.status,
                    rl.date_started,
                    rl.date_finished,
                    rl.notes,
                    rl.created_at
               FROM users u CROSS JOIN reading_log rl""",
    ),
)


def _register_migrations() -> None:
    """Register this focused module's migrations with the central runner.

    Router-package extensions are imported before ``init_db`` at application
    startup, and this service is imported during test collection too. Keeping
    the migration definitions beside the feature avoids a large, conflict-prone
    edit to ``database.py`` while still using Shelf's atomic migration runner.
    """
    from app import database

    existing = {version for version, _description, _sql in database.MIGRATIONS}
    pending = tuple(migration for migration in _USER_STATE_MIGRATIONS if migration[0] not in existing)
    if pending:
        database.MIGRATIONS = tuple(database.MIGRATIONS) + pending


_register_migrations()


def ensure_schema(db) -> None:
    """Idempotent safety net for callers outside normal application startup."""
    for statement in _SCHEMA_STATEMENTS:
        db.execute(statement)


def get_state(db, user_id: int, item_id: int) -> dict | None:
    """Return one user's state; a missing row is a clean personal state."""
    ensure_schema(db)
    item = db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
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
        if progress_value < 0:
            raise ValueError("Progress cannot be negative")
        state["progress_value"] = progress_value
    if progress_total is not None:
        progress_total = float(progress_total)
        if progress_total <= 0:
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


def save_state(db, user_id: int, item_id: int, **changes) -> dict:
    """Persist selected personal fields for one user/item pair."""
    state = get_state(db, user_id, item_id)
    if state is None:
        raise LookupError("Item not found")

    allowed = {
        "reading_status", "date_started", "date_finished", "rating", "wishlist",
        "favourite", "personal_notes", "progress_value", "progress_total", "progress_unit",
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
    """Set the acting user's status and maintain their dates/history."""
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


def get_reading_history(db, user_id: int, item_id: int) -> list[dict]:
    """Return only the acting user's completion history."""
    ensure_schema(db)
    rows = db.execute(
        "SELECT id, status, date_started, date_finished FROM user_reading_log "
        "WHERE user_id = ? AND item_id = ? "
        "ORDER BY COALESCE(date_finished, '') DESC, id DESC",
        (user_id, item_id),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "status": row["status"],
            "date_started": row["date_started"],
            "date_finished": row["date_finished"],
            "source": "personal",
        }
        for row in rows
    ]


def seed_wishlist_for_user(db, user_id: int, item_id: int) -> dict:
    """Mark a newly catalogued not-owned item as wanted by the acting user."""
    return save_state(db, user_id, item_id, wishlist=1)


def status_labels(media_type: str) -> dict[str, str]:
    """Human labels for the same stored status values across media families."""
    if media_type == "audiobook" or media_type in {"vinyl", "cassette", "cd", "digital_music", "music_other"}:
        return {
            "heading": "Listening status",
            "want_to_read": "Want to Listen",
            "reading": "Listening",
            "read": "Listened",
        }
    if media_type == "dvd":
        return {
            "heading": "Watching status",
            "want_to_read": "Want to Watch",
            "reading": "Watching",
            "read": "Watched",
        }
    if media_type in {"video_game", "digital_game"}:
        return {
            "heading": "Playing status",
            "want_to_read": "Want to Play",
            "reading": "Playing",
            "read": "Completed",
        }
    return {
        "heading": "Reading status",
        "want_to_read": "Want to Read",
        "reading": "Reading",
        "read": "Read",
    }
