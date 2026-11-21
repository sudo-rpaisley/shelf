"""NODE-domain hierarchy pages/admin routes, explicitly composed by main.py."""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.services import item_copies, location_tree as tree, locations
from app.services.catalogue_security import require_item_role, require_catalogue_role

router = APIRouter()


def _redirect(location_id: int | None = None, *, error: str = ""):
    target = f"/locations/{location_id}" if location_id else "/locations"
    if error:
        target += "?" + urlencode({"error": error})
    return RedirectResponse(url=target, status_code=303)


def _parse_parent(value) -> int | None:
    value = str(value or "").strip()
    if not value or value == "0":
        return None
    if not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise ValueError("Invalid parent")
    return int(value)


def _context(db, selected=None, *, error=""):
    nodes = tree.flattened_tree(db)
    node_id = selected["id"] if selected else None
    children = [row for row in nodes if row["parent_id"] == node_id] if selected else []
    copies = tree.direct_copies(db, node_id) if selected else []
    return {
        "location_nodes": nodes, "locations": locations.location_tree(db),
        "selected_location": dict(selected) if selected else None,
        "location": dict(selected) if selected else None,
        "breadcrumb": tree.location_path(db, node_id),
        "child_locations": children, "children": children,
        "location_copies": copies, "copies": copies, "error": error,
    }


@router.get("/locations")
async def locations_index(request: Request, _=Depends(require_catalogue_role("viewer"))):
    with get_db() as db:
        context = _context(db)
    return request.app.state.templates.TemplateResponse(request, "locations.html", context)


@router.get("/locations/{location_id}")
async def location_detail(request: Request, location_id: int, error: str = Query(""),
                          _=Depends(require_catalogue_role("viewer"))):
    with get_db() as db:
        selected = db.execute("SELECT * FROM location_nodes WHERE id = ?", (location_id,)).fetchone()
        if selected is None:
            return _redirect(error="missing")
        context = _context(db, selected, error=error)
    return request.app.state.templates.TemplateResponse(request, "locations.html", context)


@router.post("/api/location-tree")
async def create_location_node(name: str = Form(...), parent_id: str = Form(""),
                               sort_order: int = Form(0), _=Depends(require_role("admin"))):
    try:
        parent = _parse_parent(parent_id)
    except ValueError:
        return _redirect(error="parent_missing")
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            node_id = locations.create_location(db, name, parent_id=parent, sort_order=sort_order)
    except locations.LocationNotFound:
        return _redirect(error="parent_missing")
    except locations.DuplicateLocation:
        return _redirect(parent, error="duplicate")
    except locations.LocationError:
        return _redirect(parent, error="blank")
    return _redirect(node_id)


@router.post("/api/location-tree/{location_id}/update")
async def update_location_node(location_id: int, name: str = Form(...),
                               parent_id: str = Form(""), sort_order: int = Form(0),
                               _=Depends(require_role("admin"))):
    try:
        parent = _parse_parent(parent_id)
    except ValueError:
        return _redirect(location_id, error="parent_missing")
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            locations.update_location(db, location_id, name, parent_id=parent, sort_order=sort_order)
    except locations.InvalidLocationParent:
        return _redirect(location_id, error="cycle")
    except locations.LocationNotFound:
        return _redirect(location_id, error="missing")
    except locations.DuplicateLocation:
        return _redirect(location_id, error="duplicate")
    except locations.LocationError:
        return _redirect(location_id, error="blank")
    return _redirect(location_id)


@router.post("/api/location-tree/{location_id}/delete")
async def delete_location_node(location_id: int, _=Depends(require_role("admin"))):
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT parent_id FROM location_nodes WHERE id = ?", (location_id,)).fetchone()
            if row is None:
                return _redirect(error="missing")
            parent = row["parent_id"]
            locations.delete_location(db, location_id)
    except locations.LocationHasChildren:
        return _redirect(location_id, error="has_children")
    except locations.LocationHasCopies:
        return _redirect(location_id, error="has_copies")
    return _redirect(parent)


@router.post("/api/location-tree/{location_id}/order")
async def reorder_location_copies(location_id: int, copy_ids: str = Form(...),
                                  _=Depends(require_catalogue_role("editor"))):
    try:
        ids = [int(value) for value in copy_ids.split(",") if value.strip()]
    except ValueError:
        return JSONResponse({"ok": False, "message": "Invalid copy order"}, status_code=400)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM location_nodes WHERE id = ?", (location_id,)).fetchone():
            return JSONResponse({"ok": False, "message": "Location not found"}, status_code=404)
        try:
            tree.apply_copy_order(db, location_id, ids)
        except ValueError as exc:
            db.rollback()
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
    return {"ok": True, "copy_ids": ids}


@router.post("/api/location-tree/{location_id}/auto-order")
async def auto_order_location_copies(location_id: int, sort_key: str = Form(...),
                                    _=Depends(require_catalogue_role("editor"))):
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            tree.validated_node_id(db, location_id)
            tree.auto_order_copies(db, location_id, sort_key)
    except ValueError:
        return _redirect(location_id, error="sort")
    return _redirect(location_id)


@router.post("/api/location-tree/copies/{copy_id}/move")
async def move_copy(copy_id: int, location_id: int = Form(...), _=Depends(require_catalogue_role("editor"))):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        copy = db.execute("SELECT * FROM copies_live WHERE id = ?", (copy_id,)).fetchone()
        if copy is None:
            return _redirect(location_id, error="copy_missing")
        require_item_role(db, copy["item_id"], "editor")
        try:
            item_copies.relocate_copy(db, copy, location_id)
        except ValueError:
            db.rollback()
            return _redirect(location_id, error="missing")
    return _redirect(location_id)
