"""Provider-neutral OpenID Connect identity policy for Shelf (#89).

This module deliberately starts with the security/policy boundary that Shelf
must own regardless of provider: issuer validation, group claim extraction,
required-group access control and deterministic mapping to Shelf's existing
admin/editor/viewer roles.

Transport, Authorization Code + PKCE, token validation and local-account
provisioning are layered on top of this core rather than embedding provider-
specific assumptions here. Authentik, Authelia, Keycloak, Dex and other
conforming providers can therefore use the same Shelf policy.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


_USERNAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_GROUP_SPLIT = re.compile(r"[\r\n,]+")


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

    def __post_init__(self) -> None:
        if self.default_role not in {"deny", "viewer", "editor"}:
            raise OIDCError("Invalid default OIDC role")
        if not self.group_claim.strip():
            raise OIDCError("OIDC group claim cannot be blank")


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
