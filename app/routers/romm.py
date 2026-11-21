"""RomM native API and adapters for main's published sync endpoints."""

import asyncio
import json

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from app.auth import require_role
from app.config import HTTP_TIMEOUT
from app.database import get_db
from app.features import require_feature
from app.services import romm, romm_client, romm_records, romm_sync, sync_jobs
from app.services.catalogue_security import require_item_role

router = APIRouter(prefix="/api/romm")
legacy_router = APIRouter(prefix="/api/sync/romm", dependencies=[Depends(require_role("admin"))])
_ADMIN = [Depends(require_role("admin"))]
_SYNC = [Depends(require_role("admin")), Depends(require_feature("romm"))]


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return {}
    return body if isinstance(body, dict) else {}


def _validate_url(url: str) -> str | None:
    try:
        return None if romm_sync.clean_url(url) else "RomM URL is required"
    except (romm_client.RomMError, ValueError):
        return "Invalid RomM URL"


_headers = romm_client._headers


@router.get("/status", dependencies=_ADMIN)
async def status():
    config = romm_sync.configuration()
    return {"ok": True, "url": config["url"], "public_url": config["public_url"], "token_saved": config["token_saved"]}


@router.post("/settings", dependencies=_ADMIN)
async def save_settings(request: Request):
    body = await _json_body(request)
    if not isinstance(body.get("url"), str) or not all(isinstance(body.get(key, ""), str) for key in ("public_url", "token")):
        return {"ok": False, "message": "Invalid request"}
    if not isinstance(body.get("clear_token", False), bool):
        return {"ok": False, "message": "Invalid request"}
    try:
        romm_sync.save_configuration(url=body["url"], public_url=body.get("public_url", ""),
                                     token=body.get("token", ""), clear_token=body.get("clear_token", False))
    except (romm_client.RomMError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": "RomM settings saved"}


@legacy_router.post("/test")
@router.post("/test", dependencies=_ADMIN)
async def test_connection(request: Request):
    body = await _json_body(request)
    if any(body.get(key) is not None and not isinstance(body[key], str) for key in ("url", "token")):
        return {"ok": False, "message": "Invalid request"}
    try:
        config = romm_sync.configuration()
        url = romm_sync.clean_url(body.get("url") or config["url"])
        token = body.get("token") or config["token"]
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            rows = await romm_client.fetch_platforms(client, url, token)
            # Platform discovery alone does not prove roms.read permission.
            probe = await client.get(f"{url}/api/roms", headers=_headers(token), params={"limit": 1, "offset": 0})
        if probe.status_code in {401, 403}:
            return {"ok": False, "message": "Client API Token is missing roms.read permission"}
        if probe.status_code != 200 or not isinstance(probe.json(), (dict, list)):
            return {"ok": False, "message": "RomM returned an invalid ROM response"}
    except (romm_client.RomMError, ValueError, httpx.HTTPError) as exc:
        message = str(exc) if isinstance(exc, romm_client.RomMError) else "Could not connect to RomM"
        return {"ok": False, "message": message}
    return {"ok": True, "message": f"Connected — {len(rows)} platform(s) found"}


test_romm = test_connection


@legacy_router.post("/public-url")
async def save_public_url(romm_public_url: str = Form("")):
    try:
        public_url = romm_sync.clean_url(romm_public_url)
    except (romm_client.RomMError, ValueError):
        return RedirectResponse("/settings?romm_public_url_error=invalid", status_code=303)
    with get_db() as db:
        romm_sync._write(db, "romm_public_url", public_url)
    return RedirectResponse("/settings", status_code=303)


@legacy_router.get("/platforms")
@router.get("/platforms", dependencies=_ADMIN)
async def platforms():
    try:
        rows = await romm_sync.discover_platforms()
    except (romm_client.RomMError, ValueError) as exc:
        return {"ok": False, "message": str(exc), "platforms": []}
    return {"ok": True, "platforms": [{**row, "slug": row["romm_slug"], "media_type": "digital_game"} for row in rows]}


list_platforms = platforms


@legacy_router.post("/platforms")
@router.post("/platforms", dependencies=_ADMIN)
async def save_platforms(request: Request):
    body = await _json_body(request)
    if "platforms" in body:
        if not isinstance(body["platforms"], list):
            return {"ok": False, "message": "Invalid request"}
        try:
            romm_sync.save_platform_selection(body["platforms"])
        except romm_client.RomMError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": "RomM platform selection saved"}
    excluded = body.get("excluded", [])
    if not isinstance(excluded, list) or not all(type(value) in (int, str) and str(value).strip() for value in excluded):
        return {"ok": False, "message": "Invalid request body"}
    excluded = [str(value) for value in excluded]
    with get_db() as db:
        romm_sync._write(db, "romm_excluded_platforms", json.dumps(excluded))
    return {"ok": True, "excluded": excluded}


@legacy_router.post("/platforms/cleanup", dependencies=[Depends(require_feature("romm"))])
async def cleanup_platforms():
    if sync_jobs.is_running("romm") or not romm_sync._sync_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "message": "RomM sync is running"}, status_code=409)
    try:
        excluded = romm.get_excluded_platforms()
        with get_db() as db:
            counts = romm_records.cleanup_excluded_platforms(db, excluded)
        return {"ok": True, **counts}
    finally:
        romm_sync._sync_lock.release()


@legacy_router.get("/job", dependencies=[Depends(require_feature("romm"))])
async def romm_job_status():
    return sync_jobs.get_status("romm")


@legacy_router.post("/job", dependencies=[Depends(require_feature("romm"))])
async def start_romm_job():
    async def runner(on_progress):
        return await romm_sync.sync(on_progress=on_progress)
    return sync_jobs.start("romm", runner, source="manual")


@legacy_router.post("", dependencies=[Depends(require_feature("romm"))])
async def sync_now():
    await start_romm_job()
    final = await sync_jobs.wait("romm")
    stats = dict(final.get("stats") or {})
    stats["added"] = stats.get("created", 0)
    return {"error": final["error"]} if final.get("error") else stats


@router.get("/sync/stream", dependencies=_SYNC)
async def sync_stream():
    await start_romm_job()

    async def events():
        last = None
        while True:
            snapshot = sync_jobs.get_status("romm")
            marker = (snapshot["current"], snapshot["total"], snapshot["title"], snapshot["item_status"])
            if marker != last:
                yield f"data: {json.dumps(dict(type='progress', current=marker[0], total=marker[1], title=marker[2], status=marker[3]))}\n\n"
                last = marker
            if snapshot["state"] != "running":
                stats = dict(snapshot.get("stats") or {})
                stats["added"] = stats.get("created", 0)
                message = {"type": "error", "message": snapshot.get("error") or "Sync interrupted"} if snapshot["state"] != "completed" else {"type": "done", **stats}
                yield f"data: {json.dumps(message)}\n\n"
                break
            await asyncio.sleep(0.2)
    return StreamingResponse(events(), media_type="text/event-stream")


@legacy_router.post("/schedule")
async def set_schedule(interval: str = Form("off")):
    if interval not in {"off", "daily", "weekly"}:
        return JSONResponse({"ok": False, "message": "Invalid sync interval"}, status_code=400)
    with get_db() as db:
        romm_sync._write(db, "romm_sync_interval", interval)
    return RedirectResponse("/settings", status_code=303)


@router.get("/items/{item_id}/action", dependencies=[Depends(require_role("viewer")), Depends(require_feature("romm"))])
async def item_action(item_id: int):
    with get_db() as db:
        require_item_role(db, item_id, "viewer")
    url = romm_sync.item_action(item_id)
    return {"ok": bool(url), "url": url}


@router.get("/items/{item_id}/open", dependencies=[Depends(require_role("viewer")), Depends(require_feature("romm"))])
async def open_item(item_id: int):
    result = await item_action(item_id)
    return RedirectResponse(result["url"], status_code=302) if result["url"] else RedirectResponse(f"/item/{item_id}", status_code=303)
