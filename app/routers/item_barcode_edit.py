"""Barcode-aware wrapper for the general item edit form.

The long-standing item update endpoint owns validation and persistence for the
normal metadata fields.  This focused extension adds the generic UPC/EAN field
without duplicating that handler: it validates the barcode first, delegates the
rest of the edit, then persists the barcode only when the delegated edit
succeeds.
"""

import sqlite3

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import require_role
from app.database import get_db
from app.routers import items
from app.services import upc as upc_svc


def _canonical_upc(value: str | None) -> tuple[bool, str | None]:
    """Return (valid, storage value) for an editable UPC/EAN field."""
    if value is None:
        return True, None
    raw = str(value).strip()
    if not raw:
        return True, None
    code = upc_svc.normalize_barcode(raw)
    # Generic item UPC storage is the carrier itself. Periodical 2/5-digit
    # extensions belong to periodical_issues and must not be folded into this
    # field.
    if len(code) not in (12, 13) or upc_svc.detect_barcode_type(code) != "upc":
        return False, None
    return True, upc_svc.normalize_upc(code)


@items.router.post("/items/{item_id}/edit")
async def update_item_with_barcode(
    request: Request,
    item_id: int,
    _=Depends(require_role("editor")),
):
    """Save normal item edits plus an optional manually entered/scanned UPC/EAN."""
    form = await request.form()
    if "upc" not in form:
        return await items.update_item(request, item_id, _)

    valid, canonical_upc = _canonical_upc(form.get("upc"))
    if not valid:
        return HTMLResponse("Invalid UPC / EAN barcode", status_code=400)

    with get_db() as db:
        current = db.execute(
            "SELECT media_type FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not current:
            return HTMLResponse("Not found", status_code=404)
        effective_media_type = str(form.get("media_type") or current["media_type"])
        if canonical_upc:
            conflict = db.execute(
                "SELECT id FROM items WHERE upc = ? AND media_type = ? AND id != ? LIMIT 1",
                (canonical_upc, effective_media_type, item_id),
            ).fetchone()
            if conflict:
                return HTMLResponse(
                    "Update conflicts with existing catalogue data", status_code=409
                )

    # Request.form() is cached by Starlette, so the original handler can read
    # the same multipart submission (including cover uploads) normally.
    response = await items.update_item(request, item_id, _)
    if response.status_code not in (302, 303, 307, 308):
        return response

    try:
        with get_db() as db:
            db.execute(
                "UPDATE items SET upc = ?, updated_at = datetime('now') WHERE id = ?",
                (canonical_upc, item_id),
            )
    except sqlite3.IntegrityError:
        # The preflight above catches normal conflicts. Keep the database
        # constraint as the final guard against a concurrent edit racing us.
        return HTMLResponse(
            "Update conflicts with existing catalogue data", status_code=409
        )

    return response
