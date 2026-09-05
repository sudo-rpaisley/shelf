"""Magazine and periodical-specific barcode scan flow.

A 977 barcode carries an ISSN-shaped serial identifier. North American consumer
magazines may instead use an ordinary UPC-A carrier with a 2/5-digit add-on.
Neither form safely identifies human-readable issue semantics on its own, so
Shelf preserves the carrier/add-on and uses them as search hints rather than as
an application-local title mapping.

For ISSN-bearing carriers the automatic lookup ladder deliberately favours
serial-aware sources: Google Books, ISSN Portal and Crossref, then UPC Item DB.
When those sources cannot make a useful match, the issue form keeps the scanned
barcode context and offers an assisted title search. Generic category labels
such as ``Magazine`` are never accepted as publication titles.
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
    periodical_records,
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


def _known_issue(serial: periodicals.PeriodicalBarcode) -> dict | None:
    """Return an exact previously catalogued issue for carrier+supplement."""
    if not serial.supplement:
        return None
    with get_db() as db:
        periodical_records.ensure_extended_schema(db)
        row = db.execute(
            "SELECT i.id, i.title, i.authors, i.cover_path "
            "FROM periodical_issues pi JOIN items i ON i.id = pi.item_id "
            "WHERE pi.barcode_ean = ? AND pi.barcode_supplement = ? "
            "ORDER BY pi.item_id DESC LIMIT 1",
            (serial.ean13, serial.supplement),
        ).fetchone()
    return dict(row) if row else None


def _retail_lookup_code(ean13: str) -> str:
    """UPC Item DB is happiest with the native 12-digit UPC-A representation."""
    return ean13[1:] if len(ean13) == 13 and ean13.startswith("0") else ean13


async def scan_magazine(
    request: Request,
    templates,
    upc_norm: str,
    serial: periodicals.PeriodicalBarcode,
    media_type_hint: str,
    location_id: int | None,
    mode: str,
):
    """Identify a periodical publication, then request concrete issue details."""
    existing = _known_issue(serial)
    if existing is not None:
        items_common._log_scan(
            serial.full_code, "magazine", "duplicate", existing["id"], mode
        )
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {
                "status": "duplicate",
                "isbn": serial.full_code,
                "title": existing["title"],
                "authors": existing.get("authors"),
                "cover_path": existing.get("cover_path"),
                "item_id": existing["id"],
            },
        )

    with get_db() as db:
        google_api_key = get_setting(db, "google_books_api_key") or None

    # Do not infer a publication title from another local issue merely because
    # it used the same carrier. Historical periodical carriers can be ambiguous;
    # only an exact carrier+supplement duplicate is resolved locally above.
    metadata = None
    google_result = None
    issn_result = None
    crossref_result = None
    fallback_result = None

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        if serial.issn:
            google_result = await googlebooks.lookup_magazine_by_issn(
                serial.issn, client, api_key=google_api_key
            )
            metadata = _metadata_with_usable_title(google_result)

        if metadata is None and serial.issn:
            issn_result = await issn_portal.lookup(serial.issn, client)
            metadata = _metadata_with_usable_title(issn_result)

        if metadata is None and serial.issn:
            crossref_result = await crossref_journals.lookup(serial.issn, client)
            metadata = _metadata_with_usable_title(crossref_result)

        if metadata is None:
            fallback_result = await upcitemdb.lookup(
                _retail_lookup_code(serial.ean13), client
            )
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
                        "issn": None,
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
            "issn": None,
        }

    metadata_issn = periodical_records.normalise_issn(metadata.get("issn"))
    items_common._log_scan(serial.full_code, "magazine", "not_found", mode=mode)
    return templates.TemplateResponse(
        request,
        "fragments/magazine_issue_form.html",
        {
            "title": metadata.get("title") or "",
            "publisher": metadata.get("publisher"),
            "description": metadata.get("description"),
            "language": metadata.get("language"),
            # A publication ISSN is stored only when a provider supplied one.
            # The value encoded by a 977 carrier remains a search/display hint.
            "issn": metadata_issn or "",
            "issn_hint": serial.issn,
            "carrier_ean": serial.ean13,
            "supplement": serial.supplement,
            "mode": mode,
            "locations": items_common._manual_form_locations(),
            "selected_location_id": location_id if location_id and location_id > 0 else None,
            "detect_overrode": media_type_hint != "magazine",
        },
    )


def install_scan_dispatch() -> None:
    """Wrap the shared UPC handler so periodical carriers are dispatched here."""
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
