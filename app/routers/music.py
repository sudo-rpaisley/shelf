"""First-class music catalogue: release search, add, detail and copy metadata."""

from __future__ import annotations

import sqlite3

import httpx
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_role
from app.config import HTTP_TIMEOUT, MEDIA_TYPES, MUSIC_MEDIA_TYPES
from app.database import get_db
from app.services import covers, music_catalog, musicbrainz
from app.services import upc as upc_svc
from app.services.item_write import insert_item
from app.services.write_targets import UnknownLocationError, validated_location_id

router = APIRouter()


def _year(value: str | None) -> int | None:
    text = (value or "").strip()
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None


def _infer_media_type(release: dict) -> str:
    """Map MusicBrainz's exact medium format to Shelf's owned-item family."""
    media = release.get("media") or []
    formats = [
        str(m.get("format") or "").casefold()
        for m in media
        if isinstance(m, dict)
    ]
    summary = str(release.get("format_summary") or "").casefold()
    if not formats and summary:
        formats = [summary]
    for fmt in formats:
        if "vinyl" in fmt or fmt in {"7\"", "10\"", "12\""}:
            return "vinyl"
        if "cassette" in fmt:
            return "cassette"
        if fmt == "cd" or "compact disc" in fmt:
            return "cd"
        if "digital" in fmt:
            return "digital_music"
    return "music_other"


def _search_error(result) -> str | None:
    if result is None or result.found:
        return None
    return {
        "rate_limited": "MusicBrainz is rate-limiting requests. Try the search again shortly.",
        "transport_failed": "MusicBrainz could not be reached.",
        "rejected": "MusicBrainz rejected the request.",
        "no_match": "No matching releases were found.",
    }.get(result.outcome, "MusicBrainz search failed.")


@router.get("/music")
async def music_page(
    request: Request,
    q: str = Query(""),
    artist: str = Query(""),
    barcode: str = Query(""),
    catalog_number: str = Query(""),
    _=Depends(require_role("viewer")),
):
    """Search MusicBrainz exact releases and add the chosen edition to Shelf."""
    q = q.strip()[:200]
    artist = artist.strip()[:200]
    barcode = upc_svc.normalize_barcode(barcode)[:32]
    catalog_number = catalog_number.strip()[:100]
    results: list[dict] = []
    error = None

    if q or artist or barcode or catalog_number:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            result = await musicbrainz.search_releases(
                q,
                client,
                artist=artist or None,
                barcode=barcode or None,
                catalog_number=catalog_number or None,
                limit=20,
            )
        if result.found:
            results = result.payload or []
            for release in results:
                release["shelf_media_type"] = _infer_media_type(release)
                release["shelf_media_label"] = MEDIA_TYPES[release["shelf_media_type"]]
        else:
            error = _search_error(result)

    with get_db() as db:
        locations = db.execute(
            "SELECT * FROM locations ORDER BY sort_order, name"
        ).fetchall()

    return request.app.state.templates.TemplateResponse(
        request,
        "music.html",
        {
            "results": results,
            "error": error,
            "q": q,
            "artist": artist,
            "barcode": barcode,
            "catalog_number": catalog_number,
            "locations": locations,
            "music_media_types": {
                key: MEDIA_TYPES[key] for key in MEDIA_TYPES if key in MUSIC_MEDIA_TYPES
            },
        },
    )


@router.post("/api/music/add")
async def add_music_release(
    request: Request,
    release_id: str = Form(...),
    media_type: str = Form(""),
    location_id: int | None = Form(None),
    owned: int = Form(1),
    _=Depends(require_role("editor")),
):
    release_id = release_id.strip()
    if not release_id:
        return RedirectResponse("/music", status_code=303)

    try:
        with get_db() as db:
            loc_id = validated_location_id(db, location_id)
            existing = db.execute(
                "SELECT item_id FROM music_releases WHERE musicbrainz_release_id = ?",
                (release_id,),
            ).fetchone()
    except UnknownLocationError:
        return HTMLResponse("Selected location no longer exists", status_code=400)

    if existing:
        return RedirectResponse(f"/item/{existing['item_id']}?from=music", status_code=303)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await musicbrainz.lookup_release(release_id, client)
    if not result.found:
        return HTMLResponse(_search_error(result) or "Release lookup failed", status_code=502)
    release = result.payload

    if media_type not in MUSIC_MEDIA_TYPES:
        media_type = _infer_media_type(release)

    barcode = upc_svc.normalize_upc(release.get("barcode") or "") or None
    publish_year = _year(release.get("release_date")) or _year(release.get("first_release_date"))

    try:
        with get_db() as db:
            item_id = insert_item(
                db,
                title=release["title"],
                authors=release.get("artist_credit"),
                upc=barcode,
                media_type=media_type,
                publisher=release.get("label"),
                publish_year=publish_year,
                location_id=loc_id,
                owned=1 if owned else 0,
                source="musicbrainz",
            )
            music_catalog.save_release(db, item_id, release)
    except sqlite3.IntegrityError:
        with get_db() as db:
            existing = db.execute(
                "SELECT item_id FROM music_releases WHERE musicbrainz_release_id = ?",
                (release_id,),
            ).fetchone()
        if existing:
            return RedirectResponse(f"/item/{existing['item_id']}?from=music", status_code=303)
        raise

    # Artwork is deliberately outside the insert transaction: a slow or absent
    # Cover Art Archive response must not roll back valid catalogue metadata.
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        art_result = await musicbrainz.cover_art(release_id, client)
        candidates = art_result.payload if art_result.found else []
        front = next((c for c in candidates if c.get("front")), None)
        chosen = front or (candidates[0] if candidates else None)
        if chosen:
            cover_path = await covers._download_to_item(item_id, chosen["url"], client)
            if cover_path:
                with get_db() as db:
                    db.execute(
                        "UPDATE items SET cover_path = ?, updated_at = datetime('now') WHERE id = ?",
                        (cover_path, item_id),
                    )

    return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)


@router.get("/api/music/items/{item_id}/detail")
async def music_item_detail(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    with get_db() as db:
        item = db.execute(
            "SELECT id, title, media_type, upc FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not item or item["media_type"] not in MUSIC_MEDIA_TYPES:
            return HTMLResponse("")
        release = music_catalog.get_release(db, item_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/music_detail.html",
        {"item": item, "release": release},
    )


@router.get("/music/item/{item_id}/edit")
async def edit_music_copy(
    request: Request,
    item_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        item = db.execute(
            "SELECT id, title, media_type, upc FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        release = music_catalog.get_release(db, item_id) if item else None
    if not item or item["media_type"] not in MUSIC_MEDIA_TYPES or not release:
        return RedirectResponse(f"/item/{item_id}", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request,
        "music_edit.html",
        {"item": item, "release": release},
    )


@router.post("/api/music/items/{item_id}/copy")
async def update_music_copy(
    item_id: int,
    edition_notes: str = Form(""),
    media_condition: str = Form(""),
    packaging_condition: str = Form(""),
    condition_notes: str = Form(""),
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        music_catalog.update_copy_details(
            db,
            item_id,
            edition_notes=edition_notes.strip() or None,
            media_condition=media_condition.strip() or None,
            packaging_condition=packaging_condition.strip() or None,
            condition_notes=condition_notes.strip() or None,
        )
    return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)


@router.post("/api/music/items/{item_id}/identifiers")
async def add_music_identifier(
    item_id: int,
    identifier_type: str = Form(...),
    value: str = Form(...),
    description: str = Form(""),
    _=Depends(require_role("editor")),
):
    try:
        with get_db() as db:
            music_catalog.add_identifier(
                db, item_id, identifier_type, value, description or None
            )
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=400)
    return RedirectResponse(f"/music/item/{item_id}/edit", status_code=303)


@router.post("/api/music/items/{item_id}/identifiers/{identifier_id}/delete")
async def delete_music_identifier(
    item_id: int,
    identifier_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        music_catalog.remove_identifier(db, item_id, identifier_id)
    return RedirectResponse(f"/music/item/{item_id}/edit", status_code=303)


@router.post("/api/music/items/{item_id}/refresh")
async def refresh_music_metadata(
    item_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        release_row = db.execute(
            "SELECT musicbrainz_release_id FROM music_releases WHERE item_id = ?",
            (item_id,),
        ).fetchone()
    if not release_row or not release_row["musicbrainz_release_id"]:
        return HTMLResponse("This copy has no MusicBrainz release ID", status_code=400)

    release_id = release_row["musicbrainz_release_id"]
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await musicbrainz.lookup_release(release_id, client)
    if not result.found:
        return HTMLResponse(_search_error(result) or "Release refresh failed", status_code=502)
    release = result.payload
    with get_db() as db:
        music_catalog.save_release(db, item_id, release)
        db.execute(
            "UPDATE items SET title = ?, authors = ?, publisher = ?, publish_year = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (
                release["title"],
                release.get("artist_credit"),
                release.get("label"),
                _year(release.get("release_date")) or _year(release.get("first_release_date")),
                item_id,
            ),
        )
    return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)
