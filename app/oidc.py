"""Provider-neutral OpenID Connect identity and authorisation flow for Shelf.

The first layer owns Shelf policy: issuer validation, group claim extraction,
required-group access control and deterministic mapping to Shelf's existing
admin/editor/viewer roles. This stacked layer adds standards-based discovery
and Authorization Code + PKCE state/nonce handling, still without binding the
flow to Shelf's login routes or local user provisioning.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from app.crypto import decrypt_value, encrypt_value, get_encryption_key


_USERNAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_GROUP_SPLIT = re.compile(r"[\r\n,]+")

FLOW_COOKIE = "oidc_flow"
FLOW_TTL_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 10.0


class OIDCError(Exception):
    """A safe, user-presentable OIDC configuration/identity failure."""


class OIDCAccessDenied(OIDCError):
    """Identity was valid but Shelf's access policy denied it."""


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    group_claim: str = "groups"
    required_group: str = ""
    admin_groups: tuple[str, ...] = ()
    editor_groups: tuple[str, ...] = ()
    viewer_groups: tuple[str, ...] = ()
    default_role: str = "deny"
    auto_provision: bool = True
    sync_roles: bool = True
    client_secret: str = ""
    scopes: str = "openid profile email"

    def __post_init__(self) -> None:
        if self.default_role not in {"deny", "viewer", "editor"}:
            raise OIDCError("Invalid default OIDC role")
        if not self.group_claim.strip():
            raise OIDCError("OIDC group claim cannot be blank")
        if self.scopes and "openid" not in self.scopes.split():
            object.__setattr__(self, "scopes", "openid " + self.scopes.strip())

    @property
    def configured(self) -> bool:
        return bool(self.issuer.strip() and self.client_id.strip())


@dataclass(frozen=True)
class OIDCIdentity:
    issuer: str
    subject: str
    username: str
    display_name: str
    email: str | None
    groups: tuple[str, ...]
    role: str


def parse_groups(value: str | None) -> tuple[str, ...]:
    """Parse comma/newline-separated group names without changing case."""
    if not value:
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for raw in _GROUP_SPLIT.split(value):
        group = raw.strip()
        if group and group not in seen:
            seen.add(group)
            result.append(group)
    return tuple(result)


def validate_issuer_url(url: str) -> None:
    """Require a clean HTTPS issuer URL unless an explicit dev override exists."""
    parsed = urlsplit(url)
    allow_http = bool(os.environ.get("SHELF_OIDC_ALLOW_INSECURE_HTTP"))
    valid_schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme not in valid_schemes:
        raise OIDCError("OIDC issuer must use HTTPS")
    if (
        not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise OIDCError("OIDC issuer URL is invalid")


def _validate_endpoint_url(url: str, label: str) -> None:
    parsed = urlsplit(url)
    allow_http = bool(os.environ.get("SHELF_OIDC_ALLOW_INSECURE_HTTP"))
    valid_schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme not in valid_schemes or not parsed.netloc:
        raise OIDCError(f"OIDC {label} is not a valid HTTPS URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise OIDCError(f"OIDC {label} is invalid")


async def discover(
    config: OIDCConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch and strictly validate the provider's discovery document."""
    if not config.configured:
        raise OIDCError("OIDC is not configured")
    validate_issuer_url(config.issuer)
    discovery_url = config.issuer.rstrip("/") + "/.well-known/openid-configuration"

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)
    try:
        response = await client.get(discovery_url, headers={"Accept": "application/json"})
        response.raise_for_status()
        metadata = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError("Could not retrieve the OpenID Connect provider configuration") from exc
    finally:
        if owns_client:
            await client.aclose()

    if not isinstance(metadata, dict) or metadata.get("issuer") != config.issuer:
        raise OIDCError("OIDC discovery issuer does not exactly match the configured issuer")

    for field, label in (
        ("authorization_endpoint", "authorization endpoint"),
        ("token_endpoint", "token endpoint"),
        ("jwks_uri", "JWKS endpoint"),
    ):
        endpoint = metadata.get(field)
        if not isinstance(endpoint, str):
            raise OIDCError(f"OIDC discovery is missing the {label}")
        _validate_endpoint_url(endpoint, label)

    response_types = metadata.get("response_types_supported")
    if isinstance(response_types, list) and "code" not in response_types:
        raise OIDCError("OIDC provider does not advertise Authorization Code flow support")
    pkce_methods = metadata.get("code_challenge_methods_supported")
    if isinstance(pkce_methods, list) and "S256" not in pkce_methods:
        raise OIDCError("OIDC provider does not advertise PKCE S256 support")
    return metadata


def _pkce_verifier() -> str:
    # token_urlsafe(64) produces an RFC 7636-compliant verifier length.
    return secrets.token_urlsafe(64)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _encode_flow(payload: dict[str, Any]) -> str:
    return encrypt_value(
        json.dumps(payload, separators=(",", ":")), get_encryption_key()
    )


def _decode_flow(value: str | None) -> dict[str, Any]:
    if not value:
        raise OIDCError("OIDC sign-in session is missing or expired")
    try:
        raw = decrypt_value(value, get_encryption_key(), key_name=FLOW_COOKIE)
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OIDCError("OIDC sign-in session is invalid") from exc
    if not isinstance(payload, dict):
        raise OIDCError("OIDC sign-in session is invalid")
    try:
        expires_at = float(payload.get("exp", 0))
    except (TypeError, ValueError) as exc:
        raise OIDCError("OIDC sign-in session is invalid") from exc
    if expires_at < time.time():
        raise OIDCError("OIDC sign-in session is missing or expired")
    return payload


def build_authorization_redirect(
    config: OIDCConfig,
    metadata: dict[str, Any],
    *,
    redirect_uri: str,
) -> tuple[str, dict[str, Any]]:
    """Build an Authorization Code + PKCE request and encrypted-cookie payload."""
    if not config.configured:
        raise OIDCError("OIDC is not configured")
    endpoint = metadata.get("authorization_endpoint")
    if not isinstance(endpoint, str):
        raise OIDCError("OIDC discovery is missing the authorization endpoint")
    _validate_endpoint_url(endpoint, "authorization endpoint")

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = _pkce_verifier()
    params = {
        "client_id": config.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": config.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    flow = {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "redirect_uri": redirect_uri,
        "issuer": config.issuer,
        "exp": time.time() + FLOW_TTL_SECONDS,
    }
    return endpoint + "?" + urlencode(params), flow


def validate_callback_state(flow: dict[str, Any], state: str | None) -> None:
    expected = flow.get("state")
    if not isinstance(expected, str) or not isinstance(state, str):
        raise OIDCError("OIDC state validation failed")
    if not secrets.compare_digest(expected, state):
        raise OIDCError("OIDC state validation failed")


def _claim_value(claims: dict[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def groups_from_claims(claims: dict[str, Any], claim_name: str) -> tuple[str, ...]:
    """Read a string/list group claim, including dotted nested claim paths."""
    value = _claim_value(claims, claim_name)
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(v) for v in value if isinstance(v, (str, int)))
    return ()


def role_from_groups(groups: tuple[str, ...], config: OIDCConfig) -> str | None:
    """Map provider groups to Shelf roles, always preferring highest privilege."""
    group_set = set(groups)
    for role, mapped_groups in (
        ("admin", config.admin_groups),
        ("editor", config.editor_groups),
        ("viewer", config.viewer_groups),
    ):
        if any(group in group_set for group in mapped_groups):
            return role
    if config.default_role == "deny":
        return None
    return config.default_role


def identity_from_claims(claims: dict[str, Any], config: OIDCConfig) -> OIDCIdentity:
    """Project validated OIDC claims into Shelf's identity/role vocabulary."""
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise OIDCError("OIDC subject claim is missing")

    groups = groups_from_claims(claims, config.group_claim)
    if config.required_group and config.required_group not in set(groups):
        raise OIDCAccessDenied(
            "Your account is not a member of the required Shelf access group"
        )

    role = role_from_groups(groups, config)
    if role is None:
        raise OIDCAccessDenied("Your OIDC groups do not grant access to Shelf")

    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    preferred = (
        claims.get("preferred_username")
        if isinstance(claims.get("preferred_username"), str)
        else ""
    )
    if not preferred and email:
        preferred = email.split("@", 1)[0]
    if not preferred:
        preferred = "oidc-user"

    username = _USERNAME_SAFE.sub("-", preferred).strip(".-_") or "oidc-user"
    display = claims.get("name") if isinstance(claims.get("name"), str) else ""
    display_name = display.strip() or preferred.strip() or username

    return OIDCIdentity(
        issuer=config.issuer,
        subject=subject,
        username=username[:64],
        display_name=display_name[:128],
        email=email[:320] if email else None,
        groups=groups,
        role=role,
    )
