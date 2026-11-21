"""Place exact physical copies at location nodes through the native writers.

The item-level location is a legacy flat ID, never a copy's node ID. Both the
modern location_id and fork location_node_id request names designate a NODE on
this surface; conversion to the legacy item seam is explicit.
"""

from fastapi import APIRouter, Depends, Form, Request

from app import features, i18n
from app.config import MEDIA_TYPES
from app.database import get_db, get_game_platforms
from app.features import require_feature
from app.media_types import is_physical_media
from app.routers import items, items_common, items_scan_modes
from app.services import holdings, item_copies, item_write, location_tree, locations
from app.services import tags as tags_svc
from app.services.catalogue_security import require_catalogue_role, require_item_role
from app.services.item_write import update_item_fields

router = APIRouter()


def _location(db, node_id: int):
    row = db.execute("SELECT id, name FROM location_nodes WHERE id = ?", (node_id,)).fetchone()
    if not row:
        raise ValueError(i18n.gettext("Shelf location no longer exists"))
    return row


def _target(location_id, location_node_id):
    if location_id is not None and location_node_id is not None and location_id != location_node_id:
        raise ValueError("Conflicting shelf locations")
    value = location_node_id if location_node_id is not None else location_id
    if value is None or value <= 0:
        raise ValueError("Choose a shelf location before scanning")
    return value


def continuation_fields(location_node_id: int | None, shelf_fill_origin: str) -> dict:
    """Explicit opt-in: a copy NODE plus one of two local return surfaces.

    Never infer Shelf Fill from a flat location_id or an arbitrary return URL.
    Routes using this data still run their own dependencies and write-lock ACLs.
    """
    if location_node_id is None and not shelf_fill_origin:
        return {}
    if not location_node_id or location_node_id <= 0 or shelf_fill_origin not in {"scan", "shelf_fill"}:
        raise ValueError("Invalid Shelf Fill continuation; start a fresh scan")
    return {"location_node_id": location_node_id, "shelf_fill_origin": shelf_fill_origin}


async def require_continuation(request: Request, location_node_id: int | None, shelf_fill_origin: str) -> dict:
    context = continuation_fields(location_node_id, shelf_fill_origin)
    if context:
        await require_feature("shelf_fill")(request)
    return context


def continuation_location(db, context: dict, flat_location_id: int | None) -> int | None:
    """Call under the confirmation's write lock, before the first mutation."""
    if not context:
        return item_write.validated_location_id(db, flat_location_id)
    node_id = context["location_node_id"]
    _location(db, node_id)
    return location_tree.node_to_legacy_location(db, node_id)


def _copy_by_barcode(db, raw: str):
    row = db.execute(
        "SELECT c.id AS copy_id, c.item_id, c.copy_number, c.is_primary "
        "FROM copies_live c JOIN items_live i ON i.id = c.item_id "
        "WHERE c.copy_barcode = ? LIMIT 1", (raw,),
    ).fetchone()
    return dict(row) if row else None


def _place_item(db, item_id: int, location_id: int, *, copy_id: int | None = None) -> dict:
    item_copies.write_lock(db)
    require_item_role(db, item_id, "editor")
    target = _location(db, location_id)
    item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if not item:
        raise ValueError(i18n.gettext("Item no longer exists"))
    if not is_physical_media(item["media_type"]):
        raise ValueError("Shelf Fill only works with physical media")
    was_wishlist = not bool(item["owned"])
    if was_wishlist:
        update_item_fields(db, item_id, {"owned": 1})
    holdings.sync_item(db, item_id)
    if copy_id is None:
        copy = db.execute(
            "SELECT id, copy_number FROM copies_live WHERE item_id = ? "
            "ORDER BY is_primary DESC, copy_number, id LIMIT 1", (item_id,),
        ).fetchone()
        if copy is None:
            copy_id = item_copies.add_copy(db, item_id, {"location_id": location_id})
        else:
            copy_id = copy["id"]
    copy = db.execute(
        "SELECT id, copy_number FROM copies_live WHERE id = ? AND item_id = ?",
        (copy_id, item_id),
    ).fetchone()
    if copy is None:
        raise ValueError("Physical copy no longer exists")
    position = db.execute(
        "SELECT COALESCE(MAX(position_order), 0) + 1 AS n FROM copies_live "
        "WHERE location_id = ? AND id != ?", (location_id, copy_id),
    ).fetchone()["n"]
    if not item_copies.update_copy(db, copy_id, {"location_id": location_id, "position_order": position}):
        raise ValueError("Physical copy no longer exists")
    item_copies.clear_missing(db, copy_id)
    return {
        "item_id": item_id, "title": item["title"], "authors": item["authors"],
        "media_type": item["media_type"], "cover_path": item["cover_path"],
        "copy_id": copy_id, "copy_number": copy["copy_number"],
        "position_order": position, "location_id": location_id,
        "location_node_id": location_id, "location_name": target["name"],
        "location_path": " › ".join(p["name"] for p in location_tree.location_path(db, location_id)),
        "was_wishlist": was_wishlist,
    }


def _shelf_summary(db, location_id: int) -> dict:
    row = db.execute(
        "SELECT COUNT(*) AS total, COUNT(position_order) AS placed, "
        "COALESCE(MAX(position_order), 0) + 1 AS next_position "
        "FROM copies_live WHERE location_id = ?", (location_id,),
    ).fetchone()
    return dict(row)


def _render_response(request: Request, context: dict, *, origin: str = ""):
    """HTMX/fetch get a fragment; native continuations get a usable full page.

    Only this fixed origin vocabulary selects a template. No redirect target or
    item metadata is accepted from the browser. All receipts are server results.
    """
    if origin and request.headers.get("HX-Request") != "true":
        continuation_fields(context["location_node_id"], origin)
        with get_db() as db:
            common = {"media_types": MEDIA_TYPES, "game_platforms": get_game_platforms(db),
                      "shelf_fill_receipt": True, "shelf_fill_origin": origin,
                      "initial_shelf_node": context["location_node_id"], **context,
                      "render_oob_summary": False}
            if origin == "scan":
                from app.routers.pages import _status_label_sets
                common.update(
                    locations=db.execute("SELECT * FROM locations ORDER BY sort_order, name").fetchall(),
                    location_nodes=locations.location_tree(db),
                    borrowers=db.execute("SELECT * FROM borrowers ORDER BY name").fetchall(),
                    manual_open=False, manual_prefill={}, off_scan_modes=features.disabled_scan_modes(),
                    seed_mode="shelf_fill", seed_code="", status_label_sets=_status_label_sets(),
                )
            else:
                common["locations"] = locations.location_tree(db)
        return request.app.state.templates.TemplateResponse(
            request, "scan.html" if origin == "scan" else "shelf_fill.html", common,
        )
    return request.app.state.templates.TemplateResponse(request, "fragments/scan_result.html", context)


def _render_result(request: Request, placed: dict, *, newly_added: bool = False, origin: str = ""):
    with get_db() as db:
        summary = _shelf_summary(db, placed["location_id"])
    return _render_response(
        request, {**placed, "status": "moved", "shelf_fill_placed": True,
                  "newly_added": newly_added, "summary": summary, "render_oob_summary": True,
                  "shelf_fill_origin": origin}, origin=origin,
    )


def _render_saved_unplaced(request: Request, raw: str, context: dict, node_id: int,
                           message: str, *, origin: str = ""):
    """Only for an accepted, ALREADY COMMITTED Add/restore/promotion result.

    The retry contract is status=saved_unplaced, actual item_id, captured NODE,
    and a native /place form. It must never cause a second Add.
    """
    return _render_response(
        request, {"status": "saved_unplaced", "isbn": raw, "item_id": int(context["item_id"]),
                  "title": context.get("title") or f'Item #{context["item_id"]}',
                  "location_node_id": node_id, "shelf_fill_origin": origin, "message": message},
        origin=origin,
    )


def _render_error(request: Request, raw: str, message: str):
    return request.app.state.templates.TemplateResponse(
        request, "fragments/scan_result.html", {"status": "error", "isbn": raw, "message": message},
    )


@router.get("/shelf-fill")
async def shelf_fill_page(request: Request, _=Depends(require_catalogue_role("editor")),
                          __=Depends(require_feature("shelf_fill"))):
    with get_db() as db:
        tree = locations.location_tree(db)
        platforms = get_game_platforms(db)
    return request.app.state.templates.TemplateResponse(
        request, "shelf_fill.html", {"locations": tree, "media_types": MEDIA_TYPES, "game_platforms": platforms},
    )


@router.get("/api/shelf-fill/location-picker")
async def shelf_fill_location_picker(request: Request, _=Depends(require_catalogue_role("editor")),
                                     __=Depends(require_feature("shelf_fill"))):
    with get_db() as db:
        tree = locations.location_tree(db)
    return request.app.state.templates.TemplateResponse(
        request, "fragments/shelf_fill_location_picker.html", {"locations": tree},
    )


@router.get("/api/shelf-fill/summary")
async def shelf_fill_summary(request: Request, location_id: int = 0,
                            _=Depends(require_catalogue_role("editor")),
                            __=Depends(require_feature("shelf_fill"))):
    context = {"summary": None}
    if location_id > 0:
        with get_db() as db:
            try:
                target = _location(db, location_id)
                context.update(summary=_shelf_summary(db, location_id),
                               location_name=target["name"], location_id=location_id)
            except ValueError as exc:
                context["error"] = str(exc)
    return request.app.state.templates.TemplateResponse(request, "fragments/shelf_fill_summary.html", context)


@router.post("/api/shelf-fill/place")
async def shelf_fill_place(request: Request, item_id: int = Form(...),
                          location_id: int | None = Form(None), location_node_id: int | None = Form(None),
                          shelf_fill_origin: str = Form(""),
                          _=Depends(require_catalogue_role("editor")),
                          __=Depends(require_feature("shelf_fill"))):
    target = None
    try:
        target = _target(location_id, location_node_id)
        if shelf_fill_origin:
            continuation_fields(target, shelf_fill_origin)
        with get_db() as db:
            placed = _place_item(db, item_id, target)
    except ValueError as exc:
        if target is not None and shelf_fill_origin in {"", "scan", "shelf_fill"}:
            # A native retry must keep its ID on a second failure too. Verify
            # that this is a real, authorized saved item; never invent success
            # for an arbitrary/missing/hidden item_id supplied by a caller.
            with get_db() as db:
                item_copies.write_lock(db)
                item = require_item_role(db, item_id, "editor")
            return _render_saved_unplaced(
                request, str(item_id), {"item_id": item_id, "title": item["title"]},
                target, str(exc), origin=shelf_fill_origin,
            )
        return _render_error(request, str(item_id), str(exc))
    items_common._log_scan(str(item_id), placed["media_type"], "shelved", item_id, "shelf_fill")
    return _render_result(request, placed, origin=shelf_fill_origin)


@router.post("/api/shelf-fill/scan")
async def shelf_fill_scan(request: Request, isbn: str = Form(...),
                         location_id: int | None = Form(None), location_node_id: int | None = Form(None),
                         media_type: str = Form("auto"), platform: str = Form(""),
                         legacy_confirm_isbn13: str = Form(""), legacy_supplement: str = Form(""),
                         tags: str = Form(""), reading_status: str = Form(""),
                         shelf_fill_origin: str = Form(""),
                         _=Depends(require_catalogue_role("editor")),
                         __=Depends(require_feature("shelf_fill"))):
    raw = isbn.strip()
    if not raw:
        return _render_error(request, raw, i18n.gettext("Enter or scan a barcode"))
    try:
        target_id = _target(location_id, location_node_id)
        if shelf_fill_origin:
            continuation_fields(target_id, shelf_fill_origin)
        with get_db() as db:
            item_copies.write_lock(db)
            _location(db, target_id)
            legacy_id = location_tree.node_to_legacy_location(db, target_id)
            exact = _copy_by_barcode(db, raw)
            placed = _place_item(db, exact["item_id"], target_id, copy_id=exact["copy_id"]) if exact else None
        if placed:
            items_common._log_scan(raw, placed["media_type"], "moved", placed["item_id"], "move")
            return _render_result(request, placed, origin=shelf_fill_origin)
        existing = items_scan_modes._find_item_by_barcode(raw)
        if existing and existing.get("deleted_at"):
            response = items_scan_modes._scan_mode_in_trash(
                request, request.app.state.templates, existing, raw, "move")
            return request.app.state.templates.TemplateResponse(
                request, "fragments/scan_result.html",
                {**response.context, "location_node_id": target_id,
                 "shelf_fill_origin": shelf_fill_origin or "shelf_fill"},
            )
        if existing:
            with get_db() as db:
                placed = _place_item(db, existing["id"], target_id)
            items_common._log_scan(raw, placed["media_type"], "moved", placed["item_id"], "move")
            return _render_result(request, placed, origin=shelf_fill_origin)
    except ValueError as exc:
        return _render_error(request, raw, str(exc))

    # Use the undecorated business helper, never bypass a route dependency by
    # directly calling its decorated handler. This route owns its gates; the
    # native writers recheck each target's authority under their write locks.
    try:
        with tags_svc.default_tags(tags), item_write.default_reading_status(reading_status):
            response = await items._scan_isbn_inner(
                request, raw, media_type, legacy_id, platform, "add", None,
                legacy_confirm_isbn13, legacy_supplement, tags, reading_status, None,
            )
    except item_write.ItemValueError as exc:
        return _render_error(request, raw, str(exc))
    context = getattr(response, "context", None) or {}
    status, item_id = context.get("status"), context.get("item_id")
    continuation = {"location_node_id": target_id,
                    "shelf_fill_origin": shelf_fill_origin or "shelf_fill",
                    "tags": tags, "reading_status": reading_status}
    submitted = await request.form()
    if submitted.get("library_id"):
        continuation["library_id"] = submitted["library_id"]
    if status in {"legacy_incomplete", "legacy_ambiguous"}:
        return request.app.state.templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {**context, **continuation, "scan_action": "/api/shelf-fill/scan", "location_id": None},
        )
    if item_id and status in {"added", "duplicate", "restored", "promoted"}:
        try:
            with get_db() as db:
                placed = _place_item(db, int(item_id), target_id)
        except ValueError as exc:
            return _render_saved_unplaced(request, raw, context, target_id, str(exc), origin=shelf_fill_origin)
        return _render_result(request, placed, newly_added=status == "added", origin=shelf_fill_origin)
    template_name = getattr(getattr(response, "template", None), "name", "")
    if template_name in {"fragments/periodical_scan_result.html", "fragments/magazine_issue_form.html",
                         "fragments/scan_result.html"} and context:
        return request.app.state.templates.TemplateResponse(
            request, template_name, {**context, **continuation},
        )
    return response
