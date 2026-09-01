"""Background integration sync job regressions.

These tests pin the behaviour the Settings page relies on when a user starts a
sync, navigates elsewhere in Shelf, and later returns to reattach to progress,
including the server-seeded first paint before polling resumes.
"""

import asyncio

from app.services import sync_jobs


def setup_function():
    sync_jobs._reset_for_tests()


def teardown_function():
    sync_jobs._reset_for_tests()


def test_job_returns_immediately_and_records_progress():
    gate = asyncio.Event()

    async def scenario():
        async def runner(progress):
            await progress(1, 3, "One", "added")
            await gate.wait()
            await progress(3, 3, "Three", "updated")
            return {"added": 1, "updated": 1}

        started = sync_jobs.start("komga", runner)
        assert started["started"] is True
        assert started["state"] == "running"

        await asyncio.sleep(0)
        live = sync_jobs.get_status("komga")
        assert live["state"] == "running"
        assert live["current"] == 1
        assert live["total"] == 3
        assert live["title"] == "One"
        assert live["recent"][-1] == {"i": 1, "t": "One", "s": "added"}

        gate.set()
        done = await sync_jobs.wait("komga")
        assert done["state"] == "completed"
        assert done["current"] == 3
        assert done["stats"] == {"added": 1, "updated": 1}

    asyncio.run(scenario())


def test_duplicate_start_reuses_running_provider_job():
    gate = asyncio.Event()
    calls = 0

    async def scenario():
        nonlocal calls

        async def runner(progress):
            nonlocal calls
            calls += 1
            await progress(1, 2, "Working", "unchanged")
            await gate.wait()
            return {"unchanged": 1}

        first = sync_jobs.start("audiobookshelf", runner)
        await asyncio.sleep(0)
        second = sync_jobs.start("audiobookshelf", runner)

        assert first["started"] is True
        assert second["started"] is False
        assert calls == 1

        gate.set()
        await sync_jobs.wait("audiobookshelf")

    asyncio.run(scenario())


def test_runner_error_is_exposed_as_job_error():
    async def scenario():
        async def runner(progress):
            return {"error": "provider unavailable"}

        sync_jobs.start("komga", runner)
        status = await sync_jobs.wait("komga")
        assert status["state"] == "error"
        assert status["error"] == "provider unavailable"
        assert status["finished_at"] is not None

    asyncio.run(scenario())


def test_settings_render_seeds_running_komga_progress(admin_client, monkeypatch):
    real_get_status = sync_jobs.get_status

    def fake_status(provider):
        if provider == "komga":
            return {
                "provider": "komga",
                "source": "manual",
                "state": "running",
                "current": 546,
                "total": 2576,
                "title": "The Deluge Drivers",
                "item_status": "updated",
                "started_at": 1.0,
                "updated_at": 2.0,
                "finished_at": None,
                "stats": {},
                "error": None,
                "recent": [],
            }
        return real_get_status(provider)

    monkeypatch.setattr(sync_jobs, "get_status", fake_status)
    html = admin_client.get("/settings").text

    assert 'data-sync-state="running"' in html
    assert 'data-sync-current="546"' in html
    assert 'data-sync-total="2576"' in html
    assert 'data-sync-title="The Deluge Drivers"' in html
