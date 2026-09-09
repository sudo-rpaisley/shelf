"""OIDC token exchange and asymmetric ID-token validation.

This module intentionally stops at *validated claims*. It does not create or
update Shelf users: upstream's household/account model is still evolving, so
the protocol boundary can be tested now without binding OIDC identities to a
user table that is expected to change.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import jwt

from app.oidc import HTTP_TIMEOUT_SECONDS, OIDCConfig, OIDCError


# A Shelf client secret is never accepted as an ID-token verification key.
# Only asymmetric algorithms backed by the provider's JWKS are permitted.
ALLOWED_ID_TOKEN_ALGS = frozenset(
    {
        "RS256", "RS384", "RS512",
        "PS256", "PS384", "PS512",
        "ES256", "ES384", "ES512",
        "EdDSA",
    }
)


async def exchange_code(
    metadata: dict[str, Any],
    config: OIDCConfig,
    *,
    code: str,
    redirect_uri: str,
    verifier: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Exchange one Authorization Code using the PKCE verifier."""
    endpoint = metadata.get("token_endpoint")
    if not isinstance(endpoint, str):
        raise OIDCError("OIDC discovery is missing the token endpoint")

    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    auth: tuple[str, str] | None = None
    supported = metadata.get("token_endpoint_auth_methods_supported")
    if not isinstance(supported, list) or not supported:
        supported = ["client_secret_basic"]

    if config.client_secret:
        if "client_secret_basic" in supported:
            auth = (config.client_id, config.client_secret)
        elif "client_secret_post" in supported:
            data["client_id"] = config.client_id
            data["client_secret"] = config.client_secret
        else:
            raise OIDCError(
                "OIDC provider does not support a compatible client authentication method"
            )
    else:
        if "none" not in supported:
            raise OIDCError("OIDC provider requires a client secret")
        data["client_id"] = config.client_id

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)
    try:
        response = await client.post(
            endpoint,
            data=data,
            auth=auth,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        token = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError(
            "The OpenID Connect provider rejected the sign-in response"
        ) from exc
    finally:
        if owns_client:
            await client.aclose()

    if not isinstance(token, dict) or not isinstance(token.get("id_token"), str):
        raise OIDCError("OIDC token response did not contain an ID token")
    return token


async def fetch_jwks(
    metadata: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch and structurally validate the provider's signing-key set."""
    endpoint = metadata.get("jwks_uri")
    if not isinstance(endpoint, str):
        raise OIDCError("OIDC discovery is missing the JWKS endpoint")

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)
    try:
        response = await client.get(endpoint, headers={"Accept": "application/json"})
        response.raise_for_status()
        jwks = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError("Could not retrieve the OpenID Connect signing keys") from exc
    finally:
        if owns_client:
            await client.aclose()

    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise OIDCError("OIDC provider returned an invalid signing-key set")
    return jwks


def select_jwk(jwks: dict[str, Any], kid: str | None) -> jwt.PyJWK:
    """Select exactly one signature key, optionally by ``kid``."""
    candidates: list[dict[str, Any]] = []
    for key in jwks.get("keys", []):
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


def validate_id_token(
    id_token: str,
    metadata: dict[str, Any],
    config: OIDCConfig,
    *,
    expected_nonce: str,
    jwks: dict[str, Any],
) -> dict[str, Any]:
    """Verify signature, issuer, audience, lifetime, nonce and authorised party."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OIDCError("OIDC ID token header is invalid") from exc

    alg = header.get("alg")
    if alg not in ALLOWED_ID_TOKEN_ALGS:
        raise OIDCError("OIDC ID token uses a disallowed signing algorithm")

    advertised = metadata.get("id_token_signing_alg_values_supported")
    if isinstance(advertised, list) and alg not in advertised:
        raise OIDCError(
            "OIDC ID token signing algorithm was not advertised by the provider"
        )

    pyjwk = select_jwk(jwks, header.get("kid"))
    if pyjwk.algorithm_name and pyjwk.algorithm_name != alg:
        raise OIDCError("OIDC signing key algorithm does not match the ID token")

    try:
        claims = jwt.decode(
            id_token,
            key=pyjwk.key,
            algorithms=[alg],
            audience=config.client_id,
            issuer=str(metadata.get("issuer") or config.issuer),
            leeway=60,
            options={"require": ["iss", "sub", "aud", "exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise OIDCError("OIDC ID token validation failed") from exc

    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not secrets.compare_digest(nonce, expected_nonce):
        raise OIDCError("OIDC nonce validation failed")

    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != config.client_id:
        raise OIDCError("OIDC authorised-party validation failed")
    return claims


async def userinfo_claims(
    metadata: dict[str, Any],
    token: dict[str, Any],
    *,
    subject: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch optional UserInfo claims and bind them to the ID-token subject."""
    endpoint = metadata.get("userinfo_endpoint")
    access_token = token.get("access_token")
    if not endpoint or not isinstance(access_token, str):
        return {}

    token_type = token.get("token_type")
    if token_type is not None and (
        not isinstance(token_type, str) or token_type.casefold() != "bearer"
    ):
        raise OIDCError("OIDC provider returned an unsupported access-token type")

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)
    try:
        response = await client.get(
            str(endpoint),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        claims = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError("Could not retrieve OIDC UserInfo claims") from exc
    finally:
        if owns_client:
            await client.aclose()

    if not isinstance(claims, dict) or claims.get("sub") != subject:
        raise OIDCError("OIDC UserInfo subject does not match the ID token")
    return claims
