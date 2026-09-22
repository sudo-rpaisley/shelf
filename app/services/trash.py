"""Trash: the retention window, the one "expired" predicate, the cached count.

A row is in Trash when its `deleted_at` is set (`item_write.trash_item`,
`item_copies.trash_copy`). Nothing here deletes on a timer. The retention
setting only decides when a trashed row counts as *expired* — which the Trash
page can filter on, an admin can purge, and the admin banner counts.

**One predicate.** `expired_clause` is the only spelling of "expired" in the
app. The count, the page's filter and Empty expired all call it; a second
spelling anywhere is the defect G29 records.

**A copy is expired on its own clock only while its item is live.** A trashed
item's copies were never stamped — they are hidden through `copies_live`'s
join, return with the item, and are purged with it — so they are counted once,
as the item.

**The count is cached, not swept.** A background task would be the one part of
this feature `make test` cannot see (G33), to compute a number that changes
only when a row is trashed, restored or purged, the setting is saved, or a day
passes. So `expired_count()` keeps a module-level value for an hour and every
writer calls `invalidate(db)`. `tests/conftest.py` resets it per test (G13).

**Invalidate again after the commit.** A writer invalidates inside its
transaction, and another request can refill the cache in the gap before the
commit, from a connection that still sees the old rows. So `invalidate(db)`
also runs once the writer's transaction commits (`database.after_commit`), and
a refill that began before an invalidation is not stored.
"""

import time

from app.database import after_commit, get_db, get_setting, set_setting

DEFAULT_TRASH_RETENTION_DAYS = 180

#: A hundred years. Past a few million days SQLite's `datetime('now', '-N
#: days')` returns NULL and nothing would ever expire.
MAX_TRASH_RETENTION_DAYS = 36500

#: How long a cached count stays good. A day passing is the only change no
#: writer announces, so an hour of lag on it is the whole cost.
CACHE_TTL_SECONDS = 3600

#: {"count": int, "dismissed": int, "at": float (time.monotonic)} or None.
#: The admin banner's dismissal marker rides in the same entry as the count,
#: so one reset (conftest, G13) and one invalidation cover both.
_cache: dict | None = None

#: Bumped by every invalidation. A refill stores its entry only if no
#: invalidation happened while it was reading.
_generation = 0

#: The `settings` key holding the expired count at the moment an admin
#: dismissed the banner. A row, not browser storage: a dismissal holds on
#: every device and across a restart, and a test can read it.
NAG_DISMISSED_KEY = "trash_nag_dismissed_count"


def invalidate(db=None) -> None:
    """Drop the cached count so the next read recomputes it.

    Called by the four funnel functions, the purge, the dismiss and the
    settings save. With `db`, it runs again after that connection's
    transaction commits, so a refill in the gap cannot outlive the write.
    It touches module state and registers a callback — no query, no log (G3).
    """
    global _cache, _generation
    _cache = None
    _generation += 1
    if db is not None:
        after_commit(db, invalidate)


def settle_marker(db) -> None:
    """Lower the banner's dismissal marker to the current expired count.

    The banner shows while the count exceeds the marker. A restore, a purge
    or a longer retention window lowers the count, and without this the
    marker would stay at its old height: the banner would stay silent until
    the count passed the largest value ever dismissed. Runs on the caller's
    connection, under its lock.
    """
    marker = _dismissed(db)
    if marker <= 0:
        return
    current = expired_count(db)
    if current < marker:
        set_setting(db, NAG_DISMISSED_KEY, str(current))


def get_retention_days(db) -> int:
    """The retention window in days; `0` means nothing ever expires.

    Follows `checkouts.get_overdue_days`, except that `0` comes back as `0`:
    the predicate needs "never", not a very large window. A negative or
    unparseable value falls back to the default.
    """
    raw = get_setting(db, "trash_retention_days")
    try:
        days = int(raw) if raw else DEFAULT_TRASH_RETENTION_DAYS
    except ValueError:
        days = DEFAULT_TRASH_RETENTION_DAYS
    return days if days >= 0 else DEFAULT_TRASH_RETENTION_DAYS


def expired_clause(days: int, alias: str) -> tuple[str, list]:
    """The one "expired" predicate, for a table aliased as `alias`.

    Returns `(sql, params)`. With `days <= 0` it is `0 = 1`: nothing expires.
    The copy rule (its item must be live) is the caller's join, not part of
    the clause, because the clause is shared with the item side.
    """
    if days <= 0:
        return "0 = 1", []
    return (
        f"{alias}.deleted_at IS NOT NULL "
        f"AND {alias}.deleted_at <= datetime('now', ?)",
        [f"-{days} days"],
    )


def _count(db, days: int) -> int:
    if days <= 0:
        return 0
    item_sql, item_params = expired_clause(days, "i")
    copy_sql, copy_params = expired_clause(days, "c")
    # Physical tables, both halves: this counts exactly the rows the views
    # hide (allowlisted in scripts/check_items_live.py).
    row = db.execute(
        "SELECT "
        f"(SELECT COUNT(*) FROM items i WHERE {item_sql}) + "
        "(SELECT COUNT(*) FROM item_copies c JOIN items i "
        f"ON i.id = c.item_id AND i.deleted_at IS NULL WHERE {copy_sql}) AS n",
        item_params + copy_params,
    ).fetchone()
    return row["n"]


def expired_ids(db, days: int) -> tuple[list[int], list[int]]:
    """Item ids and copy ids past the window — what Empty expired purges.

    Copies only where their item is live, as in the count. Physical reads (the
    rows the views hide). Call it under `BEGIN IMMEDIATE` and purge in the
    same block: the set is stale the moment the lock is released (G18).
    """
    if days <= 0:
        return [], []
    item_sql, item_params = expired_clause(days, "i")
    copy_sql, copy_params = expired_clause(days, "c")
    item_ids = [
        r["id"] for r in db.execute(
            f"SELECT i.id FROM items i WHERE {item_sql} ORDER BY i.id",
            item_params,
        )
    ]
    copy_ids = [
        r["id"] for r in db.execute(
            "SELECT c.id FROM item_copies c JOIN items i ON i.id = c.item_id "
            f"AND i.deleted_at IS NULL WHERE {copy_sql} ORDER BY c.id",
            copy_params,
        )
    ]
    return item_ids, copy_ids


def copy_state(db, copy_id: int) -> str | None:
    """Where one copy stands, for the restore route to answer honestly.

    `item_copies.restore_copy` returns `None` for three different reasons;
    this tells them apart. `None` — no such copy. `"item_trashed"` — its item
    is in Trash, so the item is what to restore. `"live"` — nothing to
    restore. `"trashed"` — restorable. Physical, because it exists to find the
    rows the views hide. Read it under the caller's lock.
    """
    row = db.execute(
        "SELECT c.deleted_at AS copy_deleted, i.deleted_at AS item_deleted "
        "FROM item_copies c JOIN items i ON i.id = c.item_id WHERE c.id = ?",
        (copy_id,),
    ).fetchone()
    if row is None:
        return None
    if row["item_deleted"] is not None:
        return "item_trashed"
    return "live" if row["copy_deleted"] is None else "trashed"


def item_is_live(db, item_id: int) -> bool:
    return db.execute(
        "SELECT 1 FROM items_live WHERE id = ?", (item_id,)
    ).fetchone() is not None


def purge_item(db, item_id: int) -> bool:
    """Delete one **trashed** item for good, and return whether it went.

    The permanent delete Trash exists to put off. It is one of the three
    functions allowed to remove an `items` row (the others are `merge_items`
    and `_retire_kids_book`; `tests/test_item_write.py` pins the set).

    A live row is refused (`False`) — only Trash can be emptied. `scan_log`
    is the one child with no `ON DELETE` clause, so its link is nulled first;
    every other child (copies, loans, tags, links, reading history) goes with
    the row through its cascade, which is the one place those cascades should
    fire. The cover file stays on disk: no delete path unlinks one.

    Caller holds the write lock, and the guard read happens under it (G18).
    No logging here (G3).
    """
    # The physical table: this read exists to find the row items_live hides.
    if db.execute(
        "SELECT id FROM items WHERE id = ? AND deleted_at IS NOT NULL",
        (item_id,),
    ).fetchone() is None:
        return False
    db.execute("UPDATE scan_log SET item_id = NULL WHERE item_id = ?", (item_id,))
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    settle_marker(db)
    invalidate(db)
    return True


def listing(db, *, expired_only: bool, days: int) -> dict:
    """The Trash page's full contents: trashed items, and individually
    trashed copies grouped under their item.

    Both halves are physical reads (find what the views hide, G105/G107) and
    both reuse `expired_clause` for their `expired` flag — the one spelling,
    same as the count and Empty expired. `expired_only` is applied in Python
    against that same computed flag rather than as a second WHERE clause, so
    there is exactly one occurrence of each read to allowlist.

    A copy's own clock only runs while its item is live (see the module
    docstring) — a trashed item's copies ride with the item instead, so a
    copy group's `expired` is forced false whenever `item_live` is false.
    """
    item_cond, item_params = expired_clause(days, "i")
    copy_cond, copy_params = expired_clause(days, "c")

    items = db.execute(
        "SELECT i.*, "
        f"({item_cond}) AS expired, "
        "EXISTS(SELECT 1 FROM checkouts ck WHERE ck.item_id = i.id "
        "AND ck.checked_in IS NULL) AS has_open_loan "
        "FROM items i WHERE i.deleted_at IS NOT NULL "
        "ORDER BY i.deleted_at DESC",
        item_params,
    ).fetchall()
    if expired_only:
        items = [r for r in items if r["expired"]]

    copy_rows = db.execute(
        "SELECT c.*, i.title AS item_title, (i.deleted_at IS NULL) AS item_live, "
        f"(i.deleted_at IS NULL AND ({copy_cond})) AS expired, "
        "l.name AS location_name "
        "FROM item_copies c JOIN items i ON i.id = c.item_id "
        "LEFT JOIN locations l ON l.id = c.location_id "
        "WHERE c.deleted_at IS NOT NULL "
        "ORDER BY i.id, c.copy_number, c.id",
        copy_params,
    ).fetchall()
    if expired_only:
        copy_rows = [r for r in copy_rows if r["expired"]]

    copy_groups: list[dict] = []
    current = None
    for row in copy_rows:
        if current is None or current["item_id"] != row["item_id"]:
            current = {
                "item_id": row["item_id"],
                "item_title": row["item_title"],
                "item_live": bool(row["item_live"]),
                "copies": [],
            }
            copy_groups.append(current)
        current["copies"].append(row)

    return {"items": items, "copy_groups": copy_groups}


def _dismissed(db) -> int:
    try:
        return int(get_setting(db, NAG_DISMISSED_KEY) or 0)
    except ValueError:
        return 0


def _cached_entry() -> dict:
    """The cache entry, refreshed on a miss from one connection of its own."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache["at"] < CACHE_TTL_SECONDS:
        return _cache
    generation = _generation
    with get_db() as own:
        entry = {
            "count": _count(own, get_retention_days(own)),
            "dismissed": _dismissed(own),
            "at": now,
        }
    if generation == _generation:
        _cache = entry
    return entry


def expired_count(db=None) -> int:
    """How many items and individually trashed copies are past the window.

    With no `db`, answers from the module cache (one hour), opening its own
    connection on a miss. That read sees committed state, which is right
    because the only caller without a connection is the template wrapper, and
    it renders after the route's transaction has closed (G112).

    With a `db`, reads fresh on that connection and leaves the cache alone —
    for a caller inside a transaction (the Trash page, the purge, the nag's
    dismiss) that must count what it can see, including its own writes.
    """
    if db is not None:
        return _count(db, get_retention_days(db))
    return _cached_entry()["count"]


def nag_state(user) -> dict | None:
    """What the admin banner shows, or `None` for no banner.

    `None` unless the user is an admin — the wrapper also checks the role
    before calling, so a non-admin render reaches no service code at all.
    Then `{"count": n}` while the expired count exceeds the count at the last
    dismissal; dismissing at 3 hides the banner until a fourth row expires.
    Every writer that lowers the count lowers the marker with it
    (`settle_marker`), so after a purge the banner returns on the next growth.
    With retention `0` the count is always 0, so there is never a banner.
    Reads only the cache (one query pair per process per hour on a miss).
    """
    if not user or user["role"] != "admin":
        return None
    entry = _cached_entry()
    if entry["count"] > entry["dismissed"]:
        return {"count": entry["count"]}
    return None


def dismiss(db) -> int:
    """Record the current expired count as dismissed, and return it.

    Caller takes the write lock before this read, so no restore, purge or
    retention change can commit between the count and the marker; the marker
    is exactly the count as of the write (G18). No logging (G3).
    """
    current = expired_count(db)
    set_setting(db, NAG_DISMISSED_KEY, str(current))
    invalidate(db)
    return current
