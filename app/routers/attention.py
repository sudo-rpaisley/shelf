"""Catalogue data-quality workflow.

This is intentionally read-only: it tells a user what needs work and sends
them to the existing item editor rather than inventing a second mutation path.
"""

from fastapi import APIRouter, Depends, Query, Request

from app.auth import require_role
from app.database import get_db

router = APIRouter()

_PHYSICAL_MEDIA_TYPES = (
    "book", "kids_book", "magazine", "dvd", "vinyl", "cassette", "cd",
    "music_other", "comic", "video_game",
)

_CATEGORIES = {
    "cover": {
        "label": "Missing cover",
        "description": "Owned items without cover artwork.",
    },
    "location": {
        "label": "No physical location",
        "description": "Owned physical media that has not been placed anywhere.",
    },
    "creator": {
        "label": "Missing creator",
        "description": "Books, comics and magazines without an author or creator credit.",
    },
    "magazine_issue": {
        "label": "Magazine issue details",
        "description": "Magazine records without a publication year or issue-level date yet.",
    },
}


def _category_where(category: str) -> tuple[str, list]:
    if category == "cover":
        return "i.owned = 1 AND (i.cover_path IS NULL OR TRIM(i.cover_path) = '')", []
    if category == "location":
        placeholders = ",".join("?" for _ in _PHYSICAL_MEDIA_TYPES)
        return (
            f"i.owned = 1 AND i.media_type IN ({placeholders}) AND i.location_id IS NULL",
            list(_PHYSICAL_MEDIA_TYPES),
        )
    if category == "creator":
        return (
            "i.owned = 1 AND i.media_type IN ('book','kids_book','comic','magazine') "
            "AND (i.authors IS NULL OR TRIM(i.authors) = '')",
            [],
        )
    if category == "magazine_issue":
        return "i.owned = 1 AND i.media_type = 'magazine' AND i.publish_year IS NULL", []
    return "0", []


def attention_counts(db) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in _CATEGORIES:
        where, params = _category_where(key)
        counts[key] = db.execute(
            f"SELECT COUNT(*) AS c FROM items i WHERE {where}", params
        ).fetchone()["c"]
    return counts


@router.get("/attention")
async def attention_page(
    request: Request,
    category: str = Query("cover"),
    _=Depends(require_role("viewer")),
):
    if category not in _CATEGORIES:
        category = "cover"

    where, params = _category_where(category)
    with get_db() as db:
        counts = attention_counts(db)
        items = db.execute(
            "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, "
            "i.publish_year, i.location_id, l.name AS location_name "
            "FROM items i LEFT JOIN locations l ON l.id = i.location_id "
            f"WHERE {where} ORDER BY i.title COLLATE NOCASE, i.id LIMIT 200",
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
