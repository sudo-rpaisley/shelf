"""The one place that inserts a row into `items`, and the one place that
updates user-supplied fields on one.

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

**The value stage** (issue #54). Field *names* were the first invariant;
values are the second. `validate_item_fields()` enforces, once, every value
rule an item row carries — the ISBN check digit and the canonical 13/10 pair,
`media_type` membership, location existence, game-platform membership, the
reading-status domain, the `owned` flag — and raises a typed `ItemValueError`
subclass when one fails. `insert_item()` runs it before building the
statement, and so do the two update funnels:

- `update_item_fields(db, item_id, fields)` — one row, always stamps
  `updated_at`; with empty `fields` it is a bare touch.
- `update_items_fields(db, item_ids, fields)` — the bulk form, one statement.

Every route that writes a user-supplied value goes through one of the three
and reduces to `except ItemValueError as e`, rendering the message on its own
surface. Only system-managed columns (`cover_path`, `estimated_value`,
Hardcover ids, the `location_id = NULL` cascades, name-keyed series writes)
still use a raw `UPDATE items SET`; `tests/test_item_write.py` allowlists
those and fails on any other.

`items.location_id` is also the compatibility seam for first-class physical
copies (#97). After library assignment, one holdings projection explicitly
converts this legacy flat ID to the copy's node domain, preserving precise
child placements and never moving secondary copies. Initial copy creation is
requested only for a new item or an explicit owned=0 -> owned=1 acquisition;
existing copy history, including Trash, suppresses creation. Routine metadata
or location updates, even those repeating owned=1, preserve intentional
zero-copy archive states. An eligible new/acquired copy may be unlocated.

**The virtual `wishlisted` field** (issue #125). Wishlist membership is not a
column on `items` — it is a row in `list_items`, and `app/services/lists.py`
is the only module that writes that table. All three funnels accept
`wishlisted: bool` anyway, popped before the name check so it never reaches
the statement, and applied through `lists.set_membership` on the same
connection inside the caller's transaction. One rule lives here and survives
the follow-on plan: **writing `owned = 1` removes wishlist membership** — you
do not wish for what you have — which is what makes Shelf Fill's promotion
correct without it knowing the list exists.

The single contradiction the funnel refuses is an *owned* item on the
wishlist, and it is refused **before anything is written** (G85): an archive
import catches per-item exceptions and carries on, so a raise after the
insert would leave a half-written record behind. Ownership is judged
effective rather than submitted — the `SCHEMA` default on insert, the row's
current value on a partial update — so `wishlisted=True` with no `owned` key
is a refusal, not a silent contradiction. `owned = 0` *without* `wishlisted`
writes no membership at all: a writer that forgets the field is a bug the
per-writer pins catch, not something this funnel papers over.

A field that is not present is not validated: an update that touches only
`notes` never reads `isbn`. Callers that hold a *provider's* value (an
Audiobookshelf ASIN, a Hardcover edition ISBN) pre-clean it with
`isbn.canonical_isbn_pair()` and pass `None` on failure — the funnel is strict
in both cases; dropping versus refusing is the caller's decision.

Provider-specific metadata can pass ``series_memberships`` and callers that
already know the security target can pass ``library_id`` as pseudo-fields.
They are consumed after insertion rather than written to ``items``. The insert
funnel is also the central hook for Shelf's additive related-media and holding
projections.

**Call these inside an existing `with get_db() as db:` block**, never around
one. The caller owns the transaction: several sites need the insert and their
follow-up writes (tags, scan log, cover path) to commit together, and
`cursor.lastrowid` is only meaningful on the connection that did the insert
(G16, G18).
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from itertools import count
from typing import Any, Iterable, Mapping

from app.config import MEDIA_TYPES, STATUS_MEDIA_TYPES, canonical_media_type
from app.database import get_game_platforms
from app.services import author_index
from app.services import isbn as isbn_svc
from app.services import lists
from app.services import tags
from app.services import trash
from app.services import catalogue_security, libraries, user_state
from app.services.write_targets import (  # noqa: F401 — re-exported
    IdentifierInTrash,
    ItemValueError,
    UnknownLocationError,
    validated_location_id,
)

#: The reading-status domain. `items.py` and `reading_imports.py` used to each
#: spell this out; it is declared once here and read from both.
READING_STATUSES = ("want_to_read", "reading", "read")
_savepoints = count()
_UNSET = object()


@contextmanager
def _write_scope(db):
    """Lock, authorize and project atomically, without committing caller work.

    A per-call savepoint matters for importers which catch an individual error
    and then commit other records. Validation/projection failure must never
    leave a half-insert, restore, private write or bulk update behind.
    """
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    else:
        # Even an explicitly deferred transaction must obtain its write lock
        # before the permission/collision reads. No rows or timestamps change.
        db.execute("UPDATE items SET updated_at = updated_at WHERE 0")
    name = f"item_funnel_{next(_savepoints)}"
    db.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        db.execute(f"ROLLBACK TO {name}")
        db.execute(f"RELEASE {name}")
        raise
    else:
        db.execute(f"RELEASE {name}")


def _atomic_write(function):
    @wraps(function)
    def wrapped(db, *args, **kwargs):
        with _write_scope(db):
            return function(db, *args, **kwargs)
    return wrapped


def _library_target(db, requested):
    target = libraries.DEFAULT_LIBRARY_ID if requested in (None, "") else int(requested)
    catalogue_security.require_library_role(db, target, "editor")
    return target


def _pop_personal(db, values):
    if catalogue_security.current_actor() is None:
        return {}
    personal = {name: values.pop(name) for name in user_state.PERSONAL_FIELDS if name in values}
    personal = validate_item_fields(db, personal)
    return user_state.validate_state_changes(personal)


def _apply_personal(db, item_ids, changes):
    actor = catalogue_security.current_actor()
    if changes and actor is not None:
        for item_id in item_ids:
            user_state.save_state(db, actor["id"], item_id, **changes)


def _acquiring_item_ids(db, item_ids, fields):
    """Explicit 0->1 acquisitions, read live under the caller's write lock.

    A repeated owned=1 is metadata refresh, not evidence of acquisition. Capture
    this set BEFORE the update; the holdings owner decides whether history or
    physical eligibility permits an initial copy after library assignment.
    """
    if "owned" not in fields or _coerce_owned(fields["owned"]) != 1:
        return set()
    ids = list(item_ids)
    if not ids:
        return set()
    marks = ", ".join("?" for _ in ids)
    return {
        row["id"] for row in db.execute(
            f"SELECT id FROM items_live WHERE id IN ({marks}) AND owned = 0", ids
        ).fetchall()
    }


def _project_item(db, item_id, values, series_values=None, *, inserted=False, acquired=False):
    """One physical/digital handoff; no independent primary-copy projection.

    Must run after library assignment. Only insertion or a proven explicit
    acquisition asks for creation; holdings also preserves existing copy history.
    Merge preflight/reparenting must not request an initial copy before moving
    source copies. Post-reparent ownership promotion sees those retained copies.
    """
    from app.services import holdings, series_memberships, media_groups

    if series_values is not None or {"series_name", "series_position"} & values.keys():
        series_memberships.add_metadata_memberships(db, item_id, series_values)
    if inserted and media_groups.should_autolink_on_insert(values.get("source")):
        media_groups.auto_link_item(db, item_id)
    holdings.sync_item(db, item_id, create_copy=inserted or acquired)


def _refuse_hidden_collisions(db, values, *, exclude_id=None):
    actor = catalogue_security.current_actor()
    if actor is None:
        return
    for field in ("isbn", "upc"):
        if values.get(field) is None:
            continue
        hits = db.execute(
            f"SELECT id FROM items WHERE {field} = ? AND media_type = ? AND id != ?",
            (values[field], values.get("media_type") or "book", exclude_id or -1),
        ).fetchall()
        if any(not libraries.has_item_role(db, actor, hit["id"], "viewer") for hit in hits):
            # Do not disclose title/id/Trash status, and do not reassign or
            # resurrect a hidden row even if the account edits another library.
            raise IdentifierUnavailable("This identifier is unavailable")


class InvalidIsbn(ItemValueError):
    code = "invalid_isbn"
    field = "isbn"


class UnknownMediaType(ItemValueError):
    code = "unknown_media_type"
    field = "media_type"


class UnknownPlatform(ItemValueError):
    code = "unknown_platform"
    field = "platform"


class InvalidReadingStatus(ItemValueError):
    code = "invalid_reading_status"
    field = "reading_status"


class InvalidOwned(ItemValueError):
    code = "invalid_owned"
    field = "owned"


class InvalidWishlisted(ItemValueError):
    code = "invalid_wishlisted"
    field = "wishlisted"


class IdentifierUnavailable(ItemValueError):
    """A hidden identifier slot; deliberately contains no item id or title."""

    code = "identifier_unavailable"


#: Columns a caller may never set on insert — the database owns them.
#: `deleted_at` is owned by `trash_item` / `restore_item` below and by nothing
#: else. Both funnels build their `SET` clause from caller-supplied field
#: names, so refusing the name here is what makes "exactly four writers" true
#: of behaviour rather than only of the spelling a source pin can grep for:
#: without it, `update_item_fields(db, id, {"deleted_at": ...})` would reach
#: the column through the ordinary update path.
_MANAGED = frozenset({"id", "deleted_at"})
#: On update, `created_at` joins the list: it is set once, by SQLite.
_MANAGED_ON_UPDATE = frozenset({"id", "created_at", "deleted_at"})

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


def _validated_names(db, values: Mapping[str, Any], managed: frozenset[str],
                     who: str) -> None:
    """Refuse an unknown or database-managed field name, loudly."""
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
            f"{who}() got field(s) not on the items table: "
            f"{sorted(unknown)}. Add the column to both SCHEMA and MIGRATIONS "
            "in app/database.py (G1), or fix the spelling."
        )

    hit = set(values) & managed
    if hit:
        raise ValueError(
            f"{who}() cannot set {sorted(hit)} — the database assigns it."
        )


def validate_item_fields(db, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Apply every value invariant to the fields present; return a new dict.

    Pure with respect to the connection except for the two existence lookups
    (locations, game_platforms). Never mutates its argument. Fields that are
    not present are not validated — an update touching only `notes` never
    reads `isbn`.

    Raises the matching `ItemValueError` subclass: `InvalidIsbn`,
    `UnknownMediaType`, `UnknownLocationError`, `UnknownPlatform`,
    `InvalidReadingStatus`, `InvalidOwned`.
    """
    out: dict[str, Any] = dict(fields)

    # ISBN — the canonical pair rewrites BOTH columns whenever either is
    # supplied, so an inconsistent isbn10 a caller passed is overwritten and
    # a 979 gives isbn10 = None. `""` clears; explicit None clears both.
    if "isbn" in out or "isbn10" in out:
        raw = out.get("isbn")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raw = None if "isbn" in out else out.get("isbn10")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            out["isbn"] = None
            out["isbn10"] = None
        else:
            pair = isbn_svc.canonical_isbn_pair(str(raw))
            if pair is None:
                raise InvalidIsbn(f"Invalid ISBN: {raw}", value=raw)
            out["isbn"], out["isbn10"] = pair

    if "media_type" in out:
        # The backstop for every write path — insert, update and bulk update
        # alike. Routes canonicalise earlier, above their own duplicate
        # guards, because a guard that compares the raw value would miss a
        # twin stored under the canonical one; this is what guarantees
        # nothing retired can ever reach the table.
        mt = canonical_media_type(out["media_type"])
        out["media_type"] = mt
        if mt not in MEDIA_TYPES:
            raise UnknownMediaType(f"Unknown media type: {mt!r}", value=mt)

    if "location_id" in out:
        loc = out["location_id"]
        if isinstance(loc, str):
            loc = loc.strip()
            if not loc:
                loc = None
            else:
                try:
                    loc = int(loc)
                except ValueError:
                    raise UnknownLocationError(
                        f"Location {loc!r} not found", value=loc
                    ) from None
        out["location_id"] = validated_location_id(db, loc)

    if "platform" in out:
        plat = out["platform"]
        if isinstance(plat, str):
            plat = plat.strip() or None
        if plat is not None and plat not in get_game_platforms(db):
            raise UnknownPlatform(f"Unknown game platform: {plat!r}", value=plat)
        out["platform"] = plat

    if "reading_status" in out:
        status = out["reading_status"]
        if isinstance(status, str):
            status = status.strip() or None
        if status is not None and status not in READING_STATUSES:
            raise InvalidReadingStatus(
                f"Invalid reading status: {status!r}", value=status
            )
        out["reading_status"] = status

    if "owned" in out:
        out["owned"] = _coerce_owned(out["owned"])

    return out


def _coerce_owned(value: Any) -> int:
    """`owned` as 0 or 1, or raise `InvalidOwned`.

    Factored out of `validate_item_fields` because two other callers need the
    *same* acceptance before validation has run: `_refuse_owned_wishlist`
    below, and `_apply_membership`'s promotion arm. Both see the caller's raw
    mapping — `validate_item_fields` normalises a copy — so a bare
    `values.get("owned") == 1` there would miss the `"1"` this funnel
    deliberately accepts, and silently leave a promoted item on the wishlist.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value.strip() in ("0", "1"):
        return int(value.strip())
    if isinstance(value, int) and value in (0, 1):
        return value
    raise InvalidOwned("Owned must be 0 or 1", value=value)


def _pop_wishlisted(values: dict[str, Any]) -> bool | None:
    """Remove the virtual `wishlisted` key and return it as a bool, or None.

    `wishlisted` is not a column on `items` — it is membership of the wishlist
    in `list_items`. Popping it here is what keeps `_validated_names` from
    rejecting it as "not on the items table" and keeps it out of the SQL
    statement, exactly as `_execute_update` already pops `updated_at`.
    """
    if "wishlisted" not in values:
        return None
    value = values.pop("wishlisted")
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise InvalidWishlisted("Wishlisted must be true or false", value=value)


def _refuse_owned_wishlist(db, wishlisted: bool | None, values: Mapping[str, Any],
                          item_ids: Iterable[int] | None = None) -> None:
    """Refuse the one contradiction the funnel does not allow: an owned item
    on the wishlist. **Runs before any SQL this call writes.**

    Ordering is the whole point (G85). `archive.py`'s `apply_plan` catches
    per-item exceptions into an `errors` list and carries on inside one shared
    transaction, so a raise *after* the insert leaves the item, its copies and
    its tags committed while the report tells the user that item failed. The
    same shape makes an "unchanged row" test untrue: the `db` fixture is one
    `get_db()` block per test and `pytest.raises` swallows the exception
    inside it.

    Ownership is judged *effective*, not submitted. On insert an absent
    `owned` means the `SCHEMA` default of 1, and on a partial update it means
    whatever the row already holds — so `values.get("owned")` alone would let
    `insert_item(db, title="X", wishlisted=True)` create the forbidden state.
    """
    if wishlisted is not True:
        return

    if "owned" in values:
        if _coerce_owned(values["owned"]) == 1:
            raise InvalidWishlisted("An owned item cannot be on the wishlist")
        return

    if item_ids is None:
        # Insert with no `owned` key: the column default applies, and it is 1.
        raise InvalidWishlisted("An owned item cannot be on the wishlist")

    ids = list(item_ids)
    if not ids:
        return
    marks = ", ".join("?" for _ in ids)
    # Trusted Trash metadata work must still respect real shared ownership.
    sql = (f"SELECT 1 FROM items WHERE id IN ({marks}) AND owned = 1"
           if catalogue_security.current_actor() is None else
           f"SELECT 1 FROM items_live WHERE id IN ({marks}) AND owned = 1")
    if db.execute(sql, ids).fetchone():
        raise InvalidWishlisted("An owned item cannot be on the wishlist")


def _apply_membership(db, item_ids: Iterable[int], wishlisted: bool | None,
                      values: Mapping[str, Any]) -> None:
    """Write wishlist membership after the row write.

    Ordered after the item statement and the single holdings projection (G96 —
    the two side tables are independent, but the order is stated so nobody
    moves it). This function only writes; the contradiction was already
    refused by `_refuse_owned_wishlist` before anything was written.
    """
    ids = list(item_ids)
    if not ids:
        return
    if wishlisted is not None:
        lists.set_membership(db, lists.WISHLIST, ids, wishlisted)
    elif "owned" in values and _coerce_owned(values["owned"]) == 1:
        # You do not wish for what you have. This is what makes Shelf Fill's
        # promotion correct without it knowing the list exists.
        lists.set_membership(db, lists.WISHLIST, ids, False)


class ItemId(int):
    """An item id that also says whether the insert funnel *restored* it.

    An `int` subclass rather than a tuple or a dataclass, because
    `insert_item` has 18 call sites and every one of them — plus every test
    double that returns a bare `int` — would otherwise have to change in one
    commit, with a missed one becoming a runtime `TypeError` on a path no E2E
    covers (Komga sync, RomM).

    **Probed 2026-09-20, and these are the limits:** it binds as a sqlite3
    parameter, `json.dumps` renders it as a bare number, and it formats in an
    f-string — so every existing caller keeps working untouched. But
    **arithmetic or `int()` returns a plain `int`**, which silently drops the
    flag. Read `was_restored` *before* the id is transformed or stored
    anywhere that normalises it.
    """

    restored: bool

    def __new__(cls, value, *, restored: bool = False):
        obj = super().__new__(cls, value)
        obj.restored = restored
        return obj


def was_restored(item_id) -> bool:
    """Whether `insert_item` restored this id rather than creating it.

    Takes any int. A plain `int` — from a test double, or from an id that has
    been through arithmetic — answers `False`, which is the safe default: a
    caller that has lost the flag reports "added", never a restore that did
    not happen.
    """
    return isinstance(item_id, ItemId) and item_id.restored


def _trashed_twin(db, values: Mapping[str, Any]):
    """The trashed row holding a slot this insert is about to claim, or None.

    Returns `(row, field)` where `field` is `"isbn"` or `"upc"`. **ISBN wins**
    when the two identifiers hit different rows — it is the more specific
    claim, and the one a person scanning a book is acting on.

    Reads the **physical** table on purpose, because the constraints do: a
    trashed row never gave up its `UNIQUE(isbn, media_type)` or
    `UNIQUE(upc, media_type)` slot (G107). This lookup needs **no live-first
    ordering**, unlike the four non-unique reads elsewhere in this program: a
    trashed row holding a unique slot means there *is* no live holder.

    Ownership is judged on the *effective* row (G85) — an absent `media_type`
    is the `SCHEMA` default `'book'`, so a caller who omits it is matched
    against the slot the insert would actually claim, not against NULL.

    `upc` is tested `is not None` rather than for truthiness: the UPC index is
    partial (`WHERE upc IS NOT NULL`), so an empty string claims a real slot
    while NULL claims none.

    No identifier in the write means no lookup and no cost.
    """
    media_type = values.get("media_type") or "book"

    isbn = values.get("isbn")
    if isbn:
        row = db.execute(
            "SELECT id, title FROM items "
            "WHERE isbn = ? AND media_type = ? AND deleted_at IS NOT NULL "
            "LIMIT 1",
            (isbn, media_type),
        ).fetchone()
        if row is not None:
            return row, "isbn"

    upc = values.get("upc")
    if upc is not None:
        row = db.execute(
            "SELECT id, title FROM items "
            "WHERE upc = ? AND media_type = ? AND deleted_at IS NOT NULL "
            "LIMIT 1",
            (upc, media_type),
        ).fetchone()
        if row is not None:
            return row, "upc"

    return None


def restore_intent(values: Mapping[str, Any]) -> tuple[dict[str, Any], bool | None]:
    """Check the ownership intent `apply_restored_ownership` will act on, and
    return `(values, wishlisted)` in the shape it takes.

    For a caller that found the trashed row itself rather than through
    `insert_item` — the archive import's `restore` verdict. `insert_item`
    validates before its first write, and so must that caller: the import
    catches a per-record error and then commits, so an `InvalidOwned` or
    `InvalidWishlisted` raised *after* `restore_item` would leave a restored
    row beside a report saying the record failed (G85). Raises exactly what
    the funnel would.
    """
    out = dict(values)
    wishlisted = _pop_wishlisted(out)
    if "owned" in out:
        out["owned"] = _coerce_owned(out["owned"])
    return out, wishlisted


def apply_restored_ownership(db, item_id: int, values: Mapping[str, Any],
                              wishlisted: bool | None) -> None:
    """Carry the caller's ownership intent onto a row just restored.

    **Ownership only ever moves toward owned**, which is the rule the existing
    duplicate guards already follow (G100): an add-mode re-add of a trashed
    *wishlist* row yields an owned item, and a wishlist-mode re-add of a
    trashed *owned* row leaves it owned rather than demoting something the
    user already has.

    Effective, not submitted: an absent `owned` on an insert is the `SCHEMA`
    default of 1, so a plain `insert_item(db, title=...)` states the intent
    "owned" even though it names no ownership at all.

    A `wishlisted=True` intent is **skipped, not refused**, when the stored row
    is owned — refusing would turn an ordinary wishlist-mode scan into an
    error about a row the user cannot see.
    """
    incoming_owned = _coerce_owned(values["owned"]) if "owned" in values else 1
    stored = db.execute(
        "SELECT owned FROM items_live WHERE id = ?", (item_id,)
    ).fetchone()
    if stored is None:  # pragma: no cover — restored one statement ago
        return

    if incoming_owned == 1:
        if not stored["owned"]:
            update_item_fields(db, item_id, {"owned": 1})
        return

    if wishlisted and not stored["owned"]:
        update_item_fields(db, item_id, {"wishlisted": True})


@_atomic_write
def insert_item(db, fields: Mapping[str, Any] | None = None, *,
                restore_trashed: bool = True, **kwargs) -> int:
    """Insert one row into `items` and return its id.

    Accepts a dict, keyword arguments, or both. Fields whose value is not
    supplied are simply left out of the statement, so the column defaults in
    `SCHEMA` apply — `source` becomes 'manual', `owned` becomes 1,
    `media_type` becomes 'book', and `created_at`/`updated_at` are stamped by
    SQLite. That means the defaults live in exactly one place too.

    ``series_memberships`` and ``library_id`` are deliberate non-column inputs.
    The former stores richer provider series metadata; the latter selects the
    item's Shelf security library. Until every add/import UI exposes a target,
    an omitted ``library_id`` means the upgrade-compatible ``Main Library``.

    Raises `ValueError` on an unknown or database-managed field rather than
    dropping it. A typo in a column name is the failure this module exists to
    make impossible, so it must be loud.

    Raises `ItemValueError` (a `ValueError`) — `InvalidIsbn`,
    `UnknownMediaType`, `UnknownLocationError`, `UnknownPlatform`,
    `InvalidReadingStatus`, `InvalidOwned` — when a value fails its
    invariant; see `validate_item_fields`. `sqlite3.IntegrityError` still
    reaches the caller — which now means a **lost race**, exactly as the
    handlers' comments say, rather than an ordinary duplicate: a live twin is
    still caught by the callers' own guards, and a trashed twin is resolved
    here.

    **A third outcome: the slot is held by an item in Trash.** Before the
    `INSERT`, a write carrying an `isbn` or a `upc` looks for a trashed row
    holding the slot it is about to claim, and then:

    - **restores it** (the default), returning that row's id with
      `was_restored` true and **no other stored field changed** — not the
      title, not `location_id`, not `source`. A person re-adding something
      they deleted gets it back as they left it, not overwritten by whatever
      the provider says today. The caller's ownership intent is applied, and
      only ever toward owned (see `apply_restored_ownership`).
      One exception: inside a `default_reading_status` block, a restored
      row whose status is NULL takes the default. A status the row already
      has is never overwritten.
    - **refuses**, with `restore_trashed=False`, raising `IdentifierInTrash`
      and writing nothing. That is the machine path: a background sync must
      not resurrect what a person deleted.

    The return is an `ItemId` — an `int` subclass every existing caller can
    keep treating as an `int`. Read the flag with `was_restored(item_id)`
    **before** the id is transformed (see `ItemId`).
    """
    values: dict[str, Any] = dict(fields or {})
    values.update(kwargs)
    series_values = values.pop("series_memberships", None)
    requested_library = values.pop("library_id", None)
    wishlisted = _pop_wishlisted(values)

    if not values.get("title"):
        raise ValueError(
            "insert_item() requires a non-empty 'title' — items.title is NOT "
            "NULL, and a blank title is unrecoverable in the UI."
        )

    pending_status = _pending_status()
    if (pending_status is not None and values.get("reading_status") is None
            and _status_type(values.get("media_type"))):
        values["reading_status"] = pending_status

    personal = _pop_personal(db, values)
    _validated_names(db, values, _MANAGED, "insert_item")
    values = validate_item_fields(db, values)
    _refuse_owned_wishlist(db, wishlisted, values)
    _refuse_hidden_collisions(db, values)

    # The collision-with-Trash rule. It runs after validation — so the slot is
    # looked up with the canonical ISBN, not the caller's spelling — and
    # **before the first write**, so a refusal leaves nothing behind (G85).
    hit = _trashed_twin(db, values)
    if hit is not None:
        row, field = hit
        catalogue_security.require_item_role(db, row["id"], "editor", include_deleted=True)
        existing_library = libraries.item_library_id(db, row["id"])
        if requested_library not in (None, "") and int(requested_library) != existing_library:
            raise ItemValueError("This identifier is unavailable in the requested library")
        if not restore_trashed:
            raise IdentifierInTrash(
                f"“{row['title']}” is in Trash and already uses this "
                f"{field.upper()}. Restore it from Trash, or delete it "
                "permanently, before adding this item.",
                value=values.get(field),
                field=field,
                item_id=row["id"],
                title=row["title"],
            )
        if restore_item(db, row["id"]):
            apply_restored_ownership(db, row["id"], values, wishlisted)
            if pending_status is not None:
                _restore_default_status(db, row["id"], pending_status)
            tags.attach_pending(db, row["id"])
            return ItemId(row["id"], restored=True)
        # The row stopped being trashed between the lookup and the write —
        # most add routes hold no `BEGIN IMMEDIATE` around their insert, so
        # this is reachable (G18). Fall through to the INSERT and let the
        # constraint answer, rather than returning an id we did not restore.

    library_id = _library_target(db, requested_library)
    names = list(values)
    placeholders = ", ".join("?" for _ in names)
    cursor = db.execute(
        f"INSERT INTO items ({', '.join(names)}) VALUES ({placeholders})",
        [values[n] for n in names],
    )
    item_id = ItemId(cursor.lastrowid)
    libraries.assign_item(db, item_id, library_id, new_item=True)
    _project_item(db, item_id, values, series_values, inserted=True)
    _apply_personal(db, [item_id], personal)
    _apply_membership(db, [item_id], wishlisted, values)
    tags.attach_pending(db, item_id)
    # The author index (#117b). Not on the restored-twin return above: a
    # restore changes no field, and the trashed row's index is still there.
    if values.get("authors"):
        author_index.reindex_item(db, item_id, values["authors"])
    return item_id


_pending_reading_status: ContextVar = ContextVar(
    "pending_default_reading_status", default=None
)


@contextmanager
def default_reading_status(raw):
    """Within this block, every item `insert_item` files with no status of
    its own takes `raw` as its reading status, if its type has one.

    `raw` is checked on entry, before the body runs: blank or None means no
    default, anything outside `READING_STATUSES` raises
    `InvalidReadingStatus`. An add route enters the block before its lookup
    or its insert, so a bad value files nothing.

    The holder is emptied on exit, not only unset: a task spawned inside the
    block copies the context, and must not apply it later (as
    `tags.default_tags`).
    """
    status = raw.strip() if isinstance(raw, str) else raw
    status = status or None
    if status is not None and status not in READING_STATUSES:
        raise InvalidReadingStatus(
            f"Invalid reading status: {status!r}", value=status
        )
    holder = {"status": status}
    token = _pending_reading_status.set(holder)
    try:
        yield
    finally:
        holder["status"] = None
        _pending_reading_status.reset(token)


def _pending_status() -> str | None:
    """The enclosing `default_reading_status` block's value, else None."""
    holder = _pending_reading_status.get()
    return holder["status"] if holder else None


def _status_type(media_type) -> bool:
    """Whether a row of `media_type` carries a reading status. A missing
    type is the `SCHEMA` default, `book`."""
    return canonical_media_type(media_type or "book") in STATUS_MEDIA_TYPES


def _restore_default_status(db, item_id: int, status: str) -> None:
    """Give a just-restored row the block's status, only when it has none.

    Same connection as the restore (G118). A reading-only update requests no
    initial copy creation; actor-private updates skip shared projections entirely.
    """
    row = db.execute(
        "SELECT reading_status, media_type FROM items_live WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row and row["reading_status"] is None and _status_type(row["media_type"]):
        update_item_fields(db, item_id, {"reading_status": status})


def trashed_title(db, item_id: int) -> str | None:
    """The title of `item_id` **if it is in Trash**, else None.

    One physical read, for a surface that must name the row blocking an edit.
    The router sends the id and the code; the sentence and the escaping live
    in the template (G58).
    """
    actor = catalogue_security.current_actor()
    if actor is not None and not libraries.has_item_role(db, actor, item_id, "viewer"):
        return None
    row = db.execute(
        "SELECT title FROM items WHERE id = ? AND deleted_at IS NOT NULL",
        (item_id,),
    ).fetchone()
    return row["title"] if row else None


def refuse_trash_collision(db, where: str, where_params: list[Any],
                           values: Mapping[str, Any]) -> None:
    """Refuse an update that would move a live row onto a trashed row's slot.

    The mirror of `insert_item`'s rule, and deliberately **not** symmetric
    with it: this one cannot restore, because the user is editing a
    *different* item and resurrecting someone else's row is the wrong repair.
    It refuses, naming the trashed title and both ways out.

    **It fires on a `media_type`-only change too**, not only on an identifier.
    The slot is `(isbn, media_type)`, so `bulk_update` and Komga's
    `_reclassify_owned_record` can both move a live row onto a trashed row's
    slot without touching an identifier at all — which is how this would
    otherwise reach the database as an `IntegrityError`.

    Skipped entirely unless `values` carries one of the three: a location
    move, a reading-status change and the wishlist writes stay free of any
    lookup at all.

    **Every target is checked before the `UPDATE` runs** (G85), so a bulk
    update over a mixed selection refuses whole and moves nothing — a partial
    application would be the worse outcome, since the user cannot see which
    half landed.

    The collision census includes Trash because the constraints do (G107).
    HTTP targets remain scoped/live; explicit actorless maintenance also checks
    its trashed targets before attempting an identifier change.
    """
    if not {"isbn", "upc", "media_type"} & set(values):
        return

    sql = (f"SELECT id, isbn, upc, media_type FROM items WHERE {where}"
           if catalogue_security.current_actor() is None else
           f"SELECT id, isbn, upc, media_type FROM items_live WHERE {where}")
    targets = db.execute(sql, where_params).fetchall()

    for row in targets:
        # Effective values: what the row will hold once this update lands.
        isbn = values["isbn"] if "isbn" in values else row["isbn"]
        upc = values["upc"] if "upc" in values else row["upc"]
        media_type = (
            values["media_type"] if "media_type" in values else row["media_type"]
        ) or "book"
        _refuse_hidden_collisions(
            db, {"isbn": isbn, "upc": upc, "media_type": media_type}, exclude_id=row["id"]
        )

        if isbn:
            hit = db.execute(
                "SELECT id, title FROM items WHERE isbn = ? AND media_type = ? "
                "AND deleted_at IS NOT NULL AND id != ?",
                (isbn, media_type, row["id"]),
            ).fetchone()
            if hit is not None:
                raise IdentifierInTrash(
                    f"“{hit['title']}” is in Trash and already uses this ISBN. "
                    "Restore it from Trash, or delete it permanently, before "
                    "using this ISBN here.",
                    value=isbn, field="isbn",
                    item_id=hit["id"], title=hit["title"],
                )

        if upc is not None:
            hit = db.execute(
                "SELECT id, title FROM items WHERE upc = ? AND media_type = ? "
                "AND deleted_at IS NOT NULL AND id != ?",
                (upc, media_type, row["id"]),
            ).fetchone()
            if hit is not None:
                raise IdentifierInTrash(
                    f"“{hit['title']}” is in Trash and already uses this UPC. "
                    "Restore it from Trash, or delete it permanently, before "
                    "using this UPC here.",
                    value=upc, field="upc",
                    item_id=hit["id"], title=hit["title"],
                )


def _execute_update(db, fields: Mapping[str, Any], where: str,
                    where_params: list[Any], who: str) -> dict[str, Any]:
    """Validate names and values, then run the one UPDATE this module holds.

    `updated_at` is always stamped, so an empty `fields` is a bare touch.
    Returns the normalised values so compatibility projections can use exactly
    what was written rather than re-parsing the caller's raw input.

    Raises `IdentifierInTrash` before the statement when the update would
    claim a slot a trashed row holds — see `refuse_trash_collision`.
    """
    values = dict(fields)
    if "cover_path" in values:
        values.setdefault("cover_edition_isbn", None)
        values.setdefault("cover_edition_language", None)
    values.pop("updated_at", None)
    _validated_names(db, values, _MANAGED_ON_UPDATE, who)
    values = validate_item_fields(db, values)
    refuse_trash_collision(db, where, where_params, values)
    # HTTP metadata writes remain live and request-scoped. Trusted maintenance
    # retains Trash updates/reindexing, without writing managed lifecycle fields
    # or restoring/projecting a physical copy for a trashed parent.
    if catalogue_security.current_actor() is not None:
        where = f"({where}) AND id IN (SELECT id FROM items_live)"
    assignments = [f"{n} = ?" for n in values]
    assignments.append("updated_at = datetime('now')")
    db.execute(
        f"UPDATE items SET {', '.join(assignments)} WHERE {where}",
        [*(values[n] for n in values), *where_params],
    )
    return values


def _existing_update_ids(db, ids):
    """Rows actually written: HTTP live-only, trusted maintenance includes Trash."""
    marks = ", ".join("?" for _ in ids)
    if catalogue_security.current_actor() is None:
        rows = db.execute(f"SELECT id FROM items WHERE id IN ({marks})", ids).fetchall()
    else:
        rows = db.execute(f"SELECT id FROM items_live WHERE id IN ({marks})", ids).fetchall()
    return [row["id"] for row in rows]


@_atomic_write
def update_item_fields(db, item_id: int, fields: Mapping[str, Any], *,
                       create_copy_on_acquisition: bool = True) -> None:
    """Update user-supplied fields on one item through the value stage.

    Same name and value contract as `insert_item` (managed on update: `id`,
    `created_at`); always stamps `updated_at`. Raises `ItemValueError` on a
    bad value, `ValueError` on a bad name.

    Merging existing identities is not a new acquisition: its caller disables
    initial-copy creation after reparenting the source's real copy history.
    """
    fields = dict(fields)
    catalogue_security.require_item_role(db, item_id, "editor")
    series_values = fields.pop("series_memberships", None)
    library_id = fields.pop("library_id", _UNSET)
    personal = _pop_personal(db, fields)
    if library_id is not _UNSET:
        library_id = _library_target(db, library_id)
    wishlisted = _pop_wishlisted(fields)
    _refuse_owned_wishlist(db, wishlisted, fields, [item_id])
    acquiring = _acquiring_item_ids(db, [item_id], fields)

    personal_only = bool(personal) and not fields and wishlisted is None and series_values is None and library_id is _UNSET
    values = {} if personal_only else _execute_update(db, fields, "id = ?", [item_id], "update_item_fields")
    existing = _existing_update_ids(db, [item_id])
    if existing and "authors" in values:
        author_index.reindex_item(db, item_id, values["authors"])
    for existing_id in existing:
        if library_id is not _UNSET:
            libraries.assign_item(db, existing_id, library_id)
        if not personal_only:
            _project_item(db, existing_id, values, series_values,
                          acquired=create_copy_on_acquisition and existing_id in acquiring)
    _apply_personal(db, existing, personal)
    _apply_membership(db, existing, wishlisted, fields)


@_atomic_write
def update_items_fields(db, item_ids: Iterable[int],
                        fields: Mapping[str, Any]) -> None:
    """Bulk form of `update_item_fields`: one statement, validated once."""
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        return
    marks = ", ".join("?" for _ in ids)

    fields = dict(fields)
    for item_id in ids:
        catalogue_security.require_item_role(db, item_id, "editor")
    series_values = fields.pop("series_memberships", None)
    library_id = fields.pop("library_id", _UNSET)
    personal = _pop_personal(db, fields)
    if library_id is not _UNSET:
        library_id = _library_target(db, library_id)
    wishlisted = _pop_wishlisted(fields)
    _refuse_owned_wishlist(db, wishlisted, fields, ids)
    acquiring = _acquiring_item_ids(db, ids, fields)

    personal_only = bool(personal) and not fields and wishlisted is None and series_values is None and library_id is _UNSET
    values = {} if personal_only else _execute_update(db, fields, f"id IN ({marks})", ids, "update_items_fields")
    existing_ids = _existing_update_ids(db, ids)
    if "authors" in values and existing_ids:
        author_index.reindex_items(db, existing_ids, values["authors"])
    for item_id in existing_ids:
        if library_id is not _UNSET:
            libraries.assign_item(db, item_id, library_id)
        if not personal_only:
            _project_item(db, item_id, values, series_values, acquired=item_id in acquiring)
    _apply_personal(db, existing_ids, personal)
    _apply_membership(db, existing_ids, wishlisted, fields)


def promote_wishlisted(db, item_id: int) -> bool:
    """Mark a wishlisted item owned — Add mode scanning a book you bought.

    Returns True when the item was on the wishlist and is now owned (the
    `owned = 1` write removes the membership, see `_apply_membership`), and
    False, writing nothing, for an owned or a neither item (#125).

    Runs on the caller's connection and transaction, and callers hold
    `BEGIN IMMEDIATE` across the duplicate read that found `item_id` (G18).
    It never logs: a log handler opening its own connection would wait on
    that same lock (G3).
    """
    if not lists.is_member(db, lists.WISHLIST, item_id):
        return False
    update_item_fields(db, item_id, {"owned": 1})
    return True


@_atomic_write
def trash_item(db, item_id: int, *, at: str | None = None) -> bool:
    """Move one item to Trash, and return whether this call moved it.

    Stamps `deleted_at`, which hides the row from `items_live` and — because
    `copies_live` joins the items relation — every one of its copies, with no
    write to `item_copies` at all. `scan_log`, loans and wishlist membership
    are left exactly as they are, so a restore finds the item as the user left
    it.

    `at`, when given, replaces the current time as the value written into
    `deleted_at` (via `COALESCE(?, datetime('now'))`). This funnel does not
    validate it — it is trusted verbatim. The one caller that will ever pass
    a non-None value is the archive import, replaying a source's own deletion
    timestamp so the retention clock does not restart on re-import; that
    caller validates `at` before this function's first write. Every existing
    caller passes nothing, and gets `datetime('now')` exactly as before.

    Guarded on the current state, so a second call on an already-trashed row
    writes nothing and returns `False`. That is what lets a caller treat the
    return as "I am the one who trashed it" rather than re-reading the row.

    The three soft delete sites call this: the item page's and Browse's
    Delete (`routers/items.delete_item`) and the Audiobookshelf excluded-
    library cleanup. Only Trash's Delete permanently removes the row
    (`services/trash.purge_item`).

    Caller must hold the write lock. The row is not re-read first — the guard
    is in the statement, which is one serialized unit, rather than in a bare
    `SELECT` that would take no lock under sqlite3's deferred isolation (G18).
    It never logs: a log handler opening its own connection would wait on that
    same lock (G3).
    """
    catalogue_security.require_item_role(db, item_id, "editor", include_deleted=True)
    cursor = db.execute(
        "UPDATE items SET deleted_at = COALESCE(?, datetime('now')), "
        "updated_at = datetime('now') "
        "WHERE id = ? AND deleted_at IS NULL",
        (at, item_id),
    )
    trash.invalidate(db)
    return cursor.rowcount > 0


@_atomic_write
def restore_item(db, item_id: int) -> bool:
    """Bring one item back from Trash, and return whether this call moved it.

    The mirror of `trash_item`, guarded the same way: a row that is not
    trashed is left alone and `False` comes back. **A restore cannot collide** —
    a trashed row never gave up its `UNIQUE(isbn, media_type)` or
    `UNIQUE(upc, media_type)` slot, so nothing else can have taken it
    meanwhile.

    The `False` return is load-bearing for `insert_item`'s restore path: most
    add routes hold no `BEGIN IMMEDIATE` around their insert, so the row can
    stop being trashed between that path's lookup and this write. Falling
    through to the ordinary `INSERT` on `False` is what keeps the funnel from
    returning an id it did not restore (G18).

    Restoring an item does not touch its copies: their own `deleted_at` was
    never written, so they return with it. Same lock and logging rules as
    `trash_item`.
    """
    catalogue_security.require_item_role(db, item_id, "editor", include_deleted=True)
    cursor = db.execute(
        "UPDATE items SET deleted_at = NULL, updated_at = datetime('now') "
        "WHERE id = ? AND deleted_at IS NOT NULL",
        (item_id,),
    )
    restored = cursor.rowcount > 0
    if restored:
        trash.settle_marker(db)
    trash.invalidate(db)
    return restored
