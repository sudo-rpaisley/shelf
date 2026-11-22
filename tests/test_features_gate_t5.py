"""Sharing and Valuation behind their feature flags.

The share page is the one anonymous gated route: while Sharing is off, a
valid token answers byte for byte like an unknown one (design decision f).
"""

from unittest.mock import AsyncMock

import pytest

from tests.test_features_gate import HX, NAV, SSE, assert_json_403, assert_sse_error, assert_toast, off


def _link(db, token="tok-valid-123", scope="collection"):
    db.execute("INSERT INTO share_links (token, scope, label) VALUES (?, ?, 'x')", (token, scope))
    db.commit()
    return token


def _count(db, table):
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- The anonymous share page ---------------------------------------------


def test_disabled_share_reads_exactly_like_unknown_token(client, db):
    token = _link(db)
    off("share")
    valid = client.get(f"/share/{token}", follow_redirects=False)
    unknown = client.get("/share/no-such-token", follow_redirects=False)
    assert valid.status_code == unknown.status_code == 404
    assert valid.content == unknown.content
    assert valid.headers["x-robots-tag"] == unknown.headers["x-robots-tag"] == "noindex"
    assert valid.headers["content-type"] == unknown.headers["content-type"]


def test_enabled_share_renders(client, db):
    token = _link(db)
    resp = client.get(f"/share/{token}", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers["x-robots-tag"] == "noindex"


def test_reenabling_share_restores_the_link(client, admin_client, db):
    token = _link(db)
    off("share")
    assert client.get(f"/share/{token}").status_code == 404
    admin_client.post("/api/settings/features/share/enable", follow_redirects=False)
    assert client.get(f"/share/{token}").status_code == 200


# --- Share management -------------------------------------------------------


def test_create_share_refused_and_writes_nothing(admin_client, db):
    off("share")
    before = _count(db, "share_links")
    resp = admin_client.post("/api/share", data={"scope": "wishlist"})
    assert_json_403(resp, "share", "Sharing")
    assert _count(db, "share_links") == before


def test_revoke_share_refused_and_deletes_nothing(admin_client, db):
    _link(db)
    link_id = db.execute("SELECT id FROM share_links").fetchone()[0]
    off("share")
    resp = admin_client.post(f"/api/share/{link_id}/delete", headers=HX)
    assert_toast(resp, "Sharing")
    assert _count(db, "share_links") == 1


def test_revoke_native_form_lands_on_disabled_page(admin_client, db):
    _link(db)
    link_id = db.execute("SELECT id FROM share_links").fetchone()[0]
    off("share")
    resp = admin_client.post(f"/api/share/{link_id}/delete", headers=NAV)
    assert resp.status_code == 200
    assert 'data-testid="feature-disabled"' in resp.text
    assert _count(db, "share_links") == 1


def test_editor_gets_role_refusal_not_feature_refusal(editor_client):
    on = editor_client.post("/api/share", data={"scope": "wishlist"}, follow_redirects=False)
    off("share")
    disabled = editor_client.post("/api/share", data={"scope": "wishlist"}, follow_redirects=False)
    assert disabled.status_code == on.status_code == 403
    assert disabled.content == on.content


# --- Valuation ---------------------------------------------------------------


def test_valuation_report_refused(admin_client):
    off("valuation")
    assert_json_403(admin_client.get("/api/valuation/report"), "valuation", "Valuation")


def test_valuate_item_refused(admin_client, db):
    from tests.conftest import _insert_item
    item_id = _insert_item(db)
    db.commit()
    off("valuation")
    assert_toast(admin_client.post(f"/api/valuate/{item_id}", headers=HX), "Valuation")


def test_valuate_all_refused(admin_client):
    off("valuation")
    assert_json_403(admin_client.post("/api/valuate/all"), "valuation", "Valuation")


def test_valuate_stream_sends_error_frame(admin_client):
    off("valuation")
    assert_sse_error(admin_client.get("/api/valuate/stream", headers=SSE), "Valuation")


@pytest.mark.parametrize("path", ["/api/valuate/test-key", "/api/tmdb/test-key"])
def test_test_key_routes_stay_reachable(admin_client, path):
    """Configuration stays reachable; TMDb is core DVD scanning."""
    on = admin_client.post(path, json={})
    off("valuation")
    disabled = admin_client.post(path, json={})
    assert disabled.status_code == on.status_code == 200
    assert disabled.json() == on.json() == {"ok": False, "message": "No key configured"}


def test_tmdb_test_key_reaches_the_service_with_valuation_off(admin_client, monkeypatch):
    from app.services import tmdb
    stub = AsyncMock(return_value={"ok": True, "message": "Key is valid"})
    monkeypatch.setattr(tmdb, "test_key", stub)
    off("valuation")
    resp = admin_client.post("/api/tmdb/test-key", json={"key": "k"})
    assert resp.json() == {"ok": True, "message": "Key is valid"}
    stub.assert_awaited_once()
