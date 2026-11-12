"""Viewer-safe provenance fragment and explicit, editor-only field resolution."""

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException as FormHTTPException

from app import i18n
from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.http_body import BodyTooLarge, METADATA_FORM_BYTES, bounded_body
from app.routers import items_common
from app.services import metadata_review


router = APIRouter()


def _not_found():
    message = i18n.gettext("Item not found")
    raise HTTPException(404, message, headers={
        "Cache-Control": "no-store", "HX-Trigger": items_common._toast_header(message, "error"),
    })


def _fragment(request: Request, context: dict, *, error: str | None = None,
              status_code: int = 200, notice: str | None = None):
    headers = {"Cache-Control": "no-store"}
    if error:
        # HTMX does not swap 4xx bodies; HX-Trigger is the visible error path
        # (G145). The body also remains useful to native form submissions.
        headers["HX-Trigger"] = items_common._toast_header(error, "error")
    return request.app.state.templates.TemplateResponse(
        request, "fragments/metadata_provenance.html", {
            **context, "metadata_error": error, "metadata_notice": notice,
            "metadata_open": bool(error or notice),
        }, status_code=status_code, headers=headers,
    )


def _refused(request: Request, item_id: int, error: metadata_review.ReviewError):
    with get_db() as db:
        context = metadata_review.context_for(db, item_id)
    if context is None:
        _not_found()
    return _fragment(request, context, error=str(error), status_code=error.status_code)


@router.get("/api/library/items/{item_id}/metadata")
async def item_metadata(
    request: Request,
    item_id: int = Path(..., gt=0, le=2**63 - 1),
    _=Depends(require_role("viewer")),
    _feature=Depends(require_feature("connected_library")),
):
    with get_db() as db:
        context = metadata_review.context_for(db, item_id)
    if context is None:
        _not_found()
    return _fragment(request, context)


@router.post("/api/library/items/{item_id}/metadata/resolve")
async def resolve_metadata(
    request: Request,
    item_id: int = Path(..., gt=0, le=2**63 - 1),
    _=Depends(require_role("editor")),
    _feature=Depends(require_feature("connected_library")),
):
    try:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"application/x-www-form-urlencoded", "multipart/form-data"}:
            raise metadata_review.ReviewError(i18n.gettext("Submit metadata decisions using the review form"))
        try:
            await bounded_body(request, METADATA_FORM_BYTES)
        except BodyTooLarge:
            raise metadata_review.ReviewError(i18n.gettext("The metadata review form is too large; reload Metadata sources"))
        try:
            async with request.form() as form:
                values = metadata_review.parse_form(form)
        except FormHTTPException:
            raise metadata_review.ReviewError(i18n.gettext("Submit a valid metadata review form")) from None
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            metadata_review.resolve_field(db, item_id, **values)
    except metadata_review.ReviewError as error:
        # The write context has rolled back and released its lock before any
        # rendering, toast generation or second connection is reached (G3).
        return _refused(request, item_id, error)

    if request.headers.get("HX-Request", "").lower() != "true":
        # Fixed local target: never Referer, next, HX-Current-URL or a source URL.
        return RedirectResponse(f"/item/{item_id}#metadata-provenance", status_code=303,
                                headers={"Cache-Control": "no-store"})
    with get_db() as db:
        context = metadata_review.context_for(db, item_id)
    if context is None:
        _not_found()
    notice = (i18n.gettext("Local value kept, including any blank value. This source will not replace it.")
              if values["decision"] == "keep_local" else
              i18n.gettext("Source value saved. Future refreshes may update this field only while you leave it unmodified."))
    return _fragment(request, context, notice=notice)
