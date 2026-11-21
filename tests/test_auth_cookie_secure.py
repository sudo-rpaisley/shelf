"""The auth cookies' Secure flag follows the request's scheme (#124).

The shared `client` fixture's base URL is https://testserver, so every request
here names its scheme explicitly rather than inheriting it (a relative URL would
silently test the https branch twice).
"""

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest

from app.auth import get_secret_key
from app.config import JWT_ALGORITHM, JWT_EXPIRY_SECONDS


def _cookie_headers(resp) -> dict[str, str]:
    """Map cookie name -> its raw Set-Cookie header (the jar hides the Secure flag)."""
    out = {}
    for header in resp.headers.get_list("set-cookie"):
        out[header.split("=", 1)[0].strip()] = header
    return out


def _is_secure(header: str) -> bool:
    return any(part.strip().lower() == "secure" for part in header.split(";"))


@pytest.mark.parametrize("scheme,secure", [("http", False), ("https", True)])
def test_login_cookie_secure_follows_scheme(client, admin_user, scheme, secure):
    resp = client.post(
        f"{scheme}://testserver/login",
        data={"username": "admin", "password": "password123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cookies = _cookie_headers(resp)
    assert _is_secure(cookies["access_token"]) is secure
    assert _is_secure(cookies["csrf_token"]) is secure
    assert "httponly" in cookies["access_token"].lower()
    assert "samesite=strict" in cookies["access_token"].lower()


@pytest.mark.parametrize("scheme,secure", [("http", False), ("https", True)])
def test_setup_cookie_secure_follows_scheme(client, scheme, secure):
    resp = client.post(
        f"{scheme}://testserver/setup",
        data={
            "username": "admin",
            "display_name": "Admin",
            "password": "password123",
            "password_confirm": "password123",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cookies = _cookie_headers(resp)
    assert _is_secure(cookies["access_token"]) is secure
    assert _is_secure(cookies["csrf_token"]) is secure


@pytest.mark.parametrize("scheme,secure", [("http", False), ("https", True)])
def test_sliding_refresh_reissues_with_request_scheme(client, admin_user, scheme, secure):
    iat = datetime.now(timezone.utc) - timedelta(seconds=JWT_EXPIRY_SECONDS * 0.6)
    payload = {
        "sub": str(admin_user["id"]),
        "username": admin_user["username"],
        "role": admin_user["role"],
        "display_name": admin_user["display_name"],
        "tv": 1,
        "iat": iat,
        "exp": iat + timedelta(seconds=JWT_EXPIRY_SECONDS),
    }
    client.cookies.set("access_token", pyjwt.encode(payload, get_secret_key(), algorithm=JWT_ALGORITHM))

    resp = client.get(f"{scheme}://testserver/browse", follow_redirects=False)
    assert resp.status_code == 200
    cookies = _cookie_headers(resp)
    assert "access_token" in cookies, "past half-life, the middleware must re-issue"
    assert _is_secure(cookies["access_token"]) is secure
