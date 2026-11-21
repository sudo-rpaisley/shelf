"""Both RomM credential names use named decryption and encrypted persistence."""

import pytest


@pytest.mark.parametrize("key", ["romm_token", "romm_api_token"])
def test_romm_legacy_plaintext_alias_is_migrated_once_and_read_by_name(db, key):
    from app import crypto
    from app.database import get_all_settings, get_setting

    value = "local-test-token"
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()
    assert key in crypto.SENSITIVE_KEYS
    assert crypto.migrate_sensitive_settings() == 1
    raw = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()[0]
    assert raw != value
    assert raw.startswith("gAAAAA")
    assert get_setting(db, key) == value
    assert get_all_settings(db)[key] == value
    assert crypto.migrate_sensitive_settings() == 0
    assert db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()[0] == raw


def test_conflicting_romm_environment_aliases_refuse_without_exposing_values(db, monkeypatch):
    from app import features
    from app.services import romm_client, romm_sync

    monkeypatch.setenv("ROMM_TOKEN", "first-test")
    monkeypatch.setenv("ROMM_API_TOKEN", "other-test")
    monkeypatch.setenv("ROMM_URL", "https://romm.invalid")
    before = [tuple(row) for row in db.execute("SELECT key, value FROM settings ORDER BY key")]
    with pytest.raises(romm_client.RomMError) as error:
        romm_sync.configuration()
    assert "first-test" not in str(error.value)
    assert "other-test" not in str(error.value)
    assert features._romm_configured() is False
    assert [tuple(row) for row in db.execute("SELECT key, value FROM settings ORDER BY key")] == before


def test_compatibility_settings_replacement_and_clear_apply_to_both_aliases(db, admin_client):
    from app.database import get_setting
    from app.routers.settings import _upsert_setting

    _upsert_setting(db, "romm_token", "old-local-token")
    db.commit()
    response = admin_client.post("/api/settings", data={"romm_api_token": "new-local-token"}, follow_redirects=False)
    assert response.status_code == 303
    for key in ("romm_token", "romm_api_token"):
        assert get_setting(db, key) == "new-local-token"
        raw = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()[0]
        assert raw.startswith("gAAAAA")
    response = admin_client.post("/api/settings", data={"romm_api_token": "", "clear_romm_api_token": "on"}, follow_redirects=False)
    assert response.status_code == 303
    assert get_setting(db, "romm_token") == get_setting(db, "romm_api_token") == ""
