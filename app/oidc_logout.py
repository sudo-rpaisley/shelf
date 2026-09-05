"""Best-effort RP-initiated logout support for Shelf OIDC sessions."""

import logging
import os
from urllib.parse import urlencode, urlsplit

from fastapi import Request

from app.database import get_db, get_setting
from app.oidc import OIDCError, discover, get_oidc_config

logger = logging.getLogger(__name__)


def provider_logout_enabled() -> bool:
    with get_db() as db:
        value = get_setting(db, "oidc_provider_logout") or "0"
    return value.strip().lower() in {"1", "true", "yes", "on"}


def save_provider_logout(enabled: bool) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('oidc_provider_logout', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if enabled else "0",),
        )


def _validate_logout_endpoint(url: str) -> None:
    parsed = urlsplit(url)
    allow_http = bool(os.environ.get("SHELF_OIDC_ALLOW_INSECURE_HTTP"))
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise OIDCError("OIDC provider advertised an invalid logout endpoint")


async def provider_logout_url(request: Request) -> str | None:
    """Return a discovered provider logout URL, or None when unsupported.

    Shelf does not retain the provider's ID token after sign-in, so this uses
    the interoperable ``client_id`` + ``post_logout_redirect_uri`` parameters.
    Providers that require an ``id_token_hint`` may ignore the request; local
    Shelf logout has already succeeded regardless.
    """
    config = get_oidc_config()
    if not config.enabled or not config.configured:
        return None

    metadata = await discover(config)
    endpoint = metadata.get("end_session_endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return None
    _validate_logout_endpoint(endpoint)

    params = urlencode(
        {
            "client_id": config.client_id,
            "post_logout_redirect_uri": str(request.url_for("login_page")),
        }
    )
    separator = "&" if urlsplit(endpoint).query else "?"
    return endpoint + separator + params
