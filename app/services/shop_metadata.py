"""Read public shop metadata, not scripts, players, media files or account pages.

JSON-LD Product/MusicAlbum/MusicRelease and OpenGraph work across many shops.
JavaScript-only/challenge pages are intentionally not bypassed. Every URL hop
uses the public, DNS-pinned HTTPS boundary shared with manual cover downloads.
"""

import asyncio
import html
import json
import re
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from app.services import manual_cover, outbound, upc

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGES = 12


class ShopError(ValueError):
    pass


def public_url(value: str, *, page=False) -> str | None:
    """Syntactic boundary; public DNS is checked immediately before each fetch."""
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\\]", value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)):
            return None
        if page:
            pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=20)
            # Keep product/variant selectors; drop tracking. Do not send pasted
            # credentials, signed download URLs or arbitrary API query strings.
            if any(key.casefold() not in {"id", "pid", "product_id", "variant", "sku", "ref", "fbclid", "gclid"}
                   and not key.casefold().startswith("utm_") for key, _ in pairs):
                return None
            parsed = parsed._replace(query=urlencode([(key, val) for key, val in pairs
                                                       if key.casefold() in {"id", "pid", "product_id", "variant", "sku"}]))
        return urlunsplit(parsed._replace(fragment=""))
    except (ValueError, UnicodeError):
        return None


class _Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.meta, self.documents, self.json_text = {}, [], None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").casefold()
            if key and attrs.get("content") and len(self.meta) < 100:
                self.meta.setdefault(key, []).append(attrs["content"][:20000])
        if tag == "script" and (attrs.get("type") or "").split(";", 1)[0].strip().casefold() == "application/ld+json":
            self.json_text = []

    def handle_data(self, data):
        if self.json_text is not None:
            self.json_text.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.json_text is not None:
            if len(self.documents) < 30:
                try:
                    self.documents.append(json.loads("".join(self.json_text)))
                except (ValueError, RecursionError):
                    pass
            self.json_text = None


class _Plain(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.parts = []
        self.feed(text)

    def handle_data(self, data):
        self.parts.append(data)


def _plain(value, limit=1000):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return " ".join(html.unescape(" ".join(_Plain(str(value)[:20000]).parts)).split())[:limit]


def _name(value):
    if isinstance(value, list):
        return ", ".join(filter(None, (_name(part) for part in value[:20])))[:1000]
    return _plain(value.get("name")) if isinstance(value, dict) else _plain(value)


def _nodes(value, depth=0):
    if depth > 12:
        return
    if isinstance(value, list):
        for node in value[:100]:
            yield from _nodes(node, depth + 1)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "mainEntity", "hasVariant", "hasVariants", "itemListElement", "item"):
            if key in value:
                yield from _nodes(value[key], depth + 1)


def _images(value, base):
    if not isinstance(value, list):
        value = [value]
    found = []
    for image in value[:MAX_IMAGES]:
        if isinstance(image, dict):
            image = image.get("contentUrl") or image.get("url")
        if isinstance(image, str):
            url = public_url(urljoin(base, image))
            if url and url not in found:
                found.append(url)
    return found


def _tracks(value):
    if isinstance(value, dict):
        value = value.get("itemListElement") or [value]
    tracks = []
    for i, raw in enumerate(value[:200] if isinstance(value, list) else [], 1):
        if not isinstance(raw, dict):
            continue
        raw = raw.get("item", raw)
        if not isinstance(raw, dict) or not _plain(raw.get("name")):
            continue
        duration = str(raw.get("duration") or "")
        match = re.fullmatch(r"PT(?:(\d{1,3})H)?(?:(\d{1,3})M)?(?:(\d{1,3})S)?", duration)
        milliseconds = sum(int(n or 0) * factor for n, factor in zip(match.groups(), (3600000, 60000, 1000))) if match else None
        tracks.append({"position": i, "number": str(i), "title": _plain(raw["name"]),
                       "artist_credit": _name(raw.get("byArtist")), "duration_ms": milliseconds})
    return tracks


def parse(text: str, base: str) -> list[dict]:
    page = _Page(text)
    candidates = []
    for node in _nodes(page.documents):
        kinds = node.get("@type", [])
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not isinstance(kinds, list) or not {str(kind).rsplit("/", 1)[-1] for kind in kinds} & {"Product", "MusicAlbum", "MusicRelease"}:
            continue
        title = _plain(node.get("name"))
        if not title:
            continue
        album = node.get("releaseOf")
        album = album if isinstance(album, dict) else {}
        author = _name(node.get("byArtist") or album.get("byArtist") or node.get("author"))
        label = _name(node.get("recordLabel") or node.get("publisher") or node.get("brand"))
        date = _plain(node.get("datePublished") or node.get("releaseDate") or album.get("datePublished"), 32)
        if date and not re.match(r"\d{4}(?:-|$)", date):
            try:
                date = parsedate_to_datetime(date).date().isoformat()
            except (ValueError, TypeError, OverflowError):
                pass
        year = int(date[:4]) if re.match(r"\d{4}(?:-|$)", date) and 1 <= int(date[:4]) <= 9999 else None
        barcode_ok, barcode = upc.canonical_retail_barcode(str(node.get("gtin13") or node.get("gtin12") or node.get("gtin") or ""))
        if not barcode_ok:
            barcode = None
        catno = _plain(node.get("catalogNumber") or node.get("catalogueNumber") or node.get("mpn"), 200)
        tracks = _tracks(node.get("track") or album.get("track"))
        format = _name(node.get("musicReleaseFormat"))
        if format.startswith("http"):
            format = format.rsplit("/", 1)[-1]
        candidate = {
            "kind": "shop", "source_url": base,
            "fields": {key: value for key, value in {
                "title": title, "authors": author, "publisher": label, "publish_year": year,
                "upc": barcode, "description": _plain(node.get("description") or album.get("description"), 10000),
            }.items() if value not in (None, "")},
            "images": _images(node.get("image") or album.get("image"), base),
            "release": {"title": title, "artist_credit": author, "release_date": date or None,
                        "label": label, "catalog_number": catno, "format_summary": format,
                        "country": _name(node.get("countryOfOrigin")), "source": "shop",
                        "media": [{"position": 1, "format": format, "tracks": tracks, "track_count": len(tracks)}] if tracks else []},
        }
        if candidate not in candidates:
            candidates.append(candidate)
        if len(candidates) == 10:
            break
    og = lambda key: next(iter(page.meta.get(key, [])), "")
    og_images = _images(page.meta.get("og:image", []) + page.meta.get("og:image:secure_url", []), base)
    if not candidates and _plain(og("og:title")):
        candidates.append({"kind": "shop", "source_url": base, "images": og_images, "release": {"source": "shop"},
                           "fields": {key: value for key, value in {
                               "title": _plain(og("og:title")), "description": _plain(og("og:description"), 10000),
                           }.items() if value}})
    elif len(candidates) == 1:
        candidates[0]["images"] = list(dict.fromkeys(candidates[0]["images"] + og_images))[:MAX_IMAGES]
    return candidates


async def _fetch(url):
    for _ in range(6):
        target = await manual_cover._public_target(url)
        if not target:
            raise ShopError("Use a public HTTPS product page without credentials or tokens.")
        host, address = target
        await outbound.acquire(host)
        async with httpx.AsyncClient(transport=manual_cover._PinnedIPTransport(address), trust_env=False,
                                    timeout=12, follow_redirects=False) as client:
            async with client.stream("GET", url, headers={"Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity",
                                                          "User-Agent": "Shelf/1.0 (catalogue metadata preview)"}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = public_url(urljoin(url, response.headers.get("location", "")), page=True)
                    if not response.headers.get("location") or not url:
                        break
                    continue
                if (response.status_code != 200 or response.headers.get("content-type", "").split(";", 1)[0].lower()
                        not in {"text/html", "application/xhtml+xml"}
                        or response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}):
                    break
                data = bytearray()
                async for chunk in response.aiter_raw():
                    if len(data) + len(chunk) > MAX_PAGE_BYTES:
                        raise ShopError("That shop page is too large to preview.")
                    data.extend(chunk)
                return parse(data.decode("utf-8", errors="replace"), url)
    raise ShopError("That shop did not provide a readable public product page. Use another link or enter the details by hand.")


async def preview(url: str):
    url = public_url(url.strip(), page=True)
    if not url:
        raise ShopError("Use a public HTTPS product page without credentials or tokens.")
    try:
        async with asyncio.timeout(25):
            candidates = await _fetch(url)
    except (httpx.HTTPError, OSError, TimeoutError, ValueError, RecursionError) as error:
        if isinstance(error, ShopError):
            raise
        raise ShopError("The shop page could not be read. Try another link or enter the details by hand.") from None
    if not candidates:
        raise ShopError("No product metadata was found. JavaScript-only shops need manual details or a direct image URL.")
    return candidates
