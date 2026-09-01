"""In-process background jobs for long-running integration synchronisations.

Shelf runs as a single application process in its standard container.  A sync
started from the Settings UI therefore does not need to remain attached to the
browser request: keep the task on the server and expose a small status snapshot
that the UI can poll when it is visible.

The registry is intentionally process-local.  A container restart interrupts
an in-flight sync anyway; completed Shelf rows remain durable and the provider
syncs are idempotent, so a subsequent run resumes by reconciling those rows.
"""

from __future__ import annotations

import asyncio
import logging
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
    task: asyncio.Task | None = None


_jobs: dict[str, _Job] = {}


def _snapshot(job: _Job, *, started: bool | None = None) -> dict:
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
        if result.get("error"):
            job.state = "error"
            job.error = str(result["error"])
        else:
            job.state = "completed"
            job.stats = dict(result)
    except asyncio.CancelledError:
        job.state = "cancelled"
        job.error = "Sync interrupted because Shelf stopped"
        raise
    except Exception:
        logger.exception("%s background sync failed", job.provider)
        job.state = "error"
        job.error = "Sync failed — check server logs"
    finally:
        job.updated_at = time.time()
        job.finished_at = job.updated_at


def start(provider: str, runner: Runner, *, source: str = "manual") -> dict:
    """Start one provider sync and return immediately.

    At most one job per provider may run at a time.  Calling start again while
    the provider is active simply returns the existing status snapshot.
    """
    existing = _jobs.get(provider)
    if existing and existing.state == "running" and existing.task and not existing.task.done():
        return _snapshot(existing, started=False)

    job = _Job(provider=provider, source=source)
    _jobs[provider] = job
    job.task = asyncio.create_task(_run(job, runner), name=f"shelf-sync-{provider}")
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
    """Cancel active jobs during application shutdown."""
    tasks = [job.task for job in _jobs.values() if job.task and not job.task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _reset_for_tests() -> None:
    """Test helper: clear completed state between isolated unit tests."""
    for job in _jobs.values():
        if job.task and not job.task.done():
            job.task.cancel()
    _jobs.clear()
