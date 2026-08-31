"""Request-boundary regressions for series metadata and Hardcover lookups."""

from app.routers import series


def _meta_count(db, name: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM series_meta WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()[0]


def test_description_rejects_unknown_series_without_creating_metadata(editor_client, db):
    response = editor_client.post(
        "/api/series/Not%20In%20Library/description",
        data={"description": "Should never be stored"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "Series not found"}
    assert _meta_count(db, "Not In Library") == 0


def test_fetch_description_rejects_unknown_series_before_hardcover(
    editor_client, db, monkeypatch
):
    monkeypatch.setattr(series, "get_setting", lambda db, key: "configured-token")

    async def _forbid_lookup(*args, **kwargs):
        raise AssertionError("Hardcover lookup must not run for an unknown local series")

    monkeypatch.setattr(series.hardcover, "get_series_description", _forbid_lookup)

    response = editor_client.post("/api/series/Not%20In%20Library/fetch-description")

    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "Series not found"}
    assert _meta_count(db, "Not In Library") == 0


def test_check_rejects_unknown_series_before_hardcover(viewer_client, monkeypatch):
    monkeypatch.setattr(series, "get_setting", lambda db, key: "configured-token")

    async def _forbid_lookup(*args, **kwargs):
        raise AssertionError("Hardcover lookup must not run for an unknown local series")

    monkeypatch.setattr(series.hardcover, "get_series_books", _forbid_lookup)

    response = viewer_client.get("/api/series/check", params={"name": "Not In Library"})

    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "Series not found"}
