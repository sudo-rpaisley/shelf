"""Explicit magazine issue APIs and publication pages.

Startup composes ``router`` and ``page_router`` exactly once. The canonical
items_catalog title-search handler may call ``magazine_search_results`` for its
magazine branch; importing this module never changes another module's routes.
"""

from datetime import date
import re

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import HTTP_TIMEOUT, MEDIA_TYPES
from app.database import get_db, get_setting
from app.features import require_feature
from app.routers import items_common, items_magazines, shelf_fill
from app.services import catalogue_security, covers, item_write, libraries, magazine_google
from app.services import periodical_records, periodicals, scan_outcome
from app.services import tags as tags_svc
from app.services.catalogue_security import require_catalogue_role, require_item_role, require_library_role

router = APIRouter(prefix="/api")
page_router = APIRouter()
_PARTIAL_DATE_RE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")


def _normalise_issue_date(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if not _PARTIAL_DATE_RE.fullmatch(raw):
        raise ValueError("Issue date must be YYYY, YYYY-MM or YYYY-MM-DD")
    if int(raw[:4]) < 1:
        raise ValueError("Issue year must be between 0001 and 9999")
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


async def _require_periodical_request(request: Request, role: str) -> None:
    """Direct helper calls do not run FastAPI dependencies automatically.

    The caller must be inside the canonical authenticated actor middleware and,
    for POSTs, the application's CSRF middleware. Never create a trusted service
    context as a fallback when a request forgot to install its actor.
    """
    user = getattr(request.state, "user", None)
    if user and catalogue_security.current_actor() != dict(user):
        raise RuntimeError("Magazine requests require the authenticated catalogue actor")
    await require_catalogue_role(role)(request)
    await require_feature("periodicals")(request)


def _scan_card(request: Request, **context):
    return request.app.state.templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {"media_type": "magazine", "media_type_label": MEDIA_TYPES["magazine"], **context},
    )


def publication_list_context(db) -> dict:
    """Read-only context; the caller supplies a live, actor-scoped connection."""
    return {"publications": periodical_records.list_publications(db)}


def publication_detail_context(db, publication_id: int) -> dict | None:
    """Read-only issue/copy context; hidden and Trash rows stay in the service."""
    context = periodical_records.publication_catalogue(db, publication_id)
    if context is None:
        return None
    actor = catalogue_security.current_actor()
    editable = actor is not None and all(
        libraries.has_item_role(db, actor, issue["item_id"], "editor")
        for issue in context["issues"]
    )
    source_libraries = {
        libraries.item_library_id(db, issue["item_id"]) for issue in context["issues"]
    }
    return {
        **context, "can_edit": editable, "can_edit_publication": editable,
        "can_bulk_add": editable and None not in source_libraries and len(source_libraries) == 1,
    }


@page_router.get("/magazines")
async def magazine_publications(
    request: Request,
    _=Depends(require_catalogue_role("viewer")),
    __=Depends(require_feature("periodicals")),
):
    """Browse magazines by publication rather than as one flat item list."""
    with get_db() as db:
        context = publication_list_context(db)
        user = request.state.user
        context["barcode_libraries"] = [
            library for library in libraries.list_libraries(db)
            if libraries.has_library_role(db, user, library["id"], "editor")
        ]
    if context["barcode_libraries"]:
        context["barcode_locations"] = items_common._manual_form_locations()
    return request.app.state.templates.TemplateResponse(
        request,
        "magazines.html",
        context,
    )


@page_router.get("/magazines/publications/{publication_id}")
async def magazine_publication_detail(
    request: Request,
    publication_id: int,
    _=Depends(require_catalogue_role("viewer")),
    __=Depends(require_feature("periodicals")),
):
    """Show every tracked issue and copy belonging to one publication."""
    with get_db() as db:
        catalogue = publication_detail_context(db, publication_id)
    if catalogue is None:
        return RedirectResponse(url="/magazines", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request,
        "magazine_publication.html",
        catalogue,
    )


@router.get("/magazines/assist/scan")
async def assist_magazine_barcode(
    request: Request,
    raw_barcode: str = Query(..., max_length=32),
    location_id: str = Query(""),
    mode: str = Query("add"),
    library_id: int | None = Query(None),
    tags: str = Query(""),
    reading_status: str = Query(""),
    location_node_id: int | None = Query(None),
    shelf_fill_origin: str = Query(""),
    _=Depends(require_catalogue_role("editor")),
    __=Depends(require_feature("periodicals")),
):
    """Explicit legacy assistance; native core977 never enters this ladder."""
    await _require_periodical_request(request, "editor")
    try:
        # A native GET form submits location_id= for its "No location" option.
        location_id = int(location_id) if location_id else None
        serial = periodicals.parse_barcode(raw_barcode)
        if serial is None:
            raise ValueError("A valid magazine barcode is required")
        if mode not in {"add", "wishlist"}:
            raise ValueError("Magazine barcode assistance requires Add or Wishlist mode")
        if location_node_id is not None and location_node_id >= 2**63:
            raise ValueError("Invalid Shelf Fill continuation; start a fresh scan")
        continuation = await shelf_fill.require_continuation(request, location_node_id, shelf_fill_origin)
        if continuation and mode != "add":
            raise ValueError("Shelf Fill confirmations must use Add mode")
        with get_db() as db:
            target_library = library_id if library_id is not None else libraries.DEFAULT_LIBRARY_ID
            require_library_role(db, target_library, "editor")
            # Even with a NODE continuation, a supplied FLAT selection must be
            # valid. Conversion never equates the two integer namespaces.
            item_write.validated_location_id(db, location_id)
            flat_location_id = shelf_fill.continuation_location(db, continuation, location_id)
    except ValueError as exc:
        return _scan_card(request, status="error", isbn=raw_barcode, message=str(exc))

    return await items_magazines.scan_magazine(
        request, request.app.state.templates, serial.full_code, serial,
        "magazine", flat_location_id, mode,
        library_id=target_library, tags=tags, reading_status=reading_status,
        **continuation,
    )


@router.get("/magazines/search")
async def search_magazines(
    request: Request,
    q: str = Query("", max_length=200),
    location_id: int | None = None,
    mode: str = "add",
    carrier_ean: str = "",
    barcode_supplement: str = "",
    issn_hint: str = "",
    for_scan: bool = False,
    library_id: int | None = None,
    tags: str = "",
    reading_status: str = "",
    location_node_id: int | None = None,
    shelf_fill_origin: str = "",
    _=Depends(require_catalogue_role("editor")),
    __=Depends(require_feature("periodicals")),
):
    return await magazine_search_results(
        request, q=q, location_id=location_id, mode=mode,
        carrier_ean=carrier_ean, barcode_supplement=barcode_supplement,
        issn_hint=issn_hint, for_scan=for_scan, library_id=library_id,
        tags=tags, reading_status=reading_status,
        location_node_id=location_node_id, shelf_fill_origin=shelf_fill_origin,
    )


def magazine_search_context(
    result, *, q: str, location_id: int | None, mode: str,
    carrier_ean: str, barcode_supplement: str, issn_hint: str | None,
    for_scan: bool, library_id: int | None, tags: str, reading_status: str,
    location_node_id: int | None = None, shelf_fill_origin: str = "",
) -> dict:
    """Pure result-to-template mapping, with every continuation field retained."""
    return {
        "results": result.payload or [], "query": q,
        "selected_location_id": location_id,
        "mode": mode if mode in ("add", "wishlist") else "add",
        "search_status": scan_outcome.not_found_status(result),
        "search_provider": scan_outcome.provider_label(result),
        "for_scan": for_scan, "carrier_ean": carrier_ean.strip(),
        "barcode_supplement": barcode_supplement.strip(), "issn_hint": issn_hint,
        "library_id": library_id, "selected_library_id": library_id,
        "tags": tags, "reading_status": reading_status,
        "location_node_id": location_node_id, "shelf_fill_origin": shelf_fill_origin,
    }


async def magazine_search_results(
    request: Request,
    q: str = "",
    *,
    location_id: int | None = None,
    mode: str = "add",
    carrier_ean: str = "",
    barcode_supplement: str = "",
    issn_hint: str = "",
    for_scan: bool = False,
    library_id: int | None = None,
    tags: str = "",
    reading_status: str = "",
    location_node_id: int | None = None,
    shelf_fill_origin: str = "",
):
    """Guarded, undecorated helper for canonical magazine title search.

    No Depends/Query/Form defaults and no delegation to a bound route handler.
    Authentication and periodicals gating also apply when called directly.
    Non-magazine dispatch remains entirely the canonical handler's concern.
    """
    await _require_periodical_request(request, "editor")
    try:
        await shelf_fill.require_continuation(request, location_node_id, shelf_fill_origin)
    except ValueError as exc:
        return _scan_card(request, status="error", isbn=carrier_ean, message=str(exc))
    templates = request.app.state.templates
    q = q.strip()[:200]
    if not q:
        return HTMLResponse("")
    try:
        hint = periodical_records.normalise_issn(issn_hint)
    except ValueError as exc:
        return _scan_card(request, status="error", isbn=carrier_ean, message=str(exc))
    with get_db() as db:
        if library_id is not None:
            require_library_role(db, library_id, "editor")
        api_key = get_setting(db, "google_books_api_key") or None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await magazine_google.search_issues(
            q, client, api_key=api_key, limit=10
        )
    return templates.TemplateResponse(
        request,
        "fragments/magazine_search_results.html",
        magazine_search_context(
            result, q=q, location_id=location_id, mode=mode, carrier_ean=carrier_ean,
            barcode_supplement=barcode_supplement, issn_hint=hint, for_scan=for_scan,
            library_id=library_id, tags=tags, reading_status=reading_status,
            location_node_id=location_node_id, shelf_fill_origin=shelf_fill_origin,
        ),
    )


@router.get("/magazines/select-search-result")
async def select_magazine_search_result(
    request: Request,
    google_volume_id: str = "",
    carrier_ean: str = "",
    barcode_supplement: str = "",
    issn_hint: str = "",
    location_id: int | None = None,
    mode: str = "add",
    library_id: int | None = None,
    tags: str = "",
    reading_status: str = "",
    location_node_id: int | None = None,
    shelf_fill_origin: str = "",
    _=Depends(require_catalogue_role("editor")),
    __=Depends(require_feature("periodicals")),
):
    """Turn a searched Google issue into a prefilled, still-confirmable scan form."""
    templates = request.app.state.templates
    try:
        continuation = await shelf_fill.require_continuation(request, location_node_id, shelf_fill_origin)
    except ValueError as exc:
        return _scan_card(request, status="error", isbn=carrier_ean, message=str(exc))
    mode = mode if mode in ("add", "wishlist") else "add"
    google_volume_id = google_volume_id.strip()
    with get_db() as db:
        if library_id is not None:
            require_library_role(db, library_id, "editor")
        api_key = get_setting(db, "google_books_api_key") or None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await magazine_google.lookup_issue(
            google_volume_id, client, api_key=api_key
        )
    if not result.found or (result.payload or {}).get("google_volume_id") != google_volume_id:
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {
                "status": "error",
                "isbn": carrier_ean,
                "message": "Could not fetch that magazine issue from Google Books",
                "enrich_status": scan_outcome.not_found_status(result),
                "enrich_provider": scan_outcome.provider_label(result),
            },
        )

    issue = result.payload or {}
    try:
        confirmed_issn = periodical_records.normalise_issn(issue.get("issn")) or ""
        hint = periodical_records.normalise_issn(issn_hint)
    except ValueError as exc:
        return _scan_card(request, status="error", isbn=carrier_ean, message=str(exc))
    return templates.TemplateResponse(
        request,
        "fragments/magazine_issue_form.html",
        {
            "google_volume_id": google_volume_id,
            "title": issue.get("title") or "",
            "publisher": issue.get("publisher"),
            "description": issue.get("description"),
            "language": issue.get("language"),
            "issn": confirmed_issn,
            "issn_hint": hint,
            "issue_date": issue.get("issue_date") or "",
            "cover_date_label": issue.get("issue_date") or "",
            "carrier_ean": carrier_ean.strip(),
            "supplement": barcode_supplement.strip(),
            "mode": mode,
            "locations": items_common._manual_form_locations(),
            "selected_location_id": location_id if location_id and location_id > 0 else None,
            "match_source": "Google Books",
            "library_id": library_id,
            "selected_library_id": library_id,
            "tags": tags,
            "reading_status": reading_status,
            **continuation,
        },
    )


def _existing_issue(db, publication_id: int | None, *, volume: str,
                    issue_number: str, issue_date: str | None, carrier_ean: str,
                    barcode_supplement: str, google_volume_id: str):
    """Collision-only physical read, followed by ALL-target authorization.

    A visible number/date cannot hide a conflicting provider identity in another
    library or in Trash. No hidden title, ID, library or deletion state escapes.
    Publication metadata is global; issue/copy browsing still uses live views.
    """
    strategies = []
    if google_volume_id:
        strategies.append(("pi.google_volume_id = ?", (google_volume_id,)))
    if carrier_ean and barcode_supplement:
        strategies.append(("pi.barcode_ean = ? AND pi.barcode_supplement = ?",
                           (carrier_ean, barcode_supplement)))
    if publication_id is not None and issue_number:
        strategies.append((
            "pi.publication_id = ? AND COALESCE(TRIM(pi.volume), '') = ? "
            "AND pi.issue_number = ? COLLATE NOCASE", (publication_id, volume, issue_number),
        ))
    if publication_id is not None and issue_date:
        strategies.append(("pi.publication_id = ? AND pi.issue_date = ?",
                           (publication_id, issue_date)))
    targets = set()
    for predicate, params in strategies:
        targets.update(row["item_id"] for row in db.execute(
            f"SELECT pi.item_id FROM periodical_issues pi WHERE {predicate}", params,
        ).fetchall())
    existing = None
    for item_id in sorted(targets):
        try:
            existing = require_item_role(db, item_id, "editor", include_deleted=True)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise item_write.ItemValueError("This issue identity is unavailable") from None
            raise
    if len(targets) > 1:
        raise ValueError("Issue evidence matches more than one catalogue item")
    return existing


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
    library_id: int | None = Form(None),
    tags: str = Form(""),
    reading_status: str = Form(""),
    location_node_id: int | None = Form(None),
    shelf_fill_origin: str = Form(""),
    _=Depends(require_catalogue_role("editor")),
    __=Depends(require_feature("periodicals")),
):
    templates = request.app.state.templates
    mode = mode if mode in ("add", "wishlist") else "add"
    try:
        continuation = await shelf_fill.require_continuation(request, location_node_id, shelf_fill_origin)
        if continuation and mode != "add":
            raise ValueError("Shelf Fill confirmations must use Add mode")
    except ValueError as exc:
        return _scan_card(request, status="error", isbn=carrier_ean, message=str(exc))
    exact = None
    cover_url = None
    google_volume_id = google_volume_id.strip()

    if google_volume_id:
        with get_db() as db:
            api_key = get_setting(db, "google_books_api_key") or None
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            result = await magazine_google.lookup_issue(
                google_volume_id, client, api_key=api_key
            )
        if not result.found or (result.payload or {}).get("google_volume_id") != google_volume_id:
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
    submitted_issn = issn.strip()
    try:
        issn = periodical_records.normalise_issn(submitted_issn) or ""
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
    carrier_ean = carrier_ean.strip()
    barcode_supplement = barcode_supplement.strip()
    if barcode_supplement and (not carrier_ean or not re.fullmatch(r"(?:[0-9]{2}|[0-9]{5})", barcode_supplement)):
        return _scan_card(request, status="error", isbn=carrier_ean, message="Invalid magazine barcode supplement")
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
        # The barcode's derived ISSN is a lookup hint, not an authority over a
        # provider/user-confirmed publication. Preserve the raw barcode and keep
        # an explicitly supplied publication ISSN when one exists.
        barcode_supplement = serial.supplement or ""
        carrier_ean = serial.ean13

    existing = None
    item_id = None
    placed = None
    target_library = library_id if library_id is not None else libraries.DEFAULT_LIBRARY_ID
    try:
        with get_db() as db, item_write._write_scope(db):
            loc_id = shelf_fill.continuation_location(db, continuation, location_id)
            if library_id is not None:
                require_library_role(db, library_id, "editor")
            publication_id = periodical_records.find_publication(db, title=title, issn=issn or None)
            existing = _existing_issue(
                db, publication_id, volume=volume, issue_number=issue_number,
                issue_date=issue_date_value, carrier_ean=carrier_ean,
                barcode_supplement=barcode_supplement, google_volume_id=google_volume_id,
            )
            if existing is not None:
                if library_id is not None and libraries.item_library_id(db, existing["id"]) != library_id:
                    raise item_write.ItemValueError("This issue identity is unavailable in the requested library")
                existing = dict(existing)
            else:
                require_library_role(db, target_library, "editor")
                # Reuse global publication metadata without editing issues in
                # other libraries. An Add is not a shared-publication edit.
                if publication_id is None:
                    publication_id = periodical_records.upsert_publication(
                        db, title=title, issn=issn or None,
                        publisher=publisher.strip() or None, language=language.strip() or None,
                    )
                with tags_svc.default_tags(tags), item_write.default_reading_status(reading_status):
                    item_id = item_write.insert_item(
                        db, library_id=target_library, title=title,
                        description=description.strip() or None, media_type="magazine",
                        publisher=publisher.strip() or None,
                        publish_year=int(issue_date_value[:4]) if issue_date_value else None,
                        page_count=exact.get("page_count") if exact is not None else None,
                        series_name=title, location_id=loc_id,
                        source="google" if exact is not None else "manual",
                        language=language.strip() or None,
                        owned=0 if mode == "wishlist" else 1,
                        wishlisted=(mode == "wishlist"),
                        **({"reading_status": reading_status} if reading_status else {}),
                    )
                periodical_records.link_issue(
                    db, item_id=item_id, publication_id=publication_id,
                    volume=volume, issue_number=issue_number, issue_date=issue_date_value,
                    barcode_ean=carrier_ean, barcode_supplement=barcode_supplement,
                    cover_date_label=cover_date_label or issue_date_value,
                    google_volume_id=google_volume_id or None,
                )
            if continuation and (existing is None or not existing["deleted_at"]):
                # No commit between identity authorization, insert/link and
                # precise-copy placement. A refusal rolls this entire Add back.
                placed = shelf_fill._place_item(
                    db, existing["id"] if existing is not None else item_id,
                    continuation["location_node_id"],
                )
    except ValueError as exc:
        return _scan_card(request, status="error", isbn=carrier_ean, message=str(exc))

    if existing is not None:
        if placed:
            return shelf_fill._render_result(request, placed, origin=shelf_fill_origin)
        # Keep Trash explicit; only the native restore endpoint changes lifecycle.
        return _scan_card(
            request, status="in_trash" if existing["deleted_at"] else "duplicate",
            isbn=serial.full_code if serial else google_volume_id,
            title=existing["title"], item_id=existing["id"],
            cover_path=existing["cover_path"], deleted_at=existing["deleted_at"], mode=mode,
            **continuation,
        )

    cover_path = None
    if cover_url and item_id:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            cover_path = await covers._download_to_item(item_id, cover_url, client)
        if cover_path:
            with get_db() as db:
                items_common.store_item_cover(
                    db, item_id, cover_path, library_ids={int(item_id): target_library},
                )

    status = "wishlisted" if mode == "wishlist" else "added"
    items_common._log_scan(
        (serial.full_code if serial else google_volume_id),
        "magazine",
        status,
        item_id,
        mode,
    )
    if placed:
        placed["cover_path"] = cover_path or placed["cover_path"]
        return shelf_fill._render_result(request, placed, newly_added=True, origin=shelf_fill_origin)
    return templates.TemplateResponse(
        request,
        "fragments/scan_result.html",
        {
            "status": status,
            "media_type": "magazine",
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
