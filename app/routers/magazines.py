"""Magazine issue search and add workflows."""

from datetime import date
import re

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.auth import require_role
from app.config import HTTP_TIMEOUT, MEDIA_TYPES
from app.database import get_db, get_setting
from app.routers import items_common
from app.services import covers, magazine_google, periodical_records, periodicals, scan_outcome
from app.services.item_write import insert_item
from app.services.write_targets import UnknownLocationError, validated_location_id

router = APIRouter(prefix="/api")
_PARTIAL_DATE_RE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")


def _normalise_issue_date(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if not _PARTIAL_DATE_RE.fullmatch(raw):
        raise ValueError("Issue date must be YYYY, YYYY-MM or YYYY-MM-DD")
    if len(raw) == 10:
        try:
            date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("Issue date is not a valid date") from exc
    if len(raw) >= 7 and not 1 <= int(raw[5:7]) <= 12:
        raise ValueError("Issue month must be between 01 and 12")
    return raw


def _issue_label(issue_number: str | None, issue_date: str | None) -> str:
    if issue_number and issue_date:
        return f"Issue {issue_number} · {issue_date}"
    if issue_number:
        return f"Issue {issue_number}"
    if issue_date:
        return issue_date
    return "Issue"


@router.get("/magazines/search")
async def search_magazines(
    request: Request,
    q: str = "",
    _=Depends(require_role("editor")),
):
    templates = request.app.state.templates
    if not q.strip():
        return HTMLResponse("")
    with get_db() as db:
        api_key = get_setting(db, "google_books_api_key") or None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await magazine_google.search_issues(
            q.strip(), client, api_key=api_key, limit=10
        )
    return templates.TemplateResponse(
        request,
        "fragments/magazine_search_results.html",
        {
            "results": result.payload or [],
            "query": q.strip(),
            "search_status": scan_outcome.not_found_status(result),
            "search_provider": scan_outcome.provider_label(result),
        },
    )


@router.post("/magazines/add")
async def add_magazine_issue(
    request: Request,
    google_volume_id: str = Form(""),
    title: str = Form(""),
    issn: str = Form(""),
    publisher: str = Form(""),
    language: str = Form(""),
    description: str = Form(""),
    volume: str = Form(""),
    issue_number: str = Form(""),
    issue_date: str = Form(""),
    cover_date_label: str = Form(""),
    carrier_ean: str = Form(""),
    barcode_supplement: str = Form(""),
    location_id: int | None = Form(None),
    mode: str = Form("add"),
    _=Depends(require_role("editor")),
):
    templates = request.app.state.templates
    mode = mode if mode in ("add", "wishlist") else "add"
    exact = None
    cover_url = None

    if google_volume_id.strip():
        with get_db() as db:
            api_key = get_setting(db, "google_books_api_key") or None
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            result = await magazine_google.lookup_issue(
                google_volume_id.strip(), client, api_key=api_key
            )
        if not result.found:
            return templates.TemplateResponse(
                request,
                "fragments/scan_result.html",
                {
                    "status": "error",
                    "isbn": "",
                    "message": "Could not fetch that magazine issue from Google Books",
                    "enrich_status": scan_outcome.not_found_status(result),
                    "enrich_provider": scan_outcome.provider_label(result),
                },
            )
        exact = result.payload or {}
        title = exact.get("title") or title
        issn = exact.get("issn") or issn
        publisher = exact.get("publisher") or publisher
        language = exact.get("language") or language
        description = exact.get("description") or description
        issue_date = exact.get("issue_date") or issue_date
        cover_date_label = exact.get("issue_date") or cover_date_label
        cover_url = exact.get("cover_url")

    title = title.strip()
    issue_number = issue_number.strip()
    volume = volume.strip()
    try:
        issue_date_value = _normalise_issue_date(issue_date)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {"status": "error", "isbn": carrier_ean, "message": str(exc)},
        )

    if not title:
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {"status": "error", "isbn": carrier_ean, "message": "Magazine title is required"},
        )
    if exact is None and not issue_number and not issue_date_value:
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {
                "status": "error",
                "isbn": carrier_ean,
                "message": "Enter an issue number or issue date so this issue can be distinguished from the others",
            },
        )

    serial = None
    if carrier_ean.strip():
        serial = periodicals.parse_barcode(
            carrier_ean + (barcode_supplement.strip() or "")
        )
        if serial is None:
            return templates.TemplateResponse(
                request,
                "fragments/scan_result.html",
                {"status": "error", "isbn": carrier_ean, "message": "Invalid magazine barcode"},
            )
        if issn and periodical_records.normalise_issn(issn) != serial.issn:
            return templates.TemplateResponse(
                request,
                "fragments/scan_result.html",
                {"status": "error", "isbn": carrier_ean, "message": "Barcode ISSN does not match the selected publication"},
            )
        issn = serial.issn
        barcode_supplement = serial.supplement or ""
        carrier_ean = serial.ean13

    try:
        with get_db() as db:
            loc_id = validated_location_id(db, location_id)
    except UnknownLocationError:
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {"status": "error", "isbn": carrier_ean, "message": "Selected location no longer exists — choose another location"},
        )

    duplicate_id = None
    item_id = None
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        publication_id = periodical_records.upsert_publication(
            db,
            title=title,
            issn=issn or None,
            publisher=publisher.strip() or None,
            language=language.strip() or None,
        )
        duplicate_id = periodical_records.find_duplicate_issue(
            db,
            publication_id,
            volume=volume,
            issue_number=issue_number,
            issue_date=issue_date_value,
            google_volume_id=google_volume_id.strip() or None,
        )
        if duplicate_id is None:
            publish_year = int(issue_date_value[:4]) if issue_date_value else None
            item_id = insert_item(
                db,
                title=title,
                description=description.strip() or None,
                media_type="magazine",
                publisher=publisher.strip() or None,
                publish_year=publish_year,
                series_name=title,
                location_id=loc_id,
                source="google" if exact is not None else "manual",
                language=language.strip() or None,
                owned=0 if mode == "wishlist" else 1,
            )
            periodical_records.link_issue(
                db,
                item_id=item_id,
                publication_id=publication_id,
                volume=volume,
                issue_number=issue_number,
                issue_date=issue_date_value,
                barcode_ean=carrier_ean,
                barcode_supplement=barcode_supplement,
                cover_date_label=cover_date_label or issue_date_value,
                google_volume_id=google_volume_id.strip() or None,
            )

    if duplicate_id is not None:
        with get_db() as db:
            existing = db.execute(
                "SELECT title FROM items WHERE id = ?", (duplicate_id,)
            ).fetchone()
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {
                "status": "duplicate",
                "isbn": carrier_ean,
                "title": existing["title"] if existing else title,
                "item_id": duplicate_id,
            },
        )

    cover_path = None
    if cover_url and item_id:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            cover_path = await covers._download_to_item(item_id, cover_url, client)
        if cover_path:
            with get_db() as db:
                db.execute(
                    "UPDATE items SET cover_path = ? WHERE id = ?",
                    (cover_path, item_id),
                )

    status = "wishlisted" if mode == "wishlist" else "added"
    items_common._log_scan(
        (serial.full_code if serial else google_volume_id),
        "magazine",
        status,
        item_id,
        mode,
    )
    return templates.TemplateResponse(
        request,
        "fragments/scan_result.html",
        {
            "status": status,
            "isbn": serial.full_code if serial else "",
            "title": title,
            "authors": _issue_label(issue_number or None, issue_date_value),
            "cover_path": cover_path,
            "item_id": item_id,
            "source": "google" if exact is not None else "manual",
            "media_type_label": MEDIA_TYPES["magazine"],
            "detect_reason": (
                f"ISSN {periodical_records.normalise_issn(issn)} · "
                f"{_issue_label(issue_number or None, issue_date_value)}"
                if issn else _issue_label(issue_number or None, issue_date_value)
            ),
        },
    )
