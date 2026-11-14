"""Bounded, read-only individual-item recency queries.

Only the caller's current ``items_live`` relation is read. This service does
not authorize catalogue access, expand presentation selectors, open a connection
or manage a transaction. Callers needing a consistent count/page snapshot must
supply it; reads never acquire a writer or upgrade a read-only connection.

Empty ``media_types`` explicitly means mixed, including unknown legacy types.
Nonempty input must contain canonical config keys, not aliases or families.
No holdings, graph, source-availability or group-count claims are made here.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass

from app import config
from app.services import lists


MAX_MEDIA_TYPE_INPUTS = 64
MAX_MEDIA_TYPE_LENGTH = 64
MAX_PREVIEW_LIMIT = 50
DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 100
MAX_PAGE = 1_000_000


@dataclass(frozen=True, slots=True)
class RecentItem:
    id: int
    title: str
    creator_text: str | None
    media_type: str
    cover_path: str | None
    owned: bool
    wishlisted: bool


@dataclass(frozen=True, slots=True)
class ShelfPreview:
    items: tuple[RecentItem, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class PageDTO:
    items: tuple[RecentItem, ...]
    matching_count: int
    page: int
    pages: int
    page_size: int
    has_more: bool


def _bounded_int(value: int, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid_{name}")
    return value


def _type_predicate(media_types: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    # Bound the input before iteration/deduplication, including repeated keys.
    # This is a data API: HTTP/body parsing and selector expansion belong above
    # it, and must not turn an invalid nonempty selection into mixed mode.
    if type(media_types) is not tuple or len(media_types) > MAX_MEDIA_TYPE_INPUTS:
        raise ValueError("invalid_media_types")
    for media_type in media_types:
        if (type(media_type) is not str or len(media_type) > MAX_MEDIA_TYPE_LENGTH
                or media_type not in config.MEDIA_TYPES):
            raise ValueError("invalid_media_types")
    if not media_types:
        return "", ()
    selected = set(media_types)
    canonical = tuple(key for key in config.MEDIA_TYPES if key in selected)
    placeholders = ", ".join("?" for _ in canonical)
    return f" WHERE i.media_type IN ({placeholders})", canonical


def _selected_ids(db, predicate, parameters, *, limit, offset=None) -> tuple[int, ...]:
    # Native SQLite DESC matches Home: timestamps are not parsed or rewritten;
    # NULL sorts last if a caller's legacy relation permits it. The current
    # migrated schema has NOT NULL created_at. IDs break every timestamp tie.
    sql = (
        "SELECT i.id FROM items_live i" + predicate
        + " ORDER BY i.created_at DESC, i.id DESC LIMIT ?"
    )
    parameters = (*parameters, limit)
    if offset is not None:
        sql += " OFFSET ?"
        parameters = (*parameters, offset)
    with closing(db.execute(sql, parameters)) as cursor:
        return tuple(row["id"] for row in cursor.fetchall())


def _hydrate(db, item_ids: tuple[int, ...]) -> tuple[RecentItem, ...]:
    if not item_ids:
        return ()
    # Membership and card fields are evaluated only for selected IDs, never
    # inside the unbounded sort/count. Reuse the real wishlist definition;
    # owned=0 alone is the legal "neither" state, not a wishlist membership.
    placeholders = ", ".join("?" for _ in item_ids)
    sql = (
        "SELECT i.id, i.title, i.authors AS creator_text, i.media_type, "
        f"i.cover_path, i.owned, {lists.WISHLISTED_SQL} AS wishlisted "
        f"FROM items_live i WHERE i.id IN ({placeholders})"
    )
    with closing(db.execute(sql, item_ids)) as cursor:
        items = {
            row["id"]: RecentItem(
                id=row["id"], title=row["title"], creator_text=row["creator_text"],
                media_type=row["media_type"], cover_path=row["cover_path"],
                owned=bool(row["owned"]), wishlisted=bool(row["wishlisted"]),
            )
            for row in cursor.fetchall()
        }
    # Also recheck liveness through items_live during hydration. If a caller
    # omitted a snapshot and an item disappeared, do not revive it or refill
    # the page with an unbounded/per-item fallback query.
    return tuple(items[item_id] for item_id in item_ids if item_id in items)


def recent_preview(db, media_types: tuple[str, ...] = (), limit: int = 8) -> ShelfPreview:
    """Fetch at most limit+1 IDs, hydrate at most limit cards, report overflow.

    ``limit`` is an integer in 0..50 (never bool/coerced text). At zero, one
    ID is still fetched so ``has_more`` accurately describes matching data.
    Invalid input raises a fixed-code ValueError before any database access.
    """
    predicate, parameters = _type_predicate(media_types)
    limit = _bounded_int(limit, "limit", 0, MAX_PREVIEW_LIMIT)
    selected = _selected_ids(db, predicate, parameters, limit=limit + 1)
    return ShelfPreview(items=_hydrate(db, selected[:limit]), has_more=len(selected) > limit)


def recent_items_page(
    db, media_types: tuple[str, ...] = (), page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
) -> PageDTO:
    """Count matches once; LIMIT/OFFSET IDs before hydrating bounded cards.

    Page/page_size must be integers in 1..1,000,000 / 1..100. A valid page
    beyond the last match clamps to the last page, or page 1 for no matches.
    ``matching_count`` counts all matching live items, not returned rows or
    format groups; ``has_more`` follows that count in the caller's snapshot.
    """
    predicate, parameters = _type_predicate(media_types)
    page = _bounded_int(page, "page", 1, MAX_PAGE)
    page_size = _bounded_int(page_size, "page_size", 1, MAX_PAGE_SIZE)
    with closing(db.execute(
        "SELECT COUNT(*) AS matching_count FROM items_live i" + predicate, parameters,
    )) as cursor:
        matching_count = cursor.fetchone()["matching_count"]
    pages = max(1, (matching_count + page_size - 1) // page_size)
    page = min(page, pages)
    selected = _selected_ids(
        db, predicate, parameters, limit=page_size, offset=(page - 1) * page_size,
    ) if matching_count else ()
    return PageDTO(
        items=_hydrate(db, selected), matching_count=matching_count,
        page=page, pages=pages, page_size=page_size, has_more=page < pages,
    )
