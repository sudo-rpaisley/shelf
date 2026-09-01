"""Komga settings, request boundaries and browser-link regressions."""
import httpx
import respx

from app.database import get_setting
from app.services.komga import get_browser_url
from tests.conftest import _insert_item

KOMGA = "http://komga:25600"


def _set(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


class TestKomgaSettingsUI:
    def test_settings_renders_sync_and_browser_url_fields(self, admin_client):
        html = admin_client.get("/settings").text
        assert 'data-testid="komga-sync-card"' in html
        assert 'name="komga_url"' in html
        assert 'name="komga_api_key"' in html
        assert 'name="komga_public_url"' in html
        assert 'Komga Internal / API URL' in html
        assert 'Komga Browser / Public URL' in html
        assert 'Audiobookshelf Internal / API URL' in html
        assert 'Audiobookshelf Browser / Public URL' in html

    def test_generic_settings_normalizes_url_and_keeps_api_key_write_only(
        self, admin_client, db
    ):
        response = admin_client.post(
            "/api/settings",
            data={"komga_url": "  http://komga:25600/  ", "komga_api_key": "secret-key"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert get_setting(db, "komga_url") == KOMGA
        assert get_setting(db, "komga_api_key") == "secret-key"
        raw = db.execute(
            "SELECT value FROM settings WHERE key = 'komga_api_key'"
        ).fetchone()["value"]
        assert raw != "secret-key"
        html = admin_client.get("/settings").text
        assert "secret-key" not in html


class TestKomgaConnectionBoundary:
    def test_invalid_request_shapes_are_rejected(self, admin_client):
        assert admin_client.post("/api/sync/komga/test", json=[]).json()["ok"] is False
        assert admin_client.post(
            "/api/sync/komga/test", json={"url": 123, "api_key": "key"}
        ).json()["ok"] is False
        assert admin_client.post(
            "/api/sync/komga/test", json={"url": KOMGA, "api_key": True}
        ).json()["ok"] is False

    @respx.mock
    def test_connection_uses_x_api_key(self, admin_client):
        route = respx.get(f"{KOMGA}/api/v1/libraries").mock(
            return_value=httpx.Response(200, json=[{"id": "lib", "name": "Comics"}])
        )
        data = admin_client.post(
            "/api/sync/komga/test", json={"url": KOMGA, "api_key": "key-123"}
        ).json()
        assert data["ok"] is True
        assert route.calls[0].request.headers["X-API-Key"] == "key-123"

    def test_invalid_schedule_is_400(self, admin_client):
        response = admin_client.post(
            "/api/sync/komga/schedule", data={"interval": "hourly"}
        )
        assert response.status_code == 400


class TestKomgaPublicUrl:
    def test_save_normalizes_public_url_without_touching_api_url(
        self, admin_client, db
    ):
        _set(db, "komga_url", KOMGA)
        response = admin_client.post(
            "/api/sync/komga/public-url",
            data={"komga_public_url": "  https://comics.example.test/  "},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert get_setting(db, "komga_url") == KOMGA
        assert get_setting(db, "komga_public_url") == "https://comics.example.test"
        assert get_browser_url(KOMGA, "book_1") == "https://comics.example.test/book/book_1"

    def test_invalid_public_url_preserves_saved_value(self, admin_client, db):
        _set(db, "komga_public_url", "https://comics.example.test")
        response = admin_client.post(
            "/api/sync/komga/public-url",
            data={"komga_public_url": "ftp://comics.example.test"},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/settings?komga_public_url_error=invalid"
        assert get_setting(db, "komga_public_url") == "https://comics.example.test"

    def test_blank_public_url_restores_internal_url_fallback(self, admin_client, db):
        _set(db, "komga_public_url", "https://comics.example.test")
        admin_client.post(
            "/api/sync/komga/public-url", data={"komga_public_url": "   "}
        )
        assert get_browser_url(KOMGA, "book_1") == f"{KOMGA}/book/book_1"

    def test_item_detail_uses_public_komga_url(self, admin_client, db):
        item_id = _insert_item(
            db,
            title="Komga Comic",
            isbn=None,
            media_type="digital_comic",
            komga_id="book_public",
            komga_library_id="lib_1",
            source="komga",
        )
        _set(db, "komga_url", KOMGA)
        _set(db, "komga_public_url", "https://comics.example.test")

        html = admin_client.get(f"/item/{item_id}").text
        assert "Open in Komga" in html
        assert 'href="https://comics.example.test/book/book_public"' in html
        assert f'{KOMGA}/book/book_public' not in html

    def test_linked_item_exposes_komga_copy(self, admin_client, db):
        ebook = _insert_item(
            db, title="Linked Work", isbn=None, media_type="ebook"
        )
        comic = _insert_item(
            db,
            title="Linked Work",
            isbn=None,
            media_type="digital_comic",
            komga_id="book_linked",
            source="komga",
        )
        db.execute(
            "INSERT INTO item_links (item_a_id, item_b_id) VALUES (?, ?)",
            (min(ebook, comic), max(ebook, comic)),
        )
        _set(db, "komga_url", KOMGA)
        _set(db, "komga_public_url", "https://comics.example.test")

        html = admin_client.get(f"/item/{ebook}").text
        assert "Also in Komga" in html
        assert 'href="https://comics.example.test/book/book_linked"' in html
