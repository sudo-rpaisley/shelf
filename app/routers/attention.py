"""Catalogue data-quality workflow attached to the existing pages router.

The page is intentionally diagnostic. It centralises common incomplete-record
states and sends editors to the existing item editor rather than inventing a
second mutation path. Holding-aware checks use the additive copy/periodical
model so digital media is never reported as missing a shelf and magazines are
judged by issue metadata rather than a generic publication year.
"""

from fastapi import Depends, Query, Request

from app.auth import require_role
from app.database import get_db
from app.routers import pages
from app.services import holdings


_CATEGORIES = {
    "cover": {
        "label": "Missing cover",
        "description": "Owned items without cover artwork.",
    },
    "location": {
        "label": "No physical location",
        "description": "Owned physical copies that have not been placed anywhere.",
    },
    "creator": {
        "label": "Missing creator",
        "description": "Books, comics and magazines without an author or creator credit.",
    },
    "magazine_issue": {
        "label": "Magazine issue details",
        "description": "Magazine records that still need an issue number or issue date.",
    },
}


def _category_where(category: str) -> tuple[str, list]:
    if category == "cover":
        return "i.owned = 1 AND (i.cover_path IS NULL OR TRIM(i.cover_path) = '')", []
    if category == "location":
        # item_copies is physical-only by construction. Querying it instead of
        # a duplicated media-type list means new digital formats cannot be
        # accidentally told to choose a room/shelf.
        return (
            "i.owned = 1 AND EXISTS ("
            "SELECT 1 FROM item_copies c "
            "WHERE c.item_id = i.id AND c.location_id IS NULL"
            ")",
            [],
        )
    if category == "creator":
        return (
            "i.owned = 1 AND i.media_type IN ('book','kids_book','comic','magazine') "
            "AND (i.authors IS NULL OR TRIM(i.authors) = '')",
            [],
        )
    if category == "magazine_issue":
        return (
            "i.owned = 1 AND i.media_type = 'magazine' AND ("
            "pi.item_id IS NULL OR ("
            "(pi.issue_number IS NULL OR TRIM(pi.issue_number) = '') AND "
            "(pi.issue_date IS NULL OR TRIM(pi.issue_date) = '')"
            ")"
            ")",
            [],
        )
    return "0", []


def attention_counts(db) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in _CATEGORIES:
        where, params = _category_where(key)
        counts[key] = db.execute(
            "SELECT COUNT(DISTINCT i.id) AS c FROM items i "
            "LEFT JOIN periodical_issues pi ON pi.item_id = i.id "
            f"WHERE {where}",
            params,
        ).fetchone()["c"]
    return counts


@pages.router.get("/attention")
async def attention_page(
    request: Request,
    category: str = Query("cover"),
    _=Depends(require_role("viewer")),
):
    if category not in _CATEGORIES:
        category = "cover"

    where, params = _category_where(category)
    with get_db() as db:
        # Installs the additive tables and performs the one-time legacy copy /
        # provider backfill before any quality rule reasons about holdings.
        holdings.ensure_foundation(db)
        counts = attention_counts(db)
        items = db.execute(
            "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, "
            "i.publish_year, i.location_id, l.name AS location_name, "
            "pi.issue_number, pi.issue_date "
            "FROM items i "
            "LEFT JOIN locations l ON l.id = i.location_id "
            "LEFT JOIN periodical_issues pi ON pi.item_id = i.id "
            f"WHERE {where} "
            "ORDER BY i.title COLLATE NOCASE, i.id LIMIT 200",
            params,
        ).fetchall()

    categories = [
        {"key": key, **definition, "count": counts[key]}
        for key, definition in _CATEGORIES.items()
    ]
    return request.app.state.templates.TemplateResponse(
        request,
        "attention.html",
        {
            "categories": categories,
            "active_category": category,
            "active_definition": _CATEGORIES[category],
            "attention_items": items,
            "result_truncated": len(items) == 200,
        },
    )
