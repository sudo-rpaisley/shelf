"""Exact music releases, in-place scan enrichment and optional pressing metadata."""

from __future__ import annotations

import sqlite3

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import HTTP_TIMEOUT, MEDIA_TYPES, MUSIC_MEDIA_TYPES
from app.database import get_db, get_setting
from app.features import require_feature
from app.services import covers, discogs, discogs_selection, item_write, music_artwork, music_catalog, musicbrainz
from app.services import upc as upc_svc
from app.services.catalogue_security import require_item_role
from app.services.catalogue_security import require_catalogue_role as require_role
from app.services.item_write import insert_item, update_item_fields
from app.services.write_targets import UnknownLocationError, validated_location_id

router = APIRouter(dependencies=[Depends(require_feature("music"))])


def _year(value: str | None) -> int | None:
    text = str(value or "").strip()
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None


def _infer_media_type(release: dict) -> str | None:
    """Only recognised provider medium evidence supplies a format; never guess CD."""
    formats = [str(m.get("format") or "").casefold() for m in release.get("media") or [] if isinstance(m, dict)]
    if not formats and release.get("format_summary"):
        formats = [str(release["format_summary"]).casefold()]
    types = set()
    for fmt in formats:
        if "vinyl" in fmt or fmt in {'7"', '10"', '12"'}:
            types.add("vinyl")
        elif "cassette" in fmt:
            types.add("cassette")
        elif fmt == "cd" or "compact disc" in fmt:
            types.add("cd")
        elif "digital" in fmt:
            types.add("digital_music")
    return next(iter(types)) if len(types) == 1 else None


def _provider_error(result) -> str | None:
    if result is None or result.found:
        return None
    return {"rate_limited": "MusicBrainz is rate-limiting requests. Try again shortly.",
            "transport_failed": "MusicBrainz could not be reached.", "rejected": "MusicBrainz rejected the request.",
            "no_match": "No matching releases were found."}.get(result.outcome, "MusicBrainz search failed.")


_search_error = _provider_error


def _discogs_error(result) -> str | None:
    if result is None or result.found:
        return None
    return {"no_credential": "Configure a Discogs API token in Settings first.",
            "rate_limited": "Discogs is rate-limiting requests. Try again shortly.",
            "transport_failed": "Discogs could not be reached.", "rejected": "Discogs rejected the configured token.",
            "no_match": "No matching Discogs releases were found."}.get(result.outcome, "Discogs lookup failed.")


def _music_types_sql() -> tuple[str, tuple[str, ...]]:
    values = tuple(sorted(MUSIC_MEDIA_TYPES))
    return ",".join("?" for _ in values), values


def _music_item(db, item_id: int | None):
    row = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone() if item_id else None
    return row if row and row["media_type"] in MUSIC_MEDIA_TYPES else None


async def _apply_release_artwork(item_id: int, release_id: str) -> None:
    with get_db() as db:
        require_item_role(db, item_id, "editor")
        item = _music_item(db, item_id)
    if not item or item["cover_path"]:
        return
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await musicbrainz.cover_art(release_id, client)
        candidates = result.payload if result.found else []
        chosen = next((c for c in candidates if c.get("front")), None) or (candidates[0] if candidates else None)
        path = await covers._download_to_item(item_id, chosen["url"], client) if chosen else None
    if path:
        with get_db() as db:
            require_item_role(db, item_id, "editor")
            current = _music_item(db, item_id)
            if current and not current["cover_path"]:
                update_item_fields(db, item_id, {"cover_path": path})


@router.get("/music")
async def music_library(_=Depends(require_role("viewer"))):
    """Retain main's unified, ACL-scoped Browse library; adding is a separate action."""
    return RedirectResponse("/browse?media_family_filter=music", status_code=303)


@router.get("/music/add")
async def music_page(request: Request, q: str = Query(""), artist: str = Query(""), barcode: str = Query(""),
                     catalog_number: str = Query(""), item_id: int | None = Query(None),
                     _=Depends(require_role("viewer"))):
    q, artist = q.strip()[:200], artist.strip()[:200]
    barcode, catalog_number = upc_svc.normalize_barcode(barcode)[:32], catalog_number.strip()[:100]
    placeholders, music_types = _music_types_sql()
    with get_db() as db:
        target = _music_item(db, item_id)
        if item_id and not target:
            return HTMLResponse("Music item not found", status_code=404)
        if target:
            require_item_role(db, item_id, "editor")
        items = db.execute(
            f"SELECT i.*, mr.format_summary, mr.catalog_number, mr.release_date FROM items_live i "
            f"LEFT JOIN music_releases mr ON mr.item_id = i.id WHERE i.media_type IN ({placeholders}) "
            "ORDER BY i.authors COLLATE NOCASE, i.title COLLATE NOCASE, i.id LIMIT 250", music_types,
        ).fetchall()
        locations = db.execute("SELECT id, name FROM locations ORDER BY sort_order, name").fetchall()
    if target and not (q or artist or barcode or catalog_number):
        if target["upc"]:
            barcode = target["upc"]
        else:
            q, artist = target["title"] or "", target["authors"] or ""
    results, error = [], None
    if q or artist or barcode or catalog_number:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            result = await musicbrainz.search_releases(q, client, artist=artist or None, barcode=barcode or None,
                                                      catalog_number=catalog_number or None, limit=20)
        if result.found:
            results = result.payload or []
            for release in results:
                kind = _infer_media_type(release)
                release.update(shelf_media_type=kind, shelf_media_label=MEDIA_TYPES[kind] if kind else "Choose format")
        else:
            error = _provider_error(result)
    return request.app.state.templates.TemplateResponse(request, "music.html", {
        "items": items, "results": results, "error": error, "q": q, "artist": artist, "barcode": barcode,
        "catalog_number": catalog_number, "locations": locations, "target_item": target,
        "music_media_types": {key: MEDIA_TYPES[key] for key in MEDIA_TYPES if key in MUSIC_MEDIA_TYPES},
    })


@router.post("/api/music/add")
async def add_music_release(request: Request, release_id: str = Form(...), media_type: str = Form(""),
                            location_id: int | None = Form(None), owned: int = Form(1),
                            target_item_id: int | None = Form(None), _=Depends(require_role("editor"))):
    release_id = release_id.strip()
    if not release_id:
        return RedirectResponse("/music/add", status_code=303)
    with get_db() as db:
        target = _music_item(db, target_item_id)
        if target_item_id and not target:
            return HTMLResponse("Music item not found", status_code=404)
        if target:
            require_item_role(db, target["id"], "editor")
        # The sidecar read deliberately includes Trash, but permission is
        # checked before disclosure or a user-requested restore.
        existing = db.execute("SELECT item_id FROM music_releases WHERE musicbrainz_release_id = ?", (release_id,)).fetchone()
        if existing:
            require_item_role(db, existing["item_id"], "editor", include_deleted=True)
            if target and existing["item_id"] != target["id"]:
                return HTMLResponse("That exact release is already catalogued", status_code=409)
            restored = item_write.restore_item(db, existing["item_id"])
            if target:
                return RedirectResponse(f"/item/{existing['item_id']}?from=music", status_code=303)
            flag = "?restored=1" if restored else ""
            return RedirectResponse(f"/music/item/{existing['item_id']}{flag}", status_code=303)
        if target:
            current = music_catalog.get_release(db, target["id"])
            if current and current.get("musicbrainz_release_id"):
                return HTMLResponse("This item already has a different exact release identity", status_code=409)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await musicbrainz.lookup_release(release_id, client)
    if not result.found:
        return HTMLResponse(_provider_error(result) or "Release lookup failed", status_code=502)
    release = result.payload
    if media_type not in MUSIC_MEDIA_TYPES:
        media_type = _infer_media_type(release) or ""
    if not media_type:
        return HTMLResponse("Choose a music format before adding this release", status_code=400)
    provider_barcode = upc_svc.normalize_upc(release.get("barcode") or "") or None
    fields = {"title": release["title"], "authors": release.get("artist_credit"), "media_type": media_type,
              "publisher": release.get("label"), "publish_year": _year(release.get("release_date")) or _year(release.get("first_release_date"))}
    try:
        with get_db() as db:
            if target:
                require_item_role(db, target["id"], "editor")
                live_target = _music_item(db, target["id"])
                if not live_target:
                    return HTMLResponse("Music item not found", status_code=404)
                # Preserve scanned identity; only fill a blank provider barcode
                # through the canonical writer's hidden-collision protection.
                fields.update(upc=live_target["upc"] or provider_barcode, source="musicbrainz")
                update_item_fields(db, target["id"], fields)
                item_id = target["id"]
            else:
                loc_id = validated_location_id(db, location_id)
                item_id = insert_item(db, {**fields, "upc": provider_barcode, "location_id": loc_id,
                                          "owned": 1 if owned else 0, "wishlisted": not owned, "source": "musicbrainz"})
            # A user restore keeps an already stored exact release untouched.
            if not (item_write.was_restored(item_id) and music_catalog.get_release(db, item_id)):
                music_catalog.save_release(db, item_id, release)
    except (UnknownLocationError, item_write.ItemValueError, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=400)
    except sqlite3.IntegrityError:
        # Never resolve a collision by a guessed title or by revealing a hidden
        # edition. A concurrent claimant is a safe conflict, not reassignment.
        return HTMLResponse("This release or barcode is already catalogued", status_code=409)
    await _apply_release_artwork(item_id, release_id)
    if target:
        return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)
    flag = "?restored=1" if item_write.was_restored(item_id) else ""
    return RedirectResponse(f"/music/item/{item_id}{flag}", status_code=303)


@router.get("/music/item/{item_id}")
async def music_item_page(request: Request, item_id: int, restored: int = 0, _=Depends(require_role("viewer"))):
    with get_db() as db:
        item = _music_item(db, item_id)
        release = music_catalog.get_release(db, item_id) if item else None
    if not item:
        return RedirectResponse(f"/item/{item_id}", status_code=303)
    return request.app.state.templates.TemplateResponse(request, "music_item.html", {
        "item": item, "release": release, "media_types": MEDIA_TYPES, "restored": bool(restored),
    })


@router.get("/api/music/items/{item_id}/detail")
async def music_item_detail(request: Request, item_id: int, _=Depends(require_role("viewer"))):
    with get_db() as db:
        item = _music_item(db, item_id)
        release = music_catalog.get_release(db, item_id) if item else None
    return request.app.state.templates.TemplateResponse(request, "fragments/music_detail.html", {"item": item, "release": release}) if item else HTMLResponse("")


@router.get("/music/item/{item_id}/edit")
async def edit_music_copy(request: Request, item_id: int, _=Depends(require_role("editor"))):
    with get_db() as db:
        require_item_role(db, item_id, "editor")
        item = _music_item(db, item_id)
        release = music_catalog.get_release(db, item_id) if item else None
    if not item or not release:
        return RedirectResponse(f"/item/{item_id}", status_code=303)
    return request.app.state.templates.TemplateResponse(request, "music_edit.html", {"item": item, "release": release})


@router.post("/api/music/items/{item_id}/copy")
async def update_music_copy(item_id: int, edition_notes: str = Form(""), media_condition: str = Form(""),
                            packaging_condition: str = Form(""), condition_notes: str = Form(""), _=Depends(require_role("editor"))):
    with get_db() as db:
        require_item_role(db, item_id, "editor")
        music_catalog.update_copy_details(db, item_id, edition_notes=edition_notes.strip() or None,
            media_condition=media_condition.strip() or None, packaging_condition=packaging_condition.strip() or None,
            condition_notes=condition_notes.strip() or None)
    return RedirectResponse(f"/item/{item_id}?from=music", status_code=303)


@router.post("/api/music/items/{item_id}/identifiers")
async def add_music_identifier(item_id: int, identifier_type: str = Form(...), value: str = Form(...),
                               description: str = Form(""), _=Depends(require_role("editor"))):
    try:
        with get_db() as db:
            require_item_role(db, item_id, "editor")
            music_catalog.add_identifier(db, item_id, identifier_type, value, description or None)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=400)
    return RedirectResponse(f"/music/item/{item_id}/edit", status_code=303)


@router.post("/api/music/items/{item_id}/identifiers/{identifier_id}/delete")
async def delete_music_identifier(item_id: int, identifier_id: int, _=Depends(require_role("editor"))):
    with get_db() as db:
        require_item_role(db, item_id, "editor")
        music_catalog.remove_identifier(db, item_id, identifier_id)
    return RedirectResponse(f"/music/item/{item_id}/edit", status_code=303)


@router.post("/api/music/items/{item_id}/refresh")
async def refresh_music_release(item_id: int, _=Depends(require_role("editor"))):
    with get_db() as db:
        require_item_role(db, item_id, "editor")
        item = _music_item(db, item_id)
        release = music_catalog.get_release(db, item_id) if item else None
    if not release or not release.get("musicbrainz_release_id"):
        return HTMLResponse("MusicBrainz release ID is missing", status_code=400)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await musicbrainz.lookup_release(release["musicbrainz_release_id"], client)
    if not result.found:
        return HTMLResponse(_provider_error(result) or "Release lookup failed", status_code=502)
    refreshed = result.payload
    with get_db() as db:
        require_item_role(db, item_id, "editor")
        update_item_fields(db, item_id, {"title": refreshed["title"], "authors": refreshed.get("artist_credit"),
            "publisher": refreshed.get("label"), "publish_year": _year(refreshed.get("release_date")) or _year(refreshed.get("first_release_date"))})
        music_catalog.save_release(db, item_id, refreshed)
    await _apply_release_artwork(item_id, release["musicbrainz_release_id"])
    return RedirectResponse(f"/music/item/{item_id}", status_code=303)


refresh_music_metadata = refresh_music_release


@router.get("/api/music/items/{item_id}/discogs")
async def discogs_panel(request: Request, item_id: int, search: int = Query(0), details: int = Query(0),
                        q: str = Query(""), artist: str = Query(""), barcode: str = Query(""), catalog_number: str = Query(""),
                        _=Depends(require_role("viewer"))):
    with get_db() as db:
        item = _music_item(db, item_id)
        release = music_catalog.get_release(db, item_id) if item else None
        token = get_setting(db, "discogs_token")
        identity_error = None
        try:
            selected_id = music_catalog.selected_discogs_release_id(db, item_id) if item else None
        except ValueError as exc:
            selected_id, identity_error = None, str(exc)
        if search or details:
            require_item_role(db, item_id, "editor")
        try:
            require_item_role(db, item_id, "editor")
            can_edit = True
        except HTTPException as exc:
            if exc.status_code not in {403, 404}:
                raise
            can_edit = False
    if not item or not release:
        return HTMLResponse("Music release not found", status_code=404)
    if (search or details) and not can_edit:
        return HTMLResponse("Editor role required", status_code=403)
    fields = {"q": q.strip()[:200] or item["title"], "artist": artist.strip()[:200] or item["authors"] or "",
              "barcode": upc_svc.normalize_barcode(barcode or item["upc"] or "")[:32],
              "catalog_number": catalog_number.strip()[:100] or release.get("catalog_number") or ""}
    results, selected, error = [], None, identity_error
    if (search or details) and not token:
        error = "Discogs is not configured. Add a token in Settings → Integrations."
    elif search:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            result = await discogs.search_releases(fields["q"], client, token=token, artist=fields["artist"] or None,
                barcode=fields["barcode"] or None, catalog_number=fields["catalog_number"] or None, limit=20)
        results = result.payload or [] if result.found else []
        error = _discogs_error(result)
    elif details and selected_id:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            result = await discogs.lookup_release(selected_id, client, token=token)
        selected = result.payload if result.found else None
        error = _discogs_error(result)
    return request.app.state.templates.TemplateResponse(request, "fragments/music_discogs.html", {
        "item": item, "release": release, "selected_id": selected_id, "selected_release": selected,
        "results": results, "error": error, "fields": fields, "configured": bool(token), "can_edit": can_edit,
    })


@router.get("/music/item/{item_id}/discogs")
async def match_discogs_release(request: Request, item_id: int, q: str = Query(""), artist: str = Query(""),
                                barcode: str = Query(""), catalog_number: str = Query(""), _=Depends(require_role("editor"))):
    # Same normalisation/provider boundary as the native panel, with main's
    # full-page pressing picker context rather than a second selection writer.
    with get_db() as db:
        require_item_role(db, item_id, "editor")
        item = _music_item(db, item_id)
        release = music_catalog.get_release(db, item_id) if item else None
        token = get_setting(db, "discogs_token")
    if not item or not release:
        return RedirectResponse(f"/music/add?item_id={item_id}", status_code=303)
    q, artist = q.strip()[:200] or item["title"], artist.strip()[:200] or item["authors"] or ""
    barcode = upc_svc.normalize_barcode(barcode or item["upc"] or "")[:32]
    catalog_number = catalog_number.strip()[:100] or release.get("catalog_number") or ""
    results, error = [], None
    if token:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            result = await discogs.search_releases("" if barcode or catalog_number else q, client, token=token,
                artist=None if barcode else artist or None, barcode=barcode or None, catalog_number=None if barcode else catalog_number or None, limit=30)
        results = result.payload or [] if result.found else []
        error = _discogs_error(result)
    else:
        error = "Configure a Discogs API token in Settings before matching a pressing."
    return request.app.state.templates.TemplateResponse(request, "discogs_match.html", {
        "item": item, "release": release, "results": results, "error": error, "discogs_configured": bool(token),
        "q": q, "artist": artist, "barcode": barcode, "catalog_number": catalog_number,
    })


@router.post("/api/music/items/{item_id}/discogs")
async def attach_discogs_release(item_id: int, release_id: str = Form(...), _=Depends(require_role("editor"))):
    return await _store_discogs_release(item_id, release_id, enrich=True)


@router.post("/api/music/items/{item_id}/discogs/select")
async def select_discogs_release(item_id: int, release_id: str = Form(...), _=Depends(require_role("editor"))):
    """Validate a concrete pressing, then store its ID without caching API data."""
    return await _store_discogs_release(item_id, release_id, enrich=False)


async def _store_discogs_release(item_id: int, release_id: str, *, enrich: bool):
    with get_db() as db:
        require_item_role(db, item_id, "editor")
        item = _music_item(db, item_id)
        current = music_catalog.get_release(db, item_id) if item else None
        token = get_setting(db, "discogs_token")
    if not item or not current:
        return HTMLResponse("This item needs an exact MusicBrainz release first", status_code=400)
    if not token:
        return HTMLResponse("Discogs API token is not configured", status_code=400)
    release_id = release_id.strip()
    try:
        selected_id = discogs_selection._release_id(release_id)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=400)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        result = await discogs.lookup_release(release_id, client, token=token)
    if not result.found:
        return HTMLResponse(_discogs_error(result) or "Discogs lookup failed", status_code=502)
    try:
        returned_id = discogs_selection._release_id(
            result.payload.get("discogs_release_id") if isinstance(result.payload, dict) else None
        )
    except ValueError:
        return HTMLResponse("Discogs did not return the selected release", status_code=502)
    if returned_id != selected_id:
        return HTMLResponse("Discogs did not return the selected release", status_code=502)
    try:
        with get_db() as db, item_write._write_scope(db):
            require_item_role(db, item_id, "editor")
            if not _music_item(db, item_id) or not music_catalog.get_release(db, item_id):
                return HTMLResponse("Music release not found", status_code=404)
            if enrich:
                music_catalog.save_discogs_enrichment(db, item_id, result.payload)
            else:
                discogs_selection.set_selected_release_id(db, item_id, selected_id)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=409)
    return RedirectResponse(f"/music/item/{item_id}", status_code=303)


@router.post("/api/music/items/{item_id}/discogs/refresh")
async def refresh_discogs_release(item_id: int, _=Depends(require_role("editor"))):
    with get_db() as db:
        require_item_role(db, item_id, "editor")
        try:
            release_id = music_catalog.selected_discogs_release_id(db, item_id)
        except ValueError as exc:
            return HTMLResponse(str(exc), status_code=409)
    if not release_id:
        return HTMLResponse("This copy has no Discogs release match", status_code=400)
    return await attach_discogs_release(item_id, str(release_id))


@router.post("/api/music/items/{item_id}/discogs/clear")
async def clear_discogs_release(item_id: int, _=Depends(require_role("editor"))):
    with get_db() as db:
        require_item_role(db, item_id, "editor")
        music_catalog.clear_discogs_enrichment(db, item_id)
    return RedirectResponse(f"/music/item/{item_id}", status_code=303)


@router.post("/api/music/repair-artwork")
async def repair_music_artwork(_=Depends(require_role("editor"))):
    placeholders, kinds = _music_types_sql()
    with get_db() as db:
        rows = db.execute(f"SELECT id, upc, media_type FROM items_live WHERE media_type IN ({placeholders}) "
            "AND upc IS NOT NULL AND TRIM(upc) != '' AND COALESCE(TRIM(cover_path), '') = '' ORDER BY id LIMIT 15", kinds).fetchall()
    repaired = 0
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for row in rows:
            with get_db() as db:
                try:
                    require_item_role(db, row["id"], "editor")
                except HTTPException as exc:
                    if exc.status_code in {403, 404}:
                        continue
                    raise
            url = await music_artwork.find_cover_url(row["upc"], row["media_type"], client)
            path = await covers._download_to_item(row["id"], url, client) if url else None
            if path:
                with get_db() as db:
                    require_item_role(db, row["id"], "editor")
                    current = _music_item(db, row["id"])
                    if current and not current["cover_path"]:
                        update_item_fields(db, row["id"], {"cover_path": path})
                        repaired += 1
    return RedirectResponse(f"/browse?media_family_filter=music&music_artwork_checked={len(rows)}&music_artwork_repaired={repaired}", status_code=303)
