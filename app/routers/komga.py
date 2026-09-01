import asyncio
import json
import logging
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import StreamingResponse

from app.auth import require_role
from app.config import HTTP_TIMEOUT
from app.database import get_db, get_setting
from app.services import komga

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/sync/komga", dependencies=[Depends(require_role("admin"))]
)


def _validate_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "URL must use http:// or https://"
        if not parsed.hostname:
            return "Invalid URL"
    except Exception:
        return "Invalid URL"
    return None


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key, "Accept": "application/json"}


@router.post("/test")
async def test_komga(request: Request):
    """Test a submitted Komga URL/API key, falling back to saved values."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return {"ok": False, "message": "Invalid request body"}

    raw_url = body.get("url")
    raw_key = body.get("api_key")
    if (
        (raw_url is not None and not isinstance(raw_url, str))
        or (raw_key is not None and not isinstance(raw_key, str))
    ):
        return {"ok": False, "message": "Invalid request body"}

    url = (raw_url or "").strip().rstrip("/")
    api_key = (raw_key or "").strip()
    if not url or not api_key:
        with get_db() as db:
            url = url or get_setting(db, "komga_url")
            api_key = api_key or get_setting(db, "komga_api_key")

    if not url or not api_key:
        return {"ok": False, "message": "URL and API key are required"}
    url_error = _validate_url(url)
    if url_error:
        return {"ok": False, "message": url_error}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(
                f"{url}/api/v1/libraries", headers=_headers(api_key)
            )
    except httpx.ConnectError:
        return {"ok": False, "message": f"Cannot connect to {url}"}
    except httpx.HTTPError:
        return {"ok": False, "message": "Connection failed — check URL and network"}

    if response.status_code == 200:
        libraries = response.json()
        count = len(libraries) if isinstance(libraries, list) else 0
        return {"ok": True, "message": f"Connected — {count} library(ies) found"}
    if response.status_code in (401, 403):
        return {"ok": False, "message": "Invalid API key"}
    return {"ok": False, "message": f"Unexpected response: HTTP {response.status_code}"}


@router.post("/public-url")
async def save_public_url(komga_public_url: str = Form("")):
    public_url = komga_public_url.strip().rstrip("/")
    if public_url and _validate_url(public_url):
        return RedirectResponse(
            url="/settings?komga_public_url_error=invalid", status_code=303
        )
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('komga_public_url', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = ?",
            (public_url, public_url),
        )
    return RedirectResponse(url="/settings", status_code=303)


@router.get("/libraries")
async def list_libraries():
    with get_db() as db:
        url = get_setting(db, "komga_url")
        api_key = get_setting(db, "komga_api_key")
    if not url or not api_key:
        return {"ok": False, "message": "Komga is not configured"}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(
                f"{url}/api/v1/libraries", headers=_headers(api_key)
            )
    except httpx.HTTPError:
        return {"ok": False, "message": f"Cannot connect to {url}"}

    if response.status_code != 200:
        return {"ok": False, "message": f"Komga returned HTTP {response.status_code}"}
    raw_libraries = response.json()
    if not isinstance(raw_libraries, list):
        return {"ok": False, "message": "Komga returned an invalid library response"}

    excluded = komga.get_excluded_libraries()
    libraries = [
        {
            "id": library.get("id"),
            "name": library.get("name"),
            "media_type": "comic",
            "included": library.get("id") not in excluded,
        }
        for library in raw_libraries
        if isinstance(library, dict) and library.get("id")
    ]
    return {"ok": True, "libraries": libraries}


@router.post("/libraries")
async def save_libraries(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "Invalid request body"}
    if not isinstance(body, dict):
        return {"ok": False, "message": "Invalid request body"}
    raw_excluded = body.get("excluded") or []
    if not isinstance(raw_excluded, list) or not all(
        isinstance(value, str) for value in raw_excluded
    ):
        return {"ok": False, "message": "Invalid request body"}

    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('komga_excluded_libraries', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(raw_excluded),),
        )
    return {"ok": True, "excluded": raw_excluded}


@router.post("/libraries/cleanup")
async def cleanup_libraries():
    excluded = komga.get_excluded_libraries()
    if not excluded:
        return {"ok": True, "deleted": 0, "detached": 0}

    placeholders = ",".join("?" * len(excluded))
    deleted = 0
    detached = 0
    with get_db() as db:
        rows = db.execute(
            f"""SELECT id, source FROM items
                WHERE komga_library_id IN ({placeholders})""",
            tuple(excluded),
        ).fetchall()
        for row in rows:
            if row["source"] == "komga":
                db.execute(
                    "UPDATE scan_log SET item_id = NULL WHERE item_id = ?", (row["id"],)
                )
                db.execute("DELETE FROM items WHERE id = ?", (row["id"],))
                deleted += 1
            else:
                db.execute(
                    """UPDATE items SET komga_id = NULL, komga_library_id = NULL,
                       komga_series_id = NULL WHERE id = ?""",
                    (row["id"],),
                )
                detached += 1
    logger.info(
        "Komga cleanup removed %d synced items and detached %d adopted items",
        deleted,
        detached,
    )
    return {"ok": True, "deleted": deleted, "detached": detached}


@router.post("")
async def sync_now():
    with get_db() as db:
        url = get_setting(db, "komga_url")
        api_key = get_setting(db, "komga_api_key")
    if not url or not api_key:
        return {"error": "Komga URL and API key must be configured in Settings"}
    url_error = _validate_url(url)
    if url_error:
        return {"error": url_error}
    return await komga.sync(url, api_key)


@router.get("/stream")
async def sync_stream():
    with get_db() as db:
        url = get_setting(db, "komga_url")
        api_key = get_setting(db, "komga_api_key")

    if not url or not api_key:
        async def missing_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'URL and API key required'})}\n\n"
        return StreamingResponse(missing_stream(), media_type="text/event-stream")

    url_error = _validate_url(url)
    if url_error:
        async def invalid_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': url_error})}\n\n"
        return StreamingResponse(invalid_stream(), media_type="text/event-stream")

    queue: asyncio.Queue = asyncio.Queue()

    async def on_progress(current, total, title, status):
        await queue.put(
            {
                "type": "progress",
                "current": current,
                "total": total,
                "title": title,
                "status": status,
            }
        )

    async def run_sync():
        try:
            stats = await komga.sync(url, api_key, on_progress=on_progress)
            if stats.get("error"):
                await queue.put({"type": "error", "message": stats["error"]})
            else:
                await queue.put({"type": "done", **stats})
        except Exception:
            logger.exception("Komga sync failed")
            await queue.put(
                {"type": "error", "message": "Sync failed — check server logs"}
            )

    async def event_stream():
        task = asyncio.create_task(run_sync())
        try:
            while True:
                message = await queue.get()
                yield f"data: {json.dumps(message)}\n\n"
                if message["type"] in ("done", "error"):
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/schedule")
async def set_schedule(interval: str = Form("off")):
    if interval not in ("off", "daily", "weekly"):
        return JSONResponse(
            {"ok": False, "message": "Invalid sync interval"}, status_code=400
        )
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('komga_sync_interval', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = ?",
            (interval, interval),
        )
    return RedirectResponse(url="/settings", status_code=303)
