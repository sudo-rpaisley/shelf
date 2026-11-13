"""Unauthenticated, exact-ASIN public catalogue metadata, never library/audio APIs.

Only a user-selected, allowlisted marketplace is queried. The public catalogue
is not a documented availability/licensing guarantee: a refusal, challenge,
sparse record or regional miss stays a failed preview, not a scraping fallback.
No account cookies, credentials, audio/asset URLs or provider HTML pages are used.
"""

import asyncio
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

from app import config, i18n
from app.services import authors, bib_normalize, covers, isbn, manual_cover, outbound, provider_result


# Keys, not user-supplied hostnames. Adding a market also requires pacing in config.
MARKETPLACES = {
    "us": (i18n.N_("United States"), "audible.com"), "uk": (i18n.N_("United Kingdom"), "audible.co.uk"),
    "ca": (i18n.N_("Canada"), "audible.ca"), "au": (i18n.N_("Australia"), "audible.com.au"),
    "de": (i18n.N_("Germany"), "audible.de"), "fr": (i18n.N_("France"), "audible.fr"),
    "it": (i18n.N_("Italy"), "audible.it"), "es": (i18n.N_("Spain"), "audible.es"),
    "jp": (i18n.N_("Japan"), "audible.co.jp"), "in": (i18n.N_("India"), "audible.in"),
}
MAX_RESPONSE_BYTES = 256 * 1024
PREVIEW_SECONDS = 15
ARTWORK_SECONDS = 25
FIELD_LIMITS = {"title": 2000, "subtitle": 2000, "authors": 2000, "narrator": 2000,
                "description": 20000, "publisher": 1000, "language": 100, "series_name": 1000}
_LANGUAGES = {"english": "en", "german": "de", "french": "fr", "italian": "it", "spanish": "es",
              "japanese": "ja", "portuguese": "pt", "dutch": "nl", "russian": "ru", "chinese": "zh",
              "hindi": "hi", "polish": "pl", "swedish": "sv", "danish": "da", "norwegian": "no"}
_IMAGE_HOSTS = frozenset({"m.media-amazon.com", "images-na.ssl-images-amazon.com"})
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


def identity(asin, marketplace):
    cleaned = isbn.clean_asin(asin)
    if not cleaned:
        raise ValueError(i18n.gettext("Save a valid ten-character ASIN on the audiobook before looking it up."))
    if not isinstance(marketplace, str) or marketplace not in MARKETPLACES:
        raise ValueError(i18n.gettext("Choose one of the listed Audible marketplaces."))
    return cleaned, marketplace


def product_url(asin, marketplace):
    asin, marketplace = identity(asin, marketplace)
    return f"https://www.{MARKETPLACES[marketplace][1]}/pd/{asin}"


def artwork_url(value):
    """Public image paths only: no signatures, credentials, query or fragments."""
    if not isinstance(value, str) or len(value) > 1024 or re.search(r"[\x00-\x20\\]", value):
        return None
    try:
        p = urlsplit(value)
        if (p.scheme != "https" or p.hostname not in _IMAGE_HOSTS or p.username is not None
                or p.password is not None or p.port not in (None, 443) or p.query or p.fragment
                or not re.fullmatch(r"/images/[IP]/[A-Za-z0-9+.,_!~()-]{1,250}\.(?:jpg|jpeg|png|webp|gif)", p.path)):
            return None
        # No path escapes, encoded slashes or dot segments pass the grammar.
        url = f"https://{p.hostname}{p.path}"
        return url if covers.is_allowed_cover_url(url) else None
    except (ValueError, UnicodeError):
        return None


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "li", "div"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _text(value, limit):
    if not isinstance(value, str) or len(value) > 100000:
        return None
    parser = _Plain()
    parser.feed(value)
    value = bib_normalize.nfc(" ".join("".join(parser.parts).split()))
    value = "".join(c for c in value if ord(c) >= 32 and not 127 <= ord(c) <= 159 and not 0xD800 <= ord(c) <= 0xDFFF)
    return value[:limit].strip() or None


def _names(value):
    if not isinstance(value, list):
        return None
    names = authors.join_names(_text(row.get("name"), 200) for row in value[:20] if isinstance(row, dict))
    return names[:2000] if names else None


def _integer(value, low, high):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,7}", value):
        value = int(value)
    return value if type(value) is int and low <= value <= high else None


def normalise(payload, asin, marketplace):
    """Filter identity BEFORE mapping. Related editions/podcasts are not matches."""
    asin, marketplace = identity(asin, marketplace)
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list) or len(products) > 10:
        return None
    exact = [p for p in products if isinstance(p, dict) and isbn.clean_asin(p.get("asin")) == asin]
    if len(exact) != 1:
        return None
    p = exact[0]
    if p.get("content_type") != "Product" or p.get("content_delivery_type") not in ("SinglePartBook", "MultiPartBook"):
        return None
    fields = {key: _text(p.get(source), FIELD_LIMITS[key]) for key, source in {
        "title": "title", "subtitle": "subtitle", "publisher": "publisher_name",
        "description": "publisher_summary", "language": "language",
    }.items()}
    if not fields["title"]:
        return None
    fields.update(authors=_names(p.get("authors")), narrator=_names(p.get("narrators")),
                  duration_mins=_integer(p.get("runtime_length_min"), 1, 1000000))
    if fields["language"]:
        language = fields["language"].casefold()
        fields["language"] = bib_normalize.to_iso639_1(_LANGUAGES.get(language, language))
    date = _text(p.get("release_date"), 32)
    fields["publish_year"] = _integer(date[:4], 1, 9999) if date and re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}-[0-9]{2})?", date) else None
    series = p.get("series")
    if isinstance(series, list) and series and isinstance(series[0], dict):
        first = series[0]
        fields["series_name"] = _text(first.get("title"), FIELD_LIMITS["series_name"])
        sequence = first.get("sequence")
        if fields["series_name"] and isinstance(sequence, str) and re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,4})?", sequence):
            fields["series_position"] = float(sequence)
    images = p.get("product_images")
    choices = sorted(((int(key), url) for key, url in images.items()
                      if isinstance(key, str) and re.fullmatch(r"[0-9]{1,4}", key) and artwork_url(url)), reverse=True) if isinstance(images, dict) else []
    return {"kind": "audible", "asin": asin, "marketplace": marketplace, "source_url": product_url(asin, marketplace),
            "fields": {k: v for k, v in fields.items() if v not in (None, "")},
            "edition": {"format": _text(p.get("format_type"), 100), "release_date": date},
            "images": [artwork_url(choices[0][1])] if choices else []}


class _Unreadable(ValueError):
    pass


async def _read_public(url, *, params=None, image=False):
    """One DNS-pinned, paced hop; no inherited auth/proxies/cookies or redirects."""
    target = await manual_cover._public_target(url)
    if target is None:
        raise _Unreadable
    host, address = target
    await outbound.acquire(host)
    async with httpx.AsyncClient(transport=manual_cover._PinnedIPTransport(address), trust_env=False,
                                timeout=8, follow_redirects=False) as client:
        async with client.stream("GET", url, params=params, headers={
            "Accept": "image/jpeg,image/png,image/webp,image/gif" if image else "application/json",
            "Accept-Encoding": "identity", "User-Agent": "Shelf/1.0 (public catalogue metadata preview)",
        }) as response:
            if response.status_code != 200:
                return response.status_code, response.headers.get("location"), None
            mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            allowed = {"image/jpeg", "image/png", "image/webp", "image/gif"} if image else {"application/json"}
            if mime not in allowed or response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                raise _Unreadable
            limit = covers.MAX_COVER_SIZE if image else MAX_RESPONSE_BYTES
            length = response.headers.get("content-length")
            if length and (not length.isascii() or not length.isdigit() or int(length) > limit):
                raise _Unreadable
            data = bytearray()
            async for chunk in response.aiter_raw():
                if len(data) + len(chunk) > limit:
                    raise _Unreadable
                data.extend(chunk)
            return 200, None, bytes(data)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Unreadable
        result[key] = value
    return result


def _nonfinite(value):
    raise _Unreadable


async def lookup(asin, marketplace):
    """One bounded request, no retries, marketplace search or title fallback."""
    asin, marketplace = identity(asin, marketplace)
    url = f"https://api.{MARKETPLACES[marketplace][1]}/1.0/catalog/products"
    try:
        async with asyncio.timeout(PREVIEW_SECONDS):
            status, _, data = await _read_public(url, params={"asins": asin, "num_results": "1", "image_sizes": "500",
                "response_groups": "contributors,media,product_attrs,product_desc,series"})
        if status == 429:
            return provider_result.rate_limited("audible")
        if status in {401, 403}:
            return provider_result.rejected("audible", status=status)
        if status in {404, 410}:
            return provider_result.no_match("audible", status=status)
        if status != 200:
            return provider_result.transport_failed("audible")
        candidate = normalise(json.loads(data, object_pairs_hook=_unique_object, parse_constant=_nonfinite), asin, marketplace)
        return provider_result.found("audible", candidate) if candidate else provider_result.no_match("audible")
    except (httpx.HTTPError, OSError, ValueError, RecursionError, TimeoutError):
        return provider_result.transport_failed("audible")


def _stage_artwork(item_id, content):
    """Install complete bytes atomically, retaining ownership of every path.

    The generic cover writer chooses its unique filename internally, so an
    exception before it returns cannot tell this caller which partial file to
    remove. Keep both paths here, reuse shared validation, and never expose a
    partial file as retained item artwork.
    """
    if not covers.validate_uploaded_cover(content):
        return None
    config.COVERS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{item_id}-{uuid4().hex}.jpg"
    destination = config.COVERS_DIR / name
    temporary = config.COVERS_DIR / f".{name}.part"
    try:
        if temporary.write_bytes(content) != len(content):
            raise OSError("Incomplete artwork write")
        temporary.replace(destination)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            destination.unlink(missing_ok=True)
        raise
    return f"covers/{name}"


async def download_artwork(item_id, url):
    """Use shared DNS/image/file mechanisms, checking EVERY redirect before I/O."""
    try:
        async with asyncio.timeout(ARTWORK_SECONDS):
            for _ in range(4):
                url = artwork_url(url)
                if not url:
                    return None
                status, location, content = await _read_public(url, image=True)
                if status in _REDIRECTS and location:
                    url = urljoin(url, location)
                    continue
                if status != 200 or content is None:
                    return None
                return _stage_artwork(item_id, content)
    except (httpx.HTTPError, OSError, ValueError, TimeoutError):
        pass
    return None
