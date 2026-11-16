"""Explicit music matching: identifiers first, explained candidates, no writes."""

import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlsplit
from uuid import UUID

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


async def _catalogue_mb_search(query, client, *, artist=None, barcode=None, catalog_number=None,
                               media_type="", country="", limit=30):
    """Strict empty-response boundary for catalogue fallback, not legacy search.

    The legacy MB client accepts a missing/null releases member as an empty
    list. That cannot authorize a second provider, so validate the raw search
    envelope here before using its existing release normalizer.
    """
    def literal(value):
        return re.sub(r'([+\-!(){}\[\]^"~*?:\\/|&])', r'\\\1', value.strip())

    clauses = []
    for field, value in (("barcode", barcode), ("catno", catalog_number), ("release", query), ("artist", artist)):
        if value and value.strip():
            clauses.append(f'{field}:"{literal(value)}"')
    if not clauses:
        return provider_result.no_match("musicbrainz")
    formats = {"vinyl": "(Vinyl OR 7vinyl OR 10vinyl OR 12vinyl)", "cd": "CD",
               "cassette": "Cassette", "digital_music": "DigitalMedia"}
    if media_type in formats:
        clauses.append("format:" + formats[media_type])
    if country:
        clauses.append(f'country:"{literal(country)}"')
    result = await musicbrainz._request_json(client, f"{musicbrainz.MUSICBRAINZ_BASE}/release/", params={
        "query": " AND ".join(clauses), "fmt": "json", "limit": str(limit),
    })
    if not result.found:
        if result.outcome == "no_match" and result.status in (400, 401, 403):
            return provider_result.rejected("musicbrainz", status=result.status)
        if result.outcome == "no_match" and result.status is not None and result.status >= 500:
            return provider_result.transport_failed("musicbrainz")
        return result
    payload = result.payload
    if not isinstance(payload, dict) or not isinstance(payload.get("releases"), list):
        return provider_result.transport_failed("musicbrainz")
    releases = []
    try:
        for row in payload["releases"][:100]:
            if (not isinstance(row, dict) or not release_id(row.get("id"))
                    or not isinstance(row.get("title"), str) or not row["title"].strip()):
                return provider_result.transport_failed("musicbrainz")
            releases.append(musicbrainz._search_summary(row))
    except (TypeError, ValueError, AttributeError):
        return provider_result.transport_failed("musicbrainz")
    return result.with_payload(releases)


def _catalogue_summaries(result):
    if not result.found:
        return result
    candidates = []
    for row in result.payload:
        candidate = dict(row)
        if result.provider == "musicbrainz":
            mbid = candidate.get("musicbrainz_release_id")
            candidate.update(provider="musicbrainz", provider_release_id=mbid,
                             source_url=f"https://musicbrainz.org/release/{mbid}")
        candidates.append(candidate)
    return result.with_payload(candidates)


async def search_catalogue(client, *, title="", artist="", barcode="", catalog_number="", format="",
                           country="", discogs_token=""):
    """MusicBrainz first; only a genuine empty cassette lookup permits Discogs."""
    title, artist = title.strip(), artist.strip()
    if format != "cassette" or not title or not artist:
        return _catalogue_summaries(await search(client, title=title, artist=artist, barcode=barcode,
                                                catalog_number=catalog_number, format=format, country=country))
    barcode = upc.normalize_barcode(barcode)[:32]
    options = {"media_type": format, "country": country[:2], "limit": 30}
    queries = []
    if barcode:
        codes = [barcode]
        if len(barcode) == 12:
            codes.append("0" + barcode)
        elif len(barcode) == 13 and barcode.startswith("0"):
            codes.append(barcode[1:])
        queries.extend(("", {"barcode": code}) for code in codes)
    if catalog_number.strip():
        queries.append(("", {"catalog_number": catalog_number.strip()}))
    queries.append((title, {"artist": artist}))
    for query, identifiers in queries:
        result = await _catalogue_mb_search(query, client, **identifiers, **options)
        if result.found and result.payload:
            return _catalogue_summaries(result.with_payload(rank(result.payload, title=title, artist=artist,
                barcode=barcode, catalog_number=catalog_number, format=format, country=country)))
        if not (result.found or (result.outcome == "no_match" and result.status == 200)):
            return result
    result = await discogs.search_public_releases(title, client, token=discogs_token, artist=artist,
        barcode=barcode or None, catalog_number=catalog_number.strip() or None,
        media_type=format, country=country or None, limit=20)
    if not result.found:
        return result
    return result.with_payload(rank(result.payload, title=title, artist=artist, barcode=barcode,
                                    catalog_number=catalog_number, format=format, country=country))
