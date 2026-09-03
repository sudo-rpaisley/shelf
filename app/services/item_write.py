"""The one place that inserts a row into `items`.

`INSERT INTO items` used to exist at 13 sites — `_save_item`, manual add, the
scan path, CSV import, photo-intake confirm, Hardcover sync and discover, ABS
sync, the store's bare-wishlist fallback, the game/DVD/book adds, and archive
import. G25 recorded the consequence: adding a metadata column meant auditing
all 13 and deciding capture-or-gap at each, and a plan that assumed
"everything funnels through `_save_item`" silently stored NULL for the new
`language` column on the headline photo-intake path.

`insert_item()` is that funnel. Adding a column to `items` now means adding it
to `SCHEMA` and `MIGRATIONS` (still both — see G1) and passing it wherever it
is actually known; no site can silently drop it, because an unknown field name
raises instead of being ignored.

It is also the common hook for related-media discovery on ordinary inserts and
for automatic series membership persistence. Provider batch syncs
(Audiobookshelf, Komga and RomM) defer related-media grouping until the end of
their batch for efficiency; manual/scanned/catalogue additions can join an
existing safe same-work group immediately. Explicit provider series are stored
in ``item_series`` at insert time so Series rows do not depend on first opening
the item-detail page.

**Call it inside an existing `with get_db() as db:` block**, never around one.
The caller owns the transaction: several sites need the insert and their
follow-up writes (tags, scan log, cover path) to commit together, and
`cursor.lastrowid` is only meaningful on the connection that did the insert
(G16, G18).
"""

from typing import Any, Mapping

#: Columns a caller may never set — the database owns them.
_MANAGED = frozenset({"id"})

# ``series_memberships`` is a richer metadata input rather than an items-table
# column. It is consumed after the item insert by the shared membership helper.
_PSEUDO_FIELDS = frozenset({"series_memberships"})

# Cached column set for the `items` table. Read from the live schema rather
# than hardcoded, so this cannot drift from SCHEMA/MIGRATIONS the way a
# transcribed list would — which is the whole point of the module.
_columns: frozenset[str] | None = None


def item_columns(db) -> frozenset[str]:
    """Every column on `items`, cached after the first read."""
    global _columns
    if _columns is None:
        _columns = _read_columns(db)
    return _columns


def _read_columns(db) -> frozenset[str]:
    rows = db.execute("PRAGMA table_info(items)").fetchall()
    if not rows:
        raise RuntimeError(
            "PRAGMA table_info(items) returned no rows — the items table does "
            "not exist on this connection. insert_item() must be called on an "
            "initialised database."
        )
    # sqlite3.Row indexes by name; a bare tuple has the name at position 1.
    return frozenset(r["name"] if hasattr(r, "keys") else r[1] for r in rows)


def reset_column_cache() -> None:
    """Drop the cached column set. For tests that build a schema by hand."""
    global _columns
    _columns = None


def insert_item(db, fields: Mapping[str, Any] | None = None, **kwargs) -> int:
    """Insert one row into `items` and return its id.

    Accepts a dict, keyword arguments, or both. Fields whose value is not
    supplied are simply left out of the statement, so the column defaults in
    `SCHEMA` apply — `source` becomes 'manual', `owned` becomes 1,
    `media_type` becomes 'book', and `created_at`/`updated_at` are stamped by
    SQLite. That means the defaults live in exactly one place too.

    ``series_memberships`` is the one deliberate non-column input: providers
    that know more than one explicit series can pass a list of name/position
    records and Shelf will persist them after the item row exists.

    Raises `ValueError` on any other unknown or database-managed field rather
    than dropping it. A typo in a column name is the failure this module exists
    to make impossible, so it must be loud.
    """
    values: dict[str, Any] = dict(fields or {})
    values.update(kwargs)
    series_values = values.pop("series_memberships", None)

    if not values.get("title"):
        raise ValueError(
            "insert_item() requires a non-empty 'title' — items.title is NOT "
            "NULL, and a blank title is unrecoverable in the UI."
        )

    columns = item_columns(db)
    unknown = set(values) - columns
    if unknown:
        # A stale cache is the benign explanation (a migration added a column
        # after the first insert of this process), so re-read once before
        # blaming the caller.
        columns = _read_columns(db)
        globals()["_columns"] = columns
        unknown = set(values) - columns
    if unknown:
        raise ValueError(
            f"insert_item() got field(s) not on the items table: "
            f"{sorted(unknown)}. Add the column to both SCHEMA and MIGRATIONS "
            "in app/database.py (G1), or fix the spelling."
        )

    managed = set(values) & _MANAGED
    if managed:
        raise ValueError(
            f"insert_item() cannot set {sorted(managed)} — the database "
            "assigns it."
        )

    names = list(values)
    placeholders = ", ".join("?" for _ in names)
    cursor = db.execute(
        f"INSERT INTO items ({', '.join(names)}) VALUES ({placeholders})",
        [values[n] for n in names],
    )
    item_id = cursor.lastrowid

    # Persist the primary legacy series immediately and add any additional
    # explicit memberships supplied by a richer metadata provider. This makes
    # new books/comics/games appear in Series without waiting for a later page
    # load to reconcile the compatibility fields.
    if values.get("series_name") or series_values:
        from app.services import series_memberships as series_svc

        series_svc.add_metadata_memberships(db, item_id, series_values)

    # A normal add should become part of an existing same-work group right
    # away. Integration syncs insert many rows at once and group once at the
    # end of the batch instead, avoiding repeated collection-wide scans.
    from app.services import media_groups
    if media_groups.should_autolink_on_insert(values.get("source")):
        media_groups.auto_link_item(db, item_id)

    return item_id
