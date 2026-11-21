"""First-class periodical catalogue and 977 scan confirmation routes."""

from __future__ import annotations

from html import escape

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_role
from app.features import feature_enabled, require_feature
from app.config import HTTP_TIMEOUT
from app.database import get_db, get_setting
from app.routers import items_common, shelf_fill
from app.services import cover_queue, item_write, periodical_google, periodical_records, periodical_scan, periodicals
from app.services import libraries
from app.services import tags as tags_svc
from app.services.catalogue_security import require_catalogue_role, require_item_role, require_library_role
from app.services.item_write import ItemValueError, insert_item

router = APIRouter()


def _issue_title(
    publication: str,
    *,
    issue_number: str | None = None,
    issue_date: str | None = None,
    cover_date_label: str | None = None,
) -> str:
    publication = publication.strip()
    if issue_number and issue_number.strip():
        return f"{publication} — No. {issue_number.strip()}"
    if cover_date_label and cover_date_label.strip():
        return f"{publication} — {cover_date_label.strip()}"
    if issue_date and issue_date.strip():
        return f"{publication} — {issue_date.strip()}"
    return publication


def _issue_year(issue_date: str | None) -> int | None:
    value = (issue_date or "").strip()
    return int(value[:4]) if len(value) >= 4 and value[:4].isdigit() else None


def _scan_context(raw_barcode: str):
    serial = periodicals.parse_barcode(raw_barcode)
    if serial is None:
        raise ValueError("A valid periodical barcode is required")
    return serial


def _require_issue_editor(db, item_id: int) -> None:
    """A hidden live/Trash match is an opaque identifier refusal, not a restore."""
    try:
        require_item_role(db, item_id, "editor", include_deleted=True)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise item_write.IdentifierUnavailable("This identifier is unavailable") from None
        raise


def _preflight_issue_matches(db, publication_id, serial, *, volume, issue_number, issue_date):
    """Inspect ALL matching evidence, including hidden matches behind a live one.

    The native duplicate finder prefers live items over Trash. Keep that choice,
    but never let its scoped first pass conceal an unauthorized competing match.
    This helper returns no catalogue metadata to the caller.
    """
    predicates, params = [], []
    if serial.supplement:
        predicates.append("(barcode_ean = ? AND barcode_supplement = ?)")
        params.extend((serial.ean13, serial.supplement))
    if publication_id is not None:
        if issue_number.strip():
            predicates.append("(publication_id = ? AND COALESCE(volume, '') = ? AND issue_number = ? COLLATE NOCASE)")
            params.extend((publication_id, volume.strip(), issue_number.strip()))
        if issue_date.strip():
            predicates.append("(publication_id = ? AND issue_date = ?)")
            params.extend((publication_id, issue_date.strip()))
    if predicates:
        for row in db.execute(
            "SELECT item_id FROM periodical_issues WHERE " + " OR ".join(predicates) + " ORDER BY item_id",
            params,
        ).fetchall():
            _require_issue_editor(db, row["item_id"])


def _assist_message(message: str, *, raw_barcode: str | None = None) -> HTMLResponse:
    """Return an HTMX-visible assisted-search message without a silent 4xx.

    A selected result targets the whole scan card. For a valid scan we retarget
    refusals back into the assisted-results slot so the confirmation form stays
    intact and the user can try another result.
    """
    headers = None
    serial = periodicals.parse_barcode(raw_barcode or "")
    if serial is not None:
        headers = {
            "HX-Retarget": f"#periodical-assisted-{serial.full_code}",
            "HX-Reswap": "innerHTML",
        }
    return HTMLResponse(
        '<p class="text-xs text-shelf-warning">' + message + "</p>",
        status_code=200,
        headers=headers,
    )


def find_periodical_item(raw: str) -> dict | None:
    """Resolve a stored issue only from a full 977 + supplement identity.

    A 977 carrier identifies the publication, not one concrete issue. A
    carrier-only scan must therefore never resolve to an existing issue even
    when only one issue currently uses that carrier: doing so would make the
    next issue a false duplicate. The add-on is required at this identity
    boundary.
    """
    serial = periodicals.parse_barcode(raw)
    if serial is None or not serial.supplement:
        return None

    with get_db() as db:
        rows = db.execute(
            """SELECT i.*, l.name AS location_name
               FROM periodical_issues pi
               JOIN items_live i ON i.id = pi.item_id
               LEFT JOIN locations l ON l.id = i.location_id
               WHERE pi.barcode_ean = ? AND pi.barcode_supplement = ?
               ORDER BY i.id LIMIT 2""",
            (serial.ean13, serial.supplement),
        ).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


async def render_scan_candidate(
    request: Request,
    templates,
    raw: str,
    location_id: int | None,
    mode: str,
):
    """Render publication metadata plus issue-confirmation fields for a 977 scan."""
    serial = periodicals.parse_barcode(raw)
    if serial is None:
        return HTMLResponse("Not a valid periodical barcode", status_code=400)

    existing = find_periodical_item(raw)
    if existing:
        items_common._log_scan(serial.full_code, "magazine", "duplicate", existing["id"], mode)
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {
                "status": "duplicate",
                "isbn": serial.full_code,
                "title": existing["title"],
                "item_id": existing["id"],
                "message": "This periodical issue is already in the collection",
            },
        )

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await periodical_scan.resolve_serial_barcode(raw, client)
    candidate = result.payload if isinstance(result.payload, dict) else {}
    submitted = await request.form()
    items_common._log_scan(serial.full_code, "magazine", "periodical_confirm", mode=mode)
    notice = None
    if not result.found:
        notice = {
            "rate_limited": "The ISSN metadata source is rate-limiting requests. The barcode was decoded; confirm the publication below.",
            "transport_failed": "The ISSN metadata source could not be reached. The barcode was decoded; confirm the publication below.",
            "rejected": "The ISSN metadata source rejected the lookup. The barcode was decoded; confirm the publication below.",
            "no_match": "No publication metadata was returned for this ISSN. Enter the publication title below.",
        }.get(result.outcome, "Publication metadata was unavailable; confirm the publication below.")

    return templates.TemplateResponse(
        request,
        "fragments/periodical_scan_result.html",
        {
            "candidate": candidate,
            "raw_barcode": serial.full_code,
            "location_id": location_id,
            "mode": mode if mode in {"add", "wishlist"} else "add",
            "notice": notice,
            "csrf_token": request.cookies.get("csrf_token", ""),
            "library_id": submitted.get("library_id"), "tags": submitted.get("tags") or "",
            "reading_status": submitted.get("reading_status") or "",
        },
    )


@router.get("/api/periodicals/assist/search")
async def assisted_periodical_search(
    request: Request,
    q: str = Query("", max_length=120),
    raw_barcode: str = Query(..., max_length=32),
    location_id: int | None = Query(None),
    mode: str = Query("add"),
    location_node_id: int | None = Query(None),
    shelf_fill_origin: str = Query(""),
    library_id: int | None = Query(None),
    tags: str = Query(""),
    reading_status: str = Query(""),
    _=Depends(require_catalogue_role("editor")),
):
    try:
        serial = _scan_context(raw_barcode)
        continuation = await shelf_fill.require_continuation(request, location_node_id, shelf_fill_origin)
    except ValueError:
        return _assist_message("A valid periodical barcode is required")

    with get_db() as db:
        if library_id is not None:
            require_library_role(db, library_id, "editor")
        api_key = get_setting(db, "google_books_api_key") or None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await periodical_google.search_issues(q, client, api_key=api_key)
    candidates = result.payload if result.found and isinstance(result.payload, list) else []
    if continuation and request.headers.get("HX-Request") != "true":
        # The historical assisted fragment has HX-only buttons. A native
        # continuation instead renders real GET selection forms in the owned
        # confirmation card; do not silently drop its NODE or return surface.
        return request.app.state.templates.TemplateResponse(
            request, "fragments/periodical_scan_result.html",
            {"candidate": {"publication_title": q.strip(), "issn": serial.issn,
                           "barcode_ean": serial.ean13, "barcode_supplement": serial.supplement},
             "native_assisted_candidates": candidates, "raw_barcode": serial.full_code,
             "location_id": location_id, "mode": mode if mode in {"add", "wishlist"} else "add",
             **continuation, "library_id": library_id, "tags": tags, "reading_status": reading_status},
        )
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
            **continuation, "library_id": library_id, "tags": tags, "reading_status": reading_status,
        },
    )


@router.get("/api/periodicals/assist/select")
async def assisted_periodical_select(
    request: Request,
    volume_id: str = Query(..., max_length=80),
    raw_barcode: str = Query(..., max_length=32),
    location_id: str = Query(""),
    mode: str = Query("add"),
    location_node_id: int | None = Query(None),
    shelf_fill_origin: str = Query(""),
    library_id: int | None = Query(None),
    tags: str = Query(""),
    reading_status: str = Query(""),
    _=Depends(require_catalogue_role("editor")),
):
    try:
        serial = _scan_context(raw_barcode)
        # Older assisted-result buttons include location_id= for an unmapped
        # shelf. Empty means no flat location, never a fallback to a NODE int.
        location_id = int(location_id) if location_id else None
        continuation = await shelf_fill.require_continuation(request, location_node_id, shelf_fill_origin)
    except ValueError:
        return _assist_message("A valid periodical barcode is required")

    with get_db() as db:
        if library_id is not None:
            require_library_role(db, library_id, "editor")
        api_key = get_setting(db, "google_books_api_key") or None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await periodical_google.lookup_issue(volume_id, client, api_key=api_key)
    if not result.found or not isinstance(result.payload, dict):
        return _assist_message(
            "That magazine result is no longer available. Try another result.",
            raw_barcode=serial.full_code,
        )

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
            **continuation, "library_id": library_id, "tags": tags, "reading_status": reading_status,
        },
    )


@router.post("/api/periodicals/confirm")
async def confirm_periodical_issue(
    request: Request,
    raw_barcode: str = Form(...),
    publication_title: str = Form(...),
    publication_issn: str = Form(""),
    publisher: str = Form(""),
    language: str = Form(""),
    volume: str = Form(""),
    issue_number: str = Form(""),
    issue_date: str = Form(""),
    cover_date_label: str = Form(""),
    cover_url: str = Form(""),
    location_id: int | None = Form(None),
    library_id: int | None = Form(None, ge=1, le=2**63 - 1),
    mode: str = Form("add"),
    location_node_id: int | None = Form(None),
    shelf_fill_origin: str = Form(""),
    tags: str = Form(""),
    reading_status: str = Form(""),
    _=Depends(require_catalogue_role("editor")),
):
    """Create one concrete issue after the user confirms its issue identity."""
    serial = periodicals.parse_barcode(raw_barcode)
    publication_title = publication_title.strip()
    if serial is None or not publication_title:
        return HTMLResponse("A valid periodical barcode and publication title are required", status_code=400)
    if mode not in {"add", "wishlist"}:
        mode = "add"

    try:
        continuation = await shelf_fill.require_continuation(request, location_node_id, shelf_fill_origin)
        if continuation and mode != "add":
            raise ValueError("Shelf Fill confirmations must use Add mode")
        # The scanned carrier remains the issue-barcode identity. An explicit,
        # checksum-valid ISSN can correct only the publication identity when
        # stronger evidence (provider result or printed masthead) proves the
        # 977-derived hint is misleading.
        confirmed_issn = periodicals.normalise_issn(publication_issn) or serial.issn
        placed = None
        with get_db() as db, item_write._write_scope(db):
            flat_location_id = shelf_fill.continuation_location(db, continuation, location_id)
            # Look up before writing: a re-confirmed issue redirects without
            # touching any publication, so an overridden ISSN cannot leave an
            # empty publication behind.
            known_publication_id = periodical_records.find_publication(
                db, title=publication_title, issn=confirmed_issn
            )
            _preflight_issue_matches(
                db, known_publication_id, serial,
                volume=volume, issue_number=issue_number, issue_date=issue_date,
            )
            existing_id = periodical_records.find_duplicate_issue(
                db,
                known_publication_id,
                volume=volume,
                issue_number=issue_number,
                issue_date=issue_date,
                barcode_ean=serial.ean13,
                barcode_supplement=serial.supplement,
            )
            if existing_id:
                # 0.45.1 makes this earliest guard the periodical restore path:
                # `find_duplicate_issue` can see the trashed issue while normal
                # Browse cannot, so restore it before redirecting to its item page.
                _require_issue_editor(db, existing_id)
                if library_id is not None and libraries.item_library_id(db, existing_id) != library_id:
                    raise ItemValueError("This issue is not in the requested library")
                item_write.restore_item(db, existing_id)
                item_id = existing_id
            else:
                target_library = library_id if library_id is not None else libraries.DEFAULT_LIBRARY_ID
                require_library_role(db, target_library, "editor")
                # Upsert changes SHARED metadata. Authorize every attachment,
                # including Trash, before the first publication mutation.
                if known_publication_id is not None:
                    for child in db.execute(
                        "SELECT item_id FROM periodical_issues WHERE publication_id = ? ORDER BY item_id",
                        (known_publication_id,),
                    ).fetchall():
                        _require_issue_editor(db, child["item_id"])
                publication_id = periodical_records.upsert_publication(
                    db, title=publication_title, issn=confirmed_issn,
                    publisher=publisher.strip() or None, language=language.strip() or None,
                )
                with tags_svc.default_tags(tags), item_write.default_reading_status(reading_status):
                    item_id = insert_item(
                        db,
                        title=_issue_title(publication_title, issue_number=issue_number,
                                           issue_date=issue_date, cover_date_label=cover_date_label),
                        media_type="magazine", library_id=target_library,
                        publisher=publisher.strip() or None, publish_year=_issue_year(issue_date),
                        location_id=flat_location_id, owned=0 if mode == "wishlist" else 1,
                        wishlisted=(mode == "wishlist"), source="issn",
                        **({"reading_status": reading_status} if reading_status else {}),
                    )
                require_item_role(db, item_id, "editor")
                periodical_records.link_issue(
                    db, item_id=item_id, publication_id=publication_id,
                    volume=volume, issue_number=issue_number, issue_date=issue_date,
                    barcode_ean=serial.ean13, barcode_supplement=serial.supplement,
                    cover_date_label=cover_date_label,
                )
            if continuation:
                # The SAME connection/scope owns issue, restore and copy writes.
                # Any placement refusal rolls back all of them; no accepted ID
                # exists yet, so this path must not claim saved_unplaced.
                placed = shelf_fill._place_item(db, item_id, continuation["location_node_id"])
    except ItemValueError as exc:
        if request.headers.get("HX-Request") == "true":
            return shelf_fill._render_error(request, raw_barcode, str(exc))
        return HTMLResponse(escape(str(exc)), status_code=400)
    except ValueError as exc:
        if request.headers.get("HX-Request") == "true":
            return shelf_fill._render_error(request, raw_barcode, str(exc))
        return HTMLResponse(escape(str(exc)), status_code=400)

    if existing_id:
        if placed:
            return shelf_fill._render_result(request, placed, origin=shelf_fill_origin)
        return RedirectResponse(f"/item/{existing_id}", status_code=303)

    selected_cover_url = cover_url.strip()
    if selected_cover_url:
        # A magazine must never fall through to the generic book title/author
        # recovery path if this provider image fails. The exact URL is still
        # checked by covers.download_cover's trusted-domain allowlist.
        cover_queue.enqueue(
            item_id,
            hints={
                "cover_url": selected_cover_url,
                "skip_title_search": True,
            },
        )

    if placed:
        return shelf_fill._render_result(request, placed, newly_added=True, origin=shelf_fill_origin)
    # Confirmation is core Scan even when optional publication browsing is off.
    # A committed issue must lead to a usable core page, not the disabled door.
    if not feature_enabled("periodicals"):
        return RedirectResponse(f"/item/{item_id}", status_code=303)
    return RedirectResponse(f"/periodicals/{publication_id}", status_code=303)


@router.get("/periodicals")
async def periodicals_page(request: Request, _=Depends(require_role("viewer")),
                           __=Depends(require_feature("periodicals"))):
    with get_db() as db:
        # Empty publications are an explicit native account-admin catalogue
        # capability, not evidence that an ordinary reader can see a serial.
        user = request.state.user
        show_empty = bool(
            user.get("role") == "admin" and not user.get("api_token_id")
            and db.execute("SELECT 1 FROM users WHERE id = ? AND role = 'admin'", (user["id"],)).fetchone()
        )
        publications = db.execute(
            """SELECT p.*, COUNT(il.id) AS issue_count
               FROM periodical_publications p
               LEFT JOIN periodical_issues pi ON pi.publication_id = p.id
                LEFT JOIN items_live il ON il.id = pi.item_id
                GROUP BY p.id
                 HAVING COUNT(il.id) > 0 OR ?
               ORDER BY p.title COLLATE NOCASE""", (show_empty,)
        ).fetchall()
    return request.app.state.templates.TemplateResponse(
        request, "periodicals.html", {"publications": publications}
    )


@router.get("/periodicals/{publication_id}")
async def publication_page(
    request: Request,
    publication_id: int,
    _=Depends(require_role("viewer")),
    __=Depends(require_feature("periodicals")),
):
    with get_db() as db:
        publication = db.execute(
            "SELECT p.* FROM periodical_publications p WHERE p.id = ? "
            "AND EXISTS (SELECT 1 FROM periodical_issues pi JOIN items_live i ON i.id = pi.item_id "
            "WHERE pi.publication_id = p.id)", (publication_id,)
        ).fetchone()
        if not publication:
            return RedirectResponse("/periodicals", status_code=303)
        issues = periodical_records.issues_for_publication(db, publication_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "periodical_publication.html",
        {"publication": publication, "issues": issues},
    )
