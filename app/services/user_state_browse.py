"""Browse-facing projections of per-user media state.

Migrations 55-56 snapshot the old shared activity for users that exist during
upgrade. After that snapshot a missing ``user_item_state`` row means genuinely
unset personal state. The central Browse registry owns the SQL expressions;
this module provides explicit personal overlays and a count API adapter.
"""

from __future__ import annotations

from app.services import user_state


def _personal_reading_status_sql(user_id: int) -> tuple[str, list]:
    return (
        "(SELECT uis.reading_status FROM user_item_state uis "
        "WHERE uis.user_id = ? AND uis.item_id = i.id)",
        [int(user_id)],
    )


def _personal_wishlist_sql(user_id: int) -> tuple[str, list]:
    return (
        "COALESCE((SELECT uis.wishlist FROM user_item_state uis "
        "WHERE uis.user_id = ? AND uis.item_id = i.id), 0)",
        [int(user_id)],
    )


# browse_filters owns these SQL contracts directly. Importing a read helper
# must not replace global query behavior or depend on router import order.


def overlay_items(db, user_id: int, items: list[dict]) -> list[dict]:
    """Replace personal fields on Browse rows with one user's values.

    Browse grouping returns mutable dictionaries, so the projection can happen
    after grouping without changing group identity, pagination or catalogue
    sort order. Shared ownership/lending/location fields are deliberately left
    untouched.
    """
    if not items:
        return items

    from fastapi import HTTPException
    from app.services.catalogue_security import current_actor

    actor = current_actor()
    if actor is not None and int(actor["id"]) != int(user_id):
        raise HTTPException(403, "Forbidden")
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
            item["reading_status"] = None
            item["date_started"] = None
            item["date_finished"] = None
            item["personal_rating"] = None
            item["personal_wishlist"] = False
            item["personal_favourite"] = False
            item["personal_notes"] = None
            item["personal_progress_value"] = None
            item["personal_progress_total"] = None
            item["personal_progress_unit"] = None
            item["personal_state_persisted"] = False
    return items


def filter_counts(
    db,
    values: dict,
    total: int,
    user_id: int,
    *,
    user: dict | None = None,
) -> dict:
    """Cross-filter counts resolved against one user's state and access set."""
    from app.services.browse_counts import filter_counts as catalogue_counts

    user_state.ensure_schema(db)
    return catalogue_counts(db, values, total, user_id=user_id, user=user)
