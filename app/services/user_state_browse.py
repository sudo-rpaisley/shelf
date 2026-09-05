"""Browse-facing projections of per-user media state."""

from __future__ import annotations

from app import browse_filters
from app.services import user_state


def overlay_items(db, user_id: int, items: list[dict]) -> list[dict]:
    """Replace legacy personal fields on Browse rows with one user's values.

    Browse grouping returns mutable dictionaries, so the projection can happen
    after grouping without changing group identity, pagination or catalogue
    sort order. Shared ownership/lending/location fields are deliberately left
    untouched.
    """
    if not items:
        return items

    user_state.ensure_schema(db)
    ids = [int(item["id"]) for item in items]
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        f"""SELECT item_id, reading_status, date_started, date_finished,
                   rating, wishlist, favourite, personal_notes,
                   progress_value, progress_total, progress_unit
              FROM user_item_state
             WHERE user_id = ? AND item_id IN ({placeholders})""",
        [int(user_id)] + ids,
    ).fetchall()
    by_item = {row["item_id"]: row for row in rows}

    for item in items:
        row = by_item.get(int(item["id"]))
        if row:
            item["reading_status"] = row["reading_status"]
            item["date_started"] = row["date_started"]
            item["date_finished"] = row["date_finished"]
            item["personal_rating"] = row["rating"]
            item["personal_wishlist"] = bool(row["wishlist"])
            item["personal_favourite"] = bool(row["favourite"])
            item["personal_notes"] = row["personal_notes"]
            item["personal_progress_value"] = row["progress_value"]
            item["personal_progress_total"] = row["progress_total"]
            item["personal_progress_unit"] = row["progress_unit"]
            item["personal_state_persisted"] = True
        else:
            # Legacy fallback mirrors get_state(): existing installations keep
            # their old status/wishlist appearance until a user changes it.
            item["personal_rating"] = None
            item["personal_wishlist"] = not bool(item.get("owned"))
            item["personal_favourite"] = False
            item["personal_notes"] = None
            item["personal_progress_value"] = None
            item["personal_progress_total"] = None
            item["personal_progress_unit"] = None
            item["personal_state_persisted"] = False
    return items


def filter_counts(db, values: dict, total: int, user_id: int) -> dict:
    """Cross-filter counts resolved against the signed-in user's state."""
    user_state.ensure_schema(db)

    def _count_where(exclude):
        return browse_filters.build_where(values, exclude=exclude, user_id=user_id)

    type_where, type_params = _count_where("media_type_filter")
    type_counts = {
        row["media_type"]: row["c"]
        for row in db.execute(
            f"SELECT media_type, COUNT(*) AS c FROM items i {type_where} GROUP BY media_type",
            type_params,
        ).fetchall()
    }
    type_total = sum(type_counts.values())

    own_where, own_params = _count_where("owned")
    own_join = " AND" if own_where else " WHERE"
    owned_count = db.execute(
        f"SELECT COUNT(*) AS c FROM items i {own_where}{own_join} i.owned = 1",
        own_params,
    ).fetchone()["c"]
    wishlist_expr, wishlist_expr_params = browse_filters.personal_wishlist_sql(user_id)
    wishlist_count = db.execute(
        f"SELECT COUNT(*) AS c FROM items i {own_where}{own_join} {wishlist_expr} = 1",
        list(own_params) + wishlist_expr_params,
    ).fetchone()["c"]

    loc_where, loc_params = _count_where("location_filter")
    loc_join = " AND" if loc_where else " WHERE"
    location_counts = {
        row["location_id"]: row["c"]
        for row in db.execute(
            f"SELECT location_id, COUNT(*) AS c FROM items i {loc_where}"
            f"{loc_join} location_id IS NOT NULL GROUP BY location_id",
            loc_params,
        ).fetchall()
    }
    no_location_count = db.execute(
        f"SELECT COUNT(*) AS c FROM items i {loc_where}{loc_join} location_id IS NULL",
        loc_params,
    ).fetchone()["c"]

    rs_where, rs_params = _count_where("reading_status")
    status_expr, status_expr_params = browse_filters.personal_reading_status_sql(user_id)
    reading_status_counts = {
        row["reading_status"]: row["c"]
        for row in db.execute(
            f"""SELECT reading_status, COUNT(*) AS c
                  FROM (
                      SELECT {status_expr} AS reading_status
                        FROM items i
                        {rs_where}
                  ) resolved
                 WHERE reading_status IS NOT NULL AND reading_status != ''
                 GROUP BY reading_status""",
            status_expr_params + list(rs_params),
        ).fetchall()
    }

    locations = db.execute(
        "SELECT * FROM locations ORDER BY sort_order, name"
    ).fetchall()

    return {
        "type_counts": type_counts,
        "type_total": type_total,
        "owned_count": owned_count,
        "wishlist_count": wishlist_count,
        "location_counts": location_counts,
        "no_location_count": no_location_count,
        "reading_status_counts": reading_status_counts,
        "locations": locations,
        "filtered_total": total,
        "active_type": values["media_type_filter"],
        "active_owned": values["owned"],
        "active_location": values["location_filter"],
        "active_reading_status": values["reading_status"],
    }
