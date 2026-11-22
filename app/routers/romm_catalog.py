"""Viewer-facing catalogue for games synchronised from RomM."""

from math import ceil
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse

from app.database import get_db, get_setting
from app.features import require_feature
from app.services import libraries, romm_catalog
from app.services.catalogue_security import require_catalogue_role

router = APIRouter()
PER_PAGE = 60


def _browser_root(server: str, public: str) -> str:
    """Configuration is not an authority to emit executable/credential URLs."""
    root = (public or server).strip().rstrip("/")
    try:
        parts = urlsplit(root)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or "\\" in root
                or any(ord(char) < 33 for char in root)):
            return ""
        # Accessing port validates malformed configured ports too.
        parts.port
    except ValueError:
        return ""
    return root


@router.get("/romm")
async def romm_index(
    _=Depends(require_catalogue_role("viewer")),
    __=Depends(require_feature("romm")),
):
    return RedirectResponse(url="/romm/library", status_code=303)


@router.get("/romm/library")
async def romm_library(
    request: Request,
    q: str = Query("", max_length=200),
    platform: str = Query("", max_length=200),
    page: int = Query(1, ge=1, le=2_147_483_647),
    _=Depends(require_catalogue_role("viewer")),
    __=Depends(require_feature("romm")),
):
    user = dict(request.state.user)
    offset = (page - 1) * PER_PAGE
    with get_db() as db:
        access_sql, access_params = libraries.item_access_condition(
            user, item_alias="i", minimum_role="viewer"
        )
        summaries = romm_catalog.platform_summaries(
            db,
            access_sql=access_sql,
            access_params=access_params,
        )
        games, total = romm_catalog.fetch_page(
            db,
            platform_id=platform,
            query=q,
            limit=PER_PAGE,
            offset=offset,
            access_sql=access_sql,
            access_params=access_params,
        )
        server = str(get_setting(db, "romm_url") or "").strip().rstrip("/")
        public = str(get_setting(db, "romm_public_url") or "").strip().rstrip("/")

    browser_root = _browser_root(server, public)
    for game in games:
        game["romm_url"] = None
        identity = str(game["romm_id"])
        # Browsers normalize even percent-encoded dot-only path segments.
        if browser_root and identity not in {".", ".."}:
            game["romm_url"] = f"{browser_root}/rom/{quote(identity, safe='')}"

    page_count = max(1, ceil(total / PER_PAGE)) if total else 1
    if page > page_count:
        query = {}
        if q:
            query["q"] = q
        if platform:
            query["platform"] = platform
        query["page"] = page_count
        return RedirectResponse(
            url=f"/romm/library?{urlencode(query)}",
            status_code=303,
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
            "can_manage_romm": user.get("role") == "admin" and not user.get("api_token_id"),
        },
    )
