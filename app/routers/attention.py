"""Library-aware catalogue health workflow for Shelf 0.37.

The page is diagnostic only: it finds incomplete catalogue records and sends
users with edit rights to the existing item editor. Schema ownership remains
in :mod:`app.database`; this router never creates or migrates tables.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.auth import require_role
from app.config import BOOK_MEDIA_TYPES, PERIODICAL_MEDIA_TYPES
from app.database import get_db
from app.services import libraries

router = APIRouter()

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
        "description": "Readable and periodical items without a creator credit.",
    },
    "periodical_issue": {
        "label": "Periodical issue details",
        "description": "Periodical records that still need an issue number or issue date.",
    },
}

_CREATOR_TYPES = tuple(sorted(set(BOOK_MEDIA_TYPES) | set(PERIODICAL_MEDIA_TYPES)))
_PERIODICAL_TYPES = tuple(sorted(PERIODICAL_MEDIA_TYPES))


def _in_clause(values: tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


def _category_where(category: str) -> tuple[str, list]:
    if category == "cover":
        return "i.owned = 1 AND (i.cover_path IS NULL OR TRIM(i.cover_path) = '')", []

    if category == "location":
        # Do not infer physicality from media_type. item_copies represents an
        # actual physical object; a copy row without a location is therefore
        # the conservative, future-proof signal that placement is missing.
        return (
            "i.owned = 1 AND EXISTS ("
            "SELECT 1 FROM item_copies c "
            "WHERE c.item_id = i.id AND c.location_id IS NULL"
            ")",
            [],
        )

    if category == "creator":
        return (
            f"i.owned = 1 AND i.media_type IN ({_in_clause(_CREATOR_TYPES)}) "
            "AND (i.authors IS NULL OR TRIM(i.authors) = '')",
            list(_CREATOR_TYPES),
        )

    if category == "periodical_issue":
        return (
            f"i.owned = 1 AND i.media_type IN ({_in_clause(_PERIODICAL_TYPES)}) AND ("
            "pi.item_id IS NULL OR ("
            "(pi.issue_number IS NULL OR TRIM(pi.issue_number) = '') AND "
            "(pi.issue_date IS NULL OR TRIM(pi.issue_date) = '')"
            ")"
            ")",
            list(_PERIODICAL_TYPES),
        )

    return "0", []


def _scoped_where(category: str, user: dict) -> tuple[str, list]:
    where, params = _category_where(category)
    access, access_params = libraries.item_access_condition(user, item_alias="i")
    return f"({where}) AND ({access})", [*params, *access_params]


def attention_counts(db, user: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in _CATEGORIES:
        where, params = _scoped_where(key, user)
        row = db.execute(
            "SELECT COUNT(DISTINCT i.id) AS c FROM items i "
            "LEFT JOIN periodical_issues pi ON pi.item_id = i.id "
            f"WHERE {where}",
            params,
        ).fetchone()
        counts[key] = int(row["c"])
    return counts


def _actor(request: Request) -> dict:
    return dict(request.state.user)


@router.get("/attention")
async def attention_page(
    request: Request,
    category: str = Query("cover"),
    _=Depends(require_role("viewer")),
):
    if category not in _CATEGORIES:
        category = "cover"

    user = _actor(request)
    where, params = _scoped_where(category, user)
    with get_db() as db:
        counts = attention_counts(db, user)
        rows = db.execute(
            "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, "
            "i.publish_year, pi.issue_number, pi.issue_date "
            "FROM items i "
            "LEFT JOIN periodical_issues pi ON pi.item_id = i.id "
            f"WHERE {where} "
            "ORDER BY i.title COLLATE NOCASE, i.id LIMIT 200",
            params,
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["can_edit"] = libraries.has_item_role(
                db, user, int(item["id"]), "editor"
            )
            items.append(item)

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
