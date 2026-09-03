"""RomM settings, public links, platform selection and cleanup."""
import json

import httpx
import respx

from app.database import get_setting
from app.services import sync_jobs
from app.services.romm import get_browser_url
from tests.conftest import _insert_item

ROMM = "http://romm.example"
TOKEN = "rmm_test_token"


def _save_creds(db):
    db.execute("INSERT INTO settings (key, value) VALUES ('romm_url', ?)", (ROMM,))
    from app.crypto import encrypt_value, get_encryption_key
    encrypted = encrypt_value(TOKEN, get_encryption_key())
    db.execute("INSERT INTO settings (key, value) VALUES ('romm_api_token', ?)",
               (encrypted,))
    db.execute("COMMIT")


def _platforms():
    return [
        {"id": 1, "slug": "snes", "fs_slug": "snes", "name": "Super Nintendo",
         "display_name": "Super Nintendo", "custom_name": None,
         "rom_count": 123, "igdb_id": 19},
        {"id": 2, "slug": "ps1", "fs_slug": "ps1", "name": "PlayStation",
         "display_name": "PlayStation", "custom_name": None,
         "rom_count": 42, "igdb_id": 7},
    ]


def test_settings_renders_romm_fields_without_echoing_token(admin_client, db):
    _save_creds(db)
    response = admin_client.get("/settings")
    assert response.status_code == 200
    assert "RomM Sync" in response.text
    assert 'name="romm_url"' in response.text
    assert 'name="romm_api_token"' in response.text
    assert "RomM Browser / Public URL" in response.text
    assert "http://romm:8080" in response.text
    assert ':disabled="cleaning || !hasExcluded || syncing"' in response.text
    assert TOKEN not in response.text


def test_generic_settings_saves_normalized_url_and_encrypted_token(admin_client, db):
    response = admin_client.post(
        "/api/settings",
        data={"romm_url": "http://romm:80/", "romm_api_token": TOKEN},
    )
    assert response.status_code == 200
    assert get_setting(db, "romm_url") == "http://romm:80"
    assert get_setting(db, "romm_api_token") == TOKEN
    raw = db.execute("SELECT value FROM settings WHERE key='romm_api_token'").fetchone()["value"]
    assert raw != TOKEN


@respx.mock
def test_connection_test_uses_bearer_token(admin_client):
    platforms_route = respx.get(f"{ROMM}/api/platforms").mock(
        return_value=httpx.Response(200, json=_platforms()))
    roms_route = respx.get(f"{ROMM}/api/roms").mock(side_effect=[
        httpx.Response(200, json={"items": [], "total": 0}),
        httpx.Response(403, json={"detail": "Forbidden"}),
    ])

    response = admin_client.post(
        "/api/sync/romm/test", json={"url": ROMM, "token": TOKEN})
    assert response.json()["ok"] is True
    assert platforms_route.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert roms_route.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert roms_route.calls[0].request.url.params["limit"] == "1"

    missing_scope = admin_client.post(
        "/api/sync/romm/test", json={"url": ROMM, "token": TOKEN})
    assert missing_scope.json()["ok"] is False
    assert "roms.read" in missing_scope.json()["message"]


def test_public_url_normalizes_and_blank_restores_fallback(admin_client, db):
    admin_client.post("/api/sync/romm/public-url",
                      data={"romm_public_url": "https://romm.example.com/"})
    assert get_setting(db, "romm_public_url") == "https://romm.example.com"
    assert get_browser_url(ROMM, "101") == "https://romm.example.com/rom/101"
    admin_client.post("/api/sync/romm/public-url", data={"romm_public_url": ""})
    assert get_browser_url(ROMM, "101") == f"{ROMM}/rom/101"


def test_invalid_public_url_does_not_replace_saved_value(admin_client, db):
    db.execute("INSERT INTO settings (key, value) VALUES ('romm_public_url', 'https://good.example')")
    db.execute("COMMIT")
    response = admin_client.post(
        "/api/sync/romm/public-url", data={"romm_public_url": "ftp://bad.example"},
        follow_redirects=False)
    assert response.status_code == 303
    assert "romm_public_url_error=invalid" in response.headers["location"]
    assert get_setting(db, "romm_public_url") == "https://good.example"


@respx.mock
def test_platform_endpoint_marks_excluded_platforms(admin_client, db):
    _save_creds(db)
    db.execute("INSERT INTO settings (key, value) VALUES ('romm_excluded_platforms', ?)",
               (json.dumps(["2"]),))
    db.execute("COMMIT")
    respx.get(f"{ROMM}/api/platforms").mock(
        return_value=httpx.Response(200, json=_platforms()))
    data = admin_client.get("/api/sync/romm/platforms").json()
    assert data["ok"] is True
    assert [(p["id"], p["included"]) for p in data["platforms"]] == [
        ("1", True), ("2", False)]


def test_platform_selection_validates_and_saves(admin_client, db):
    bad = admin_client.post("/api/sync/romm/platforms",
                            json={"excluded": {"bad": "shape"}})
    assert bad.json()["ok"] is False
    good = admin_client.post("/api/sync/romm/platforms", json={"excluded": [1, "2"]})
    assert good.json() == {"ok": True, "excluded": ["1", "2"]}
    assert json.loads(get_setting(db, "romm_excluded_platforms")) == ["1", "2"]


def test_cleanup_deletes_romm_owned_and_detaches_adopted(admin_client, db, monkeypatch):
    db.execute("INSERT INTO settings (key, value) VALUES ('romm_excluded_platforms', '[\"2\"]')")
    owned = _insert_item(db, title="Owned", isbn=None, media_type="digital_game",
                         source="romm", romm_id="10", romm_platform_id="2")
    adopted = _insert_item(db, title="Adopted", isbn=None,
                           media_type="digital_game", source="manual",
                           romm_id="11", romm_platform_id="2")
    db.execute("COMMIT")

    monkeypatch.setattr(sync_jobs, "is_running", lambda provider: provider == "romm")
    blocked = admin_client.post("/api/sync/romm/platforms/cleanup")
    assert blocked.status_code == 409
    assert blocked.json()["ok"] is False
    assert "wait for it to finish" in blocked.json()["message"]
    assert db.execute("SELECT 1 FROM items WHERE id=?", (owned,)).fetchone() is not None

    monkeypatch.setattr(sync_jobs, "is_running", lambda provider: False)
    result = admin_client.post("/api/sync/romm/platforms/cleanup").json()
    assert result == {"ok": True, "deleted": 1, "detached": 1}
    assert db.execute("SELECT 1 FROM items WHERE id=?", (owned,)).fetchone() is None
    row = db.execute("SELECT romm_id, romm_platform_id, source FROM items WHERE id=?",
                     (adopted,)).fetchone()
    assert row["romm_id"] is None
    assert row["romm_platform_id"] is None
    assert row["source"] == "manual"


def test_item_detail_opens_direct_romm_copy(admin_client, db):
    db.execute("INSERT INTO settings (key, value) VALUES ('romm_url', ?)", (ROMM,))
    item_id = _insert_item(db, title="Chrono Trigger", isbn=None,
                           media_type="digital_game", romm_id="101",
                           romm_platform_id="1", platform="snes")
    db.execute("COMMIT")
    response = admin_client.get(f"/item/{item_id}")
    assert "Open in RomM" in response.text
    assert f'href="{ROMM}/rom/101"' in response.text


def test_physical_item_links_to_romm_copy(admin_client, db):
    db.execute("INSERT INTO settings (key, value) VALUES ('romm_url', ?)", (ROMM,))
    physical = _insert_item(db, title="Chrono Trigger", isbn=None,
                            media_type="video_game", platform="snes")
    digital = _insert_item(db, title="Chrono Trigger", isbn=None,
                           media_type="digital_game", romm_id="101",
                           romm_platform_id="1", platform="snes")
    a, b = sorted((physical, digital))
    db.execute("INSERT INTO item_links (item_a_id, item_b_id) VALUES (?, ?)", (a, b))
    db.execute("COMMIT")
    response = admin_client.get(f"/item/{physical}")
    assert "Available in" in response.text
    assert "RomM" in response.text
    assert f'href="{ROMM}/rom/101"' in response.text


def test_schedule_validation(admin_client):
    response = admin_client.post("/api/sync/romm/schedule", data={"interval": "hourly"})
    assert response.status_code == 400
    assert response.json()["ok"] is False
