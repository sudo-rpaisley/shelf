"""OpenID Connect authentication support for Shelf.

Shelf remains the session/authorisation authority after sign-in: OIDC proves
identity and supplies groups, then Shelf issues its existing local JWT.  The
implementation uses Authorization Code + PKCE (S256), state and nonce, strict
ID-token validation, optional UserInfo claims, and stable issuer+subject
identity keys.

No provider-specific assumptions are made.  Authentik, Keycloak, Dex and
other conforming OpenID Providers can be configured from Settings -> Users.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import Request, Response

from app.auth import hash_password
from app.crypto import decrypt_value, encrypt_value, get_encryption_key
from app.database import get_db, get_setting

logger = logging.getLogger(__name__)

FLOW_COOKIE = "oidc_flow"
FLOW_TTL_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 10.0

# Symmetric ID-token algorithms are deliberately excluded.  A Shelf client
# secret must never double as an ID-token verification key; public-key JWKS
# validation avoids algorithm/key-confusion mistakes and is supported by
# conforming OIDC providers (RS256 is the OIDC baseline algorithm).
ALLOWED_ID_TOKEN_ALGS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)

_USERNAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_GROUP_SPLIT = re.compile(r"[\r\n,]+")


class OIDCError(Exception):
    """A safe, user-presentable OIDC failure."""


class OIDCAccessDenied(OIDCError):
    """Authentication succeeded but Shelf access policy denied the user."""


@dataclass(frozen=True)
class OIDCConfig:
    enabled: bool
    provider_name: str
    issuer: str
    client_id: str
    client_secret: str
    scopes: str
    group_claim: str
    required_group: str
    admin_groups: tuple[str, ...]
    editor_groups: tuple[str, ...]
    viewer_groups: tuple[str, ...]
    default_role: str
    auto_provision: bool
    sync_roles: bool

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.client_id)


@dataclass(frozen=True)
class OIDCIdentity:
    issuer: str
    subject: str
    username: str
    display_name: str
    email: str | None
    groups: tuple[str, ...]
    role: str


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_groups(value: str | None) -> tuple[str, ...]:
    """Parse comma/newline-separated group names, retaining declaration order."""
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


def get_oidc_config() -> OIDCConfig:
    with get_db() as db:
        enabled = _as_bool(get_setting(db, "oidc_enabled"), False)
        provider_name = (get_setting(db, "oidc_provider_name") or "OpenID Connect").strip()
        issuer = (get_setting(db, "oidc_issuer") or "").strip()
        client_id = (get_setting(db, "oidc_client_id") or "").strip()
        client_secret = get_setting(db, "oidc_client_secret") or ""
        scopes = (get_setting(db, "oidc_scopes") or "openid profile email").strip()
        group_claim = (get_setting(db, "oidc_group_claim") or "groups").strip() or "groups"
        required_group = (get_setting(db, "oidc_required_group") or "").strip()
        admin_groups = parse_groups(get_setting(db, "oidc_admin_groups"))
        editor_groups = parse_groups(get_setting(db, "oidc_editor_groups"))
        viewer_groups = parse_groups(get_setting(db, "oidc_viewer_groups"))
        default_role = (get_setting(db, "oidc_default_role") or "deny").strip().lower()
        auto_provision = _as_bool(get_setting(db, "oidc_auto_provision"), True)
        sync_roles = _as_bool(get_setting(db, "oidc_sync_roles"), True)

    if default_role not in {"deny", "viewer", "editor"}:
        default_role = "deny"
    scope_parts = scopes.split()
    if "openid" not in scope_parts:
        scopes = "openid " + scopes

    return OIDCConfig(
        enabled=enabled,
        provider_name=provider_name or "OpenID Connect",
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        group_claim=group_claim,
        required_group=required_group,
        admin_groups=admin_groups,
        editor_groups=editor_groups,
        viewer_groups=viewer_groups,
        default_role=default_role,
        auto_provision=auto_provision,
        sync_roles=sync_roles,
    )


def _upsert_setting(db, key: str, value: str, *, sensitive: bool = False) -> None:
    stored = value
    if sensitive and value:
        stored = encrypt_value(value, get_encryption_key())
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, stored),
    )


def save_oidc_config(values: dict[str, str | bool]) -> None:
    """Validate and persist OIDC settings.

    A blank client-secret submission keeps the existing write-only secret;
    ``clear_client_secret`` explicitly removes it.
    """
    issuer = str(values.get("issuer") or "").strip()
    client_id = str(values.get("client_id") or "").strip()
    enabled = bool(values.get("enabled"))
    default_role = str(values.get("default_role") or "deny").strip().lower()
    if default_role not in {"deny", "viewer", "editor"}:
        raise OIDCError("Invalid default OIDC role")
    if issuer:
        validate_issuer_url(issuer)
    if enabled and (not issuer or not client_id):
        raise OIDCError("Issuer URL and Client ID are required before OIDC can be enabled")

    scopes = str(values.get("scopes") or "openid profile email").strip()
    if "openid" not in scopes.split():
        scopes = "openid " + scopes

    with get_db() as db:
        rows = {
            "oidc_enabled": "1" if enabled else "0",
            "oidc_provider_name": str(values.get("provider_name") or "OpenID Connect").strip() or "OpenID Connect",
            "oidc_issuer": issuer,
            "oidc_client_id": client_id,
            "oidc_scopes": scopes,
            "oidc_group_claim": str(values.get("group_claim") or "groups").strip() or "groups",
            "oidc_required_group": str(values.get("required_group") or "").strip(),
            "oidc_admin_groups": str(values.get("admin_groups") or "").strip(),
            "oidc_editor_groups": str(values.get("editor_groups") or "").strip(),
            "oidc_viewer_groups": str(values.get("viewer_groups") or "").strip(),
            "oidc_default_role": default_role,
            "oidc_auto_provision": "1" if bool(values.get("auto_provision")) else "0",
            "oidc_sync_roles": "1" if bool(values.get("sync_roles")) else "0",
        }
        for key, value in rows.items():
            _upsert_setting(db, key, value)

        if bool(values.get("clear_client_secret")):
            db.execute("DELETE FROM settings WHERE key = 'oidc_client_secret'")
        else:
            client_secret = str(values.get("client_secret") or "")
            if client_secret:
                _upsert_setting(db, "oidc_client_secret", client_secret, sensitive=True)


def validate_issuer_url(url: str) -> None:
    parsed = urlsplit(url)
    allow_http = bool(os.environ.get("SHELF_OIDC_ALLOW_INSECURE_HTTP"))
    if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
        raise OIDCError("OIDC issuer must use HTTPS")
    if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise OIDCError("OIDC issuer URL is invalid")


def _validate_endpoint_url(url: str, label: str) -> None:
    parsed = urlsplit(url)
    allow_http = bool(os.environ.get("SHELF_OIDC_ALLOW_INSECURE_HTTP"))
    if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}) or not parsed.netloc:
        raise OIDCError(f"OIDC {label} is not a valid HTTPS URL")


async def discover(config: OIDCConfig) -> dict[str, Any]:
    if not config.configured:
        raise OIDCError("OIDC is not configured")
    validate_issuer_url(config.issuer)
    discovery_url = config.issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.get(discovery_url, headers={"Accept": "application/json"})
            response.raise_for_status()
            metadata = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("OIDC discovery failed for issuer %s: %s", config.issuer, type(exc).__name__)
        raise OIDCError("Could not retrieve the OpenID Connect provider configuration") from exc

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
    if metadata.get("userinfo_endpoint"):
        _validate_endpoint_url(str(metadata["userinfo_endpoint"]), "UserInfo endpoint")
    response_types = metadata.get("response_types_supported")
    if isinstance(response_types, list) and "code" not in response_types:
        raise OIDCError("OIDC provider does not advertise Authorization Code flow support")
    pkce_methods = metadata.get("code_challenge_methods_supported")
    if isinstance(pkce_methods, list) and "S256" not in pkce_methods:
        raise OIDCError("OIDC provider does not advertise PKCE S256 support")
    return metadata


def _pkce_verifier() -> str:
    # token_urlsafe(64) produces an RFC 7636-compliant 86-character verifier.
    return secrets.token_urlsafe(64)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _encode_flow(payload: dict[str, Any]) -> str:
    return encrypt_value(json.dumps(payload, separators=(",", ":")), get_encryption_key())


def _decode_flow(value: str | None) -> dict[str, Any]:
    if not value:
        raise OIDCError("OIDC sign-in session is missing or expired")
    try:
        payload = json.loads(decrypt_value(value, get_encryption_key()))
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


def set_flow_cookie(response: Response, payload: dict[str, Any]) -> None:
    secure = not bool(os.environ.get("SHELF_DEV_INSECURE_COOKIES"))
    response.set_cookie(
        FLOW_COOKIE,
        _encode_flow(payload),
        max_age=FLOW_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/login",
    )


def clear_flow_cookie(response: Response) -> None:
    response.delete_cookie(FLOW_COOKIE, path="/login")


async def authorization_redirect(request: Request, config: OIDCConfig) -> tuple[str, dict[str, Any]]:
    if not config.enabled or not config.configured:
        raise OIDCError("OIDC sign-in is not enabled")
    metadata = await discover(config)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = _pkce_verifier()
    redirect_uri = str(request.url_for("login_page"))
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
    return str(metadata["authorization_endpoint"]) + "?" + urlencode(params), flow


async def _exchange_code(
    metadata: dict[str, Any], config: OIDCConfig, code: str, redirect_uri: str, verifier: str
) -> dict[str, Any]:
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    auth: tuple[str, str] | None = None
    supported = metadata.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if config.client_secret:
        if "client_secret_basic" in supported:
            auth = (config.client_id, config.client_secret)
        elif "client_secret_post" in supported:
            data["client_id"] = config.client_id
            data["client_secret"] = config.client_secret
        else:
            raise OIDCError("OIDC provider does not support a compatible client authentication method")
    else:
        if "none" not in supported:
            raise OIDCError("OIDC provider requires a client secret")
        data["client_id"] = config.client_id

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(
                str(metadata["token_endpoint"]), data=data, auth=auth, headers={"Accept": "application/json"}
            )
            response.raise_for_status()
            token = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("OIDC token exchange failed: %s", type(exc).__name__)
        raise OIDCError("The OpenID Connect provider rejected the sign-in response") from exc
    if not isinstance(token, dict) or not isinstance(token.get("id_token"), str):
        raise OIDCError("OIDC token response did not contain an ID token")
    return token


async def _fetch_jwks(metadata: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.get(str(metadata["jwks_uri"]), headers={"Accept": "application/json"})
            response.raise_for_status()
            jwks = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError("Could not retrieve the OpenID Connect signing keys") from exc
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise OIDCError("OIDC provider returned an invalid signing-key set")
    return jwks


def _select_jwk(jwks: dict[str, Any], kid: str | None) -> Any:
    candidates = []
    for key in jwks["keys"]:
        if not isinstance(key, dict):
            continue
        if key.get("use") not in (None, "sig"):
            continue
        if kid is not None and key.get("kid") != kid:
            continue
        candidates.append(key)
    if len(candidates) != 1:
        raise OIDCError("OIDC signing key could not be uniquely identified")
    try:
        return jwt.PyJWK.from_dict(candidates[0])
    except (jwt.PyJWTError, ValueError) as exc:
        raise OIDCError("OIDC provider returned an unsupported signing key") from exc


async def _validate_id_token(
    id_token: str, metadata: dict[str, Any], config: OIDCConfig, expected_nonce: str
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OIDCError("OIDC ID token header is invalid") from exc
    alg = header.get("alg")
    if alg not in ALLOWED_ID_TOKEN_ALGS:
        raise OIDCError("OIDC ID token uses a disallowed signing algorithm")
    advertised = metadata.get("id_token_signing_alg_values_supported")
    if isinstance(advertised, list) and alg not in advertised:
        raise OIDCError("OIDC ID token signing algorithm was not advertised by the provider")

    jwks = await _fetch_jwks(metadata)
    pyjwk = _select_jwk(jwks, header.get("kid"))
    if pyjwk.algorithm_name and pyjwk.algorithm_name != alg:
        raise OIDCError("OIDC signing key algorithm does not match the ID token")
    try:
        claims = jwt.decode(
            id_token,
            key=pyjwk.key,
            algorithms=[alg],
            audience=config.client_id,
            issuer=str(metadata["issuer"]),
            leeway=60,
            options={"require": ["iss", "sub", "aud", "exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        logger.warning("OIDC ID token validation failed: %s", type(exc).__name__)
        raise OIDCError("OIDC ID token validation failed") from exc

    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not secrets.compare_digest(nonce, expected_nonce):
        raise OIDCError("OIDC nonce validation failed")
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != config.client_id:
        raise OIDCError("OIDC authorised-party validation failed")
    return claims


async def _userinfo(metadata: dict[str, Any], token: dict[str, Any], subject: str) -> dict[str, Any]:
    endpoint = metadata.get("userinfo_endpoint")
    access_token = token.get("access_token")
    if not endpoint or not isinstance(access_token, str):
        return {}
    token_type = token.get("token_type")
    if token_type is not None and (not isinstance(token_type, str) or token_type.casefold() != "bearer"):
        raise OIDCError("OIDC provider returned an unsupported access-token type")
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.get(
                str(endpoint),
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            response.raise_for_status()
            claims = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError("Could not retrieve OIDC UserInfo claims") from exc
    if not isinstance(claims, dict) or claims.get("sub") != subject:
        raise OIDCError("OIDC UserInfo subject does not match the ID token")
    return claims


def _claim_value(claims: dict[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def groups_from_claims(claims: dict[str, Any], claim_name: str) -> tuple[str, ...]:
    value = _claim_value(claims, claim_name)
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(v) for v in value if isinstance(v, (str, int)))
    return ()


def role_from_groups(groups: tuple[str, ...], config: OIDCConfig) -> str | None:
    group_set = set(groups)
    mappings = (
        ("admin", config.admin_groups),
        ("editor", config.editor_groups),
        ("viewer", config.viewer_groups),
    )
    for role, mapped_groups in mappings:
        if any(group in group_set for group in mapped_groups):
            return role
    if config.default_role == "deny":
        return None
    return config.default_role


def identity_from_claims(claims: dict[str, Any], config: OIDCConfig) -> OIDCIdentity:
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise OIDCError("OIDC subject claim is missing")
    groups = groups_from_claims(claims, config.group_claim)
    if config.required_group and config.required_group not in set(groups):
        raise OIDCAccessDenied("Your account is not a member of the required Shelf access group")
    role = role_from_groups(groups, config)
    if role is None:
        raise OIDCAccessDenied("Your OIDC groups do not grant access to Shelf")

    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    preferred = claims.get("preferred_username") if isinstance(claims.get("preferred_username"), str) else ""
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


def _unique_username(db, base: str) -> str:
    candidate = base[:64]
    if not db.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)).fetchone():
        return candidate
    stem = base[:55] or "oidc-user"
    for i in range(1, 10000):
        suffix = "-oidc" if i == 1 else f"-oidc{i}"
        candidate = (stem[: 64 - len(suffix)] + suffix)[:64]
        if not db.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)).fetchone():
            return candidate
    raise OIDCError("Could not allocate a unique Shelf username")


def provision_or_sync(identity: OIDCIdentity, config: OIDCConfig) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            "SELECT u.id, u.username, u.display_name, u.role, u.token_version "
            "FROM user_identities ui JOIN users u ON u.id = ui.user_id "
            "WHERE ui.issuer = ? AND ui.subject = ?",
            (identity.issuer, identity.subject),
        ).fetchone()

        if not row:
            if not config.auto_provision:
                raise OIDCAccessDenied("Your OIDC identity is valid but is not provisioned in Shelf")
            username = _unique_username(db, identity.username)
            # OIDC-only accounts get an unknowable random local credential. A
            # Shelf admin can deliberately set a local fallback password later;
            # there is never a shared/default password to attack.
            disabled_local_password = hash_password(secrets.token_urlsafe(48))
            db.execute(
                "INSERT INTO users (username, password, display_name, role) VALUES (?, ?, ?, ?)",
                (username, disabled_local_password, identity.display_name, identity.role),
            )
            user_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            db.execute(
                "INSERT INTO user_identities (user_id, provider, issuer, subject, email, last_login_at) "
                "VALUES (?, 'oidc', ?, ?, ?, datetime('now'))",
                (user_id, identity.issuer, identity.subject, identity.email),
            )
            row = db.execute(
                "SELECT id, username, display_name, role, token_version FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            logger.info("Provisioned OIDC user '%s' with role '%s'", username, identity.role)
        else:
            role_changed = config.sync_roles and row["role"] != identity.role
            if role_changed and row["role"] == "admin" and identity.role != "admin":
                admin_count = db.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'admin'").fetchone()["c"]
                if admin_count <= 1:
                    raise OIDCError(
                        "OIDC role synchronisation would demote the last Shelf administrator; "
                        "create or restore a break-glass admin first"
                    )
            if config.sync_roles:
                db.execute(
                    "UPDATE users SET role = ?, display_name = ?, "
                    "token_version = token_version + ?, updated_at = datetime('now') WHERE id = ?",
                    (identity.role, identity.display_name, 1 if role_changed else 0, row["id"]),
                )
            else:
                db.execute(
                    "UPDATE users SET display_name = ?, updated_at = datetime('now') WHERE id = ?",
                    (identity.display_name, row["id"]),
                )
            db.execute(
                "UPDATE user_identities SET email = ?, last_login_at = datetime('now'), "
                "updated_at = datetime('now') WHERE issuer = ? AND subject = ?",
                (identity.email, identity.issuer, identity.subject),
            )
            row = db.execute(
                "SELECT id, username, display_name, role, token_version FROM users WHERE id = ?", (row["id"],)
            ).fetchone()

        return dict(row)


async def complete_login(request: Request, config: OIDCConfig) -> dict[str, Any]:
    # Validate state for every authorization response, including provider error
    # responses. Error callbacks are just as cross-site as success callbacks
    # and must be bound to a Shelf-initiated flow before they are trusted.
    state = request.query_params.get("state")
    if not state:
        raise OIDCError("OIDC callback is missing state")

    flow = _decode_flow(request.cookies.get(FLOW_COOKIE))
    expected_state = flow.get("state")
    if not isinstance(expected_state, str) or not secrets.compare_digest(state, expected_state):
        raise OIDCError("OIDC state validation failed")
    if flow.get("issuer") != config.issuer:
        raise OIDCError("OIDC provider changed during sign-in")

    error = request.query_params.get("error")
    if error:
        if error == "access_denied":
            raise OIDCAccessDenied("Sign-in was cancelled or denied by the identity provider")
        raise OIDCError("The identity provider returned an authentication error")

    code = request.query_params.get("code")
    if not code:
        raise OIDCError("OIDC callback is missing the authorization code")

    metadata = await discover(config)
    token = await _exchange_code(
        metadata, config, code, str(flow["redirect_uri"]), str(flow["verifier"])
    )
    id_claims = await _validate_id_token(
        str(token["id_token"]), metadata, config, str(flow["nonce"])
    )
    subject = str(id_claims["sub"])
    userinfo = await _userinfo(metadata, token, subject)
    # ID-token security claims remain authoritative; UserInfo may enrich
    # profile/group claims but may not replace the validated subject.
    claims = dict(id_claims)
    for key, value in userinfo.items():
        if key not in {"iss", "aud", "exp", "iat", "nonce", "azp", "sub"}:
            claims[key] = value
    claims["sub"] = subject
    identity = identity_from_claims(claims, config)
    return provision_or_sync(identity, config)


def managed_user_ids() -> set[int]:
    config = get_oidc_config()
    if not config.sync_roles:
        return set()
    with get_db() as db:
        return {row["user_id"] for row in db.execute("SELECT DISTINCT user_id FROM user_identities").fetchall()}


def is_role_managed(user_id: int) -> bool:
    if not get_oidc_config().sync_roles:
        return False
    with get_db() as db:
        return bool(db.execute("SELECT 1 FROM user_identities WHERE user_id = ? LIMIT 1", (user_id,)).fetchone())
