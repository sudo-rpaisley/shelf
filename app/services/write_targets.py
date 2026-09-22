"""The base error for every value invariant on an item write, and the
location rule the write funnel calls.

`ItemValueError` is what `app.services.item_write` raises when a value
fails its invariant — a bad ISBN check digit, an unknown media type, a
location id that no longer exists, an unknown game platform, an
out-of-domain reading status or `owned` flag. Each concrete failure is a
subclass carrying a stable `code` (the key a template switches on) and the
`field` it belongs to; the message is the user-readable sentence. Routes
reduce to one `except ItemValueError as e` and render it on their own
surface — a scan card, a redirect with `?error=<code>`, a JSON body.

It lives here rather than in `item_write` because `UnknownLocationError`
pre-dates the funnel (#76) and `item_write` imports this module; putting
the base below both keeps `except ItemValueError` able to catch a location
failure without an import cycle.

`validated_location_id` is the location rule. Routes may still call it
before doing outbound work — a bad target should not cost a provider
lookup — but the funnel calls it on every insert and update that carries
`location_id`, so no write reaches SQLite's foreign-key error for a stale
or tampered id. SQLite's exception stays the final invariant, not a
user-facing control-flow mechanism.
"""

from typing import Any


class ItemValueError(ValueError):
    """A value on an item write failed its invariant.

    `code` is stable and template-facing; `field` names the column. The
    message is the sentence a user can read. `value` is kept for callers
    that want to log what was refused without re-parsing the message.
    """

    code: str = "item_value"
    field: str = ""

    def __init__(self, message: str, *, value: Any = None):
        super().__init__(message)
        self.value = value


class UnknownLocationError(ItemValueError):
    """A positive location id was supplied but no such location exists."""

    code = "unknown_location"
    field = "location_id"


class IdentifierInTrash(ItemValueError):
    """The ISBN or UPC this write claims is held by an item in Trash.

    Raised by the insert funnel when the caller passed
    `restore_trashed=False` — a machine re-syncing, which must not resurrect
    something a person deleted — and by the update funnel always, since an
    edit is moving a *different* item onto the slot and restoring someone
    else's row would be the wrong repair.

    Carries `item_id` and `title` of the trashed row so the surface can name
    it. The message states both ways out, because "that ISBN is taken" is
    unactionable when the thing taking it is invisible.
    """

    code = "identifier_in_trash"

    def __init__(self, message: str, *, value: Any = None, field: str = "isbn",
                 item_id: int | None = None, title: str | None = None):
        super().__init__(message, value=value)
        # Per-instance: the slot is an ISBN on one write and a UPC on the
        # next, so unlike its siblings this one cannot be a class attribute.
        self.field = field
        self.item_id = item_id
        self.title = title


def validated_location_id(db, location_id: int | None) -> int | None:
    """Return a usable location id, or ``None`` for the no-location sentinel.

    Existing forms historically treat zero/negative values as "no location";
    keep that compatibility. A positive id is different: it represents an
    explicit selection and must still exist when the mutation reaches the
    server, otherwise the caller gets ``UnknownLocationError`` rather than a
    later SQLite ``IntegrityError``.
    """
    if location_id is None or location_id <= 0:
        return None
    row = db.execute(
        "SELECT 1 FROM locations WHERE id = ?",
        (location_id,),
    ).fetchone()
    if row is None:
        raise UnknownLocationError(
            f"Location {location_id} not found", value=location_id
        )
    return location_id
