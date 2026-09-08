"""Explicit OIDC provider logout policy.

Shelf never guesses an end-session endpoint from discovery metadata. An admin
may opt into one exact HTTPS URL; invalid persisted values fail safely back to
Shelf's local login page.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

from app.database import get_db, get_setting

logger = logging.getLogger(__name__)


class OIDCLogoutError(ValueError):
    pass


def validate_provider_logout_url(value: str) -> str:
    url = (value or "").strip()
    if not url:
        return ""
    if len(url) > 2048:
        raise OIDCLogoutError("OIDC provider logout URL is too long")

    parsed = urlsplit(url)
    allow_http = bool(os.environ.get("SHELF_OIDC_ALLOW_INSECURE_HTTP"))
    valid_schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme not in valid_schemes or not parsed.netloc:
        raise OIDCLogoutError("OIDC provider logout URL must use HTTPS")
    if parsed.username or parsed.password or parsed.fragment:
        raise OIDCLogoutError("OIDC provider logout URL is invalid")
    return url


def get_provider_logout_url() -> str:
    with get_db() as db:
        value = get_setting(db, "oidc_provider_logout_url") or ""
    if not value:
        return ""
    try:
        return validate_provider_logout_url(value)
    except OIDCLogoutError:
        logger.error("Ignoring invalid persisted OIDC provider logout URL")
        return ""
