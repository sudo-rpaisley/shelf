"""Regression coverage for series-description write targets."""

from unittest.mock import AsyncMock, patch


def test_manual_description_rejects_missing_series_without_meta(admin_client, db):
    response = admin_client.post(
        "/api/series/No Such Series/description",
        data={"description": "Should never be stored"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "Series not found"}
    assert db.execute(
        "SELECT name FROM series_meta WHERE name = ?", ("No Such Series",)
    ).fetchone() is None


def test_fetch_description_rejects_missing_series_before_provider(admin_client, db):
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('hardcover_token', 'configured-token')"
    )
    db.commit()
    lookup = AsyncMock(return_value="Should never be fetched")

    with patch("app.routers.series.hardcover.get_series_description", new=lookup):
        response = admin_client.post(
            "/api/series/No Such Series/fetch-description"
        )

    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "Series not found"}
    lookup.assert_not_awaited()
    assert db.execute(
        "SELECT name FROM series_meta WHERE name = ?", ("No Such Series",)
    ).fetchone() is None
