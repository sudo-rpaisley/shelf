"""Barcode-first sleeve artwork lookup for Shelf music items."""

from __future__ import annotations

import httpx

from app.config import MUSIC_MEDIA_TYPES
from app.services import musicbrainz
from app.services import upc as upc_svc

_PHYSICAL_TYPES = {"vinyl", "cd", "cassette"}


def infer_media_type(release: dict) -> str:
    """Map a compact MusicBrainz release summary to a Shelf music type."""
    text = str(release.get("format_summary") or "").casefold()
    if "vinyl" in text or any(token in text for token in ('7"', '10"', '12"')):
        return "vinyl"
    if "cassette" in text:
        return "cassette"
    if "compact disc" in text or text.strip() == "cd" or " cd" in text:
        return "cd"
    if "digital" in text:
        return "digital_music"
    return "music_other"


def barcode_variants(barcode: str) -> list[str]:
    """Return the scanned value plus its UPC-A/EAN-13 equivalent."""
    scanned = upc_svc.normalize_barcode(barcode)
    canonical = upc_svc.normalize_upc(barcode)
    values = [scanned, canonical]
    if len(canonical) == 13 and canonical.startswith("0"):
        values.append(canonical[1:])
    return list(dict.fromkeys(value for value in values if value))


def _compatible(release: dict, preferred_media_type: str | None) -> bool:
    inferred = infer_media_type(release)
    if preferred_media_type in _PHYSICAL_TYPES:
        return inferred == preferred_media_type
    return inferred in MUSIC_MEDIA_TYPES


async def find_cover_url(
    barcode: str,
    media_type: str,
    client: httpx.AsyncClient,
) -> str | None:
    """Find front artwork for a barcode without guessing across formats.

    A vinyl item only accepts vinyl MusicBrainz candidates, and likewise for
    CD/cassette.  Ambiguous barcodes are fine for artwork: each compatible
    release is tried until Cover Art Archive has an image, but no exact release
    identity is persisted here.
    """
    releases: list[dict] = []
    for variant in barcode_variants(barcode):
        result = await musicbrainz.search_by_barcode(variant, client, limit=20)
        if result.found and result.payload:
            releases = [
                release for release in result.payload
                if isinstance(release, dict) and _compatible(release, media_type)
            ]
            if releases:
                break
        if result.outcome in {"rate_limited", "rejected", "transport_failed"}:
            return None

    for release in releases[:3]:
        release_id = release.get("musicbrainz_release_id")
        if not release_id:
            continue
        art = await musicbrainz.cover_art(release_id, client)
        if not art.found:
            continue
        candidates = art.payload or []
        front = next((candidate for candidate in candidates if candidate.get("front")), None)
        chosen = front or (candidates[0] if candidates else None)
        if chosen and chosen.get("url"):
            return chosen["url"]
    return None
