"""Re-point a merged row's child records before the row is deleted (#86).

``items`` has five child tables declared ``ON DELETE CASCADE``. Deleting the
merged row therefore destroys its loan history, tags, cross-format links and
physical copies unless each one is re-pointed at the kept row first. Only
``scan_log`` and ``reading_log`` ever were, so a merge looked like it worked
and silently took the rest with it.

``item_copies`` became a casualty after the issue was written: first-class
physical copies shipped in 0.36.0, and that table carries acquisition price,
provenance, condition and copy barcode — the least reconstructible data in
Shelf.

Re-pointing is not a plain UPDATE for most of them. Each child table has its
own conflict rule, and getting one wrong raises ``IntegrityError`` mid-merge:

- ``item_tags`` is keyed on (item_id, tag_id), so a tag both rows carry
  collides.
- ``item_links`` is unique on (item_a_id, item_b_id), needs both columns
  re-pointed, and collapses into a self-link when the two rows were linked to
  each other.
- ``item_copies`` is unique on (item_id, copy_number) and has a partial unique
  index allowing one primary copy per item, so copies must be renumbered and
  at most one primary may survive.
- ``checkouts`` has no uniqueness constraint and is the only plain UPDATE.
- ``list_items`` is keyed on (list_id, item_id), so a list both rows are on
  collides — and it carries one rule the others do not, because ownership
  and wanting are mutually exclusive. ``app.services.lists.reparent`` holds
  both; the move lives there because that module is the one write path for
  the table.
"""

from app.services import item_copies, lists
from app.services.write_targets import ItemValueError
from app.media_types import is_physical_media

# The fields a merge fills on the kept row from the merged-away one when the
# kept row has none. The merge route applies it; the Duplicates page reads it too.
MERGE_FILLABLE = frozenset({
    "subtitle", "authors", "publisher", "publish_year", "page_count", "description",
    "series_name", "series_position", "narrator", "duration_mins", "isbn", "asin", "upc",
    "cover_path", "cover_edition_isbn", "cover_edition_language", "location_id", "notes",
    "abs_id", "abs_library_id", "hardcover_book_id", "hardcover_edition_id",
    "hardcover_user_book_id", "platform", "manual_value", "estimated_value", "value_updated_at",
    "language", "owned",
})


def active_loan_ids(db, item_ids) -> set[int]:
    """Which of these items are currently checked out.

    Merging two rows that are both on loan would leave the kept row with two
    open checkouts, which no surface in Shelf can represent — ``pages.py``
    resolves ``lent_to`` with a ``LIMIT 1`` subquery, so the second loan would
    exist in the database and nowhere in the UI.
    """
    ids = list(item_ids)
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT DISTINCT item_id FROM checkouts "
        f"WHERE checked_in IS NULL AND item_id IN ({marks})",
        ids,
    ).fetchall()
    return {row["item_id"] for row in rows}


def _reparent_tags(db, keep_id: int, other_id: int) -> None:
    # OR IGNORE skips a tag the kept row already carries; the leftover row
    # stays on other_id and is cleared explicitly rather than left to the
    # cascade, so this function's effect does not depend on the DELETE.
    db.execute(
        "UPDATE OR IGNORE item_tags SET item_id = ? WHERE item_id = ?",
        (keep_id, other_id),
    )
    db.execute("DELETE FROM item_tags WHERE item_id = ?", (other_id,))


def _reparent_links(db, keep_id: int, other_id: int) -> None:
    _reparent_pairs(db, "item_links", keep_id, other_id)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _combined(kept, incoming, *, identity, domain):
    """Union complementary facts; never choose between unequal values.

    Timestamps retain their range. Private values never appear in the refusal:
    a catalogue editor does not gain access to another person's notes/status.
    """
    changes = {}
    for name, value in dict(incoming).items():
        if name in identity or value is None:
            continue
        old = kept[name]
        if old is None:
            changes[name] = value
        elif name == "created_at":
            changes[name] = min(old, value)
        elif name == "updated_at":
            changes[name] = max(old, value)
        elif old != value:
            raise ItemValueError(f"Cannot merge conflicting {domain}")
    return changes


def _merge_keyed(db, table, keep_id, other_id, keys=(), *, parent="item_id", children=None):
    """Reparent a known relation inside the caller's whole-merge savepoint.

    Validate all facts on a colliding key before removing it, and move its
    children first. Release the incoming row's global UNIQUE identities before
    filling the keeper (e.g. musicbrainz_release_id); a later refusal rolls the
    source row, identities and private children back with the entire merge.
    """
    qt = _quote(table)
    qp = _quote(parent)
    rows = db.execute(f"SELECT * FROM {qt} WHERE {qp} = ?", (other_id,)).fetchall()
    for incoming in rows:
        key_sql = "".join(
            f" AND {_quote(key)} = ?" + (" COLLATE NOCASE" if key == "series_name" else "")
            for key in keys
        )
        key_values = [incoming[key] for key in keys]
        kept = db.execute(
            f"SELECT * FROM {qt} WHERE {qp} = ?{key_sql}", [keep_id, *key_values]
        ).fetchone()
        if kept is None:
            db.execute(
                f"UPDATE {qt} SET {qp} = ? WHERE {qp} = ?{key_sql}",
                [keep_id, other_id, *key_values],
            )
            continue
        changes = _combined(
            kept, incoming, identity={parent, "id", *keys}, domain=table
        )
        if children is not None:
            children(db, kept["id"], incoming["id"])
        db.execute(f"DELETE FROM {qt} WHERE {qp} = ?{key_sql}", [other_id, *key_values])
        if changes:
            assignment = ", ".join(f"{_quote(key)} = ?" for key in changes)
            db.execute(
                f"UPDATE {qt} SET {assignment} WHERE {qp} = ?{key_sql}",
                [*changes.values(), keep_id, *key_values],
            )


def _reparent_pairs(db, table, keep_id, other_id):
    qt = _quote(table)
    rows = db.execute(
        f"SELECT * FROM {qt} WHERE item_a_id = ? OR item_b_id = ?", (other_id, other_id)
    ).fetchall()
    for row in rows:
        a, b = sorted(keep_id if row[key] == other_id else row[key] for key in ("item_a_id", "item_b_id"))
        old_key = [row["item_a_id"], row["item_b_id"]]
        if a == b:
            # A relationship/dismissal of the two items with each other cannot
            # describe the single resulting identity; it becomes a self-link.
            db.execute(f"DELETE FROM {qt} WHERE item_a_id = ? AND item_b_id = ?", old_key)
            continue
        kept = db.execute(
            f"SELECT * FROM {qt} WHERE item_a_id = ? AND item_b_id = ?", (a, b)
        ).fetchone()
        if kept:
            changes = _combined(
                kept, row, identity={"id", "item_a_id", "item_b_id"}, domain=table
            )
            if changes:
                assignment = ", ".join(f"{_quote(key)} = ?" for key in changes)
                db.execute(
                    f"UPDATE {qt} SET {assignment} WHERE item_a_id = ? AND item_b_id = ?",
                    [*changes.values(), a, b],
                )
            db.execute(f"DELETE FROM {qt} WHERE item_a_id = ? AND item_b_id = ?", old_key)
        else:
            db.execute(
                f"UPDATE {qt} SET item_a_id = ?, item_b_id = ? WHERE item_a_id = ? AND item_b_id = ?",
                [a, b, *old_key],
            )


def _merge_tracks(db, keep_medium, other_medium):
    _merge_keyed(db, "music_tracks", keep_medium, other_medium, ("position",), parent="medium_id")


def _reparent_private_hardcover_links(db, keep_id, other_id):
    """Retain account-owned opaque link IDs; unequal links never get chosen.

    These identifiers live in settings rather than an item FK. An item delete
    therefore cannot cascade them safely, and suffix matching would mix users.
    """
    source = []
    for row in db.execute(
        "SELECT key, value FROM settings WHERE key GLOB 'hardcover_user_book:*:*'"
    ):
        parts = row["key"].split(":")
        if len(parts) != 3 or not parts[2].isascii() or not parts[2].isdigit():
            continue  # Unrelated opaque settings are not item-child records.
        if int(parts[2]) != other_id:
            continue
        if not parts[1].isascii() or not parts[1].isdigit() or int(parts[1]) <= 0:
            raise ItemValueError("Cannot merge ambiguous private integration links")
        key = f"hardcover_user_book:{int(parts[1])}:{keep_id}"
        kept = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if kept is not None and kept["value"] != row["value"]:
            raise ItemValueError("Cannot merge conflicting private integration links")
        source.append((row["key"], key, row["value"], kept is not None))
    for old, new, value, exists in source:
        if not exists:
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (new, value))
        db.execute("DELETE FROM settings WHERE key = ?", (old,))


def _check_series_identity(db, keep_id, other_id):
    rows = db.execute(
        "SELECT series_name FROM item_series WHERE item_id IN (?, ?) AND is_primary = 1",
        (keep_id, other_id),
    ).fetchall()
    names = {row[0].casefold() for row in rows}
    legacy = db.execute(
        "SELECT series_name, series_position FROM items WHERE id IN (?, ?) AND series_name IS NOT NULL AND TRIM(series_name) != ''",
        (keep_id, other_id),
    ).fetchall()
    names.update(row[0].strip().casefold() for row in legacy)
    if len(names) > 1:
        raise ItemValueError("Cannot merge conflicting primary series identities")
    if len({row[1] for row in legacy if row[1] is not None}) > 1:
        raise ItemValueError("Cannot merge conflicting primary series positions")


def _refuse_unknown_children(db, tables, keep_id, other_id):
    known = {
        "scan_log", "audit_entries", "reading_log", "checkouts", "item_tags", "item_links",
        "item_copies", "list_items", "user_item_state", "user_reading_log", "library_items",
        "item_series", "collection_items", "digital_holdings", "romm_records", "komga_records",
        "periodical_issues", "music_releases", "music_media", "music_identifiers", "price_history",
        "duplicate_dismissals", "item_authors",
    }
    for table in tables - known:
        for fk in db.execute(f"PRAGMA foreign_key_list({_quote(table)})"):
            if fk[2] == "items" and db.execute(
                f"SELECT 1 FROM {_quote(table)} WHERE {_quote(fk[3])} = ? LIMIT 1", (other_id,)
            ).fetchone():
                raise ItemValueError("Cannot merge an item with unsupported child records")


def _reparent_copies(db, keep_id: int, other_id: int) -> None:
    keep_has_primary = db.execute(
        "SELECT 1 FROM copies_live WHERE item_id = ? AND is_primary = 1", (keep_id,)
    ).fetchone() is not None
    highest = db.execute(
        "SELECT COALESCE(MAX(copy_number), 0) AS n FROM item_copies WHERE item_id = ?",
        (keep_id,),
    ).fetchone()["n"]
    # Physical, deliberately, and NOT the predict-a-UNIQUE-violation class
    # most of this module's exemptions are: reading copies_live here would
    # make a trashed copy of the losing item invisible, so it would stay
    # parented to `other_id` and be destroyed by the caller's cascading
    # DELETE two lines later — a restorable row lost for good. `deleted_at`
    # and `is_primary` are carried through untouched below; this read only
    # changes which rows are found, not what is done with them.
    rows = db.execute(
        "SELECT id FROM item_copies WHERE item_id = ? ORDER BY copy_number, id",
        (other_id,),
    ).fetchall()

    # Numbering continues above the kept row's highest, so no copy_number
    # collides and the kept row's own copies keep the numbers the user knows.
    # The merged row has at most one primary (its own partial unique index
    # guarantees that), so demoting every moved copy when the kept row already
    # has one leaves exactly one primary either way. A trashed copy always
    # carries is_primary = 0 by trash_copy's own demote contract (it demotes
    # in the same statement that stamps deleted_at), so moving one along with
    # the rest can never mint a second primary.
    for offset, row in enumerate(rows, start=1):
        fields = {"item_id": keep_id, "copy_number": highest + offset}
        if keep_has_primary:
            fields["is_primary"] = 0
        item_copies.update_copy(db, row["id"], fields)


def reparent_children(db, keep_id: int, other_id: int) -> None:
    """Preserve fork and native children, or refuse the entire operation.

    Runs inside the caller's merge transaction before its DELETE. It also owns
    a savepoint, so a caller catching a refusal cannot commit partial child
    moves. The ROUTE must roll back its entire multi-item merge on any refusal
    (including earlier targets and field fills); this helper never commits.
    The only disposable child is item_authors, a derived authors-string index.
    """
    from app.services import catalogue_security, holdings
    from app.services.item_write import _write_scope

    if keep_id == other_id:
        raise ItemValueError("Cannot merge an item into itself")
    with _write_scope(db):
        for item_id in (keep_id, other_id):
            row = catalogue_security.require_item_role(db, item_id, "editor")
            if row is None or row["deleted_at"] is not None:
                raise ItemValueError("Cannot merge missing or trashed items")
        tables = _tables(db)
        _refuse_unknown_children(db, tables, keep_id, other_id)
        if "settings" in tables:
            _reparent_private_hardcover_links(db, keep_id, other_id)
        if "checkouts" in tables and len(active_loan_ids(db, (keep_id, other_id))) > 1:
            raise ItemValueError("Cannot merge items that are both on loan")
        if "library_items" in tables:
            mappings = db.execute(
                "SELECT DISTINCT library_id FROM library_items WHERE item_id IN (?, ?)",
                (keep_id, other_id),
            ).fetchall()
            if len(mappings) > 1:
                raise ItemValueError("Cannot merge conflicting library identities")
        keeper = db.execute("SELECT media_type FROM items WHERE id = ?", (keep_id,)).fetchone()
        if "item_copies" in tables and not is_physical_media(keeper["media_type"]) and db.execute(
            "SELECT 1 FROM item_copies WHERE item_id = ? LIMIT 1", (other_id,)
        ).fetchone():
            raise ItemValueError("Cannot merge physical copies into a nonphysical item")
        if "item_series" in tables:
            _check_series_identity(db, keep_id, other_id)
        # Project legacy connector ids BEFORE deleting their parent; the
        # physical/connector owner keeps plural holdings and copy placements.
        # Do not mint a placeholder before moving the existing source copies.
        holdings.sync_item(db, keep_id, create_copy=False)
        holdings.sync_item(db, other_id, create_copy=False)
        keyed = (
            ("user_item_state", ("user_id",)),
            ("item_series", ("series_name",)),
            ("collection_items", ("collection_id",)),
            ("digital_holdings", ("provider", "external_id")),
            ("periodical_issues", ()),
            ("music_releases", ()),
            ("music_identifiers", ("identifier_type", "value")),
        )
        for table, keys in keyed:
            if table in tables:
                _merge_keyed(db, table, keep_id, other_id, keys)
        if "music_media" in tables:
            _merge_keyed(db, "music_media", keep_id, other_id, ("position",), children=_merge_tracks)
        for table in (
            "scan_log", "audit_entries", "reading_log", "user_reading_log", "checkouts",
            "romm_records", "komga_records", "price_history",
        ):
            if table in tables:
                db.execute(f"UPDATE {_quote(table)} SET item_id = ? WHERE item_id = ?", (keep_id, other_id))
        if "item_tags" in tables:
            _reparent_tags(db, keep_id, other_id)
        if "item_links" in tables:
            _reparent_links(db, keep_id, other_id)
        if "duplicate_dismissals" in tables:
            _reparent_pairs(db, "duplicate_dismissals", keep_id, other_id)
        if "item_copies" in tables:
            _reparent_copies(db, keep_id, other_id)
        if "list_items" in tables:
            lists.reparent(db, keep_id, other_id)
        # Keep BOTH security mappings available while copy/connector funnels
        # authorize their source and destination parents.
        if "library_items" in tables:
            _merge_keyed(db, "library_items", keep_id, other_id)
