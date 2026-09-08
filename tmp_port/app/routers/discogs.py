"""Optional Discogs exact-pressing enrichment for Music."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_role
from app.config import HTTP_TIMEOUT, MUSIC_MEDIA_TYPES
from app.database import get_db, get_setting
from app.services import discogs, discogs_catalog, music_catalog

router = APIRouter()


def _stored_token(db) -> str:
    return (get_setting(db, "discogs_token") or "").strip()


def _music_context(db, item_id: int):
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item or item["media_type"] not in MUSIC_MEDIA_TYPES:
        return None, None
    return item, music_catalog.get_release(db, item_id)


def _provider_message(result) -> str:
    return {
        "no_credential": "Configure a Discogs token first.",
        "rate_limited": "Discogs is rate-limiting requests. Try again shortly.",
        "transport_failed": "Discogs could not be reached.",
        "rejected": "Discogs rejected the token or request.",
        "no_match": "No matching Discogs releases were found.",
    }.get(getattr(result, "outcome", ""), "Discogs request failed.")


@router.post("/api/discogs/test")
async def test_discogs_connection(
    token: str = Form(""),
    _=Depends(require_role("admin")),
):
    candidate = token.strip()
    if not candidate:
        with get_db() as db:
            candidate = _stored_token(db)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await discogs.test_connection(candidate, client)
    if result.found:
        username = (result.payload or {}).get("username") or "Discogs user"
        return HTMLResponse(f"Connected to Discogs as {username}")
    return HTMLResponse(_provider_message(result), status_code=400)


@router.get("/api/discogs/items/{item_id}/card")
async def discogs_card(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    with get_db() as db:
        item, release = _music_context(db, item_id)
        enrichment = discogs_catalog.get_enrichment(db, item_id) if release else None
        configured = bool(_stored_token(db))
    if not item or not release:
        return HTMLResponse("")
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/discogs_card.html",
        {
            "item": item,
            "release": release,
            "discogs": enrichment,
            "configured": configured,
        },
    )


@router.get("/music/item/{item_id}/discogs")
async def match_discogs_release(
    request: Request,
    item_id: int,
    q: str = Query("", max_length=200),
    artist: str = Query("", max_length=200),
    barcode: str = Query("", max_length=40),
    catalog_number: str = Query("", max_length=100),
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        item, release = _music_context(db, item_id)
        token = _stored_token(db)
    if not item or not release:
        return RedirectResponse(f"/item/{item_id}", status_code=303)
    if not token:
        return RedirectResponse("/settings", status_code=303)

    q = q.strip()
    artist = artist.strip()
    barcode = barcode.strip()
    catalog_number = catalog_number.strip()
    searched = any((q, artist, barcode, catalog_number))
    results = []
    error = ""
    if searched:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            result = await discogs.search_releases(
                q,
                client,
                token=token,
                artist=artist or None,
                barcode=barcode or None,
                catalog_number=catalog_number or None,
            )
        if result.found:
            results = result.payload or []
        else:
            error = _provider_message(result)

    return request.app.state.templates.TemplateResponse(
        request,
        "discogs_match.html",
        {
            "item": item,
            "release": release,
            "results": results,
            "error": error,
            "searched": searched,
            "q": q or item["title"],
            "artist": artist or (item["authors"] or ""),
            "barcode": barcode or (item["upc"] or ""),
            "catalog_number": catalog_number or (release.get("catalog_number") or ""),
        },
    )


@router.post("/api/discogs/items/{item_id}/select")
async def select_discogs_release(
    item_id: int,
    release_id: int = Form(...),
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        item, release = _music_context(db, item_id)
        token = _stored_token(db)
    if not item or not release:
        return HTMLResponse("Music release not found", status_code=404)
    if not token:
        return RedirectResponse("/settings", status_code=303)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await discogs.lookup_release(release_id, client, token=token)
    if not result.found:
        return HTMLResponse(_provider_message(result), status_code=502)
    with get_db() as db:
        discogs_catalog.save_enrichment(db, item_id, result.payload)
    return RedirectResponse(f"/music/item/{item_id}", status_code=303)


@router.post("/api/discogs/items/{item_id}/refresh")
async def refresh_discogs_release(
    item_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        item, release = _music_context(db, item_id)
        enrichment = discogs_catalog.get_enrichment(db, item_id) if release else None
        token = _stored_token(db)
    if not item or not release or not enrichment:
        return HTMLResponse("Discogs pressing not found", status_code=404)
    if not token:
        return RedirectResponse("/settings", status_code=303)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await discogs.lookup_release(
            enrichment["discogs_release_id"], client, token=token
        )
    if not result.found:
        return HTMLResponse(_provider_message(result), status_code=502)
    with get_db() as db:
        discogs_catalog.save_enrichment(db, item_id, result.payload)
    return RedirectResponse(f"/music/item/{item_id}", status_code=303)


@router.post("/api/discogs/items/{item_id}/remove")
async def remove_discogs_release(
    item_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        item, release = _music_context(db, item_id)
        if not item or not release:
            return HTMLResponse("Music release not found", status_code=404)
        if not discogs_catalog.clear_enrichment(db, item_id):
            return HTMLResponse("Discogs pressing not found", status_code=404)
    return RedirectResponse(f"/music/item/{item_id}", status_code=303)
