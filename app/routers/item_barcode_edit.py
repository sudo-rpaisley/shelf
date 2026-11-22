"""Pure barcode adapters for the modern, atomic item edit handler.

No routes are replaced here. Magazine carriers/add-ons remain issue metadata,
not a globally unique retail item barcode.
"""

from fastapi import HTTPException

from app.config import STATUS_MEDIA_TYPES, canonical_media_type
from app.database import ensure_periodical_schema
from app.services import item_write, tags
from app.services.catalogue_security import require_item_role
from app.services import upc as upc_svc


def _canonical_upc(value):
    return upc_svc.canonical_retail_barcode(value)


def _legacy_isbn_row(db, isbn, media_type):
    """Recognize old book aliases without changing their stored identities.

    Deliberate physical collision read: Trash and hidden rows still hold their
    identifiers. Do not pick one of multiple old/new twins or reveal a hidden
    row's title/id. Callers hold their write lock before this preflight.
    """
    if not isbn or canonical_media_type(media_type) != "book":
        return None
    rows = db.execute(
        "SELECT id, media_type, deleted_at FROM items "
        "WHERE isbn = ? AND media_type IN ('book', 'kids_book') ORDER BY id", (isbn,),
    ).fetchall()
    for row in rows:
        try:
            require_item_role(db, row["id"], "viewer", include_deleted=True)
        except HTTPException:
            raise item_write.IdentifierUnavailable("This identifier is unavailable") from None
    if len(rows) > 1:
        raise item_write.IdentifierUnavailable("This identifier is unavailable")
    return rows[0] if rows and rows[0]["media_type"] == "kids_book" else None


def find_duplicate_item(db, isbn, upc, media_type):
    legacy = _legacy_isbn_row(db, isbn, media_type)
    if isbn:
        row = db.execute(
            "SELECT id, title FROM items_live WHERE isbn = ? AND media_type = ?",
            (isbn, media_type),
        ).fetchone()
        if row:
            return dict(row)
        if legacy and legacy["deleted_at"] is None:
            row = db.execute("SELECT id, title FROM items_live WHERE id = ?", (legacy["id"],)).fetchone()
            return dict(row) if row else None
    if upc:
        row = db.execute(
            "SELECT id, title FROM items_live WHERE upc = ? AND media_type = ?", (upc, media_type),
        ).fetchone()
        if row:
            return dict(row)
        # Compatibility with pre-#20 retail codes filed in the ISBN column.
        row = db.execute(
            "SELECT id, title FROM items_live WHERE isbn = ? AND media_type = ?", (upc, media_type),
        ).fetchone()
        if row:
            return dict(row)
    return None


def restore_legacy_book(db, isbn, media_type, mode, reading_status=""):
    """Adapt only alias identity; native lifecycle/ownership/defaults still win."""
    row = _legacy_isbn_row(db, isbn, media_type)
    if row is None or row["deleted_at"] is None:
        return None
    require_item_role(db, row["id"], "editor", include_deleted=True)
    values, wishlisted = item_write.restore_intent(
        {"owned": int(mode != "wishlist"), "wishlisted": mode == "wishlist"})
    item_write.restore_item(db, row["id"])
    item_write.apply_restored_ownership(db, row["id"], values, wishlisted)
    tags.attach_pending(db, row["id"])
    current = db.execute("SELECT reading_status FROM items_live WHERE id = ?", (row["id"],)).fetchone()
    if reading_status and current["reading_status"] is None and canonical_media_type(media_type) in STATUS_MEDIA_TYPES:
        item_write.update_item_fields(db, row["id"], {"reading_status": reading_status})
    return item_write.ItemId(row["id"], restored=True)


def _valid_ean13(code: str) -> bool:
    if len(code) != 13 or not code.isascii() or not code.isdigit():
        return False
    total = sum(int(digit) * (3 if index % 2 else 1) for index, digit in enumerate(code[:12]))
    return (10 - total % 10) % 10 == int(code[-1])


def _canonical_periodical_barcode(carrier_value, supplement_value):
    carrier = upc_svc.normalize_barcode(str(carrier_value or ""))
    supplement = upc_svc.normalize_barcode(str(supplement_value or ""))
    if not carrier and not supplement:
        return True, None, None, None
    if not carrier:
        return False, None, None, "A magazine barcode carrier is required"
    embedded = None
    if len(carrier) in (14, 17):
        embedded, carrier = carrier[12:], carrier[:12]
    elif len(carrier) in (15, 18):
        embedded, carrier = carrier[13:], carrier[:13]
    if embedded:
        if supplement and supplement != embedded:
            return False, None, None, "Embedded barcode add-on does not match the add-on field"
        supplement = embedded
    if supplement and (len(supplement) not in (2, 5) or not supplement.isascii() or not supplement.isdigit()):
        return False, None, None, "Magazine barcode add-on must be 2 or 5 digits"
    if len(carrier) == 12:
        if not upc_svc.validate_upc(carrier):
            return False, None, None, "Invalid magazine UPC barcode"
        carrier = upc_svc.normalize_upc(carrier)
    elif not _valid_ean13(carrier):
        return False, None, None, "Invalid magazine EAN barcode"
    return True, carrier, supplement or None, None


def prepare_periodical_update(db, item_id, form):
    """Validate without writing. Called after the parent is locked/authorized."""
    names = {"magazine_barcode_ean", "magazine_barcode_supplement"}
    if not names.intersection(form.keys()):
        return None
    ensure_periodical_schema(db)
    issue = db.execute(
        "SELECT barcode_ean, barcode_supplement FROM periodical_issues WHERE item_id = ?", (item_id,),
    ).fetchone()
    if issue is None:
        raise LookupError("Magazine issue record not found")
    valid, carrier, supplement, message = _canonical_periodical_barcode(
        form.get("magazine_barcode_ean") if "magazine_barcode_ean" in form else issue["barcode_ean"],
        form.get("magazine_barcode_supplement") if "magazine_barcode_supplement" in form else issue["barcode_supplement"],
    )
    if not valid:
        raise ValueError(message)
    return carrier, supplement
