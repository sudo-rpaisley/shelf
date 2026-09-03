"""Hierarchical location browsing, administration and shelf ordering."""

import sqlite3

from fastapi import Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.routers import pages
from app.services import holdings
from app.services import location_tree as location_svc


def _redirect(location_id: int | None = None, *, error: str = "") -> RedirectResponse:
    target = f"/locations/{location_id}" if location_id else "/locations"
    if error:
        target += f"?error={error}"
    return RedirectResponse(url=target, status_code=303)


def _parent_exists(db, parent_id: int | None) -> bool:
    if parent_id is None:
        return True
    return db.execute(
        "SELECT 1 FROM location_nodes WHERE id = ?", (parent_id,)
    ).fetchone() is not None


def _parse_parent(value: str | int | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(value)


def _prepare(db) -> None:
    """Install/backfill the additive model before a location operation."""
    holdings.ensure_foundation(db)


@pages.router.get("/locations")
async def locations_index(request: Request, _=Depends(require_role("viewer"))):
    with get_db() as db:
        _prepare(db)
        nodes = location_svc.flattened_tree(db)
    return request.app.state.templates.TemplateResponse(
        request,
        "locations.html",
        {"location_nodes": nodes, "selected_location": None, "error": ""},
    )


@pages.router.get("/locations/{location_id}")
async def location_detail(
    request: Request,
    location_id: int,
    error: str = Query(""),
    _=Depends(require_role("viewer")),
):
    with get_db() as db:
        _prepare(db)
        selected = db.execute(
            "SELECT * FROM location_nodes WHERE id = ?", (location_id,)
        ).fetchone()
        if not selected:
            return _redirect(error="missing")
        nodes = location_svc.flattened_tree(db)
        breadcrumb = location_svc.location_path(db, location_id)
        children = db.execute(
            "SELECT id, name, sort_order FROM location_nodes WHERE parent_id = ? "
            "ORDER BY sort_order, name COLLATE NOCASE",
            (location_id,),
        ).fetchall()
        copies = location_svc.direct_copies(db, location_id)

    return request.app.state.templates.TemplateResponse(
        request,
        "locations.html",
        {
            "location_nodes": nodes,
            "selected_location": dict(selected),
            "breadcrumb": breadcrumb,
            "child_locations": children,
            "location_copies": copies,
            "error": error,
        },
    )


@pages.router.post("/api/location-tree")
async def create_location_node(
    name: str = Form(...),
    parent_id: str = Form(""),
    sort_order: int = Form(0),
    _=Depends(require_role("admin")),
):
    clean_name = name.strip()
    try:
        parent = _parse_parent(parent_id)
    except ValueError:
        return _redirect(error="parent_missing")
    if not clean_name:
        return _redirect(parent, error="blank")
    try:
        with get_db() as db:
            _prepare(db)
            if not _parent_exists(db, parent):
                return _redirect(error="parent_missing")
            cursor = db.execute(
                "INSERT INTO location_nodes (parent_id, name, sort_order) VALUES (?, ?, ?)",
                (parent, clean_name, sort_order),
            )
            new_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        return _redirect(parent, error="duplicate")
    return _redirect(new_id)


@pages.router.post("/api/location-tree/{location_id}/update")
async def update_location_node(
    location_id: int,
    name: str = Form(...),
    parent_id: str = Form(""),
    sort_order: int = Form(0),
    _=Depends(require_role("admin")),
):
    clean_name = name.strip()
    try:
        parent = _parse_parent(parent_id)
    except ValueError:
        return _redirect(location_id, error="parent_missing")
    if not clean_name:
        return _redirect(location_id, error="blank")
    if parent == location_id:
        return _redirect(location_id, error="cycle")

    try:
        with get_db() as db:
            _prepare(db)
            node = db.execute(
                "SELECT id, legacy_location_id FROM location_nodes WHERE id = ?",
                (location_id,),
            ).fetchone()
            if not node:
                return _redirect(error="missing")
            if not _parent_exists(db, parent):
                return _redirect(location_id, error="parent_missing")
            if parent is not None and parent in location_svc.descendant_ids(
                db, location_id, include_self=False
            ):
                return _redirect(location_id, error="cycle")

            # Keep the legacy flat location truthful while old scan/edit forms
            # still read it. New child-only nodes have no legacy counterpart.
            if node["legacy_location_id"] is not None:
                db.execute(
                    "UPDATE locations SET name = ?, sort_order = ? WHERE id = ?",
                    (clean_name, sort_order, node["legacy_location_id"]),
                )
            db.execute(
                "UPDATE location_nodes SET parent_id = ?, name = ?, sort_order = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (parent, clean_name, sort_order, location_id),
            )
    except sqlite3.IntegrityError:
        return _redirect(location_id, error="duplicate")
    return _redirect(location_id)


@pages.router.post("/api/location-tree/{location_id}/delete")
async def delete_location_node(
    location_id: int,
    _=Depends(require_role("admin")),
):
    with get_db() as db:
        _prepare(db)
        node = db.execute(
            "SELECT id, parent_id, legacy_location_id FROM location_nodes WHERE id = ?",
            (location_id,),
        ).fetchone()
        if not node:
            return _redirect(error="missing")
        if db.execute(
            "SELECT 1 FROM location_nodes WHERE parent_id = ? LIMIT 1", (location_id,)
        ).fetchone():
            return _redirect(location_id, error="has_children")
        if db.execute(
            "SELECT 1 FROM item_copies WHERE location_id = ? LIMIT 1", (location_id,)
        ).fetchone():
            return _redirect(location_id, error="has_copies")

        if node["legacy_location_id"] is not None:
            legacy_id = node["legacy_location_id"]
            # Clear stale/wishlist/digital references before deleting the flat
            # compatibility record. Owned physical copies were guarded above.
            db.execute("UPDATE items SET location_id = NULL WHERE location_id = ?", (legacy_id,))
            db.execute("DELETE FROM locations WHERE id = ?", (legacy_id,))
        db.execute("DELETE FROM location_nodes WHERE id = ?", (location_id,))
        parent = node["parent_id"]
    return _redirect(parent)


@pages.router.post("/api/location-tree/{location_id}/order")
async def reorder_location_copies(
    location_id: int,
    copy_ids: str = Form(...),
    _=Depends(require_role("editor")),
):
    try:
        ids = [int(value) for value in copy_ids.split(",") if value.strip()]
    except ValueError:
        return JSONResponse({"ok": False, "message": "Invalid copy order"}, status_code=400)

    with get_db() as db:
        _prepare(db)
        if not db.execute(
            "SELECT 1 FROM location_nodes WHERE id = ?", (location_id,)
        ).fetchone():
            return JSONResponse({"ok": False, "message": "Location not found"}, status_code=404)
        try:
            location_svc.apply_copy_order(db, location_id, ids)
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
    return {"ok": True, "copy_ids": ids}


@pages.router.post("/api/location-tree/{location_id}/auto-order")
async def auto_order_location_copies(
    location_id: int,
    sort_key: str = Form(...),
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        _prepare(db)
        if not db.execute(
            "SELECT 1 FROM location_nodes WHERE id = ?", (location_id,)
        ).fetchone():
            return _redirect(error="missing")
        try:
            location_svc.auto_order_copies(db, location_id, sort_key)
        except ValueError:
            return _redirect(location_id, error="sort")
    return _redirect(location_id)


@pages.router.post("/api/location-tree/copies/{copy_id}/move")
async def move_copy(
    copy_id: int,
    location_id: int = Form(...),
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        _prepare(db)
        if not db.execute(
            "SELECT 1 FROM location_nodes WHERE id = ?", (location_id,)
        ).fetchone():
            return _redirect(error="missing")
        copy = db.execute(
            "SELECT item_id, is_primary FROM item_copies WHERE id = ?", (copy_id,)
        ).fetchone()
        if not copy:
            return _redirect(location_id, error="copy_missing")

        # The legacy field can name only the nearest flat/root location. The
        # copy row remains authoritative for the precise shelf/sub-location.
        if copy["is_primary"]:
            legacy = location_svc.nearest_legacy_location(db, location_id)
            db.execute(
                "UPDATE items SET location_id = ? WHERE id = ?",
                (legacy, copy["item_id"]),
            )
        db.execute(
            "UPDATE item_copies SET location_id = ?, position_order = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (location_id, copy_id),
        )
    return _redirect(location_id)
