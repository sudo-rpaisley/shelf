"""The generic CSV import's row mapping.

Split out of `app/routers/items_csv.py` to keep that route under its line cap
in `tests/test_module_sizes.py`. The route imports this module and calls
through it (`csv_import.resolve_state(...)`) so tests can patch.
"""

from dataclasses import dataclass

from app.services import item_copies, location_tree, locations, user_state
from app.services.catalogue_security import require_item_role
from app.services.item_write import update_item_fields, validate_item_fields

# LibraryThing columns that land on a new item's copy (normalize_librarything's
# `copy`), and are reported as `existing_item` on any other row.
LT_ACQUISITION_COLUMNS = (
    "acquired", "date_acquired", "from_where", "purchase_price", "condition",
)


def identity_media_types(norm: dict, media: str) -> tuple[str, ...]:
    """Match a retained identity first; only a miss may use the new-input slot."""
    return tuple(dict.fromkeys((norm.get("identity_media_type", media), media)))


@dataclass(frozen=True)
class RowLocation:
    node_id: int
    legacy_id: int | None


def export_locations(db, rows, *, user=None) -> dict[int, str | None]:
    """Exact primary NODE paths; only genuinely copy-less rows use the flat seam.

    `rows` is the already library/Trash-authorized CSV selection. Ordinary
    copies use copies_live. Authorized parent Trash needs the narrow physical
    read below because copies_live deliberately hides that parent's children.
    """
    result = {row["id"]: row["location_name"] for row in rows}
    live_ids = [row["id"] for row in rows if row["deleted_at"] is None]
    primaries = []
    for start in range(0, len(live_ids), 500):
        chunk = live_ids[start:start + 500]
        primaries.extend(db.execute(
            "SELECT item_id, location_id FROM copies_live WHERE is_primary = 1 "
            f"AND item_id IN ({','.join('?' for _ in chunk)})", chunk,
        ).fetchall())
    for row in rows:
        if row["deleted_at"] is not None:
            require_item_role(db, row["id"], "editor", user=user, include_deleted=True)
            primaries.extend(db.execute(
                "SELECT item_id, location_id FROM item_copies WHERE item_id = ? "
                "AND is_primary = 1 AND deleted_at IS NULL", (row["id"],),
            ).fetchall())
    seen = set()
    for copy in primaries:
        if copy["item_id"] in seen:
            raise locations.LocationError("Item has ambiguous primary copies")
        seen.add(copy["item_id"])
        result[copy["item_id"]] = " / ".join(
            part["name"] for part in location_tree.location_path(db, copy["location_id"])
        ) or None
    return result


def place_location(db, item_id: int, location: RowLocation) -> None:
    """Move only the existing primary via its writer; never synthesize a copy.

    That writer explicitly projects NODE -> nearest mapped FLAT ancestor. A
    zero-copy row can retain a flat label, but cannot represent a precise child
    or an unmapped node without inventing a copy or losing the requested path.
    Refuse that unrepresentable row atomically instead.
    """
    primary = db.execute(
        "SELECT id, location_id FROM copies_live WHERE item_id = ? AND is_primary = 1", (item_id,),
    ).fetchone()
    if primary is not None:
        seam = db.execute("SELECT location_id FROM items_live WHERE id = ?", (item_id,)).fetchone()
        if primary["location_id"] == location.node_id and seam["location_id"] == location.legacy_id:
            return
        item_copies.update_copy(db, primary["id"], {"location_id": location.node_id})
        return
    path = location_tree.location_path(db, location.node_id)
    if (location.legacy_id is None or len(path) != 1
            or path[0]["legacy_location_id"] != location.legacy_id):
        raise locations.LocationError("A precise CSV location requires an existing primary copy")
    update_item_fields(db, item_id, {"location_id": location.legacy_id}, create_copy_on_acquisition=False)


def save_personal_state(db, user_id: int, item_id: int, norm: dict,
                        owned: bool, to_read_wishlist: bool) -> None:
    """Map tracker activity to one actor, never the shared acquisition list."""
    changes = {}
    if norm.get("reading_status"):
        changes["reading_status"] = norm["reading_status"]
    if norm.get("date_finished"):
        changes["date_finished"] = norm["date_finished"]
    if norm.get("wishlisted") is not None:
        changes["wishlist"] = int(norm["wishlisted"])
    elif to_read_wishlist and not owned and norm.get("reading_status") == "want_to_read":
        changes["wishlist"] = 1
    if changes:
        user_state.save_state(db, user_id, item_id, **changes)


def resolve_state(norm: dict, to_read_wishlist: bool,
                  tracker: bool) -> tuple[bool, bool, bool, bool]:
    """Resolve a row's (owned, wishlisted) once, and whether it states each.

    `owned` comes from the normaliser untouched. A generic row with no
    `owned` value is one of two things: a 0.41.1-era export, where
    `wishlisted=1` meant not owned, or any other file, where the historical
    default is owned (applied on insert; an update leaves the row alone).

    `wishlisted`, in order: an owned row is never on the wishlist (the funnel
    would refuse the pair, and an import must not fail a row that only says
    "I have it" — G96); else the file's own `wishlisted` column; else the
    to-read option. A present `wishlisted=0` is honoured, not replaced by the
    option (G87).
    """
    owned = norm["owned"]
    column_wish = norm["wishlisted"]
    if tracker:
        # A tracker's wishlist is personal intent, NOT the household's shared
        # acquisition list. The caller saves it through user_state separately.
        return bool(owned), False, owned is not None, False
    if owned is None and column_wish:
        owned = False
    owned_given = owned is not None
    if owned is None:
        owned = True

    if owned:
        wishlisted = False
    elif column_wish is not None:
        wishlisted = column_wish
    else:
        wishlisted = to_read_wishlist and norm["reading_status"] == "want_to_read"

    # A reading-tracker row states both flags. A generic row states
    # `wishlisted` only through its column, or by being owned (rule 1).
    wishlisted_given = tracker or column_wish is not None or (owned_given and owned)
    return owned, wishlisted, owned_given, wishlisted_given


# Audiobook fields an import writes on insert, and in update mode only into
# an empty column (see update_from_row).
FILL_EMPTY = ("subtitle", "narrator", "duration_mins", "description", "asin")


def match_audiobook(db, asin: str, title: str, authors: str | None):
    """Find the audiobook an Audible export row describes: `(hit, rung)`.

    The ASIN rung first, then title and authors against audiobooks that hold
    no ASIN yet (an Audiobookshelf row often has an ISBN and, until its next
    sync, no ASIN). `rung` is "asin" or "title"; `(None, None)` on a miss.

    Both reads use the physical table and order a live row first: the ASIN
    rung must find a trashed twin so the route restores it (G107, "find the
    row the view hides"), and the title rung follows the existing title
    rung's "live wins" rule. Neither has a UNIQUE constraint behind it.
    """
    hit = db.execute(
        "SELECT id, deleted_at FROM items WHERE asin = ? AND media_type = 'audiobook' "
        "ORDER BY deleted_at IS NOT NULL, id LIMIT 1",
        (asin,),
    ).fetchone()
    if hit:
        return hit, "asin"
    hit = db.execute(
        "SELECT id, deleted_at FROM items WHERE "
        "TRIM(title) = TRIM(?) COLLATE NOCASE AND "
        "TRIM(COALESCE(authors, '')) = TRIM(?) COLLATE NOCASE AND "
        "media_type = 'audiobook' AND asin IS NULL "
        "ORDER BY deleted_at IS NOT NULL, id LIMIT 1",
        (title, authors or ""),
    ).fetchone()
    return (hit, "title") if hit else (None, None)


def update_from_row(db, item_id: int, row: dict, *, generic: bool = False,
                    location: RowLocation | None = None):
    """Update an existing item from CSV row data (non-empty fields only).

    Generic rows also carry the parsed round-trip fields (#145): each applies
    only when the normalizer produced a value, so an empty cell is no opinion.
    `estimated_value` is never applied here — the valuation route owns it.
    The `FILL_EMPTY` audiobook fields apply to every format, and only into a
    column the existing row leaves empty.
    A generic location names a precise NODE, not the flat compatibility seam.
    Move only the existing primary after metadata; preserve secondary copies
    and deliberate zero-copy states. No import path creates a replacement copy.
    """
    field_map = {
        "authors": "authors", "author": "authors",
        "publisher": "publisher",
        "publish_year": "publish_year", "year": "publish_year",
        "page_count": "page_count", "pages": "page_count",
        "series_name": "series_name", "series": "series_name",
    }
    updates = {}
    for csv_key, db_key in field_map.items():
        val = (row.get(csv_key) or "").strip()
        if val and db_key not in updates:
            if db_key in ("publish_year", "page_count"):
                updates[db_key] = int(val) if val.isdigit() else None
            else:
                updates[db_key] = val
    # Fill-only fields (any format): written only where the row holds none,
    # because an Audiobookshelf-synced row's values come from the user's own
    # files and an import must not overwrite them.
    fill = {k: row[k] for k in FILL_EMPTY if row.get(k) not in (None, "")}
    if fill:
        current = db.execute(
            f"SELECT {', '.join(fill)} FROM items_live WHERE id = ?", (item_id,)
        ).fetchone()
        for key, val in fill.items():
            if current is not None and current[key] in (None, ""):
                updates.setdefault(key, val)
    if generic:
        # Already parsed by normalize_generic — never through .strip() above.
        for key in ("language", "series_position", "manual_value", "platform"):
            if row.get(key) is not None:
                updates[key] = row[key]
    if updates:
        update_item_fields(db, item_id, updates)
    if generic and location is not None:
        place_location(db, item_id, location)


def precheck_row(db, norm: dict) -> dict:
    """Raise now what the funnel would raise later, before any write (G85).

    Validate the raw ISBN and media type even without a location/platform,
    preserving the funnel's native diagnostics and canonical ISBN pair. A
    location may be created and a matched item restored only after this
    read-only value check succeeds.
    """
    fields = {"isbn": norm["isbn"], "media_type": norm["media_type"]}
    if norm.get("platform") is not None:
        fields["platform"] = norm["platform"]
    return validate_item_fields(db, fields)


class RowLocations:
    """One import's location lookups: check early, create late (G85).

    `check()` is read-only and runs with the row's other pre-checks;
    `ensure()` may write and is called only where the row is about to be
    written, so a skipped or failed row creates no location. Names are
    cached per request, and `created` lists each new name once, in order.
    """

    def __init__(self, db):
        self.db = db
        self._ids: dict[str, RowLocation] = {}
        self.created: list[str] = []

    def check(self, name: str) -> RowLocation | None:
        key = name.casefold()
        if key in self._ids:
            return self._ids[key]
        node = locations.find_node_by_path(self.db, name)
        legacy = locations.find_by_path(self.db, name)
        if node is not None:
            if legacy is not None and location_tree.legacy_location_to_node(self.db, legacy) != node:
                raise locations.LocationError(f'location "{name}" is ambiguous')
            found = RowLocation(node, location_tree.node_to_legacy_location(self.db, node))
        elif legacy is not None:
            # A historical flat label may not have its explicit map yet.
            # Checking stays read-only; ensure() creates the map only on write.
            mapped = location_tree.legacy_location_to_node(self.db, legacy)
            if mapped is None:
                return None
            found = RowLocation(mapped, legacy)
        else:
            found = None
        if found is None and " / " in name and legacy is None:
            raise locations.LocationError(f'location "{name}" not found')
        if found is not None:
            self._ids[key] = found
        return found

    def ensure(self, name: str) -> RowLocation:
        key = name.casefold()
        if key not in self._ids:
            found = self.check(name)
            if found is not None:
                return found
            legacy, created = locations.resolve_or_create(self.db, name)
            node = location_tree.legacy_location_to_node(self.db, legacy, create=True)
            if created:
                self.created.append(name)
            self._ids[key] = RowLocation(node, legacy)
        return self._ids[key]

    def checkpoint(self):
        return dict(self._ids), list(self.created)

    def rollback(self, checkpoint):
        self._ids, self.created = checkpoint
