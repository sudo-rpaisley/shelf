"""Book metadata cascade shared by HTTP and service callers.

Provider modules stay module-bound: late patches to their public lookup APIs
reach this cascade, without any service importing a router.
"""

import httpx

from app.services import googlebooks, hardcover, national, openlibrary, provider_result


async def lookup_metadata(isbn13: str, hc_token: str | None, client: httpx.AsyncClient,
                          *, google_api_key: str | None = None
                          ) -> tuple[dict | None, str, dict, provider_result.ProviderResult]:
    """Return metadata, source, Hardcover IDs and the outcomes of executed legs."""
    metadata = None
    source = "manual"
    legs: list[provider_result.ProviderResult] = []

    # Consult an authoritative national source before the general cascade.
    national_result: provider_result.ProviderResult | None = None
    provider = national.provider_for(isbn13)
    if provider:
        national_result = await provider.lookup(isbn13, client)
        legs.append(national_result)
        if national_result.found:
            metadata = national_result.payload
            source = national_result.provider

    if not metadata:
        ol_result = await openlibrary.lookup(isbn13, client)
        legs.append(ol_result)
        if ol_result.found:
            metadata = ol_result.payload
            source = "openlibrary"

    hc_ids = {}
    if not metadata and hc_token:
        hc_result = await hardcover.lookup_by_isbn(isbn13, client, token=hc_token)
        legs.append(hc_result)
        if hc_result.found:
            metadata = hc_result.payload
            source = "hardcover"
            hc_ids = {
                "hardcover_book_id": metadata.get("hardcover_book_id"),
                "hardcover_edition_id": metadata.get("hardcover_edition_id"),
            }

    if not metadata:
        gb_result = await googlebooks.lookup(isbn13, client, api_key=google_api_key)
        legs.append(gb_result)
        if gb_result.found:
            metadata = gb_result.payload
            source = "google"

    # Enrich only missing series/description, retaining primary-source metadata.
    if metadata and hc_token and source != "hardcover":
        if not metadata.get("series_name") or not metadata.get("description"):
            hc_enrich_result = await hardcover.lookup_by_isbn(isbn13, client, token=hc_token)
            legs.append(hc_enrich_result)
            if hc_enrich_result.found:
                hc_data = hc_enrich_result.payload
                if hc_data.get("series_name") and not metadata.get("series_name"):
                    metadata["series_name"] = hc_data["series_name"]
                    metadata["series_position"] = hc_data.get("series_position")
                if hc_data.get("description") and not metadata.get("description"):
                    metadata["description"] = hc_data["description"]
                hc_ids = {
                    "hardcover_book_id": hc_data.get("hardcover_book_id"),
                    "hardcover_edition_id": hc_data.get("hardcover_edition_id"),
                    "cover_url": hc_data.get("cover_url"),
                }

    return metadata, source, hc_ids, provider_result.combine(legs, provider="isbn-cascade")
