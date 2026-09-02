import json
import logging
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import require_role
from app.config import HTTP_TIMEOUT
from app.database import get_db, get_setting
from app.services import romm, sync_jobs

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/sync/romm", dependencies=[Depends(require_role("admin"))]
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


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


@router.post("/test")
async def test_romm(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return {"ok": False, "message": "Invalid request body"}

    raw_url = body.get("url")
    raw_token = body.get("token")
    if (
        (raw_url is not None and not isinstance(raw_url, str))
        or (raw_token is not None and not isinstance(raw_token, str))
    ):
        return {"ok": False, "message": "Invalid request body"}

    url = (raw_url or "").strip().rstrip("/")
    token = (raw_token or "").strip()
    if not url or not token:
        with get_db() as db:
            url = url or get_setting(db, "romm_url")
            token = token or get_setting(db, "romm_api_token")

    if not url or not token:
        return {"ok": False, "message": "URL and Client API Token are required"}
    error = _validate_url(url)
    if error:
        return {"ok": False, "message": error}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            platforms_response = await client.get(
                f"{url}/api/platforms", headers=_headers(token)
            )
            if platforms_response.status_code == 401:
                return {"ok": False, "message": "RomM rejected the Client API Token"}
            if platforms_response.status_code == 403:
                return {
                    "ok": False,
                    "message": "Client API Token is missing platforms.read permission",
                }
            if platforms_response.status_code != 200:
                return {
                    "ok": False,
                    "message": f"Unexpected platform response: HTTP {platforms_response.status_code}",
                }
            try:
                platforms = platforms_response.json()
            except ValueError:
                return {"ok": False, "message": "RomM returned an invalid platform response"}
            if not isinstance(platforms, list):
                return {"ok": False, "message": "RomM returned an invalid platform response"}

            # A token can read /api/platforms but still lack roms.read. Probe the
            # ROM endpoint too so Test cannot report success for credentials that
            # the actual sync will immediately reject.
            roms_response = await client.get(
                f"{url}/api/roms",
                headers=_headers(token),
                params={"limit": 1, "offset": 0},
            )
    except httpx.ConnectError:
        return {"ok": False, "message": f"Cannot connect to {url}"}
    except httpx.HTTPError:
        return {"ok": False, "message": "Connection failed — check URL and network"}

    if roms_response.status_code == 401:
        return {"ok": False, "message": "RomM rejected the Client API Token"}
    if roms_response.status_code == 403:
        return {
            "ok": False,
            "message": "Client API Token is missing roms.read permission",
        }
    if roms_response.status_code != 200:
        return {
            "ok": False,
            "message": f"Unexpected ROM response: HTTP {roms_response.status_code}",
        }
    try:
        roms_payload = roms_response.json()
    except ValueError:
        return {"ok": False, "message": "RomM returned an invalid ROM response"}
    if not isinstance(roms_payload, (list, dict)):
        return {"ok": False, "message": "RomM returned an invalid ROM response"}

    return {"ok": True, "message": f"Connected — {len(platforms)} platform(s) found"}


@router.post("/public-url")
async def save_public_url(romm_public_url: str = Form("")):
    public_url = romm_public_url.strip().rstrip("/")
    if public_url and _validate_url(public_url):
        return RedirectResponse(
            url="/settings?romm_public_url_error=invalid", status_code=303
        )
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('romm_public_url', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = ?",
            (public_url, public_url),
        )
    return RedirectResponse(url="/settings", status_code=303)


@router.get("/platforms")
async def list_platforms():
    with get_db() as db:
        url = get_setting(db, "romm_url")
        token = get_setting(db, "romm_api_token")
    if not url or not token:
        return {"ok": False, "message": "RomM is not configured"}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(f"{url}/api/platforms", headers=_headers(token))
    except httpx.HTTPError:
        return {"ok": False, "message": f"Cannot connect to {url}"}

    if response.status_code in (401, 403):
        return {"ok": False, "message": "RomM rejected the Client API Token"}
    if response.status_code != 200:
        return {"ok": False, "message": f"RomM returned HTTP {response.status_code}"}
    try:
        raw = response.json()
    except ValueError:
        return {"ok": False, "message": "RomM returned an invalid platform response"}
    if not isinstance(raw, list):
        return {"ok": False, "message": "RomM returned an invalid platform response"}

    excluded = romm.get_excluded_platforms()
    platforms = []
    for platform in raw:
        if not isinstance(platform, dict) or platform.get("id") is None:
            continue
        platform_id = str(platform["id"])
        platforms.append(
            {
                "id": platform_id,
                "name": platform.get("display_name")
                or platform.get("custom_name")
                or platform.get("name")
                or platform.get("slug"),
                "slug": platform.get("slug") or platform.get("fs_slug"),
                "rom_count": platform.get("rom_count") or 0,
                "media_type": "digital_game",
                "included": platform_id not in excluded,
            }
        )
    return {"ok": True, "platforms": platforms}


@router.post("/platforms")
async def save_platforms(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "Invalid request body"}
    if not isinstance(body, dict):
        return {"ok": False, "message": "Invalid request body"}
    raw = body.get("excluded") or []
    if not isinstance(raw, list) or not all(
        isinstance(v, (str, int)) and not isinstance(v, bool) for v in raw
    ):
        return {"ok": False, "message": "Invalid request body"}
    excluded = [str(v) for v in raw]

    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('romm_excluded_platforms', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(excluded),),
        )
    return {"ok": True, "excluded": excluded}


@router.post("/platforms/cleanup")
async def cleanup_platforms():
    if sync_jobs.is_running("romm"):
        return JSONResponse(
            {
                "ok": False,
                "message": "RomM sync is running — wait for it to finish before cleanup",
            },
            status_code=409,
        )

    excluded = romm.get_excluded_platforms()
    if not excluded:
        return {"ok": True, "deleted": 0, "detached": 0}

    placeholders = ",".join("?" * len(excluded))
    deleted = 0
    detached = 0
    with get_db() as db:
        rows = db.execute(
            f"SELECT id, source FROM items WHERE romm_platform_id IN ({placeholders})",
            tuple(excluded),
        ).fetchall()
        for row in rows:
            if row["source"] == "romm":
                db.execute(
                    "UPDATE scan_log SET item_id = NULL WHERE item_id = ?", (row["id"],)
                )
                db.execute("DELETE FROM items WHERE id = ?", (row["id"],))
                deleted += 1
            else:
                db.execute(
                    "UPDATE items SET romm_id = NULL, romm_platform_id = NULL WHERE id = ?",
                    (row["id"],),
                )
                detached += 1
    return {"ok": True, "deleted": deleted, "detached": detached}


@router.get("/job")
async def romm_job_status():
    return sync_jobs.get_status("romm")


@router.post("/job")
async def start_romm_job():
    with get_db() as db:
        url = get_setting(db, "romm_url")
        token = get_setting(db, "romm_api_token")
    if not url or not token:
        return {
            "state": "error",
            "error": "RomM URL and Client API Token must be configured in Settings",
        }
    error = _validate_url(url)
    if error:
        return {"state": "error", "error": error}

    async def runner(on_progress):
        return await romm.sync(url, token, on_progress=on_progress)

    return sync_jobs.start("romm", runner, source="manual")


@router.post("")
async def sync_now():
    with get_db() as db:
        url = get_setting(db, "romm_url")
        token = get_setting(db, "romm_api_token")
    if not url or not token:
        return {"error": "RomM URL and Client API Token must be configured in Settings"}
    error = _validate_url(url)
    if error:
        return {"error": error}
    return await romm.sync(url, token)


@router.post("/schedule")
async def set_schedule(interval: str = Form("off")):
    if interval not in ("off", "daily", "weekly"):
        return JSONResponse(
            {"ok": False, "message": "Invalid sync interval"}, status_code=400
        )
    with get_db() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('romm_sync_interval', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = ?",
            (interval, interval),
        )
    return RedirectResponse(url="/settings", status_code=303)
