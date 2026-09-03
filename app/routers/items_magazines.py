"""Magazine-specific barcode scan flow.

Kept out of items_common.py because 977/ISSN handling is its own catalogue
domain.  The public route still lives in items.py; items_common dispatches a
recognised serial barcode here after the normal scanner has classified it as
a retail/EAN barcode.
"""

import sqlite3

import httpx
from fastapi import Request

from app.config import HTTP_TIMEOUT, MEDIA_TYPES
from app.database import get_db, get_setting
from app.routers import items_common
from app.services import googlebooks, periodicals, scan_outcome, upcitemdb
from app.services.item_write import insert_item


async def scan_magazine(
    request: Request,
    templates,
    upc_norm: str,
    serial: periodicals.PeriodicalBarcode,
    media_type_hint: str,
    location_id: int | None,
    mode: str,
):
    """Identify and file a 977 serial barcode as a Magazine.

    The carrier EAN identifies the serial publication by ISSN.  It does not
    reliably identify the exact physical issue, so Google Books contributes
    publication-stable metadata only.  UPC Item DB remains a fallback for a
    useful retail title when Google Books has no exact ISSN record.
    """
    upc_key = serial.ean13

    with get_db() as db:
        existing = db.execute(
            "SELECT id, title, media_type FROM items WHERE upc = ?", (upc_key,)
        ).fetchone()
    if existing:
        items_common._log_scan(
            upc_norm, existing["media_type"], "duplicate", existing["id"], mode
        )
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {
                "status": "duplicate",
                "isbn": upc_norm,
                "title": existing["title"],
                "item_id": existing["id"],
            },
        )

    with get_db() as db:
        google_api_key = get_setting(db, "google_books_api_key") or None

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        google_result = await googlebooks.lookup_magazine_by_issn(
            serial.issn, client, api_key=google_api_key
        )
        metadata = google_result.payload if google_result.found else None
        source = "google" if metadata else None
        fallback_result = None

        if metadata is None:
            fallback_result = await upcitemdb.lookup(upc_norm, client)
            if fallback_result.found:
                product = fallback_result.payload or {}
                title = upcitemdb.clean_title(product.get("title") or "")
                if title:
                    metadata = {
                        "title": title,
                        "publisher": product.get("brand"),
                        "description": None,
                        "series_name": title,
                    }
                    source = "upc"

    if metadata is None:
        # A genuine network failure is not an absent magazine.  Either source
        # being offline matters when the other source had no usable record.
        if google_result.outcome == "transport_failed" or (
            fallback_result is not None
            and fallback_result.outcome == "transport_failed"
        ):
            failed = (
                google_result
                if google_result.outcome == "transport_failed"
                else fallback_result
            )
            return items_common._upc_lookup_error(
                request, templates, upc_norm, "magazine", mode, failed
            )

        outcome = google_result
        if google_result.outcome == "no_match" and fallback_result is not None:
            outcome = fallback_result

        items_common._log_scan(upc_norm, "magazine", "not_found", mode=mode)
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {
                "status": "not_found",
                "isbn": upc_norm,
                "media_type": "magazine",
                "message": (
                    f"Magazine ISSN {serial.issn} not found — add manually below"
                ),
                "preview_cover": None,
                "enrich_status": scan_outcome.not_found_status(outcome),
                "enrich_provider": scan_outcome.provider_label(outcome),
                "locations": items_common._manual_form_locations(),
            },
        )

    loc_id = location_id if location_id and location_id > 0 else None
    existing = None
    item_id = None
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = items_common._find_upc_row(db, upc_key, "magazine")
        if existing is None:
            try:
                item_id = insert_item(
                    db,
                    title=metadata["title"],
                    description=metadata.get("description"),
                    media_type="magazine",
                    publisher=metadata.get("publisher"),
                    series_name=metadata.get("series_name") or metadata["title"],
                    location_id=loc_id,
                    upc=upc_key,
                    source=source,
                    language=metadata.get("language"),
                    owned=0 if mode == "wishlist" else 1,
                )
            except sqlite3.IntegrityError:
                existing = items_common._find_upc_row(db, upc_key, "magazine")
                if existing is None:
                    raise

    if existing:
        items_common._log_scan(
            upc_norm, "magazine", "duplicate", existing["id"], mode
        )
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {
                "status": "duplicate",
                "isbn": upc_norm,
                "title": existing["title"],
                "item_id": existing["id"],
            },
        )

    status = "wishlisted" if mode == "wishlist" else "added"
    items_common._log_scan(upc_norm, "magazine", status, item_id, mode)
    return templates.TemplateResponse(
        request,
        "fragments/scan_result.html",
        {
            "status": status,
            "isbn": upc_norm,
            "title": metadata["title"],
            "authors": None,
            "cover_path": None,
            "item_id": item_id,
            "source": source,
            "media_type_label": MEDIA_TYPES["magazine"],
            "detect_reason": (
                f"977 serial barcode → ISSN {serial.issn} — filed as Magazine."
            ),
            "detect_overrode": media_type_hint != "magazine",
            "enrich_status": None,
            "enrich_provider": (
                "Google Books" if source == "google" else "UPC Item DB"
            ),
        },
    )
