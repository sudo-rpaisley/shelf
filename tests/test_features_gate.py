"""The feature gate: what a disabled feature's routes answer, per request kind.

Every pin uses a session fixture — an anonymous request is answered by
AuthMiddleware with a redirect to /login before any gate runs (G117).
"""

import json

import pytest

from app.features import feature_enabled, set_feature_enabled

HX = {"HX-Request": "true"}
SSE = {"Accept": "text/event-stream"}
NAV = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}


def _set(key, enabled):
    from app.database import get_db
    with get_db() as db:
        set_feature_enabled(db, key, enabled)


def off(*keys):
    for key in keys:
        _set(key, False)


def assert_disabled_page(resp, label, *, enable_form):
    assert resp.status_code == 200
    assert 'data-testid="feature-disabled"' in resp.text
    assert f"{label} is turned off on this Shelf." in resp.text
    assert ('data-testid="feature-enable-form"' in resp.text) is enable_form
    if not enable_form:
        assert "Ask your administrator to turn it on." in resp.text


def assert_toast(resp, label):
    assert resp.status_code == 403
    assert resp.content == b""
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert trigger["showToast"]["type"] == "error"
    assert label in trigger["showToast"]["message"]


def assert_json_403(resp, key, label):
    assert resp.status_code == 403
    assert resp.json() == {
        "error": "feature_disabled",
        "feature": key,
        "ok": False,
        "message": f"{label} is turned off on this Shelf.",
    }


def assert_sse_error(resp, label):
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in resp.text.split("\n\n") if line]
    assert len(frames) == 1
    payload = json.loads(frames[0].removeprefix("data: "))
    assert payload == {"type": "error", "message": f"{label} is turned off on this Shelf."}


# --- Series: the proving ground for every response kind -------------------


def test_series_page_admin_sees_enable_form(admin_client):
    off("series")
    assert_disabled_page(admin_client.get("/series"), "Series", enable_form=True)


@pytest.mark.parametrize("fixture", ["editor_client", "viewer_client"])
def test_series_page_non_admin_is_told_to_ask(fixture, request):
    c = request.getfixturevalue(fixture)
    off("series")
    assert_disabled_page(c.get("/series"), "Series", enable_form=False)


def test_series_htmx_gets_toast(admin_client):
    off("series")
    assert_toast(admin_client.get("/api/series/check?name=x", headers=HX), "Series")


def test_series_api_gets_json(admin_client):
    off("series")
    assert_json_403(admin_client.get("/api/series/check?name=x"), "series", "Series")


def test_series_event_stream_gets_error_frame(admin_client):
    off("series")
    assert_sse_error(admin_client.get("/api/series/check?name=x", headers=SSE), "Series")


def test_series_native_form_post_lands_on_disabled_page(admin_client):
    off("series")
    resp = admin_client.post("/api/series/x/rename", data={"new_name": "y"}, headers=NAV)
    assert_disabled_page(resp, "Series", enable_form=True)


@pytest.mark.parametrize("headers", [{}, HX, NAV])
def test_role_refusal_comes_before_feature_refusal(viewer_client, headers):
    on = viewer_client.post("/api/series/x/rename", data={"new_name": "y"},
                            headers=headers, follow_redirects=False)
    off("series")
    disabled = viewer_client.post("/api/series/x/rename", data={"new_name": "y"},
                                  headers=headers, follow_redirects=False)
    assert disabled.status_code == on.status_code == 403
    assert disabled.content == on.content
    assert disabled.headers.get("HX-Redirect") == on.headers.get("HX-Redirect")
    assert "feature-disabled" not in disabled.text


def test_series_enabled_serves_normally(admin_client):
    assert admin_client.get("/series").status_code == 200
    assert "feature-disabled" not in admin_client.get("/series").text


# --- The enable route ----------------------------------------------------


def test_enable_round_trip_lands_on_registry_path(admin_client):
    off("series")
    resp = admin_client.post("/api/settings/features/series/enable", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/series"
    assert feature_enabled("series") is True


def test_enable_ignores_next_and_referer(admin_client):
    off("series")
    resp = admin_client.post(
        "/api/settings/features/series/enable",
        data={"next": "//evil.com"},
        headers={"Referer": "https://evil.com/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/series"


def test_enable_feature_without_page_lands_on_settings(admin_client):
    off("lending")
    resp = admin_client.post("/api/settings/features/lending/enable", follow_redirects=False)
    assert resp.headers["location"] == "/settings"
    assert feature_enabled("lending") is True


def test_enable_refuses_editor(editor_client):
    off("series")
    resp = editor_client.post("/api/settings/features/series/enable", follow_redirects=False)
    assert resp.status_code == 403
    assert feature_enabled("series") is False


def test_enable_unknown_key_is_404(admin_client):
    resp = admin_client.post("/api/settings/features/nope/enable", follow_redirects=False)
    assert resp.status_code == 404


def test_restore_drops_the_flag_cache(admin_client, established_restore_keys):
    """A restored DB carries its own feature.* rows; the cache must not
    keep answering from the pre-restore database."""
    off("series")
    backup = admin_client.get("/api/settings/backup")
    assert backup.status_code == 200
    _set("series", True)
    assert feature_enabled("series") is True  # cached from the live DB

    resp = admin_client.post(
        "/api/settings/restore",
        files={"file": ("backup.db", backup.content, "application/octet-stream")},
    )
    assert resp.json()["ok"] is True, resp.json()
    assert feature_enabled("series") is False
