import sqlite3
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import features, i18n
from app.auth import require_role
from app.database import get_db, get_setting
from app.features import require_feature
from app.services.catalogue_security import require_item_role, require_catalogue_role
from app.routers import items_common

router = APIRouter(prefix="/api")

DEFAULT_OVERDUE_DAYS = 28


def _scan_loan(request, templates, item, raw, *, borrower_id=None, returning=False):
    """Native scanner loan protocol, with authorization under the write lock."""
    mode = "return" if returning else "lend"
    if not features.feature_enabled("lending"):
        return templates.TemplateResponse(request, "fragments/scan_result.html",
            {"status": "error", "isbn": raw, "message": i18n.gettext("Lending is turned off on this Shelf.")})
    if not returning and not borrower_id:
        return templates.TemplateResponse(request, "fragments/scan_result.html",
            {"status": "error", "isbn": raw, "message": i18n.gettext("No borrower selected")})
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        require_item_role(db, item["id"], "editor")
        item = dict(db.execute("SELECT * FROM items_live WHERE id = ?", (item["id"],)).fetchone())
        active = db.execute(
            "SELECT c.id, b.name FROM checkouts c JOIN borrowers b ON b.id = c.borrower_id "
            "WHERE c.item_id = ? AND c.checked_in IS NULL", (item["id"],),
        ).fetchone()
        if returning:
            status = "returned" if active else "not_checked_out"
            message = (i18n.gettext("Returned from %(name)s") % {"name": active["name"]}
                       if active else i18n.gettext("Not currently checked out"))
            if active:
                db.execute("UPDATE checkouts SET checked_in = datetime('now') WHERE id = ? AND checked_in IS NULL", (active["id"],))
        elif active:
            status = "already_checked_out"
            message = i18n.gettext("Already lent to %(name)s") % {"name": active["name"]}
        else:
            borrower = db.execute("SELECT name FROM borrowers WHERE id = ?", (borrower_id,)).fetchone()
            if borrower is None:
                return templates.TemplateResponse(request, "fragments/scan_result.html",
                    {"status": "error", "isbn": raw, "message": i18n.gettext("Borrower not found")})
            db.execute("INSERT INTO checkouts (item_id, borrower_id) VALUES (?, ?)", (item["id"], borrower_id))
            status = "checked_out"
            message = i18n.gettext("Lent to %(name)s") % {"name": borrower["name"]}
    # Logging opens another connection; never log while holding our write lock.
    items_common._log_scan(raw, item.get("media_type", ""), status, item["id"], mode)
    return templates.TemplateResponse(request, "fragments/scan_result.html",
        {"status": status, "isbn": raw, "item_id": item["id"], "title": item["title"],
         "cover_path": item.get("cover_path"), "authors": item.get("authors"),
         "media_type": item.get("media_type"), "message": message})


def scan_lend(request, templates, item, borrower_id, raw):
    return _scan_loan(request, templates, item, raw, borrower_id=borrower_id)


def scan_return(request, templates, item, raw):
    return _scan_loan(request, templates, item, raw, returning=True)

# A loan is overdue when its explicit due date has passed, or — for loans
# with no due date (e.g. created via the Lend scan mode) — when it has been
# out longer than the configured fallback window. One ? param: fallback days.
OVERDUE_CONDITION = (
    "c.checked_in IS NULL AND ("
    "  (c.due_date IS NOT NULL AND c.due_date < date('now'))"
    "  OR (c.due_date IS NULL AND julianday('now') - julianday(c.checked_out) > ?)"
    ")"
)


def get_overdue_days(db) -> int:
    """Fallback overdue window in days; 0 disables the no-due-date fallback."""
    raw = get_setting(db, "lending_overdue_days")
    try:
        days = int(raw) if raw else DEFAULT_OVERDUE_DAYS
    except ValueError:
        days = DEFAULT_OVERDUE_DAYS
    # 0 = disabled: make the fallback unreachable rather than branching SQL
    return days if days > 0 else 10**9


def get_overdue_loans(db) -> list[dict]:
    """All overdue checkouts with item and borrower context."""
    rows = db.execute(
        "SELECT c.*, i.title, i.cover_path, b.name as borrower_name, "
        "CAST(julianday('now') - julianday(c.checked_out) AS INTEGER) as days_out "
        "FROM checkouts c "
        "JOIN items_live i ON c.item_id = i.id "
        "JOIN borrowers b ON c.borrower_id = b.id "
        f"WHERE {OVERDUE_CONDITION} "
        "ORDER BY c.checked_out ASC",
        (get_overdue_days(db),),
    ).fetchall()
    return [dict(row) for row in rows]


def count_overdue(db) -> int:
    """How many checkouts are overdue — the same predicate as get_overdue_loans."""
    return db.execute(
        "SELECT COUNT(*) AS c FROM checkouts c "
        "JOIN items_live i ON c.item_id = i.id "
        f"WHERE {OVERDUE_CONDITION}",
        (get_overdue_days(db),),
    ).fetchone()["c"]


# --- Borrowers ---

@router.post("/borrowers")
async def create_borrower(name: str = Form(...), _=Depends(require_role("admin")),
                          __=Depends(require_feature("lending"))):
    clean_name = name.strip()
    if not clean_name:
        return JSONResponse(
            {"ok": False, "message": "Borrower name is required"}, status_code=400
        )

    try:
        with get_db() as db:
            db.execute("INSERT INTO borrowers (name) VALUES (?)", (clean_name,))
    except sqlite3.IntegrityError:
        return RedirectResponse(
            url="/settings?borrower_error=duplicate", status_code=303
        )
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/borrowers/{borrower_id}/delete")
async def delete_borrower(
    borrower_id: int,
    _=Depends(require_role("admin")),
    __=Depends(require_feature("lending")),
):
    """Remove a borrower and, with them, their completed loan history.

    `checkouts.borrower_id` has no ON DELETE action and foreign keys are
    enforced, so deleting a borrower who has ever returned a book used to
    raise IntegrityError and 500 (issue #29). Their history goes with them,
    matching what deleting a location or a platform already does.
    """
    with get_db() as db:
        # Take the write lock before the existence/active-loan guards. A rival
        # checkout writer must not be able to commit between either read and
        # the delete below.
        db.execute("BEGIN IMMEDIATE")
        borrower = db.execute(
            "SELECT id FROM borrowers WHERE id = ?", (borrower_id,)
        ).fetchone()
        if not borrower:
            return RedirectResponse(
                url="/settings?borrower_error=missing", status_code=303
            )

        active = db.execute(
            "SELECT COUNT(*) as c FROM checkouts WHERE borrower_id = ? AND checked_in IS NULL",
            (borrower_id,),
        ).fetchone()["c"]
        if active > 0:
            return RedirectResponse(url="/settings?borrower_error=active", status_code=303)
        db.execute("DELETE FROM checkouts WHERE borrower_id = ?", (borrower_id,))
        db.execute("DELETE FROM borrowers WHERE id = ?", (borrower_id,))
    return RedirectResponse(url="/settings", status_code=303)


# --- Checkouts ---

@router.post("/items/{item_id}/checkout")
async def checkout_item(
    request: Request,
    item_id: int,
    borrower_id: int = Form(...),
    due_days: int = Form(14),
    notes: str = Form(""),
    _=Depends(require_catalogue_role("editor")),
    __=Depends(require_feature("lending")),
):
    """Check out an item to a borrower."""
    if due_days > 0:
        try:
            due = (date.today() + timedelta(days=due_days)).isoformat()
        except OverflowError:
            return JSONResponse(
                {"ok": False, "message": i18n.gettext("Due date is out of range")}, status_code=400
            )
    else:
        due = None

    with get_db() as db:
        # Serialize the existence checks, active-loan guard and insert. A bare
        # SELECT does not start a SQLite write transaction, so BEGIN IMMEDIATE
        # is required before the guard to prevent two concurrent checkouts.
        db.execute("BEGIN IMMEDIATE")

        try:
            require_item_role(db, item_id, "editor")
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return JSONResponse(
                {"ok": False, "message": "Item not found"}, status_code=404
            )
        if not db.execute("SELECT 1 FROM items_live WHERE id = ?", (item_id,)).fetchone():
            return JSONResponse(
                {"ok": False, "message": "Item not found"}, status_code=404
            )
        if not db.execute(
            "SELECT 1 FROM borrowers WHERE id = ?", (borrower_id,)
        ).fetchone():
            return JSONResponse(
                {"ok": False, "message": "Borrower not found"}, status_code=404
            )

        active = db.execute(
            "SELECT id FROM checkouts WHERE item_id = ? AND checked_in IS NULL", (item_id,)
        ).fetchone()
        if active:
            return JSONResponse(
                {"ok": False, "message": "Already checked out"}, status_code=409
            )

        db.execute(
            "INSERT INTO checkouts (item_id, borrower_id, due_date, notes) VALUES (?, ?, ?, ?)",
            (item_id, borrower_id, due, notes.strip() or None),
        )

    return RedirectResponse(url=f"/item/{item_id}", status_code=303)


@router.post("/checkouts/{checkout_id}/checkin")
async def checkin_item(
    checkout_id: int,
    _=Depends(require_catalogue_role("editor")),
    __=Depends(require_feature("lending")),
):
    """Check in an active item loan exactly once."""
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        checkout = db.execute(
            "SELECT item_id, checked_in FROM checkouts WHERE id = ?", (checkout_id,)
        ).fetchone()
        if not checkout:
            return JSONResponse(
                {"ok": False, "message": i18n.gettext("Checkout not found")}, status_code=404
            )
        require_item_role(db, checkout["item_id"], "editor")
        if checkout["checked_in"] is not None:
            return JSONResponse(
                {"ok": False, "message": i18n.gettext("Checkout already checked in")}, status_code=409
            )
        cursor = db.execute(
            "UPDATE checkouts SET checked_in = datetime('now') "
            "WHERE id = ? AND checked_in IS NULL",
            (checkout_id,),
        )
        if cursor.rowcount != 1:
            return JSONResponse(
                {"ok": False, "message": i18n.gettext("Checkout already checked in")}, status_code=409
            )
    return RedirectResponse(url=f"/item/{checkout['item_id']}", status_code=303)


@router.get("/checkouts/overdue")
async def overdue_items(
    request: Request,
    _=Depends(require_catalogue_role("viewer")),
    __=Depends(require_feature("lending")),
):
    """List all overdue checkouts (explicit due date or fallback window)."""
    with get_db() as db:
        return get_overdue_loans(db)
