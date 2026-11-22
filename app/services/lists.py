"""Named lists: the one write path for ``list_items``, exactly as
``item_copies.py`` is for ``item_copies``.

``lists`` and ``list_items`` were created by migrations 33-36 (issue #125),
which also seeded a ``wishlist`` list from every ``owned = 0`` row. This
module is the funnel: ``add``, ``remove`` and ``set_membership`` are the only
way a row reaches or leaves ``list_items``. The one other writer is migration
36's own seed in ``app/database.py``, which runs before this module is
importable and is allowlisted by path in ``tests/test_lists.py`` on that
basis, the same reasoning that exempts migration 26's backfill for
``item_copies``.

**No list id is cached anywhere in this module.** Every entry point resolves
a slug to an id by a fresh ``SELECT`` against ``lists`` on the caller's own
connection. A module-level cache is this repo's standard pattern for a value
read at request time (see G13's list in ``tests/conftest.py``), but the
design for this table explicitly forbids it here: lists are few and rarely
read on a hot path, so the cache would buy nothing and would need its own
entry in the autouse test-isolation fixture to avoid leaking state across
tests. If you find yourself wanting to add one, stop and report it instead of
adding it.

**Transactions are the caller's.** Every function here runs on the
connection it is given and never opens its own with ``get_db()``. Later
tasks call these from inside ``app/services/item_write.py``, itself already
inside a ``with get_db() as db:`` block — this module must nest inside that
transaction rather than start its own.
"""

from collections.abc import Iterable

#: The one list this module currently seeds and every caller currently uses.
WISHLIST = "wishlist"

#: A boolean fragment for "is this item on the wishlist", to embed in a
#: caller's own SELECT. Requires the caller's `items` table to be aliased
#: `i` — `FROM items i` or `FROM items AS i` — and is meant to be aliased
#: itself, e.g. `{WISHLISTED_SQL} AS wishlisted`. Resolves the wishlist's
#: list id by slug inline, on every evaluation, rather than trusting a cached
#: id (see the module docstring) — sqlite's query planner evaluates the
#: uncorrelated subquery once per statement, not once per row.
WISHLISTED_SQL = (
    "EXISTS (SELECT 1 FROM list_items li WHERE li.item_id = i.id "
    "AND li.list_id = (SELECT id FROM lists WHERE slug = 'wishlist'))"
)

#: A boolean fragment for "is this item neither owned nor wishlisted" — the
#: third state #125 makes reachable. Same alias requirement as
#: `WISHLISTED_SQL`: the caller's `items` table must be aliased `i`.
NEITHER_SQL = f"(i.owned = 0 AND NOT {WISHLISTED_SQL})"


class UnknownList(ValueError):
    """Raised when a slug does not name a row in `lists`."""


def list_id(db, slug: str) -> int:
    """Resolve a list's id from its slug. Raises `UnknownList` when absent.

    Never cached (see module docstring) — always a fresh read on `db`.
    """
    row = db.execute("SELECT id FROM lists WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise UnknownList(f"No list with slug {slug!r}")
    return row["id"]


def is_member(db, slug: str, item_id: int) -> bool:
    """Is `item_id` on the list named by `slug`?"""
    lid = list_id(db, slug)
    row = db.execute(
        "SELECT 1 FROM list_items WHERE list_id = ? AND item_id = ?",
        (lid, item_id),
    ).fetchone()
    return row is not None


def add(db, slug: str, item_id: int) -> None:
    """Add one item to a list. Idempotent — already a member is a no-op."""
    lid = list_id(db, slug)
    db.execute(
        "INSERT OR IGNORE INTO list_items (list_id, item_id) VALUES (?, ?)",
        (lid, item_id),
    )


def remove(db, slug: str, item_id: int) -> None:
    """Remove one item from a list. Not a member is a no-op."""
    lid = list_id(db, slug)
    db.execute(
        "DELETE FROM list_items WHERE list_id = ? AND item_id = ?",
        (lid, item_id),
    )


def reparent(db, keep_id: int, other_id: int) -> None:
    """Move every list membership from ``other_id`` onto ``keep_id``.

    Called from ``item_merge.reparent_children`` before the merged row is
    deleted. It lives here rather than beside the other reparent helpers
    because this module is the one write path for ``list_items``.

    Two rules, and the second is not bookkeeping:

    - Memberships move with ``UPDATE OR IGNORE`` and the leftovers are
      cleared explicitly, the same shape and for the same reason as
      ``item_merge._reparent_tags`` — a list both rows are on would
      otherwise collide on ``PRIMARY KEY (list_id, item_id)``, and the
      effect must not depend on the caller's DELETE.
    - **An owned keeper then sheds the wishlist membership it just
      inherited.** ``owned = 1`` never coexists with wishlist membership
      (``tests/conftest.py::_assert_ownership_partition``), so moving a
      wishlisted row's membership onto a keeper the user already owns would
      break that partition. Owning the thing is the stronger statement, so
      the want is dropped.

    The keeper's ``owned`` is read through ``items_live``, so a trashed
    keeper reads as absent and is treated as not owned. That is unreachable
    today — nothing writes ``deleted_at`` before this release — and is left
    for the Trash plan to decide deliberately rather than settled here by
    accident.

    Runs in the caller's transaction and logs nothing (G3): a caller may
    hold a write lock around it, and a log record would open a second
    connection and wait out the busy timeout.
    """
    db.execute(
        "UPDATE OR IGNORE list_items SET item_id = ? WHERE item_id = ?",
        (keep_id, other_id),
    )
    db.execute("DELETE FROM list_items WHERE item_id = ?", (other_id,))

    keeper = db.execute(
        "SELECT owned FROM items_live WHERE id = ?", (keep_id,)
    ).fetchone()
    if keeper and keeper["owned"]:
        remove(db, WISHLIST, keep_id)


def set_membership(db, slug: str, item_ids: Iterable[int], member: bool) -> None:
    """Set membership for every id in `item_ids` in one statement.

    `member=True` inserts (idempotently) via one `executemany`; `member=False`
    deletes every row for those ids via one `DELETE ... WHERE item_id IN (...)`.
    The list is resolved first, like every other entry point in this module,
    so an unknown slug still raises `UnknownList` even when `item_ids` is
    empty. An empty `item_ids` then writes nothing.
    """
    lid = list_id(db, slug)
    ids = list(item_ids)
    if not ids:
        return

    if member:
        db.executemany(
            "INSERT OR IGNORE INTO list_items (list_id, item_id) VALUES (?, ?)",
            [(lid, item_id) for item_id in ids],
        )
    else:
        placeholders = ", ".join("?" for _ in ids)
        db.execute(
            f"DELETE FROM list_items WHERE list_id = ? AND item_id IN ({placeholders})",
            [lid, *ids],
        )
