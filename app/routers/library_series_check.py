"""ACL-aware Hardcover series completeness check.

A series lookup is partly public/provider data and partly local catalogue state.
The provider lookup may succeed even when Shelf owns none of the series; only
owned/wishlist classification and cache persistence are constrained by the
acting user's accessible local items.
"""

from __future__ import annotations

from fastapi import Depends, Request

from app.auth import require_role
from app.database import get_db, get_setting
from app.routers import series
from app.services import hardcover, libraries


def _remove_route(path: str, method: str) -> None:
    method = method.upper()
    full_path = f"{series.router.prefix}{path}" if series.router.prefix else path
    series.router.routes[:] = [
        route
        for route in series.router.routes
        if not (
            getattr(route, "path", None) == full_path
            and method in (getattr(route, "methods", None) or set())
        )
    ]


_remove_route("/api/series/check", "GET")


@series.router.get("/api/series/check")
async def library_series_check(
    request: Request,
    name: str = "",
    _=Depends(require_role("viewer")),
):
    clean = name.strip()
    if not clean:
        return {"ok": False, "message": "Series name required"}

    user = dict(request.state.user)
    access_sql, access_params = libraries.item_access_condition(user, item_alias="i")
    with get_db() as db:
        series._sync_item_series_memberships(db)
        token = get_setting(db, "hardcover_token")
        if not token:
            return {"ok": False, "message": "Hardcover integration not configured"}
        local = db.execute(
            "SELECT i.title, i.owned, i.hardcover_book_id FROM item_series s "
            "JOIN items i ON i.id = s.item_id "
            f"WHERE s.series_name = ? COLLATE NOCASE AND ({access_sql})",
            [clean] + access_params,
        ).fetchall()

    books = await hardcover.get_series_books(clean, token)
    if books is None:
        return {"ok": False, "message": "Series not found on Hardcover (or lookup failed)"}

    by_hc_id = {
        row["hardcover_book_id"]: row
        for row in local
        if row["hardcover_book_id"]
    }
    by_title = {
        row["title"].casefold().strip(): row
        for row in local
    }

    out = []
    for book in books:
        match = by_hc_id.get(book["hardcover_book_id"]) or by_title.get(
            book["title"].casefold().strip()
        )
        if match:
            status = "owned" if match["owned"] else "wishlist"
        else:
            status = "missing"
        out.append({**book, "status": status, "series_name": clean})

    missing = sum(1 for book in out if book["status"] == "missing")

    # A provider-only lookup is useful read-only data, but must not create a
    # local series_meta record. Hidden-only local series are intentionally
    # equivalent to no local series for this user.
    if local:
        with get_db() as db:
            series._upsert_series_check(db, clean, len(out), missing)

    return {
        "ok": True,
        "series": clean,
        "total": len(out),
        "missing": missing,
        "books": out,
    }
