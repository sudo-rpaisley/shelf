"""Magazine and periodical-specific barcode scan flow.

A 977 barcode identifies a serial publication by ISSN. It does not, by itself,
reliably identify the exact issue, so this module stops after publication
identification and asks for issue identity before creating an item. If a USB
scanner supplies a 2/5-digit EAN add-on concatenated to the carrier, Shelf
preserves it as issue-discriminator data without guessing its meaning.

The lookup ladder deliberately favours serial-aware sources over generic retail
metadata. This covers consumer magazines, newspapers and other ISSN serials via
Google Books / ISSN Portal, scholarly journals via Crossref, then uses UPC Item
DB only as a last resort. Generic category labels such as ``Magazine`` are not
accepted as publication titles.
"""

import re

import httpx
from fastapi import Request

from app.config import HTTP_TIMEOUT
from app.database import get_db, get_setting
from app.routers import items_common
from app.services import (
    crossref_journals,
    googlebooks,
    issn_portal,
    periodicals,
    upcitemdb,
)


_GENERIC_PERIODICAL_TITLES = frozenset({
    "magazine",
    "magazines",
    "periodical",
    "periodicals",
    "journal",
    "journals",
    "newspaper",
    "newspapers",
    "publication",
    "publications",
    "serial",
    "serials",
})


def _usable_publication_title(value: str | None) -> str | None:
    title = re.sub(r"\s+", " ", (value or "")).strip(" -–—:;,.\t\r\n")
    if not title:
        return None
    if title.casefold() in _GENERIC_PERIODICAL_TITLES:
        return None
    return title


def _metadata_with_usable_title(result):
    if result is None or not result.found:
        return None
    payload = result.payload or {}
    title = _usable_publication_title(payload.get("title"))
    if not title:
        return None
    metadata = dict(payload)
    metadata["title"] = title
    return metadata


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
        google_result = await googlebooks.lookup_magazine_by_issn(
            serial.issn, client, api_key=google_api_key
        )
        metadata = _metadata_with_usable_title(google_result)
        issn_result = None
        crossref_result = None
        fallback_result = None

        if metadata is None:
            issn_result = await issn_portal.lookup(serial.issn, client)
            metadata = _metadata_with_usable_title(issn_result)

        if metadata is None:
            crossref_result = await crossref_journals.lookup(serial.issn, client)
            metadata = _metadata_with_usable_title(crossref_result)

        if metadata is None:
            fallback_result = await upcitemdb.lookup(serial.ean13, client)
            if fallback_result.found:
                product = fallback_result.payload or {}
                title = _usable_publication_title(
                    upcitemdb.clean_title(product.get("title") or "")
                )
                if title:
                    metadata = {
                        "title": title,
                        "publisher": product.get("brand"),
                        "description": None,
                        "language": None,
                    }

    if metadata is None:
        transport_failure = next(
            (
                result
                for result in (
                    google_result,
                    issn_result,
                    crossref_result,
                    fallback_result,
                )
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
