"""Komga native API plus genuine adapters for main's published sync endpoints."""

import asyncio
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.services import item_write, komga, komga_libraries, komga_records, komga_sync, sync_jobs
from app.services.catalogue_security import require_item_role

router = APIRouter(prefix="/api/komga")
legacy_router = APIRouter(prefix="/api/sync/komga", dependencies=[Depends(require_role("admin"))])
_ADMIN = [Depends(require_role("admin"))]
_SYNC = [Depends(require_role("admin")), Depends(require_feature("komga"))]


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return {}
    return body if isinstance(body, dict) else {}


def _validate_url(url: str) -> str | None:
    try:
        return None if komga_sync.clean_url(url) else "Komga URL is required"
    except (komga_libraries.KomgaError, ValueError):
        return "Invalid Komga URL"


_headers = komga_libraries._headers


@router.get("/status", dependencies=_ADMIN)
async def status():
    config = komga_sync.configuration()
    return {"ok": True, "url": config["url"], "public_url": config["public_url"], "api_key_saved": config["api_key_saved"]}


@router.post("/settings", dependencies=_ADMIN)
async def save_settings(request: Request):
    body = await _json_body(request)
    if not isinstance(body.get("url"), str) or not all(isinstance(body.get(key, ""), str) for key in ("public_url", "api_key")):
        return {"ok": False, "message": "Invalid request"}
    if not isinstance(body.get("clear_api_key", False), bool):
        return {"ok": False, "message": "Invalid request"}
    try:
        komga_sync.save_configuration(url=body["url"], public_url=body.get("public_url", ""),
                                      api_key=body.get("api_key", ""), clear_api_key=body.get("clear_api_key", False))
    except (komga_libraries.KomgaError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": "Komga settings saved"}


@legacy_router.post("/test")
@router.post("/test", dependencies=_ADMIN)
async def test_connection(request: Request):
    body = await _json_body(request)
    if any(body.get(key) is not None and not isinstance(body[key], str) for key in ("url", "api_key")):
        return {"ok": False, "message": "Invalid request"}
    try:
        rows = await komga_sync.discover_libraries(url=body.get("url") or None, api_key=body.get("api_key") or None)
    except (komga_libraries.KomgaError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"Connected — {len(rows)} library(ies) found"}


test_komga = test_connection


@legacy_router.post("/public-url")
async def save_public_url(komga_public_url: str = Form("")):
    try:
        public_url = komga_sync.clean_url(komga_public_url)
    except (komga_libraries.KomgaError, ValueError):
        return RedirectResponse("/settings?komga_public_url_error=invalid", status_code=303)
    with get_db() as db:
        komga_sync._write(db, "komga_public_url", public_url)
    return RedirectResponse("/settings", status_code=303)


@legacy_router.get("/libraries")
@router.get("/libraries", dependencies=_ADMIN)
async def libraries():
    try:
        rows = await komga_sync.discover_libraries()
    except (komga_libraries.KomgaError, ValueError) as exc:
        return {"ok": False, "message": str(exc), "libraries": []}
    return {"ok": True, "libraries": [{**row, "media_type": f"digital_{row['kind']}",
             "media_type_label": komga.KOMGA_MEDIA_TYPE_LABELS[f"digital_{row['kind']}"],
             "explicit_media_type": row["explicit_kind"]} for row in rows]}


list_libraries = libraries


@legacy_router.post("/libraries")
@router.post("/libraries", dependencies=_ADMIN)
async def save_libraries(request: Request):
    body = await _json_body(request)
    if "libraries" in body:
        if not isinstance(body["libraries"], list):
            return {"ok": False, "message": "Invalid request"}
        try:
            komga_sync.save_library_selection(body["libraries"])
        except komga_libraries.KomgaError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": "Komga library selection saved"}
    excluded, mappings = body.get("excluded", []), body.get("media_types", {})
    if not isinstance(excluded, list) or not all(isinstance(key, str) and key.strip() for key in excluded):
        return {"ok": False, "message": "Invalid request body"}
    if not isinstance(mappings, dict) or not all(isinstance(key, str) and key.strip() and isinstance(value, str)
            and value in komga.KOMGA_LIBRARY_MEDIA_TYPES for key, value in mappings.items()):
        return {"ok": False, "message": "Invalid request body"}
    mappings = {key.strip(): value for key, value in mappings.items()}
    if not komga_sync._sync_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "message": "Komga sync is running"}, status_code=409)
    try:
        with get_db() as db, item_write._write_scope(db):
            changed = komga.apply_library_media_types(db, mappings)
            komga_sync._write(db, "komga_excluded_libraries", json.dumps(excluded))
            komga_sync._write(db, "komga_library_media_types", json.dumps(mappings, sort_keys=True))
            komga_sync._write(db, "komga_library_kinds", komga_libraries.dump_library_kinds({
                key: value.removeprefix("digital_") for key, value in mappings.items()
            }))
    except (komga_libraries.KomgaError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}
    finally:
        komga_sync._sync_lock.release()
    return {"ok": True, "excluded": excluded, "media_types": mappings, "reclassified": changed}


@legacy_router.post("/libraries/cleanup", dependencies=[Depends(require_feature("komga"))])
async def cleanup_libraries():
    if sync_jobs.is_running("komga") or not komga_sync._sync_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "message": "Komga sync is running"}, status_code=409)
    try:
        excluded = komga.get_excluded_libraries()
        deleted = detached = 0
        with get_db() as db, item_write._write_scope(db):
            records = db.execute(
                "SELECT kr.komga_id, kr.library_id, i.id, i.source FROM komga_records kr JOIN items_live i ON i.id = kr.item_id "
                "UNION SELECT dh.external_id, dh.library_id, i.id, i.source FROM digital_holdings dh "
                "JOIN items_live i ON i.id = dh.item_id WHERE dh.provider = 'komga' "
                "UNION SELECT komga_id, komga_library_id, id, source FROM items_live WHERE komga_id IS NOT NULL"
            ).fetchall()
            for row in records:
                if str(row["library_id"]) not in excluded:
                    continue
                item = komga_records._attachment(db, str(row["komga_id"]))
                if item is None or item["deleted_at"] is not None:
                    continue
                require_item_role(db, item["id"], "editor")
                retained = any(other["id"] == row["id"] and str(other["library_id"]) not in excluded for other in records)
                retained = retained or db.execute(
                    "SELECT 1 FROM digital_holdings WHERE item_id = ? AND provider != 'komga' "
                    "UNION SELECT 1 FROM copies_live WHERE item_id = ? LIMIT 1",
                    (item["id"], item["id"]),
                ).fetchone() is not None or bool(item["abs_id"] or item["romm_id"])
                if row["source"] == "komga" and not retained:
                    deleted += int(item_write.trash_item(db, row["id"]))
                else:
                    komga_records.detach_record(db, row["komga_id"])
                    detached += 1
        return {"ok": True, "deleted": deleted, "detached": detached}
    finally:
        komga_sync._sync_lock.release()


@legacy_router.get("/job", dependencies=[Depends(require_feature("komga"))])
async def komga_job_status():
    return sync_jobs.get_status("komga")


@legacy_router.post("/job", dependencies=[Depends(require_feature("komga"))])
async def start_komga_job():
    async def runner(on_progress):
        return await komga_sync.sync(on_progress=on_progress)
    return sync_jobs.start("komga", runner, source="manual")


@legacy_router.post("", dependencies=[Depends(require_feature("komga"))])
async def sync_now():
    started = await start_komga_job()
    if started.get("state") == "error":
        return {"error": started["error"]}
    final = await sync_jobs.wait("komga")
    stats = dict(final.get("stats") or {})
    stats["added"] = stats.get("created", 0)
    return {"error": final["error"]} if final.get("error") else stats


@legacy_router.get("/stream", dependencies=[Depends(require_feature("komga"))])
@router.get("/sync/stream", dependencies=_SYNC)
async def sync_stream():
    await start_komga_job()

    async def events():
        last = None
        while True:
            snapshot = sync_jobs.get_status("komga")
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
        komga_sync._write(db, "komga_sync_interval", interval)
    return RedirectResponse("/settings", status_code=303)


@router.get("/items/{item_id}/action", dependencies=[Depends(require_role("viewer")), Depends(require_feature("komga"))])
async def item_action(item_id: int):
    with get_db() as db:
        require_item_role(db, item_id, "viewer")
    url = komga_sync.item_action(item_id)
    return {"ok": bool(url), "url": url}
