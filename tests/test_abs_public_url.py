"""Audiobookshelf browser/public URL setting regressions."""

from app.database import get_setting
from app.services.audiobookshelf import get_playback_url
from tests.conftest import _insert_item


def _set(db, key: str, value: str):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )
    db.commit()


def test_public_url_save_is_separate_from_internal_abs_url(admin_client, db):
    _set(db, "abs_url", "http://audiobookshelf:80")

    response = admin_client.post(
        "/api/sync/audiobookshelf/public-url",
        data={"abs_public_url": " https://audiobooks.example.com/ "},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert get_setting(db, "abs_url") == "http://audiobookshelf:80"
    assert get_setting(db, "abs_public_url") == "https://audiobooks.example.com"


def test_invalid_public_url_is_rejected_without_overwriting_saved_value(admin_client, db):
    _set(db, "abs_public_url", "https://good.example.com")

    response = admin_client.post(
        "/api/sync/audiobookshelf/public-url",
        data={"abs_public_url": "ftp://bad.example.com"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?abs_public_url_error=invalid"
    assert get_setting(db, "abs_public_url") == "https://good.example.com"


def test_blank_public_url_restores_internal_url_fallback(admin_client, db):
    _set(db, "abs_public_url", "https://public.example.com")

    response = admin_client.post(
        "/api/sync/audiobookshelf/public-url",
        data={"abs_public_url": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert get_setting(db, "abs_public_url") == ""
    assert get_playback_url("http://audiobookshelf:80", "li_123") == (
        "http://audiobookshelf:80/item/li_123"
    )


def test_playback_url_prefers_public_browser_root(db):
    _set(db, "abs_public_url", "https://audiobooks.example.com/")

    assert get_playback_url("http://audiobookshelf:80", "li_123") == (
        "https://audiobooks.example.com/item/li_123"
    )


def test_settings_renders_saved_browser_url(admin_client, db):
    _set(db, "abs_public_url", "https://audiobooks.example.com")

    html = admin_client.get("/settings").text

    assert "Audiobookshelf Browser / Public URL" in html
    assert 'name="abs_public_url"' in html
    assert 'value="https://audiobooks.example.com"' in html
    assert "never changes the address Shelf uses for sync or API traffic" in html


def test_abs_item_detail_uses_public_browser_root(admin_client, db):
    item_id = _insert_item(
        db,
        title="Public URL Smoke",
        isbn="9780907000307",
        media_type="audiobook",
        abs_id="li_public",
    )
    _set(db, "abs_url", "http://audiobookshelf:80")
    _set(db, "abs_public_url", "https://audiobooks.example.com")

    html = admin_client.get(f"/item/{item_id}").text

    assert "Listen on Audiobookshelf" in html
    assert 'href="https://audiobooks.example.com/item/li_public"' in html
    assert "http://audiobookshelf:80/item/li_public" not in html


def test_linked_digital_copy_uses_public_browser_root(admin_client, db):
    book = _insert_item(
        db, title="Linked Public URL", isbn="9780907000314", media_type="book"
    )
    audio = _insert_item(
        db,
        title="Linked Public URL",
        isbn="9780907000321",
        media_type="audiobook",
        abs_id="li_linked",
    )
    db.execute(
        "INSERT INTO item_links (item_a_id, item_b_id) VALUES (?, ?)",
        (min(book, audio), max(book, audio)),
    )
    _set(db, "abs_url", "http://audiobookshelf:80")
    _set(db, "abs_public_url", "https://audiobooks.example.com")

    html = admin_client.get(f"/item/{book}").text

    assert "Also in Audiobookshelf (Audiobook)" in html
    assert 'href="https://audiobooks.example.com/item/li_linked"' in html