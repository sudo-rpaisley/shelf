"""T4: gate stats, store, music, periodicals, shelf_fill and intake.

Follows the pattern established by tests/test_features_gate.py, which proved
every response kind against `series`. Reuses its helpers rather than
duplicating them. See GOTCHAS G73 (store.js's offline queue), G117
(anonymous /api/ requests never reach the gate; a session fixture is
required), G80 (a task that adds tests restamps README.md's badges itself).

Some features here have no gated route beyond their page (`stats`,
`periodicals`) — for those, only the disabled-page response is exercised;
there is no API or htmx-fragment route under the flag to test against.
"""

from unittest.mock import AsyncMock, patch

from app.features import FEATURES
from app.services import provider_result
from tests.conftest import _insert_item
from tests.test_features_gate import HX, assert_disabled_page, assert_json_403, assert_toast, off


# --- stats ------------------------------------------------------------------


def test_stats_page_disabled(admin_client):
    off("stats")
    assert_disabled_page(admin_client.get("/stats"), "Statistics", enable_form=True)


def test_stats_enabled_serves_normally(admin_client):
    resp = admin_client.get("/stats")
    assert resp.status_code == 200
    assert "feature-disabled" not in resp.text


# --- store --------------------------------------------------------------


def test_store_page_disabled(admin_client):
    off("store")
    assert_disabled_page(admin_client.get("/store"), "Store Mode", enable_form=True)


def test_store_api_gets_json(admin_client):
    off("store")
    assert_json_403(admin_client.get("/api/store/data"), "store", "Store Mode")


def test_store_queue_htmx_gets_toast(admin_client):
    off("store")
    resp = admin_client.post("/api/store/queue", json={"isbns": []}, headers=HX)
    assert_toast(resp, "Store Mode")


def test_store_enabled_serves_normally(admin_client):
    resp = admin_client.get("/store")
    assert resp.status_code == 200
    assert "feature-disabled" not in resp.text


# --- music --------------------------------------------------------------


def test_music_page_disabled(admin_client):
    off("music")
    assert_disabled_page(admin_client.get("/music"), "Music", enable_form=True)


def test_music_api_gets_json(admin_client):
    off("music")
    assert_json_403(admin_client.get("/api/music/items/1/discogs"), "music", "Music")


def test_music_htmx_gets_toast(admin_client):
    off("music")
    resp = admin_client.post("/api/music/items/1/discogs/clear", headers=HX)
    assert_toast(resp, "Music")


def test_music_enabled_serves_normally(admin_client):
    resp = admin_client.get("/music")
    assert resp.status_code == 200
    assert "feature-disabled" not in resp.text


# --- periodicals ----------------------------------------------------------
# Only /periodicals and /periodicals/{id} are gated (both full pages) — no
# JSON-403 or htmx-toast case exists under this flag; the assist/confirm
# routes stay ungated by design (core Scan's 977 flow).


def test_periodicals_page_disabled(admin_client):
    off("periodicals")
    assert_disabled_page(admin_client.get("/periodicals"), "Periodicals", enable_form=True)


def test_periodicals_publication_page_disabled(admin_client, db):
    pub_id = db.execute(
        "INSERT INTO periodical_publications (title) VALUES ('Popular Science')"
    ).lastrowid
    db.commit()
    off("periodicals")
    assert_disabled_page(
        admin_client.get(f"/periodicals/{pub_id}"), "Periodicals", enable_form=True
    )


def test_periodicals_enabled_serves_normally(admin_client):
    resp = admin_client.get("/periodicals")
    assert resp.status_code == 200
    assert "feature-disabled" not in resp.text


# --- shelf_fill -----------------------------------------------------------


def test_shelf_fill_page_disabled(admin_client):
    off("shelf_fill")
    assert_disabled_page(admin_client.get("/shelf-fill"), "Shelf Fill", enable_form=True)


def test_shelf_fill_api_gets_json(admin_client):
    off("shelf_fill")
    resp = admin_client.get("/api/shelf-fill/summary?location_id=0")
    assert_json_403(resp, "shelf_fill", "Shelf Fill")


def test_shelf_fill_htmx_gets_toast(admin_client):
    off("shelf_fill")
    resp = admin_client.post(
        "/api/shelf-fill/scan",
        data={"isbn": "9780000000026", "location_id": 1, "media_type": "book"},
        headers=HX,
    )
    assert_toast(resp, "Shelf Fill")


def test_shelf_fill_enabled_serves_normally(admin_client):
    resp = admin_client.get("/shelf-fill")
    assert resp.status_code == 200
    assert "feature-disabled" not in resp.text


def test_viewer_shelf_fill_gets_role_refusal_not_disabled_page(viewer_client):
    """shelf_fill's page is editor-only. The role dependency runs first, so a
    viewer's refusal is unchanged by the flag (correction 3)."""
    on = viewer_client.get("/shelf-fill", follow_redirects=False)
    off("shelf_fill")
    disabled = viewer_client.get("/shelf-fill", follow_redirects=False)
    assert on.status_code == disabled.status_code == 303
    assert on.headers["location"] == disabled.headers["location"]
    assert "feature-disabled" not in disabled.text


# --- intake ---------------------------------------------------------------


def test_intake_page_disabled(admin_client):
    off("intake")
    assert_disabled_page(admin_client.get("/intake"), "Photo Intake", enable_form=True)


def test_intake_api_gets_json(admin_client):
    off("intake")
    resp = admin_client.post("/api/intake/plan", json={"width": 100, "height": 100})
    assert_json_403(resp, "intake", "Photo Intake")


def test_intake_htmx_gets_toast(admin_client):
    off("intake")
    resp = admin_client.post("/api/intake/confirm", json={"books": []}, headers=HX)
    assert_toast(resp, "Photo Intake")


def test_intake_enabled_serves_normally(admin_client):
    resp = admin_client.get("/intake")
    assert resp.status_code == 200
    assert "feature-disabled" not in resp.text


# --- With every feature off, core/ungated surfaces are unaffected ---------


def test_sw_js_unaffected_by_every_feature_off(client, admin_user):
    """/sw.js skips AuthMiddleware entirely (main.py `_SKIP_AUTH_PATHS`), so
    an anonymous request here is the documented shape of the route, not a
    G117 violation."""
    on = client.get("/sw.js")
    off(*FEATURES)
    disabled = client.get("/sw.js")
    assert on.status_code == disabled.status_code == 200
    assert on.content == disabled.content


def test_core_scan_unaffected_by_every_feature_off(admin_client, db):
    item_id = _insert_item(db, title="Existing Book", isbn="9780000000026")
    db.commit()
    payload = {"isbn": "9780000000026", "media_type": "book", "mode": "add"}

    on = admin_client.post("/api/scan", data=payload)
    off(*FEATURES)
    disabled = admin_client.post("/api/scan", data=payload)

    assert on.status_code == disabled.status_code == 200
    assert b"duplicate" in on.content
    assert b"duplicate" in disabled.content
    assert item_id  # the row the duplicate check matches against


def test_periodicals_assist_search_unaffected_by_every_feature_off(editor_client):
    found = AsyncMock(return_value=provider_result.found("google", [{
        "google_volume_id": "candidate-1",
        "title": "Popular Science",
        "publisher": "Bonnier",
        "issue_date": "2026-03-01",
        "issn": "2049-3630",
        "cover_url": "https://books.google.com/preview.jpg",
    }]))
    params = {"q": "Popular Science", "raw_barcode": "977016173700805", "mode": "add"}

    with patch("app.routers.periodicals.periodical_google.search_issues", new=found):
        on = editor_client.get("/api/periodicals/assist/search", params=params)
    off(*FEATURES)
    with patch("app.routers.periodicals.periodical_google.search_issues", new=found):
        disabled = editor_client.get("/api/periodicals/assist/search", params=params)

    assert on.status_code == disabled.status_code == 200
    assert "Popular Science" in on.text
    assert "Popular Science" in disabled.text


def test_periodicals_confirm_unaffected_by_every_feature_off(editor_client, db):
    on = editor_client.post(
        "/api/periodicals/confirm",
        data={"raw_barcode": "9770161737008", "publication_title": "Popular Science",
              "issue_number": "1"},
        follow_redirects=False,
    )
    saved_on = db.execute(
        "SELECT i.id, i.title, pi.publication_id FROM periodical_issues pi "
        "JOIN items i ON i.id = pi.item_id WHERE pi.barcode_ean = ?", ("9770161737008",)
    ).fetchone()
    assert saved_on is not None
    assert on.status_code == 303
    assert on.headers["location"] == f"/periodicals/{saved_on['publication_id']}"
    on_page = editor_client.get(on.headers["location"])
    assert on_page.status_code == 200
    assert saved_on["title"] in on_page.text
    assert "feature-disabled" not in on_page.text
    off(*FEATURES)
    disabled = editor_client.post(
        "/api/periodicals/confirm",
        data={"raw_barcode": "9770161737015", "publication_title": "Scientific American",
              "issue_number": "2"},
        follow_redirects=False,
    )

    assert on.status_code == disabled.status_code == 303
    saved_off = db.execute(
        "SELECT i.id, i.title, pi.publication_id FROM periodical_issues pi "
        "JOIN items i ON i.id = pi.item_id WHERE pi.barcode_ean = ?", ("9770161737015",)
    ).fetchone()
    assert saved_off is not None
    assert saved_off["id"] != saved_on["id"]
    assert disabled.headers["location"] == f"/item/{saved_off['id']}"
    off_page = editor_client.get(disabled.headers["location"])
    assert off_page.status_code == 200
    assert saved_off["title"] in off_page.text
    assert "feature-disabled" not in off_page.text
    # The optional index and both publication pages still remain gated.
    for path in ("/periodicals", f"/periodicals/{saved_on['publication_id']}",
                 f"/periodicals/{saved_off['publication_id']}"):
        assert_disabled_page(editor_client.get(path), "Periodicals", enable_form=False)
