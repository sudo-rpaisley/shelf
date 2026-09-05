"""The Browse filter set, declared once.

Every Browse filter used to be written out in four places: the `hx-include`
lists in `browse.html` and `fragments/filter_counts_oob.html` (14 hand-written
lists, each of them "every filter except my own"), three hand-maintained
condition groups in `search_items`, and two name lists in `static/js/browse.js`.
Nothing kept them in sync, and the drift was invisible to unit tests — an
include-drop only shows up when a *second* filter changes after an OOB swap.
Five user-reported issues and nine changelog fixes came out of that gap.

This module is the single declaration. Everything else derives:

- `filter_includes(exclude=...)` renders the `hx-include` selector lists
  (registered as a Jinja global in `app/main.py`).
- `build_where(values, exclude=...)` builds the SQL for the main query *and*
  for each cross-filter count group — the group for filter X is simply the
  where-clause with X excluded.
- `client_config()` is serialised into the page as JSON for `browse.js`.
- `querystring(values)` builds the load-more URL.

Adding a filter means adding one `BrowseFilter` here.
"""

import re
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence
from urllib.parse import quote

from markupsafe import Markup

from app.config import MEDIA_FAMILIES

#: Filter names become CSS attribute selectors and querystring keys, so they
#: are held to identifier syntax. `filter_includes` marks its output safe on
#: the strength of this check — without it, a name could inject markup.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# A condition builder maps a filter's raw string value to the SQL fragment and
# bound parameters it contributes. Returning None means "contributes nothing"
# — used by presentation-only filters (sort, view) and by tri-state values.
ConditionBuilder = Callable[[str], "tuple[str, list] | None"]


#: SQLite's INTEGER is a signed 64-bit value, and `sqlite3` raises
#: OverflowError at bind time for anything outside it — after the cast has
#: already succeeded, because Python ints are arbitrary precision.
_SQLITE_INT_MIN, _SQLITE_INT_MAX = -(2 ** 63), 2 ** 63 - 1

#: A condition that matches no row, for a filter value that cannot address one.
_NEVER: "tuple[str, list]" = ("1 = 0", [])


def _column(column: str, cast: Callable[[str], object] = str) -> ConditionBuilder:
    """Equality against one column.

    A value that will not cast matches nothing, which is the answer a valid
    but unused id already gives: `?location_filter=abc` and
    `?location_filter=999` both render an empty grid rather than an error.
    Query strings are shareable and bookmarkable, so a hand-edited one must
    not 500 — and both `/browse` and `/api/search` build their WHERE clause
    here, so guarding the cast covers both routes and every future `cast=`
    filter at once.

    Note it must be `_NEVER`, not `None`. Returning `None` means "contributes
    nothing" and drops the condition, which matches *everything* — the filter
    chip would say the view is narrowed to a location while the grid showed
    the whole collection.
    """
    def build(value):
        try:
            cast_value = cast(value)
        except (TypeError, ValueError):
            return _NEVER
        if isinstance(cast_value, int) and not (
            _SQLITE_INT_MIN <= cast_value <= _SQLITE_INT_MAX
        ):
            return _NEVER
        return f"{column} = ?", [cast_value]
    return build


def _search(value):
    like = f"%{value}%"
    return (
        "(i.title LIKE ? OR i.authors LIKE ? OR i.isbn LIKE ? OR i.narrator LIKE ?)",
        [like, like, like, like],
    )


def _media_family(value):
    """Match every concrete media type that belongs to one library family."""
    family = MEDIA_FAMILIES.get(value)
    if not family:
        return _NEVER
    media_types = family["types"]
    placeholders = ", ".join("?" for _ in media_types)
    return f"i.media_type IN ({placeholders})", list(media_types)


def _owned(value):
    # Tri-state: "" (either), "1" (owned), "0" (wishlist). With a user-aware
    # Browse request, build_where handles the wishlist branch specially so it
    # can resolve user_item_state while keeping owned inventory shared.
    if value == "1":
        return "i.owned = 1", []
    if value == "0":
        return "i.owned = 0", []
    return None


def _lent_out(value):
    if value != "1":
        return None
    return "i.id IN (SELECT item_id FROM checkouts WHERE checked_in IS NULL)", []


def _tag(value):
    return (
        "i.id IN (SELECT it.item_id FROM item_tags it "
        "JOIN tags t ON it.tag_id = t.id WHERE t.name = ?)",
        [value],
    )


def _series(value):
    return "i.series_name = ? COLLATE NOCASE", [value]


def _collection(value):
    return (
        "i.id IN (SELECT ci.item_id FROM collection_items ci "
        "JOIN collections c ON c.id = ci.collection_id "
        "WHERE c.name = ? COLLATE NOCASE)",
        [value],
    )


def personal_reading_status_sql(user_id: int) -> tuple[str, list]:
    """SQL expression for exactly one user's consumption status.

    Migrations 55-56 snapshot pre-feature shared state for users that exist at
    upgrade time. A missing row after that snapshot means the user has no
    personal state; it must never fall back to another person's legacy value.
    """
    expression = (
        "(SELECT uis.reading_status FROM user_item_state uis "
        "  WHERE uis.user_id = ? AND uis.item_id = i.id)"
    )
    return expression, [int(user_id)]


def personal_wishlist_sql(user_id: int) -> tuple[str, list]:
    """SQL expression for exactly one user's wishlist flag."""
    expression = (
        "COALESCE((SELECT uis.wishlist FROM user_item_state uis "
        "           WHERE uis.user_id = ? AND uis.item_id = i.id), 0)"
    )
    return expression, [int(user_id)]


@dataclass(frozen=True)
class BrowseFilter:
    """One Browse filter, in every form the app needs it.

    `name` is the control name, the querystring key and the `hx-include`
    selector all at once — they were always required to match, and now they
    cannot diverge.
    """

    name: str
    #: Chip label prefix in the UI. Empty means the value speaks for itself
    #: (Owned, Lent out), which browse.js uses to decide the label format.
    prefix: str = ""
    #: The value that means "not filtering". `sort` defaults to newest, so a
    #: sort of "newest" is inactive and stays out of the URL.
    default: str = ""
    #: None for presentation-only filters (sort, view) that participate in the
    #: querystring and hx-include but never narrow the result set.
    condition: ConditionBuilder | None = None
    #: Percent-encode the value in the load-more querystring (tag names are
    #: free text and may contain spaces or '&').
    quote_in_qs: bool = False
    #: Render a removable chip when active. False for `view`, which is a
    #: hidden control carrying the grid/list mode — real state, but not
    #: something the user thinks of as a filter.
    chip: bool = True
    #: What "clear all filters" sets this to. Defaults to `default`, so `sort`
    #: correctly returns to newest rather than empty. `None` means leave it
    #: alone — clearing filters must not throw away the chosen view mode.
    clear_to: str | None = ""
    #: Whether browse.js mirrors this into the address bar and sessionStorage.
    #: False for `view`, whose authoritative store is localStorage
    #: (`shelf-view`); it rides along to the server so the right template is
    #: rendered, but writing it to the URL would leave a bare /browse showing
    #: `?view=grid` forever and would never clear the session entry.
    in_url: bool = True

    def __post_init__(self):
        if not _NAME_RE.match(self.name):
            raise ValueError(
                f"Browse filter name {self.name!r} is not a plain identifier. "
                "Names are interpolated into CSS attribute selectors that "
                "filter_includes() marks HTML-safe, so they must match "
                f"{_NAME_RE.pattern}."
            )

    def is_active(self, value) -> bool:
        return bool(value) and value != self.default


FILTERS: tuple[BrowseFilter, ...] = (
    BrowseFilter("q", prefix="Search", condition=_search),
    BrowseFilter("media_family_filter", prefix="Family", condition=_media_family),
    BrowseFilter("media_type_filter", prefix="Type", condition=_column("i.media_type")),
    BrowseFilter("location_filter", prefix="Location", condition=_column("i.location_id", cast=int)),
    BrowseFilter("sort", prefix="Sort", default="newest", clear_to="newest"),
    BrowseFilter("reading_status", prefix="Status", condition=_column("i.reading_status")),
    BrowseFilter("owned", condition=_owned),
    BrowseFilter("lent_out", condition=_lent_out),
    BrowseFilter("tag", prefix="Tag", condition=_tag, quote_in_qs=True),
    BrowseFilter("language", prefix="Language", condition=_column("i.language")),
    BrowseFilter("series", prefix="Series", condition=_series, quote_in_qs=True),
    BrowseFilter("collection", prefix="Collection", condition=_collection, quote_in_qs=True),
    BrowseFilter("view", chip=False, clear_to=None, in_url=False),
)

BY_NAME: Mapping[str, BrowseFilter] = {f.name: f for f in FILTERS}
FILTER_NAMES: tuple[str, ...] = tuple(f.name for f in FILTERS)


def _excluded(exclude) -> frozenset:
    if exclude is None:
        return frozenset()
    if isinstance(exclude, str):
        return frozenset({exclude})
    return frozenset(exclude)


def filter_includes(exclude=None) -> Markup:
    """The `hx-include` selector list for a control, minus its own name."""
    names = _excluded(exclude)
    unknown = names - set(BY_NAME)
    if unknown:
        raise KeyError(
            f"filter_includes(exclude={sorted(names)!r}) names no such filter: "
            f"{sorted(unknown)!r}. Filters are declared in app/browse_filters.py."
        )
    return Markup(",".join(f"[name='{f.name}']" for f in FILTERS if f.name not in names))


def build_where(
    values: Mapping[str, str],
    exclude=None,
    *,
    user_id: int | None = None,
) -> "tuple[str, list]":
    """Build a WHERE clause from filter values, optionally dropping some.

    When ``user_id`` is supplied, consumption status and Wishlist are resolved
    exclusively from that user's personal-state rows. The migration snapshot
    preserves old data for existing accounts; missing rows stay genuinely
    empty for users created later. Owned inventory remains a shared catalogue
    fact.
    """
    skip = _excluded(exclude)
    conditions: list[str] = []
    params: list = []
    for f in FILTERS:
        if f.name in skip or f.condition is None:
            continue
        value = values.get(f.name, "")
        if not f.is_active(value):
            continue

        if user_id is not None and f.name == "reading_status":
            expression, expression_params = personal_reading_status_sql(user_id)
            conditions.append(f"{expression} = ?")
            params.extend(expression_params)
            params.append(value)
            continue

        if user_id is not None and f.name == "owned" and value == "0":
            expression, expression_params = personal_wishlist_sql(user_id)
            conditions.append(f"{expression} = 1")
            params.extend(expression_params)
            continue

        built = f.condition(value)
        if built is None:
            continue
        sql, sql_params = built
        conditions.append(sql)
        params.extend(sql_params)
    if not conditions:
        return "", []
    return "WHERE " + " AND ".join(conditions), params


def values_from(query_params: Mapping[str, str]) -> dict:
    """Read every declared filter out of a request's query string."""
    return {f.name: query_params.get(f.name, f.default) or f.default for f in FILTERS}


def has_active_filters(values: Mapping[str, str]) -> bool:
    """True if any *narrowing* filter is set — sort and view don't count."""
    return any(
        f.condition is not None and f.is_active(values.get(f.name, ""))
        for f in FILTERS
    )


def querystring(values: Mapping[str, str], extra: Sequence[str] = ()) -> str:
    """Querystring for the load-more URL, in FILTERS order.

    Filters with `in_url=False` are left out. Today that is `view` alone, and
    the reason is that this URL is rendered once, server-side, when page 1 is
    built — a copy of `view` baked into it goes stale the moment the user
    toggles grid/list, while the load-more sentinel's
    `hx-include="[name='view']"` reads the live hidden input on every request.
    """
    parts = []
    for f in FILTERS:
        value = values.get(f.name, "")
        if not f.in_url or not f.is_active(value):
            continue
        parts.append(f"{f.name}={quote(str(value)) if f.quote_in_qs else value}")
    parts.extend(extra)
    return "&".join(parts)


def client_config() -> list[dict]:
    """What `static/js/browse.js` needs, serialised into the page as JSON."""
    return [
        {
            "name": f.name,
            "prefix": f.prefix,
            "default": f.default,
            "chip": f.chip,
            "clearTo": f.clear_to,
            "inUrl": f.in_url,
        }
        for f in FILTERS
    ]
