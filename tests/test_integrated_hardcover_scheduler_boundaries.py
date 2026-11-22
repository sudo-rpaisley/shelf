"""A due scheduler may not manufacture an owner or stamp a refused pull."""

from unittest.mock import AsyncMock

import pytest


def _configure(db):
    from app.features import set_feature_enabled

    db.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", [
        ("hc_sync_interval", "daily"), ("hardcover_token", "local-test-token"),
    ])
    set_feature_enabled(db, "hardcover", True)
    db.commit()


@pytest.mark.asyncio
async def test_due_hardcover_without_an_account_neither_calls_provider_nor_stamps(db, monkeypatch):
    from app.main import _hardcover_sync_pass
    from app.services import hardcover

    _configure(db)
    sync = AsyncMock(return_value={"updated": 0, "unchanged": 0, "total": 0})
    monkeypatch.setattr(hardcover, "sync_reading_statuses", sync)
    assert await _hardcover_sync_pass() is False
    sync.assert_not_awaited()
    assert db.execute("SELECT 1 FROM settings WHERE key = 'hc_last_sync'").fetchone() is None


@pytest.mark.asyncio
async def test_refused_hardcover_pull_does_not_write_a_success_stamp(db, admin_user, monkeypatch):
    from app.main import _hardcover_sync_pass
    from app.services import hardcover

    _configure(db)
    owner = hardcover.scheduled_account(db)
    assert owner["id"] == admin_user["id"]
    sync = AsyncMock(return_value={"updated": 0, "unchanged": 0, "total": 0,
                                  "error": "Hardcover sync is not authorized"})
    monkeypatch.setattr(hardcover, "sync_reading_statuses", sync)
    assert await _hardcover_sync_pass() is False
    sync.assert_awaited_once()
    assert isinstance(sync.call_args.kwargs["user"], type(owner))
    assert sync.call_args.kwargs["user"] == owner
    assert db.execute("SELECT 1 FROM settings WHERE key = 'hc_last_sync'").fetchone() is None
