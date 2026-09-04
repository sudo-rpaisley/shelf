"""Magazine-specific barcode scan flow.

A 977 barcode identifies a serial publication by ISSN. It does not, by itself,
reliably identify the exact issue, so this module stops after publication
identification and asks for issue identity before creating an item. If a USB
scanner supplies a 2/5-digit EAN add-on concatenated to the carrier, Shelf
preserves it as issue-discriminator data without guessing its meaning.
"""

import httpx
from fastapi import Request

from app.config import HTTP_TIMEOUT
from app.database import get_db, get_setting
from app.routers import items_common
from app.services import googlebooks, issn_portal, periodicals, upcitemdb


async def scan_magazine(
    request: Request,
    templates,
    upc_norm: str,
    serial: periodicals.PeriodicalBarcode,
    media_type_hint: str,
    location_id: int | None,
    mode: str,
):
    """Identify a 977 serial publication, then request concrete issue details."""
    with get_db() as db:
        google_api_key = get_setting(db, "google_books_api_key") or None

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        # Google Books is cheap and can provide richer publication metadata for
        # magazines it has digitised. Older/niche magazines are often absent,
        # so an exact ISSN miss falls through to the ISSN International
        # Centre's linked-open-data record before the generic retail database.
        google_result = await googlebooks.lookup_magazine_by_issn(
            serial.issn, client, api_key=google_api_key
        )
        metadata = google_result.payload if google_result.found else None
        issn_result = None
        fallback_result = None

        if metadata is None:
            issn_result = await issn_portal.lookup(serial.issn, client)
            if issn_result.found:
                metadata = issn_result.payload

        if metadata is None:
            fallback_result = await upcitemdb.lookup(serial.ean13, client)
            if fallback_result.found:
                product = fallback_result.payload or {}
                title = upcitemdb.clean_title(product.get("title") or "")
                if title:
                    metadata = {
                        "title": title,
                        "publisher": product.get("brand"),
                        "description": None,
                        "language": None,
                    }

    if metadata is None:
        # If every identification source missed, a transport failure is not a
        # catalogue miss. Prefer the first failed leg so the scan card remains
        # actionable instead of silently showing an empty publication title.
        transport_failure = next(
            (
                result
                for result in (google_result, issn_result, fallback_result)
                if result is not None and result.outcome == "transport_failed"
            ),
            None,
        )
        if transport_failure is not None:
            return items_common._upc_lookup_error(
                request,
                templates,
                serial.full_code,
                "magazine",
                mode,
                transport_failure,
            )
        metadata = {
            "title": "",
            "publisher": None,
            "description": None,
            "language": None,
        }

    items_common._log_scan(serial.full_code, "magazine", "not_found", mode=mode)
    return templates.TemplateResponse(
        request,
        "fragments/magazine_issue_form.html",
        {
            "title": metadata.get("title") or "",
            "publisher": metadata.get("publisher"),
            "description": metadata.get("description"),
            "language": metadata.get("language"),
            "issn": serial.issn,
            "carrier_ean": serial.ean13,
            "supplement": serial.supplement,
            "mode": mode,
            "locations": items_common._manual_form_locations(),
            "selected_location_id": location_id if location_id and location_id > 0 else None,
            "detect_overrode": media_type_hint != "magazine",
        },
    )


def install_scan_dispatch() -> None:
    """Wrap the shared UPC handler once so 977 serials are dispatched here."""
    current = items_common._scan_upc
    if getattr(current, "_shelf_magazine_dispatch", False):
        return

    async def dispatch(
        request: Request,
        templates,
        upc_code: str,
        media_type: str,
        location_id: int | None,
        platform: str | None = None,
        mode: str = "add",
    ):
        serial = periodicals.parse_barcode(upc_code)
        if serial is not None:
            return await scan_magazine(
                request,
                templates,
                serial.full_code,
                serial,
                media_type,
                location_id,
                mode,
            )
        return await current(
            request,
            templates,
            upc_code,
            media_type,
            location_id,
            platform,
            mode,
        )

    dispatch._shelf_magazine_dispatch = True
    items_common._scan_upc = dispatch
