"""Native-administrator recovery of items lacking a library security mapping."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.services import libraries


router = APIRouter()


@router.get("/settings/library-recovery")
async def recovery_page(request: Request, user=Depends(require_role("admin"))):
    with get_db() as db:
        items = libraries.recovery_items(db, user)
        destinations = libraries.list_libraries(db)
    return request.app.state.templates.TemplateResponse(
        request, "library_recovery.html", {"recovery_items": items, "libraries": destinations},
    )


@router.post("/api/libraries/recovery/items/{item_id}")
async def recover_item(
    item_id: int, library_id: int = Form(...), user=Depends(require_role("admin")),
):
    with get_db() as db:
        libraries.recover_item(db, user, item_id, library_id)
    return RedirectResponse("/settings/library-recovery", status_code=303)
