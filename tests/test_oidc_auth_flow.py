"""Tests for the standards-based OIDC authorisation-flow layer."""

import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.oidc import (
    OIDCConfig,
    OIDCError,
    _decode_flow,
    _encode_flow,
    build_authorization_redirect,
    discover,
    validate_callback_state,
)


ISSUER = "https://idp.example/application/o/shelf/"


def _config(**overrides):
    values = {
        "issuer": ISSUER,
        "client_id": "shelf-client",
        "scopes": "profile email groups",
    }
    values.update(overrides)
    return OIDCConfig(**values)


def _metadata(**overrides):
    values = {
        "issuer": ISSUER,
        "authorization_endpoint": "https://idp.example/authorize",
        "token_endpoint": "https://idp.example/token",
        "jwks_uri": "https://idp.example/jwks",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_discovery_requires_exact_issuer_match():
    async def handler(request):
        return httpx.Response(200, json=_metadata(issuer="https://idp.example/wrong"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OIDCError, match="exactly match"):
            await discover(_config(), client=client)


@pytest.mark.asyncio
async def test_discovery_requires_code_and_pkce_s256():
    async def no_code(request):
        return httpx.Response(200, json=_metadata(response_types_supported=["id_token"]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_code)) as client:
        with pytest.raises(OIDCError, match="Authorization Code"):
            await discover(_config(), client=client)

    async def no_s256(request):
        return httpx.Response(200, json=_metadata(code_challenge_methods_supported=["plain"]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_s256)) as client:
        with pytest.raises(OIDCError, match="PKCE S256"):
            await discover(_config(), client=client)


@pytest.mark.asyncio
async def test_discovery_accepts_valid_provider_metadata():
    seen = {}

    async def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_metadata())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        metadata = await discover(_config(), client=client)

    assert metadata["issuer"] == ISSUER
    assert seen["url"] == ISSUER.rstrip("/") + "/.well-known/openid-configuration"


def test_authorization_request_uses_code_pkce_state_and_nonce():
    url, flow = build_authorization_redirect(
        _config(),
        _metadata(),
        redirect_uri="https://shelf.example/login",
    )
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert params["client_id"] == ["shelf-client"]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == ["https://shelf.example/login"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"][0]
    assert params["state"] == [flow["state"]]
    assert params["nonce"] == [flow["nonce"]]
    assert "openid" in params["scope"][0].split()
    assert flow["verifier"]
    assert flow["issuer"] == ISSUER


def test_flow_cookie_payload_round_trips_encrypted():
    flow = {
        "state": "state-1",
        "nonce": "nonce-1",
        "verifier": "verifier-1",
        "redirect_uri": "https://shelf.example/login",
        "issuer": ISSUER,
        "exp": time.time() + 60,
    }
    token = _encode_flow(flow)

    assert token != str(flow)
    assert token.startswith("gAAAAA")
    decoded = _decode_flow(token)
    assert decoded["state"] == "state-1"
    assert decoded["nonce"] == "nonce-1"
    assert decoded["verifier"] == "verifier-1"


def test_expired_flow_is_rejected():
    token = _encode_flow({"state": "x", "exp": time.time() - 1})
    with pytest.raises(OIDCError, match="expired"):
        _decode_flow(token)


def test_callback_state_uses_exact_match():
    validate_callback_state({"state": "expected"}, "expected")
    with pytest.raises(OIDCError, match="state validation failed"):
        validate_callback_state({"state": "expected"}, "attacker")
