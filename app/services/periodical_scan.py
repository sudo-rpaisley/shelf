"""Resolve periodical barcodes into publication-level scan candidates.

A 977 carrier identifies the serial publication through its ISSN. It does not
universally identify a concrete issue. Shelf therefore resolves the publication
first, preserves the carrier/add-on exactly, and leaves issue number/date for a
confirmation step rather than guessing publisher-specific semantics.
"""

from __future__ import annotations

import re

import httpx

from app.services import (
    crossref_journals, googlebooks, issn_portal, periodical_records,
    periodicals, provider_result, upcitemdb,
)


_GENERIC_PERIODICAL_TITLES = frozenset({
    "magazine", "magazines", "periodical", "periodicals", "journal", "journals",
    "newspaper", "newspapers", "publication", "publications", "serial", "serials",
})


def _usable_publication_title(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    title = re.sub(r"\s+", " ", value).strip(" -–—:;,.\t\r\n")
    return title if title and title.casefold() not in _GENERIC_PERIODICAL_TITLES else None


def _metadata_with_usable_title(result, issn: str) -> dict | None:
    if not result.found or not isinstance(result.payload, dict):
        return None
    metadata = result.payload
    try:
        matched_issn = periodicals.normalise_issn(metadata.get("issn"))
    except ValueError:
        return None
    title = _usable_publication_title(metadata.get("title"))
    if matched_issn != issn or not title:
        return None
    return {**metadata, "title": title, "issn": matched_issn}


def _retail_lookup_code(ean13: str) -> str:
    return ean13[1:] if len(ean13) == 13 and ean13.startswith("0") else ean13


def _scan_payload(serial: periodicals.PeriodicalBarcode, metadata: dict | None = None) -> dict:
    metadata = metadata or {}
    title = metadata.get("title")
    return {
        "media_type": "magazine",
        "title": title,
        "publication_title": title,
        "publisher": metadata.get("publisher"),
        "language": metadata.get("language"),
        "description": metadata.get("description"),
        "publication_issn": metadata.get("issn"),
        "issn": serial.issn,
        "barcode_ean": serial.ean13,
        "barcode_supplement": serial.supplement,
        "barcode_variant": serial.variant,
        "issue_number": None,
        "issue_date": None,
        "requires_issue_confirmation": True,
    }


async def resolve_serial_barcode(
    raw: str,
    client: httpx.AsyncClient,
) -> provider_result.ProviderResult:
    """Resolve native 977 scans through ISSN Portal only, never retail fallback.

    A failed or unusable exact-ISSN lookup still carries the printed identity
    for manual confirmation. Ordinary UPC/EAN periodicals belong to the legacy
    resolver, not this serial-only policy.
    """
    serial = periodicals.parse_barcode(raw)
    if serial is None or not serial.issn:
        return provider_result.no_match("periodical_barcode")

    result = await issn_portal.lookup(serial.issn, client)
    metadata = _metadata_with_usable_title(result, serial.issn)
    if metadata is not None:
        return result.with_payload(_scan_payload(serial, metadata))
    if result.found:
        result = provider_result.no_match(result.provider, status=result.status)
    return result.with_payload(_scan_payload(serial))


async def resolve_barcode(
    raw: str,
    client: httpx.AsyncClient,
    *,
    google_api_key: str | None = None,
) -> provider_result.ProviderResult:
    """Resolve a valid periodical barcode to its continuing publication.

    Invalid/non-periodical barcodes return ``no_match`` without making an
    outbound request. Provider failures retain the decoded barcode payload so
    the scan UI can still show the ISSN and let the user enter a publication
    title manually. A metadata hit adds the publication title/publisher but
    deliberately leaves issue identity blank.
    """
    serial = periodicals.parse_barcode(raw)
    if serial is None:
        return provider_result.no_match("periodical_barcode")

    attempts = []
    if serial.issn:
        # The same ladder serves native Scan and the legacy UPC issue form.
        # A title from a different serial is not evidence for this publication.
        for lookup, kwargs in (
            (googlebooks.lookup_magazine_by_issn, {"api_key": google_api_key}),
            (issn_portal.lookup, {}),
            (crossref_journals.lookup, {}),
        ):
            result = await lookup(serial.issn, client, **kwargs)
            metadata = _metadata_with_usable_title(result, serial.issn)
            if metadata is not None:
                return result.with_payload(_scan_payload(serial, metadata))
            attempts.append(provider_result.no_match(result.provider, status=result.status) if result.found else result)

    result = await upcitemdb.lookup(_retail_lookup_code(serial.ean13), client)
    if result.found and isinstance(result.payload, dict):
        product = result.payload
        raw_title = product.get("title")
        title = _usable_publication_title(upcitemdb.clean_title(raw_title)) if isinstance(raw_title, str) else None
        if title:
            return result.with_payload(_scan_payload(serial, {
                "title": title, "publisher": product.get("brand"),
            }))
    attempts.append(provider_result.no_match(result.provider, status=result.status) if result.found else result)
    return provider_result.combine(attempts, provider="periodical_barcode").with_payload(_scan_payload(serial))


def attach_issue_identity(
    db,
    *,
    item_id: int,
    candidate: dict,
    publication_title: str | None = None,
    issue_number: str | None = None,
    volume: str | None = None,
    issue_date: str | None = None,
    cover_date_label: str | None = None,
) -> int:
    """Persist a confirmed scan candidate against an existing Shelf item.

    The caller must supply/confirm the publication title if lookup did not find
    one. Barcode supplement/variant values are never promoted to issue number.
    Returns the publication id for follow-up navigation.
    """
    title = (publication_title or candidate.get("publication_title") or "").strip()
    if not title:
        raise ValueError("Periodical publication title is required")

    publication_id = periodical_records.upsert_publication(
        db,
        title=title,
        issn=candidate.get("issn"),
        publisher=candidate.get("publisher"),
        language=candidate.get("language"),
    )
    periodical_records.link_issue(
        db,
        item_id=item_id,
        publication_id=publication_id,
        volume=volume,
        issue_number=issue_number,
        issue_date=issue_date,
        barcode_ean=candidate.get("barcode_ean"),
        barcode_supplement=candidate.get("barcode_supplement"),
        cover_date_label=cover_date_label,
    )
    return publication_id
