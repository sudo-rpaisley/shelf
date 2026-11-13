"""User-triggered, exact-ASIN metadata/artwork preview and selective backfill."""

import sqlite3
from pathlib import Path as FilePath

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException as FormHTTPException

from app import auth, config, i18n
from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.http_body import BodyTooLarge, bounded_body
from app.routers import items_common
from app.services import audible_catalog, audiobook_review as review, covers, item_write


router = APIRouter(dependencies=[Depends(require_role("editor")), Depends(require_feature("connected_library"))])
form_limit = review.form_limit  # main.CSRFMiddleware must call this before parsing native forms.


def _entry_actor(request):
    """Verify/capture the session before any await, never decode/log under lock."""
    actor = dict(request.state.user)
    claims = auth.decode_token(request.cookies.get("access_token", ""))
    try:
        if (not claims or claims.get("sub") != str(actor["id"])
                or claims.get("tv", 1) != actor["token_version"]
                or str(claims.get("username", "")).casefold() != actor["username"].casefold()):
            raise ValueError
        actor["expires_at"] = int(claims["exp"])
    except (ValueError, TypeError, KeyError, OverflowError):
        raise review.PermissionChanged(i18n.gettext("Your session changed while this review was open. Nothing was saved; sign in and review again."), 401) from None
    return actor


def _refused(error):
    return Response(str(error), status_code=error.status_code, media_type="text/plain", headers={
        "Cache-Control": "no-store", "HX-Trigger": items_common._toast_header(str(error), "error"),
    })


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


def _render(request, item_id, *, candidate=None, error=None, marketplace="", status_code=200, checked=None):
    if checked is not None:
        item, missing = checked
    else:
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


def _error(request, item_id, actor, message, **kwargs):
    # Form/provider/download failures must not fall back to cached editor
    # controls after a revocation either. Rendering/logging stays outside locks.
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            review.authorize_apply(db, actor)
    except review.PermissionChanged as error:
        return _refused(error)
    return _render(request, item_id, error=message, **kwargs)


@router.get("/api/audiobooks/{item_id}/metadata")
async def panel(request: Request, item_id: int = Path(..., gt=0, le=2**63 - 1)):
    # Rendering never spends an external request; even lookup requires a POST.
    return _render(request, item_id)


@router.post("/api/audiobooks/{item_id}/metadata/preview")
async def preview(request: Request, item_id: int = Path(..., gt=0, le=2**63 - 1)):
    market = ""
    try:
        actor = _entry_actor(request)
        form = await _form(request, review.PREVIEW_FORM_BYTES)
        if set(form) - {"marketplace", "_csrf"} or len(form.getlist("marketplace")) != 1 or len(form.getlist("_csrf")) > 1:
            raise review.ReviewError(i18n.gettext("Choose a marketplace; edit the item's ASIN on its edit page."))
        market = form["marketplace"]
        with get_db() as db:
            item = review.item(db, item_id)
            asin, market = audible_catalog.identity(item["asin"], market)
            fingerprint = review.fingerprint(db, item_id)
        answer = await audible_catalog.lookup(asin, market)
        previous = {"candidate": {"asin": asin, "marketplace": market}, "fingerprint": fingerprint}
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = review.authorize_read(db, item_id, actor, previous)
            candidate = dict(answer.payload) if answer.found else None
            checked = (current, review.backfill_fields(db, item_id, candidate) if candidate else set())
            if candidate:
                # Mint only after the live actor, uncached feature and exact
                # item fingerprint agree under this SAME lock. No HTTP here.
                candidate["ticket"] = review.sign(candidate, user_id=actor["id"], item_id=item_id, fingerprint=fingerprint)
                image_candidate = {key: candidate[key] for key in ("kind", "asin", "marketplace", "images")}
                candidate["image_ticket"] = review.sign(image_candidate, user_id=actor["id"], item_id=item_id,
                                                         fingerprint=fingerprint, image=True) if candidate["images"] else ""
        if not answer.found:
            messages = {
                "no_match": i18n.N_("No exact audiobook was found in that marketplace. Existing metadata and artwork were kept."),
                "rate_limited": i18n.N_("Audible rate-limited this request. Try again later; nothing was changed."),
                "rejected": i18n.N_("The public Audible catalogue refused this request. No login or scraping fallback is used; nothing was changed."),
            }
            message = i18n.gettext(messages.get(answer.outcome, i18n.N_("The public Audible catalogue could not be read. Try again later; nothing was changed.")))
            return _render(request, item_id, error=message, marketplace=market, checked=checked)
        return _render(request, item_id, candidate=candidate, marketplace=market, checked=checked)
    except review.PermissionChanged as error:
        return _refused(error)
    except (review.ReviewError, ValueError) as error:
        return _error(request, item_id, actor, str(error), marketplace=market if market in audible_catalog.MARKETPLACES else "")


@router.get("/api/audiobooks/{item_id}/metadata/image")
async def preview_image(request: Request, ticket: str, item_id: int = Path(..., gt=0, le=2**63 - 1)):
    path = None
    try:
        actor = _entry_actor(request)
        signed = review.verify(ticket, user_id=request.state.user["id"], item_id=item_id, image=True)
        with get_db() as db:
            review.check_current(db, item_id, signed)
        images = signed["candidate"]["images"]
        if not images:
            return Response(status_code=404)
        path = await audible_catalog.download_artwork(0, images[0])
        content = (config.COVERS_DIR / FilePath(path).name).read_bytes() if path else None
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            review.authorize_read(db, item_id, actor, signed)
            if content is None or not covers.validate_uploaded_cover(content):
                return Response(status_code=204, headers={"Cache-Control": "no-store"})
            mime = ("image/png" if content.startswith(b"\x89PNG") else "image/webp" if content.startswith(b"RIFF")
                    else "image/gif" if content.startswith(b"GIF") else "image/jpeg")
            return Response(content, media_type=mime, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except review.PermissionChanged as error:
        return _refused(error)
    except (review.ReviewError, ValueError, OSError):
        return Response(status_code=404)
    finally:
        if path:
            (config.COVERS_DIR / FilePath(path).name).unlink(missing_ok=True)


@router.post("/api/audiobooks/{item_id}/metadata/apply")
async def apply(request: Request, item_id: int = Path(..., gt=0, le=2**63 - 1)):
    path = None
    try:
        actor = _entry_actor(request)
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
        return _refused(error)
    except (review.ReviewError, ValueError, item_write.ItemValueError, sqlite3.IntegrityError) as error:
        message = str(error) if isinstance(error, review.ReviewError) else i18n.gettext("Those catalogue values could not be saved. Nothing was changed; preview the audiobook again.")
        return _error(request, item_id, actor, message)
    finally:
        if path:
            (config.COVERS_DIR / FilePath(path).name).unlink(missing_ok=True)
