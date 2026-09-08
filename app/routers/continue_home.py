"""Personal Continue / Up Next rail for Home.

Current activity comes from the acting user's personal state. Automatic
progression is derived from the shared catalogue's canonical
``series_name``/``series_position`` fields. A protected next position is a
hard stop: Shelf never skips a hidden series member to suggest a later title,
which would disclose protected ordering by implication.
"""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import require_role
from app.config import (
    BOOK_MEDIA_TYPES,
    MEDIA_TYPES,
    MUSIC_MEDIA_TYPES,
    PERIODICAL_MEDIA_TYPES,
)
from app.database import get_db
from app.services import libraries, user_state

router = APIRouter(prefix="/api")

_MAX_CONTINUE_ITEMS = 8


def _actor(request: Request) -> dict:
    return dict(request.state.user)


def _family_types(media_type: str) -> tuple[str, ...]:
    """Media identities that may represent neighbouring series instalments."""
    if media_type in BOOK_MEDIA_TYPES:
        return tuple(sorted(BOOK_MEDIA_TYPES))
    if media_type in MUSIC_MEDIA_TYPES:
        return tuple(sorted(MUSIC_MEDIA_TYPES))
    if media_type in PERIODICAL_MEDIA_TYPES:
        return tuple(sorted(PERIODICAL_MEDIA_TYPES))
    return (media_type,)


def _whole_number(value) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def _has_missing_integer_position(previous, candidate) -> bool:
    """Do not jump over an obvious missing numbered instalment."""
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


def _series_members(db, user_id: int, series_name: str, media_type: str) -> list[dict]:
    family = _family_types(media_type)
    placeholders = ",".join("?" for _ in family)
    rows = db.execute(
        "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, i.owned, "
        "i.series_name, i.series_position, i.publish_year, "
        "uis.reading_status, uis.progress_value, uis.progress_total, "
        "uis.progress_unit "
        "FROM items i "
        "LEFT JOIN user_item_state uis ON uis.item_id = i.id AND uis.user_id = ? "
        "WHERE i.series_name = ? COLLATE NOCASE "
        "AND i.series_position IS NOT NULL "
        f"AND i.media_type IN ({placeholders}) "
        "ORDER BY i.series_position, i.publish_year IS NULL, i.publish_year, "
        "i.title COLLATE NOCASE, i.id",
        [user_id, series_name, *family],
    ).fetchall()
    return [dict(row) for row in rows]


def _next_series_candidate(
    db,
    user: dict,
    completed: dict,
    active_ids: set[int],
) -> dict | None:
    """Find the next safe local position after one completed series item.

    Members are grouped by position so alternate local formats of the same
    instalment do not become an artificial "next" item. If a whole next
    position is inaccessible, progression stops rather than stepping over it.
    """
    members = _series_members(
        db,
        int(user["id"]),
        str(completed["series_name"]),
        str(completed["media_type"]),
    )
    if not any(int(member["id"]) == int(completed["id"]) for member in members):
        return None

    current_position = completed["series_position"]
    try:
        current_number = float(current_position)
    except (TypeError, ValueError):
        return None

    by_position: dict[float, list[dict]] = defaultdict(list)
    raw_position: dict[float, object] = {}
    for member in members:
        try:
            number = float(member["series_position"])
        except (TypeError, ValueError):
            continue
        if number <= current_number:
            continue
        by_position[number].append(member)
        raw_position.setdefault(number, member["series_position"])

    previous = current_position
    for number in sorted(by_position):
        position = raw_position[number]
        if _has_missing_integer_position(previous, position):
            return None

        group = by_position[number]
        accessible = [
            member
            for member in group
            if libraries.has_item_role(db, user, int(member["id"]), "viewer")
        ]
        if not accessible:
            # The next known local position exists but is protected. Never
            # leak that fact by suggesting something beyond it.
            return None

        if any(member.get("reading_status") == "reading" for member in accessible):
            return None

        # Completing any accessible representation completes this local
        # position for progression purposes. Continue through consecutive
        # already-read positions until the first unfinished one.
        if any(member.get("reading_status") == "read" for member in accessible):
            previous = position
            continue

        candidate = next(
            (
                member
                for member in accessible
                if int(member["id"]) not in active_ids
            ),
            None,
        )
        if candidate is None:
            return None
        candidate = dict(candidate)
        candidate["position"] = position
        return candidate

    return None


def continue_rows(db, user: dict) -> list[dict]:
    """Current personal activity followed by safe automatic series suggestions."""
    user_id = int(user["id"])
    condition, access_params = libraries.item_access_condition(user, item_alias="i")

    active_rows = db.execute(
        "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, i.owned, "
        "i.series_name, i.series_position, uis.progress_value, uis.progress_total, "
        "uis.progress_unit, uis.updated_at "
        "FROM user_item_state uis JOIN items i ON i.id = uis.item_id "
        f"WHERE uis.user_id = ? AND uis.reading_status = 'reading' AND ({condition}) "
        "ORDER BY uis.updated_at DESC, i.id DESC",
        [user_id, *access_params],
    ).fetchall()
    result = [_decorate(dict(row), mode="continue") for row in active_rows]
    if len(result) >= _MAX_CONTINUE_ITEMS:
        return result[:_MAX_CONTINUE_ITEMS]

    active_ids = {int(row["id"]) for row in result}
    suggested_ids: set[int] = set()
    seen_series: set[str] = set()

    completed_rows = db.execute(
        "SELECT i.id, i.media_type, i.series_name, i.series_position, uis.updated_at "
        "FROM user_item_state uis JOIN items i ON i.id = uis.item_id "
        f"WHERE uis.user_id = ? AND uis.reading_status = 'read' "
        "AND i.series_name IS NOT NULL AND trim(i.series_name) != '' "
        "AND i.series_position IS NOT NULL "
        f"AND ({condition}) "
        "ORDER BY uis.updated_at DESC, i.id DESC",
        [user_id, *access_params],
    ).fetchall()

    for row in completed_rows:
        if len(result) >= _MAX_CONTINUE_ITEMS:
            break
        completed = dict(row)
        series_name = str(completed.get("series_name") or "").strip()
        key = series_name.casefold()
        if not series_name or key in seen_series:
            continue
        seen_series.add(key)

        candidate = _next_series_candidate(db, user, completed, active_ids)
        if candidate is None:
            continue
        candidate_id = int(candidate["id"])
        if candidate_id in suggested_ids:
            continue
        suggested_ids.add(candidate_id)
        result.append(
            _decorate(candidate, mode="up_next", series_name=series_name)
        )

    return result[:_MAX_CONTINUE_ITEMS]


def _render(request: Request):
    with get_db() as db:
        rows = continue_rows(db, _actor(request))
    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/continue.html",
        {"continue_items": rows},
    )


@router.get("/home/continue")
async def continue_home(
    request: Request,
    _=Depends(require_role("viewer")),
):
    return _render(request)


@router.post("/items/{item_id}/continue/start")
async def start_continue_item(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    user = _actor(request)
    with get_db() as db:
        if not libraries.has_item_role(db, user, item_id, "viewer"):
            return HTMLResponse("Not found", status_code=404)
        user_state.set_reading_status(db, int(user["id"]), item_id, "reading")
    return _render(request)
