"""Personal-state adaptations for legacy item routes.

``items.py`` owns a large set of catalogue and scan workflows. Rather than
copying that module, this focused extension replaces only routes whose old
behaviour wrote household-wide reading/wishlist state. This follows Shelf's
existing focused scan-dispatch extension pattern.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import Depends, Form, Query, Request
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
    items.router.routes[:] = [
        route
        for route in items.router.routes
        if not (
            getattr(route, "path", None) == path
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


def _personal_quick_rate(request: Request, templates, item: dict, raw: str):
    """Quick Rate marks only the acting user's copy as completed."""
    user_id = int(request.state.user["id"])
    with get_db() as db:
        user_state.set_reading_status(db, user_id, item["id"], "read")

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

    # The legacy wishlist mode already records the shared fact that the item is
    # not currently owned (items.owned=0). Record *who* wants it separately.
    if mode == "wishlist":
        context = getattr(response, "context", None) or {}
        if context.get("status") == "wishlisted" and context.get("item_id"):
            with get_db() as db:
                user_state.seed_wishlist_for_user(
                    db,
                    int(request.state.user["id"]),
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
    user_id = int(request.state.user["id"])
    where, params = browse_filters.build_where(values, user_id=user_id)
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
        user_state_browse.overlay_items(db, user_id, result_items)
        counts = (
            user_state_browse.filter_counts(db, values, total, user_id)
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
                user_state.set_reading_status(
                    db,
                    int(request.state.user["id"]),
                    item_id,
                    personal_status,
                )
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
    """Reject obsolete shared activity fields on the catalogue edit endpoint."""
    form = await request.form()
    personal_fields = {"reading_status", "date_started", "date_finished"}
    if personal_fields.intersection(form.keys()):
        return HTMLResponse(
            "Reading/activity state is personal; update it from the item page",
            status_code=400,
        )
    return await _original_update_item(request, item_id, _=request.state.user)


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

    user_id = int(request.state.user["id"])
    try:
        with get_db() as db:
            state = user_state.set_reading_status(db, user_id, item_id, status or None)
            item = db.execute(
                "SELECT id, title, media_type FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if not item:
                return HTMLResponse("Not found", status_code=404)
            history = user_state.get_reading_history(db, user_id, item_id)
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
