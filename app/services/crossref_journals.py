"""Exact serial-publication lookup through the public Crossref REST API.

Crossref is primarily useful for scholarly journals and other serials whose
publishers register DOI metadata. It complements, rather than replaces, the
ISSN Portal: Shelf asks authoritative/broad serial sources before falling back
to a retail barcode database.
"""

import logging
import re
from urllib.parse import quote

import httpx

from app.services import outbound, provider_result

logger = logging.getLogger(__name__)
RESOURCE_URL = "https://api.crossref.org/journals/{issn}"
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


def _first_text(value) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, list):
        for item in value:
            text = _first_text(item)
            if text:
                return text
    return None


def _record(data, canonical: str) -> dict | None:
    if not isinstance(data, dict):
        return None
    message = data.get("message")
    if not isinstance(message, dict):
        return None
    returned = message.get("ISSN") or message.get("issn") or []
    if isinstance(returned, str):
        returned = [returned]
    if returned and not any(_normalise_issn(v) == _normalise_issn(canonical) for v in returned):
        return None
    title = _first_text(message.get("title"))
    if not title:
        return None
    publisher = _first_text(message.get("publisher"))
    return {
        "title": title,
        "publisher": publisher,
        "description": None,
        "issn": canonical,
        "series_name": title,
        "language": None,
    }


async def lookup(issn: str, client: httpx.AsyncClient) -> provider_result.ProviderResult:
    canonical = _canonical_issn(issn)
    if canonical is None:
        return provider_result.no_match("crossref")
    try:
        resp = await outbound.fetch(
            client,
            "GET",
            RESOURCE_URL.format(issn=quote(canonical, safe="")),
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            follow_redirects=True,
        )
    except Exception:
        logger.debug("Crossref journal lookup failed for ISSN %s", canonical, exc_info=True)
        return provider_result.transport_failed("crossref")
    classified = provider_result.classify_response("crossref", resp)
    if classified is not None:
        return classified
    try:
        metadata = _record(resp.json(), canonical)
    except Exception:
        logger.debug("Crossref returned malformed JSON for ISSN %s", canonical, exc_info=True)
        return provider_result.no_match("crossref", status=resp.status_code)
    if metadata is None:
        return provider_result.no_match("crossref", status=resp.status_code)
    return provider_result.found("crossref", metadata, status=resp.status_code)
