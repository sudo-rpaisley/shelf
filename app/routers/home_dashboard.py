"""Separately mounted Home dashboard metrics."""

from fastapi import APIRouter, Depends, Request

from app.services.catalogue_security import require_catalogue_role as require_role
from app.database import get_db
from app.routers.checkouts import get_overdue_loans
from app.services import user_state

router = APIRouter()

# The dashboard's legacy flat-location summary is distinct from the precise
# copy-location workflow; this list does not define copy eligibility or IDs.
_PHYSICAL_MEDIA_TYPES = (
    "book",
    "kids_book",
    "magazine",
    "dvd",
    "vinyl",
    "cassette",
    "cd",
    "music_other",
    "comic",
    "video_game",
)


@router.get("/api/home/dashboard")
async def home_dashboard(request: Request, _=Depends(require_role("viewer"))):
    """Render the small operational dashboard used on Home.

    Kept out of the initial Home query so the landing page remains quick and
    can paint its catalogue-family cards immediately. HTMX loads this panel
    after first paint. Catalogue health remains household-wide; Wishlist is a
    personal preference and is resolved for the signed-in user.
    """
    placeholders = ",".join("?" for _ in _PHYSICAL_MEDIA_TYPES)
    user_id = int(request.state.user["id"])
    with get_db() as db:
        user_state.ensure_schema(db)
        owned_count = db.execute(
            "SELECT COUNT(*) AS c FROM items_live WHERE owned = 1"
        ).fetchone()["c"]
        wishlist_count = db.execute(
            "SELECT COUNT(*) AS c FROM user_item_state s JOIN items_live i ON i.id = s.item_id "
            "WHERE s.user_id = ? AND s.wishlist = 1",
            (user_id,),
        ).fetchone()["c"]
        lent_out_count = db.execute(
            "SELECT COUNT(DISTINCT c.item_id) AS c FROM checkouts c JOIN items_live i ON i.id = c.item_id "
            "WHERE c.checked_in IS NULL"
        ).fetchone()["c"]
        overdue_count = len(get_overdue_loans(db))
        missing_cover_count = db.execute(
            "SELECT COUNT(*) AS c FROM items_live WHERE owned = 1 "
            "AND (cover_path IS NULL OR TRIM(cover_path) = '')"
        ).fetchone()["c"]
        unlocated_count = db.execute(
            f"SELECT COUNT(*) AS c FROM items_live WHERE owned = 1 "
            f"AND media_type IN ({placeholders}) AND location_id IS NULL",
            _PHYSICAL_MEDIA_TYPES,
        ).fetchone()["c"]
        attention_count = db.execute(
            f"SELECT COUNT(*) AS c FROM items_live WHERE owned = 1 AND ("
            "cover_path IS NULL OR TRIM(cover_path) = '' OR "
            f"(media_type IN ({placeholders}) AND location_id IS NULL))",
            _PHYSICAL_MEDIA_TYPES,
        ).fetchone()["c"]
        locations = db.execute(
            "SELECT l.id, l.name, COUNT(i.id) AS item_count "
            "FROM locations l LEFT JOIN items_live i "
            "ON i.location_id = l.id AND i.owned = 1 "
            "GROUP BY l.id, l.name, l.sort_order "
            "ORDER BY item_count DESC, l.sort_order, l.name COLLATE NOCASE "
            "LIMIT 6"
        ).fetchall()

    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/home_dashboard.html",
        {
            "owned_count": owned_count,
            "wishlist_count": wishlist_count,
            "lent_out_count": lent_out_count,
            "overdue_count": overdue_count,
            "missing_cover_count": missing_cover_count,
            "unlocated_count": unlocated_count,
            "attention_count": attention_count,
            "dashboard_locations": locations,
        },
    )
