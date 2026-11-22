"""Core synopsis lookup and request-lifetime SSE backfill.

Composed once by main.py, independently of the other item routers. Provider
lookups use the same cascade; background work drops the request ContextVar and
retains only the explicit, revalidated account and original library bindings.
"""

import asyncio
import json
import logging
from contextlib import suppress
from contextvars import Context

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth import require_role
from app.config import HTTP_TIMEOUT
from app.database import get_db, get_setting
from app.routers import items_common
from app.services import synopsis as synopsis_svc
from app.services.catalogue_security import require_catalogue_role
from app.services.item_write import update_item_fields

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


@router.post("/items/{item_id}/fetch-synopsis")
async def fetch_synopsis(item_id: int, _=Depends(require_catalogue_role("editor"))):
    """Look up a description, without spending a lookup on an unavailable item."""
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            library_ids = items_common.authorize_item_targets(db, [item_id])
            item = db.execute(
                "SELECT isbn, title, authors FROM items_live WHERE id = ?", (item_id,)
            ).fetchone()
            hc_token = get_setting(db, "hardcover_token")
            google_api_key = get_setting(db, "google_books_api_key")
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return JSONResponse({"ok": False, "message": "Item not found"}, status_code=404)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        desc = await synopsis_svc.fetch_description(
            item["isbn"], item["title"], item["authors"], client,
            hc_token=hc_token, google_api_key=google_api_key)

    if desc:
        try:
            with get_db() as db:
                db.execute("BEGIN IMMEDIATE")
                items_common.authorize_item_targets(db, [item_id], library_ids=library_ids)
                update_item_fields(db, item_id, {"description": desc})
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return JSONResponse({"ok": False, "message": "Item not found"}, status_code=404)
        return {"ok": True}
    return {"ok": False, "message": "No synopsis found"}


async def _run_backfill(items, queue, *, job_user, library_ids, hc_token, google_api_key):
    """Fresh-context job: check captured authority before AND after each await."""
    results = {"success": 0, "failed": 0, "total": len(items)}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            for i, item in enumerate(items, 1):
                with get_db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    items_common.authorize_item_targets(
                        db, [item["id"]], user=job_user, library_ids=library_ids)
                desc = None
                try:
                    desc = await synopsis_svc.fetch_description(
                        item["isbn"], item["title"], item["authors"], client,
                        hc_token=hc_token, google_api_key=google_api_key)
                except Exception:
                    logger.exception("Synopsis fetch failed for item %d", item["id"])
                with get_db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    items_common.authorize_item_targets(
                        db, [item["id"]], user=job_user, library_ids=library_ids)
                    if desc:
                        update_item_fields(db, item["id"], {"description": desc})
                if desc:
                    results["success"] += 1
                    status = "found"
                else:
                    results["failed"] += 1
                    status = "not found"
                await queue.put({
                    "type": "progress", "current": i, "total": len(items),
                    "title": item["title"] or item["isbn"], "status": status,
                })
        await queue.put({"type": "done", **results})
    except HTTPException as exc:
        # Revoked authority or a stale parent is an expected terminal refusal,
        # not an unexpected exception (whose SQL log would itself mutate the DB).
        if exc.status_code not in (403, 404):
            logger.exception("Synopsis backfill failed")
        await queue.put({"type": "error", "message": "Synopsis backfill failed — check server logs"})
    except Exception:
        logger.exception("Synopsis backfill failed")
        await queue.put({"type": "error", "message": "Synopsis backfill failed — check server logs"})


@router.get("/synopses/backfill/stream")
async def backfill_synopses_stream(request: Request, _=Depends(require_role("admin"))):
    """SSE endpoint: fetch descriptions for all book-family items missing one."""
    placeholders = ",".join("?" * len(synopsis_svc.SYNOPSIS_MEDIA_TYPES))
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        items = db.execute(
            f"SELECT id, isbn, title, authors FROM items_live "
            f"WHERE (description IS NULL OR description = '') "
            f"AND media_type IN ({placeholders}) ORDER BY id",
            synopsis_svc.SYNOPSIS_MEDIA_TYPES,
        ).fetchall()
        library_ids = items_common.authorize_item_targets(db, [item["id"] for item in items])
        hc_token = get_setting(db, "hardcover_token")
        google_api_key = get_setting(db, "google_books_api_key")

    if not items:
        async def empty_stream():
            yield f"data: {json.dumps({'type': 'done', 'success': 0, 'failed': 0, 'total': 0})}\n\n"
        return StreamingResponse(empty_stream(), media_type="text/event-stream")

    queue: asyncio.Queue = asyncio.Queue()
    job_user = dict(request.state.user)

    async def event_stream():
        task = asyncio.create_task(_run_backfill(
            items, queue, job_user=job_user, library_ids=library_ids,
            hc_token=hc_token, google_api_key=google_api_key), context=Context())
        try:
            while True:
                msg = await queue.get()
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["type"] in ("done", "error"):
                    break
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(event_stream(), media_type="text/event-stream")
