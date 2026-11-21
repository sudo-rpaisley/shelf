"""T7: gate hardcover, abs_sync, komga and romm.

Follows the pattern established by tests/test_features_gate.py. Reuses its
helpers rather than duplicating them. See GOTCHAS G117 (anonymous /api/
requests never reach the gate; a session fixture is required) and G56 (stub
service functions, not a whole module, with AsyncMock).
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.database import get_db
from app.services import provider_result, romm_records
from tests.conftest import _insert_item
from tests.test_features_gate import (
    HX, NAV, SSE, assert_disabled_page, assert_json_403, assert_sse_error, assert_toast, off,
)


def _set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


# --- /discover --------------------------------------------------------------


def test_discover_admin_sees_enable_form(admin_client):
    off("hardcover")
    assert_disabled_page(admin_client.get("/discover"), "Hardcover", enable_form=True)


def test_discover_editor_is_told_to_ask(editor_client):
    off("hardcover")
    assert_disabled_page(editor_client.get("/discover"), "Hardcover", enable_form=False)


def test_discover_enabled_serves_normally(admin_client):
    resp = admin_client.get("/discover")
    assert resp.status_code == 200
    assert "feature-disabled" not in resp.text


# --- hardcover.py gated routes ----------------------------------------------


def test_hardcover_search_gets_json_403(admin_client):
    off("hardcover")
    resp = admin_client.get("/api/hardcover/search?q=x")
    assert_json_403(resp, "hardcover", "Hardcover")


def test_hardcover_add_to_shelf_gets_json_403(editor_client):
    off("hardcover")
    resp = editor_client.post("/api/hardcover/add-to-shelf", json={"title": "x"})
    assert_json_403(resp, "hardcover", "Hardcover")


def test_hardcover_add_to_shelf_htmx_gets_toast(editor_client):
    off("hardcover")
    resp = editor_client.post(
        "/api/hardcover/add-to-shelf", json={"title": "x"}, headers=HX,
    )
    assert_toast(resp, "Hardcover")


def test_hardcover_push_gets_json_403(editor_client, db):
    item_id = _insert_item(db)
    db.commit()
    off("hardcover")
    resp = editor_client.post(f"/api/hardcover/push/{item_id}")
    assert_json_403(resp, "hardcover", "Hardcover")


def test_hardcover_export_stream_gets_error_frame(editor_client):
    off("hardcover")
    resp = editor_client.get("/api/hardcover/export/stream", headers=SSE)
    assert_sse_error(resp, "Hardcover")


def test_hardcover_import_stream_gets_error_frame(editor_client):
    off("hardcover")
    resp = editor_client.get("/api/hardcover/import/stream", headers=SSE)
    assert_sse_error(resp, "Hardcover")


def test_hardcover_writes_nothing_while_off(editor_client, db):
    before = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    off("hardcover")
    editor_client.post("/api/hardcover/add-to-shelf", json={"title": "Should not land"})
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == before


# --- hardcover.py configuration routes stay reachable -----------------------


def test_hardcover_test_route_stays_reachable(admin_client):
    on = admin_client.post("/api/hardcover/test", json={"token": ""})
    off("hardcover")
    disabled = admin_client.post("/api/hardcover/test", json={"token": ""})
    assert disabled.status_code == on.status_code == 200
    assert disabled.json() == on.json() == {"ok": False, "message": "No token provided"}


def test_hardcover_schedule_route_stays_reachable(admin_client):
    on = admin_client.post("/api/hardcover/schedule", data={"interval": "daily"},
                           follow_redirects=False)
    off("hardcover")
    disabled = admin_client.post("/api/hardcover/schedule", data={"interval": "daily"},
                                 follow_redirects=False)
    assert disabled.status_code == on.status_code == 303
    assert disabled.headers["location"] == on.headers["location"]


# --- sync.py (abs_sync) -----------------------------------------------------


def test_sync_audiobookshelf_post_gets_json_403(admin_client):
    off("abs_sync")
    resp = admin_client.post("/api/sync/audiobookshelf")
    assert_json_403(resp, "abs_sync", "Audiobookshelf sync")


def test_sync_audiobookshelf_stream_gets_error_frame(admin_client):
    off("abs_sync")
    resp = admin_client.get("/api/sync/audiobookshelf/stream", headers=SSE)
    assert_sse_error(resp, "Audiobookshelf sync")


def test_sync_audiobookshelf_cleanup_gets_json_403(admin_client):
    off("abs_sync")
    resp = admin_client.post("/api/sync/audiobookshelf/libraries/cleanup")
    assert_json_403(resp, "abs_sync", "Audiobookshelf sync")


def test_sync_editor_gets_role_refusal_not_feature_refusal(editor_client):
    on = editor_client.post("/api/sync/audiobookshelf", follow_redirects=False)
    off("abs_sync")
    disabled = editor_client.post("/api/sync/audiobookshelf", follow_redirects=False)
    assert disabled.status_code == on.status_code == 403
    assert disabled.content == on.content
    assert "feature-disabled" not in disabled.text


# --- sync.py configuration routes stay reachable ----------------------------


def test_sync_test_route_stays_reachable(admin_client):
    on = admin_client.post("/api/sync/audiobookshelf/test", json={})
    off("abs_sync")
    disabled = admin_client.post("/api/sync/audiobookshelf/test", json={})
    assert disabled.status_code == on.status_code == 200
    assert disabled.json() == on.json() == {
        "ok": False, "message": "URL and token are required",
    }


def test_sync_libraries_get_stays_reachable(admin_client):
    on = admin_client.get("/api/sync/audiobookshelf/libraries")
    off("abs_sync")
    disabled = admin_client.get("/api/sync/audiobookshelf/libraries")
    assert disabled.status_code == on.status_code == 200
    assert disabled.json() == on.json() == {
        "ok": False, "message": "Audiobookshelf is not configured",
    }


def test_sync_libraries_post_stays_reachable(admin_client):
    on = admin_client.post("/api/sync/audiobookshelf/libraries", json={"excluded": []})
    off("abs_sync")
    disabled = admin_client.post("/api/sync/audiobookshelf/libraries", json={"excluded": []})
    assert disabled.status_code == on.status_code == 200
    assert disabled.json() == on.json() == {"ok": True, "excluded": []}


def test_sync_schedule_route_stays_reachable(admin_client):
    on = admin_client.post("/api/sync/audiobookshelf/schedule", data={"interval": "daily"},
                           follow_redirects=False)
    off("abs_sync")
    disabled = admin_client.post("/api/sync/audiobookshelf/schedule", data={"interval": "daily"},
                                 follow_redirects=False)
    assert disabled.status_code == on.status_code == 303
    assert disabled.headers["location"] == on.headers["location"]


@pytest.mark.parametrize("key, provider, setting", [
    ("abs_sync", "audiobookshelf", "abs_sync_interval"),
    ("komga", "komga", "komga_sync_interval"),
    ("romm", "romm", "romm_sync_interval"),
])
def test_disabled_schedule_is_configuration_only_and_never_starts_provider_work(
    admin_client, db, key, provider, setting,
):
    off(key)
    before_items = [tuple(row) for row in db.execute("SELECT * FROM items ORDER BY id")]
    service = "audiobookshelf" if provider == "audiobookshelf" else f"{provider}_sync"
    with patch("app.services.sync_jobs.start") as start, \
         patch(f"app.services.{service}.sync", new=AsyncMock()) as sync:
        saved = admin_client.post(f"/api/sync/{provider}/schedule", data={"interval": "weekly"},
                                  follow_redirects=False)
        assert saved.status_code == 303
        assert saved.headers["location"] == "/settings"
        assert db.execute("SELECT value FROM settings WHERE key = ?", (setting,)).fetchone()[0] == "weekly"
        before_invalid = "\n".join(db.iterdump())
        invalid = admin_client.post(f"/api/sync/{provider}/schedule", data={"interval": "never"},
                                    follow_redirects=False)
        assert invalid.status_code == 400
        assert invalid.json() == {"ok": False, "message": "Invalid sync interval"}
        assert "\n".join(db.iterdump()) == before_invalid
    start.assert_not_called()
    sync.assert_not_awaited()
    assert [tuple(row) for row in db.execute("SELECT * FROM items ORDER BY id")] == before_items
    for stamp in ("abs_last_sync", "komga_last_sync", "romm_last_sync"):
        assert db.execute("SELECT value FROM settings WHERE key = ?", (stamp,)).fetchone() is None


# --- komga.py ----------------------------------------------------------------


def test_komga_sync_stream_gets_error_frame(admin_client):
    off("komga")
    resp = admin_client.get("/api/komga/sync/stream", headers=SSE)
    assert_sse_error(resp, "Komga")


def test_komga_item_action_gets_json_403(viewer_client, db):
    item_id = _insert_item(db)
    db.commit()
    off("komga")
    resp = viewer_client.get(f"/api/komga/items/{item_id}/action")
    assert_json_403(resp, "komga", "Komga")


def test_komga_viewer_gets_role_refusal_not_feature_refusal(viewer_client):
    on = viewer_client.get("/api/komga/sync/stream", follow_redirects=False)
    off("komga")
    disabled = viewer_client.get("/api/komga/sync/stream", follow_redirects=False)
    assert disabled.status_code == on.status_code == 403
    assert disabled.content == on.content
    assert "feature-disabled" not in disabled.text


# --- romm.py -------------------------------------------------------------


def _seed_romm_item(db):
    with get_db() as conn:
        return romm_records.persist_candidate(conn, {
            "romm_id": "rom-t7-123",
            "romm_platform_id": "1",
            "title": "Chrono Trigger",
            "platform": "snes",
            "platform_name": "SNES",
        })["item_id"]


def test_romm_sync_stream_gets_error_frame(admin_client):
    off("romm")
    resp = admin_client.get("/api/romm/sync/stream", headers=SSE)
    assert_sse_error(resp, "RomM")


def test_romm_item_action_gets_json_403(viewer_client, db):
    item_id = _insert_item(db)
    db.commit()
    off("romm")
    resp = viewer_client.get(f"/api/romm/items/{item_id}/action")
    assert_json_403(resp, "romm", "RomM")


def test_romm_open_gets_json_403(viewer_client, db):
    item_id = _seed_romm_item(db)
    off("romm")
    resp = viewer_client.get(f"/api/romm/items/{item_id}/open")
    assert_json_403(resp, "romm", "RomM")


def test_romm_open_nav_headers_get_disabled_page(viewer_client, db):
    """R4 pin: the Browse card's native link uses Sec-Fetch-Mode: navigate."""
    item_id = _seed_romm_item(db)
    off("romm")
    resp = viewer_client.get(f"/api/romm/items/{item_id}/open", headers=NAV)
    assert_disabled_page(resp, "RomM", enable_form=False)


def test_romm_viewer_gets_role_refusal_not_feature_refusal(viewer_client):
    on = viewer_client.get("/api/romm/sync/stream", follow_redirects=False)
    off("romm")
    disabled = viewer_client.get("/api/romm/sync/stream", follow_redirects=False)
    assert disabled.status_code == on.status_code == 403
    assert disabled.content == on.content
    assert "feature-disabled" not in disabled.text


# --- Item page keeps rendering with komga and romm off (correction 5) ------


def test_item_page_renders_with_komga_and_romm_off(admin_client, db):
    item_id = _insert_item(db, title="Still Visible")
    db.commit()
    off("komga", "romm")
    resp = admin_client.get(f"/item/{item_id}")
    assert resp.status_code == 200
    assert "feature-disabled" not in resp.text


# --- The scan cascade still consults Hardcover with the feature off --------


def test_scan_cascade_still_calls_hardcover_with_feature_off(admin_client, db):
    """Design decision: Hardcover-as-metadata-source is out of scope for this
    plan (see plan's Deferred/out of scope). The cascade never reads the
    flag, so a book scan still consults Hardcover when a token is configured,
    even while the `hardcover` feature itself is turned off."""
    _set_setting(db, "hardcover_token", "tok-123")
    off("hardcover")

    hc_payload = {
        "title": "HC-Sourced Book", "authors": "A. Writer",
        "hardcover_book_id": 5, "hardcover_edition_id": 7,
    }
    hc_stub = AsyncMock(return_value=provider_result.found("hardcover", hc_payload))
    with patch("app.services.openlibrary.lookup",
              new=AsyncMock(return_value=provider_result.no_match("openlibrary"))), \
         patch("app.services.hardcover.lookup_by_isbn", new=hc_stub):
        resp = admin_client.post("/api/scan", data={
            "isbn": "9780000001030", "media_type": "book", "mode": "add",
        })

    assert resp.status_code == 200
    hc_stub.assert_awaited()
    row = db.execute(
        "SELECT title, source FROM items WHERE isbn = '9780000001030'"
    ).fetchone()
    assert row["title"] == "HC-Sourced Book"
    assert row["source"] == "hardcover"
