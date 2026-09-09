"""Curated Collections pages and management endpoints."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.services import collections as collection_service

router = APIRouter()


def _error(exc: Exception):
    if isinstance(exc, PermissionError):
        return HTMLResponse(str(exc), status_code=403)
    if isinstance(exc, LookupError):
        return HTMLResponse(str(exc), status_code=404)
    return HTMLResponse(str(exc), status_code=409 if "already exists" in str(exc) else 400)


@router.get("/collections")
async def collections_page(request: Request, _=Depends(require_role("viewer"))):
    user = dict(request.state.user)
    with get_db() as db:
        cards = collection_service.list_cards(db, user)
        editable = collection_service.editable_libraries(db, user)
    return request.app.state.templates.TemplateResponse(
        request, "collections.html", {"collections": cards, "editable_libraries": editable},
    )


@router.get("/collections/{collection_id}")
async def collection_detail(
    request: Request, collection_id: int, _=Depends(require_role("viewer"))
):
    user = dict(request.state.user)
    with get_db() as db:
        collection, items = collection_service.list_items(db, user, collection_id)
    if not collection:
        return RedirectResponse(url="/collections", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request, "collection_detail.html", {"collection": collection, "items": items},
    )


@router.post("/api/collections")
async def create_collection(
    request: Request,
    library_id: int = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    _=Depends(require_role("viewer")),
):
    try:
        with get_db() as db:
            collection_service.create(db, dict(request.state.user), library_id, name, description)
    except (PermissionError, ValueError, LookupError) as exc:
        return _error(exc)
    return RedirectResponse(url="/collections", status_code=303)


@router.post("/api/collections/{collection_id}")
async def update_collection(
    request: Request,
    collection_id: int,
    name: str = Form(...),
    description: str = Form(""),
    _=Depends(require_role("viewer")),
):
    try:
        with get_db() as db:
            collection_service.update(
                db, dict(request.state.user), collection_id, name, description
            )
    except (PermissionError, ValueError, LookupError) as exc:
        return _error(exc)
    return RedirectResponse(url="/collections", status_code=303)


@router.delete("/api/collections/{collection_id}")
async def delete_collection(
    request: Request, collection_id: int, _=Depends(require_role("viewer"))
):
    try:
        with get_db() as db:
            collection_service.delete(db, dict(request.state.user), collection_id)
    except (PermissionError, ValueError, LookupError) as exc:
        return _error(exc)
    return HTMLResponse("")


@router.post("/api/items/{item_id}/collections")
async def add_item_to_collection(
    request: Request,
    item_id: int,
    collection_id: int = Form(...),
    _=Depends(require_role("viewer")),
):
    try:
        with get_db() as db:
            collection_service.add_item(
                db, dict(request.state.user), collection_id, item_id
            )
    except (PermissionError, ValueError, LookupError) as exc:
        return _error(exc)
    return RedirectResponse(url=f"/item/{item_id}", status_code=303)


@router.delete("/api/items/{item_id}/collections/{collection_id}")
async def remove_item_from_collection(
    request: Request,
    item_id: int,
    collection_id: int,
    _=Depends(require_role("viewer")),
):
    try:
        with get_db() as db:
            collection_service.remove_item(
                db, dict(request.state.user), collection_id, item_id
            )
    except (PermissionError, ValueError, LookupError) as exc:
        return _error(exc)
    return HTMLResponse("")
