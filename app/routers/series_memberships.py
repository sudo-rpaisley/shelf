"""Multiple series memberships for item detail pages.

Shelf historically stores one ``series_name``/``series_position`` pair on the
item row.  This module adds a compatibility membership table so an item can
belong to more than one ordered series without breaking existing filters,
imports, series management or integrations that still read the legacy pair.

The legacy pair is treated as the primary membership and is kept in sync.  The
membership table is created/backfilled idempotently when this feature is first
used; existing libraries therefore acquire memberships without a destructive
migration or data rewrite.
"""

from __future__ import annotations

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.auth import require_role
from app.config import MEDIA_FAMILIES
from app.database import get_db
from app.routers.series import MAX_SERIES_NAME, router


_ITEM_SERIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS item_series (
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    series_name  TEXT NOT NULL COLLATE NOCASE,
    position     REAL,
    is_primary   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_id, series_name)
)
"""


def _ensure_item_series_schema(db) -> None:
    """Create/backfill the compatibility membership table idempotently.

    The reconciliation step makes edits performed through Shelf's older
    single-series field safe: the old primary membership is removed when the
    legacy field changes, while secondary memberships remain untouched.
    """
    db.execute(_ITEM_SERIES_SCHEMA)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_item_series_name "
        "ON item_series(series_name COLLATE NOCASE)"
    )
    db.execute(
        "DELETE FROM item_series WHERE is_primary = 1 AND NOT EXISTS ("
        "SELECT 1 FROM items i WHERE i.id = item_series.item_id "
        "AND i.series_name IS NOT NULL AND TRIM(i.series_name) != '' "
        "AND TRIM(i.series_name) = item_series.series_name COLLATE NOCASE"
        ")"
    )
    db.execute(
        "INSERT INTO item_series (item_id, series_name, position, is_primary) "
        "SELECT id, TRIM(series_name), series_position, 1 FROM items "
        "WHERE series_name IS NOT NULL AND TRIM(series_name) != '' "
        "ON CONFLICT(item_id, series_name) DO UPDATE SET "
        "position = excluded.position, is_primary = 1"
    )


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
        return float(value)
    except ValueError as exc:
        raise ValueError("Series position must be a number") from exc


def _item_and_series_context(db, item_id: int) -> dict | None:
    _ensure_item_series_schema(db)
    item_row = db.execute(
        "SELECT id, title, media_type, series_name, series_position FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not item_row:
        return None
    item = dict(item_row)

    memberships = [
        dict(row)
        for row in db.execute(
            "SELECT series_name, position, is_primary FROM item_series "
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
                "i.publish_year, s.position FROM item_series s "
                "JOIN items i ON i.id = s.item_id "
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
            "SELECT series_name FROM item_series GROUP BY series_name COLLATE NOCASE "
            "ORDER BY series_name COLLATE NOCASE"
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
        context = _item_and_series_context(db, item_id)
    if not context:
        return HTMLResponse("Item not found", status_code=404)
    context["can_edit"] = bool(
        request.state.user and request.state.user.role in ("admin", "editor")
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/item_series_rows.html",
        context,
    )


@router.get("/api/items/{item_id}/series-rows")
async def item_series_rows(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    return _render_panel(request, item_id)


@router.post("/api/items/{item_id}/series-memberships")
async def upsert_item_series_membership(
    request: Request,
    item_id: int,
    series_name: str = Form(...),
    position: str = Form(""),
    _=Depends(require_role("editor")),
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
        _ensure_item_series_schema(db)
        item = db.execute(
            "SELECT id, series_name FROM items WHERE id = ?", (item_id,)
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
            db.execute(
                "UPDATE items SET series_name = ?, series_position = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (name, parsed_position, item_id),
            )
        elif legacy_name.casefold() == name.casefold():
            is_primary = 1
            db.execute(
                "UPDATE items SET series_position = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (parsed_position, item_id),
            )

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
    _=Depends(require_role("editor")),
):
    name = (series_name or "").strip()
    if not name:
        return HTMLResponse("Series name is required", status_code=400)

    with get_db() as db:
        _ensure_item_series_schema(db)
        membership = db.execute(
            "SELECT is_primary FROM item_series "
            "WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
            (item_id, name),
        ).fetchone()
        if not membership:
            return _render_panel(request, item_id)

        was_primary = bool(membership["is_primary"])
        db.execute(
            "DELETE FROM item_series WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
            (item_id, name),
        )

        if was_primary:
            replacement = db.execute(
                "SELECT series_name, position FROM item_series WHERE item_id = ? "
                "ORDER BY created_at, series_name COLLATE NOCASE LIMIT 1",
                (item_id,),
            ).fetchone()
            if replacement:
                db.execute(
                    "UPDATE item_series SET is_primary = CASE "
                    "WHEN series_name = ? COLLATE NOCASE THEN 1 ELSE 0 END "
                    "WHERE item_id = ?",
                    (replacement["series_name"], item_id),
                )
                db.execute(
                    "UPDATE items SET series_name = ?, series_position = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (replacement["series_name"], replacement["position"], item_id),
                )
            else:
                db.execute(
                    "UPDATE items SET series_name = NULL, series_position = NULL, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (item_id,),
                )

    return _render_panel(request, item_id)
