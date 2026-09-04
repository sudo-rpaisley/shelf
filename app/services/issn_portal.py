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

# ISSN documents the canonical linked-data resource at issn.org. During the
# 2026 portal transition the same records have also been served from the public
# portal and portal-plus hosts, so keep those as fallbacks. A 200 response that
# is not usable JSON-LD is treated as a miss for that host so another endpoint
# can still answer.
RESOURCE_URLS = (
    "https://issn.org/resource/ISSN/{issn}",
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
    # Schema.org, Dublin Core and BIBO forms as well as the compact properties.
    for field in (
        "identifier",
        "issn",
        "http://schema.org/identifier",
        "https://schema.org/identifier",
        "http://schema.org/issn",
        "https://schema.org/issn",
        "http://purl.org/dc/elements/1.1/identifier",
        "http://purl.org/ontology/bibo/issn",
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


def _reference_ids(value) -> list[str]:
    """Return JSON-LD resource identifiers from a scalar/list reference value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        ref = value.get("@id")
        return [ref] if isinstance(ref, str) and ref else []
    if isinstance(value, list):
        refs: list[str] = []
        for item in value:
            refs.extend(_reference_ids(item))
        return refs
    return []


def _looks_like_reference(value: str) -> bool:
    lowered = value.casefold()
    return (
        lowered.startswith(("http://", "https://", "resource/", "organization/", "#"))
        or "#keytitle" in lowered
        or "/resource/" in lowered
    )


def _title_from_node(node: dict, by_id: dict[str, dict]) -> str | None:
    # Direct literals used by compact and expanded ISSN profiles. Bibframe's
    # full mainTitle IRI matters when the JSON-LD response is expanded.
    title = _first_text(
        node,
        "mainTitle",
        "http://id.loc.gov/ontologies/bibframe/mainTitle",
        "titleProper",
        "name",
        "http://schema.org/name",
        "https://schema.org/name",
        "http://purl.org/dc/terms/title",
        "http://purl.org/dc/elements/1.1/title",
    )
    if title:
        return title

    # In some ISSN JSON-LD records bf:title is a link to a KeyTitle resource
    # whose human-readable value lives in rdf:value. Follow that link instead
    # of accidentally displaying the resource URI as the publication title.
    for field in ("title", "http://id.loc.gov/ontologies/bibframe/title"):
        raw = node.get(field)
        for ref in _reference_ids(raw):
            linked = by_id.get(ref)
            if linked:
                linked_title = _first_text(
                    linked,
                    "@value",
                    "value",
                    "http://www.w3.org/1999/02/22-rdf-syntax-ns#value",
                    "name",
                    "http://schema.org/name",
                    "https://schema.org/name",
                )
                if linked_title:
                    return linked_title

        literal = _text(raw)
        if literal and not _looks_like_reference(literal):
            return literal

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

    by_id = {
        str(node.get("@id")): node
        for node in nodes
        if isinstance(node, dict) and node.get("@id")
    }

    for node in nodes:
        if not isinstance(node, dict) or not _identifier_matches(node, target):
            continue

        title = _title_from_node(node, by_id)
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
