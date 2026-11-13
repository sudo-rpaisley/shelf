"""Shutdown cancellation must survive network cleanup and failed lease ACKs."""

import asyncio
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import httpx
import pytest

from app.database import get_db
from app.services import source_progress as svc
from tests.test_availability import _item, _record, _source
from tests.test_source_progress import (  # noqa: F401 — isolated HTTP/DB fixtures
    REMOTE_USER, ROOT, _jelly, _link, _state, bound, remote,
)
from tests.test_source_progress_schedule import _enable


class FinalizerFailure(BaseException):
    """Prove that cancellation is not protected only against Exception."""


def _progress_rows(db):
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in (
        "user_item_state", "user_item_events", "user_source_progress", "user_source_progress_events",
    )}


async def _exercise_shutdown(db, monkeypatch, viewer_user, bound, remote, *,
                             finish_error=None, contention=False, cleanup_failure=False, repeated=False,
                             suppress_transport_cancellation=False):
    uid = viewer_user["id"]
    other_source = _source(db, provider="jellyfin", name="Second due connection", base_url=ROOT)
    other_item = _item(db, "Must not import after shutdown", "digital_video")
    _record(db, other_source, other_item, external_id="other-item", media_type="digital_video")
    db.commit()
    other_progress = remote.get(f"{ROOT}/Users/{REMOTE_USER}/Items/other-item").respond(
        200, json=_jelly(Id="other-item"),
    )
    account = await _link(uid, bound["key"])
    other_account = await _link(uid, f"source:{other_source}")
    _enable(db, uid, account)
    _enable(db, uid, other_account)
    before = _progress_rows(db)
    entered, cleaning, release_cleanup, drained, resumed, resume_gate = (asyncio.Event() for _ in range(6))
    cleanup_aborted, finalizations, ack_errors, resumes = [], [], [], []

    async def blocked_progress(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if suppress_transport_cancellation:
                return httpx.Response(200, json=_jelly())
            raise

    class DrainingClient(httpx.AsyncClient):
        async def __aexit__(self, exc_type, exc, traceback):
            if isinstance(exc, asyncio.CancelledError) or asyncio.current_task().cancelling():
                cleaning.set()
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    cleanup_aborted.append(True)
                    raise
                finally:
                    await super().__aexit__(exc_type, exc, traceback)
                    drained.set()
                if cleanup_failure:
                    raise RuntimeError("injected HTTP cleanup failure")
                return None
            return await super().__aexit__(exc_type, exc, traceback)

    original_finish = svc._finish_schedule

    def finish(row, *args, **kwargs):
        if row["account_id"] != account:
            return original_finish(row, *args, **kwargs)
        finalizations.append(drained.is_set())
        try:
            if finish_error is not None:
                raise finish_error
            return original_finish(row, *args, **kwargs)
        except BaseException as exc:
            ack_errors.append(exc)
            raise

    @contextmanager
    def fast_db():
        # Keep real SQLite locking, but fail immediately rather than making a
        # blocking write wait five seconds on the test's event-loop thread.
        with get_db() as connection:
            connection.execute("PRAGMA busy_timeout=0")
            yield connection

    remote["progress"].mock(side_effect=blocked_progress)
    monkeypatch.setattr(svc, "_client", lambda: DrainingClient(
        timeout=httpx.Timeout(svc.REQUEST_TIMEOUT, connect=5), follow_redirects=False, trust_env=False,
    ))
    monkeypatch.setattr(svc, "_finish_schedule", finish)
    if contention:
        monkeypatch.setattr(svc, "get_db", fast_db)

    async def periodic_runner():
        try:
            await svc.run_scheduled_pass(max_accounts=1)
        except Exception as exc:
            # Model the parent runner's ordinary-error retry path. A swallowed
            # cancellation would resume and import the OTHER due account.
            resumes.append(type(exc).__name__)
            resumed.set()
            await resume_gate.wait()
            await svc.run_scheduled_pass(max_accounts=1)

    task = asyncio.create_task(periodic_runner())
    resume_waiter = asyncio.create_task(resumed.wait())
    writer_context, writer = None, None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        claimed = dict(db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone())
        task.cancel("requested shutdown")
        await asyncio.wait_for(cleaning.wait(), 5)
        assert not task.done() and not drained.is_set() and finalizations == []
        assert dict(db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()) == claimed
        if repeated:
            task.cancel("later shutdown must not replace the original")
        if contention:
            writer_context = get_db()
            writer = writer_context.__enter__()
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO settings (key, value) VALUES ('progress-cancel-writer-probe', 'committed')")
        release_cleanup.set()
        await asyncio.wait_for(asyncio.wait({task, resume_waiter}, return_when=asyncio.FIRST_COMPLETED), 5)
        if writer is not None:
            writer_context.__exit__(None, None, None)
            writer_context, writer = None, None
        # Let a broken ordinary-error retry actually run; it must not be able
        # to hide behind writer contention or a never-released test gate.
        resume_gate.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, 5)
        assert cancelled.value.args == ("requested shutdown",)
        assert task.cancelled() and resumes == [] and not resumed.is_set()
        assert drained.is_set() and cleanup_aborted == [] and finalizations == [True]
        assert len(ack_errors) == 1
        if contention:
            assert isinstance(ack_errors[0], sqlite3.OperationalError)
            assert "locked" in str(ack_errors[0]).lower()
            assert db.execute("SELECT value FROM settings WHERE key = 'progress-cancel-writer-probe'").fetchone()[0] == "committed"
        else:
            assert ack_errors[0] is finish_error
        # A failed ACK leaves exactly the owned lease, never a falsely cleared
        # lease or a live worker continuing to the next due account.
        assert dict(db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()) == claimed
        assert _progress_rows(db) == before and not other_progress.called
        assert db.execute("SELECT last_status FROM user_source_schedules WHERE account_id = ?", (other_account,)).fetchone()[0] == "never"

        monkeypatch.setattr(svc, "_finish_schedule", original_finish)
        monkeypatch.setattr(svc, "get_db", get_db)
        remote["progress"].respond(200, json=_jelly())
        future = datetime.fromisoformat(claimed["lease_expires_at"]) + timedelta(microseconds=1)
        monkeypatch.setattr(svc, "_now", lambda: future.isoformat(timespec="microseconds"))
        result = await svc.run_scheduled_pass(max_accounts=1)
        assert result["accepted"] == 1 and result["errors"] == 0
        assert _state(db, uid, bound["item"])["progress"] == 25
        assert _state(db, uid, other_item)["updated_at"] is None and not other_progress.called
        assert db.execute("SELECT COUNT(*) FROM user_source_progress_events").fetchone()[0] == 1
        with pytest.raises(svc.ProgressError, match="stale_schedule"):
            await svc.pull_item(uid, account, bound["item"], _lease={"revision": claimed["revision"], "token": claimed["lease_token"]})
    finally:
        if writer is not None:
            writer_context.__exit__(None, None, None)
        release_cleanup.set()
        resume_gate.set()
        if not task.done():
            task.cancel("test cleanup")
        resume_waiter.cancel()
        await asyncio.gather(task, resume_waiter, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    pytest.param(sqlite3.OperationalError("injected finalization write failure"), id="sqlite"),
    pytest.param(RuntimeError("injected finalizer error"), id="runtime"),
    pytest.param(FinalizerFailure("injected BaseException"), id="base-exception"),
    pytest.param(asyncio.CancelledError("wrong finalizer cancellation"), id="different-cancellation"),
])
async def test_finalizer_failure_cannot_replace_shutdown_cancellation(db, monkeypatch, viewer_user, bound, remote, error):
    await _exercise_shutdown(db, monkeypatch, viewer_user, bound, remote, finish_error=error)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_real_writer_contention_preserves_cancellation_and_recoverable_lease(db, monkeypatch, viewer_user, bound, remote, cleanup_failure):
    await _exercise_shutdown(db, monkeypatch, viewer_user, bound, remote, contention=True, cleanup_failure=cleanup_failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_repeated_shutdown_drains_cleanup_and_keeps_first_cancellation(db, monkeypatch, viewer_user, bound, remote, cleanup_failure):
    await _exercise_shutdown(db, monkeypatch, viewer_user, bound, remote,
                             finish_error=sqlite3.OperationalError("injected ACK failure"), repeated=True,
                             cleanup_failure=cleanup_failure)


@pytest.mark.asyncio
async def test_transport_swallowing_cancellation_cannot_apply_late_response(db, monkeypatch, viewer_user, bound, remote):
    await _exercise_shutdown(db, monkeypatch, viewer_user, bound, remote,
                             finish_error=sqlite3.OperationalError("injected ACK failure"),
                             suppress_transport_cancellation=True)


@pytest.mark.asyncio
async def test_http_cleanup_failure_does_not_replace_cancellation_when_ack_succeeds(db, monkeypatch, viewer_user, bound, remote):
    uid = viewer_user["id"]
    account = await _link(uid, bound["key"])
    _enable(db, uid, account)
    entered, closed = asyncio.Event(), asyncio.Event()
    before = _progress_rows(db)

    async def blocked(request):
        entered.set()
        await asyncio.Event().wait()

    class FailingClose(httpx.AsyncClient):
        async def __aexit__(self, exc_type, exc, traceback):
            await super().__aexit__(exc_type, exc, traceback)
            closed.set()
            if isinstance(exc, asyncio.CancelledError):
                raise RuntimeError("injected HTTP cleanup error")

    monkeypatch.setattr(svc, "_client", lambda: FailingClose(
        timeout=svc.REQUEST_TIMEOUT, follow_redirects=False, trust_env=False,
    ))
    remote["progress"].mock(side_effect=blocked)
    task = asyncio.create_task(svc.run_scheduled_pass())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel("requested shutdown")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(task, 5)
        assert cancelled.value.args == ("requested shutdown",)
        assert task.cancelled() and closed.is_set() and _progress_rows(db) == before
        row = db.execute("SELECT * FROM user_source_schedules WHERE account_id = ?", (account,)).fetchone()
        assert row["lease_token"] is None and row["last_status"] == row["last_error"] == "interrupted"
    finally:
        if not task.done():
            task.cancel("test cleanup")
        await asyncio.gather(task, return_exceptions=True)
