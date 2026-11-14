"""The additive Library's local, read-only catalogue and personal saved views.

Filtering is per item (and source + availability per holding), then explicit
format components are built/counted, ordered and finally paginated. Nothing
uses Browse's filters, the all-edge Related Media graph or remote series IDs.
Python's Unicode casefold is deliberate: SQLite NOCASE is ASCII-only.

``search_catalog`` returns template/JSON-safe dictionaries. Transactions belong
to the caller: reads may share a snapshot; saved-view writes must be guarded by
BEGIN IMMEDIATE *before* calling ``save_view`` (including its count check).
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping
from fractions import Fraction
from urllib.parse import urlencode

from app import config, features, media_type_filters
from app.i18n import N_
from app.services import availability, format_groups
from app.services.lists import WISHLISTED_SQL


MAX_SAVED_VIEWS = 50
DEFAULT_FILTERS = {
    "q": "", "media_type": "", "types": "", "family": "", "series": "",
    "ownership": "", "wishlist": "", "source": "", "availability": "",
    "view": "groups", "sort": "title", "page_size": "24",
}
FILTER_KEYS = frozenset(DEFAULT_FILTERS)
FAMILIES = {
    "book": (N_("Books and comics"), config.BOOK_MEDIA_TYPES),
    "music": (N_("Music"), config.MUSIC_MEDIA_TYPES),
    "periodical": (N_("Periodicals"), config.PERIODICAL_MEDIA_TYPES),
    "video": (N_("Video"), frozenset({"dvd", "digital_video", "tv_series"})),
    "game": (N_("Games"), frozenset({"video_game"})),
}
AVAILABILITY_CHOICES = (
    ("", N_("Any availability")), ("available", N_("Available")),
    ("digital", N_("Available digitally")), ("physical", N_("On my shelves")),
    ("lent", N_("Lent out")), ("missing", N_("Missing copies")),
    ("unavailable", N_("Unavailable holdings")), ("unknown", N_("Unknown availability")),
)
SORT_CHOICES = (
    ("title", N_("Title")), ("author", N_("Creator")),
    ("series", N_("Series position")), ("recent", N_("Recently added")),
)
PRESETS = (
    (N_("All"), {}), (N_("Available digitally"), {"availability": "digital"}),
    (N_("On my shelves"), {"availability": "physical"}),
    (N_("Wishlist"), {"wishlist": "yes"}),
    (N_("Available"), {"availability": "available"}),
    (N_("Lent out"), {"availability": "lent"}),
)
_PROVIDER_FEATURES = {
    "abs": "abs_sync", "audiobookshelf": "abs_sync", "abs_sync": "abs_sync",
    "komga": "komga", "romm": "romm",
}
_SEARCH_FIELDS = (
    "title", "subtitle", "authors", "isbn", "isbn10", "upc", "asin",
    "narrator", "series_name", "publisher", "abs_id", "hardcover_book_id",
    "hardcover_edition_id", "hardcover_user_book_id",
)
_ITEM_COLUMNS = (
    "id", "title", "subtitle", "authors", "isbn", "isbn10", "upc", "asin",
    "media_type", "cover_path", "publisher", "narrator", "series_name",
    "series_position", "owned", "created_at", "abs_id", "hardcover_book_id",
    "hardcover_edition_id", "hardcover_user_book_id",
)
_IDENTIFIER_KEYS = frozenset({
    "isbn", "isbn10", "isbn13", "upc", "ean", "asin", "imdb", "tmdb", "tvdb",
    "igdb", "discogs", "musicbrainz", "musicbrainzalbum", "musicbrainzreleasegroup",
    "musicbrainzrelease", "openlibrary", "googlebooks", "hardcover", "goodreads",
})


def positive_int(value, default: int = 1, maximum: int = 1_000_000) -> int:
    """Bound untrusted page/ID input without accepting signs, floats or bools."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return default
    text = str(value)
    if not text.isascii() or not text.isdecimal() or len(text) > 19:
        return default
    number = int(text)
    return number if 0 < number <= maximum else default


def _text(value, *, maximum=300) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        return ""
    if any(unicodedata.category(c) == "Cc" for c in value):
        return ""
    return value.strip()


def normalize_filters(values: Mapping | None = None) -> dict[str, str]:
    """Only known, bounded, scalar filters survive; invalid legacy values reset.

    No SQL, pagination, user IDs, redirect targets or saved-view IDs can be
    stored here. Empty values intentionally clear a saved view's filter.
    Invalid new ``types`` selections raise rather than silently matching all.
    """
    values = values if isinstance(values, Mapping) else {}
    result = dict(DEFAULT_FILTERS)
    result["types"] = ",".join(media_type_filters.parse_types(values.get("types", "")))
    for key in ("q", "series"):
        result[key] = _text(values.get(key, ""))
    result["series"] = format_groups.series_name_key(result["series"])
    choices = {
        "media_type": config.MEDIA_TYPES,
        "family": FAMILIES,
        "ownership": ("owned", "unowned", "neither"),
        "wishlist": ("yes", "no"),
        "availability": tuple(key for key, _ in AVAILABILITY_CHOICES),
        "view": ("groups", "items", "series"),
        "sort": tuple(key for key, _ in SORT_CHOICES),
    }
    for key, allowed in choices.items():
        value = values.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    source = values.get("source", "")
    if isinstance(source, str) and (
        source in {"physical", "abs", "komga", "romm"}
        or re.fullmatch(r"source:[1-9][0-9]{0,18}", source)
    ):
        if not source.startswith("source:") or int(source[7:]) <= 2**63 - 1:
            result["source"] = source
    result["page_size"] = str(positive_int(values.get("page_size"), 24, 100))
    return result


def library_url(filters: Mapping | None = None, *, page: int = 1,
                saved_view: int | None = None, **overrides) -> str:
    """Internal GET links with canonical filters, never a caller-supplied path.

    A saved-view link carries even empty overrides: omitting one would silently
    resurrect the saved value on the next page. Ordinary links stay compact.
    """
    values = normalize_filters({**(filters or {}), **overrides})
    params = {k: v for k, v in values.items() if saved_view or v != DEFAULT_FILTERS[k]}
    if saved_view:
        params["saved_view"] = str(saved_view)
    if page > 1:
        params["page"] = str(page)
    return "/library" + ("?" + urlencode(params) if params else "")


def _fold(value) -> str:
    return unicodedata.normalize("NFC", str(value) if value is not None else "").casefold()


def _position_key(value) -> tuple[bool, Fraction]:
    try:
        text = str(value).strip()
        mixed = re.fullmatch(r"([+-]?\d+)\s+(\d+/\d+)", text)
        if mixed:
            whole, part = mixed.groups()
            number = Fraction(whole) + (-1 if whole.startswith("-") else 1) * Fraction(part)
        else:
            number = Fraction(text)
        return False, number
    except (ValueError, ZeroDivisionError):
        return True, Fraction(0)


def _item_order(item, filters):
    title = (_fold(item["title"]), _fold(item["authors"]), item["id"])
    # A local series drill-down always follows its numeric positions. Unknown
    # positions are last, including malformed legacy values and non-finite data.
    if filters["series"] or filters["sort"] == "series":
        return (item["series_key"], *_position_key(item["series_position"]), *title)
    if filters["sort"] == "author":
        return (_fold(item["authors"]), *title)
    if filters["sort"] == "recent":
        # SQLite's sortable timestamps are inverted without assuming that a
        # hand-imported timestamp can be parsed by datetime.
        return (tuple(-ord(c) for c in (item["created_at"] or "")) + (1,), -item["id"])
    return title


def _provider_on(provider: str) -> bool:
    feature = _PROVIDER_FEATURES.get(provider.strip().casefold())
    return not feature or features.feature_enabled(feature)


def _source_choices(db) -> list[dict]:
    choices = [{"value": "physical", "name": N_("Physical copies"), "enabled": True, "translate": True}]
    for key, name in (("abs", N_("Audiobookshelf")), ("komga", N_("Komga")), ("romm", N_("RomM"))):
        if _provider_on(key):
            choices.append({"value": key, "name": name, "enabled": True, "translate": True})
    for row in db.execute("SELECT id, name, provider, enabled FROM source_instances ORDER BY id"):
        if _provider_on(row["provider"]):
            choices.append({"value": f"source:{row['id']}", "name": row["name"], "id": row["id"],
                            "enabled": bool(row["enabled"])})
    return choices


def _snapshot_identifiers(payload) -> list[str]:
    """Index named identifier fields, never arbitrary provider JSON or secrets."""
    if not isinstance(payload, str) or len(payload) > 64_000:
        return []
    try:
        metadata = json.loads(payload)
    except (TypeError, ValueError, RecursionError):
        return []
    if not isinstance(metadata, dict):
        return []
    result = []
    dictionaries = [metadata]
    for field in ("provider_ids", "identifiers", "external_ids"):
        if isinstance(metadata.get(field), dict):
            dictionaries.append(metadata[field])
    for values in dictionaries:
        for key, value in values.items():
            if not isinstance(key, str):
                continue
            key = key.strip().casefold().replace("_", "").replace("-", "")
            if key in _IDENTIFIER_KEYS and type(value) in (str, int):
                value = _text(str(value), maximum=512)
                if value:
                    result.extend((value, f"{key}:{value}"))
    return result


def _external_ids(db, items) -> dict[int, list[str]]:
    result = {item_id: [] for item_id in items}
    # Fixed SQL, no table/column names or predicates supplied by a request.
    queries = (
        "SELECT r.item_id, r.external_id AS value FROM source_records r JOIN items_live i ON i.id = r.item_id",
        "SELECT r.item_id, r.komga_id AS value FROM komga_records r JOIN items_live i ON i.id = r.item_id",
        "SELECT r.item_id, r.romm_id AS value FROM romm_records r JOIN items_live i ON i.id = r.item_id",
        "SELECT r.item_id, r.value FROM music_identifiers r JOIN items_live i ON i.id = r.item_id",
        "SELECT r.item_id, r.musicbrainz_release_id AS value FROM music_releases r JOIN items_live i ON i.id = r.item_id",
        "SELECT r.item_id, r.musicbrainz_release_group_id AS value FROM music_releases r JOIN items_live i ON i.id = r.item_id",
    )
    for query in queries:
        for row in db.execute(query):
            if row["item_id"] in result and row["value"] is not None:
                result[row["item_id"]].append(str(row["value"]))
    for row in db.execute(
        "SELECT r.item_id, r.metadata_json FROM source_records r JOIN items_live i ON i.id = r.item_id"
    ):
        if row["item_id"] in result:
            result[row["item_id"]].extend(_snapshot_identifiers(row["metadata_json"]))
    return result


def _holdings_match(rows: list[dict], source: str, state: str) -> bool:
    rows = [row for row in rows if row["kind"] in {"physical", "digital"}
            and (not source or row["source_key"] == source)]
    if not state:
        return bool(rows) if source else True
    if state == "unknown" and not rows:
        return not source  # No holdings is unknown, never explicit unavailability.
    if state in {"digital", "physical"}:
        return any(row["kind"] == state and row["state"] == "available" for row in rows)
    return any(row["state"] == state for row in rows)


def _matches(item, filters, tokens, external, types) -> bool:
    if types and item["media_type"] not in types:
        return False
    if filters["media_type"] and item["media_type"] != filters["media_type"]:
        return False
    if filters["family"] and item["media_type"] not in FAMILIES[filters["family"]][1]:
        return False
    if filters["series"] and item["series_key"] != filters["series"]:
        return False
    ownership = filters["ownership"]
    if ownership == "owned" and not item["owned"]:
        return False
    if ownership in {"unowned", "neither"} and item["owned"]:
        return False
    if ownership == "neither" and item["wishlisted"]:
        return False
    if filters["wishlist"] and item["wishlisted"] != (filters["wishlist"] == "yes"):
        return False
    if not _holdings_match(item["holdings"], filters["source"], filters["availability"]):
        return False
    if tokens:
        haystack = "\n".join(_fold(item.get(key)) for key in _SEARCH_FIELDS)
        haystack += "\n" + "\n".join(_fold(value) for value in item["tags"] + external)
        if not all(token in haystack for token in tokens):
            return False
    return True


def search_catalog(db, filters: Mapping | None = None, *, page: int = 1) -> dict:
    """Return a complete page context, counting/grouping *before* pagination.

    Counts are matching items and distinct format groups, not all displayed
    members. ``groups[*].members`` always includes the full live component;
    ``matches`` flags and ``matching_ids`` identify the actual filter matches.
    Items mode includes only matching items. Series mode lists distinct local
    names, or drills into ordered format groups when ``series`` is selected.
    """
    filters = normalize_filters(filters)
    types = media_type_filters.parse_types(filters["types"])
    columns = ", ".join(f"i.{column}" for column in _ITEM_COLUMNS)
    items = {row["id"]: dict(row) for row in db.execute(
        f"SELECT {columns}, {WISHLISTED_SQL} AS wishlisted FROM items_live i"
    )}
    holdings = availability.availability_for_items(db, items)
    external = _external_ids(db, items)
    tags = {item_id: [] for item_id in items}
    for row in db.execute(
        "SELECT it.item_id, t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id "
        "JOIN items_live i ON i.id = it.item_id ORDER BY t.name COLLATE NOCASE, t.id"
    ):
        tags[row["item_id"]].append(row["name"])
    tokens = _fold(filters["q"]).split()
    series_choices = {}
    result_kind = ("series" if filters["view"] == "series" and not filters["series"]
                   else "items" if filters["view"] == "items" else "groups")
    for item in items.values():
        if isinstance(item["series_position"], float) and not math.isfinite(item["series_position"]):
            item["series_position"] = None
        item["owned"] = bool(item["owned"])
        item["wishlisted"] = bool(item["wishlisted"])
        item["series_key"] = format_groups.series_name_key(item["series_name"])
        item["holdings"] = holdings.get(item["id"], [])
        item["tags"] = tags[item["id"]]
        item["copy_count"] = sum(row["kind"] == "physical" for row in item["holdings"])
        item["matches"] = _matches(item, filters, tokens, external[item["id"]], types)
        if result_kind == "series" and not item["series_key"]:
            item["matches"] = False
        if item["series_key"]:
            series_choices.setdefault(item["series_key"], item["series_name"].strip())
    groups, series = [], {}
    for ids in format_groups.format_components(db):
        # A caller should use a read snapshot, but keep hydration safe even on
        # an externally supplied connection whose catalogue changed mid-read.
        members = [items[item_id] for item_id in ids if item_id in items]
        matching = [item for item in members if item["matches"]]
        if not matching:
            continue
        matching.sort(key=lambda item: _item_order(item, filters))
        members.sort(key=lambda item: (not item["matches"], _item_order(item, filters)))
        group = {
            "id": min(item["id"] for item in members),
            "member_ids": sorted(item["id"] for item in members),
            "matching_ids": [item["id"] for item in matching],
            "matching_count": len(matching), "members": members,
            "representative": matching[0],
        }
        groups.append(group)
        for item in matching:
            key = item["series_key"]
            if not key:
                continue
            entry = series.setdefault(key, {
                "key": key, "name": series_choices[key], "item_count": 0,
                "group_ids": set(), "representative": item,
            })
            entry["item_count"] += 1
            entry["group_ids"].add(group["id"])
            if _item_order(item, {**filters, "sort": "series"}) < _item_order(
                entry["representative"], {**filters, "sort": "series"}
            ):
                entry["representative"] = item
    groups.sort(key=lambda group: _item_order(group["representative"], filters))
    matching_items = sorted((item for item in items.values() if item["matches"]),
                            key=lambda item: _item_order(item, filters))
    series_rows = []
    for key in sorted(series):
        entry = series[key]
        entry["group_count"] = len(entry.pop("group_ids"))
        series_rows.append(entry)
    units = {"groups": groups, "items": matching_items, "series": series_rows}[result_kind]
    page_size = int(filters["page_size"])
    pages = max(1, (len(units) + page_size - 1) // page_size)
    page = min(positive_int(page), pages)
    start = (page - 1) * page_size
    return {
        "filters": filters, "result_kind": result_kind,
        "groups": groups[start:start + page_size] if result_kind == "groups" else [],
        "items": matching_items[start:start + page_size] if result_kind == "items" else [],
        "series_results": series_rows[start:start + page_size] if result_kind == "series" else [],
        "matching_count": len(matching_items), "group_count": len(groups),
        "total": len(units), "page": page, "pages": pages, "page_size": page_size,
        "series_choices": [{"key": key, "name": series_choices[key]} for key in sorted(series_choices)],
        "source_choices": _source_choices(db),
    }


def saved_views(db, user_id: int) -> list[dict]:
    """Only this account's names/IDs; no other user's saved query is exposed."""
    return [dict(row) for row in db.execute(
        "SELECT id, name FROM library_saved_views WHERE user_id = ? ORDER BY name COLLATE NOCASE, id",
        (user_id,),
    )]


def load_saved_view(db, user_id: int, view_id: int) -> dict | None:
    row = db.execute(
        "SELECT id, name, filters_json FROM library_saved_views WHERE id = ? AND user_id = ?",
        (view_id, user_id),
    ).fetchone()
    if row is None:
        return None
    try:
        values = json.loads(row["filters_json"])
    except (ValueError, TypeError, RecursionError):
        values = {}
    return {"id": row["id"], "name": row["name"], "filters": normalize_filters(values)}


def save_view(db, user_id: int, name: str, filters: Mapping) -> int:
    """Create/update an own view by case-insensitive name; caller holds write lock.

    A same-name save updates that account's view, even at its 50-view limit.
    ValueError codes are fixed strings, not database or request content.
    """
    name = _text(name, maximum=80)
    if not name:
        raise ValueError("invalid_name")
    existing = db.execute(
        "SELECT id FROM library_saved_views WHERE user_id = ? AND name = ? COLLATE NOCASE",
        (user_id, name),
    ).fetchone()
    payload = json.dumps(normalize_filters(filters), ensure_ascii=False, sort_keys=True)
    if existing:
        db.execute(
            "UPDATE library_saved_views SET name = ?, filters_json = ?, updated_at = datetime('now') "
            "WHERE id = ? AND user_id = ?", (name, payload, existing["id"], user_id),
        )
        return existing["id"]
    count = db.execute("SELECT COUNT(*) FROM library_saved_views WHERE user_id = ?", (user_id,)).fetchone()[0]
    if count >= MAX_SAVED_VIEWS:
        raise ValueError("view_limit")
    return db.execute(
        "INSERT INTO library_saved_views (user_id, name, filters_json) VALUES (?, ?, ?)",
        (user_id, name, payload),
    ).lastrowid


def delete_view(db, user_id: int, view_id: int) -> None:
    """An absent view and another account's view are identical no-ops."""
    db.execute("DELETE FROM library_saved_views WHERE id = ? AND user_id = ?", (view_id, user_id))
