"""Personal-state adaptations for legacy item routes.

``items.py`` owns a large set of catalogue and scan workflows. Rather than
copying that module, this focused extension replaces only routes whose old
behaviour wrote household-wide reading/wishlist state and adds the explicitly
personal API endpoints to the already-mounted ``/api`` router.

The legacy ``items.reading_status`` columns remain a compatibility shadow only
while an installation has exactly one user. As soon as Shelf is genuinely
multi-user, personal state is authoritative and the shared shadow is no longer
written. This keeps old single-user integrations usable without leaking one
person's state into another account.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse
from starlette.requests import Request as StarletteRequest

from app import browse_filters
from app.auth import require_role
from app.config import DEFAULT_PAGE_SIZE, MEDIA_TYPES
from app.database import get_db
from app.routers import items, items_common
from app.routers.items_common import SORT_OPTIONS
from app.services import browse_grouping, user_state, user_state_browse


def _remove_route(path: str, method: str) -> None:
    """Remove one route from the not-yet-mounted items router."""
    method = method.upper()
    full_path = f"{items.router.prefix}{path}" if items.router.prefix else path
    items.router.routes[:] = [
        route
        for route in items.router.routes
        if not (
            getattr(route, "path", None) == full_path
            and method in (getattr(route, "methods", None) or set())
        )
    ]


def _int_or_none(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _user_id(request: Request) -> int:
    return int(request.state.user["id"])


def _single_user(db) -> bool:
    return db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 1


def _mirror_legacy_status(db, item_id: int, status: str | None, state: dict) -> None:
    """Maintain deprecated shared columns only for a true single-user install."""
    if not _single_user(db):
        return
    db.execute(
        "UPDATE items SET reading_status = ?, date_started = ?, date_finished = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (
            status,
            state.get("date_started"),
            state.get("date_finished"),
            item_id,
        ),
    )


def _render_personal_state(request: Request, item_id: int, *, status_code: int = 200):
    with get_db() as db:
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


# Register personal-state endpoints on the router that app.main already mounts
# explicitly. This avoids import-order sensitivity from decorating pages.router
# after FastAPI may already have copied its routes.
@items.router.get("/items/{item_id}/personal-state")
async def personal_state_fragment(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    return _render_personal_state(request, item_id)


@items.router.post("/items/{item_id}/personal-state")
async def update_personal_state(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    form = await request.form()
    uid = _user_id(request)

    try:
        with get_db() as db:
            if not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
                return HTMLResponse("Item not found", status_code=404)

            if "reading_status" in form:
                status = str(form.get("reading_status") or "")
                if status not in ("want_to_read", "reading", "read", ""):
                    return HTMLResponse("Invalid reading status", status_code=400)
                state = user_state.set_reading_status(db, uid, item_id, status or None)
                _mirror_legacy_status(db, item_id, status or None, state)

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

            progress_fields = ("progress_value", "progress_total", "progress_unit")
            if any(field in form for field in progress_fields):
                if "progress_value" in form:
                    raw = str(form.get("progress_value") or "").strip()
                    updates["progress_value"] = None if not raw else float(raw)
                if "progress_total" in form:
                    raw = str(form.get("progress_total") or "").strip()
                    updates["progress_total"] = None if not raw else float(raw)
                if "progress_unit" in form:
                    updates["progress_unit"] = str(form.get("progress_unit") or "")

            if updates:
                user_state.save_state(db, uid, item_id, **updates)
    except (TypeError, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=400)
    except LookupError:
        return HTMLResponse("Item not found", status_code=404)

    return _render_personal_state(request, item_id)


@items.router.get("/home/personal-in-progress")
async def personal_in_progress(
    request: Request,
    _=Depends(require_role("viewer")),
):
    uid = _user_id(request)
    with get_db() as db:
        user_state.ensure_schema(db)
        rows = db.execute(
            """SELECT i.id, i.title, i.authors, i.media_type, i.cover_path,
                      uis.progress_value, uis.progress_total, uis.progress_unit
                 FROM user_item_state uis
                 JOIN items i ON i.id = uis.item_id
                WHERE uis.user_id = ? AND uis.reading_status = 'reading'
                ORDER BY uis.updated_at DESC, i.id DESC
                LIMIT 6""",
            (uid,),
        ).fetchall()

    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/personal_in_progress.html",
        {"in_progress": rows, "media_types": MEDIA_TYPES},
    )


def _personal_quick_rate(request: Request, templates, item: dict, raw: str):
    """Quick Rate marks only the acting user's copy as completed."""
    uid = _user_id(request)
    with get_db() as db:
        state = user_state.set_reading_status(db, uid, item["id"], "read")
        _mirror_legacy_status(db, item["id"], "read", state)

    items_common._log_scan(
        raw,
        item.get("media_type", ""),
        "marked_read",
        item["id"],
        "quick_rate",
    )
    completed = user_state.status_labels(item.get("media_type") or "book")["read"]
    return templates.TemplateResponse(
        request,
        "fragments/scan_result.html",
        {
            "status": "marked_read",
            "isbn": raw,
            "title": item["title"],
            "item_id": item["id"],
            "cover_path": item.get("cover_path"),
            "authors": item.get("authors"),
            "message": f"Marked as {completed.lower()}",
        },
    )


# scan_isbn resolves this helper from the module global at request time, so the
# focused replacement covers Quick Rate without duplicating the whole scanner.
items._scan_mode_quick_rate = _personal_quick_rate


_original_scan = items.scan_isbn
_remove_route("/scan", "POST")


@items.router.post("/scan")
async def personal_state_scan(request: Request, _=Depends(require_role("editor"))):
    """Preserve the scanner, adding per-user semantics to Wishlist/Quick Rate."""
    form = await request.form()
    raw = str(form.get("isbn") or "")
    mode = str(form.get("mode") or "add")
    media_type = str(form.get("media_type") or "book")
    platform = str(form.get("platform") or "")
    location_id = _int_or_none(form.get("location_id"))
    borrower_id = _int_or_none(form.get("borrower_id"))

    response = await _original_scan(
        request,
        isbn=raw,
        media_type=media_type,
        location_id=location_id,
        platform=platform,
        mode=mode,
        borrower_id=borrower_id,
        _=request.state.user,
    )

    # The legacy wishlist mode still records the shared acquisition fact that
    # the item is not owned. Record *who* wants it separately.
    if mode == "wishlist":
        context = getattr(response, "context", None) or {}
        if context.get("status") == "wishlisted" and context.get("item_id"):
            with get_db() as db:
                user_state.seed_wishlist_for_user(
                    db,
                    _user_id(request),
                    int(context["item_id"]),
                )
    return response


_original_search = items.search_items
_remove_route("/search", "GET")


@items.router.get("/search")
async def personal_search_items(
    request: Request,
    page: int = 1,
    per_page: int = DEFAULT_PAGE_SIZE,
    _=Depends(require_role("viewer")),
):
    """Search/filter items using the signed-in user's status and wishlist."""
    templates = request.app.state.templates
    values = browse_filters.values_from(request.query_params)
    values["q"] = values["q"][:200]
    sort = values["sort"]
    view = values["view"]
    uid = _user_id(request)
    where, params = browse_filters.build_where(values, user_id=uid)
    _, order_clause = SORT_OPTIONS.get(sort, SORT_OPTIONS["newest"])
    offset = (max(page, 1) - 1) * per_page

    with get_db() as db:
        user_state.ensure_schema(db)
        result_items, total, display_total = browse_grouping.fetch_page(
            db,
            where,
            params,
            order_clause,
            limit=per_page,
            offset=offset,
            values=values,
        )
        user_state_browse.overlay_items(db, uid, result_items)
        counts = (
            user_state_browse.filter_counts(db, values, total, uid)
            if page <= 1
            else None
        )

    has_more = (offset + per_page) < display_total
    load_more_url = "/api/search?" + browse_filters.querystring(
        values, extra=[f"page={page + 1}"]
    )
    if page <= 1:
        template = "fragments/item_grid.html"
    elif view == "list":
        template = "fragments/item_rows_page.html"
    else:
        template = "fragments/item_cards_page.html"

    ctx = {
        "items": result_items,
        "media_types": MEDIA_TYPES,
        "has_more": has_more,
        "load_more_url": load_more_url,
        "page": page,
        "total": total,
        "has_filters": browse_filters.has_active_filters(values),
        "seven_days_ago": (
            datetime.now(tz=None) - timedelta(days=7)
        ).strftime("%Y-%m-%d"),
    }
    if counts:
        ctx.update(counts)
        ctx["render_oob_counts"] = True
    return templates.TemplateResponse(request, template, ctx)


_original_bulk_update = items.bulk_update
_remove_route("/items/bulk-update", "POST")


async def _request_with_json(request: Request, payload: dict) -> StarletteRequest:
    body = json.dumps(payload).encode("utf-8")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = dict(request.scope)
    headers = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() not in (b"content-length", b"content-type")
    ]
    headers.extend(
        [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
    )
    scope["headers"] = headers
    return StarletteRequest(scope, receive)


async def _request_with_form(request: Request, pairs: list[tuple[str, str]]) -> StarletteRequest:
    body = urlencode(pairs, doseq=True).encode("utf-8")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = dict(request.scope)
    headers = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() not in (b"content-length", b"content-type")
    ]
    headers.extend(
        [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
    )
    scope["headers"] = headers
    return StarletteRequest(scope, receive)


@items.router.post("/items/bulk-update")
async def personal_bulk_update(
    request: Request,
    _=Depends(require_role("admin")),
):
    """Keep catalogue bulk edits shared but make status changes personal."""
    data = await request.json()
    raw_ids = data.get("item_ids", []) if isinstance(data, dict) else []
    updates = dict(data.get("updates", {})) if isinstance(data, dict) else {}
    if not raw_ids or not updates:
        return {"ok": False, "message": "No items or updates specified"}

    try:
        item_ids = list(dict.fromkeys(int(value) for value in raw_ids))
    except (TypeError, ValueError):
        return {"ok": False, "message": "Invalid item IDs"}
    if any(item_id <= 0 for item_id in item_ids):
        return {"ok": False, "message": "Invalid item IDs"}

    marker = object()
    personal_status = updates.pop("reading_status", marker)
    if personal_status is not marker:
        if personal_status in ("", "__clear__", None):
            personal_status = None
        elif personal_status not in {"want_to_read", "reading", "read"}:
            return {"ok": False, "message": "Invalid reading status"}

    result = None
    if updates:
        patched = await _request_with_json(
            request,
            {"item_ids": item_ids, "updates": updates},
        )
        result = await _original_bulk_update(patched, _=request.state.user)
        if not result.get("ok"):
            return result

    personal_count = 0
    if personal_status is not marker:
        with get_db() as db:
            placeholders = ",".join("?" for _ in item_ids)
            existing_ids = [
                row["id"]
                for row in db.execute(
                    f"SELECT id FROM items WHERE id IN ({placeholders})",
                    item_ids,
                ).fetchall()
            ]
            for item_id in existing_ids:
                state = user_state.set_reading_status(
                    db,
                    _user_id(request),
                    item_id,
                    personal_status,
                )
                _mirror_legacy_status(db, item_id, personal_status, state)
            personal_count = len(existing_ids)

    if result is None:
        if personal_count == 0:
            return {"ok": False, "message": "No matching items found", "updated": 0}
        return {"ok": True, "updated": personal_count}

    result["updated"] = max(int(result.get("updated") or 0), personal_count)
    return result


_original_update_item = items.update_item
_remove_route("/items/{item_id}", "POST")


@items.router.post("/items/{item_id}")
async def personal_safe_update_item(
    request: Request,
    item_id: int,
    _=Depends(require_role("editor")),
):
    """Keep old edit clients safe while activity moves to personal state."""
    form = await request.form()
    personal_fields = {"reading_status", "date_started", "date_finished"}
    present = personal_fields.intersection(form.keys())
    if not present:
        return await _original_update_item(request, item_id, _=request.state.user)

    status_raw = str(form.get("reading_status") or "") if "reading_status" in form else ""
    if status_raw not in ("", "want_to_read", "reading", "read"):
        return HTMLResponse("Invalid reading status", status_code=400)

    try:
        with get_db() as db:
            if not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
                return HTMLResponse("Not found", status_code=404)

            state = None
            if status_raw:
                state = user_state.set_reading_status(
                    db, _user_id(request), item_id, status_raw
                )

            date_updates = {}
            if "date_started" in form and str(form.get("date_started") or "").strip():
                date_updates["date_started"] = str(form.get("date_started")).strip()
            if "date_finished" in form and str(form.get("date_finished") or "").strip():
                date_updates["date_finished"] = str(form.get("date_finished")).strip()
            if date_updates:
                state = user_state.save_state(
                    db, _user_id(request), item_id, **date_updates
                )
            if state is not None:
                _mirror_legacy_status(
                    db, item_id, state.get("reading_status"), state
                )
    except (TypeError, ValueError) as exc:
        return HTMLResponse(str(exc), status_code=400)
    except LookupError:
        return HTMLResponse("Not found", status_code=404)

    # Strip personal fields before the legacy catalogue handler sees the form.
    # New Shelf forms no longer send these fields; this path exists for older
    # clients and bookmarks during the transition.
    pairs: list[tuple[str, str]] = []
    for key, value in form.multi_items():
        if key in personal_fields:
            continue
        if hasattr(value, "filename"):
            # Current Shelf never combines the removed activity fields with a
            # new upload. Ignore an obsolete-client upload rather than serialise
            # an UploadFile object into a text field.
            continue
        pairs.append((str(key), str(value)))
    patched = await _request_with_form(request, pairs)
    return await _original_update_item(patched, item_id, _=request.state.user)


_remove_route("/items/{item_id}/reading-status", "POST")


@items.router.post("/items/{item_id}/reading-status")
async def personal_legacy_reading_status(
    request: Request,
    item_id: int,
    status: str = Form(""),
    _=Depends(require_role("viewer")),
):
    """Backward-compatible old status endpoint, now scoped to the user."""
    if status not in ("want_to_read", "reading", "read", ""):
        return HTMLResponse("Invalid reading status", status_code=400)

    uid = _user_id(request)
    try:
        with get_db() as db:
            state = user_state.set_reading_status(db, uid, item_id, status or None)
            _mirror_legacy_status(db, item_id, status or None, state)
            item = db.execute(
                "SELECT id, title, media_type FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if not item:
                return HTMLResponse("Not found", status_code=404)
            history = user_state.get_reading_history(db, uid, item_id)
    except LookupError:
        return HTMLResponse("Not found", status_code=404)

    labels = user_state.status_labels(item["media_type"])
    label = labels.get(status, "Cleared") if status else "Cleared"
    response = request.app.state.templates.TemplateResponse(
        request,
        "fragments/personal_state.html",
        {
            "item": item,
            "personal_state": state,
            "reading_history": history,
            "status_labels": labels,
        },
    )
    response.headers["HX-Trigger"] = items_common._toast_header(f"Status: {label}")
    return response
