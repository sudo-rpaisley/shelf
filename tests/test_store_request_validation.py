"""Regression coverage for Store queue request-shape validation."""


def test_store_queue_rejects_non_object_json(admin_client, db, monkeypatch):
    from unittest.mock import AsyncMock
    from app.routers import items_common

    lookup = AsyncMock(side_effect=AssertionError("invalid JSON must not reach providers"))
    monkeypatch.setattr(items_common, "_lookup_metadata", lookup)
    before = list(db.iterdump())
    for body in ([], "not-an-object", None):
        response = admin_client.post("/api/store/queue", json=body)

        assert response.status_code == 400
        assert response.json() == {"error": "Invalid JSON body"}
        assert list(db.iterdump()) == before
    lookup.assert_not_awaited()
