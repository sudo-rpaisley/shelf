"""In-process background jobs for long-running integration synchronisations.

Shelf runs as a single application process in its standard container. A sync
started from the Settings UI therefore does not need to remain attached to the
browser request: keep the job on the server and expose a small status snapshot
that the UI can poll when it is visible.

Provider work runs in a dedicated daemon thread with its own asyncio event loop.
This is important because provider syncs mix async HTTP with synchronous SQLite
and filesystem work. Running that work as a detached task on Uvicorn's request
event loop still allowed a busy database write or cover write to make the whole
web UI pause. The worker thread keeps those blocking operations away from page
requests while retaining the provider's async HTTP implementation.

The registry is intentionally process-local. A container restart interrupts an
in-flight sync anyway; completed Shelf rows remain durable and provider syncs
are idempotent, so a subsequent run resumes by reconciling those rows.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str, str], Awaitable[None]]
Runner = Callable[[ProgressCallback], Awaitable[dict]]

_RECENT_LIMIT = 100


@dataclass
class _Job:
    provider: str
    source: str
    state: str = "running"
    current: int = 0
    total: int = 0
    title: str = ""
    item_status: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    stats: dict = field(default_factory=dict)
    error: str | None = None
    recent: deque[dict] = field(default_factory=lambda: deque(maxlen=_RECENT_LIMIT))
    task: asyncio.Future | None = None
    thread: threading.Thread | None = field(default=None, repr=False)
    stop_requested: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


_jobs: dict[str, _Job] = {}


def _snapshot(job: _Job, *, started: bool | None = None) -> dict:
    # Progress is written by the provider worker thread while status requests
    # read it on Uvicorn's event loop. Protect the deque conversion and related
    # fields so a progress append cannot race a reconnect/status response.
    with job.lock:
        data = {
            "provider": job.provider,
            "source": job.source,
            "state": job.state,
            "current": job.current,
            "total": job.total,
            "title": job.title,
            "item_status": job.item_status,
            "started_at": job.started_at,
            "updated_at": job.updated_at,
            "finished_at": job.finished_at,
            "stats": dict(job.stats),
            "error": job.error,
            "recent": list(job.recent),
        }
    if started is not None:
        data["started"] = started
    return data


def get_status(provider: str) -> dict:
    job = _jobs.get(provider)
    if job is None:
        return {
            "provider": provider,
            "source": None,
            "state": "idle",
            "current": 0,
            "total": 0,
            "title": "",
            "item_status": "",
            "started_at": None,
            "updated_at": None,
            "finished_at": None,
            "stats": {},
            "error": None,
            "recent": [],
        }
    return _snapshot(job)


def is_running(provider: str) -> bool:
    job = _jobs.get(provider)
    return bool(job and job.state == "running" and job.task and not job.task.done())


async def _run(job: _Job, runner: Runner) -> None:
    async def on_progress(current: int, total: int, title: str, status: str) -> None:
        # Shutdown cannot forcibly kill a Python thread safely. Instead ask the
        # provider coroutine to stop at its next progress boundary.
        if job.stop_requested.is_set():
            raise asyncio.CancelledError
        with job.lock:
            job.current = current
            job.total = total
            job.title = title or ""
            job.item_status = status or ""
            job.updated_at = time.time()
            job.recent.append({"i": current, "t": title or "", "s": status or ""})

    try:
        result = await runner(on_progress)
        if not isinstance(result, dict):
            raise TypeError("sync runner returned a non-dict result")
        with job.lock:
            if result.get("error"):
                job.state = "error"
                job.error = str(result["error"])
            else:
                job.state = "completed"
                job.stats = dict(result)
    except asyncio.CancelledError:
        with job.lock:
            job.state = "cancelled"
            job.error = "Sync interrupted because Shelf stopped"
        raise
    except Exception:
        logger.exception("%s background sync failed", job.provider)
        with job.lock:
            job.state = "error"
            job.error = "Sync failed — check server logs"
    finally:
        with job.lock:
            job.updated_at = time.time()
            job.finished_at = job.updated_at


def _settle_future(future: asyncio.Future, error: BaseException | None) -> None:
    """Complete a main-loop Future from a provider worker thread callback."""
    if future.done():
        return
    if isinstance(error, asyncio.CancelledError):
        future.cancel()
    elif error is not None:
        future.set_exception(error)
    else:
        future.set_result(None)


def _worker_main(job: _Job, runner: Runner, loop: asyncio.AbstractEventLoop) -> None:
    """Run one async provider sync in an isolated daemon thread/event loop."""
    error: BaseException | None = None
    try:
        asyncio.run(_run(job, runner))
    except BaseException as exc:  # includes CancelledError requested at shutdown
        error = exc
    try:
        loop.call_soon_threadsafe(_settle_future, job.task, error)
    except RuntimeError:
        # The application event loop can already be closed during process exit.
        # The worker is daemonised, so there is nothing useful left to notify.
        pass


def start(provider: str, runner: Runner, *, source: str = "manual") -> dict:
    """Start one provider sync in a worker thread and return immediately.

    At most one job per provider may run at a time. Calling start again while
    the provider is active simply returns the existing status snapshot.
    """
    existing = _jobs.get(provider)
    if existing and existing.state == "running" and existing.task and not existing.task.done():
        return _snapshot(existing, started=False)

    loop = asyncio.get_running_loop()
    job = _Job(provider=provider, source=source)
    job.task = loop.create_future()
    job.thread = threading.Thread(
        target=_worker_main,
        args=(job, runner, loop),
        name=f"shelf-sync-{provider}",
        daemon=True,
    )
    _jobs[provider] = job
    job.thread.start()
    return _snapshot(job, started=True)


async def wait(provider: str) -> dict:
    """Wait for the current provider job, if any, and return its final state."""
    job = _jobs.get(provider)
    if job and job.task and not job.task.done():
        try:
            await job.task
        except asyncio.CancelledError:
            pass
    return get_status(provider)


async def cancel_all() -> None:
    """Ask active worker jobs to stop during application shutdown."""
    active = [
        job
        for job in _jobs.values()
        if job.task and not job.task.done()
    ]
    for job in active:
        job.stop_requested.set()

    # Give providers a brief chance to reach their next progress boundary. The
    # threads are daemon threads, so shutdown must not wait for a slow remote
    # server or filesystem operation to time out.
    tasks = [job.task for job in active if job.task]
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=1.0)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def _reset_for_tests() -> None:
    """Test helper: stop active workers and clear state between unit tests."""
    for job in _jobs.values():
        job.stop_requested.set()
        if job.task and not job.task.done():
            job.task.cancel()
    _jobs.clear()
