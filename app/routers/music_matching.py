"""Music search, exact-release review and public shop-link previews."""

import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import require_role
from app.config import COVERS_DIR, HTTP_TIMEOUT, MEDIA_TYPES, MUSIC_MEDIA_TYPES
from app.database import get_db, get_setting
from app.features import require_feature
from app.http_body import BodyTooLarge, MUSIC_PREVIEW_FORM_BYTES, MUSIC_REVIEW_FORM_BYTES, bounded_body
from app.services import cover_sync_policy, discogs, discogs_selection, item_write, manual_cover, music_artwork, music_catalog, music_matching, music_review, musicbrainz, shop_metadata
from app.services.write_targets import UnknownLocationError, validated_location_id

router = APIRouter(prefix="/api/music", dependencies=[Depends(require_role("editor")), Depends(require_feature("music"))])


async def _form(request, limit):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        raise music_review.ReviewError("Submit the music review form without attachments.")
    try:
        await bounded_body(request, limit)
    except BodyTooLarge:
        raise music_review.ReviewError("The review form is too large. Preview the match again.") from None
    return await request.form()


def _item(db, item_id):
    item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone() if item_id else None
    if item_id and (not item or item["media_type"] not in MUSIC_MEDIA_TYPES):
        raise music_review.ReviewError("Music item not found.")
    return dict(item) if item else None


def _render(request, *, item=None, candidates=(), error=None, results=(), fields=None):
    if fields is None and item:
        fields = {"q": item["title"], "artist": item.get("authors") or "", "barcode": item.get("upc") or "",
                  "format": item["media_type"]}
    return request.app.state.templates.TemplateResponse(request, "fragments/music_matching.html", {
        "item": item, "candidates": candidates, "match_error": error, "match_results": results,
        "match_fields": fields or {}, "music_media_types": {key: MEDIA_TYPES[key] for key in MEDIA_TYPES if key in MUSIC_MEDIA_TYPES},
        "field_labels": music_review.FIELD_LABELS,
        "show_match_search": bool(item),
    }, headers={"Cache-Control": "no-store"})


@router.get("/match")
async def match_panel(request: Request, item_id: int = Query(0, ge=0, le=2**63 - 1), search: int = 0, q: str = "", artist: str = "",
                      barcode: str = "", catalog_number: str = "", format: str = "", country: str = ""):
    try:
        with get_db() as db:
            item = _item(db, item_id)
            release = music_catalog.get_release(db, item_id) if item else None
        fields = {"q": q.strip()[:200] if search else (item or {}).get("title", ""),
                  "artist": artist.strip()[:200] if search else (item or {}).get("authors") or "",
                  "barcode": barcode.strip()[:32] if search else (item or {}).get("upc") or "",
                  "catalog_number": catalog_number.strip()[:100] if search else (release or {}).get("catalog_number") or "",
                  "format": format if search else (item or {}).get("media_type", "vinyl"), "country": country.strip()[:2].upper()}
        results, error = [], None
        if search:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                answer = await music_matching.search(client, title=fields["q"], artist=fields["artist"], barcode=fields["barcode"],
                                                     catalog_number=fields["catalog_number"], format=fields["format"], country=fields["country"])
            if answer.found:
                results = answer.payload
            else:
                error = "No matching release was found. Try fewer filters or paste a release/shop link." if answer.outcome == "no_match" else "The music provider could not complete this search. Try again shortly."
        return _render(request, item=item, fields=fields, results=results, error=error)
    except music_review.ReviewError as error:
        return HTMLResponse(str(error), status_code=404)


@router.post("/preview")
async def preview(request: Request):
    item = None
    try:
        form = await _form(request, MUSIC_PREVIEW_FORM_BYTES)
        if set(form) - {"item_id", "release_id", "url", "_csrf"} or any(len(form.getlist(key)) != 1 for key in form):
            raise music_review.ReviewError("Submit only the release/link preview form.")
        item_id = int(form.get("item_id", "0"))
        if not 0 <= item_id <= 2**63 - 1:
            raise music_review.ReviewError("Music item not found.")
        release_id, url = str(form.get("release_id", "")), str(form.get("url", ""))
        with get_db() as db:
            item = _item(db, item_id)
            fingerprint = music_review.fingerprint(db, item_id)
            token = get_setting(db, "discogs_token")
            current_release = music_catalog.get_release(db, item_id) if item else None
        if len(url) > 2048 or len(release_id) > 2048:
            raise music_review.ReviewError("That link is too long.")
        mbid = music_matching.release_id(release_id or url)
        parsed = urlsplit(url)
        discogs_match = re.fullmatch(r"/release/(\d+)(?:-[^/]*)?/?", parsed.path) if parsed.hostname in {"discogs.com", "www.discogs.com"} else None
        if mbid:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                answer = await musicbrainz.lookup_release(mbid, client)
                if not answer.found:
                    raise music_review.ReviewError("The MusicBrainz release could not be read. Try again shortly.")
                release = answer.payload
                art = await music_artwork.release_candidates(release, client)
            candidates = [{"kind": "musicbrainz", "source_url": f"https://musicbrainz.org/release/{mbid}",
                           "release": release, "fields": music_review.fields_for(release),
                           "images": [image["url"] for image in art.payload[:12]] if art.found else [],
                           "image_labels": [image["label"] for image in art.payload[:12]] if art.found else []}]
        elif discogs_match and shop_metadata.public_url(url, page=True):
            if not item:
                raise music_review.ReviewError("Add a catalogue item first to attach its optional Discogs pressing, or use a MusicBrainz/shop link.")
            if not token:
                raise music_review.ReviewError("Add a Discogs token in Settings → Integrations before using a Discogs release link.")
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                answer = await discogs.lookup_release(discogs_match[1], client, token=token)
            if not answer.found or not answer.payload.get("discogs_release_id"):
                raise music_review.ReviewError("The Discogs release could not be read.")
            candidates = [{"kind": "discogs", "source_url": answer.payload["discogs_url"], "release": answer.payload,
                           "fields": {}, "images": [image["url"] for image in answer.payload.get("images", [])],
                           "image_labels": [image["label"] for image in answer.payload.get("images", [])]}]
        elif release_id or parsed.hostname in {"musicbrainz.org", "www.musicbrainz.org", "discogs.com", "www.discogs.com"}:
            raise music_review.ReviewError("Paste an exact release link, not an artist, release group, master or marketplace listing.")
        else:
            candidates = await shop_metadata.preview(url)
        for candidate in candidates:
            candidate["keep_release"] = bool(current_release and current_release.get("musicbrainz_release_id") and candidate["kind"] == "shop")
            candidate["ticket"] = music_review.sign(candidate, user_id=request.state.user["id"], item_id=item_id, fingerprint=fingerprint)
            candidate["image_tickets"] = [music_review.sign({"kind": candidate["kind"], "images": [image]},
                                                           user_id=request.state.user["id"], item_id=item_id, fingerprint=fingerprint,
                                                           purpose="shelf-music-image-v1") for image in candidate.get("images", [])]
        return _render(request, item=item, candidates=candidates)
    except (music_review.ReviewError, shop_metadata.ShopError, ValueError) as error:
        return _render(request, item=item, error=str(error))


@router.get("/preview-image")
async def preview_image(request: Request, ticket: str = Query(...), index: int = Query(...)):
    """Never make the browser load an arbitrary shop-supplied image URL."""
    try:
        review = music_review.verify(ticket, user_id=request.state.user["id"], purpose="shelf-music-image-v1")
        images = review["candidate"].get("images", [])
        if not 0 <= index < len(images):
            raise music_review.ReviewError("Image not found.")
        # Use a disposable image file, then remove it. No catalogue state is
        # written by previewing. The chosen image is downloaded again on Apply.
        path = await manual_cover.download(0, images[index], unique=True, caa=review["candidate"]["kind"] == "musicbrainz")
        if not path:
            return Response(status_code=204)
        target = COVERS_DIR / Path(path).name
        try:
            content = target.read_bytes()
        finally:
            target.unlink(missing_ok=True)
        mime = "image/png" if content.startswith(b"\x89PNG") else "image/webp" if content.startswith(b"RIFF") else "image/gif" if content.startswith(b"GIF") else "image/jpeg"
        return Response(content, media_type=mime, headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})
    except music_review.ReviewError:
        return Response(status_code=404)


@router.post("/apply-match")
async def apply_match(request: Request):
    path = None
    try:
        form = await _form(request, MUSIC_REVIEW_FORM_BYTES)
        if set(form) - {"ticket", "item_id", "fields", "image", "media_type", "location_id", "owned", "_csrf"} or len(form.getlist("ticket")) != 1:
            raise music_review.ReviewError("Submit only the match review form.")
        for name in ("item_id", "image", "media_type", "location_id"):
            if len(form.getlist(name)) > 1:
                raise music_review.ReviewError("Submit each review control once.")
        if (any(value not in {"0", "1"} for value in form.getlist("owned")) or len(form.getlist("owned")) > 2
                or len(form.getlist("fields")) > len(music_review.FIELD_LABELS)):
            raise music_review.ReviewError("Submit only the choices shown in the review.")
        context_id = int(form.get("item_id", "0"))
        if not 0 <= context_id <= 2**63 - 1:
            raise music_review.ReviewError("Music item not found.")
        with get_db() as db:
            item = _item(db, context_id)
        review = music_review.verify(form["ticket"], user_id=request.state.user["id"])
        item_id, candidate = review["item"], review["candidate"]
        if "item_id" in form and context_id != item_id:
            raise music_review.ReviewError("That preview belongs to a different item.")
        chosen = form.getlist("fields")
        if any(field not in music_review.FIELD_LABELS or field not in candidate["fields"] for field in chosen):
            raise music_review.ReviewError("Choose only the fields shown in the preview.")
        with get_db() as db:
            item = _item(db, item_id)
            if music_review.fingerprint(db, item_id) != review["fingerprint"]:
                raise music_review.ReviewError("This item changed after the preview. Preview it again before applying anything.")
        index = int(form.get("image", "-1"))
        if index != -1:
            images = candidate.get("images", [])
            if not 0 <= index < len(images):
                raise music_review.ReviewError("Choose an image from the preview.")
            path = await manual_cover.download(item_id, images[index], unique=True, caa=candidate["kind"] == "musicbrainz")
            if not path:
                raise music_review.ReviewError("The chosen image could not be downloaded. Nothing was changed; try keeping the existing artwork.")
        values = {field: candidate["fields"][field] for field in chosen}
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            if music_review.fingerprint(db, item_id) != review["fingerprint"]:
                raise music_review.ReviewError("This item changed while the artwork was loading. Preview it again.")
            if not item_id:
                kind = form.get("media_type", "vinyl")
                if kind not in MUSIC_MEDIA_TYPES or not values.get("title"):
                    raise music_review.ReviewError("Choose a music format and a title before adding this item.")
                location = validated_location_id(db, int(form["location_id"]) if form.get("location_id") else None)
                owned = form.get("owned", "1") == "1"
                # A conflicting barcode belongs to an existing item: refuse
                # rather than silently replacing that item's local metadata.
                item_id = item_write.insert_item(db, {**values, "media_type": kind, "location_id": location,
                                                       "owned": int(owned), "wishlisted": not owned, "source": candidate["kind"]},
                                                 restore_trashed=False)
            elif values:
                item_write.update_item_fields(db, item_id, values)
            if candidate["kind"] == "musicbrainz":
                music_catalog.save_release(db, item_id, candidate["release"])
            elif candidate["kind"] == "discogs":
                if not music_catalog.get_release(db, item_id):
                    music_catalog.save_release(db, item_id, {"source": "manual", "media": []})
                discogs_selection.set_selected_release_id(db, item_id, candidate["release"]["discogs_release_id"])
            else:
                current = music_catalog.get_release(db, item_id)
                if not current or current.get("metadata_source") == "shop":
                    # An OpenGraph-only page must not erase tracks/pressing
                    # details collected from a richer page on an earlier visit.
                    release = {**(current or {}), **{key: value for key, value in candidate["release"].items()
                                                    if value not in (None, "", [])}, "source": "shop"}
                    music_catalog.save_release(db, item_id, release)
                music_catalog.add_identifier(db, item_id, "shop_url", candidate["source_url"], "Manually reviewed shop metadata")
            if path:
                item_write.update_item_fields(db, item_id, {"cover_path": path, "cover_edition_isbn": None, "cover_edition_language": None})
                cover_sync_policy.block(db, item_id)
        path = None  # The new cover now belongs to the committed item.
        if request.headers.get("HX-Request", "").lower() == "true":
            return Response(headers={"HX-Redirect": f"/music/item/{item_id}"})
        return RedirectResponse(f"/music/item/{item_id}", status_code=303)
    except (music_review.ReviewError, UnknownLocationError, ValueError, sqlite3.IntegrityError, item_write.IdentifierInTrash) as error:
        message = str(error) if isinstance(error, music_review.ReviewError) else "That barcode/release is already catalogued or a selected location is unavailable. No changes were saved."
        return _render(request, item=locals().get("item"), error=message)
    finally:
        if path:
            (COVERS_DIR / Path(path).name).unlink(missing_ok=True)
