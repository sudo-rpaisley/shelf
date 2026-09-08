"""Assisted periodical identification layered on Shelf's Periodicals feature."""

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.auth import require_role
from app.config import HTTP_TIMEOUT
from app.database import get_db, get_setting
from app.services import periodical_google, periodicals

router = APIRouter()


def _scan_context(raw_barcode: str):
    serial = periodicals.parse_barcode(raw_barcode)
    if serial is None:
        raise ValueError("A valid periodical barcode is required")
    return serial


@router.get("/api/periodicals/assist/search")
async def assisted_periodical_search(
    request: Request,
    q: str = Query("", max_length=120),
    raw_barcode: str = Query(..., max_length=32),
    location_id: int | None = Query(None),
    mode: str = Query("add"),
    _=Depends(require_role("editor")),
):
    try:
        _scan_context(raw_barcode)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=400)

    with get_db() as db:
        api_key = get_setting(db, "google_books_api_key") or None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await periodical_google.search_issues(q, client, api_key=api_key)
    candidates = result.payload if result.found and isinstance(result.payload, list) else []
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/periodical_assisted_results.html",
        {
            "candidates": candidates,
            "raw_barcode": raw_barcode,
            "location_id": location_id,
            "mode": mode if mode in {"add", "wishlist"} else "add",
            "query": q.strip(),
            "search_outcome": result.outcome,
        },
    )


@router.get("/api/periodicals/assist/select")
async def assisted_periodical_select(
    request: Request,
    volume_id: str = Query(..., max_length=80),
    raw_barcode: str = Query(..., max_length=32),
    location_id: int | None = Query(None),
    mode: str = Query("add"),
    _=Depends(require_role("editor")),
):
    try:
        serial = _scan_context(raw_barcode)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=400)

    with get_db() as db:
        api_key = get_setting(db, "google_books_api_key") or None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await periodical_google.lookup_issue(volume_id, client, api_key=api_key)
    if not result.found or not isinstance(result.payload, dict):
        return HTMLResponse("That magazine result is no longer available", status_code=404)

    issue = result.payload
    confirmed_issn = serial.issn
    try:
        confirmed_issn = periodicals.normalise_issn(issue.get("issn")) or serial.issn
    except ValueError:
        # Provider metadata must never make a valid scan unconfirmable. Keep
        # the checksum-valid ISSN derived from the printed 977 carrier instead.
        pass

    candidate = {
        "publication_title": issue.get("title"),
        "publisher": issue.get("publisher"),
        "language": issue.get("language"),
        "issn": confirmed_issn,
        "barcode_ean": serial.ean13,
        "barcode_supplement": serial.supplement,
        "issue_date": issue.get("issue_date"),
        "cover_url": issue.get("cover_url"),
    }
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/periodical_scan_result.html",
        {
            "candidate": candidate,
            "raw_barcode": serial.full_code,
            "location_id": location_id,
            "mode": mode if mode in {"add", "wishlist"} else "add",
            "notice": "Google Books result selected. Confirm the concrete issue details before adding it.",
            "csrf_token": request.cookies.get("csrf_token", ""),
        },
    )
