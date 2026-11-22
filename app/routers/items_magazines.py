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

import httpx
from fastapi import Request

from app.config import HTTP_TIMEOUT
from app.database import get_db, get_setting
from app.routers import items_common
from app.services import periodical_records, periodical_scan, periodicals


def _known_issue(serial: periodicals.PeriodicalBarcode) -> dict | None:
    """Return an exact previously catalogued issue for carrier+supplement."""
    if not serial.supplement:
        return None
    with get_db() as db:
        periodical_records.ensure_extended_schema(db)
        row = db.execute(
            "SELECT i.id, i.title, i.authors, i.cover_path "
            "FROM periodical_issues pi JOIN items_live i ON i.id = pi.item_id "
            "WHERE pi.barcode_ean = ? AND pi.barcode_supplement = ? "
            "ORDER BY pi.item_id DESC LIMIT 2",
            (serial.ean13, serial.supplement),
        ).fetchall()
    return dict(row[0]) if len(row) == 1 else None


async def scan_magazine(
    request: Request,
    templates,
    upc_norm: str,
    serial: periodicals.PeriodicalBarcode,
    media_type_hint: str,
    location_id: int | None,
    mode: str,
    *,
    library_id: int | None = None,
    tags: str = "",
    reading_status: str = "",
    location_node_id: int | None = None,
    shelf_fill_origin: str = "",
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
                "library_id": library_id, "tags": tags, "reading_status": reading_status,
                "location_node_id": location_node_id, "shelf_fill_origin": shelf_fill_origin,
            },
        )

    with get_db() as db:
        google_api_key = get_setting(db, "google_books_api_key") or None

    # Do not infer a publication title from another local issue merely because
    # it used the same carrier. Historical periodical carriers can be ambiguous;
    # only an exact carrier+supplement duplicate is resolved locally above.
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await periodical_scan.resolve_barcode(
            serial.full_code, client, google_api_key=google_api_key,
        )
    if result.outcome == "transport_failed":
        return items_common._upc_lookup_error(request, templates, serial.full_code, "magazine", mode, result)
    metadata = result.payload or {}
    metadata_issn = metadata.get("publication_issn")
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
            "library_id": library_id,
            "selected_library_id": library_id,
            "tags": tags,
            "reading_status": reading_status,
            "location_node_id": location_node_id,
            "shelf_fill_origin": shelf_fill_origin,
        },
    )
