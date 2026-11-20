"""Music search, exact-release review and public shop-link previews."""

from contextlib import nullcontext
from dataclasses import asdict
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import i18n
from app import add_media_context as add_context
from app.add_media_types import AddReceipt, CapturedAddActor, MusicPreview
from app.auth import require_role
from app.config import COVERS_DIR, HTTP_TIMEOUT, MEDIA_TYPES, MUSIC_MEDIA_TYPES
from app.database import get_db, get_setting
from app.features import require_feature
from app.http_body import BodyTooLarge, MUSIC_PREVIEW_FORM_BYTES, MUSIC_REVIEW_FORM_BYTES, bounded_body
from app.services import cover_sync_policy, discogs, discogs_selection, item_write, manual_cover, music_artwork, music_catalog, music_matching, music_review, musicbrainz, shop_metadata
from app.services.write_targets import UnknownLocationError, validated_location_id
from app.services import tags as tags_svc

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


def _release_formats(release):
    formats = [str(m.get("format") or "") for m in release.get("media") or [] if isinstance(m, dict)]
    formats.append(str(release.get("format_summary") or ""))
    text = " ".join(formats).casefold()
    known = set()
    for pattern, kind in ((r'\bvinyl\b|\b(?:7|10|12)"', "vinyl"), (r"\bcassette\b", "cassette"),
                          (r"\bcd\b|\bcompact disc\b", "cd"), (r"\bdigital\b|\bfile\b", "digital_music")):
        if re.search(pattern, text):
            known.add(kind)
    return known


def _check_item_format(item, release):
    known = _release_formats(release)
    if item and known and item["media_type"] not in known:
        raise music_review.ReviewError(i18n.gettext("The release format contradicts this item's music format. Choose the correct pressing."))


def _discogs_link_id(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in {"discogs.com", "www.discogs.com"}
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
            return None
        match = re.fullmatch(r"/release/([0-9]+)(?:-[^/]*)?/?", parsed.path)
        return discogs._public_release_id(match[1]) if match else None
    except ValueError:
        return None


def _check_release(release, provider, identifier, requested_format=""):
    if not isinstance(release, dict):
        raise music_review.ReviewError(i18n.gettext("The provider returned unreadable release details."))
    returned = (music_matching.release_id(release.get("musicbrainz_release_id")) if provider == "musicbrainz"
                else discogs._public_release_id(release.get("discogs_release_id")))
    if returned != identifier:
        raise music_review.ReviewError(i18n.gettext("The provider returned a different release. Preview the exact release again."))
    known = _release_formats(release)
    if requested_format and known and requested_format not in known:
        raise music_review.ReviewError(i18n.gettext("The selected format contradicts the provider's release details. Choose the correct pressing."))


def _refuse_release_duplicate(db, candidate, item_id):
    """Identifiers survive Trash; do not join them to a live-only item first."""
    release = candidate.get("release") or {}
    if candidate["kind"] == "discogs":
        release_id = discogs._public_release_id(release.get("discogs_release_id"))
        if release_id is None:
            raise music_review.ReviewError(i18n.gettext("Choose a concrete Discogs release."))
        rows = db.execute(
            "SELECT item_id FROM music_identifiers WHERE identifier_type = ? AND value = ? AND item_id != ?",
            (discogs_selection.IDENTIFIER_TYPE, str(release_id), item_id),
        ).fetchall()
    elif candidate["kind"] == "musicbrainz" and release.get("musicbrainz_release_id"):
        rows = db.execute("SELECT item_id FROM music_releases WHERE musicbrainz_release_id = ? AND item_id != ?",
                          (release["musicbrainz_release_id"], item_id)).fetchall()
    else:
        return
    for row in rows:
        live = db.execute("SELECT id FROM items_live WHERE id = ?", (row["item_id"],)).fetchone()
        if not live:
            raise music_review.ReviewError(i18n.gettext("This release is in Trash. Restore it from Trash instead of adding another item. No changes were saved."))
        raise music_review.ReviewError(i18n.gettext("This release is already catalogued. Open the existing item instead. No changes were saved."))


def _render(request, *, item=None, candidates=(), error=None, results=(), fields=None, search_statuses=()):
    if fields is None and item:
        fields = {"q": item["title"], "artist": item.get("authors") or "", "barcode": item.get("upc") or "",
                  "format": item["media_type"]}
    return request.app.state.templates.TemplateResponse(request, "fragments/music_matching.html", {
        "item": item, "candidates": candidates, "match_error": error, "match_results": results,
        "match_fields": fields or {}, "music_media_types": {key: MEDIA_TYPES[key] for key in MEDIA_TYPES if key in MUSIC_MEDIA_TYPES},
        "field_labels": music_review.FIELD_LABELS,
        "show_match_search": bool(item),
        "match_search_statuses": search_statuses,
    }, headers={"Cache-Control": "no-store"})


@router.get("/match")
async def match_panel(request: Request, item_id: int = Query(0, ge=0, le=2**63 - 1), search: int = 0, q: str = "", artist: str = "",
                      barcode: str = "", catalog_number: str = "", format: str = "", country: str = ""):
    try:
        with get_db() as db:
            item = _item(db, item_id)
            release = music_catalog.get_release(db, item_id) if item else None
            token = get_setting(db, "discogs_token") or ""
        fields = {"q": q.strip()[:200] if search else (item or {}).get("title", ""),
                  "artist": artist.strip()[:200] if search else (item or {}).get("authors") or "",
                  "barcode": barcode.strip()[:32] if search else (item or {}).get("upc") or "",
                  "catalog_number": catalog_number.strip()[:100] if search else (release or {}).get("catalog_number") or "",
                  "format": format if search and format in MUSIC_MEDIA_TYPES else (item or {}).get("media_type", ""),
                  "country": country.strip()[:2].upper()}
        results, error, search_statuses = [], None, ()
        if search:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                answer = await music_matching.search_catalogue(client, title=fields["q"], artist=fields["artist"], barcode=fields["barcode"],
                    catalog_number=fields["catalog_number"], format=fields["format"], country=fields["country"], discogs_token=token)
            search_statuses = music_matching.search_statuses(answer)
            if answer.found:
                results = answer.payload
            else:
                provider = "Discogs" if answer.provider == "discogs" else "MusicBrainz"
                error = i18n.gettext("No matching release was found. Try fewer filters or paste a release/shop link.") if answer.outcome == "no_match" else (
                    i18n.gettext("Discogs could not complete this search. Try again shortly.") if provider == "Discogs"
                    else i18n.gettext("MusicBrainz could not complete this search. Try again shortly."))
        return _render(request, item=item, fields=fields, results=results, error=error, search_statuses=search_statuses)
    except music_review.ReviewError as error:
        return HTMLResponse(str(error), status_code=404)


class _ReviewOperationError(music_review.ReviewError):
    """Keep detached legacy item context across the request-independent seam."""

    def __init__(self, message, item):
        super().__init__(message)
        self.item = item


def _check_add_actor(db, actor):
    add_context.revalidate_actor(db, actor)
    # A local, same-connection flag read: no cached resolver/log/second DB
    # connection may run under the actual writer lock.
    row = db.execute("SELECT value FROM settings WHERE key = 'feature.music'").fetchone()
    if row and row["value"] == "0":
        raise add_context.AddMediaError("music_disabled", 403)


def _check_add_intent(intent, actor, user_id, item_id, provider=None, requested_format=None):
    if not isinstance(actor, CapturedAddActor) or actor.id != user_id:
        raise add_context.ActorChanged()
    add_context.intent_dict(intent)
    if (item_id or intent.media_type not in MUSIC_MEDIA_TYPES
            or (provider is not None and (provider not in {"musicbrainz", "discogs"}
                or intent.music_provider not in {"auto", provider}))
            or (requested_format is not None and requested_format != intent.media_type)):
        raise add_context.AddMediaError("intent_changed")


def _check_add_format(release, intent):
    if not isinstance(release, dict):
        raise add_context.ProviderUnavailable("unresolved")
    known = _release_formats(release)
    if known and intent.media_type not in known:
        raise add_context.AddMediaError("format_conflict")


def _add_fields(release):
    try:
        fields = music_review.fields_for(release)
    except (TypeError, ValueError, AttributeError):
        raise add_context.ProviderUnavailable("unresolved") from None
    if not isinstance(fields.get("title"), str) or not fields["title"].strip():
        raise add_context.ProviderUnavailable("unresolved")
    for key, value in fields.items():
        if (key == "publish_year" and type(value) is not int) or (key != "publish_year" and not isinstance(value, str)):
            raise add_context.ProviderUnavailable("unresolved")
    return fields


async def _preview_release_operation(*, user_id, item_id=0, release_id="", url="", provider=None,
                                     requested_format="", intent=None, actor=None) -> MusicPreview:
    """Native release lookup/review; the Add arm is textual and purpose-closed."""
    item = None
    try:
        if intent is not None:
            _check_add_intent(intent, actor, user_id, item_id, provider, requested_format)
            if url or provider is None:
                raise add_context.AddMediaError("intent_changed")
        with get_db() as db:
            if intent is not None:
                _check_add_actor(db, actor)
            item = _item(db, item_id)
            fingerprint = music_review.fingerprint(db, item_id)
            token = get_setting(db, "discogs_token")
            current_release = music_catalog.get_release(db, item_id) if item else None
        if len(url) > 2048 or len(release_id) > 2048:
            raise music_review.ReviewError("That link is too long.")
        mbid = music_matching.release_id(release_id or url)
        parsed = urlsplit(url)
        discogs_id = _discogs_link_id(url)
        if provider == "discogs":
            discogs_id = discogs._public_release_id(release_id)
            if discogs_id is None or (url and _discogs_link_id(url) != discogs_id):
                raise music_review.ReviewError(i18n.gettext("Choose a concrete Discogs release ID, not a master or marketplace listing."))
        elif provider == "musicbrainz" and (not mbid or not release_id):
            raise music_review.ReviewError(i18n.gettext("Choose an exact MusicBrainz release ID."))
        if provider != "discogs" and mbid and url and music_matching.release_id(url) != mbid:
            raise music_review.ReviewError(i18n.gettext("The release ID and link do not match."))
        if provider != "discogs" and mbid:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                answer = await musicbrainz.lookup_release(mbid, client)
                if intent is not None:
                    with get_db() as db:
                        _check_add_actor(db, actor)
                if not answer.found:
                    if intent is not None:
                        raise add_context.ProviderUnavailable(answer.outcome)
                    raise music_review.ReviewError("The MusicBrainz release could not be read. Try again shortly.")
                release = answer.payload
                if intent is not None:
                    _check_add_format(release, intent)
                _check_release(release, "musicbrainz", mbid, requested_format)
                _check_item_format(item, release)
                if intent is None:
                    art = await music_artwork.release_candidates(release, client)
            candidates = [{"kind": "musicbrainz", "source_url": f"https://musicbrainz.org/release/{mbid}",
                            "release": release, "fields": music_review.fields_for(release) if intent is None else _add_fields(release),
                            **({"images": [image["url"] for image in art.payload[:12]] if art.found else [],
                                "image_labels": [image["label"] for image in art.payload[:12]] if art.found else []}
                               if intent is None else {})}]
        elif discogs_id:
            if provider is None and release_id:
                raise music_review.ReviewError(i18n.gettext("The release ID and link do not match."))
            if provider is None and not item:
                raise music_review.ReviewError("Add a catalogue item first to attach its optional Discogs pressing, or use a MusicBrainz/shop link.")
            if provider is None and not token:
                raise music_review.ReviewError("Add a Discogs token in Settings → Integrations before using a Discogs release link.")
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                lookup = discogs.lookup_public_release if provider == "discogs" else discogs.lookup_release
                answer = await lookup(discogs_id, client, token=token or "")
            if intent is not None:
                with get_db() as db:
                    _check_add_actor(db, actor)
            if not answer.found:
                if intent is not None:
                    raise add_context.ProviderUnavailable(answer.outcome)
                raise music_review.ReviewError(i18n.gettext("The Discogs release could not be read. Try again shortly."))
            if intent is not None:
                _check_add_format(answer.payload, intent)
            _check_release(answer.payload, "discogs", discogs_id, requested_format)
            _check_item_format(item, answer.payload)
            candidates = [{"kind": "discogs", "source_url": f"https://www.discogs.com/release/{discogs_id}", "release": answer.payload,
                             "fields": (music_review.fields_for(answer.payload) if intent is None else _add_fields(answer.payload)) if provider == "discogs" else {},
                             **({"images": [image["url"] for image in answer.payload.get("images", [])],
                                 "image_labels": [image["label"] for image in answer.payload.get("images", [])]}
                                if intent is None else {})}]
        elif release_id or parsed.hostname in {"musicbrainz.org", "www.musicbrainz.org", "discogs.com", "www.discogs.com"}:
            raise music_review.ReviewError("Paste an exact release link, not an artist, release group, master or marketplace listing.")
        else:
            candidates = await shop_metadata.preview(url)
        for candidate in candidates:
            _check_item_format(item, candidate["release"])
            known = _release_formats(candidate["release"])
            if requested_format and known and requested_format not in known:
                raise music_review.ReviewError(i18n.gettext("The selected format contradicts the provider's release details. Choose the correct pressing."))
            candidate["media_type"] = next(iter(known)) if len(known) == 1 else None
            candidate["keep_release"] = bool(current_release and current_release.get("musicbrainz_release_id") and candidate["kind"] == "shop")
            if intent is not None:
                with get_db() as db:
                    _check_add_actor(db, actor)
                candidate["release"] = {key: value for key, value in candidate["release"].items() if key != "images"}
                candidate["add_media_intent"] = add_context.intent_dict(intent)
                candidate["add_media_actor"] = asdict(actor)
                try:
                    candidate["ticket"] = music_review.sign(candidate, user_id=user_id, item_id=0,
                                                           fingerprint="new", purpose=add_context.MUSIC_PURPOSE)
                except music_review.ReviewError:
                    raise add_context.AddMediaError("invalid_input", 422) from None
            else:
                candidate["ticket"] = music_review.sign(candidate, user_id=user_id, item_id=item_id, fingerprint=fingerprint)
                candidate["image_tickets"] = [music_review.sign({"kind": candidate["kind"], "images": [image]},
                                                               user_id=user_id, item_id=item_id, fingerprint=fingerprint,
                                                               purpose="shelf-music-image-v1") for image in candidate.get("images", [])]
        return MusicPreview(item, tuple(candidates))
    except add_context.AddMediaError:
        raise
    except (music_review.ReviewError, shop_metadata.ShopError, ValueError) as error:
        raise _ReviewOperationError(str(error), item) from error


@router.post("/preview")
async def preview(request: Request):
    item = None
    try:
        form = await _form(request, MUSIC_PREVIEW_FORM_BYTES)
        if set(form) - {"item_id", "release_id", "url", "provider", "format", "_csrf"} or any(len(form.getlist(key)) != 1 for key in form):
            raise music_review.ReviewError("Submit only the release/link preview form.")
        provider = form.get("provider")
        requested_format = str(form.get("format", ""))
        if provider is not None and provider not in {"musicbrainz", "discogs"}:
            raise music_review.ReviewError(i18n.gettext("Choose MusicBrainz or Discogs as the release provider."))
        if requested_format and requested_format not in MUSIC_MEDIA_TYPES:
            raise music_review.ReviewError(i18n.gettext("Choose a valid music format."))
        item_id = int(form.get("item_id", "0"))
        if not 0 <= item_id <= 2**63 - 1:
            raise music_review.ReviewError("Music item not found.")
        release_id, url = str(form.get("release_id", "")).strip(), str(form.get("url", "")).strip()
        result = await _preview_release_operation(user_id=request.state.user["id"], item_id=item_id,
            release_id=release_id, url=url, provider=provider, requested_format=requested_format)
        return _render(request, item=result.item, candidates=result.candidates)
    except (music_review.ReviewError, shop_metadata.ShopError, ValueError) as error:
        return _render(request, item=getattr(error, "item", item), error=str(error))


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


def _add_candidate(ticket, actor, intent):
    candidate = add_context.verify_host_ticket(ticket, actor=actor, purpose=add_context.MUSIC_PURPOSE)
    if set(candidate) != {"kind", "source_url", "release", "fields", "media_type", "keep_release",
                          "add_media_intent", "add_media_actor"}:
        raise add_context.AddMediaError()
    add_context.bound_actor(candidate["add_media_actor"], actor)
    try:
        signed_intent = add_context.intent_from_dict(candidate["add_media_intent"])
    except add_context.AddMediaError:
        raise add_context.AddMediaError() from None
    if signed_intent != intent:
        raise add_context.AddMediaError("intent_changed")
    provider = candidate["kind"]
    if not isinstance(provider, str):
        raise add_context.AddMediaError()
    _check_add_intent(intent, actor, actor.id, 0, provider, intent.media_type)
    release = candidate["release"]
    if not isinstance(release, dict) or "images" in release or candidate["keep_release"] is not False:
        raise add_context.AddMediaError()
    identifier = (music_matching.release_id(release.get("musicbrainz_release_id")) if provider == "musicbrainz"
                  else discogs._public_release_id(release.get("discogs_release_id")))
    source_url = (f"https://musicbrainz.org/release/{identifier}" if provider == "musicbrainz"
                  else f"https://www.discogs.com/release/{identifier}")
    if not identifier or source_url != candidate["source_url"] or candidate["fields"] != music_review.fields_for(release):
        raise add_context.AddMediaError()
    _check_release(release, provider, identifier)
    _check_add_format(release, intent)
    known = _release_formats(release)
    if candidate["media_type"] != (next(iter(known)) if len(known) == 1 else None):
        raise add_context.AddMediaError()
    return candidate


async def _apply_review_operation(*, user_id, ticket, context_item_id=None, fields: tuple = (), image=-1,
                                  media_type="", location_id=None, owned: bool | None = None, intent=None,
                                  tags="", reading_status="", actor=None) -> AddReceipt:
    """Native selective apply; only the new arm accepts an Add-host ticket."""
    path = None
    item = None
    transaction_finished = False
    try:
        if intent is not None:
            _check_add_intent(intent, actor, user_id, context_item_id or 0)
            if owned is not None or image != -1 or media_type != intent.media_type or reading_status:
                raise add_context.AddMediaError("intent_changed")
        with get_db() as db:
            if intent is not None:
                _check_add_actor(db, actor)
            item = _item(db, context_item_id or 0)
        if intent is not None:
            candidate = _add_candidate(ticket, actor, intent)
            review = {"item": 0, "candidate": candidate, "fingerprint": "new"}
        else:
            review = music_review.verify(ticket, user_id=user_id)
        item_id, candidate = review["item"], review["candidate"]
        if context_item_id is not None and context_item_id != item_id:
            raise music_review.ReviewError("That preview belongs to a different item.")
        chosen = fields
        if any(field not in music_review.FIELD_LABELS or field not in candidate["fields"] for field in chosen):
            if intent is not None:
                raise add_context.AddMediaError("invalid_input", 422)
            raise music_review.ReviewError("Choose only the fields shown in the preview.")
        if intent is not None and (len(chosen) > 6 or len(set(chosen)) != len(chosen)):
            raise add_context.AddMediaError("invalid_input", 422)
        with get_db() as db:
            if intent is not None:
                _check_add_actor(db, actor)
            item = _item(db, item_id)
            if music_review.fingerprint(db, item_id) != review["fingerprint"]:
                raise music_review.ReviewError("This item changed after the preview. Preview it again before applying anything.")
            _check_item_format(item, candidate["release"])
            _refuse_release_duplicate(db, candidate, item_id)
        values = {field: candidate["fields"][field] for field in chosen}
        if not item_id:
            kind = str(media_type)
            known = _release_formats(candidate["release"])
            if kind not in MUSIC_MEDIA_TYPES or not values.get("title"):
                if intent is not None:
                    raise add_context.AddMediaError("invalid_input", 422)
                raise music_review.ReviewError(i18n.gettext("Choose a music format and a title before adding this item."))
            if known and kind not in known:
                raise music_review.ReviewError(i18n.gettext("The selected format contradicts the provider's release details. Choose the correct pressing."))
        index = int(image)
        if index != -1:
            images = candidate.get("images", [])
            if not 0 <= index < len(images):
                raise music_review.ReviewError("Choose an image from the preview.")
            path = await manual_cover.download(item_id, images[index], unique=True, caa=candidate["kind"] == "musicbrainz")
            if not path:
                raise music_review.ReviewError("The chosen image could not be downloaded. Nothing was changed; try keeping the existing artwork.")
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            if intent is not None:
                _check_add_actor(db, actor)
            item = _item(db, item_id)
            if music_review.fingerprint(db, item_id) != review["fingerprint"]:
                raise music_review.ReviewError("This item changed while the artwork was loading. Preview it again.")
            _check_item_format(item, candidate["release"])
            _refuse_release_duplicate(db, candidate, item_id)
            creating = not item_id
            if not item_id:
                location = validated_location_id(db, int(location_id) if location_id else None)
                ownership = intent.mode == "add" if intent is not None else (True if owned is None else owned)
                # A conflicting barcode belongs to an existing item: refuse
                # rather than silently replacing that item's local metadata.
                with (tags_svc.default_tags(tags) if intent is not None else nullcontext()), \
                        (item_write.default_reading_status(reading_status) if intent is not None else nullcontext()):
                    item_id = item_write.insert_item(db, {**values, "media_type": kind, "location_id": location,
                                                            "owned": int(ownership), "wishlisted": not ownership, "source": candidate["kind"]},
                                                      restore_trashed=False)
            elif values:
                item_write.update_item_fields(db, item_id, values)
            if candidate["kind"] == "musicbrainz":
                music_catalog.save_release(db, item_id, candidate["release"])
            elif candidate["kind"] == "discogs":
                if not music_catalog.get_release(db, item_id):
                    music_catalog.save_release(db, item_id, {"source": "discogs" if creating else "manual", "media": []})
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
            if intent is not None:
                row = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
                receipt = AddReceipt("added" if row["owned"] else "wishlisted", "add" if row["owned"] else "wishlist",
                                     row["media_type"], row["id"], row["source"], row["title"], row["authors"], row["cover_path"])
            transaction_finished = True
        path = None  # The new cover now belongs to the committed item.
        if intent is None:
            from app.services import music_auto_covers
            music_auto_covers.enqueue_missing(item_id)
        return receipt if intent is not None else AddReceipt("added" if creating else "updated", item_id=item_id)
    except add_context.AddMediaError:
        raise
    except item_write.ItemValueError as error:
        if intent is not None:
            if transaction_finished:
                code, status = "outcome_unknown", 503
            elif isinstance(error, item_write.IdentifierInTrash):
                code, status = "stale_review", 409
            else:
                code, status = "invalid_input", 422
            raise add_context.AddMediaError(code, status) from error
        raise _ReviewOperationError(
            "That barcode/release is already catalogued or a selected location is unavailable. No changes were saved.", item) from error
    except (music_review.ReviewError, UnknownLocationError, ValueError, sqlite3.IntegrityError, item_write.IdentifierInTrash) as error:
        if intent is not None and transaction_finished:
            raise add_context.AddMediaError("outcome_unknown", 503) from error
        message = str(error) if isinstance(error, music_review.ReviewError) else "That barcode/release is already catalogued or a selected location is unavailable. No changes were saved."
        raise _ReviewOperationError(message, item) from error
    finally:
        if path:
            (COVERS_DIR / Path(path).name).unlink(missing_ok=True)


@router.post("/apply-match")
async def apply_match(request: Request):
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
        result = await _apply_review_operation(user_id=request.state.user["id"], ticket=form["ticket"],
            context_item_id=context_id if "item_id" in form else None, fields=tuple(form.getlist("fields")),
            image=form.get("image", "-1"), media_type=str(form.get("media_type", "")),
            location_id=form.get("location_id"), owned=form.get("owned", "1") == "1")
        if request.headers.get("HX-Request", "").lower() == "true":
            return Response(headers={"HX-Redirect": f"/music/item/{result.item_id}"})
        return RedirectResponse(f"/music/item/{result.item_id}", status_code=303)
    except (music_review.ReviewError, UnknownLocationError, ValueError, sqlite3.IntegrityError, item_write.IdentifierInTrash) as error:
        message = str(error) if isinstance(error, music_review.ReviewError) else "That barcode/release is already catalogued or a selected location is unavailable. No changes were saved."
        return _render(request, item=getattr(error, "item", None), error=message)
