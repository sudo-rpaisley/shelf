"""Discogs database integration for exact physical music releases.

MusicBrainz remains Shelf's canonical music identity source. Discogs enriches
an already-catalogued exact release with collector-facing pressing identity and
identifiers such as matrix/runout, pressing-plant IDs and label codes.

Discogs database search requires authentication. Shelf supports a personal
access token from Settings or ``DISCOGS_TOKEN`` and sends an identifying
User-Agent on every request. Release artwork is deliberately not consumed here;
Shelf continues to use Cover Art Archive for music covers.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.services import outbound, provider_result

logger = logging.getLogger(__name__)

DISCOGS_BASE = "https://api.discogs.com"
USER_AGENT = "Shelf/1.0 +https://github.com/sudo-rpaisley/shelf"


def _headers(token: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Authorization": f"Discogs token={token.strip()}",
    }


def _display_name(value: Any) -> str | None:
    """Return a human artist name without Discogs' duplicate-name suffix."""
    if not isinstance(value, dict):
        return None
    text = str(value.get("anv") or value.get("name") or "").strip()
    if not text:
        return None
    # Discogs disambiguates same-named artists as "Name (2)". That suffix is
    # database identity, not part of the artist's display name.
    return re.sub(r"\s+\(\d+\)$", "", text).strip() or None


def _artist_credit(values: Any) -> str | None:
    if not isinstance(values, list):
        return None
    parts: list[str] = []
    for artist in values:
        if not isinstance(artist, dict):
            continue
        name = _display_name(artist)
        if not name:
            continue
        parts.append(name)
        join = str(artist.get("join") or "").strip()
        if join:
            parts.append(f" {join} ")
        elif artist is not values[-1]:
            parts.append(", ")
    text = "".join(parts).strip(" ,")
    return text or None


def _duration_ms(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parts = [int(part) for part in text.split(":")]
    except ValueError:
        return None
    if len(parts) == 2:
        minutes, seconds = parts
        return (minutes * 60 + seconds) * 1000
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return (hours * 3600 + minutes * 60 + seconds) * 1000
    return None


def _format_summary(formats: Any) -> str | None:
    if not isinstance(formats, list):
        return None
    rendered: list[str] = []
    for value in formats:
        if isinstance(value, str):
            if value.strip():
                rendered.append(value.strip())
            continue
        if not isinstance(value, dict):
            continue
        bits: list[str] = []
        name = str(value.get("name") or "").strip()
        if name:
            bits.append(name)
        for description in value.get("descriptions") or []:
            text = str(description or "").strip()
            if text and text not in bits:
                bits.append(text)
        text = str(value.get("text") or "").strip()
        if text and text not in bits:
            bits.append(text)
        qty = str(value.get("qty") or "").strip()
        if qty and qty != "1" and bits:
            bits[0] = f"{qty}× {bits[0]}"
        if bits:
            rendered.append(" · ".join(bits))
    return "; ".join(rendered) or None


def _first_label(release: dict) -> tuple[str | None, str | None]:
    for entry in release.get("labels") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip() or None
        catno = str(entry.get("catno") or "").strip()
        if catno.casefold() in {"none", "n/a"}:
            catno = ""
        if name or catno:
            return name, catno or None
    return None, None


_IDENTIFIER_TYPES = {
    "barcode": "barcode",
    "matrix / runout": "matrix_runout",
    "matrix/runout": "matrix_runout",
    "pressing plant id": "pressing_plant_id",
    "label code": "label_code",
    "rights society": "rights_society",
    "price code": "price_code",
    "sparscode": "spars_code",
    "spars code": "spars_code",
    "depuesto legal": "legal_deposit",
    "depósito legal": "legal_deposit",
    "other": "other",
}


def _identifier_type(value: Any) -> str:
    text = str(value or "other").strip()
    mapped = _IDENTIFIER_TYPES.get(text.casefold())
    if mapped:
        return mapped
    slug = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")
    return slug or "other"


def _discogs_url(release_id: int | str | None, uri: Any = None) -> str | None:
    path = str(uri or "").strip()
    if path.startswith("/"):
        return f"https://www.discogs.com{path}"
    if release_id:
        return f"https://www.discogs.com/release/{release_id}"
    return None


def normalise_release(release: dict) -> dict:
    """Convert one exact Discogs Release payload into Shelf enrichment data."""
    release_id = release.get("id")
    label, catalog_number = _first_label(release)
    identifiers: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for identifier in release.get("identifiers") or []:
        if not isinstance(identifier, dict):
            continue
        value = str(identifier.get("value") or "").strip()
        if not value:
            continue
        identifier_type = _identifier_type(identifier.get("type"))
        description = str(identifier.get("description") or "").strip()
        key = (identifier_type, value.casefold(), description.casefold())
        if key in seen:
            continue
        seen.add(key)
        identifiers.append(
            {
                "identifier_type": identifier_type,
                "value": value,
                "description": description or None,
            }
        )

    tracklist: list[dict] = []
    for index, track in enumerate(release.get("tracklist") or [], start=1):
        if not isinstance(track, dict) or track.get("type_") not in (None, "track"):
            continue
        title = str(track.get("title") or "").strip()
        if not title:
            continue
        tracklist.append(
            {
                "position": index,
                "number": str(track.get("position") or index),
                "title": title,
                "artist_credit": _artist_credit(track.get("artists")),
                "duration_ms": _duration_ms(track.get("duration")),
            }
        )

    master_id = release.get("master_id") or None
    try:
        master_id = int(master_id) if master_id else None
    except (TypeError, ValueError):
        master_id = None
    try:
        release_id = int(release_id) if release_id else None
    except (TypeError, ValueError):
        release_id = None

    released = str(release.get("released") or "").strip()
    if not released and release.get("year"):
        released = str(release.get("year"))

    return {
        "title": release.get("title") or "Untitled",
        "artist_credit": _artist_credit(release.get("artists"))
        or str(release.get("artists_sort") or "").strip()
        or None,
        "discogs_release_id": release_id,
        "discogs_master_id": master_id,
        "release_date": released or None,
        "country": release.get("country"),
        "label": label,
        "catalog_number": catalog_number,
        "format_summary": _format_summary(release.get("formats")),
        "genres": [str(value) for value in release.get("genres") or [] if value],
        "styles": [str(value) for value in release.get("styles") or [] if value],
        "notes": str(release.get("notes") or "").strip() or None,
        "identifiers": identifiers,
        "tracks": tracklist,
        "discogs_url": _discogs_url(release_id, release.get("uri")),
        "source": "discogs",
    }


def _search_summary(result: dict) -> dict:
    release_id = result.get("id")
    master_id = result.get("master_id") or None
    try:
        release_id = int(release_id) if release_id else None
    except (TypeError, ValueError):
        release_id = None
    try:
        master_id = int(master_id) if master_id else None
    except (TypeError, ValueError):
        master_id = None
    labels = result.get("label") or []
    if isinstance(labels, str):
        labels = [labels]
    barcodes = result.get("barcode") or []
    if isinstance(barcodes, str):
        barcodes = [barcodes]
    return {
        "discogs_release_id": release_id,
        "discogs_master_id": master_id,
        "title": result.get("title") or "Untitled",
        "release_date": str(result.get("year") or "").strip() or None,
        "country": result.get("country"),
        "label": ", ".join(str(value) for value in labels if value) or None,
        "catalog_number": str(result.get("catno") or "").strip() or None,
        "barcodes": [str(value) for value in barcodes if value],
        "format_summary": _format_summary(result.get("format")),
        "discogs_url": _discogs_url(release_id, result.get("uri")),
    }


async def _request_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    token: str,
    params: dict[str, str] | None = None,
) -> provider_result.ProviderResult:
    token = (token or "").strip()
    if not token:
        return provider_result.no_credential("discogs")
    try:
        response = await outbound.fetch(
            client,
            "GET",
            url,
            params=params,
            headers=_headers(token),
            timeout=15,
        )
    except (httpx.ConnectError, httpx.TimeoutException):
        return provider_result.transport_failed("discogs")
    except Exception:
        logger.debug("Discogs request failed", exc_info=True)
        return provider_result.transport_failed("discogs")

    classified = provider_result.classify_response(
        "discogs", response, auth_statuses=(401, 403)
    )
    if classified is not None:
        return classified
    try:
        return provider_result.found(
            "discogs", response.json(), status=response.status_code
        )
    except ValueError:
        return provider_result.transport_failed("discogs")


async def search_releases(
    query: str,
    client: httpx.AsyncClient,
    *,
    token: str,
    artist: str | None = None,
    barcode: str | None = None,
    catalog_number: str | None = None,
    limit: int = 20,
) -> provider_result.ProviderResult:
    """Search exact Discogs Release rows, never Master Releases."""
    params: dict[str, str] = {
        "type": "release",
        "per_page": str(max(1, min(limit, 100))),
        "page": "1",
    }
    if query and query.strip():
        params["release_title"] = query.strip()
    if artist and artist.strip():
        params["artist"] = artist.strip()
    if barcode and barcode.strip():
        params["barcode"] = barcode.strip()
    if catalog_number and catalog_number.strip():
        params["catno"] = catalog_number.strip()
    if len(params) == 3:
        return provider_result.no_match("discogs")

    result = await _request_json(
        client,
        f"{DISCOGS_BASE}/database/search",
        token=token,
        params=params,
    )
    if not result.found:
        return result
    payload = result.payload if isinstance(result.payload, dict) else {}
    rows = payload.get("results") or []
    summaries = [
        _search_summary(row)
        for row in rows
        if isinstance(row, dict) and row.get("type") in (None, "release")
    ]
    return result.with_payload(summaries)


async def lookup_release(
    release_id: int | str,
    client: httpx.AsyncClient,
    *,
    token: str,
) -> provider_result.ProviderResult:
    try:
        release_id = int(str(release_id).strip())
    except (TypeError, ValueError):
        return provider_result.no_match("discogs")
    if release_id <= 0:
        return provider_result.no_match("discogs")
    result = await _request_json(
        client,
        f"{DISCOGS_BASE}/releases/{release_id}",
        token=token,
    )
    if not result.found:
        return result
    if not isinstance(result.payload, dict) or not result.payload.get("id"):
        return provider_result.no_match("discogs")
    return result.with_payload(normalise_release(result.payload))


async def test_connection(
    token: str, client: httpx.AsyncClient
) -> provider_result.ProviderResult:
    """Validate a token against Discogs' authenticated identity endpoint."""
    result = await _request_json(
        client,
        f"{DISCOGS_BASE}/oauth/identity",
        token=token,
    )
    if not result.found:
        return result
    payload = result.payload if isinstance(result.payload, dict) else {}
    username = str(payload.get("username") or "").strip()
    if not username:
        return provider_result.no_match("discogs", status=result.status)
    return result.with_payload({"username": username})
