"""Multiple series memberships for item detail pages.

Shelf historically stores one ``series_name``/``series_position`` pair on the
item row.  This module adds a compatibility membership table so an item can
belong to more than one ordered series without breaking existing filters,
imports, series management or integrations that still read the legacy pair.

The legacy pair is the primary compatibility projection. Central initialization
alone owns schema and one-time migration; writes maintain the projection without
lazy request-time backfills or endpoint replacement.
"""

from __future__ import annotations

from math import isfinite

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.features import require_feature
from app.config import MEDIA_FAMILIES
from app.database import get_db, ensure_series_memberships_schema
from app.routers.series import LIVE_SERIES_MEMBERSHIPS, MAX_SERIES_NAME, _require_series_role
from app.services import libraries
from app.services.catalogue_security import require_catalogue_role, require_item_role
from app.services.item_write import update_item_fields


router = APIRouter(dependencies=[Depends(require_feature("series"))])
_ensure_item_series_schema = ensure_series_memberships_schema


def _family_types(media_type: str) -> tuple[str, ...]:
    for family in MEDIA_FAMILIES.values():
        if media_type in family["types"]:
            return tuple(family["types"])
    return (media_type,)


def _parse_position(raw: str | None) -> float | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        parsed = float(value)
        if not isfinite(parsed):
            raise ValueError("Series position must be finite")
        return parsed
    except ValueError as exc:
        raise ValueError("Series position must be a number") from exc


def _item_and_series_context(db, item_id: int) -> dict | None:
    _ensure_item_series_schema(db)
    item_row = db.execute(
        "SELECT id, title, media_type, series_name, series_position FROM items_live WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not item_row:
        return None
    item = dict(item_row)

    memberships = [
        dict(row)
        for row in db.execute(
            f"SELECT series_name, position, is_primary FROM {LIVE_SERIES_MEMBERSHIPS} "
            "WHERE item_id = ? ORDER BY is_primary DESC, series_name COLLATE NOCASE",
            (item_id,),
        ).fetchall()
    ]

    family_types = _family_types(item["media_type"])
    placeholders = ",".join("?" for _ in family_types)
    rows = []
    for membership in memberships:
        members = [
            dict(row)
            for row in db.execute(
                "SELECT i.id, i.title, i.authors, i.cover_path, i.media_type, "
                f"i.publish_year, s.position FROM {LIVE_SERIES_MEMBERSHIPS} s "
                "JOIN items_live i ON i.id = s.item_id "
                "WHERE s.series_name = ? COLLATE NOCASE "
                f"AND i.media_type IN ({placeholders}) "
                "ORDER BY s.position IS NULL, s.position, i.publish_year IS NULL, "
                "i.publish_year, i.title COLLATE NOCASE, i.id",
                (membership["series_name"], *family_types),
            ).fetchall()
        ]
        # A series row is navigation, not metadata.  A one-item series has
        # nowhere to navigate, so keep it out of the visual rows as requested.
        if len(members) > 1:
            rows.append(
                {
                    "name": membership["series_name"],
                    "position": membership["position"],
                    "items": members,
                    "count": len(members),
                }
            )

    known_names = [
        row["series_name"]
        for row in db.execute(
            f"SELECT s.series_name FROM {LIVE_SERIES_MEMBERSHIPS} s "
            "GROUP BY s.series_name COLLATE NOCASE ORDER BY s.series_name COLLATE NOCASE"
        ).fetchall()
    ]

    return {
        "item": item,
        "series_rows": rows,
        "series_memberships": memberships,
        "known_series_names": known_names,
    }


def _render_panel(request: Request, item_id: int):
    with get_db() as db:
        require_item_role(db, item_id, "viewer")
        context = _item_and_series_context(db, item_id)
        if context:
            context["can_edit"] = libraries.has_item_role(db, dict(request.state.user), item_id, "editor")
    if not context:
        return HTMLResponse("Item not found", status_code=404)
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/item_series_rows.html",
        context,
    )


@router.get("/api/items/{item_id}/series-rows")
async def item_series_rows(
    request: Request,
    item_id: int,
    _=Depends(require_catalogue_role("viewer")),
):
    return _render_panel(request, item_id)


@router.post("/api/items/{item_id}/series-memberships")
async def upsert_item_series_membership(
    request: Request,
    item_id: int,
    series_name: str = Form(...),
    position: str = Form(""),
    _=Depends(require_catalogue_role("editor")),
):
    name = (series_name or "").strip()
    if not name:
        return HTMLResponse("Series name is required", status_code=400)
    if len(name) > MAX_SERIES_NAME:
        return HTMLResponse("Series name is too long", status_code=400)
    try:
        parsed_position = _parse_position(position)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=400)

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        require_item_role(db, item_id, "editor")
        _require_series_role(db, name)
        _ensure_item_series_schema(db)
        item = db.execute(
            "SELECT id, series_name FROM items_live WHERE id = ?", (item_id,)
        ).fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")

        existing = db.execute(
            "SELECT is_primary FROM item_series "
            "WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
            (item_id, name),
        ).fetchone()
        is_primary = int(existing["is_primary"]) if existing else 0

        # The first membership becomes the compatibility/primary series so
        # existing Browse filters, integrations and /series keep working.
        legacy_name = (item["series_name"] or "").strip()
        if not legacy_name:
            is_primary = 1
            update_item_fields(
                db,
                item_id,
                {"series_name": name, "series_position": parsed_position},
            )
        elif legacy_name.casefold() == name.casefold():
            is_primary = 1
            update_item_fields(db, item_id, {"series_position": parsed_position})

        db.execute(
            "INSERT INTO item_series (item_id, series_name, position, is_primary) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(item_id, series_name) DO UPDATE SET "
            "position = excluded.position, is_primary = excluded.is_primary",
            (item_id, name, parsed_position, is_primary),
        )

    return _render_panel(request, item_id)


@router.post("/api/items/{item_id}/series-memberships/remove")
async def remove_item_series_membership(
    request: Request,
    item_id: int,
    series_name: str = Form(...),
    _=Depends(require_catalogue_role("editor")),
):
    name = (series_name or "").strip()
    if not name:
        return HTMLResponse("Series name is required", status_code=400)

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        require_item_role(db, item_id, "editor")
        _require_series_role(db, name)
        _ensure_item_series_schema(db)
        membership = db.execute(
            f"SELECT is_primary FROM {LIVE_SERIES_MEMBERSHIPS} "
            "WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
            (item_id, name),
        ).fetchone()
        if not membership:
            return _render_panel(request, item_id)

        was_primary = bool(membership["is_primary"])
        replacement = None
        if was_primary:
            replacement = db.execute(
                "SELECT series_name, position FROM item_series WHERE item_id = ? "
                "AND series_name != ? COLLATE NOCASE "
                "ORDER BY created_at, series_name COLLATE NOCASE LIMIT 1", (item_id, name),
            ).fetchone()
            if replacement:
                _require_series_role(db, replacement["series_name"])
        db.execute(
            "DELETE FROM item_series WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
            (item_id, name),
        )

        if was_primary:
            if replacement:
                db.execute(
                    "UPDATE item_series SET is_primary = CASE "
                    "WHEN series_name = ? COLLATE NOCASE THEN 1 ELSE 0 END "
                    "WHERE item_id = ?",
                    (replacement["series_name"], item_id),
                )
                update_item_fields(
                    db,
                    item_id,
                    {
                        "series_name": replacement["series_name"],
                        "series_position": replacement["position"],
                    },
                )
            else:
                update_item_fields(
                    db, item_id, {"series_name": None, "series_position": None}
                )

    return _render_panel(request, item_id)
