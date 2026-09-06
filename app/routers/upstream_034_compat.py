"""Compatibility guards for the fork's upstream-0.34 integration.

The 0.34 merge deliberately keeps upstream's item value funnel and legacy-book
scanner, while the fork already had stricter request-boundary and mutation
semantics in a few routes.  These focused adapters preserve both sides without
copying the whole upstream ``items.py`` router.

Keep this module narrow.  Once the corresponding behaviour is folded directly
into the upstream handlers it can be retired.
"""

from __future__ import annotations

import sqlite3

import httpx
from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import require_role
from app.config import HTTP_TIMEOUT, MEDIA_TYPES
from app.database import get_db, get_game_platforms, get_setting, gc_orphaned_series_meta
from app.routers import items, user_state_items
from app.routers.series import MAX_SERIES_NAME
from app.services import igdb
from app.services import isbn as isbn_svc
from app.services.item_write import (
    ItemValueError,
    update_item_fields,
    update_items_fields,
    validate_item_fields,
)


def _remove_route(path: str, method: str) -> None:
    method = method.upper()
    full_path = f"{items.router.prefix}{path}" if items.router.prefix else path
    items.router.routes[:] = [
        route
        for route in items.router.routes
        if not (
            getattr(route, "path", None) == full_path
            and method in (getattr(route, "methods", None) or set())
        )
    ]


# ---------------------------------------------------------------------------
# Scanner bridge
# ---------------------------------------------------------------------------
# user_state_items captured items.scan_isbn before upstream 0.34 added the
# legacy_confirm_isbn13 Form parameter.  Calling a FastAPI endpoint function
# directly does not resolve Form defaults, so explicitly forward the field.
_upstream_scan = user_state_items._original_scan


async def _scan_with_legacy_confirmation(request: Request, *args, **kwargs):
    if "legacy_confirm_isbn13" not in kwargs:
        form = await request.form()
        kwargs["legacy_confirm_isbn13"] = str(
            form.get("legacy_confirm_isbn13") or ""
        )
    return await _upstream_scan(request, *args, **kwargs)


user_state_items._original_scan = _scan_with_legacy_confirmation


# ---------------------------------------------------------------------------
# Single-item edit boundary
# ---------------------------------------------------------------------------
def _looks_like_full_edit_form(form) -> bool:
    """The rendered edit page posts these core fields together.

    Older integrations and focused API callers may submit a partial form.  The
    fork historically returned direct 4xx responses for malformed partial
    writes, while the current full edit UI uses upstream's redirect + banner
    error surface.  Preserve both contracts.
    """

    return {"title", "media_type", "owned"}.issubset(form.keys())


def _edit_error_redirect(item_id: int, form, code: str) -> RedirectResponse:
    from_value = str(form.get("from") or "").strip()
    edit_url = f"/item/{item_id}/edit"
    if from_value:
        edit_url += f"?from={from_value}&error={code}"
    else:
        edit_url += f"?error={code}"
    return RedirectResponse(edit_url, status_code=303)


def _validate_partial_edit(item_id: int, form):
    """Validate a partial edit without mutating or storing a cover."""

    with get_db() as db:
        current = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not current:
            return HTMLResponse("Not found", status_code=404)

        if "title" in form and not str(form.get("title") or "").strip():
            return HTMLResponse("Title is required", status_code=400)

        int_fields = {
            "publish_year": "Invalid publish year",
            "page_count": "Invalid page count",
            "duration_mins": "Invalid duration",
            "location_id": "Invalid location",
        }
        for field, message in int_fields.items():
            if field not in form:
                continue
            raw = str(form.get(field) or "").strip()
            if raw:
                try:
                    int(raw)
                except (TypeError, ValueError):
                    return HTMLResponse(message, status_code=400)

        float_fields = {
            "series_position": "Invalid series position",
            "manual_value": "Invalid manual value",
        }
        for field, message in float_fields.items():
            if field not in form:
                continue
            raw = str(form.get(field) or "").strip()
            if raw:
                try:
                    float(raw)
                except (TypeError, ValueError):
                    return HTMLResponse(message, status_code=400)

        if "media_type" in form and form.get("media_type") not in MEDIA_TYPES:
            return HTMLResponse("Invalid media type", status_code=400)

        if "reading_status" in form:
            status = str(form.get("reading_status") or "")
            if status not in ("", "want_to_read", "reading", "read"):
                return HTMLResponse("Invalid reading status", status_code=400)

        if "owned" in form and form.get("owned") not in ("0", "1", 0, 1):
            return HTMLResponse("Owned must be 0 or 1", status_code=400)

        if "location_id" in form:
            raw = str(form.get("location_id") or "").strip()
            if raw:
                try:
                    location_id = int(raw)
                except (TypeError, ValueError):
                    return HTMLResponse("Invalid location", status_code=400)
                if location_id <= 0:
                    return HTMLResponse("Invalid location", status_code=400)
                if not db.execute(
                    "SELECT 1 FROM locations WHERE id = ?", (location_id,)
                ).fetchone():
                    return HTMLResponse("Location not found", status_code=400)

        if "platform" in form:
            platform = str(form.get("platform") or "").strip()
            if platform and platform not in get_game_platforms(db):
                return HTMLResponse("Invalid game platform", status_code=400)

        isbn13 = current["isbn"]
        if "isbn" in form:
            raw_isbn = str(form.get("isbn") or "").strip()
            if raw_isbn:
                pair = isbn_svc.canonical_isbn_pair(raw_isbn)
                if pair is None:
                    return HTMLResponse("Invalid ISBN", status_code=400)
                isbn13 = pair[0]
            else:
                isbn13 = None

        media_type = (
            str(form.get("media_type"))
            if "media_type" in form
            else current["media_type"]
        )
        if isbn13:
            collision = db.execute(
                "SELECT id FROM items WHERE isbn = ? AND media_type = ? AND id != ?",
                (isbn13, media_type, item_id),
            ).fetchone()
            if collision:
                return HTMLResponse(
                    "Update conflicts with existing catalogue data", status_code=409
                )

    return None


_original_personal_update = user_state_items.personal_safe_update_item
_remove_route("/items/{item_id}", "POST")


@items.router.post("/items/{item_id}")
async def integrated_safe_update_item(
    request: Request,
    item_id: int,
    _=Depends(require_role("editor")),
):
    form = await request.form()
    full_form = _looks_like_full_edit_form(form)

    # The per-user wrapper owns reading state.  Match upstream's full-form
    # error surface before that wrapper handles an invalid value itself.
    if "reading_status" in form:
        status = str(form.get("reading_status") or "")
        if status not in ("", "want_to_read", "reading", "read"):
            if full_form:
                return _edit_error_redirect(item_id, form, "invalid_reading_status")
            return HTMLResponse("Invalid reading status", status_code=400)

    if not full_form:
        refused = _validate_partial_edit(item_id, form)
        if refused is not None:
            return refused

    return await _original_personal_update(request, item_id, _=request.state.user)


# ---------------------------------------------------------------------------
# Bulk update bridge
# ---------------------------------------------------------------------------
async def _safe_bulk_update(request: Request, _=None):
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "message": "Invalid request body"}
    if not isinstance(data, dict):
        return {"ok": False, "message": "Invalid request body"}

    raw_ids = data.get("item_ids", [])
    updates = data.get("updates", {})
    if not raw_ids or not isinstance(updates, dict) or not updates:
        return {"ok": False, "message": "No items or updates specified"}

    try:
        item_ids = list(dict.fromkeys(int(value) for value in raw_ids))
    except (TypeError, ValueError):
        return {"ok": False, "message": "Invalid item IDs"}
    if any(item_id <= 0 for item_id in item_ids):
        return {"ok": False, "message": "Invalid item IDs"}

    allowed = {"media_type", "location_id", "reading_status", "owned", "series_name"}
    filtered = {key: value for key, value in updates.items() if key in allowed}
    if not filtered:
        return {"ok": False, "message": "No valid fields to update"}

    if "series_name" in filtered:
        if filtered["series_name"] == "__clear__":
            filtered["series_name"] = None
        else:
            series_name = str(filtered["series_name"] or "").strip()
            if not series_name:
                return {"ok": False, "message": "Series name cannot be empty"}
            if len(series_name) > MAX_SERIES_NAME:
                return {"ok": False, "message": "Series name is too long"}
            filtered["series_name"] = series_name

    if "reading_status" in filtered and filtered["reading_status"] in (
        "",
        "__clear__",
        None,
    ):
        filtered["reading_status"] = None
    if "location_id" in filtered and filtered["location_id"] in (
        "",
        "__clear__",
        None,
    ):
        filtered["location_id"] = None

    placeholders = ",".join("?" for _ in item_ids)
    with get_db() as db:
        existing_ids = [
            row["id"]
            for row in db.execute(
                f"SELECT id FROM items WHERE id IN ({placeholders})", item_ids
            ).fetchall()
        ]
        if not existing_ids:
            return {"ok": False, "message": "No matching items found", "updated": 0}

        old_series_names = []
        if "series_name" in filtered:
            existing_marks = ",".join("?" for _ in existing_ids)
            old_series_names = [
                row["series_name"]
                for row in db.execute(
                    f"SELECT DISTINCT series_name FROM items WHERE id IN ({existing_marks})",
                    existing_ids,
                ).fetchall()
                if row["series_name"]
            ]

        try:
            update_items_fields(db, existing_ids, filtered)
        except ItemValueError as exc:
            return {"ok": False, "message": str(exc), "updated": 0}
        except sqlite3.IntegrityError:
            return {
                "ok": False,
                "message": "Update conflicts with existing catalogue data",
                "updated": 0,
            }

        if old_series_names:
            gc_orphaned_series_meta(db, *old_series_names)

    return {"ok": True, "updated": len(existing_ids)}


user_state_items._original_bulk_update = _safe_bulk_update


# ---------------------------------------------------------------------------
# Merge integrity
# ---------------------------------------------------------------------------
def _merge_value_missing(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


_remove_route("/items/merge", "POST")


@items.router.post("/items/merge")
async def integrated_merge_items(
    request: Request,
    _=Depends(require_role("admin")),
):
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "message": "Invalid request body", "merged": 0}
    if not isinstance(data, dict):
        return {"ok": False, "message": "Invalid request body", "merged": 0}

    try:
        keep_id = int(data.get("keep_id", 0))
        merge_ids = list(
            dict.fromkeys(int(value) for value in data.get("merge_ids", []))
        )
    except (TypeError, ValueError):
        return {"ok": False, "message": "Invalid item IDs", "merged": 0}

    if keep_id <= 0 or not merge_ids or any(value <= 0 for value in merge_ids):
        return {"ok": False, "message": "Specify keep_id and merge_ids", "merged": 0}
    if keep_id in merge_ids:
        return {
            "ok": False,
            "message": "Primary item cannot be merged into itself",
            "merged": 0,
        }

    fillable = (
        "subtitle",
        "authors",
        "cover_path",
        "publisher",
        "publish_year",
        "page_count",
        "description",
        "series_name",
        "series_position",
        "narrator",
        "duration_mins",
        "location_id",
        "abs_id",
        "notes",
        "reading_status",
        "date_started",
        "date_finished",
        "estimated_value",
        "value_updated_at",
        "hardcover_book_id",
        "hardcover_edition_id",
        "hardcover_user_book_id",
        "platform",
        "abs_library_id",
        "manual_value",
        "language",
        "upc",
    )

    merged = 0
    with get_db() as db:
        primary_row = db.execute("SELECT * FROM items WHERE id = ?", (keep_id,)).fetchone()
        if not primary_row:
            return {"ok": False, "message": "Primary item not found", "merged": 0}
        primary = dict(primary_row)

        target_ids = [keep_id, *merge_ids]
        marks = ",".join("?" for _ in target_ids)
        active_loans = db.execute(
            f"SELECT COUNT(*) AS c FROM checkouts "
            f"WHERE checked_in IS NULL AND item_id IN ({marks})",
            target_ids,
        ).fetchone()["c"]
        if active_loans > 1:
            return {
                "ok": False,
                "message": "Cannot merge items with multiple active loans",
                "merged": 0,
            }

        try:
            for merge_id in merge_ids:
                other_row = db.execute(
                    "SELECT * FROM items WHERE id = ?", (merge_id,)
                ).fetchone()
                if not other_row:
                    continue
                other = dict(other_row)

                updates = {
                    field: other[field]
                    for field in fillable
                    if field in other
                    and field in primary
                    and _merge_value_missing(primary.get(field))
                    and not _merge_value_missing(other.get(field))
                }
                if not primary.get("isbn") and other.get("isbn"):
                    updates["isbn"] = other["isbn"]

                if updates:
                    try:
                        updates = validate_item_fields(db, updates)
                    except ItemValueError as exc:
                        return {
                            "ok": False,
                            "message": f"Cannot merge \"{other['title']}\" (#{merge_id}): {exc}",
                            "item_id": merge_id,
                            "merged": 0,
                        }

                db.execute(
                    "UPDATE scan_log SET item_id = ? WHERE item_id = ?",
                    (keep_id, merge_id),
                )
                db.execute(
                    "UPDATE reading_log SET item_id = ? WHERE item_id = ?",
                    (keep_id, merge_id),
                )
                db.execute(
                    "UPDATE checkouts SET item_id = ? WHERE item_id = ?",
                    (keep_id, merge_id),
                )

                db.execute(
                    "INSERT OR IGNORE INTO item_tags (item_id, tag_id) "
                    "SELECT ?, tag_id FROM item_tags WHERE item_id = ?",
                    (keep_id, merge_id),
                )
                db.execute("DELETE FROM item_tags WHERE item_id = ?", (merge_id,))

                links = db.execute(
                    "SELECT item_a_id, item_b_id, link_type, created_at FROM item_links "
                    "WHERE item_a_id = ? OR item_b_id = ?",
                    (merge_id, merge_id),
                ).fetchall()
                for link in links:
                    item_a = keep_id if link["item_a_id"] == merge_id else link["item_a_id"]
                    item_b = keep_id if link["item_b_id"] == merge_id else link["item_b_id"]
                    if item_a == item_b:
                        continue
                    db.execute(
                        "INSERT OR IGNORE INTO item_links "
                        "(item_a_id, item_b_id, link_type, created_at) VALUES (?, ?, ?, ?)",
                        (item_a, item_b, link["link_type"], link["created_at"]),
                    )
                db.execute(
                    "DELETE FROM item_links WHERE item_a_id = ? OR item_b_id = ?",
                    (merge_id, merge_id),
                )

                # Delete first so transferring ISBN/UPC identity cannot collide
                # with the row that is being absorbed.
                db.execute("DELETE FROM items WHERE id = ?", (merge_id,))
                if updates:
                    update_item_fields(db, keep_id, updates)
                    primary = dict(
                        db.execute("SELECT * FROM items WHERE id = ?", (keep_id,)).fetchone()
                    )
                merged += 1
        except sqlite3.IntegrityError:
            db.rollback()
            return {
                "ok": False,
                "message": "Merge conflicts with existing catalogue data",
                "merged": 0,
            }

    if merged == 0:
        return {"ok": False, "message": "No matching items found", "merged": 0}
    return {"ok": True, "merged": merged}


# ---------------------------------------------------------------------------
# Simple request-boundary regressions displaced by the upstream merge
# ---------------------------------------------------------------------------
_remove_route("/items/{item_id}", "DELETE")


@items.router.delete("/items/{item_id}")
async def integrated_delete_item(
    item_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        row = db.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return JSONResponse(
                {"ok": False, "message": "Item not found"}, status_code=404
            )
        title = row["title"]
        db.execute("UPDATE scan_log SET item_id = NULL WHERE item_id = ?", (item_id,))
        cursor = db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        if cursor.rowcount != 1:
            return JSONResponse(
                {"ok": False, "message": "Delete failed"}, status_code=409
            )
    response = HTMLResponse('{"ok": true}', headers={"Content-Type": "application/json"})
    response.headers["HX-Trigger"] = items.items_common._toast_header(
        f"Deleted: {title[:50]}"
    )
    return response


_remove_route("/inventory/missing", "POST")


@items.router.post("/inventory/missing")
async def integrated_inventory_missing(
    request: Request,
    _=Depends(require_role("editor")),
):
    form = await request.form()
    try:
        location_id = int(form.get("location_id"))
    except (TypeError, ValueError):
        return HTMLResponse("Location not found", status_code=404)
    scanned_ids = str(form.get("scanned_ids") or "")
    scanned = {
        int(value)
        for value in scanned_ids.split(",")
        if value.strip().isdigit()
    }

    with get_db() as db:
        location = db.execute(
            "SELECT name FROM locations WHERE id = ?", (location_id,)
        ).fetchone()
        if not location:
            return HTMLResponse("Location not found", status_code=404)
        rows = db.execute(
            "SELECT id, title, authors, cover_path FROM items "
            "WHERE location_id = ? ORDER BY title",
            (location_id,),
        ).fetchall()

    missing = [dict(row) for row in rows if row["id"] not in scanned]
    html_parts = []
    if not missing:
        html_parts.append(
            f'<p class="text-sm text-shelf-success">All items at {location["name"]} accounted for!</p>'
        )
    else:
        html_parts.append(
            f'<p class="text-sm text-shelf-warning mb-3">{len(missing)} item(s) at {location["name"]} not scanned:</p>'
        )
        for item in missing:
            cover = (
                f'<img src="/covers/{item["id"]}.jpg" class="w-10 h-14 object-cover rounded" alt="">'
                if item["cover_path"]
                else '<div class="w-10 h-14 bg-shelf-hover rounded flex items-center justify-center text-shelf-muted text-xs">?</div>'
            )
            authors = (
                f'<p class="text-xs text-shelf-muted truncate">{item["authors"]}</p>'
                if item.get("authors")
                else ""
            )
            html_parts.append(
                f'<div class="bg-shelf-card rounded-lg border border-shelf-border p-3 flex items-center gap-3">'
                f'{cover}<div class="flex-1 min-w-0"><p class="font-medium text-sm truncate">'
                f'<a href="/item/{item["id"]}" class="hover:text-shelf-accent2">{item["title"] or "Untitled"}</a></p>{authors}</div>'
                f'<span class="text-xs px-2 py-1 rounded-full shrink-0 bg-shelf-error/20 text-shelf-error">missing</span></div>'
            )
    return HTMLResponse("\n".join(html_parts))


_remove_route("/igdb/test-key", "POST")


@items.router.post("/igdb/test-key")
async def integrated_test_igdb_key(
    request: Request,
    _=Depends(require_role("admin")),
):
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "message": "Invalid request body"}
    if not isinstance(data, dict):
        return {"ok": False, "message": "Invalid request body"}

    raw_client_id = data.get("client_id")
    raw_client_secret = data.get("client_secret")
    if raw_client_id is not None and not isinstance(raw_client_id, str):
        return {"ok": False, "message": "Invalid request body"}
    if raw_client_secret is not None and not isinstance(raw_client_secret, str):
        return {"ok": False, "message": "Invalid request body"}

    client_id = (raw_client_id or "").strip()
    client_secret = (raw_client_secret or "").strip()
    if not client_id or not client_secret:
        with get_db() as db:
            client_id = client_id or get_setting(db, "igdb_client_id")
            client_secret = client_secret or get_setting(db, "igdb_client_secret")
    if not client_id or not client_secret:
        return {
            "ok": False,
            "message": "Both Client ID and Client Secret are required",
        }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        return await igdb.test_credentials(client_id, client_secret, client)
