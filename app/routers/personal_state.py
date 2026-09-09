"""Signed-in user's state on shared catalogue items."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import require_role
from app.database import get_db
from app.services import libraries, user_state

# Install the focused per-user Browse route adaptations while pages/items are
# imported but before app.main mounts either APIRouter onto FastAPI.
from app.routers import personal_browse  # noqa: F401,E402


router = APIRouter(prefix="/api/items")


def _user_id(request: Request) -> int:
    return int(request.state.user["id"])


def _render(request: Request, item_id: int, *, status_code: int = 200):
    with get_db() as db:
        user = dict(request.state.user)
        if not libraries.has_item_role(db, user, item_id, "viewer"):
            return HTMLResponse("Item not found", status_code=404)
        item = db.execute(
            "SELECT id, title, media_type FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if not item:
            return HTMLResponse("Item not found", status_code=404)
        state = user_state.get_state(db, _user_id(request), item_id)
        history = user_state.get_reading_history(db, _user_id(request), item_id)

    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/personal_state.html",
        {
            "item": item,
            "personal_state": state,
            "reading_history": history,
            "status_labels": user_state.status_labels(item["media_type"]),
        },
        status_code=status_code,
    )


@router.get("/{item_id}/personal-state")
async def personal_state_fragment(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    """Render only the acting user's personal state for one item."""
    return _render(request, item_id)


@router.post("/{item_id}/personal-state")
async def update_personal_state(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    """Update supplied personal fields and re-render the personal-state card."""
    form = await request.form()
    user = dict(request.state.user)
    user_id = int(user["id"])

    try:
        with get_db() as db:
            if not libraries.has_item_role(db, user, item_id, "viewer"):
                return HTMLResponse("Item not found", status_code=404)
            if not db.execute(
                "SELECT 1 FROM items WHERE id = ?", (item_id,)
            ).fetchone():
                return HTMLResponse("Item not found", status_code=404)

            if "reading_status" in form:
                status = str(form.get("reading_status") or "")
                if status not in ("want_to_read", "reading", "read", ""):
                    return HTMLResponse("Invalid reading status", status_code=400)
                user_state.set_reading_status(db, user_id, item_id, status or None)

            updates = {}
            if "rating" in form:
                raw = str(form.get("rating") or "").strip()
                updates["rating"] = None if not raw else int(raw)
            if "wishlist" in form:
                updates["wishlist"] = int(str(form.get("wishlist") or "0"))
            if "favourite" in form:
                updates["favourite"] = int(str(form.get("favourite") or "0"))
            if "personal_notes" in form:
                updates["personal_notes"] = str(form.get("personal_notes") or "")

            if "progress_value" in form:
                raw = str(form.get("progress_value") or "").strip()
                updates["progress_value"] = None if not raw else float(raw)
            if "progress_total" in form:
                raw = str(form.get("progress_total") or "").strip()
                updates["progress_total"] = None if not raw else float(raw)
            if "progress_unit" in form:
                updates["progress_unit"] = str(form.get("progress_unit") or "")

            if updates:
                user_state.save_state(db, user_id, item_id, **updates)
    except (TypeError, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=400)
    except LookupError:
        return HTMLResponse("Item not found", status_code=404)

    return _render(request, item_id)
