"""Personal library pages and forms: viewer role first, optional feature second."""

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import RedirectResponse

from app import i18n
from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.routers import items_common
from app.services import personal_library


router = APIRouter()


def _not_found():
    message = i18n.gettext("Item not found")
    raise HTTPException(404, message, headers={
        "HX-Trigger": items_common._toast_header(message, "error"),
    })


def _fragment(request: Request, context: dict, *, error: str | None = None):
    headers = {"Cache-Control": "no-store"}
    if error:
        # HTMX doesn't swap 4xx bodies (G145); its toast header is still read.
        headers["HX-Trigger"] = items_common._toast_header(error, "error")
    return request.app.state.templates.TemplateResponse(
        request, "fragments/personal_library.html", {
            **context, "personal_error": error, "personal_editor_open": True,
        }, status_code=422 if error else 200, headers=headers,
    )


def _saved(request: Request, item_id: int, context: dict):
    if request.headers.get("HX-Request", "").lower() == "true":
        return _fragment(request, context)
    # Never use Referer, next, a username, or any other caller-supplied URL.
    return RedirectResponse(f"/item/{item_id}", status_code=303,
                            headers={"Cache-Control": "no-store"})


@router.get("/activity")
async def activity_page(
    request: Request,
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
):
    with get_db() as db:
        context = personal_library.dashboard(db, user["id"])
    return request.app.state.templates.TemplateResponse(
        request, "activity.html", context, headers={"Cache-Control": "no-store"},
    )


@router.get("/api/library/items/{item_id}/personal")
async def get_personal(
    request: Request,
    item_id: int = Path(gt=0, le=2**63 - 1),
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
):
    with get_db() as db:
        context = personal_library.personal_context(db, user["id"], item_id)
    if context is None:
        _not_found()
    return _fragment(request, context)


@router.post("/api/library/items/{item_id}/personal")
async def save_personal(
    request: Request,
    item_id: int = Path(gt=0, le=2**63 - 1),
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in ("application/x-www-form-urlencoded", "multipart/form-data"):
        message = i18n.gettext("Submit personal changes using the form")
        raise HTTPException(415, message, headers={
            "HX-Trigger": items_common._toast_header(message, "error"),
        })
    try:
        async with request.form() as form:
            values = personal_library.parse_form(form)
    except ValueError as exc:
        with get_db() as db:
            context = personal_library.personal_context(db, user["id"], item_id)
        if context is None:
            _not_found()
        return _fragment(request, context, error=str(exc))

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if personal_library.save(db, user["id"], item_id, values) is None:
            _not_found()
        context = personal_library.personal_context(db, user["id"], item_id)
    # Rendering (which may read nav settings or log) is outside the write lock.
    return _saved(request, item_id, context)


@router.post("/api/library/items/{item_id}/personal/reset")
async def reset_personal(
    request: Request,
    item_id: int = Path(gt=0, le=2**63 - 1),
    user=Depends(require_role("viewer")),
    _=Depends(require_feature("connected_library")),
):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if not personal_library.reset(db, user["id"], item_id):
            _not_found()
        context = personal_library.personal_context(db, user["id"], item_id)
    return _saved(request, item_id, context)
