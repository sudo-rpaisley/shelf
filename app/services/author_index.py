"""The author index (#117b): `authors` + `item_authors`, derived from `items.authors`.

The comma-joined `items.authors` string stays the record. These two tables are
an index over it, so a wrong split is a parse-rule fix and a re-index, never a
data repair. This module holds the only SQL that writes them
(`tests/test_item_write.py` pins that), and it is called from exactly two
places: the `item_write` funnel, on every write that sets `authors`, and the
boot step in `app/database.py`.

Everything here runs on the caller's connection and inside the caller's
transaction — no `get_db()` of its own (G112) and no commit — so an index row
and the string it came from commit or roll back together. Nothing here calls
back into `item_write` (G96).

The index covers **physical** rows, trashed ones included: soft delete writes
nothing to children, and every read reaches the index through `items_live`
(G105/G114), exactly as `item_tags` does.
"""

from collections.abc import Iterable

from app.services import authors as authors_svc


def _upsert_author(db, name: str, key: str) -> int:
    """The id of the author whose identity is `key`, creating it if needed.

    The stored `name` is whichever spelling created the row, and is never
    updated: an author's id must be stable while anything links to it, and
    a later spelling of the same key is the same person.
    """
    db.execute(
        "INSERT INTO authors (name, name_key) VALUES (?, ?) "
        "ON CONFLICT(name_key) DO NOTHING",
        (name, key),
    )
    return db.execute(
        "SELECT id FROM authors WHERE name_key = ?", (key,)
    ).fetchone()[0]


def gc_orphans(db, author_ids: Iterable[int]) -> None:
    """Delete any of `author_ids` that no item links to any more."""
    ids = sorted(set(author_ids))
    if not ids:
        return
    marks = ", ".join("?" for _ in ids)
    db.execute(
        f"DELETE FROM authors WHERE id IN ({marks}) AND NOT EXISTS "
        "(SELECT 1 FROM item_authors ia WHERE ia.author_id = authors.id)",
        ids,
    )


def _link(db, item_id: int, authors: str | None) -> None:
    for parsed in authors_svc.parse(authors):
        author_id = _upsert_author(db, parsed.name, parsed.name_key)
        db.execute(
            "INSERT INTO item_authors (item_id, author_id, position, role) "
            "VALUES (?, ?, ?, ?)",
            (item_id, author_id, parsed.position, parsed.role),
        )


def reindex_items(db, item_ids: Iterable[int], authors: str | None) -> None:
    """Replace the index rows of every id in `item_ids` with `parse(authors)`.

    Ids with no physical `items` row are skipped — an update's WHERE may name
    one, and a link to it would fail the foreign key. Trashed rows are
    indexed like any other (see the module docstring).
    """
    ids = sorted(set(item_ids))
    if not ids:
        return
    marks = ", ".join("?" for _ in ids)
    # The physical table: the index covers trashed rows too, which the view
    # hides (G107's "find the row the view hides" class).
    present = [
        r[0] for r in db.execute(
            f"SELECT id FROM items WHERE id IN ({marks})", ids
        ).fetchall()
    ]
    if not present:
        return
    marks = ", ".join("?" for _ in present)
    previous = [
        r[0] for r in db.execute(
            f"SELECT DISTINCT author_id FROM item_authors WHERE item_id IN ({marks})",
            present,
        ).fetchall()
    ]
    db.execute(f"DELETE FROM item_authors WHERE item_id IN ({marks})", present)
    for item_id in present:
        _link(db, item_id, authors)
    gc_orphans(db, previous)


def reindex_item(db, item_id: int, authors: str | None) -> None:
    """Replace one item's index rows with `parse(authors)`."""
    reindex_items(db, [item_id], authors)


def name_for(db, author_id) -> str | None:
    """The display name of one author, for Browse's filter chip.

    None for a value that is not an id or names no author — the filter then
    matches nothing, and the chip falls back to showing the raw value.
    """
    try:
        author_id = int(author_id)
    except (TypeError, ValueError):
        return None
    if not -(2 ** 63) <= author_id < 2 ** 63:
        return None
    row = db.execute("SELECT name FROM authors WHERE id = ?", (author_id,)).fetchone()
    return row[0] if row else None


def authors_for(db, item_id) -> list[tuple[int, str, str | None]]:
    """`(author_id, name, role)` for one item, in stored order.

    Reads only `item_authors` JOIN `authors` — the caller already knows the
    item itself is live, so there is no need to join `items_live` here.
    """
    return [
        (r["author_id"], r["name"], r["role"])
        for r in db.execute(
            "SELECT ia.author_id AS author_id, a.name AS name, ia.role AS role "
            "FROM item_authors ia JOIN authors a ON a.id = ia.author_id "
            "WHERE ia.item_id = ? ORDER BY ia.position",
            (item_id,),
        ).fetchall()
    ]


#: The `settings` key recording which `authors.PARSER_VERSION` built the
#: index. Archives exclude `settings`, so an imported library rebuilds.
VERSION_KEY = "authors_index_version"

_ORPHANS = (
    "FROM authors WHERE NOT EXISTS "
    "(SELECT 1 FROM item_authors ia WHERE ia.author_id = authors.id)"
)


def _begin(db) -> None:
    # A fresh database reaches the boot steps with an implicit transaction
    # still open, and BEGIN IMMEDIATE inside one raises.
    if db.in_transaction:
        db.commit()
    db.execute("BEGIN IMMEDIATE")


def _recorded_version(db) -> str | None:
    row = db.execute(
        "SELECT value FROM settings WHERE key = ?", (VERSION_KEY,)
    ).fetchone()
    return row[0] if row else None


def rebuild_all(db) -> str | None:
    """The boot step: build the whole index when the parse rule has changed.

    When the recorded version is absent or differs from `PARSER_VERSION` —
    the first boot after upgrading, a restored older backup, a rule change —
    every **physical** item is re-indexed (trashed rows too; see the module
    docstring), orphaned authors are deleted, and the version is recorded,
    all in one `BEGIN IMMEDIATE` transaction (G16). Author ids survive for
    every name whose key is unchanged: links are replaced, authors upserted.
    Returns one log line for `_run_migrations` to hand to its caller.

    Otherwise it only sweeps orphaned authors — a purge or a merge removes an
    item's links by cascade and leaves the author behind — and returns None.
    It writes nothing when there is nothing to sweep, so a second boot on a
    clean database changes nothing. Logs nothing itself (G3).

    Never touches `items`: the upgrade changes no item bytes.
    """
    version = str(authors_svc.PARSER_VERSION)
    if _recorded_version(db) == version:
        if db.execute(f"SELECT 1 {_ORPHANS} LIMIT 1").fetchone():
            _begin(db)
            db.execute(f"DELETE {_ORPHANS}")
            db.commit()
        return None

    _begin(db)
    if _recorded_version(db) == version:  # another boot won the lock (G18)
        db.commit()
        return None
    # The physical table: the index covers trashed rows (G107's "find the row
    # the view hides" class).
    rows = db.execute(
        "SELECT id, authors FROM items WHERE authors IS NOT NULL AND authors <> ''"
    ).fetchall()
    db.execute("DELETE FROM item_authors")
    for row in rows:
        _link(db, row[0], row[1])
    db.execute(f"DELETE {_ORPHANS}")
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (VERSION_KEY, version),
    )
    authors = db.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
    db.commit()
    return f"Built author index: {len(rows)} items, {authors} authors"
