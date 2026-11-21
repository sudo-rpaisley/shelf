"""Tests for synopsis lookup and backfill (services/synopsis.py + endpoints)."""
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.services import provider_result, synopsis
from tests.conftest import _insert_item

GB_URL = "https://www.googleapis.com/books/v1/volumes"
OL_SEARCH_URL = "https://openlibrary.org/search.json"


@pytest.fixture
def items_synopses(client):
    """Use the composed endpoints, AFTER the real SQL log handler is installed."""
    import logging

    from app.log_handler import SQLiteHandler
    from app.routers import items_synopses as worker

    endpoints = {route.path: route.endpoint for route in client.app.routes if hasattr(route, "endpoint")}
    assert endpoints["/api/items/{item_id}/fetch-synopsis"] is worker.fetch_synopsis
    assert endpoints["/api/synopses/backfill/stream"] is worker.backfill_synopses_stream
    assert any(isinstance(handler, SQLiteHandler) for handler in logging.getLogger("app").handlers)
    return worker


def _gb_volume(title="Dune", authors=("Frank Herbert",), description="A desert planet epic."):
    return {"items": [{"volumeInfo": {
        "title": title, "authors": list(authors), "description": description,
    }}]}


class TestFetchDescription:
    @pytest.mark.asyncio
    async def test_google_key_reaches_isbn_and_title_searches(self):
        isbn_lookup = AsyncMock(return_value=provider_result.no_match("google"))
        title_search = AsyncMock(return_value=[])
        with patch("app.services.googlebooks.lookup", new=isbn_lookup), \
             patch("app.services.openlibrary.search_by_title_author", new=AsyncMock(return_value=[])), \
             patch("app.services.googlebooks.search_by_title_author", new=title_search):
            await synopsis.fetch_description(
                "9780441013593", "Dune", "Frank Herbert", object(),
                google_api_key="google-key",
            )

        assert isbn_lookup.await_args.kwargs["api_key"] == "google-key"
        assert title_search.await_args.kwargs["api_key"] == "google-key"

    @respx.mock
    @pytest.mark.asyncio
    async def test_google_books_isbn_first(self):
        respx.get(GB_URL).mock(return_value=httpx.Response(200, json=_gb_volume()))
        async with httpx.AsyncClient() as client:
            desc = await synopsis.fetch_description("9780441013593", "Dune", "Frank Herbert", client)
        assert desc == "A desert planet epic."

    @respx.mock
    @pytest.mark.asyncio
    async def test_openlibrary_work_fallback(self):
        # Google Books has the volume but no description
        respx.get(GB_URL).mock(return_value=httpx.Response(200, json=_gb_volume(description=None)))
        respx.get(OL_SEARCH_URL).mock(return_value=httpx.Response(200, json={"docs": [{
            "key": "/works/OL893415W", "title": "Dune", "author_name": ["Frank Herbert"],
        }]}))
        respx.get("https://openlibrary.org/works/OL893415W.json").mock(
            return_value=httpx.Response(200, json={"description": {"value": "From Open Library."}}))
        async with httpx.AsyncClient() as client:
            desc = await synopsis.fetch_description("9780441013593", "Dune", "Frank Herbert", client)
        assert desc == "From Open Library."

    @respx.mock
    @pytest.mark.asyncio
    async def test_author_guard_rejects_study_guide(self):
        # OL returns a study guide by a different author, then GB title search
        # returns the same wrong author — no description should be accepted.
        respx.get(GB_URL).mock(return_value=httpx.Response(200, json=_gb_volume(
            title="Dune (SparkNotes)", authors=("SparkNotes Editors",), description="Study guide.")))
        respx.get(OL_SEARCH_URL).mock(return_value=httpx.Response(200, json={"docs": [{
            "key": "/works/OL1W", "title": "Dune Study Guide", "author_name": ["SparkNotes Editors"],
        }]}))
        async with httpx.AsyncClient() as client:
            desc = await synopsis.fetch_description(None, "Dune", "Frank Herbert", client)
        assert desc is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_hardcover_fallback_when_gb_quota_dead(self):
        # Google Books 429 (per-IP quota), OL not needed — Hardcover has it
        respx.get(GB_URL).mock(return_value=httpx.Response(429, json={"error": {"code": 429}}))
        respx.post("https://api.hardcover.app/v1/graphql").mock(
            return_value=httpx.Response(200, json={"data": {"search": {"results": {
                "hits": [{"document": {
                    "id": "1", "title": "The Singularity Trap",
                    "author_names": ["Dennis E. Taylor"],
                    "description": "From Hardcover.",
                }}],
            }}}}))
        async with httpx.AsyncClient() as client:
            desc = await synopsis.fetch_description(
                "9781978665101", "The Singularity Trap", "Dennis E. Taylor",
                client, hc_token="hc-test-token")
        assert desc == "From Hardcover."

    @respx.mock
    @pytest.mark.asyncio
    async def test_hardcover_skipped_without_token(self):
        respx.get(GB_URL).mock(return_value=httpx.Response(429))
        respx.get(OL_SEARCH_URL).mock(return_value=httpx.Response(200, json={"docs": []}))
        async with httpx.AsyncClient() as client:
            desc = await synopsis.fetch_description(None, "Dune", "Frank Herbert", client)
        assert desc is None  # no Hardcover call attempted (respx would 404 it)

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_isbn_no_title(self):
        async with httpx.AsyncClient() as client:
            assert await synopsis.fetch_description(None, None, None, client) is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_network_errors_swallowed(self):
        respx.get(GB_URL).mock(side_effect=httpx.ConnectError("down"))
        respx.get(OL_SEARCH_URL).mock(side_effect=httpx.ConnectError("down"))
        async with httpx.AsyncClient() as client:
            assert await synopsis.fetch_description("9780441013593", "Dune", "Frank Herbert", client) is None


class TestSlugTitles:
    def test_searchable_title_deslugs(self):
        assert synopsis._searchable_title("mark-hatmaker-no-holds-barred-fighting") == \
            "mark hatmaker no holds barred fighting"
        assert synopsis._searchable_title("Winter_Time_Camping") == "Winter Time Camping"
        # Normal titles (with spaces) keep their hyphens
        assert synopsis._searchable_title("The Three-Body Problem") == "The Three-Body Problem"

    def test_title_close_enough(self):
        assert synopsis._title_close_enough(
            "mark hatmaker no holds barred fighting", "No Holds Barred Fighting") is True
        assert synopsis._title_close_enough(
            "winter time camping", "The Art of French Cooking") is False
        assert synopsis._title_close_enough("anything", None) is False

    @respx.mock
    @pytest.mark.asyncio
    async def test_authorless_slug_needs_title_match(self):
        # De-slugged search finds two results: an unrelated book first, the
        # real one second. Only the title-matching one may supply the work.
        respx.get(GB_URL).mock(return_value=httpx.Response(429))
        respx.get(OL_SEARCH_URL).mock(return_value=httpx.Response(200, json={"docs": [
            {"key": "/works/OL1W", "title": "Completely Different Book",
             "author_name": ["Someone Else"]},
            {"key": "/works/OL2W", "title": "No Holds Barred Fighting",
             "author_name": ["Mark Hatmaker"]},
        ]}))
        respx.get("https://openlibrary.org/works/OL2W.json").mock(
            return_value=httpx.Response(200, json={"description": "A grappling manual."}))
        async with httpx.AsyncClient() as client:
            desc = await synopsis.fetch_description(
                None, "mark-hatmaker-no-holds-barred-fighting", None, client)
        assert desc == "A grappling manual."


class TestStripHtml:
    def test_cleans_api_rich_text(self):
        # Import inside the test: app.main import needs the isolated-DB fixture
        from app.main import strip_html
        assert strip_html("Great. [**PDF**](https://spam.example/x) End.") == "Great.  End."
        assert strip_html("<p>First</p><p>**bold** &amp; _ital_</p>") == "First\nbold & ital"
        assert strip_html("snake_case and a_b survive") == "snake_case and a_b survive"
        assert strip_html("Line<br>break") == "Line\nbreak"
        assert strip_html(None) == ""


class TestFetchSynopsisEndpoint:
    @respx.mock
    def test_updates_item(self, admin_client, db):
        item_id = _insert_item(db, title="Dune", isbn="9780441013593", authors="Frank Herbert")
        db.execute("COMMIT")
        respx.get(GB_URL).mock(return_value=httpx.Response(200, json=_gb_volume()))

        resp = admin_client.post(f"/api/items/{item_id}/fetch-synopsis")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        row = db.execute("SELECT description FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row["description"] == "A desert planet epic."

    @respx.mock
    def test_not_found_reports_failure(self, admin_client, db):
        item_id = _insert_item(db, title="Obscure", isbn="9780000000187", authors="Nobody")
        db.execute("COMMIT")
        respx.get(GB_URL).mock(return_value=httpx.Response(200, json={}))
        respx.get(OL_SEARCH_URL).mock(return_value=httpx.Response(200, json={"docs": []}))

        resp = admin_client.post(f"/api/items/{item_id}/fetch-synopsis")
        assert resp.json()["ok"] is False

    def test_missing_item(self, admin_client, items_synopses):
        with patch.object(items_synopses.synopsis_svc, "fetch_description", new=AsyncMock()) as fetch:
            resp = admin_client.post("/api/items/99999/fetch-synopsis")
        assert resp.status_code == 404
        assert resp.json() == {"ok": False, "message": "Item not found"}
        fetch.assert_not_awaited()

    def test_viewer_forbidden(self, viewer_client, db):
        item_id = _insert_item(db)
        db.execute("COMMIT")
        resp = viewer_client.post(f"/api/items/{item_id}/fetch-synopsis")
        assert resp.status_code in (401, 403)


class TestBackfillStream:
    def _events(self, resp):
        return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]

    @respx.mock
    def test_backfills_missing_only(self, admin_client, db):
        needs = _insert_item(db, title="Dune", isbn="9780441013593", authors="Frank Herbert")
        _insert_item(db, title="Has Desc", isbn="9780000000262", description="already set")
        _insert_item(db, title="A Game", isbn=None, media_type="game")
        db.execute("COMMIT")
        respx.get(GB_URL).mock(return_value=httpx.Response(200, json=_gb_volume()))

        resp = admin_client.get("/api/synopses/backfill/stream")
        events = self._events(resp)
        done = events[-1]
        assert done["type"] == "done"
        assert done["total"] == 1
        assert done["success"] == 1
        row = db.execute("SELECT description FROM items WHERE id = ?", (needs,)).fetchone()
        assert row["description"] == "A desert planet epic."

    def test_empty_when_nothing_missing(self, admin_client, db):
        _insert_item(db, description="already set")
        db.execute("COMMIT")
        resp = admin_client.get("/api/synopses/backfill/stream")
        events = self._events(resp)
        assert events == [{"type": "done", "success": 0, "failed": 0, "total": 0}]

    def test_admin_only(self, editor_client):
        resp = editor_client.get("/api/synopses/backfill/stream")
        assert resp.status_code in (401, 403)

    def test_worker_uses_empty_context_and_captured_account_library(self, admin_client, db, monkeypatch, items_synopses):
        from app.services import catalogue_security

        needs = _insert_item(db, title="Real background target", isbn=None, authors="Writer")
        db.commit()
        original_authorize = items_synopses.items_common.authorize_item_targets
        admissions = []
        lookups = []

        def authorize(conn, ids, **kwargs):
            assert conn.in_transaction
            actor = catalogue_security.current_actor()
            admissions.append((actor, list(ids), dict(kwargs)))
            return original_authorize(conn, ids, **kwargs)

        async def fetch(isbn, title, authors, client, **kwargs):
            assert catalogue_security.current_actor() is None
            assert isinstance(client, httpx.AsyncClient)
            lookups.append((isbn, title, authors, kwargs))
            return "Background synopsis"

        monkeypatch.setattr(items_synopses.items_common, "authorize_item_targets", authorize)
        monkeypatch.setattr(items_synopses.synopsis_svc, "fetch_description", fetch)
        response = admin_client.get("/api/synopses/backfill/stream")
        assert response.status_code == 200
        assert self._events(response) == [
            {"type": "progress", "current": 1, "total": 1,
             "title": "Real background target", "status": "found"},
            {"type": "done", "success": 1, "failed": 0, "total": 1},
        ]
        assert lookups == [(None, "Real background target", "Writer",
                            {"hc_token": "", "google_api_key": ""})]
        assert len(admissions) == 3
        request_actor, ids, kwargs = admissions[0]
        assert request_actor["role"] == "admin" and ids == [needs] and kwargs == {}
        for actor, ids, kwargs in admissions[1:]:
            assert actor is None and ids == [needs]
            assert kwargs == {"user": request_actor, "library_ids": {needs: 1}}
        assert db.execute("SELECT description FROM items WHERE id = ?", (needs,)).fetchone()[0] == "Background synopsis"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["move", "revoke_account", "revoke_session", "trash"])
    async def test_worker_refuses_changed_captured_authority_before_provider(
        self, admin_user, db, monkeypatch, change, items_synopses,
    ):
        import asyncio

        from app.services import libraries

        target = _insert_item(db, title="Captured target", isbn=None)
        other = libraries.create_library(db, "Another parent")
        captured = dict(db.execute(
            "SELECT id, username, role, token_version FROM users WHERE id = ?", (admin_user["id"],),
        ).fetchone())
        if change == "move":
            libraries.assign_item(db, target, other["id"])
        elif change == "revoke_account":
            db.execute("UPDATE users SET role = 'viewer' WHERE id = ?", (admin_user["id"],))
        elif change == "revoke_session":
            db.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (admin_user["id"],))
        else:
            db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (target,))
        db.commit()
        before = list(db.iterdump())
        fetch = AsyncMock()
        monkeypatch.setattr(items_synopses.synopsis_svc, "fetch_description", fetch)
        queue = asyncio.Queue()
        await items_synopses._run_backfill(
            [{"id": target, "isbn": None, "title": "Captured target", "authors": None}],
            queue, job_user=captured, library_ids={target: 1}, hc_token="", google_api_key="",
        )
        assert queue.get_nowait() == {"type": "error", "message": "Synopsis backfill failed — check server logs"}
        assert queue.empty()
        fetch.assert_not_awaited()
        assert list(db.iterdump()) == before

    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["move", "revoke_account", "revoke_session", "trash"])
    async def test_worker_refuses_changed_captured_authority_after_real_provider_await(
        self, admin_user, db, change, items_synopses,
    ):
        import asyncio

        from app.database import get_db
        from app.services import catalogue_security, libraries, user_state

        target = _insert_item(db, title="Dune", isbn="9780441013593", authors="Frank Herbert")
        other = libraries.create_library(db, "Moved during synopsis lookup")
        user_state.set_reading_status(db, admin_user["id"], target, "read")
        captured = dict(db.execute(
            "SELECT id, username, role, token_version FROM users WHERE id = ?", (admin_user["id"],),
        ).fetchone())
        db.commit()
        snapshots = []
        mutations = []

        async def respond(request):
            assert catalogue_security.current_actor() is None
            assert request.url.params["q"] == "isbn:9780441013593"
            with get_db() as rival:
                assert rival is not db
                if change == "move":
                    libraries.assign_item(rival, target, other["id"])
                elif change == "revoke_account":
                    rival.execute("UPDATE users SET role = 'viewer' WHERE id = ?", (admin_user["id"],))
                elif change == "revoke_session":
                    rival.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (admin_user["id"],))
                else:
                    rival.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (target,))
            # This is the intentional authority change, not a worker side effect.
            # Snapshot after its commit; include ALL tables, especially SQL logs.
            with get_db() as observed:
                snapshots.append(list(observed.iterdump()))
            mutations.append(change)
            return httpx.Response(200, json=_gb_volume())

        provider = respx.get(GB_URL).mock(side_effect=respond)
        assert items_synopses.synopsis_svc is synopsis
        real_fetch = items_synopses.synopsis_svc.fetch_description
        fetch = AsyncMock(wraps=real_fetch)
        queue = asyncio.Queue()
        with patch.object(items_synopses.synopsis_svc, "fetch_description", new=fetch):
            await items_synopses._run_backfill(
                [{"id": target, "isbn": "9780441013593", "title": "Dune", "authors": "Frank Herbert"}],
                queue, job_user=captured, library_ids={target: 1}, hc_token="", google_api_key="",
            )

        assert provider.call_count == 1 and len(respx.calls) == 1
        assert mutations == [change] and len(snapshots) == 1
        provider_client = fetch.await_args.args[3]
        fetch.assert_awaited_once_with(
            "9780441013593", "Dune", "Frank Herbert", provider_client, hc_token="", google_api_key="",
        )
        assert isinstance(provider_client, httpx.AsyncClient) and provider_client.is_closed
        assert queue.get_nowait() == {"type": "error", "message": "Synopsis backfill failed — check server logs"}
        assert queue.empty()
        assert db.execute("SELECT description FROM items WHERE id = ?", (target,)).fetchone()[0] is None
        assert list(db.iterdump()) == snapshots[0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["runtime", "http500"])
    async def test_worker_logs_unexpected_failures_and_keeps_terminal_error(
        self, admin_user, db, monkeypatch, items_synopses, failure, caplog,
    ):
        import asyncio

        from fastapi import HTTPException

        target = _insert_item(db, title="Unexpected failure target", isbn=None)
        captured = dict(db.execute(
            "SELECT id, username, role, token_version FROM users WHERE id = ?", (admin_user["id"],),
        ).fetchone())
        db.commit()
        stored = tuple(db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone())
        logs = [tuple(row) for row in db.execute("SELECT * FROM log_entries ORDER BY id")]
        real_authorize = items_synopses.items_common.authorize_item_targets
        admissions = []

        def fail_after_authorization(conn, ids, **kwargs):
            assert conn.in_transaction
            admissions.append(real_authorize(conn, ids, **kwargs))
            if failure == "runtime":
                raise RuntimeError("Unexpected authority collaborator failure")
            raise HTTPException(500, "Unexpected authority collaborator failure")

        monkeypatch.setattr(items_synopses.items_common, "authorize_item_targets", fail_after_authorization)
        fetch = AsyncMock()
        monkeypatch.setattr(items_synopses.synopsis_svc, "fetch_description", fetch)
        queue = asyncio.Queue()
        await items_synopses._run_backfill(
            [{"id": target, "isbn": None, "title": "Unexpected failure target", "authors": None}],
            queue, job_user=captured, library_ids={target: 1}, hc_token="", google_api_key="",
        )

        assert admissions == [{target: 1}]
        fetch.assert_not_awaited()
        assert queue.get_nowait() == {"type": "error", "message": "Synopsis backfill failed — check server logs"}
        assert queue.empty()
        assert tuple(db.execute("SELECT * FROM items WHERE id = ?", (target,)).fetchone()) == stored
        after_logs = [tuple(row) for row in db.execute("SELECT * FROM log_entries ORDER BY id")]
        assert after_logs[:-1] == logs and len(after_logs) == len(logs) + 1
        error = db.execute("SELECT level, module, message FROM log_entries ORDER BY id DESC LIMIT 1").fetchone()
        assert (error["level"], error["module"]) == ("ERROR", items_synopses.__name__)
        assert "Synopsis backfill failed\nTraceback" in error["message"]
        assert "Unexpected authority collaborator failure" in error["message"]
        records = [record for record in caplog.records if record.name == items_synopses.__name__]
        assert len(records) == 1 and records[0].exc_info is not None
