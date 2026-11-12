"""Read-only connector contract and credential-free URL boundaries.

Administrator-configured LAN/localhost endpoints are intentional (G12). Remote
payloads are not configuration: their links must pass the same URL checks, and
credentials are never accepted in a URL. No connector writes to a remote server.
"""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit


@dataclass(frozen=True)
class SourceRecord:
    external_id: str
    title: str
    media_type: str
    authors: str = ""
    isbn: str | None = None
    library_id: str | None = None
    format: str | None = None
    open_url: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class Catalogue:
    records: list[SourceRecord]
    complete: bool = True
    warnings: list[str] = field(default_factory=list)


SAFE_CODES = frozenset({
    "connector_error", "invalid_url", "invalid_config", "invalid_credentials",
    "invalid_options", "unsupported_provider", "auth_failed", "unauthorized",
    "forbidden", "rate_limited", "timeout", "unreachable", "http_error",
    "malformed_response", "too_large", "unsafe_redirect", "unsafe_link",
    "limit_reached", "cycle", "incomplete", "invalid_record", "duplicate_record",
    "unexpected_error", "disabled", "busy", "stale_config", "not_found",
    "manual_only", "credentials_unavailable", "write_failed", "in_trash",
    "ambiguous_isbn", "record_conflict", "interrupted", "unsupported_encoding",
    # Jellyfin adapter codes are also fixed strings, not remote diagnostics.
    "auth", "redirect", "user_required", "credentials", "http", "limit",
    "invalid_response", "network", "pagination", "library_unavailable",
})


def safe_code(code: object) -> str:
    return code if isinstance(code, str) and code in SAFE_CODES else "connector_error"


class ConnectorError(Exception):
    """Only a stable, safe code crosses the provider boundary, never a body/URL."""

    def __init__(self, message: str = "connector_error", *, code: str | None = None):
        # Older/self-contained adapters may pass a diagnostic sentence as the
        # first argument. Never retain it: only their explicit safe code escapes.
        self.code = safe_code(code if code is not None else message)
        super().__init__(self.code)


def credential_secrets(credentials: dict) -> list[str]:
    """Actual authentication representations that must never reach snapshots.

    A username can legitimately equal a hostname/title, so it is not searched as
    a standalone secret. Basic's reversible credential blob IS a secret, even
    when the server reflects only base64 rather than the original password.
    """
    secrets = {value for key, value in credentials.items()
               if key != "username" and isinstance(value, str) and value}
    username, password = credentials.get("username", ""), credentials.get("password", "")
    if isinstance(username, str) and isinstance(password, str) and (username or password):
        basic = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        secrets.update((basic, f"Basic {basic}"))
    # ISBN-10/13 are reversible representations too. The OPDS parser may have
    # already canonicalised a reflected ISBN-shaped password before persistence.
    from app.services.isbn import canonical_isbn_pair
    for value in tuple(secrets):
        if re.fullmatch(r"[0-9Xx -]+", value):
            pair = canonical_isbn_pair(value)
            if pair:
                secrets.update(v for v in pair if v)
    return list(secrets)


def contains_secret(value: object, secrets: list[str]) -> bool:
    """Match raw and up to four percent/HTML-encoded reflection layers."""
    if not isinstance(value, str):
        return False
    for _ in range(5):
        if any(secret and secret in value for secret in secrets):
            return True
        decoded = html.unescape(unquote(value))
        if decoded == value:
            return False
        value = decoded
    return False


# Query strings on public links are deliberately conservative. Credentials may
# hide behind arbitrary names, so an allowlist is safer than a token denylist.
_PUBLIC_QUERY = frozenset({
    "id", "itemid", "bookid", "libraryid", "serverid", "page", "lang", "language",
    "isbn", "format", "view",
})
_FETCH_QUERY = _PUBLIC_QUERY | frozenset({
    "offset", "limit", "count", "start", "startindex", "pagesize", "pagenumber",
    "cursor", "after", "before", "sort", "order", "q", "query", "search",
})


def _url_parts(value: str):
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or "\\" in value):
        raise ConnectorError("invalid_url")
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or any(c.isspace() for c in parts.netloc)
                or "%" in parts.netloc):
            raise ValueError
        if parts.port == 0:
            raise ValueError
        parts.port  # Validate malformed/out-of-range ports, including IPv6.
    except ValueError:
        raise ConnectorError("invalid_url") from None
    return parts


def validate_root_url(value: str, *, optional: bool = False) -> str:
    """Validate an admin root; auth belongs in encrypted credentials, not URL."""
    if optional and not value:
        return ""
    parts = _url_parts(value.strip() if isinstance(value, str) else value)
    if parts.query or parts.fragment:
        raise ConnectorError("invalid_url")
    return urlunsplit(parts)


def origin(value: str) -> tuple[str, str, int]:
    parts = _url_parts(value)
    return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)


def safe_url(value: object, *, fetch: bool = False) -> str | None:
    """A credential-free HTTP(S) link, or None; never sanitize by deleting auth.

    Unknown query names are refused, not stripped into a misleading working link.
    Fragments support browser routes (e.g. Jellyfin #/details?id=...), using the
    same allowlist. Percent-encoded controls and credential path markers fail.
    """
    if not isinstance(value, str):
        return None
    try:
        parts = _url_parts(value)
        decoded = value
        for _ in range(3):
            decoded = unquote(decoded)
        if any(ord(c) < 32 or ord(c) == 127 for c in decoded) or "\\" in decoded:
            return None
        lowered = decoded.casefold()
        if any(marker in lowered for marker in (
            "api_key", "apikey", "access_token", "auth_token", "password",
            "authorization", "devkey", "x-emby-token", "x-plex-token",
        )):
            return None
        allowed = _FETCH_QUERY if fetch else _PUBLIC_QUERY
        queries = [parts.query]
        if "?" in parts.fragment:
            queries.append(parts.fragment.split("?", 1)[1])
        elif "=" in parts.fragment:
            queries.append(parts.fragment)
        # Decode names, not the whole query: an opaque, correctly quoted `id`
        # can legitimately contain '&' or '=' without creating new parameters.
        if any(unquote(key).casefold() not in allowed for query in queries
               for key, _ in parse_qsl(query, keep_blank_values=True)):
            return None
        if fetch and parts.fragment:
            return None
        return value
    except (ConnectorError, ValueError):
        return None


def browser_link(value: str | None, source: dict) -> str | None:
    """Map an API path under the configured root onto an optional browser root.

    Only links under the root path and on its exact origin are rewritten. The
    configured browser path is a prefix, not a replacement for arbitrary URLs.
    """
    value = safe_url(value)
    if not value:
        return None
    browser = source.get("browser_url")
    root = source.get("base_url")
    if not browser or not root:
        return value
    try:
        if origin(value) != origin(root):
            return value
        remote, base, public = urlsplit(value), urlsplit(root), urlsplit(browser)
        prefix = base.path.rstrip("/")
        if remote.path != prefix and not remote.path.startswith(prefix + "/"):
            return value
        suffix = remote.path[len(prefix):]
        return safe_url(urlunsplit((public.scheme, public.netloc,
                                   public.path.rstrip("/") + suffix,
                                   remote.query, remote.fragment)))
    except (ConnectorError, ValueError):
        return None
