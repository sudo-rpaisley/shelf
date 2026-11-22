"""Personal Continue / Up Next rail for Home.

The rail is derived rather than stored: current personal in-progress state is
shown first, and the next accessible member of a recently completed series is
suggested automatically. Hidden library items are a hard stop, never a bridge
to later visible titles.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.config import MEDIA_FAMILIES, MEDIA_TYPES
from app.database import get_db
from app.routers import series_memberships
from app.routers.series import LIVE_SERIES_MEMBERSHIPS, PHYSICAL_SERIES_MEMBERSHIPS
from app.services import libraries, user_state
from app.services.catalogue_security import require_catalogue_role, require_item_role

router = APIRouter(prefix="/api")

_MAX_CONTINUE_ITEMS = 8


def _user(request: Request) -> dict:
    return dict(request.state.user)


def _family_types(media_type: str) -> tuple[str, ...]:
    for family in MEDIA_FAMILIES.values():
        if media_type in family["types"]:
            return tuple(family["types"])
    return (media_type,)


def _whole_number(value) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return None


def _has_missing_integer_position(previous, candidate) -> bool:
    """Avoid silently jumping over an obvious missing numbered instalment."""
    left = _whole_number(previous)
    right = _whole_number(candidate)
    return left is not None and right is not None and right > left + 1


def _decorate(row: dict, *, mode: str, series_name: str | None = None) -> dict:
    item = dict(row)
    labels = user_state.status_labels(item["media_type"])
    activity = labels["reading"]
    item["mode"] = mode
    item["series_name"] = series_name or item.get("series_name")
    item["position"] = item.get("position", item.get("series_position"))
    item["media_type_label"] = MEDIA_TYPES.get(item["media_type"], item["media_type"])
    item["activity_label"] = activity
    item["headline"] = f"Continue {activity}" if mode == "continue" else "Up Next"
    item["start_label"] = f"Start {activity}"
    return item


def _continue_rows(db, user: dict) -> list[dict]:
    """Return current activity followed by safe, series-derived next items."""
    user_state.ensure_schema(db)
    series_memberships._ensure_item_series_schema(db)
    user_id = int(user["id"])
    condition, access_params = libraries.item_access_condition(user, item_alias="i")

    active_rows = db.execute(
        "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, i.owned, "
        "i.series_name, i.series_position, uis.progress_value, uis.progress_total, "
        "uis.progress_unit, uis.updated_at "
        "FROM user_item_state uis JOIN items_live i ON i.id = uis.item_id "
        f"WHERE uis.user_id = ? AND uis.reading_status = 'reading' AND ({condition}) "
        "ORDER BY uis.updated_at DESC, i.id DESC",
        [user_id] + access_params,
    ).fetchall()
    result = [_decorate(dict(row), mode="continue") for row in active_rows]
    if len(result) >= _MAX_CONTINUE_ITEMS:
        return result[:_MAX_CONTINUE_ITEMS]

    active_ids = {int(row["id"]) for row in result}
    suggested_ids: set[int] = set()
    seen_series: set[str] = set()

    completed = db.execute(
        "SELECT i.id, i.media_type, uis.updated_at, s.series_name, s.position "
        "FROM user_item_state uis "
        "JOIN items_live i ON i.id = uis.item_id "
        f"JOIN {LIVE_SERIES_MEMBERSHIPS} s ON s.item_id = i.id "
        f"WHERE uis.user_id = ? AND uis.reading_status = 'read' "
        f"AND s.position IS NOT NULL AND ({condition}) "
        "ORDER BY uis.updated_at DESC, i.id DESC, s.series_name COLLATE NOCASE",
        [user_id] + access_params,
    ).fetchall()

    for completed_row in completed:
        if len(result) >= _MAX_CONTINUE_ITEMS:
            break
        series_name = str(completed_row["series_name"] or "").strip()
        series_key = series_name.casefold()
        if not series_name or series_key in seen_series:
            continue
        seen_series.add(series_key)

        family_types = _family_types(completed_row["media_type"])
        placeholders = ",".join("?" for _ in family_types)
        members = db.execute(
            "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, i.owned, "
            "i.series_name, i.series_position, i.publish_year, i.deleted_at, s.position, "
            "uis.reading_status, uis.progress_value, uis.progress_total, "
            "uis.progress_unit "
            # Deliberate physical order census; blocked rows are never rendered.
            f"FROM {PHYSICAL_SERIES_MEMBERSHIPS} s JOIN items i ON i.id = s.item_id "
            "LEFT JOIN user_item_state uis ON uis.item_id = i.id AND uis.user_id = ? "
            "WHERE s.series_name = ? COLLATE NOCASE "
            f"AND i.media_type IN ({placeholders}) "
            "ORDER BY s.position IS NULL, s.position, i.publish_year IS NULL, "
            "i.publish_year, i.title COLLATE NOCASE, i.id",
            [user_id, series_name] + list(family_types),
        ).fetchall()

        current_index = next(
            (index for index, member in enumerate(members) if int(member["id"]) == int(completed_row["id"])),
            None,
        )
        if current_index is None:
            continue

        previous_position = members[current_index]["position"]
        candidate = None
        for member in members[current_index + 1 :]:
            position = member["position"]
            if position is None or _has_missing_integer_position(previous_position, position):
                break

            member_id = int(member["id"])
            # Determine the real next local member before applying ACL. If that
            # member is hidden, stop instead of skipping through it and leaking
            # the existence/order of a protected title via a later suggestion.
            if member["deleted_at"] is not None or not libraries.has_item_role(db, user, member_id, "viewer"):
                break

            state = member["reading_status"]
            if state == "read":
                previous_position = position
                continue
            if state == "reading" or member_id in active_ids:
                break
            candidate = dict(member)
            break

        if not candidate:
            continue
        candidate_id = int(candidate["id"])
        if candidate_id in suggested_ids:
            continue
        suggested_ids.add(candidate_id)
        result.append(_decorate(candidate, mode="up_next", series_name=series_name))

    return result[:_MAX_CONTINUE_ITEMS]


def _render(request: Request):
    with get_db() as db:
        rows = _continue_rows(db, _user(request))
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/continue.html",
        {"continue_items": rows},
    )


@router.get("/home/continue")
async def continue_home(
    request: Request,
    _=Depends(require_catalogue_role("viewer")),
):
    return _render(request)


@router.post("/items/{item_id}/continue/start")
async def start_continue_item(
    request: Request,
    item_id: int,
    _=Depends(require_catalogue_role("viewer")),
):
    user = _user(request)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        require_item_role(db, item_id, "viewer")
        user_state.set_reading_status(db, int(user["id"]), item_id, "reading")
    return _render(request)
