"""First-class music catalogue: release search, add, enrich and copy metadata."""

from __future__ import annotations

import sqlite3

import httpx
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_role
from app.config import HTTP_TIMEOUT, MEDIA_TYPES, MUSIC_MEDIA_TYPES
from app.database import get_db, get_setting
from app.services import covers, discogs, music_catalog, musicbrainz
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


def _discogs_error(result) -> str | None:
    if result is None or result.found:
        return None
    return {
        "no_credential": "Configure a Discogs API token in Settings first.",
        "rate_limited": "Discogs is rate-limiting requests. Try again shortly.",
        "transport_failed": "Discogs could not be reached.",
        "rejected": "Discogs rejected the configured token.",
        "no_match": "No matching Discogs releases were found.",
    }.get(result.outcome, "Discogs search failed.")


def _music_item(db, item_id: int | None):
    if not item_id:
        return None
    row = db.execute(
        "SELECT id, title, authors, media_type, upc, location_id, cover_path, owned "
        "FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not row or row["media_type"] not in MUSIC_MEDIA_TYPES:
        return None
    return row


@router.get("/music")
async def music_page(
    request: Request,
    q: str = Query(""),
    artist: str = Query(""),
    barcode: str = Query(""),
    catalog_number: str = Query(""),
    item_id: int | None = Query(None),
    _=Depends(require_role("viewer")),
):
    """Search exact MusicBrainz releases, optionally for an existing scan.

    A UPC scan can create a valid music item even when no music metadata
    provider runs on the scan request itself. Passing ``item_id`` turns this
    page into an in-place enrichment picker: barcode is preferred because it
    identifies the physical release more strongly than a cleaned retail title.
    """
    q = q.strip()[:200]
    artist = artist.strip()[:200]
    barcode = upc_svc.normalize_barcode(barcode)[:32]
    catalog_number = catalog_number.strip()[:100]

    with get_db() as db:
        target_item = _music_item(db, item_id)
        locations = db.execute(
            "SELECT * FROM locations ORDER BY sort_order, name"
        ).fetchall()

    if target_item and not (q or artist or barcode or catalog_number):
        if target_item["upc"]:
            barcode = target_item["upc"]
        else:
            q = target_item["title"] or ""
            artist = target_item["authors"] or ""

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
            "target_item": target_item,
            "music_media_types": {
                key: MEDIA_TYPES[key] for key in MEDIA_TYPES if key in MUSIC_MEDIA_TYPES
            },
        },
    )


async def _apply_release_artwork(item_id: int, release_id: str) -> None:
    """Fill a missing cover from the exact release without overwriting uploads."""
    with get_db() as db:
        row = db.execute("SELECT cover_path FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row or row["cover_path"]:
        return

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        art_result = await musicbrainz.cover_art(release_id, client)
        candidates = art_result.payload if art_result.found else []
        front = next((c for c in candidates if c.get("front")), None)
        chosen = front or (candidates[0] if candidates else None)
        if not chosen:
            return
        cover_path = await covers._download_to_item(item_id, chosen["url"], client)
    if cover_path:
        with get_db() as db:
            db.execute(
                "UPDATE items SET cover_path = ?, updated_at = datetime('now') WHERE id = ?",
                (cover_path, item_id),
            )


@router.post("/api/music/add")
async def add_music_release(
    request: Request,
    release_id: str = Form(...),
    media_type: str = Form(""),
    location_id: int | None = Form(None),
    owned: int = Form(1),
    target_item_id: int | None = Form(None),
    _=Depends(require_role("editor")),
):
    """Add a new exact release or attach it to a title-only scanned music item."""
    release_id = release_id.strip()
    if not release_id:
        return RedirectResponse("/music", status_code=303)

    try:
        with get_db() as db:
            target_item = _music_item(db, target_item_id)
            if target_item_id and not target_item:
                return HTMLResponse("The target item is not a music item", status_code=400)
            loc_id = (
                target_item["location_id"]
                if target_item
                else validated_location_id(db, location_id)
            )
            existing = db.execute(
                "SELECT item_id FROM music_releases WHERE musicbrainz_release_id = ?",
                (release_id,),
            ).fetchone()
    except UnknownLocationError:
        return HTMLResponse("Selected location no longer exists", status_code=400)

    if existing and (not target_item or existing["item_id"] != target_item["id"]):
        return RedirectResponse(f"/item/{existing['item_id']}?from=music", status_code=303)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await musicbrainz.lookup_release(release_id, client)
    if not result.found:
        return HTMLResponse(_search_error(result) or "Release lookup failed", status_code=502)
    release = result.payload

    if media_type not in MUSIC_MEDIA_TYPES:
        media_type = _infer_media_type(release)
    publish_year = _year(release.get("release_date")) or _year(release.get("first_release_date"))
    provider_barcode = upc_svc.normalize_upc(release.get("barcode") or "") or None

    try:
        with get_db() as db:
            if target_item:
                item_id = target_item["id"]
                # Preserve the barcode the user actually scanned. Only fill a
                # blank barcode from MusicBrainz when it will not collide with
                # Shelf's existing unique (upc, media_type) identity.
                item_barcode = target_item["upc"]
                if not item_barcode and provider_barcode:
                    collision = db.execute(
                        "SELECT 1 FROM items WHERE upc = ? AND media_type = ? AND id != ?",
                        (provider_barcode, media_type, item_id),
                    ).fetchone()
                    if not collision:
                        item_barcode = provider_barcode
                db.execute(
                    "UPDATE items SET title = ?, authors = ?, upc = ?, media_type = ?, "
                    "publisher = ?, publish_year = ?, source = 'musicbrainz', "
                    "updated_at = datetime('now') WHERE id = ?",
                    (
                        release["title"],
                        release.get("artist_credit"),
                        item_barcode,
                        media_type,
                        release.get("label"),
                        publish_year,
                        item_id,
                    ),
                )
                music_catalog.save_release(db, item_id, release)
            else:
                item_id = insert_item(
                    db,
                    title=release["title"],
                    authors=release.get("artist_credit"),
                    upc=provider_barcode,
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

    # Network artwork work is outside the catalogue write transaction.
    await _apply_release_artwork(item_id, release_id)
    return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)


@router.get("/api/music/items/{item_id}/detail")
async def music_item_detail(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    with get_db() as db:
        item = db.execute(
            "SELECT id, title, authors, media_type, upc FROM items WHERE id = ?",
            (item_id,),
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
    await _apply_release_artwork(item_id, release_id)
    return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)



@router.get("/music/item/{item_id}/discogs")
async def match_discogs_release(
    request: Request,
    item_id: int,
    q: str = Query(""),
    artist: str = Query(""),
    barcode: str = Query(""),
    catalog_number: str = Query(""),
    _=Depends(require_role("editor")),
):
    """Pick the exact Discogs Release that represents this physical pressing."""
    q = q.strip()[:200]
    artist = artist.strip()[:200]
    barcode = upc_svc.normalize_barcode(barcode)[:32]
    catalog_number = catalog_number.strip()[:100]

    with get_db() as db:
        item = _music_item(db, item_id)
        release = music_catalog.get_release(db, item_id) if item else None
        token = get_setting(db, "discogs_token")
    if not item:
        return RedirectResponse("/music", status_code=303)
    if not release:
        return RedirectResponse(f"/music?item_id={item_id}", status_code=303)

    if not (q or artist or barcode or catalog_number):
        q = item["title"] or ""
        artist = release.get("artist_credit") or item["authors"] or ""
        barcode = item["upc"] or ""
        catalog_number = release.get("catalog_number") or ""

    results: list[dict] = []
    error = None
    if not token:
        error = "Configure a Discogs API token in Settings before matching a pressing."
    else:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            if barcode:
                result = await discogs.search_releases("", client, token=token, barcode=barcode, limit=30)
            elif catalog_number:
                result = await discogs.search_releases(
                    "", client, token=token, artist=artist or None,
                    catalog_number=catalog_number, limit=30,
                )
            else:
                result = await discogs.search_releases(
                    q, client, token=token, artist=artist or None, limit=30,
                )
        if result.found:
            results = result.payload or []
        else:
            error = _discogs_error(result)

    return request.app.state.templates.TemplateResponse(
        request,
        "discogs_match.html",
        {
            "item": item,
            "release": release,
            "results": results,
            "error": error,
            "discogs_configured": bool(token),
            "q": q,
            "artist": artist,
            "barcode": barcode,
            "catalog_number": catalog_number,
        },
    )


@router.post("/api/music/items/{item_id}/discogs")
async def attach_discogs_release(
    item_id: int,
    release_id: int = Form(...),
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        item = _music_item(db, item_id)
        current = music_catalog.get_release(db, item_id) if item else None
        token = get_setting(db, "discogs_token")
    if not item or not current:
        return HTMLResponse("This item needs an exact MusicBrainz release first", status_code=400)
    if not token:
        return HTMLResponse("Discogs API token is not configured", status_code=400)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await discogs.lookup_release(release_id, client, token=token)
    if not result.found:
        return HTMLResponse(_discogs_error(result) or "Discogs release lookup failed", status_code=502)

    with get_db() as db:
        if not music_catalog.save_discogs_enrichment(db, item_id, result.payload):
            return HTMLResponse("Music release no longer exists", status_code=404)
    return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)


@router.post("/api/music/items/{item_id}/discogs/refresh")
async def refresh_discogs_release(
    item_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        row = db.execute(
            "SELECT discogs_release_id FROM music_releases WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        token = get_setting(db, "discogs_token")
    if not row or not row["discogs_release_id"]:
        return HTMLResponse("This copy has no Discogs release match", status_code=400)
    if not token:
        return HTMLResponse("Discogs API token is not configured", status_code=400)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await discogs.lookup_release(row["discogs_release_id"], client, token=token)
    if not result.found:
        return HTMLResponse(_discogs_error(result) or "Discogs refresh failed", status_code=502)
    with get_db() as db:
        music_catalog.save_discogs_enrichment(db, item_id, result.payload)
    return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)


@router.post("/api/music/items/{item_id}/discogs/clear")
async def clear_discogs_release(
    item_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        music_catalog.clear_discogs_enrichment(db, item_id)
    return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)
