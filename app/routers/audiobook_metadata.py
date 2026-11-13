"""User-triggered, exact-ASIN metadata/artwork preview and selective backfill."""

import sqlite3
from pathlib import Path as FilePath

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException as FormHTTPException

from app import config, i18n
from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.http_body import BodyTooLarge, bounded_body
from app.routers import items_common
from app.services import audible_catalog, audiobook_review as review, covers, item_write


router = APIRouter(dependencies=[Depends(require_role("editor")), Depends(require_feature("connected_library"))])
form_limit = review.form_limit  # main.CSRFMiddleware must call this before parsing native forms.


async def _form(request, limit):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise review.ReviewError(i18n.gettext("Submit the audiobook review form without attachments."))
    try:
        await bounded_body(request, limit)
        return await request.form()
    except BodyTooLarge:
        raise review.ReviewError(i18n.gettext("The audiobook review form is too large. Preview it again.")) from None
    except FormHTTPException:
        raise review.ReviewError(i18n.gettext("Submit a valid audiobook review form.")) from None


def _render(request, item_id, *, candidate=None, error=None, marketplace="", status_code=200):
    try:
        with get_db() as db:
            item = review.item(db, item_id)
            missing = review.backfill_fields(db, item_id, candidate) if candidate else set()
    except review.ReviewError as unavailable:
        item, missing, candidate = None, set(), None
        error = str(unavailable)
        status_code = 404
    headers = {"Cache-Control": "no-store"}
    if error:
        headers["HX-Trigger"] = items_common._toast_header(error, "error")
    template = "fragments/audiobook_metadata.html" if request.headers.get("HX-Request", "").lower() == "true" else "audiobook_metadata.html"
    return request.app.state.templates.TemplateResponse(request, template, {
        "item": item, "audiobook_error": error, "candidate": candidate, "marketplace": marketplace,
        "marketplaces": audible_catalog.MARKETPLACES, "field_labels": review.FIELD_LABELS, "backfill_fields": missing,
    }, status_code=status_code, headers=headers)


@router.get("/api/audiobooks/{item_id}/metadata")
async def panel(request: Request, item_id: int = Path(..., gt=0, le=2**63 - 1)):
    # Rendering never spends an external request; even lookup requires a POST.
    return _render(request, item_id)


@router.post("/api/audiobooks/{item_id}/metadata/preview")
async def preview(request: Request, item_id: int = Path(..., gt=0, le=2**63 - 1)):
    market = ""
    try:
        form = await _form(request, review.PREVIEW_FORM_BYTES)
        if set(form) - {"marketplace", "_csrf"} or len(form.getlist("marketplace")) != 1 or len(form.getlist("_csrf")) > 1:
            raise review.ReviewError(i18n.gettext("Choose a marketplace; edit the item's ASIN on its edit page."))
        market = form["marketplace"]
        with get_db() as db:
            item = review.item(db, item_id)
            asin, market = audible_catalog.identity(item["asin"], market)
            fingerprint = review.fingerprint(db, item_id)
        answer = await audible_catalog.lookup(asin, market)
        if not answer.found:
            messages = {
                "no_match": i18n.N_("No exact audiobook was found in that marketplace. Existing metadata and artwork were kept."),
                "rate_limited": i18n.N_("Audible rate-limited this request. Try again later; nothing was changed."),
                "rejected": i18n.N_("The public Audible catalogue refused this request. No login or scraping fallback is used; nothing was changed."),
            }
            raise review.ReviewError(i18n.gettext(messages.get(answer.outcome, i18n.N_("The public Audible catalogue could not be read. Try again later; nothing was changed."))))
        candidate = answer.payload
        # A concurrent edit during the catalogue request must not be disguised
        # as the baseline the user is now reviewing.
        with get_db() as db:
            if review.fingerprint(db, item_id) != fingerprint:
                raise review.ReviewError(i18n.gettext("This audiobook changed during lookup. Preview it again."))
        candidate["ticket"] = review.sign(candidate, user_id=request.state.user["id"], item_id=item_id, fingerprint=fingerprint)
        image_candidate = {key: candidate[key] for key in ("kind", "asin", "marketplace", "images")}
        candidate["image_ticket"] = review.sign(image_candidate, user_id=request.state.user["id"], item_id=item_id,
                                                 fingerprint=fingerprint, image=True) if candidate["images"] else ""
        return _render(request, item_id, candidate=candidate, marketplace=market)
    except (review.ReviewError, ValueError) as error:
        return _render(request, item_id, error=str(error), marketplace=market if market in audible_catalog.MARKETPLACES else "")


@router.get("/api/audiobooks/{item_id}/metadata/image")
async def preview_image(request: Request, ticket: str, item_id: int = Path(..., gt=0, le=2**63 - 1)):
    path = None
    try:
        signed = review.verify(ticket, user_id=request.state.user["id"], item_id=item_id, image=True)
        with get_db() as db:
            review.check_current(db, item_id, signed)
        images = signed["candidate"]["images"]
        if not images:
            return Response(status_code=404)
        path = await audible_catalog.download_artwork(0, images[0])
        if not path:
            return Response(status_code=204)
        content = (config.COVERS_DIR / FilePath(path).name).read_bytes()
        if not covers.validate_uploaded_cover(content):
            return Response(status_code=204)
        mime = ("image/png" if content.startswith(b"\x89PNG") else "image/webp" if content.startswith(b"RIFF")
                else "image/gif" if content.startswith(b"GIF") else "image/jpeg")
        return Response(content, media_type=mime, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except (review.ReviewError, ValueError, OSError):
        return Response(status_code=404)
    finally:
        if path:
            (config.COVERS_DIR / FilePath(path).name).unlink(missing_ok=True)


@router.post("/api/audiobooks/{item_id}/metadata/apply")
async def apply(request: Request, item_id: int = Path(..., gt=0, le=2**63 - 1)):
    path = None
    actor = dict(request.state.user)
    try:
        form = await _form(request, review.REVIEW_FORM_BYTES)
        signed = review.verify(form.get("ticket"), user_id=request.state.user["id"], item_id=item_id)
        candidate = signed["candidate"]
        mode, chosen, image = review.choices(form, candidate)
        with get_db() as db:
            review.check_current(db, item_id, signed)
            if image and mode == "backfill" and not review.backfill_artwork_allowed(db, item_id):
                raise review.ReviewError(i18n.gettext("Backfill keeps existing or manually removed artwork. Choose Keep current artwork and try again."))
        if image:
            path = await audible_catalog.download_artwork(item_id, candidate["images"][0])
            if not path:
                raise review.ReviewError(i18n.gettext("The chosen artwork could not be downloaded. Nothing was changed; try keeping the current artwork."))
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            review.apply(db, item_id, signed, mode=mode, chosen=chosen, path=path, actor=actor)
        path = None  # The staged image now belongs to the committed item.
        target = f"/item/{item_id}"
        if request.headers.get("HX-Request", "").lower() == "true":
            return Response(headers={"HX-Redirect": target, "Cache-Control": "no-store"})
        return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})
    except review.PermissionChanged as error:
        # Do not render catalogue values or stale editor controls after a
        # session/permission revocation. The write context has already rolled
        # back; finally removes any staged image before returning the refusal.
        return Response(str(error), status_code=error.status_code, media_type="text/plain", headers={
            "Cache-Control": "no-store", "HX-Trigger": items_common._toast_header(str(error), "error"),
        })
    except (review.ReviewError, ValueError, item_write.ItemValueError, sqlite3.IntegrityError) as error:
        message = str(error) if isinstance(error, review.ReviewError) else i18n.gettext("Those catalogue values could not be saved. Nothing was changed; preview the audiobook again.")
        return _render(request, item_id, error=message)
    finally:
        if path:
            (config.COVERS_DIR / FilePath(path).name).unlink(missing_ok=True)
