"""Bounded, read-only OPDS 1 Atom / OPDS 2 JSON enumeration.

The configured root defines the catalogue scope. We follow same-origin `next`
pages and subsection/navigation feeds beneath its path (or its directory for a
file-like root), at most MAX_DEPTH levels. Search, facets, start/up/related and
acquisition URLs are never fetched. A bound, cycle or failed child makes the
whole enumeration partial, so the service must not reconcile missing records.
Same-origin canonical redirects are supported within the configured path/feed
directory. Every traversal and redirect retains that boundary; a moved endpoint
outside it needs a configuration change. Compressed bodies are never read.
"""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx

from app.services.source_connectors.base import (
    Catalogue, ConnectorError, SourceRecord, browser_link, contains_secret, credential_secrets, origin, safe_url,
    validate_root_url,
)

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_PAGES = 50
MAX_ENTRIES = 5000
MAX_DEPTH = 4
FETCH_SECONDS = 60
OPEN_ACCESS = "http://opds-spec.org/acquisition/open-access"
ACQUISITION = "http://opds-spec.org/acquisition"


@dataclass
class _Page:
    records: list[SourceRecord] = field(default_factory=list)
    next_links: list[str] = field(default_factory=list)
    navigation: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total: int | None = None


def _text(value, maximum=2000) -> str:
    return value[:maximum] if isinstance(value, str) else ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node, name: str) -> str:
    for child in node:
        if _local(child.tag) == name:
            return "".join(child.itertext())
    return ""


def _relations(link: dict) -> list[str]:
    rel = link.get("rel", [])
    return rel.split() if isinstance(rel, str) else [r for r in rel if isinstance(r, str)] if isinstance(rel, list) else []


def _is_feed(link: dict) -> bool:
    mime = link.get("type", "")
    return isinstance(mime, str) and mime.split(";", 1)[0].strip().casefold() in {
        "application/atom+xml", "application/opds+json",
    }


def _open_link(links: list, page_url: str, source: dict) -> tuple[str | None, str | None]:
    def paid(link: dict) -> bool:
        properties = link.get("properties")
        return bool(link.get("_paid") or (isinstance(properties, dict) and "price" in properties)
                    or any(rel.startswith(ACQUISITION + "/") and rel != OPEN_ACCESS
                           for rel in _relations(link)))

    # Multiple declarations of one href must not turn a buy/borrow/sample URL
    # into a free action merely by adding the generic acquisition relation.
    paid_targets = {urljoin(page_url, link["href"]) for link in links
                    if isinstance(link, dict) and isinstance(link.get("href"), str) and paid(link)}
    for link in links:
        if (not isinstance(link, dict) or not {OPEN_ACCESS, ACQUISITION}.intersection(_relations(link))
                or paid(link)):
            continue
        href = link.get("href")
        if not isinstance(href, str) or len(href) > 2048:
            continue
        target = urljoin(page_url, href)
        if target in paid_targets:
            continue
        url = browser_link(target, source)
        if url:
            return url, _text(link.get("type"), 100) or None
    return None, None


def _isbn(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = re.sub(r"^(?:urn:isbn:|isbn(?:-1[03])?:?)\s*", "", value.strip(), flags=re.I)
    # The shared canonicaliser intentionally removes arbitrary letters for
    # legacy callers. Here an opaque URI/URL is NOT evidence of an ISBN.
    if not re.fullmatch(r"[0-9Xx -]+", value):
        return None
    from app.services.isbn import canonical_isbn_pair
    pair = canonical_isbn_pair(value)
    return pair[0] if pair else None


def _atom_links(node) -> list[dict]:
    result = []
    for child in node:
        if _local(child.tag) == "link":
            link = dict(child.attrib)
            if any(_local(n.tag) == "price" for n in child):
                link["_paid"] = True
            result.append(link)
    return result


def _link_lists(page: _Page, links: list, *, navigation=False):
    for link in links:
        if not isinstance(link, dict):
            page.warnings.append("malformed_response")
            continue
        raw_rel = link.get("rel", [])
        if not (isinstance(raw_rel, str) or (isinstance(raw_rel, list) and all(isinstance(r, str) for r in raw_rel))):
            page.warnings.append("malformed_response")
            continue
        relations = _relations(link)
        traversal = navigation or "next" in relations or "subsection" in relations
        if not traversal:
            continue
        mime = link.get("type")
        # A navigation array is not permission to fetch acquisitions, images,
        # searches or other non-feed bodies with the server's authentication.
        excluded = any(rel.startswith((ACQUISITION, "https://opds-spec.org/acquisition",
                                      "http://opds-spec.org/image", "https://opds-spec.org/image",
                                      "http://opds-spec.org/cover", "https://opds-spec.org/cover"))
                       or rel in {"search", "http://opds-spec.org/search", "https://opds-spec.org/search",
                                  "image", "thumbnail", "up", "start", "related", "self", "previous", "prev", "first", "last"}
                       for rel in (r.casefold() for r in relations))
        if excluded or (mime not in (None, "") and not _is_feed(link)):
            page.warnings.append("unsafe_link")
            continue
        href = link.get("href")
        if not isinstance(href, str) or not href.strip() or len(href) > 2048:
            page.warnings.append("malformed_response")
            continue
        if "next" in relations:
            page.next_links.append(href)
        else:
            page.navigation.append(href)


def _xml_page(body: bytes, page_url: str, source: dict, media_type: str) -> _Page:
    # NUL rejection prevents a UTF-16/32 declaration from bypassing this check.
    if b"\x00" in body or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.I):
        raise ConnectorError("malformed_response")
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError):
        raise ConnectorError("malformed_response") from None
    if _local(root.tag) != "feed":
        raise ConnectorError("malformed_response")
    page = _Page()
    _link_lists(page, _atom_links(root))
    for node in root:
        if _local(node.tag) == "totalResults":
            try:
                page.total = int(node.text)
                if page.total < 0:
                    raise ValueError
            except (TypeError, ValueError):
                page.warnings.append("malformed_response")
        if _local(node.tag) != "entry":
            continue
        links = _atom_links(node)
        if any(_is_feed(link) or "subsection" in _relations(link) for link in links) and not any(
            any(rel.startswith("http://opds-spec.org/acquisition") for rel in _relations(link))
            for link in links
        ):
            _link_lists(page, links, navigation=True)
            continue
        external_id, title = _child_text(node, "id"), _child_text(node, "title")
        if not external_id.strip() or len(external_id) > 512 or not title.strip():
            page.warnings.append("invalid_record")
            continue
        authors = [_child_text(n, "name") for n in node if _local(n.tag) == "author"]
        isbn = None
        metadata = {}
        for n in node:
            name = _local(n.tag)
            if name in {"identifier", "id"}:
                isbn = isbn or _isbn(n.text)
            if name in {"publisher", "language", "issued", "summary", "content"}:
                field_name = {"issued": "published_date", "summary": "description", "content": "description"}.get(name, name)
                metadata[field_name] = _text("".join(n.itertext()), 8000)
        open_url, format_name = _open_link(links, page_url, source)
        page.records.append(SourceRecord(
            external_id, _text(title.strip()), media_type,
            _text(", ".join(a.strip() for a in authors if a.strip())), isbn,
            format=format_name, open_url=open_url, metadata=metadata,
        ))
    return page


def _json_page(body: bytes, page_url: str, source: dict, media_type: str) -> _Page:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ConnectorError("malformed_response") from None
    if not isinstance(data, dict) or not any(k in data for k in ("publications", "navigation", "groups")):
        raise ConnectorError("malformed_response")
    page = _Page()
    groups = [data]
    if "groups" in data:
        if not isinstance(data["groups"], list) or any(not isinstance(g, dict) for g in data["groups"]):
            raise ConnectorError("malformed_response")
        groups += data["groups"]
    for group_index, group in enumerate(groups):
        links = group.get("links", [])
        publications, navigation = group.get("publications", []), group.get("navigation", [])
        if not all(isinstance(v, list) for v in (links, publications, navigation)):
            raise ConnectorError("malformed_response")
        _link_lists(page, links)
        _link_lists(page, navigation, navigation=True)
        group_ids = set()
        for publication in publications:
            if not isinstance(publication, dict) or not isinstance(publication.get("metadata"), dict):
                page.warnings.append("invalid_record")
                continue
            meta = publication["metadata"]
            external_id, title = meta.get("identifier"), meta.get("title")
            if (not isinstance(external_id, str) or not external_id.strip() or len(external_id) > 512
                    or not isinstance(title, str) or not title.strip()):
                page.warnings.append("invalid_record")
                continue
            authors = meta.get("author", [])
            if not isinstance(authors, list):
                authors = [authors]
            names = [_text(a.get("name")) if isinstance(a, dict) else _text(a) for a in authors]
            publication_links = publication.get("links", [])
            if not isinstance(publication_links, list):
                page.warnings.append("invalid_record")
                publication_links = []
            open_url, format_name = _open_link(publication_links, page_url, source)
            descriptive = {key: _text(meta.get(key), 8000) for key in ("description", "language", "publisher")
                           if isinstance(meta.get(key), str)}
            if isinstance(meta.get("published"), str):
                descriptive["published_date"] = _text(meta["published"], 100)
            page.records.append(SourceRecord(
                external_id, _text(title.strip()), media_type,
                _text(", ".join(n for n in names if n)), _isbn(external_id),
                format=format_name, open_url=open_url, metadata=descriptive,
            ))
            group_ids.add(external_id)
        if group_index:
            metadata = group.get("metadata", {})
            if not isinstance(metadata, dict):
                page.warnings.append("malformed_response")
            elif "numberOfItems" in metadata:
                total = metadata["numberOfItems"]
                if type(total) is not int or total < 0:
                    page.warnings.append("malformed_response")
                elif total != len(group_ids):
                    # Group identity/counts across remote pages are not a
                    # reliable snapshot. Unresolved counts stay conservative;
                    # fully enumerated inline groups remain complete.
                    page.warnings.append("incomplete")
            if group.get("groups"):
                page.warnings.append("incomplete")
    metadata = data.get("metadata", {})
    if isinstance(metadata, dict) and "numberOfItems" in metadata:
        total = metadata["numberOfItems"]
        if type(total) is int and total >= 0:
            page.total = total
        else:
            page.warnings.append("malformed_response")
    return page


def _bounded_option(options: dict, key: str, default: int, maximum: int) -> int:
    value = options.get(key, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ConnectorError("invalid_options")
    return value


async def _read_page(client, url: str, root_origin, auth, budget: list[int], secrets: list[str], scope_root: str | None = None):
    for _ in range(4):
        if (not safe_url(url, fetch=True) or origin(url) != root_origin
                or (scope_root is not None and not _scoped(url, scope_root))
                or contains_secret(url, secrets)):
            raise ConnectorError("unsafe_redirect")
        async with client.stream("GET", url, auth=auth) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ConnectorError("unsafe_redirect")
                try:
                    url = urljoin(url, location)
                except ValueError:
                    raise ConnectorError("unsafe_redirect") from None
                continue
            if response.status_code == 401:
                raise ConnectorError("auth_failed")
            if response.status_code == 403:
                raise ConnectorError("forbidden")
            if response.status_code == 429:
                raise ConnectorError("rate_limited")
            if response.status_code != 200:
                raise ConnectorError("http_error")
            if response.headers.get("content-encoding", "identity").strip().casefold() not in {"", "identity"}:
                raise ConnectorError("unsupported_encoding")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                budget[0] += len(chunk)
                if len(body) + len(chunk) > MAX_PAGE_BYTES or budget[0] > MAX_TOTAL_BYTES:
                    raise ConnectorError("too_large")
                body.extend(chunk)
            return bytes(body), url, response.headers.get("content-type", "")
    raise ConnectorError("unsafe_redirect")


def _scoped(url: str, root: str) -> bool:
    path, root_path = urlsplit(url).path, urlsplit(root).path
    for _ in range(4):
        path, root_path = unquote(path), unquote(root_path)
    if any(segment in {".", ".."} for segment in path.split("/")):
        return False
    prefix = root_path.rstrip("/")
    if "." in prefix.rsplit("/", 1)[-1]:
        prefix = prefix.rsplit("/", 1)[0]
    return path == prefix or path.startswith(prefix + "/")


async def fetch_catalogue(source: dict, credentials: dict) -> Catalogue:
    root = validate_root_url(source.get("base_url", ""))
    root_origin = origin(root)
    options = source.get("options")
    if options is None:
        try:
            options = json.loads(source.get("options_json") or "{}")
        except (ValueError, TypeError):
            raise ConnectorError("invalid_options") from None
    if not isinstance(options, dict):
        raise ConnectorError("invalid_options")
    pages_limit = _bounded_option(options, "max_pages", 20, MAX_PAGES)
    entries_limit = _bounded_option(options, "max_entries", 2000, MAX_ENTRIES)
    depth_limit = _bounded_option(options, "max_depth", 2, MAX_DEPTH)
    media_type = options.get("media_type", "ebook")
    if media_type not in {"ebook", "book", "audiobook", "comic", "manga"}:
        raise ConnectorError("invalid_options")
    username, password = credentials.get("username", ""), credentials.get("password", "")
    if not isinstance(username, str) or not isinstance(password, str) or len(username) > 512 or len(password) > 4096:
        raise ConnectorError("invalid_credentials")
    auth = httpx.BasicAuth(username, password) if username or password else None
    # A username may legitimately be the hostname (e.g. "books"). Never put the
    # saved username in a DTO, but do not mistake that coincidence for a leak.
    secrets = credential_secrets(credentials)
    result = Catalogue([])
    pending = [(root, 0, (root,))]
    seen_urls, seen_ids = set(), set()
    budget = [0]
    declared, observed = {}, {}
    parsed_pages = 0
    try:
        async with asyncio.timeout(FETCH_SECONDS):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False, trust_env=False,
                headers={"Accept": "application/opds+json, application/atom+xml;q=0.9", "Accept-Encoding": "identity", "User-Agent": "Shelf-OPDS/1"},
            ) as client:
                while pending:
                    if len(seen_urls) >= pages_limit:
                        raise ConnectorError("limit_reached")
                    url, depth, ancestry = pending.pop(0)
                    if url in seen_urls:
                        result.warnings.append("cycle")
                        continue
                    seen_urls.add(url)
                    body, final_url, content_type = await _read_page(
                        client, url, root_origin, auth, budget, secrets,
                        scope_root=root,
                    )
                    if final_url != url and final_url in seen_urls:
                        raise ConnectorError("cycle")
                    parser = _json_page if "json" in content_type or body.lstrip().startswith(b"{") else _xml_page
                    page = parser(body, final_url, source, media_type)
                    parsed_pages += 1
                    if page.total is not None:
                        group = ancestry[-1]
                        if group in declared and declared[group] != page.total:
                            result.warnings.append("incomplete")
                        declared[group] = max(declared.get(group, 0), page.total)
                    result.warnings.extend(page.warnings)
                    for record in page.records:
                        if record.external_id in seen_ids:
                            result.warnings.append("duplicate_record")
                            continue
                        if len(result.records) >= entries_limit:
                            raise ConnectorError("limit_reached")
                        seen_ids.add(record.external_id)
                        result.records.append(record)
                        for group in ancestry:
                            observed.setdefault(group, set()).add(record.external_id)
                    for href, is_navigation in [(h, False) for h in page.next_links] + [(h, True) for h in page.navigation]:
                        try:
                            link = safe_url(urljoin(final_url, href), fetch=True)
                        except ValueError:
                            link = None
                        if (not link or origin(link) != root_origin
                                or contains_secret(link, secrets)
                                or not _scoped(link, root)):
                            result.warnings.append("unsafe_link")
                            continue
                        if is_navigation and depth >= depth_limit:
                            result.warnings.append("limit_reached")
                            continue
                        pending.append((urlunsplit(urlsplit(link)), depth + int(is_navigation),
                                        ancestry + (link,) if is_navigation else ancestry))
                    if len(pending) > pages_limit:
                        raise ConnectorError("limit_reached")
    except (TimeoutError, httpx.TimeoutException):
        if not parsed_pages:
            raise ConnectorError("timeout") from None
        result.warnings.append("timeout")
    except httpx.HTTPError:
        if not parsed_pages:
            raise ConnectorError("unreachable") from None
        result.warnings.append("unreachable")
    except ConnectorError as exc:
        if not parsed_pages:
            raise
        result.warnings.append(exc.code)
    if any(total > len(observed.get(group, set())) for group, total in declared.items()):
        result.warnings.append("incomplete")
    result.warnings = list(dict.fromkeys(result.warnings))
    result.complete = not result.warnings
    return result
