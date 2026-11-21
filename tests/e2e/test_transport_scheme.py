"""HSTS and the auth cookie's Secure flag follow the request scheme (#124).

This is the path no unit test reaches: uvicorn's proxy-headers middleware,
which trusts 127.0.0.1 when FORWARDED_ALLOW_IPS is unset. `server_factory`
clears that variable, so a host value cannot change what is exercised.
"""
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

pytestmark = pytest.mark.e2e


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at the first hop; the 303 surfaces as an HTTPError we read headers from."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_no_redirect(request: urllib.request.Request):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        return opener.open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        return exc


def _get(url: str, *, forwarded_proto: str | None = None):
    request = urllib.request.Request(url, method="GET")
    if forwarded_proto is not None:
        request.add_header("X-Forwarded-Proto", forwarded_proto)
    return _open_no_redirect(request)


def _post(url: str, data: dict, *, forwarded_proto: str | None = None):
    request = urllib.request.Request(
        url, data=urlencode(data).encode(), method="POST"
    )
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    if forwarded_proto is not None:
        request.add_header("X-Forwarded-Proto", forwarded_proto)
    return _open_no_redirect(request)


def _set_cookie_headers(response) -> list[str]:
    cookies = response.headers.get_all("Set-Cookie") or []
    names = {c.split("=", 1)[0].strip() for c in cookies}
    assert {"access_token", "csrf_token"} <= names, cookies
    return cookies


def _cookie_is_secure(set_cookie_header: str) -> bool:
    parts = [p.strip().lower() for p in set_cookie_header.split(";")]
    return "secure" in parts


# --- HSTS: SecurityHeadersMiddleware, on every response including a redirect ---


def test_hsts_present_when_forwarded_proto_is_https(server_factory):
    server = server_factory()
    resp = _get(f"{server['url']}/login", forwarded_proto="https")
    assert resp.headers.get("Strict-Transport-Security"), (
        "expected Strict-Transport-Security with X-Forwarded-Proto: https"
    )


def test_hsts_absent_without_forwarded_proto(server_factory):
    server = server_factory()
    resp = _get(f"{server['url']}/login")
    assert resp.headers.get("Strict-Transport-Security") is None, (
        "HSTS must not be sent over a plain http request"
    )


# --- Cookie Secure flag: auth.py's set_auth_cookie, via /setup and /login ---


def test_setup_cookies_are_secure_when_forwarded_proto_is_https(server_factory):
    server = server_factory()
    resp = _post(
        f"{server['url']}/setup",
        {
            "username": "e2e-scheme-admin",
            "display_name": "Scheme Admin",
            "password": "e2epassword1",
            "password_confirm": "e2epassword1",
        },
        forwarded_proto="https",
    )
    cookies = _set_cookie_headers(resp)
    for cookie in cookies:
        assert _cookie_is_secure(cookie), f"expected Secure on: {cookie}"


def test_login_cookies_are_secure_only_when_forwarded_proto_is_https(server_factory):
    server = server_factory()
    setup_resp = _post(
        f"{server['url']}/setup",
        {
            "username": "e2e-scheme-admin2",
            "display_name": "Scheme Admin 2",
            "password": "e2epassword1",
            "password_confirm": "e2epassword1",
        },
    )
    assert setup_resp.status in (302, 303), setup_resp.status

    resp_https = _post(
        f"{server['url']}/login",
        {"username": "e2e-scheme-admin2", "password": "e2epassword1"},
        forwarded_proto="https",
    )
    cookies_https = _set_cookie_headers(resp_https)
    for cookie in cookies_https:
        assert _cookie_is_secure(cookie), f"expected Secure on: {cookie}"

    resp_http = _post(
        f"{server['url']}/login",
        {"username": "e2e-scheme-admin2", "password": "e2epassword1"},
    )
    cookies_http = _set_cookie_headers(resp_http)
    for cookie in cookies_http:
        assert not _cookie_is_secure(cookie), f"did not expect Secure on: {cookie}"
