"""Viewer-facing catalogue for games synchronised from RomM."""

from math import ceil

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.services import romm_catalog, romm_client, romm_sync

router = APIRouter()
PER_PAGE = 60


@router.get("/romm")
async def romm_index(_=Depends(require_role("viewer"))):
    return RedirectResponse(url="/romm/library", status_code=303)


@router.get("/romm/library")
async def romm_library(
    request: Request,
    q: str = Query("", max_length=200),
    platform: str = Query("", max_length=200),
    page: int = Query(1, ge=1),
    _=Depends(require_role("viewer")),
):
    offset = (page - 1) * PER_PAGE
    with get_db() as db:
        summaries = romm_catalog.platform_summaries(db)
        games, total = romm_catalog.fetch_page(
            db,
            platform_id=platform,
            query=q,
            limit=PER_PAGE,
            offset=offset,
        )

    config = romm_sync.configuration()
    server = str(config.get("url") or "").strip()
    public = str(config.get("public_url") or "").strip() or None
    for game in games:
        game["romm_url"] = None
        if server:
            try:
                game["romm_url"] = romm_client.browser_rom_url(
                    server, game["romm_id"], public_url=public
                )
            except romm_client.RomMError:
                pass

    page_count = max(1, ceil(total / PER_PAGE)) if total else 1
    if page > page_count and total:
        return RedirectResponse(
            url=f"/romm/library?page={page_count}", status_code=303
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "romm_library.html",
        {
            "games": games,
            "platforms": summaries,
            "total": total,
            "q": q,
            "selected_platform": platform,
            "page": page,
            "page_count": page_count,
        },
    )
