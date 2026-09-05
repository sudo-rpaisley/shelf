"""Exact magazine-issue search/lookup using Google Books volumes."""

import logging
import re

import httpx

from app.services import googlebooks, outbound, provider_result

logger = logging.getLogger(__name__)
_VOLUME_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _issue_from_item(item: dict) -> dict | None:
    info = item.get("volumeInfo", {})
    if str(info.get("printType", "")).upper() != "MAGAZINE":
        return None
    title = (info.get("title") or "").strip()
    volume_id = str(item.get("id") or "").strip()
    if not title or not _VOLUME_ID_RE.fullmatch(volume_id):
        return None

    issn = None
    for ident in info.get("industryIdentifiers", []):
        if str(ident.get("type", "")).upper() == "ISSN":
            issn = ident.get("identifier") or None
            break

    published = str(info.get("publishedDate") or "").strip() or None
    year = None
    if published:
        match = re.search(r"(\d{4})", published)
        if match:
            year = int(match.group(1))

    cover_url = None
    images = info.get("imageLinks", {})
    for key in ("large", "medium", "thumbnail", "smallThumbnail"):
        if images.get(key):
            cover_url = images[key].replace("http://", "https://")
            break

    language = None
    if info.get("language"):
        from app.services.national import to_iso639_1
        language = to_iso639_1(info["language"])

    return {
        "google_volume_id": volume_id,
        "title": title,
        "publisher": info.get("publisher"),
        "description": info.get("description"),
        "issn": issn,
        "issue_date": published,
        "publish_year": year,
        "page_count": info.get("pageCount"),
        "cover_url": cover_url,
        "language": language,
    }


async def search_issues(
    query: str,
    client: httpx.AsyncClient,
    *,
    api_key: str | None = None,
    limit: int = 10,
) -> provider_result.ProviderResult:
    """Search Google Books magazine volumes by publication title."""
    value = (query or "").strip()
    if not value:
        return provider_result.no_match("google")
    try:
        resp = await outbound.fetch(
            client,
            "GET",
            googlebooks.VOLUMES_URL,
            params={
                # A bare q is a full-text search and returned unrelated scanned
                # magazines merely containing words such as "VW" and
                # "motoring". Manual magazine search is intentionally title-
                # scoped so the candidate list is useful for confirmation.
                "q": f'intitle:"{value}"',
                "printType": "magazines",
                "maxResults": str(max(1, min(limit, 20))),
                "orderBy": "relevance",
            },
            headers=googlebooks._api_headers(api_key),
        )
    except Exception:
        logger.debug("Google Books magazine title search failed", exc_info=True)
        return provider_result.transport_failed("google")

    auth_statuses = (
        googlebooks._AUTH_STATUSES if googlebooks._api_headers(api_key) else ()
    )
    classified = provider_result.classify_response(
        "google", resp, auth_statuses=auth_statuses
    )
    if classified is not None:
        return classified

    try:
        results = []
        for item in resp.json().get("items", []):
            issue = _issue_from_item(item)
            if issue:
                results.append(issue)
        if not results:
            return provider_result.no_match("google", status=resp.status_code)
        return provider_result.found("google", results, status=resp.status_code)
    except Exception:
        logger.debug("Malformed Google Books magazine search response", exc_info=True)
        return provider_result.no_match("google", status=resp.status_code)


async def lookup_issue(
    volume_id: str,
    client: httpx.AsyncClient,
    *,
    api_key: str | None = None,
) -> provider_result.ProviderResult:
    """Fetch one exact magazine volume selected from title search."""
    volume_id = (volume_id or "").strip()
    if not _VOLUME_ID_RE.fullmatch(volume_id):
        return provider_result.no_match("google")
    try:
        resp = await outbound.fetch(
            client,
            "GET",
            f"{googlebooks.VOLUMES_URL}/{volume_id}",
            headers=googlebooks._api_headers(api_key),
        )
    except Exception:
        logger.debug("Google Books magazine issue lookup failed", exc_info=True)
        return provider_result.transport_failed("google")

    auth_statuses = (
        googlebooks._AUTH_STATUSES if googlebooks._api_headers(api_key) else ()
    )
    classified = provider_result.classify_response(
        "google", resp, auth_statuses=auth_statuses
    )
    if classified is not None:
        return classified
    try:
        issue = _issue_from_item(resp.json())
        if not issue:
            return provider_result.no_match("google", status=resp.status_code)
        return provider_result.found("google", issue, status=resp.status_code)
    except Exception:
        logger.debug("Malformed Google Books magazine issue response", exc_info=True)
        return provider_result.no_match("google", status=resp.status_code)
