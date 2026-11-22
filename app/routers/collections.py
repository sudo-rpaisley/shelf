"""Shared, user-curated collections.

Collections are deliberately different from Series (ordered publication
membership) and Tags (lightweight labels). A collection is an intentional
library grouping such as favourites, a project, a course list, or a theme.
"""

import re
import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_role
from app.database import get_db
from app.services import catalogue_security, item_write, libraries
from app.services.catalogue_security import require_catalogue_role, require_item_role

router = APIRouter()

MAX_COLLECTION_NAME = 100
MAX_COLLECTION_DESCRIPTION = 500


def normalize_collection_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()[:MAX_COLLECTION_NAME]


def normalize_collection_description(description: str | None) -> str | None:
    value = re.sub(r"\s+", " ", description or "").strip()[:MAX_COLLECTION_DESCRIPTION]
    return value or None


def get_all_collections(db) -> list:
    """Definitions are shared; counts include only visible, live members."""
    return db.execute(
        "SELECT c.id, c.name, c.description, c.created_at, c.updated_at, "
        "COUNT(i.id) AS item_count "
        "FROM collections c "
        "LEFT JOIN collection_items ci ON ci.collection_id = c.id "
        "LEFT JOIN items_live i ON i.id = ci.item_id "
        "GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
    ).fetchall()


def get_collection_cards(db) -> list[dict]:
    cards = []
    user = catalogue_security.current_actor()
    can_edit_definitions = bool(user) and any(
        libraries.has_library_role(db, user, library_id, "editor")
        for library_id in libraries.accessible_library_ids(db, user)
    )
    for row in get_all_collections(db):
        card = dict(row)
        card["preview_items"] = db.execute(
            "SELECT i.id, i.title, i.cover_path, i.media_type "
            "FROM collection_items ci JOIN items_live i ON i.id = ci.item_id "
            "WHERE ci.collection_id = ? "
            "ORDER BY ci.created_at DESC, i.id DESC LIMIT 4",
            (row["id"],),
        ).fetchall()
        card["can_edit"] = can_edit_definitions and all(
            libraries.has_item_role(db, user, child["item_id"], "editor")
            for child in db.execute(
                "SELECT item_id FROM collection_items WHERE collection_id = ?", (row["id"],)
            )
        )
        cards.append(card)
    return cards


def get_item_collections(db, item_id: int) -> list:
    return db.execute(
        "SELECT c.id, c.name, c.description FROM collection_items ci "
        "JOIN collections c ON c.id = ci.collection_id "
        "JOIN items_live i ON i.id = ci.item_id "
        "WHERE ci.item_id = ? ORDER BY c.name COLLATE NOCASE",
        (item_id,),
    ).fetchall()


def item_collection_context(db, item_id: int) -> dict:
    current = get_item_collections(db, item_id)
    current_ids = {row["id"] for row in current}
    available = [row for row in get_all_collections(db) if row["id"] not in current_ids]
    user = catalogue_security.current_actor()
    return {
        "item_collections": current, "available_collections": available,
        "can_edit_item": bool(user) and libraries.has_item_role(db, user, item_id, "editor"),
    }


def _require_collection_editor(db, collection_id: int) -> None:
    """A shared rename/delete affects EVERY attachment, including Trash.

    Empty definitions may be managed by any catalogue editor. Definitions have
    no per-library owner; populated ones require authority over all children,
    never a global-editor shortcut or a successful partial detach.
    """
    if not db.execute("SELECT 1 FROM collections WHERE id = ?", (collection_id,)).fetchone():
        raise HTTPException(404, "Collection not found")
    for row in db.execute(
        "SELECT item_id FROM collection_items WHERE collection_id = ? ORDER BY item_id",
        (collection_id,),
    ).fetchall():
        require_item_role(db, row["item_id"], "editor", include_deleted=True)


def _render_item_collections(request: Request, db, item_id: int):
    context = item_collection_context(db, item_id)
    context["item_id"] = item_id
    return request.app.state.templates.TemplateResponse(
        request, "fragments/item_collections.html", context,
    )


@router.get("/collections")
async def collections_page(request: Request, _=Depends(require_role("viewer"))):
    with get_db() as db:
        cards = get_collection_cards(db)
    return request.app.state.templates.TemplateResponse(
        request, "collections.html", {"collections": cards},
    )


@router.post("/api/collections")
async def create_collection(
    name: str = Form(...),
    description: str = Form(""),
    _=Depends(require_catalogue_role("editor")),
):
    clean_name = normalize_collection_name(name)
    if not clean_name:
        return HTMLResponse("Collection name required", status_code=400)
    clean_description = normalize_collection_description(description)
    try:
        with get_db() as db, item_write._write_scope(db):
            db.execute(
                "INSERT INTO collections (name, description) VALUES (?, ?)",
                (clean_name, clean_description),
            )
    except sqlite3.IntegrityError:
        return HTMLResponse("A collection with that name already exists", status_code=409)
    return RedirectResponse(url="/collections", status_code=303)


@router.post("/api/collections/{collection_id}")
async def update_collection(
    collection_id: int,
    name: str = Form(...),
    description: str = Form(""),
    _=Depends(require_catalogue_role("editor")),
):
    clean_name = normalize_collection_name(name)
    if not clean_name:
        return HTMLResponse("Collection name required", status_code=400)
    clean_description = normalize_collection_description(description)
    try:
        with get_db() as db, item_write._write_scope(db):
            _require_collection_editor(db, collection_id)
            result = db.execute(
                "UPDATE collections SET name = ?, description = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (clean_name, clean_description, collection_id),
            )
            if result.rowcount != 1:
                return HTMLResponse("Collection not found", status_code=404)
    except sqlite3.IntegrityError:
        return HTMLResponse("A collection with that name already exists", status_code=409)
    return RedirectResponse(url="/collections", status_code=303)


@router.delete("/api/collections/{collection_id}")
async def delete_collection(
    collection_id: int,
    _=Depends(require_catalogue_role("editor")),
):
    with get_db() as db, item_write._write_scope(db):
        _require_collection_editor(db, collection_id)
        result = db.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
        if result.rowcount != 1:
            return HTMLResponse("Collection not found", status_code=404)
    return HTMLResponse("")


@router.post("/api/items/{item_id}/collections")
async def add_item_to_collection(
    request: Request,
    item_id: int,
    collection_id: int = Form(...),
    _=Depends(require_catalogue_role("editor")),
):
    with get_db() as db, item_write._write_scope(db):
        require_item_role(db, item_id, "editor")
        collection = db.execute(
            "SELECT id FROM collections WHERE id = ?", (collection_id,)
        ).fetchone()
        if not collection:
            return HTMLResponse("Collection not found", status_code=404)
        db.execute(
            "INSERT OR IGNORE INTO collection_items (collection_id, item_id) VALUES (?, ?)",
            (collection_id, item_id),
        )
        return _render_item_collections(request, db, item_id)


@router.delete("/api/items/{item_id}/collections/{collection_id}")
async def remove_item_from_collection(
    request: Request,
    item_id: int,
    collection_id: int,
    _=Depends(require_catalogue_role("editor")),
):
    with get_db() as db, item_write._write_scope(db):
        require_item_role(db, item_id, "editor")
        result = db.execute(
            "DELETE FROM collection_items WHERE collection_id = ? AND item_id = ?",
            (collection_id, item_id),
        )
        if result.rowcount != 1:
            return HTMLResponse("Collection membership not found", status_code=404)
        return _render_item_collections(request, db, item_id)
