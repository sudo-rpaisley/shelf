"""Bulk issue creation for physical comics and magazine publications."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import Depends, Form
from fastapi.responses import RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.routers.series import router
from app.services import bulk_issues


def _detail_url(name: str, **feedback) -> str:
    params = {"name": name, "media_type": "comic"}
    params.update({key: str(value) for key, value in feedback.items() if value is not None})
    return "/series/detail?" + urlencode(params)


def _publication_url(publication_id: int, **feedback) -> str:
    query = urlencode(
        {key: str(value) for key, value in feedback.items() if value is not None}
    )
    url = f"/magazines/publications/{publication_id}"
    return f"{url}?{query}" if query else url


@router.post("/api/series/bulk-add-issues")
async def bulk_add_comic_issues(
    name: str = Form(""),
    media_type: str = Form("comic"),
    from_issue: str = Form(""),
    to_issue: str = Form(""),
    _=Depends(require_role("editor")),
):
    """Add an inclusive physical-comic range, skipping existing positions."""
    clean_name = (name or "").strip()
    if (media_type or "").strip() != "comic":
        return RedirectResponse(
            _detail_url(
                clean_name,
                bulk_error="Series bulk issue ranges are only available for physical comics",
            ),
            status_code=303,
        )

    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = bulk_issues.add_comic_issue_range(
                db,
                series_name=clean_name,
                first_issue=from_issue,
                last_issue=to_issue,
            )
    except ValueError as exc:
        return RedirectResponse(
            _detail_url(clean_name, bulk_error=str(exc)),
            status_code=303,
        )

    return RedirectResponse(
        _detail_url(
            clean_name,
            bulk_added=result.created,
            bulk_skipped=result.skipped,
        ),
        status_code=303,
    )


@router.post("/api/magazines/publications/{publication_id}/bulk-add-issues")
async def bulk_add_magazine_issues(
    publication_id: int,
    from_issue: str = Form(""),
    to_issue: str = Form(""),
    volume: str = Form(""),
    _=Depends(require_role("editor")),
):
    """Add an inclusive issue range to one magazine publication and volume."""
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            result = bulk_issues.add_magazine_issue_range(
                db,
                publication_id=publication_id,
                first_issue=from_issue,
                last_issue=to_issue,
                volume=volume,
            )
    except ValueError as exc:
        return RedirectResponse(
            _publication_url(publication_id, bulk_error=str(exc)),
            status_code=303,
        )

    return RedirectResponse(
        _publication_url(
            publication_id,
            bulk_added=result.created,
            bulk_skipped=result.skipped,
        ),
        status_code=303,
    )
