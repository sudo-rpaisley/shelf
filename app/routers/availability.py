"""Read-only item availability fragment for the connected Library."""

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import HTMLResponse

from app import i18n
from app.auth import require_role
from app.database import get_db
from app.features import require_feature
from app.services import availability


router = APIRouter()


@router.get("/api/library/items/{item_id}/availability")
async def item_availability(
    request: Request,
    item_id: int = Path(..., gt=0, le=2**63 - 1),
    _=Depends(require_role("viewer")),
    _feature=Depends(require_feature("connected_library")),
):
    with get_db() as db:
        context = availability.available_here(db, item_id)
    if context is None:
        return HTMLResponse(i18n.gettext("Item not found"), status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "fragments/available_here.html", context
    )
