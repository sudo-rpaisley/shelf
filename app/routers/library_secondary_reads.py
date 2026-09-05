"""Apply first-class library visibility to secondary catalogue read surfaces."""

from __future__ import annotations

from collections import Counter, defaultdict

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_role
from app.config import MEDIA_TYPES, MUSIC_MEDIA_TYPES
from app.database import get_db
from app.routers import attention, collections, music, pages, series, series_detail, series_memberships
from app.routers import location_tree as location_router
from app.services import holdings, libraries
from app.services import location_tree as location_svc
from app.services.series_display import count_label, find_gaps as display_find_gaps, infer_series_unit


def _remove_route(router, path: str, method: str) -> None:
    method = method.upper()
    full_path = f"{router.prefix}{path}" if router.prefix else path
    router.routes[:] = [
        route
        for route in router.routes
        if not (
            getattr(route, "path", None) == full_path
            and method in (getattr(route, "methods", None) or set())
        )
    ]


def _user(request: Request) -> dict:
    return dict(request.state.user)


def _access(user: dict, *, alias: str = "i", minimum_role: str = "viewer"):
    return libraries.item_access_condition(
        user,
        item_alias=alias,
        minimum_role=minimum_role,
    )


def _item_allowed(request: Request, item_id: int, role: str = "viewer") -> bool:
    with get_db() as db:
        return libraries.has_item_role(db, _user(request), item_id, role)


# ---------------------------------------------------------------------------
# Series overview and detail
# ---------------------------------------------------------------------------

_original_series_page = series.series_page
_remove_route(series.router, "/series", "GET")


@series.router.get("/series")
async def library_series_page(
    request: Request,
    _=Depends(require_role("viewer")),
):
    response = await _original_series_page(request, _=request.state.user)
    context = getattr(response, "context", None)
    if not context:
        return response

    user = _user(request)
    ctx = dict(context)
    with get_db() as db:
        filtered_series = []
        for entry in ctx.get("series_list") or []:
            allowed = set(
                libraries.accessible_item_ids(
                    db,
                    user,
                    [item["id"] for item in entry.get("items", [])],
                )
            )
            items_in_scope = [
                item for item in entry.get("items", []) if int(item["id"]) in allowed
            ]
            if not items_in_scope:
                continue
            scoped = dict(entry)
            scoped["items"] = items_in_scope
            scoped["owned_count"] = sum(1 for item in items_in_scope if item["owned"])
            scoped["gaps"] = series.find_gaps(
                [item["series_position"] for item in items_in_scope]
            )
            filtered_series.append(scoped)
        ctx["series_list"] = sorted(
            filtered_series,
            key=lambda entry: (-len(entry["items"]), entry["name"].casefold()),
        )

        series._sync_item_series_memberships(db)
        access_sql, access_params = _access(user, alias="items")
        unassigned_where = (
            "NOT EXISTS (SELECT 1 FROM item_series s WHERE s.item_id = items.id) "
            f"AND media_type IN ({','.join('?' * len(series.UNASSIGNED_MEDIA_TYPES))}) "
            f"AND ({access_sql})"
        )
        ctx["unassigned_total"] = db.execute(
            f"SELECT COUNT(*) FROM items WHERE {unassigned_where}",
            [*series.UNASSIGNED_MEDIA_TYPES, *access_params],
        ).fetchone()[0]
        ctx["unassigned_items"] = [
            dict(row)
            for row in db.execute(
                "SELECT id, title, authors, cover_path, series_name, series_position, "
                f"owned, reading_status FROM items WHERE {unassigned_where} "
                "ORDER BY title COLLATE NOCASE LIMIT ?",
                [
                    *series.UNASSIGNED_MEDIA_TYPES,
                    *access_params,
                    series.UNASSIGNED_STRIP_CAP,
                ],
            ).fetchall()
        ]

    return request.app.state.templates.TemplateResponse(request, "series.html", ctx)


_original_series_detail = series_detail.series_detail_page
_remove_route(series.router, "/series/detail", "GET")


@series.router.get("/series/detail")
async def library_series_detail(
    request: Request,
    name: str = Query(...),
    media_type: str | None = Query(default=None),
    komga_series_id: str | None = Query(default=None),
    _=Depends(require_role("viewer")),
):
    response = await _original_series_detail(
        request,
        name=name,
        media_type=media_type,
        komga_series_id=komga_series_id,
        _=request.state.user,
    )
    context = getattr(response, "context", None)
    if not context:
        return response

    user = _user(request)
    ctx = dict(context)
    series_ctx = dict(ctx["series"])
    with get_db() as db:
        allowed = set(
            libraries.accessible_item_ids(
                db,
                user,
                [item["id"] for item in series_ctx.get("items", [])],
            )
        )
    scoped_items = [
        item for item in series_ctx.get("items", []) if int(item["id"]) in allowed
    ]
    if not scoped_items:
        raise HTTPException(status_code=404, detail="Series not found")

    unit = infer_series_unit(scoped_items)
    series_ctx["items"] = scoped_items
    series_ctx["item_count"] = len(scoped_items)
    series_ctx["count_label"] = count_label(len(scoped_items), unit)
    series_ctx["owned_count"] = sum(1 for item in scoped_items if item.get("owned"))
    series_ctx["wishlist_count"] = len(scoped_items) - series_ctx["owned_count"]
    series_ctx["gaps"] = display_find_gaps(
        item.get("series_position") for item in scoped_items
    )
    series_ctx["unit"] = unit
    media_types = {
        item.get("media_type") for item in scoped_items if item.get("media_type")
    }
    resolved = media_type or (next(iter(media_types)) if len(media_types) == 1 else None)
    series_ctx["media_type"] = resolved
    series_ctx["can_bulk_add"] = bool(
        not series_ctx.get("komga_series_id") and resolved == "comic"
    )
    ctx["series"] = series_ctx
    return request.app.state.templates.TemplateResponse(
        request, "series_detail.html", ctx
    )


_original_check_series = series.check_series
_remove_route(series.router, "/api/series/check", "GET")


@series.router.get("/api/series/check")
async def library_check_series(
    request: Request,
    name: str = "",
    _=Depends(require_role("viewer")),
):
    clean = name.strip()
    if not clean:
        return {"ok": False, "message": "Series name required"}
    user = _user(request)
    access_sql, access_params = _access(user)
    with get_db() as db:
        series._sync_item_series_memberships(db)
        local = db.execute(
            "SELECT i.title, i.owned, i.hardcover_book_id FROM item_series s "
            "JOIN items i ON i.id = s.item_id "
            f"WHERE s.series_name = ? COLLATE NOCASE AND ({access_sql})",
            [clean] + access_params,
        ).fetchall()
    if not local:
        return {"ok": False, "message": "Series not found"}

    result = await _original_check_series(clean, _=request.state.user)
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    by_hc = {row["hardcover_book_id"]: row for row in local if row["hardcover_book_id"]}
    by_title = {row["title"].casefold().strip(): row for row in local}
    books = []
    for book in result.get("books") or []:
        match = by_hc.get(book.get("hardcover_book_id")) or by_title.get(
            str(book.get("title") or "").casefold().strip()
        )
        status = "missing"
        if match:
            status = "owned" if match["owned"] else "wishlist"
        books.append({**book, "status": status})
    result = dict(result)
    result["books"] = books
    result["missing"] = sum(1 for book in books if book["status"] == "missing")
    return result


# ---------------------------------------------------------------------------
# Item-detail series rows loaded through HTMX
# ---------------------------------------------------------------------------

_remove_route(series.router, "/api/items/{item_id}/series-rows", "GET")


@series.router.get("/api/items/{item_id}/series-rows")
async def library_item_series_rows(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    user = _user(request)
    with get_db() as db:
        if not libraries.has_item_role(db, user, item_id, "viewer"):
            return HTMLResponse("Not found", status_code=404)
        context = series_memberships._item_and_series_context(db, item_id)
        if not context:
            return HTMLResponse("Not found", status_code=404)

        rows = []
        for row in context["series_rows"]:
            allowed = set(
                libraries.accessible_item_ids(
                    db, user, [item["id"] for item in row["items"]]
                )
            )
            members = [item for item in row["items"] if int(item["id"]) in allowed]
            if len(members) > 1:
                scoped = dict(row)
                scoped["items"] = members
                scoped["count"] = len(members)
                rows.append(scoped)
        context["series_rows"] = rows

        access_sql, access_params = _access(user)
        context["known_series_names"] = [
            row["series_name"]
            for row in db.execute(
                "SELECT s.series_name FROM item_series s JOIN items i ON i.id = s.item_id "
                f"WHERE {access_sql} GROUP BY s.series_name COLLATE NOCASE "
                "ORDER BY s.series_name COLLATE NOCASE",
                access_params,
            ).fetchall()
        ]
        context["can_edit"] = libraries.has_item_role(db, user, item_id, "editor")

    return request.app.state.templates.TemplateResponse(
        request,
        "fragments/item_series_rows.html",
        context,
    )


# ---------------------------------------------------------------------------
# Collections: definitions stay shared, item counts/previews are scoped.
# ---------------------------------------------------------------------------

_remove_route(collections.router, "/collections", "GET")


@collections.router.get("/collections")
async def library_collections_page(
    request: Request,
    _=Depends(require_role("viewer")),
):
    user = _user(request)
    access_sql, access_params = _access(user)
    cards = []
    with get_db() as db:
        collection_rows = db.execute(
            "SELECT id, name, description, created_at, updated_at "
            "FROM collections ORDER BY name COLLATE NOCASE"
        ).fetchall()
        for row in collection_rows:
            card = dict(row)
            card["item_count"] = db.execute(
                "SELECT COUNT(*) AS c FROM collection_items ci "
                "JOIN items i ON i.id = ci.item_id "
                f"WHERE ci.collection_id = ? AND ({access_sql})",
                [row["id"]] + access_params,
            ).fetchone()["c"]
            card["preview_items"] = db.execute(
                "SELECT i.id, i.title, i.cover_path, i.media_type "
                "FROM collection_items ci JOIN items i ON i.id = ci.item_id "
                f"WHERE ci.collection_id = ? AND ({access_sql}) "
                "ORDER BY ci.created_at DESC, i.id DESC LIMIT 4",
                [row["id"]] + access_params,
            ).fetchall()
            cards.append(card)
    return request.app.state.templates.TemplateResponse(
        request, "collections.html", {"collections": cards}
    )


# ---------------------------------------------------------------------------
# Hierarchical physical locations
# ---------------------------------------------------------------------------


def _scoped_location_nodes(db, user: dict) -> list[dict]:
    base = [dict(row) for row in location_svc.flattened_tree(db)]
    access_sql, access_params = _access(user)
    direct = {
        int(row["location_id"]): int(row["c"])
        for row in db.execute(
            "SELECT c.location_id, COUNT(*) AS c FROM item_copies c "
            "JOIN items i ON i.id = c.item_id "
            f"WHERE c.location_id IS NOT NULL AND ({access_sql}) GROUP BY c.location_id",
            access_params,
        ).fetchall()
    }
    children: dict[int | None, list[int]] = defaultdict(list)
    for node in base:
        children[node.get("parent_id")].append(int(node["id"]))
    totals: dict[int, int] = {}

    def total(node_id: int, seen: set[int]) -> int:
        if node_id in totals:
            return totals[node_id]
        if node_id in seen:
            return 0
        value = direct.get(node_id, 0) + sum(
            total(child, seen | {node_id}) for child in children.get(node_id, [])
        )
        totals[node_id] = value
        return value

    for node in base:
        node_id = int(node["id"])
        node["direct_copy_count"] = direct.get(node_id, 0)
        node["copy_count"] = total(node_id, set())
    return base


_remove_route(pages.router, "/locations", "GET")
_remove_route(pages.router, "/locations/{location_id}", "GET")


@pages.router.get("/locations")
async def library_locations_index(
    request: Request,
    _=Depends(require_role("viewer")),
):
    with get_db() as db:
        location_router._prepare(db)
        nodes = _scoped_location_nodes(db, _user(request))
    return request.app.state.templates.TemplateResponse(
        request,
        "locations.html",
        {"location_nodes": nodes, "selected_location": None, "error": ""},
    )


@pages.router.get("/locations/{location_id}")
async def library_location_detail(
    request: Request,
    location_id: int,
    error: str = Query(""),
    _=Depends(require_role("viewer")),
):
    user = _user(request)
    with get_db() as db:
        location_router._prepare(db)
        selected = db.execute(
            "SELECT * FROM location_nodes WHERE id = ?", (location_id,)
        ).fetchone()
        if not selected:
            return location_router._redirect(error="missing")
        nodes = _scoped_location_nodes(db, user)
        breadcrumb = location_svc.location_path(db, location_id)
        children = db.execute(
            "SELECT id, name, sort_order FROM location_nodes WHERE parent_id = ? "
            "ORDER BY sort_order, name COLLATE NOCASE",
            (location_id,),
        ).fetchall()
        copies = location_svc.direct_copies(db, location_id)
        allowed = set(
            libraries.accessible_item_ids(
                db, user, [copy["item_id"] for copy in copies]
            )
        )
        copies = [copy for copy in copies if int(copy["item_id"]) in allowed]
    return request.app.state.templates.TemplateResponse(
        request,
        "locations.html",
        {
            "location_nodes": nodes,
            "selected_location": dict(selected),
            "breadcrumb": breadcrumb,
            "child_locations": children,
            "location_copies": copies,
            "error": error,
        },
    )


# ---------------------------------------------------------------------------
# Attention / data-quality view
# ---------------------------------------------------------------------------

_remove_route(pages.router, "/attention", "GET")


@pages.router.get("/attention")
async def library_attention_page(
    request: Request,
    category: str = Query("cover"),
    _=Depends(require_role("viewer")),
):
    if category not in attention._CATEGORIES:
        category = "cover"
    user = _user(request)
    access_sql, access_params = _access(user)

    with get_db() as db:
        holdings.ensure_foundation(db)
        counts = {}
        for key in attention._CATEGORIES:
            where, where_params = attention._category_where(key)
            counts[key] = db.execute(
                "SELECT COUNT(DISTINCT i.id) AS c FROM items i "
                "LEFT JOIN periodical_issues pi ON pi.item_id = i.id "
                f"WHERE ({where}) AND ({access_sql})",
                list(where_params) + access_params,
            ).fetchone()["c"]

        where, where_params = attention._category_where(category)
        rows = db.execute(
            "SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, "
            "i.publish_year, i.location_id, l.name AS location_name, "
            "pi.issue_number, pi.issue_date FROM items i "
            "LEFT JOIN locations l ON l.id = i.location_id "
            "LEFT JOIN periodical_issues pi ON pi.item_id = i.id "
            f"WHERE ({where}) AND ({access_sql}) "
            "ORDER BY i.title COLLATE NOCASE, i.id LIMIT 200",
            list(where_params) + access_params,
        ).fetchall()

    categories = [
        {"key": key, **definition, "count": counts[key]}
        for key, definition in attention._CATEGORIES.items()
    ]
    return request.app.state.templates.TemplateResponse(
        request,
        "attention.html",
        {
            "categories": categories,
            "active_category": category,
            "active_definition": attention._CATEGORIES[category],
            "attention_items": rows,
            "result_truncated": len(rows) == 200,
        },
    )


# ---------------------------------------------------------------------------
# Music item-specific read helpers
# ---------------------------------------------------------------------------

_original_music_page = music.music_page
_original_music_detail = music.music_item_detail
_original_music_edit = music.edit_music_copy
_original_discogs_match = music.match_discogs_release

_remove_route(music.router, "/music/add", "GET")
_remove_route(music.router, "/api/music/items/{item_id}/detail", "GET")
_remove_route(music.router, "/music/item/{item_id}/edit", "GET")
_remove_route(music.router, "/music/item/{item_id}/discogs", "GET")


@music.router.get("/music/add")
async def library_music_page(
    request: Request,
    q: str = Query(""),
    artist: str = Query(""),
    barcode: str = Query(""),
    catalog_number: str = Query(""),
    item_id: int | None = Query(None),
    _=Depends(require_role("viewer")),
):
    if item_id is not None and not _item_allowed(request, item_id, "viewer"):
        return RedirectResponse("/music", status_code=303)
    return await _original_music_page(
        request,
        q=q,
        artist=artist,
        barcode=barcode,
        catalog_number=catalog_number,
        item_id=item_id,
        _=request.state.user,
    )


@music.router.get("/api/music/items/{item_id}/detail")
async def library_music_item_detail(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    if not _item_allowed(request, item_id, "viewer"):
        return HTMLResponse("")
    return await _original_music_detail(request, item_id, _=request.state.user)


@music.router.get("/music/item/{item_id}/edit")
async def library_music_edit(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):
    if not _item_allowed(request, item_id, "editor"):
        return RedirectResponse("/music", status_code=303)
    return await _original_music_edit(request, item_id, _=request.state.user)


@music.router.get("/music/item/{item_id}/discogs")
async def library_discogs_match(
    request: Request,
    item_id: int,
    q: str = Query(""),
    artist: str = Query(""),
    barcode: str = Query(""),
    catalog_number: str = Query(""),
    _=Depends(require_role("viewer")),
):
    if not _item_allowed(request, item_id, "editor"):
        return RedirectResponse("/music", status_code=303)
    return await _original_discogs_match(
        request,
        item_id,
        q=q,
        artist=artist,
        barcode=barcode,
        catalog_number=catalog_number,
        _=request.state.user,
    )
