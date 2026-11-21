"""Personal item state endpoints; never replaces catalogue/scanner handlers."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app import i18n
from app.config import MEDIA_TYPES, status_labels
from app.database import get_db
from app.services import user_state
from app.services.catalogue_security import require_catalogue_role, require_item_role

router = APIRouter(prefix="/api")


def _user_id(request: Request) -> int:
    return int(request.state.user["id"])


def job_principal(db, captured: dict):
    """Re-resolve a request-spawned job's account without adopting its context."""
    if captured.get("api_token_id"):
        return None
    row = db.execute("SELECT id, username, role, token_version FROM users WHERE id = ?", (captured["id"],)).fetchone()
    if row is None or row["token_version"] != captured.get("token_version"):
        return None
    return dict(row)


def remember_wishlist_item(db, user_id: int, item_id: int):
    """Personal wish, independent of shared ownership/acquisition membership.

    Called only after a confirmed catalogue success, never on an error redirect
    or an unresolved scan card. An owned restored row retains native ownership
    and must not be silently demoted just to record a personal preference.
    """
    require_item_role(db, item_id, "viewer")
    row = db.execute("SELECT owned FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if row is not None and not row["owned"]:
        user_state.seed_wishlist_for_user(db, user_id, item_id)


def remember_wishlist_result(request: Request, response, mode: str):
    context = getattr(response, "context", None) or {}
    if mode != "wishlist" or context.get("status") not in {"wishlisted", "restored"} or not context.get("item_id"):
        return
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        remember_wishlist_item(db, _user_id(request), int(context["item_id"]))


def _render_personal_state(request: Request, item_id: int):
    with get_db() as db:
        require_item_role(db, item_id, "viewer")
        item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
        if not item:
            return HTMLResponse("Item not found", status_code=404)
        state = user_state.get_state(db, _user_id(request), item_id)
        history = user_state.get_reading_history(db, _user_id(request), item_id)
    return request.app.state.templates.TemplateResponse(
        request, "fragments/personal_state.html",
        {"item": item, "personal_state": state, "reading_history": history,
         "personal_status_labels": user_state.status_labels(item["media_type"])},
    )


@router.get("/items/{item_id}/personal-state")
async def personal_state_fragment(request: Request, item_id: int,
                                  _=Depends(require_catalogue_role("viewer"))):
    return _render_personal_state(request, item_id)


@router.post("/items/{item_id}/personal-state")
async def update_personal_state(request: Request, item_id: int,
                                _=Depends(require_catalogue_role("viewer"))):
    form = await request.form()
    uid = _user_id(request)
    try:
        changes = {}
        for key in ("rating", "wishlist", "favourite"):
            if key in form:
                raw = str(form.get(key) or "").strip()
                changes[key] = None if key == "rating" and not raw else int(raw or "0")
        for key in ("personal_notes", "progress_unit", "date_started", "date_finished"):
            if key in form:
                changes[key] = str(form.get(key) or "").strip() or None
        for key in ("progress_value", "progress_total"):
            if key in form:
                raw = str(form.get(key) or "").strip()
                changes[key] = float(raw) if raw else None
        if "reading_status" in form:
            changes["reading_status"] = str(form.get("reading_status") or "") or None
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            require_item_role(db, item_id, "viewer")
            state = user_state.get_state(db, uid, item_id)
            # Validate the ENTIRE submission before creating a completion log.
            user_state.validate_state_changes(changes, state)
            if "reading_status" in changes:
                user_state.set_reading_status(db, uid, item_id, changes.pop("reading_status"))
            if changes:
                user_state.save_state(db, uid, item_id, **changes)
    except (TypeError, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=400)
    except LookupError:
        return HTMLResponse("Item not found", status_code=404)
    return _render_personal_state(request, item_id)


@router.get("/home/personal-in-progress")
async def personal_in_progress(request: Request, _=Depends(require_catalogue_role("viewer"))):
    with get_db() as db:
        rows = db.execute(
            "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, "
            "s.progress_value, s.progress_total, s.progress_unit "
            "FROM user_item_state s JOIN items_live i ON i.id = s.item_id "
            "WHERE s.user_id = ? AND s.reading_status = 'reading' "
            "ORDER BY s.updated_at DESC, i.id DESC LIMIT 6", (_user_id(request),),
        ).fetchall()
    return request.app.state.templates.TemplateResponse(
        request, "fragments/personal_in_progress.html", {"in_progress": rows, "media_types": MEDIA_TYPES},
    )


def quick_rate(request: Request, templates, item: dict, raw: str):
    """Native scanner result protocol, with actor-owned completion/history."""
    from app.routers import items_common

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        require_item_role(db, item["id"], "viewer")
        # Re-read eligibility/liveness and media language inside the lock. A
        # stale barcode result is not authority to complete a now-trashed item.
        current = db.execute("SELECT * FROM items_live WHERE id = ?", (item["id"],)).fetchone()
        if current is None:
            return HTMLResponse("Item not found", status_code=404)
        item = dict(current)
        user_state.set_reading_status(db, _user_id(request), item["id"], "read")
    items_common._log_scan(raw, item.get("media_type", ""), "marked_read", item["id"], "quick_rate")
    completed = status_labels(item.get("media_type")).done_lower
    return templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {"status": "marked_read", "isbn": raw, "title": item["title"], "item_id": item["id"],
          "cover_path": item.get("cover_path"), "authors": item.get("authors"),
          "media_type": item.get("media_type"),
          "message": i18n.gettext("Marked as %(status)s") % {"status": i18n.gettext(completed)}},
    )
