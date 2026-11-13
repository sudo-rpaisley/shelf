"""Local, read-only projections for the stable v1 API.

All catalogue reads (including counts, memberships, loans and release watches)
are rooted in items_live. The caller owns a read transaction so counts and pages
share one snapshot. SQL handles filtering/order/LIMIT before row hydration.
Only allowlisted SQL identifiers are interpolated; all values are bound.
"""

import math
import re
import sqlite3
from urllib.parse import quote

from app.api_v1_schemas import (
    MAX_PAGE, ItemDetail, ItemDetailResponse, ItemPage, ItemQuery, ItemSeries, ItemSummary,
    ItemValue, ListPage, ListQuery, ListSummary, Loan, LoanPage, LoanQuery, PagingQuery,
    SeriesDetailResponse, SeriesItemsQuery, SeriesPage, SeriesQuery, SeriesSummary,
    SourcePage, UpcomingPage, UpcomingQuery, UpcomingRelease,
)
from app.currency import get_currency
from app.database import get_setting
from app.features import feature_enabled
from app.routers.checkouts import OVERDUE_CONDITION, get_overdue_days
from app.services import availability, audiobookshelf, komga_libraries, lists, romm_client


_ITEM_COLUMNS = (
    "id", "title", "subtitle", "authors", "media_type", "isbn", "isbn10", "upc", "asin",
    "cover_path", "owned", "series_name", "series_position", "created_at", "updated_at",
)
_DETAIL_COLUMNS = (
    "publisher", "publish_year", "language", "description", "page_count", "narrator",
    "duration_mins", "platform", "manual_value", "estimated_value",
)
_ITEM_SELECT = ", ".join(f"i.{key}" for key in _ITEM_COLUMNS)
_ITEM_SELECT += f", {lists.WISHLISTED_SQL} AS wishlisted"
_ITEM_SORTS = {
    "title": "i.title COLLATE NOCASE", "authors": "i.authors COLLATE NOCASE",
    "created_at": "i.created_at", "id": "i.id", "position": "i.series_position",
}
_PROVIDER_FEATURES = (
    ("abs_sync", ("abs", "abs_sync", "audiobookshelf")),
    ("komga", ("komga",)), ("romm", ("romm",)),
)


def _page(model, rows, total, query):
    pages = (total + query.page_size - 1) // query.page_size
    return model(data=rows, total=total, page=query.page, page_size=query.page_size,
                 pages=pages, next_page=query.page + 1 if query.page < min(pages, MAX_PAGE) else None)


def _bounds(query):
    return query.page_size, (query.page - 1) * query.page_size


def _order(column, query, tie):
    # NULLs last in both directions; a unique tie-breaker is never omitted.
    direction = {"asc": "ASC", "desc": "DESC"}[query.order]
    return f"{column} IS NULL, {column} {direction}, {tie}"


def _like(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _cover(path):
    # Stored cover paths are filenames. Never echo a filesystem path, a remote
    # URL or query-bearing imported value as a same-origin browser URL.
    if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp|gif)", path, re.I):
        return None
    return "/covers/" + quote(path, safe="")


def _item(row, *, detail=False):
    fields = {key: row[key] for key in _ITEM_COLUMNS
              if key not in {"cover_path", "series_name", "series_position"}}
    fields.update(owned=bool(row["owned"]), wishlisted=bool(row["wishlisted"]),
                  cover_url=_cover(row["cover_path"]), series=None)
    if feature_enabled("series") and row["series_name"] and row["series_name"].strip():
        fields["series"] = ItemSeries(name=row["series_name"], position=_finite(row["series_position"]))
    if detail:
        fields.update({key: row[key] for key in _DETAIL_COLUMNS
                       if key not in {"manual_value", "estimated_value"}})
    return (ItemDetail if detail else ItemSummary)(**fields)


def item_page(db: sqlite3.Connection, query: ItemQuery | SeriesItemsQuery, *,
              list_slug: str | None = None, series_name: str | None = None) -> ItemPage:
    conditions, params = [], []
    if getattr(query, "q", ""):
        conditions.append("(" + " OR ".join(
            f"i.{column} LIKE ? ESCAPE '\\'" for column in ("title", "authors", "isbn", "isbn10", "upc", "asin")
        ) + ")")
        params.extend([_like(query.q)] * 6)
    for key in ("media_type", "owned"):
        value = getattr(query, key, None)
        if value is not None:
            conditions.append(f"i.{key} = ?")
            params.append(value)
    if getattr(query, "wishlisted", None) is not None:
        conditions.append(f"{lists.WISHLISTED_SQL} = ?")
        params.append(query.wishlisted)
    if list_slug is not None:
        conditions.append("EXISTS (SELECT 1 FROM list_items li JOIN lists l ON l.id = li.list_id "
                          "WHERE li.item_id = i.id AND l.slug = ?)")
        params.append(list_slug)
    if series_name is not None:
        conditions.append("i.series_name = ? COLLATE NOCASE")
        params.append(series_name)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    total = db.execute("SELECT COUNT(*) FROM items_live i" + where, params).fetchone()[0]
    order = _order(_ITEM_SORTS[query.sort], query, "i.id ASC")
    rows = db.execute(f"SELECT {_ITEM_SELECT} FROM items_live i{where} ORDER BY {order} LIMIT ? OFFSET ?",
                      [*params, *_bounds(query)]).fetchall()
    return _page(ItemPage, [_item(row) for row in rows], total, query)


def item_detail(db: sqlite3.Connection, item_id: int, query: PagingQuery) -> ItemDetailResponse | None:
    columns = _ITEM_SELECT + ", " + ", ".join(f"i.{key}" for key in _DETAIL_COLUMNS)
    row = db.execute(f"SELECT {columns} FROM items_live i WHERE i.id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    value = None
    if feature_enabled("valuation"):
        amount = _finite(row["manual_value"] if row["manual_value"] is not None else row["estimated_value"])
        value = ItemValue(amount=round(amount, 2) if amount is not None else None, currency=get_currency().code)
    return ItemDetailResponse(item=_item(row, detail=True), value=value,
                              sources=source_page(db, item_id, query) if feature_enabled("connected_library") else None)


def source_page(db: sqlite3.Connection, item_id: int, query: PagingQuery) -> SourcePage:
    """Page new and legacy digital holdings without reading credentials/JSON.

    Roots are used only to construct validated browser links, never returned.
    Failed/disabled sources retain cached holdings but cannot assert availability.
    """
    enabled = {key: feature_enabled(key) for key in ("abs_sync", "komga", "romm")}
    provider_gates = " AND ".join(
        "lower(trim(s.provider)) NOT IN (" + ",".join("?" for _ in names) + ")"
        for feature, names in _PROVIDER_FEATURES
        if not enabled[feature]
    )
    disabled_names = [name for feature, names in _PROVIDER_FEATURES
                      if not enabled[feature] for name in names]
    sql = (
        "SELECT 'source:' || s.id AS source_key, s.name, r.id AS record_id, r.open_url, "
        "COALESCE(NULLIF(s.browser_url, ''), s.base_url) AS root, s.enabled, s.last_status, "
        "(s.last_error IS NOT NULL AND s.last_error != '') AS failed, r.availability, "
        "r.last_seen_at, NULL AS external_id "
        "FROM source_records r JOIN source_instances s ON s.id = r.source_id "
        "JOIN items_live i ON i.id = r.item_id WHERE i.id = ?"
        + (" AND " + provider_gates if provider_gates else "")
    )
    params = [item_id, *disabled_names]
    legacy = (
        ("abs_sync", "abs", "Audiobookshelf", "i.abs_id", "items_live i", "i.id", "NULL", "i.abs_id != ''"),
        ("komga", "komga", "Komga", "r.komga_id", "komga_records r JOIN items_live i ON i.id = r.item_id", "r.item_id", "r.updated_at", "1"),
        ("romm", "romm", "RomM", "r.romm_id", "romm_records r JOIN items_live i ON i.id = r.item_id", "r.item_id", "r.updated_at", "1"),
    )
    for feature, key, name, external_id, table, id_column, last_seen, condition in legacy:
        if enabled[feature]:
            sql += (f" UNION ALL SELECT '{key}', '{name}', {external_id}, NULL, NULL, 1, NULL, 0, "
                    f"'available', {last_seen}, {external_id} FROM {table} WHERE {id_column} = ? "
                    f"AND {external_id} IS NOT NULL AND {condition}")
            params.append(item_id)
    cte = "WITH holdings AS (" + sql + ") "
    total = db.execute(cte + "SELECT COUNT(*) FROM holdings", params).fetchone()[0]
    rows = db.execute(cte + "SELECT * FROM holdings ORDER BY name COLLATE NOCASE, source_key, record_id "
                      "LIMIT ? OFFSET ?", [*params, *_bounds(query)]).fetchall()
    builders = {"abs": ("abs", audiobookshelf.get_playback_url),
                "komga": ("komga", komga_libraries.browser_book_url),
                "romm": ("romm", romm_client.browser_rom_url)}
    result = []
    for row in rows:
        state = row["availability"] if row["availability"] in {"available", "unavailable", "unknown"} else "unknown"
        failed = row["failed"] or row["last_status"] in {"error", "partial", "syncing"}
        url = availability._safe_url(row["open_url"], base=row["root"]) if row["enabled"] else None
        if row["source_key"] in builders:
            prefix, builder = builders[row["source_key"]]
            url = availability.legacy_url(get_setting(db, prefix + "_url"),
                                          get_setting(db, prefix + "_public_url"), row["external_id"], builder)
            failed = get_setting(db, f"connection.{prefix}.last_status") in {"error", "partial"} or not url
        if (not row["enabled"] or failed) and state != "unavailable":
            state = "unknown"
        result.append(dict(key=row["source_key"], name=row["name"], state=state,
                           url=url, last_seen_at=row["last_seen_at"]))
    return _page(SourcePage, result, total, query)


def _loans(db, query=None, *, loan_id=None):
    conditions, params = [], [get_overdue_days(db)]
    if loan_id is not None:
        conditions.append("c.id = ?")
        params.append(loan_id)
    if query:
        if query.status == "open":
            conditions.append("c.checked_in IS NULL")
        elif query.status == "returned":
            conditions.append("c.checked_in IS NOT NULL")
        elif query.status == "overdue":
            conditions.append("overdue = 1")
        for key in ("item_id", "borrower_id"):
            if getattr(query, key) is not None:
                conditions.append(f"c.{key} = ?")
                params.append(getattr(query, key))
    cte = ("WITH visible_loans AS (SELECT c.id, c.item_id, i.title AS item_title, c.borrower_id, "
           "b.name AS borrower_name, c.checked_out, c.due_date, c.checked_in, "
           f"({OVERDUE_CONDITION}) AS overdue FROM checkouts c "
           "JOIN items_live i ON i.id = c.item_id JOIN borrowers b ON b.id = c.borrower_id) ")
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    return cte, " FROM visible_loans c" + where, params


def _loan(row):
    return Loan(id=row["id"], item_id=row["item_id"], item_title=row["item_title"],
                borrower={"id": row["borrower_id"], "name": row["borrower_name"]},
                checked_out=row["checked_out"], due_date=row["due_date"], checked_in=row["checked_in"],
                overdue=bool(row["overdue"]))


def loan_page(db: sqlite3.Connection, query: LoanQuery) -> LoanPage:
    cte, relation, params = _loans(db, query)
    total = db.execute(cte + "SELECT COUNT(*)" + relation, params).fetchone()[0]
    column = {"checked_out": "c.checked_out", "due_date": "c.due_date",
              "title": "c.item_title COLLATE NOCASE", "id": "c.id"}[query.sort]
    rows = db.execute(cte + "SELECT *" + relation + " ORDER BY " + _order(column, query, "c.id ASC")
                      + " LIMIT ? OFFSET ?", [*params, *_bounds(query)]).fetchall()
    return _page(LoanPage, [_loan(row) for row in rows], total, query)


def loan_detail(db: sqlite3.Connection, loan_id: int) -> Loan | None:
    cte, relation, params = _loans(db, loan_id=loan_id)
    row = db.execute(cte + "SELECT *" + relation, params).fetchone()
    return _loan(row) if row else None


_VISIBLE_LISTS = (
    "WITH visible_lists AS (SELECT l.slug, l.name, l.created_at, COUNT(i.id) AS item_count "
    "FROM lists l LEFT JOIN list_items li ON li.list_id = l.id "
    "LEFT JOIN items_live i ON i.id = li.item_id GROUP BY l.id "
    "HAVING l.slug = 'wishlist' OR COUNT(i.id) > 0) "
)


def list_page(db: sqlite3.Connection, query: ListQuery) -> ListPage:
    where, params = (" WHERE name LIKE ? ESCAPE '\\'", [_like(query.q)]) if query.q else ("", [])
    total = db.execute(_VISIBLE_LISTS + "SELECT COUNT(*) FROM visible_lists" + where, params).fetchone()[0]
    column = {"name": "name COLLATE NOCASE", "created_at": "created_at", "item_count": "item_count"}[query.sort]
    rows = db.execute(_VISIBLE_LISTS + "SELECT * FROM visible_lists" + where + " ORDER BY "
                      + _order(column, query, "slug COLLATE BINARY ASC") + " LIMIT ? OFFSET ?",
                      [*params, *_bounds(query)]).fetchall()
    return _page(ListPage, [dict(row) for row in rows], total, query)


def list_detail(db: sqlite3.Connection, slug: str) -> ListSummary | None:
    row = db.execute(_VISIBLE_LISTS + "SELECT * FROM visible_lists WHERE slug = ?", (slug,)).fetchone()
    return ListSummary(**dict(row)) if row else None


_SERIES = (
    "WITH visible_series AS (SELECT MIN(i.series_name COLLATE BINARY) AS name, "
    "COUNT(*) AS item_count, SUM(i.owned = 1) AS owned_count, "
    f"SUM({lists.WISHLISTED_SQL}) AS wishlist_count FROM items_live i "
    "WHERE i.series_name IS NOT NULL AND TRIM(i.series_name) != '' GROUP BY i.series_name COLLATE NOCASE) "
)
_SERIES_SELECT = "SELECT s.*, m.complete FROM visible_series s LEFT JOIN series_meta m ON m.name = s.name COLLATE NOCASE"


def series_page(db: sqlite3.Connection, query: SeriesQuery) -> SeriesPage:
    where, params = (" WHERE s.name LIKE ? ESCAPE '\\'", [_like(query.q)]) if query.q else ("", [])
    total = db.execute(_SERIES + "SELECT COUNT(*) FROM visible_series s" + where, params).fetchone()[0]
    column = {"name": "s.name COLLATE NOCASE", "item_count": "s.item_count",
              "owned_count": "s.owned_count", "wishlist_count": "s.wishlist_count"}[query.sort]
    rows = db.execute(_SERIES + _SERIES_SELECT + where + " ORDER BY "
                      + _order(column, query, "s.name COLLATE BINARY ASC") + " LIMIT ? OFFSET ?",
                      [*params, *_bounds(query)]).fetchall()
    return _page(SeriesPage, [dict(row) for row in rows], total, query)


def series_detail(db: sqlite3.Connection, name: str, query: SeriesItemsQuery) -> SeriesDetailResponse | None:
    row = db.execute(_SERIES + "SELECT s.*, m.complete, m.description FROM visible_series s "
                     "LEFT JOIN series_meta m ON m.name = s.name COLLATE NOCASE WHERE s.name = ? COLLATE NOCASE",
                     (name,)).fetchone()
    if row is None:
        return None
    return SeriesDetailResponse(series=SeriesSummary(**dict(row)), description=row["description"],
                                items=item_page(db, query, series_name=row["name"]))


def upcoming_page(db: sqlite3.Connection, query: UpcomingQuery) -> UpcomingPage:
    # Match release_radar's casefolded/trimmed title key without hydrating the
    # full catalogue. Orphan/hidden watches must not expose another library.
    db.create_function("api_title_key", 1, lambda value: (value or "").casefold().strip(), deterministic=True)
    held = f"(i.owned = 1 OR {lists.WISHLISTED_SQL})"
    relation = (
        " FROM upcoming_releases u JOIN release_watches w ON w.id = u.watch_id "
        "WHERE u.dismissed = 0 AND EXISTS (SELECT 1 FROM items_live i "
        "WHERE i.series_name = w.series_name COLLATE NOCASE) "
        f"AND NOT EXISTS (SELECT 1 FROM items_live i WHERE {held} AND "
        "(i.hardcover_book_id = u.hc_book_id OR (i.series_name = w.series_name COLLATE NOCASE "
        "AND api_title_key(i.title) = api_title_key(u.title))))"
    )
    params = []
    if query.series_name is not None:
        relation += " AND w.series_name = ? COLLATE NOCASE"
        params.append(query.series_name)
    if query.dated is not None:
        relation += " AND u.release_date IS " + ("NOT NULL" if query.dated else "NULL")
    for key, operator in (("release_from", ">="), ("release_to", "<=")):
        if getattr(query, key) is not None:
            relation += f" AND u.release_date {operator} ?"
            params.append(getattr(query, key).isoformat())
    total = db.execute("SELECT COUNT(*)" + relation, params).fetchone()[0]
    column = {"release_date": "u.release_date", "title": "u.title COLLATE NOCASE",
              "series_name": "w.series_name COLLATE NOCASE"}[query.sort]
    rows = db.execute("SELECT u.id, u.hc_book_id, u.title, u.authors, w.series_name, u.series_position, "
                      "u.release_date, u.cover_url" + relation + " ORDER BY "
                      + _order(column, query, "w.series_name COLLATE NOCASE, u.series_position IS NULL, u.series_position, u.id ASC")
                      + " LIMIT ? OFFSET ?", [*params, *_bounds(query)]).fetchall()
    result = [UpcomingRelease(id=row["id"], hardcover_book_id=row["hc_book_id"], title=row["title"],
                              authors=row["authors"], series_name=row["series_name"],
                              series_position=_finite(row["series_position"]), release_date=row["release_date"],
                              cover_url=availability._safe_url(row["cover_url"])) for row in rows]
    return _page(UpcomingPage, result, total, query)
