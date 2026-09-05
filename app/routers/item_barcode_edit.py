"""Barcode-aware extensions for the general item edit form.

The long-standing item update endpoint owns validation and persistence for the
normal metadata fields. This focused extension adds editable/scannable barcode
identity without duplicating that handler: it validates barcode changes first,
delegates the rest of the edit, then persists barcode fields only when the
delegated edit succeeds.

Magazine issue identity deliberately remains in ``periodical_issues`` rather
than being copied into the globally unique ``items.upc`` field.
"""

import sqlite3

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import nav
from app.auth import require_role
from app.config import MEDIA_TYPES
from app.database import get_db, get_game_platforms
from app.routers import items, pages
from app.services import periodical_records
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


def _valid_ean13(code: str) -> bool:
    if len(code) != 13 or not code.isdigit():
        return False
    total = sum(
        int(digit) * (3 if index % 2 else 1)
        for index, digit in enumerate(code[:12])
    )
    return (10 - (total % 10)) % 10 == int(code[-1])


def _canonical_periodical_barcode(
    carrier_value: str | None,
    supplement_value: str | None,
) -> tuple[bool, str | None, str | None, str | None]:
    """Validate and split an editable magazine carrier/add-on pair.

    The carrier field may contain just UPC-A/EAN-13 or the full scanned value
    including a 2/5-digit add-on. UPC-A is canonicalised to EAN-13 for storage.
    The add-on remains verbatim digits; Shelf does not infer issue semantics.
    """
    carrier = upc_svc.normalize_barcode(str(carrier_value or ""))
    supplement = upc_svc.normalize_barcode(str(supplement_value or ""))

    if not carrier and not supplement:
        return True, None, None, None
    if not carrier:
        return False, None, None, "A magazine barcode carrier is required"

    embedded_supplement = None
    if len(carrier) in (14, 17):
        embedded_supplement = carrier[12:]
        carrier = carrier[:12]
    elif len(carrier) in (15, 18):
        embedded_supplement = carrier[13:]
        carrier = carrier[:13]

    if embedded_supplement:
        if supplement and supplement != embedded_supplement:
            return (
                False,
                None,
                None,
                "Embedded barcode add-on does not match the add-on field",
            )
        supplement = embedded_supplement

    if supplement and len(supplement) not in (2, 5):
        return False, None, None, "Magazine barcode add-on must be 2 or 5 digits"

    if len(carrier) == 12:
        if not upc_svc.validate_upc(carrier):
            return False, None, None, "Invalid magazine UPC barcode"
        carrier = upc_svc.normalize_upc(carrier)
    elif len(carrier) == 13:
        if not _valid_ean13(carrier):
            return False, None, None, "Invalid magazine EAN barcode"
    else:
        return False, None, None, "Magazine barcode must be UPC-A or EAN-13"

    return True, carrier, supplement or None, None


# Replace the original GET route with the same edit page plus periodical issue
# identity. This module is imported after pages.py has registered its routes.
pages.router.routes[:] = [
    route
    for route in pages.router.routes
    if not (
        getattr(route, "path", None) == "/item/{item_id}/edit"
        and "GET" in (getattr(route, "methods", None) or set())
    )
]


@pages.router.get("/item/{item_id}/edit")
async def item_edit_with_barcode_context(
    request: Request,
    item_id: int,
    from_: str = Query("", alias="from"),
    _=Depends(require_role("editor")),
):
    """Render item editing with magazine issue barcode identity when present."""
    back = nav.back_target(from_)
    periodical_issue = None
    with get_db() as db:
        item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        locations = db.execute(
            "SELECT * FROM locations ORDER BY sort_order, name"
        ).fetchall()
        game_platforms = get_game_platforms(db)
        if item and item["media_type"] == "magazine":
            periodical_records.ensure_extended_schema(db)
            periodical_issue = db.execute(
                "SELECT barcode_ean, barcode_supplement FROM periodical_issues "
                "WHERE item_id = ?",
                (item_id,),
            ).fetchone()

    if not item:
        return RedirectResponse(url="/browse")
    return request.app.state.templates.TemplateResponse(
        request,
        "item_edit.html",
        {
            "item": item,
            "back": back,
            "media_types": MEDIA_TYPES,
            "game_platforms": game_platforms,
            "locations": locations,
            "periodical_issue": periodical_issue,
        },
    )


@items.router.post("/items/{item_id}/edit")
async def update_item_with_barcode(
    request: Request,
    item_id: int,
    _=Depends(require_role("editor")),
):
    """Save normal item edits plus generic or magazine barcode identity."""
    form = await request.form()
    has_upc = "upc" in form
    has_periodical = (
        "magazine_barcode_ean" in form or "magazine_barcode_supplement" in form
    )
    if not has_upc and not has_periodical:
        return await items.update_item(request, item_id, _)

    canonical_upc = None
    if has_upc:
        valid, canonical_upc = _canonical_upc(form.get("upc"))
        if not valid:
            return HTMLResponse("Invalid UPC / EAN barcode", status_code=400)

    periodical_ean = None
    periodical_supplement = None
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

        if has_periodical:
            periodical_records.ensure_extended_schema(db)
            issue = db.execute(
                "SELECT barcode_ean, barcode_supplement FROM periodical_issues "
                "WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            if not issue:
                return HTMLResponse("Magazine issue record not found", status_code=404)

            carrier_value = (
                form.get("magazine_barcode_ean")
                if "magazine_barcode_ean" in form
                else issue["barcode_ean"]
            )
            supplement_value = (
                form.get("magazine_barcode_supplement")
                if "magazine_barcode_supplement" in form
                else issue["barcode_supplement"]
            )
            valid, periodical_ean, periodical_supplement, message = (
                _canonical_periodical_barcode(carrier_value, supplement_value)
            )
            if not valid:
                return HTMLResponse(message or "Invalid magazine barcode", status_code=400)

    # Request.form() is cached by Starlette, so the original handler can read
    # the same multipart submission (including cover uploads) normally.
    response = await items.update_item(request, item_id, _)
    if response.status_code not in (302, 303, 307, 308):
        return response

    try:
        with get_db() as db:
            if has_upc:
                db.execute(
                    "UPDATE items SET upc = ?, updated_at = datetime('now') WHERE id = ?",
                    (canonical_upc, item_id),
                )
            if has_periodical:
                db.execute(
                    "UPDATE periodical_issues SET barcode_ean = ?, "
                    "barcode_supplement = ?, updated_at = datetime('now') "
                    "WHERE item_id = ?",
                    (periodical_ean, periodical_supplement, item_id),
                )
    except sqlite3.IntegrityError:
        # The preflight above catches normal conflicts. Keep database
        # constraints as the final guard against concurrent edits racing us.
        return HTMLResponse(
            "Update conflicts with existing catalogue data", status_code=409
        )

    return response
