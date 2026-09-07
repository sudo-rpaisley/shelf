import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.oidc import OIDCConfig, OIDCError
from app import oidc_tokens


ISSUER = "https://id.example.test/application/o/shelf/"
TOKEN = "https://id.example.test/application/o/token/"
JWKS = "https://id.example.test/application/o/shelf/jwks/"
USERINFO = "https://id.example.test/application/o/userinfo/"
CLIENT_ID = "shelf-client"


def _config(**overrides):
    values = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": "secret",
    }
    values.update(overrides)
    return OIDCConfig(**values)


def _metadata(**overrides):
    values = {
        "issuer": ISSUER,
        "token_endpoint": TOKEN,
        "jwks_uri": JWKS,
        "userinfo_endpoint": USERINFO,
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    values.update(overrides)
    return values


def _rsa_token(*, nonce="nonce-1", aud=CLIENT_ID, azp=None):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk.update({"kid": "key-1", "use": "sig", "alg": "RS256"})
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "subject-123",
        "aud": aud,
        "iat": now,
        "exp": now + 300,
        "nonce": nonce,
    }
    if azp is not None:
        claims["azp"] = azp
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "key-1"})
    return token, {"keys": [public_jwk]}


@pytest.mark.asyncio
async def test_exchange_code_uses_basic_auth_and_pkce_verifier():
    seen = {}

    def handler(request: httpx.Request):
        seen["request"] = request
        return httpx.Response(200, json={"id_token": "signed-token", "access_token": "access"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await oidc_tokens.exchange_code(
            _metadata(),
            _config(),
            code="auth-code",
            redirect_uri="https://shelf.example.test/login",
            verifier="pkce-verifier",
            client=client,
        )

    assert result["id_token"] == "signed-token"
    request = seen["request"]
    assert request.url == TOKEN
    assert request.headers["authorization"].startswith("Basic ")
    body = request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "code=auth-code" in body
    assert "code_verifier=pkce-verifier" in body
    assert "client_secret=secret" not in body


@pytest.mark.asyncio
async def test_exchange_code_can_use_client_secret_post_when_required():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"id_token": "signed-token"})

    metadata = _metadata(token_endpoint_auth_methods_supported=["client_secret_post"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await oidc_tokens.exchange_code(
            metadata,
            _config(),
            code="auth-code",
            redirect_uri="https://shelf.example.test/login",
            verifier="verifier",
            client=client,
        )

    assert "client_id=shelf-client" in seen["body"]
    assert "client_secret=secret" in seen["body"]


@pytest.mark.asyncio
async def test_public_client_requires_none_auth_method():
    with pytest.raises(OIDCError, match="requires a client secret"):
        await oidc_tokens.exchange_code(
            _metadata(token_endpoint_auth_methods_supported=["client_secret_basic"]),
            _config(client_secret=""),
            code="code",
            redirect_uri="https://shelf.example.test/login",
            verifier="verifier",
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        )


@pytest.mark.asyncio
async def test_fetch_jwks_requires_a_key_list():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"not_keys": []}))
    ) as client:
        with pytest.raises(OIDCError, match="invalid signing-key set"):
            await oidc_tokens.fetch_jwks(_metadata(), client=client)


def test_valid_rs256_id_token_is_accepted():
    token, jwks = _rsa_token()
    claims = oidc_tokens.validate_id_token(
        token,
        _metadata(),
        _config(),
        expected_nonce="nonce-1",
        jwks=jwks,
    )
    assert claims["sub"] == "subject-123"


def test_symmetric_id_token_algorithm_is_rejected_before_key_selection():
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "subject-123",
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": "nonce-1",
        },
        "client-secret-must-not-be-a-verification-key",
        algorithm="HS256",
        headers={"kid": "sym"},
    )
    with pytest.raises(OIDCError, match="disallowed signing algorithm"):
        oidc_tokens.validate_id_token(
            token,
            _metadata(id_token_signing_alg_values_supported=["HS256"]),
            _config(),
            expected_nonce="nonce-1",
            jwks={"keys": []},
        )


def test_nonce_mismatch_is_rejected_after_signature_validation():
    token, jwks = _rsa_token(nonce="provider-nonce")
    with pytest.raises(OIDCError, match="nonce validation failed"):
        oidc_tokens.validate_id_token(
            token,
            _metadata(),
            _config(),
            expected_nonce="browser-flow-nonce",
            jwks=jwks,
        )


def test_multiple_audiences_require_matching_authorised_party():
    token, jwks = _rsa_token(aud=[CLIENT_ID, "another-client"], azp="another-client")
    with pytest.raises(OIDCError, match="authorised-party validation failed"):
        oidc_tokens.validate_id_token(
            token,
            _metadata(),
            _config(),
            expected_nonce="nonce-1",
            jwks=jwks,
        )


def test_key_selection_refuses_ambiguous_key_set_without_kid():
    _, first = _rsa_token()
    _, second = _rsa_token()
    for key in first["keys"] + second["keys"]:
        key.pop("kid", None)
    with pytest.raises(OIDCError, match="uniquely identified"):
        oidc_tokens.select_jwk({"keys": first["keys"] + second["keys"]}, None)


@pytest.mark.asyncio
async def test_userinfo_subject_must_match_id_token_subject():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"sub": "different-subject", "name": "Alice"})
        )
    ) as client:
        with pytest.raises(OIDCError, match="subject does not match"):
            await oidc_tokens.userinfo_claims(
                _metadata(),
                {"access_token": "access", "token_type": "Bearer"},
                subject="subject-123",
                client=client,
            )
