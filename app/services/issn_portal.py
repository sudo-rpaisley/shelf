"""Publication metadata lookup from ISSN linked-data endpoints.

The ISSN International Centre exposes essential ISSN record information as
linked open data. Shelf uses JSON-LD only to identify the serial publication
behind a 977 barcode; it does not scrape the human-facing portal and it does
not infer a concrete issue from publication-level data.
"""

import logging
import re

import httpx

from app.services import outbound, provider_result

logger = logging.getLogger(__name__)

# The ISSN service has moved between portal hosts during the 2026 portal
# transition. Ask the public Portal resource first, then the linked-data host
# used by existing JSON formatters. A 200 response that is not usable JSON-LD
# is treated as a miss for that host so the alternate can still answer.
RESOURCE_URLS = (
    "https://portal.issn.org/resource/ISSN/{issn}",
    "https://portal-plus.issn.org/resource/ISSN/{issn}",
)
_USER_AGENT = "Shelf/1.0 (+https://github.com/sudo-rpaisley/shelf)"


def _normalise_issn(value: str | None) -> str:
    return re.sub(r"[^0-9X]", "", (value or "").upper())


def _canonical_issn(value: str | None) -> str | None:
    normalised = _normalise_issn(value)
    if len(normalised) != 8 or not normalised[:7].isdigit():
        return None
    if not (normalised[-1].isdigit() or normalised[-1] == "X"):
        return None
    return f"{normalised[:4]}-{normalised[4:]}"


def _text(value) -> str | None:
    """Return a useful human-readable scalar from common JSON-LD shapes."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, list):
        for item in value:
            text = _text(item)
            if text:
                return text
        return None
    if isinstance(value, dict):
        for key in ("@value", "value", "name", "label"):
            text = _text(value.get(key))
            if text:
                return text
    return None


def _identifier_matches(node: dict, target: str) -> bool:
    # The linked-data profile has appeared both compacted and expanded. Accept
    # the compact property names and the schema.org IRIs used by expanded
    # JSON-LD so a profile/context change does not turn a valid record into a
    # miss.
    for field in (
        "identifier",
        "issn",
        "http://schema.org/identifier",
        "https://schema.org/identifier",
        "http://schema.org/issn",
        "https://schema.org/issn",
    ):
        identifiers = node.get(field)
        if not isinstance(identifiers, list):
            identifiers = [identifiers]
        for identifier in identifiers:
            text = _text(identifier)
            if text and _normalise_issn(text) == target:
                return True

    node_id = str(node.get("@id") or "")
    match = re.search(r"/ISSN/([0-9]{4}-?[0-9]{3}[0-9X])(?:$|[#/?])", node_id, re.I)
    return bool(match and _normalise_issn(match.group(1)) == target)


def _first_text(node: dict, *fields: str) -> str | None:
    for field in fields:
        text = _text(node.get(field))
        if text:
            return text
    return None


def _record_from_jsonld(data, canonical: str) -> dict | None:
    target = _normalise_issn(canonical)
    if isinstance(data, dict):
        graph = data.get("@graph")
        nodes = graph if isinstance(graph, list) else [data]
    elif isinstance(data, list):
        nodes = data
    else:
        return None

    for node in nodes:
        if not isinstance(node, dict) or not _identifier_matches(node, target):
            continue

        # Support both the compact ISSN profile and expanded Schema.org / DCT
        # forms. Essential open-data records do not all use the same compaction
        # context, but they do expose a publication title.
        title = _first_text(
            node,
            "mainTitle",
            "titleProper",
            "title",
            "name",
            "http://schema.org/name",
            "https://schema.org/name",
            "http://purl.org/dc/terms/title",
            "http://purl.org/dc/elements/1.1/title",
        )
        if not title:
            continue

        publisher = _first_text(
            node,
            "publisher",
            "http://schema.org/publisher",
            "https://schema.org/publisher",
            "http://purl.org/dc/terms/publisher",
        )
        return {
            "title": title,
            "publisher": publisher,
            "description": None,
            "issn": canonical,
            "series_name": title,
            "language": None,
        }
    return None


async def lookup(
    issn: str,
    client: httpx.AsyncClient,
) -> provider_result.ProviderResult:
    """Identify a serial publication by exact ISSN using ISSN JSON-LD."""
    canonical = _canonical_issn(issn)
    if canonical is None:
        return provider_result.no_match("issn_portal")

    attempts: list[provider_result.ProviderResult] = []
    for resource_url in RESOURCE_URLS:
        try:
            resp = await outbound.fetch(
                client,
                "GET",
                resource_url.format(issn=canonical),
                params={"format": "json"},
                headers={
                    "Accept": "application/ld+json, application/json;q=0.9",
                    "User-Agent": _USER_AGENT,
                },
                follow_redirects=True,
            )
        except Exception:
            logger.debug(
                "ISSN lookup transport failure for %s via %s",
                canonical,
                resource_url,
                exc_info=True,
            )
            attempts.append(provider_result.transport_failed("issn_portal"))
            continue

        classified = provider_result.classify_response("issn_portal", resp)
        if classified is not None:
            attempts.append(classified)
            continue

        try:
            metadata = _record_from_jsonld(resp.json(), canonical)
        except Exception:
            logger.debug(
                "ISSN lookup returned non-JSON or malformed JSON-LD for %s via %s",
                canonical,
                resource_url,
                exc_info=True,
            )
            attempts.append(
                provider_result.no_match("issn_portal", status=resp.status_code)
            )
            continue

        if metadata is not None:
            return provider_result.found(
                "issn_portal", metadata, status=resp.status_code
            )
        attempts.append(provider_result.no_match("issn_portal", status=resp.status_code))

    return provider_result.combine(attempts, provider="issn_portal")
