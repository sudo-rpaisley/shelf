"""Browse-facing projections of per-user media state."""

from __future__ import annotations

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
