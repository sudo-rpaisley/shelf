"""Optional sliding refresh must never override a route's explicit auth result."""

import time

import pytest

from app import auth


def _age_cookie(client):
    payload = auth.decode_token(client.cookies.get("access_token"))
    now = int(time.time())
    payload.update(iat=now - auth.JWT_EXPIRY_SECONDS * 3 // 4,
                   exp=now + auth.JWT_EXPIRY_SECONDS // 4)
    token = auth.jwt.encode(payload, auth.get_secret_key(), algorithm=auth.JWT_ALGORITHM)
    csrf = client.cookies.get("csrf_token")
    # Match a browser's single host cookie instead of retaining the fixture's
    # additional domainless cookie alongside cookies from actual responses.
    client.cookies.clear()
    client.cookies.set("csrf_token", csrf, domain="testserver.local", path="/")
    client.cookies.set("access_token", token, domain="testserver.local", path="/")
    assert token in client.build_request("GET", "/browse").headers["cookie"]
    return token, csrf


def _auth_headers(response):
    return [value for value in response.headers.get_list("set-cookie")
            if value.partition("=")[0].strip().lower() == "access_token"]


@pytest.mark.parametrize("action", ["logout", "login", "password", "display-name"])
def test_explicit_route_cookie_wins_over_aged_request_refresh(
    admin_client, editor_user, db, monkeypatch, action,
):
    old_token, _ = _age_cookie(admin_client)
    refreshes, original = [], auth.should_refresh_token

    def observe(request, user):
        refreshes.append(user["id"])
        return original(request, user)

    monkeypatch.setattr(auth, "should_refresh_token", observe)
    if action == "logout":
        response = admin_client.post("/logout", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"
    elif action == "login":
        response = admin_client.post("/login", data={
            "username": editor_user["username"], "password": "password123",
        }, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"
    elif action == "password":
        response = admin_client.post("/api/account/password", data={
            "current_password": "password123", "new_password": "new-password-123",
        })
        assert response.status_code == 200 and response.json()["ok"]
    else:
        response = admin_client.post("/api/account/display-name", data={"display_name": "Updated reader"})
        assert response.status_code == 200 and response.json()["ok"]

    assert len(_auth_headers(response)) == 1
    assert refreshes == []
    if action == "logout":
        assert admin_client.cookies.get("access_token") is None
        assert admin_client.get("/browse", follow_redirects=False).headers["location"] == "/login"
        return

    claims = auth.decode_token(admin_client.cookies.get("access_token"))
    assert claims is not None
    if action == "login":
        assert claims["sub"] == str(editor_user["id"])
    elif action == "password":
        assert claims["tv"] == db.execute("SELECT token_version FROM users WHERE id = 1").fetchone()[0] == 2
        assert admin_client.get("/browse", headers={"Cookie": f"access_token={old_token}"},
                                follow_redirects=False).status_code == 303
    else:
        assert claims["display_name"] == "Updated reader"
    assert admin_client.get("/browse").status_code == 200


def test_ordinary_aged_read_still_refreshes_and_preserves_csrf(admin_client):
    old, csrf = _age_cookie(admin_client)
    response = admin_client.get("/browse")
    assert response.status_code == 200 and len(_auth_headers(response)) == 1
    token = admin_client.cookies.get("access_token")
    assert token != old and auth.decode_token(token)["sub"] == "1"
    assert admin_client.cookies.get("csrf_token") == csrf


def test_fresh_read_does_not_add_auth_cookies(admin_client):
    response = admin_client.get("/browse")
    assert response.status_code == 200 and _auth_headers(response) == []
