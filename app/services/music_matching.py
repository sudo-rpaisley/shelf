"""Explicit music matching: identifiers first, explained candidates, no writes."""

import asyncio
from dataclasses import dataclass
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.config import MUSIC_MEDIA_TYPES
from app.services import discogs, musicbrainz, provider_result, upc


def release_id(value: str) -> str | None:
    """Accept an MBID or an exact MusicBrainz release URL, never a group URL."""
    text = (value or "").strip()
    if "://" in text:
        try:
            parsed = urlsplit(text)
            if (parsed.scheme != "https" or parsed.hostname not in {"musicbrainz.org", "www.musicbrainz.org"}
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
                return None
            match = re.fullmatch(r"/release/([a-fA-F0-9-]{36})/?", parsed.path)
            text = match[1] if match else ""
        except ValueError:
            return None
    try:
        return str(UUID(text)) if len(text) == 36 else None
    except (ValueError, AttributeError):
        return None


def media_type(release: dict) -> str | None:
    formats = " ".join(str(m.get("format") or "") for m in release.get("media") or [] if isinstance(m, dict))
    formats = (formats or str(release.get("format_summary") or "")).casefold()
    for word, kind in (("vinyl", "vinyl"), ("cassette", "cassette"), ("cd", "cd"), ("digital", "digital_music")):
        if word in formats:
            return kind
    return None


def _text(value):
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in text if not unicodedata.combining(c)).split())


def _barcode(value):
    text = upc.normalize_barcode(str(value or ""))
    return text.lstrip("0") if text.isdigit() and len(text) in (12, 13) else ""


def rank(releases, *, title="", artist="", barcode="", catalog_number="", format="", country=""):
    ranked = []
    for release in releases[:100]:
        candidate = dict(release)
        score, reasons, conflicts = 0, [], []
        tests = (
            ("barcode", _barcode(barcode), _barcode(candidate.get("barcode")), 200),
            ("catalog_number", _text(catalog_number).replace(" ", ""), _text(candidate.get("catalog_number")).replace(" ", ""), 60),
            ("title", _text(title), _text(candidate.get("title")), 30),
            ("artist", _text(artist), _text(candidate.get("artist_credit")), 30),
            ("format", format, media_type(candidate), 25),
            ("country", country.upper(), str(candidate.get("country") or "").upper(), 10),
        )
        for name, wanted, observed, weight in tests:
            if not wanted or not observed:
                continue
            if wanted == observed:
                score += weight
                reasons.append(name)
            elif name in {"title", "artist"}:
                score += SequenceMatcher(None, wanted, observed).ratio() * weight / 2
            elif name in {"barcode", "format", "country", "catalog_number"}:
                conflicts.append(name)
        candidate.update(match_reasons=reasons, match_conflicts=conflicts,
                         shelf_media_type=media_type(candidate), match_score=score)
        ranked.append(candidate)
    return sorted(ranked, key=lambda candidate: -candidate["match_score"])


async def search(client, *, title="", artist="", barcode="", catalog_number="", format="", country=""):
    """Do not AND uncertain local titles with exact barcode/catalogue matches.

    A bounded title fallback is marked as such by the per-candidate facts; a
    fallback is never automatically selected or represented as an exact match.
    """
    barcode = upc.normalize_barcode(barcode)[:32]
    options = {"media_type": format if format in MUSIC_MEDIA_TYPES else "", "country": country[:2], "limit": 30}
    results = []
    if barcode:
        codes = [upc.normalize_barcode(barcode)]
        if len(codes[0]) == 12:
            codes.append("0" + codes[0])
        elif len(codes[0]) == 13 and codes[0].startswith("0"):
            codes.append(codes[0][1:])
        for code in codes:
            answer = await musicbrainz.search_releases("", client, barcode=code, **options)
            results.append(answer)
            if not answer.found or answer.payload:
                break
    if catalog_number and (not results or all(result.found and not result.payload for result in results)):
        results.append(await musicbrainz.search_releases("", client, catalog_number=catalog_number, **options))
    if (not results or all(result.found and not result.payload for result in results)) and (title or artist):
        results.append(await musicbrainz.search_releases(title, client, artist=artist or None, **options))
    for result in results:
        if result.found and result.payload:
            return result.with_payload(rank(result.payload, title=title, artist=artist, barcode=barcode,
                                            catalog_number=catalog_number, format=format, country=country))
    return provider_result.combine([provider_result.no_match(r.provider) if r.found else r for r in results],
                                   provider="musicbrainz")


def _catalogue_summaries(result):
    if not result.found:
        return result
    if not isinstance(result.payload, list):
        return provider_result.transport_failed(result.provider)
    candidates = []
    for row in result.payload:
        if not isinstance(row, dict):
            return provider_result.transport_failed(result.provider)
        candidate = dict(row)
        if result.provider == "musicbrainz":
            mbid = candidate.get("musicbrainz_release_id")
            if (not isinstance(mbid, str) or not release_id(mbid)
                    or not isinstance(candidate.get("title"), str) or not candidate["title"].strip()):
                return provider_result.transport_failed("musicbrainz")
            candidate.update(provider="musicbrainz", provider_release_id=mbid,
                             source_url=f"https://musicbrainz.org/release/{mbid}")
        candidates.append(candidate)
    return result.with_payload(candidates)


def _catalogue_search_result(result):
    """An unsuccessful HTTP search is a provider failure, not an empty catalogue."""
    if result.status in (None, 200) or result.outcome not in {"found", "no_match"}:
        return result
    if result.status == 429:
        return provider_result.rate_limited(result.provider, status=result.status)
    if result.status in (400, 401, 403):
        return provider_result.rejected(result.provider, status=result.status)
    return provider_result.ProviderResult("transport_failed", result.provider, status=result.status)


@dataclass(frozen=True, slots=True)
class CatalogueSearchResult(provider_result.ProviderResult):
    """Read-only candidates plus each catalogue's independent, bounded outcome."""

    provider_results: tuple[provider_result.ProviderResult, ...] = ()


async def _catalogue_leg(provider, operation, *, options, limit):
    try:
        # Leave room for both sequential catalogues inside Add's 30-second
        # budget. A stalled first provider must not consume the second's turn.
        result = await asyncio.wait_for(operation, timeout=12)
        if not isinstance(result, provider_result.ProviderResult) or result.provider != provider:
            return provider_result.transport_failed(provider)
        result = _catalogue_summaries(_catalogue_search_result(result))
        if not result.found:
            return result
        candidates = result.payload[:limit]
        if provider == "discogs":
            candidates = [dict(row) for row in candidates]
            for row in candidates:
                identifier = discogs._public_release_id(row.get("discogs_release_id"))
                if not identifier or not isinstance(row.get("title"), str) or not row["title"].strip():
                    return provider_result.transport_failed(provider)
                row.update(provider=provider, provider_release_id=str(identifier),
                           source_url=f"https://www.discogs.com/release/{identifier}")
        if not candidates:
            return provider_result.no_match(provider, status=result.status)
        return result.with_payload(rank(candidates, **options))
    except (TimeoutError, httpx.HTTPError, TypeError, ValueError, AttributeError):
        # Provider/normalisation failures are not empty catalogues. Cancellation
        # is deliberately not caught, so the enclosing request remains bounded.
        return provider_result.transport_failed(provider)


def search_statuses(result):
    """Display-free provider facts, including failures beside another leg's hits."""
    return tuple({"provider": leg.provider, "outcome": leg.outcome,
                  "count": len(leg.payload) if leg.found else 0}
                 for leg in getattr(result, "provider_results", ()))


async def search_catalogue(client, *, title="", artist="", barcode="", catalog_number="", format="",
                           country="", discogs_token=""):
    """Search MusicBrainz then Discogs every time; never merge pressing IDs."""
    title, artist, catalog_number = title.strip(), artist.strip(), catalog_number.strip()
    barcode = upc.normalize_barcode(barcode)[:32]
    if (format and format not in MUSIC_MEDIA_TYPES) or not (title or artist or barcode or catalog_number):
        return CatalogueSearchResult("no_match", "music_catalogue")
    options = dict(title=title, artist=artist, barcode=barcode, catalog_number=catalog_number,
                   format=format, country=country)
    first = await _catalogue_leg("musicbrainz", search(client, **options), options=options, limit=30)
    second = await _catalogue_leg("discogs", discogs.search_public_releases(
        title, client, token=discogs_token, artist=artist or None, barcode=barcode or None,
        catalog_number=catalog_number or None, media_type=format or None, country=country or None, limit=20),
        options=options, limit=20)
    legs = (first, second)
    candidates = [row for leg in legs if leg.found for row in leg.payload]
    winner = (provider_result.found("music_catalogue", candidates, status=None) if candidates
              else provider_result.combine(legs, provider="music_catalogue"))
    return CatalogueSearchResult(winner.outcome, winner.provider, winner.payload, winner.status, legs)
