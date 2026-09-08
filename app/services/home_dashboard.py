"""Read-only catalogue summary for Shelf's Home page.

Catalogue ownership, lending and metadata remain shared facts. When an acting
user is supplied, every catalogue-derived projection is constrained to that
user's accessible Shelf libraries while Wishlist comes only from that user's
``user_item_state`` row. Passing no user retains the historical service
contract used by presentation-neutral unit tests and migrations.
"""

from __future__ import annotations

from app.services import libraries


def _legacy_summary(db, recent_limit: int) -> dict:
    """Pre-library/global summary kept for neutral service callers."""
    total = db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    owned = db.execute(
        "SELECT COUNT(*) AS c FROM items WHERE owned = 1"
    ).fetchone()["c"]
    wishlist = db.execute(
        "SELECT COUNT(*) AS c FROM items WHERE owned = 0"
    ).fetchone()["c"]
    lent_out = db.execute(
        "SELECT COUNT(DISTINCT item_id) AS c FROM checkouts WHERE checked_in IS NULL"
    ).fetchone()["c"]
    missing_cover = db.execute(
        "SELECT COUNT(*) AS c FROM items "
        "WHERE cover_path IS NULL OR TRIM(cover_path) = ''"
    ).fetchone()["c"]

    type_rows = db.execute(
        "SELECT media_type, COUNT(*) AS item_count, "
        "SUM(CASE WHEN owned = 1 THEN 1 ELSE 0 END) AS owned_count, "
        "SUM(CASE WHEN owned = 0 THEN 1 ELSE 0 END) AS wishlist_count "
        "FROM items GROUP BY media_type "
        "ORDER BY item_count DESC, media_type COLLATE NOCASE"
    ).fetchall()

    limit = max(0, min(int(recent_limit), 50))
    recent = []
    if limit:
        recent = [
            dict(row)
            for row in db.execute(
                "SELECT id, title, authors, media_type, cover_path, owned, "
                "CASE WHEN owned = 0 THEN 1 ELSE 0 END AS wishlist, created_at "
                "FROM items ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]

    return {
        "total_count": total,
        "owned_count": owned,
        "wishlist_count": wishlist,
        "lent_out_count": lent_out,
        "missing_cover_count": missing_cover,
        "media_types": [dict(row) for row in type_rows],
        "recent_items": recent,
    }


def dashboard_summary(db, *, recent_limit: int = 8, user: dict | None = None) -> dict:
    """Return stable Home metrics, optionally for one user's library access set.

    ``owned`` is shared catalogue acquisition state. ``wishlist`` is personal.
    Admins see the whole catalogue through the same central library predicate,
    but their Wishlist still comes from their own account state.
    """
    if user is None:
        return _legacy_summary(db, recent_limit)

    actor = dict(user)
    user_id = int(actor["id"])
    access_sql, access_params = libraries.item_access_condition(actor, item_alias="i")

    def scalar(condition: str = "1 = 1", params=()):
        return db.execute(
            f"SELECT COUNT(*) AS c FROM items i "
            f"WHERE ({condition}) AND ({access_sql})",
            [*params, *access_params],
        ).fetchone()["c"]

    total = scalar()
    owned = scalar("i.owned = 1")
    missing_cover = scalar("i.cover_path IS NULL OR TRIM(i.cover_path) = ''")

    wishlist = db.execute(
        "SELECT COUNT(*) AS c FROM items i "
        "JOIN user_item_state uis ON uis.item_id = i.id AND uis.user_id = ? "
        f"WHERE uis.wishlist = 1 AND ({access_sql})",
        [user_id, *access_params],
    ).fetchone()["c"]

    lent_out = db.execute(
        "SELECT COUNT(DISTINCT c.item_id) AS c FROM checkouts c "
        "JOIN items i ON i.id = c.item_id "
        f"WHERE c.checked_in IS NULL AND ({access_sql})",
        access_params,
    ).fetchone()["c"]

    type_rows = db.execute(
        "SELECT i.media_type, COUNT(*) AS item_count, "
        "SUM(CASE WHEN i.owned = 1 THEN 1 ELSE 0 END) AS owned_count, "
        "SUM(CASE WHEN COALESCE(uis.wishlist, 0) = 1 THEN 1 ELSE 0 END) AS wishlist_count "
        "FROM items i "
        "LEFT JOIN user_item_state uis ON uis.item_id = i.id AND uis.user_id = ? "
        f"WHERE ({access_sql}) "
        "GROUP BY i.media_type "
        "ORDER BY item_count DESC, i.media_type COLLATE NOCASE",
        [user_id, *access_params],
    ).fetchall()

    limit = max(0, min(int(recent_limit), 50))
    recent = []
    if limit:
        recent = [
            dict(row)
            for row in db.execute(
                "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, "
                "i.owned, COALESCE(uis.wishlist, 0) AS wishlist, i.created_at "
                "FROM items i "
                "LEFT JOIN user_item_state uis "
                "ON uis.item_id = i.id AND uis.user_id = ? "
                f"WHERE ({access_sql}) "
                "ORDER BY i.created_at DESC, i.id DESC LIMIT ?",
                [user_id, *access_params, limit],
            ).fetchall()
        ]

    return {
        "total_count": total,
        "owned_count": owned,
        "wishlist_count": wishlist,
        "lent_out_count": lent_out,
        "missing_cover_count": missing_cover,
        "media_types": [dict(row) for row in type_rows],
        "recent_items": recent,
    }
