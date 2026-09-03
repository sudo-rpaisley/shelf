import pytest

from app.services import googlebooks, provider_result, upcitemdb


MAGAZINE_EAN = "9770161737008"  # Popular Science / ISSN 0161-7370


@pytest.fixture
def google_magazine_hit(monkeypatch):
    async def _lookup(issn, client, *, api_key=None):
        assert issn == "0161-7370"
        return provider_result.found("google", {
            "title": "Popular Science",
            "publisher": "Bonnier Corporation",
            "description": "Science and technology magazine.",
            "issn": issn,
            "series_name": "Popular Science",
            "language": "en",
        })

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _lookup)


def test_auto_scan_routes_977_to_magazine_without_retail_lookup(
    editor_client, db, monkeypatch, google_magazine_hit
):
    async def _upc_must_not_run(upc, client):
        raise AssertionError("UPC Item DB should only be a fallback after a magazine miss")

    monkeypatch.setattr(upcitemdb, "lookup", _upc_must_not_run)

    resp = editor_client.post(
        "/api/scan", data={"isbn": MAGAZINE_EAN, "media_type": "auto"}
    )

    assert resp.status_code == 200
    assert "Popular Science" in resp.text
    assert "Magazine" in resp.text
    assert "0161-7370" in resp.text

    row = db.execute("SELECT * FROM items WHERE upc = ?", (MAGAZINE_EAN,)).fetchone()
    assert row is not None
    assert row["title"] == "Popular Science"
    assert row["media_type"] == "magazine"
    assert row["publisher"] == "Bonnier Corporation"
    assert row["description"] == "Science and technology magazine."
    assert row["series_name"] == "Popular Science"
    assert row["language"] == "en"
    assert row["source"] == "google"
    assert row["isbn"] is None


def test_977_overrides_a_wrong_media_type_hint(
    editor_client, db, google_magazine_hit
):
    resp = editor_client.post(
        "/api/scan", data={"isbn": MAGAZINE_EAN, "media_type": "dvd"}
    )

    assert resp.status_code == 200
    assert "filed as Magazine" in resp.text
    row = db.execute("SELECT media_type FROM items WHERE upc = ?", (MAGAZINE_EAN,)).fetchone()
    assert row["media_type"] == "magazine"


def test_google_miss_falls_back_to_upc_item_db(editor_client, db, monkeypatch):
    async def _google_miss(issn, client, *, api_key=None):
        return provider_result.no_match("google")

    async def _upc_hit(upc, client):
        assert upc == MAGAZINE_EAN
        return provider_result.found("upcitemdb", {
            "title": "Popular Science Magazine",
            "category": "Magazines",
            "brand": "Bonnier",
            "images": [],
        })

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _google_miss)
    monkeypatch.setattr(upcitemdb, "lookup", _upc_hit)

    resp = editor_client.post(
        "/api/scan", data={"isbn": MAGAZINE_EAN, "media_type": "auto"}
    )

    assert resp.status_code == 200
    row = db.execute("SELECT * FROM items WHERE upc = ?", (MAGAZINE_EAN,)).fetchone()
    assert row["media_type"] == "magazine"
    assert row["title"] == "Popular Science Magazine"
    assert row["publisher"] == "Bonnier"
    assert row["source"] == "upc"


def test_unknown_977_renders_magazine_manual_add(editor_client, monkeypatch):
    async def _google_miss(issn, client, *, api_key=None):
        return provider_result.no_match("google")

    async def _upc_miss(upc, client):
        return provider_result.no_match("upcitemdb")

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _google_miss)
    monkeypatch.setattr(upcitemdb, "lookup", _upc_miss)

    resp = editor_client.post(
        "/api/scan", data={"isbn": MAGAZINE_EAN, "media_type": "auto"}
    )

    assert resp.status_code == 200
    assert "Magazine ISSN 0161-7370 not found" in resp.text
    assert 'name="media_type" value="magazine"' in resp.text
    assert f'name="isbn" value="{MAGAZINE_EAN}"' in resp.text


def test_repeat_scan_is_duplicate_without_any_provider_call(
    editor_client, db, monkeypatch
):
    db.execute(
        "INSERT INTO items (title, media_type, upc, source) VALUES (?, ?, ?, ?)",
        ("Popular Science", "magazine", MAGAZINE_EAN, "test"),
    )
    # The request handler uses its own database connection.  Commit the seeded
    # row so the duplicate pre-check sees the same durable state a real prior
    # scan would have created.
    db.commit()

    async def _never(*args, **kwargs):
        raise AssertionError("duplicate 977 scan should not perform a metadata lookup")

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _never)
    monkeypatch.setattr(upcitemdb, "lookup", _never)

    resp = editor_client.post(
        "/api/scan", data={"isbn": MAGAZINE_EAN, "media_type": "auto"}
    )

    assert resp.status_code == 200
    assert "Already in collection" in resp.text
