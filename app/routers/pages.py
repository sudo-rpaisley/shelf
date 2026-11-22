from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from app import browse_filters, features, i18n, nav
from app.auth import require_role
from app.config import MEDIA_FAMILIES, MUSIC_MEDIA_TYPES
from app.features import require_feature
from app.config import (
    MEDIA_TYPES, DEFAULT_PAGE_SIZE, MAX_PAGE, BOOK_MEDIA_TYPES, STATUS_MEDIA_TYPES,
    status_labels, READ_LABELS, WATCH_LABELS, PLAY_LABELS,
)
from app.services.synopsis import SYNOPSIS_MEDIA_TYPES
from app.currency import get_currency
from app.services import author_index
from app.services import collection_value
from app.services import browse_counts
from app.services import lists
from app.services import isbn as isbn_svc
from app.services import upc as upc_svc
from app.services import tags as tags_svc
from app.services import price_alerts
from app.services import buy_links as buy_links_svc
from app.services import release_radar
from app.services import wrapups
from app.database import get_db, get_setting, get_game_platforms
from app.routers.items_common import SORT_OPTIONS
from app.routers.series import find_gaps
from app.services import user_state, media_groups, libraries
from app.routers import library_access
from app.services import item_template, item_write
from app.services import locations as location_svc
from app.services.home_dashboard import dashboard_summary
from app.services.catalogue_security import require_item_role, require_catalogue_role
from app.database import ensure_periodical_schema

router = APIRouter()


@router.get("/")
async def index(request: Request, _=Depends(require_catalogue_role("viewer"))):
    """Shelf's lightweight landing page, separate from the advanced browser."""
    with get_db() as db:
        type_counts = {
            row["media_type"]: row["c"]
            for row in db.execute(
                "SELECT media_type, COUNT(*) AS c FROM items_live GROUP BY media_type"
            ).fetchall()
        }
        total_items = db.execute("SELECT COUNT(*) AS c FROM items_live").fetchone()["c"]
        recent_items = db.execute(
            f"SELECT i.id, i.title, i.authors, i.media_type, i.cover_path, i.created_at, "
            f"{lists.WISHLISTED_SQL} AS wishlisted "
            "FROM items_live i ORDER BY i.created_at DESC, i.id DESC LIMIT 8"
        ).fetchall()
        summary = dashboard_summary(db, recent_limit=8)
        # Browse's owned=0 is the named actor's personal wishlist, which may
        # overlap ownership. Keep the household acquisition summary separate.
        personal_wishlist, personal_params = browse_filters.personal_wishlist_sql(int(request.state.user["id"]))
        personal_wishlist_count = db.execute(
            f"SELECT COUNT(*) FROM items_live i WHERE {personal_wishlist} = 1", personal_params,
        ).fetchone()[0]

    families = []
    for key, family in MEDIA_FAMILIES.items():
        families.append({
            "key": key,
            "label": family["label"],
            "count": sum(type_counts.get(media_type, 0) for media_type in family["types"]),
            # Music has a release/pressing-aware catalogue of its own. Other
            # families enter Browse with a family filter set.
            "href": ("/music" if key == "music" and features.feature_enabled("music")
                     else f"/browse?media_family_filter={key}"),
        })

    visible_nav = nav.visible_tabs(request.state.user)
    return request.app.state.templates.TemplateResponse(
        request,
        "home.html",
        {
            **summary,
            "media_type_labels": MEDIA_TYPES,
            "families": families,
            "total_items": total_items,
            "recent_items": recent_items,
            "personal_wishlist_count": personal_wishlist_count,
            "media_types": MEDIA_TYPES,
            "media_type_rows": summary["media_types"],
            "music_media_types": MUSIC_MEDIA_TYPES,
            "intake_available": any(tab["key"] == "intake" for tab in visible_nav),
        },
    )


@router.get("/search")
async def global_search(
    query: str = Query("", max_length=200),
    _=Depends(require_catalogue_role("viewer")),
):
    """Small global search endpoint used by the persistent navigation bar."""
    value = query.strip()
    target = "/browse"
    if value:
        target += "?" + urlencode({"q": value})
    return RedirectResponse(url=target, status_code=303)


@router.get("/browse")
async def browse(
    request: Request,
    _=Depends(require_catalogue_role("viewer")),
):
    """The Browse page — first paint of the item grid and its filters.

    Filter values are read from the query string via the registry rather than
    declared as parameters here, exactly as `/api/search` does: a filter added
    to `app/browse_filters.py` needs no change in this signature. The dropdown
    counts come from the same `browse_counts.filter_counts` helper `/api/search`
    uses, so the first paint and the first OOB swap cannot disagree.
    """
    values = browse_filters.values_from(request.query_params)
    values["q"] = values["q"][:200]
    user_id = int(request.state.user["id"])
    where, params = browse_filters.build_where(values)

    with get_db() as db:
        user_state.ensure_schema(db)
        _, order_clause = SORT_OPTIONS.get(values["sort"], SORT_OPTIONS["newest"])

        items, total_filtered, display_total = library_access.fetch_page(
            db,
            user_id,
            where,
            params,
            order_clause,
            limit=DEFAULT_PAGE_SIZE,
            offset=0,
            values=values,
        )
        tags_by_item = tags_svc.tags_for_items(db, [row["id"] for row in items])

        series_names = [
            row["series_name"]
            for row in db.execute(
                "SELECT DISTINCT series_name FROM items_live "
                "WHERE series_name IS NOT NULL AND TRIM(series_name) != '' "
                "ORDER BY series_name COLLATE NOCASE"
            ).fetchall()
        ]

        # Cross-filter dropdown counts — `locations`, `type_counts`,
        # `location_counts`, `reading_status_counts`, `owned_count`,
        # `wishlist_count` and `filtered_total` all come from here.
        counts = browse_counts.filter_counts(db, values, total_filtered)

        # Deliberately still global: lending is a shared physical-catalogue fact.
        lent_out_count = db.execute(
            "SELECT COUNT(DISTINCT c.item_id) as c FROM checkouts c "
            "JOIN items_live i ON i.id = c.item_id WHERE c.checked_in IS NULL"
        ).fetchone()["c"]

        from app.services.tags import get_all_tags
        all_tags = get_all_tags(db)

        item_languages = [
            row["language"]
            for row in db.execute(
                "SELECT DISTINCT language FROM items_live "
                "WHERE language IS NOT NULL AND language != '' ORDER BY language"
            ).fetchall()
        ]

        has_more = len(items) < display_total

        # The author filter's chip shows a name, not an id (#117b).
        author_filter_label = (
            author_index.name_for(db, values["author_filter"])
            if values["author_filter"] else None
        )

        load_more_url = "/api/search?" + browse_filters.querystring(
            values, extra=["page=2"]
        )

    ctx = {
        "items": items,
        "tags_by_item": tags_by_item,
        "media_types": MEDIA_TYPES,
        "media_families": MEDIA_FAMILIES,
        "series_names": series_names,
        "all_tags": all_tags,
        "lent_out_count": lent_out_count,
        "item_languages": item_languages,
        "has_more": has_more,
        "has_filters": browse_filters.has_active_filters(values),
        "load_more_url": load_more_url,
        "seven_days_ago": (datetime.now(tz=None) - timedelta(days=7)).strftime("%Y-%m-%d"),
        "initial_query": values["q"],
        "author_filter_label": author_filter_label,
        "initial_filters": {name: values[name] for name in browse_filters.FILTER_NAMES},
    }
    ctx.update(counts)

    return request.app.state.templates.TemplateResponse(
        request,
        "browse.html",
        ctx,
    )


@router.get("/discover")
async def discover(request: Request, _=Depends(require_catalogue_role("viewer")),
                   __=Depends(require_feature("hardcover"))):
    with get_db() as db:
        has_hardcover = bool(get_setting(db, "hardcover_token"))
    return request.app.state.templates.TemplateResponse(
        request, "discover.html", {"has_hardcover": has_hardcover},
    )


@router.get("/scan")
async def scan(
    request: Request,
    add: str = "",
    from_: str = Query("", alias="from"),
    mode: str = "",
    code: str = "",
    _=Depends(require_catalogue_role("editor")),
):
    """The scan surface. `?add=manual` opens the manual entry panel (#120),
    and `?from={id}` prefills it from an existing item.

    `from` is typed `str` and parsed here rather than annotated `int | None`.
    FastAPI validates an annotation *before* the handler runs, so a typed int
    would return 422 on `?from=notanumber` and the panel could never degrade
    gracefully. `item_detail` below uses the same tolerant idiom. Missing,
    malformed, zero, negative and nonexistent ids all mean the same thing: no
    prefill, a normal 200 page. Never a 404.
    """
    try:
        candidate = int(from_)
    except (TypeError, ValueError):
        candidate = 0

    # An audit report's Add link seeds Add mode and the code. Honoured only
    # as a pair: mode exactly "add" and a code of at most 64 characters.
    seed_mode, seed_code = ("add", code) if mode == "add" and len(code) <= 64 else ("", "")

    with get_db() as db:
        locations = db.execute(
            "SELECT * FROM locations ORDER BY sort_order, name"
        ).fetchall()
        location_nodes = location_svc.location_tree(db)
        game_platforms = get_game_platforms(db)
        borrowers = db.execute(
            "SELECT * FROM borrowers ORDER BY name"
        ).fetchall()
        # The same field set the copy-template endpoint serves the in-form
        # picker, read through the one helper both share.
        #
        # NULL columns are normalized to "" here rather than in the helper:
        # the endpoint serves JSON, where `null` is the honest answer and the
        # picker's JS skips it (`applyTemplate`). A template has no such
        # guard — Jinja renders None as the text "None", which reached the
        # Series and Publisher inputs and made the Year input unparseable.
        manual_prefill = {
            name: ("" if value is None else value)
            for name, value in (
                (item_template.copyable_fields(db, candidate) if candidate > 0 else None)
                or {}
            ).items()
        }
    return request.app.state.templates.TemplateResponse(
        request,
        "scan.html",
        {"media_types": MEDIA_TYPES, "game_platforms": game_platforms,
         "locations": locations, "location_nodes": location_nodes, "borrowers": borrowers,
         "manual_open": add == "manual", "manual_prefill": manual_prefill,
         "off_scan_modes": features.disabled_scan_modes(),
         "seed_mode": seed_mode, "seed_code": seed_code,
         "status_label_sets": _status_label_sets()},
    )


def _status_label_sets():
    """The Scan card's default-status words per media type: `auto` gets the
    neutral set, and every status-capable type its own. The keys other than
    `auto` are the types the control is enabled for."""
    def words(labels):
        return [i18n.gettext(labels.want), i18n.gettext(labels.doing), i18n.gettext(labels.done)]
    sets = {t: words(status_labels(t)) for t in sorted(STATUS_MEDIA_TYPES)}
    return {"auto": words(status_labels(None)), **sets}


@router.get("/intake")
async def intake(request: Request, _=Depends(require_catalogue_role("editor")),
                 __=Depends(require_feature("intake"))):
    """Shelf-photo bulk intake page."""
    from app.database import get_all_settings
    with get_db() as db:
        locations = db.execute(
            "SELECT * FROM locations ORDER BY sort_order, name"
        ).fetchall()
        app_settings = get_all_settings(db)
    provider = app_settings.get("vision_provider") or ""
    return request.app.state.templates.TemplateResponse(
        request,
        "intake.html",
        {"locations": locations, "vision_provider": provider,
         "media_types": MEDIA_TYPES},
    )


@router.get("/item/{item_id}")
async def item_detail(
    request: Request,
    item_id: int,
    from_: str = Query("", alias="from"),
    _=Depends(require_catalogue_role("viewer")),
):
    back = nav.back_target(from_)
    if not -(2**63) <= item_id < 2**63:
        return RedirectResponse(url="/browse", status_code=307)
    with get_db() as db:
        item = db.execute(
            f"SELECT i.*, l.name as location_name, "
            f"{lists.WISHLISTED_SQL} AS wishlisted "
            "FROM items_live i "
            "LEFT JOIN locations l ON i.location_id = l.id "
            "WHERE i.id = ?",
            (item_id,),
        ).fetchone()
        if not item:
            return RedirectResponse(url="/browse")

        # Checkout info
        current_checkout = db.execute(
            "SELECT c.*, b.name as borrower_name FROM checkouts c "
            "JOIN borrowers b ON c.borrower_id = b.id "
            "WHERE c.item_id = ? AND c.checked_in IS NULL",
            (item_id,),
        ).fetchone()
        checkout_history = db.execute(
            "SELECT c.*, b.name as borrower_name FROM checkouts c "
            "JOIN borrowers b ON c.borrower_id = b.id "
            "WHERE c.item_id = ? ORDER BY c.created_at DESC LIMIT 10",
            (item_id,),
        ).fetchall()
        borrowers = db.execute("SELECT * FROM borrowers ORDER BY name").fetchall()

        # Related media is a transitive group: A↔B↔C means every member sees
        # the complete set, not only its immediate item_links neighbours.
        linked_items = media_groups.related_items(
            db, item_id, visibility_sql="i.id IN (SELECT id FROM items_live)")
        # Native "Also available as" asserts format equivalence, unlike the
        # fork's separately labelled transitive Related media panel.
        format_linked_items = db.execute(
            "SELECT i.* FROM item_links il JOIN items_live i "
            "ON i.id = CASE WHEN il.item_a_id = ? THEN il.item_b_id ELSE il.item_a_id END "
            "WHERE (il.item_a_id = ? OR il.item_b_id = ?) AND il.link_type = 'format'",
            (item_id, item_id, item_id)).fetchall()

        # ABS playback URLs — for this item and for linked formats, so a
        # physical copy's page can deep-link straight into Audiobookshelf
        abs_url = None
        linked_abs_items = []
        abs_url_val = get_setting(db, "abs_url")
        # The browser-facing root, read once here rather than inside
        # get_playback_url: the comprehension below calls it per linked item,
        # and a lookup there would open a nested connection each time.
        abs_public_url_val = get_setting(db, "abs_public_url")
        if abs_url_val:
            from app.services.audiobookshelf import get_playback_url
            if item["abs_id"]:
                abs_url = get_playback_url(abs_url_val, item["abs_id"], abs_public_url_val)
            linked_abs_items = [
                {"id": li["id"], "media_type": li["media_type"],
                 "abs_url": get_playback_url(abs_url_val, li["abs_id"], abs_public_url_val)}
                for li in linked_items if li["abs_id"]
            ]

        # Komga browser URLs use the public root when configured,
        # while all server-side API traffic continues to use komga_url.
        komga_url = None
        linked_komga_items = []
        komga_url_val = get_setting(db, "komga_url")
        if komga_url_val:
            from app.services.komga import get_browser_url
            if item["komga_id"]:
                komga_url = get_browser_url(komga_url_val, item["komga_id"])
            linked_komga_items = [
                {"id": li["id"], "media_type": li["media_type"],
                 "komga_url": get_browser_url(komga_url_val, li["komga_id"])}
                for li in linked_items if li["komga_id"]
            ]

        # RomM browser URLs use the public root when configured while sync
        # traffic continues to use romm_url.
        romm_url = None
        linked_romm_items = []
        romm_url_val = get_setting(db, "romm_url")
        if romm_url_val:
            from app.services.romm import get_browser_url as get_romm_browser_url
            if item["romm_id"]:
                romm_url = get_romm_browser_url(romm_url_val, item["romm_id"])
            linked_romm_items = [
                {"id": li["id"], "media_type": li["media_type"],
                 "romm_url": get_romm_browser_url(romm_url_val, li["romm_id"])}
                for li in linked_items if li["romm_id"]
            ]

        # Hardcover token check
        has_hardcover = bool(get_setting(db, "hardcover_token"))

        game_platforms = get_game_platforms(db)
        from app.services.romm import get_platform_icon_kind, get_platform_icon_urls
        romm_platform_icon_urls = (
            get_platform_icon_urls(romm_url_val) if romm_url_val else {}
        )

        # Enrich related rows once for the unified Related media panel. Provider
        # deep links still use each integration's public/browser URL setting.
        abs_link_map = {entry["id"]: entry["abs_url"] for entry in linked_abs_items}
        komga_link_map = {entry["id"]: entry["komga_url"] for entry in linked_komga_items}
        romm_link_map = {entry["id"]: entry["romm_url"] for entry in linked_romm_items}
        from app.routers.related import manual_unlink_parent_ids

        actor_user = dict(request.state.user)
        can_edit_item = libraries.has_item_role(db, actor_user, item_id, "editor")
        related_media = []
        for related_item in linked_items:
            data = dict(related_item)
            data["media_label"] = MEDIA_TYPES.get(data["media_type"], data["media_type"])
            data["platform_label"] = (
                game_platforms.get(data["platform"], data["platform"])
                if data.get("platform") else None
            )
            data["platform_icon_url"] = (
                romm_platform_icon_urls.get(data["platform"])
                if data.get("platform") else None
            )
            data["platform_icon_kind"] = (
                get_platform_icon_kind(data["platform"])
                if data.get("platform") else None
            )
            # A related item may legitimately be represented in more than one
            # external service. Keep every deep link rather than choosing the first.
            data["provider_links"] = []
            if data["id"] in abs_link_map:
                data["provider_links"].append({
                    "name": "Audiobookshelf", "url": abs_link_map[data["id"]]
                })
            if data["id"] in komga_link_map:
                data["provider_links"].append({
                    "name": "Komga", "url": komga_link_map[data["id"]]
                })
            if data["id"] in romm_link_map:
                data["provider_links"].append({
                    "name": "RomM", "url": romm_link_map[data["id"]]
                })
            # Preserve the old singular fields for any downstream template/plugin
            # code while the built-in UI consumes provider_links.
            data["provider_url"] = (
                data["provider_links"][0]["url"] if data["provider_links"] else None
            )
            data["provider_name"] = (
                data["provider_links"][0]["name"] if data["provider_links"] else None
            )
            data["manual_linked"] = media_groups.has_manual_group_edge(
                db, item_id, data["id"]
            )
            data["can_edit_item"] = libraries.has_item_role(db, actor_user, data["id"], "editor")
            data["can_unlink"] = False
            if can_edit_item and data["can_edit_item"] and data["manual_linked"]:
                effect_parents = manual_unlink_parent_ids(db, item_id, data["id"])
                data["can_unlink"] = bool(effect_parents) and all(
                    libraries.has_item_role(db, actor_user, parent_id, "editor")
                    for parent_id in effect_parents
                )
            related_media.append(data)

        all_group_items = [dict(item)] + [dict(row) for row in linked_items]
        related_formats = []
        related_game_platforms = []
        seen_game_platforms: set[str] = set()
        for group_item in all_group_items:
            label = MEDIA_TYPES.get(group_item["media_type"], group_item["media_type"])
            if label not in related_formats:
                related_formats.append(label)
            if (
                group_item["media_type"] in ("video_game", "digital_game")
                and group_item.get("platform")
            ):
                platform_slug = group_item["platform"]
                platform_label = game_platforms.get(platform_slug, platform_slug)
                if platform_slug not in seen_game_platforms:
                    seen_game_platforms.add(platform_slug)
                    related_game_platforms.append({
                        "slug": platform_slug,
                        "label": platform_label,
                        "icon_url": romm_platform_icon_urls.get(platform_slug),
                        "icon_kind": get_platform_icon_kind(platform_slug),
                    })

        # Every physical copy, not just the primary. `items.location_id` is
        # the compatibility seam and names only one place; a merged item can
        # legitimately have copies in two (issue #116).
        # The copies block's controls build their location <select> from this;
        # a fragment re-rendered without it loses every picker. There is no
        # shared all_locations() helper — four routes inline this same query.
        locations = db.execute(
            "SELECT * FROM locations ORDER BY sort_order, name"
        ).fetchall()
        copy_locations = location_svc.location_tree(db)
        from app.routers.item_copies import copy_context

        physical_context = copy_context(db, item_id)

        from app.services.tags import get_item_tags, get_all_tags
        item_tags = get_item_tags(db, item_id)
        all_tags = get_all_tags(db, media_type=item["media_type"])

        from app.routers.collections import item_collection_context
        collection_context = item_collection_context(db, item_id)

        user_id = int(request.state.user["id"])
        reading_history = user_state.get_reading_history(db, user_id, item_id)
        personal_state = user_state.get_state(db, user_id, item_id)

        item_authors = author_index.authors_for(db, item_id)

        # Series progress from two labelled sources: local siblings and the
        # Hardcover series_meta row. Never blended into one number — see
        # .devdocs plan §4. NOCASE identity, matching /api/series/check, the
        # series_meta key, and (since this branch) /series's own grouping.
        series_progress = None
        if item["series_name"] and item["series_name"].strip():
            siblings = db.execute(
                "SELECT owned, series_position FROM items_live "
                "WHERE series_name = ? COLLATE NOCASE",
                (item["series_name"],),
            ).fetchall()
            positions = [r["series_position"] for r in siblings]
            whole = [int(p) for p in positions
                     if p is not None and float(p).is_integer() and float(p) >= 1]
            meta = db.execute(
                "SELECT hc_total, hc_missing, hc_checked_at FROM series_meta "
                "WHERE name = ? COLLATE NOCASE",
                (item["series_name"],),
            ).fetchone()
            series_progress = {
                "count": len(siblings),
                "owned": sum(1 for r in siblings if r["owned"]),
                "top": max(whole) if whole else None,
                "gaps": find_gaps(positions),
                "hc_total": meta["hc_total"] if meta else None,
            }
        # The list-price line reads history only for the live item on screen;
        # the template also gates it on feature_on('price_alerts').
        price_line = price_alerts.price_line(db, item_id) if item["wishlisted"] else None
        buy_links = buy_links_svc.for_item(db, item) if item["wishlisted"] else []

    return request.app.state.templates.TemplateResponse(
        request,
        "item_detail.html",
        {
            **{key: value for key, value in physical_context.items()
               if key not in {"item", "locations"}},
            "item": item,
            "item_id": item_id,
            "back": back,
            "copies": physical_context["copies"],
            "locations": locations,
            "copy_locations": copy_locations,
            "can_edit_item": can_edit_item,
            "personal_state": personal_state,
            "personal_status_labels": user_state.status_labels(item["media_type"]),
            "item_tags": item_tags,
            "all_tags": all_tags,
            "item_collections": collection_context["item_collections"],
            "available_collections": collection_context["available_collections"],
            "media_types": MEDIA_TYPES,
            "book_media_types": BOOK_MEDIA_TYPES,
            "status_media_types": STATUS_MEDIA_TYPES,
            "synopsis_media_types": SYNOPSIS_MEDIA_TYPES,
            "game_platforms": game_platforms,
            "has_hardcover": has_hardcover,
            "current_checkout": current_checkout,
            "checkout_history": checkout_history,
            "borrowers": borrowers,
            "now_date": date.today().isoformat(),
            "linked_items": linked_items,
            "format_linked_items": format_linked_items,
            "related_media": related_media,
            "related_formats": related_formats,
            "related_game_platforms": related_game_platforms,
            "linked_abs_items": linked_abs_items,
            "abs_url": abs_url,
            "linked_komga_items": linked_komga_items,
            "komga_url": komga_url,
            "linked_romm_items": linked_romm_items,
            "romm_url": romm_url,
            "reading_history": reading_history,
            "series_progress": series_progress,
            "item_authors": item_authors,
            "price_line": price_line,
            "buy_links": buy_links,
        },
    )


@router.get("/item/{item_id}/edit")
async def item_edit(
    request: Request,
    item_id: int,
    from_: str = Query("", alias="from"),
    error: str | None = Query(None),
    trashed: int | None = Query(None),
    _=Depends(require_catalogue_role("editor")),
):
    back = nav.back_target(from_)
    trashed_title = None
    with get_db() as db:
        try:
            require_item_role(db, item_id, "editor")
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            # Missing, hidden and Trash use the same native GET destination.
            # Mutation and private-state endpoints retain their 404 contracts.
            return RedirectResponse(url="/browse", status_code=307)
        item = db.execute(
            f"SELECT i.*, {lists.WISHLISTED_SQL} AS wishlisted FROM items_live i WHERE i.id = ?",
            (item_id,),
        ).fetchone()
        if not item:
            return RedirectResponse(url="/browse")
        # An identifier_in_trash refusal names the blocking row by id; the
        # title is resolved here and escaped by the template, so nothing the
        # user typed is ever echoed back through the query string (G58).
        # FastAPI rejects a non-integer `trashed`, so no parsing is needed.
        if trashed is not None:
            trashed_title = item_write.trashed_title(db, trashed)
        locations = db.execute(
            "SELECT * FROM locations ORDER BY sort_order, name"
        ).fetchall()
        game_platforms = get_game_platforms(db)
        item_tags = tags_svc.get_item_tags(db, item_id)
        all_tags = tags_svc.get_all_tags(db, media_type=item["media_type"])
        periodical_issue = None
        if item["media_type"] == "magazine":
            ensure_periodical_schema(db)
            periodical_issue = db.execute(
                "SELECT barcode_ean, barcode_supplement FROM periodical_issues WHERE item_id = ?", (item_id,)
            ).fetchone()
    # #87 T2: the edit funnel now saves a legacy checksum-invalid isbn/upc
    # unchanged rather than bouncing every save on that row (T1). Surface
    # that silently-kept state to the editor instead of leaving it invisible.
    isbn_invalid = bool(item["isbn"]) and isbn_svc.canonical_isbn_pair(item["isbn"]) is None
    upc_invalid = bool(item["upc"]) and not upc_svc.canonical_retail_barcode(item["upc"])[0]
    return request.app.state.templates.TemplateResponse(
        request,
        "item_edit.html",
        {"item": item, "back": back, "media_types": MEDIA_TYPES, "game_platforms": game_platforms,
         "locations": locations, "error": error,
         "trashed_title": trashed_title,
         "periodical_issue": periodical_issue,
         "item_id": item_id, "item_tags": item_tags, "all_tags": all_tags,
         "isbn_invalid": isbn_invalid, "upc_invalid": upc_invalid},
    )


def _today() -> date:
    """The date the Stats pages read — one seam, so a test can pin January."""
    return date.today()


@router.get("/stats")
async def stats(request: Request, _=Depends(require_catalogue_role("viewer")),
                __=Depends(require_feature("stats"))):
    with get_db() as db:
        user_state.ensure_schema(db)
        by_type = db.execute(
            "SELECT media_type, COUNT(*) as c FROM items_live GROUP BY media_type ORDER BY c DESC"
        ).fetchall()
        by_location = db.execute(
            "SELECT COALESCE(l.name, 'Unassigned') as name, COUNT(*) as c "
            "FROM items_live i LEFT JOIN locations l ON i.location_id = l.id "
            "GROUP BY l.name ORDER BY c DESC"
        ).fetchall()
        total = db.execute("SELECT COUNT(*) as c FROM items_live").fetchone()["c"]
        stats_wishlist = db.execute(
            f"SELECT COUNT(*) as c FROM items_live i WHERE {lists.WISHLISTED_SQL}"
        ).fetchone()["c"]
        stats_owned = db.execute(
            "SELECT COUNT(*) as c FROM items_live WHERE owned = 1"
        ).fetchone()["c"]
        with_covers = db.execute(
            "SELECT COUNT(*) as c FROM items_live WHERE cover_path IS NOT NULL"
        ).fetchone()["c"]
        without_isbn = db.execute(
            "SELECT COUNT(*) as c FROM items_live WHERE isbn IS NULL"
        ).fetchone()["c"]
        recent = db.execute(
            "SELECT i.*, l.name as location_name FROM items_live i "
            "LEFT JOIN locations l ON i.location_id = l.id "
            "WHERE i.created_at >= datetime('now', '-30 days') "
            "ORDER BY i.created_at DESC LIMIT 20"
        ).fetchall()

        # The scoped live view already overlays only the actor's private status;
        # absent or cleared personal state never falls back to the household.
        read_by_year = db.execute(
            "SELECT substr(date_finished, 1, 4) AS y, media_type, COUNT(*) AS c FROM items_live "
            "WHERE reading_status = 'read' AND date_finished IS NOT NULL "
            "GROUP BY y, media_type ORDER BY y"
        ).fetchall()
        growth_rows = db.execute(
            "SELECT substr(created_at, 1, 7) as m, COUNT(*) as c FROM items_live "
            "GROUP BY m ORDER BY m"
        ).fetchall()
        author_rows = db.execute(
            "SELECT authors, COUNT(*) as c FROM items_live "
            "WHERE authors IS NOT NULL AND TRIM(authors) != '' GROUP BY authors"
        ).fetchall()
        # Historical totals are global and have no retained library provenance.
        # Do not expose another library's past values via the scoped stats page.
        valuation_rows = db.execute(
            "SELECT substr(created_at, 1, 10) as d, total_value FROM valuation_history "
            "ORDER BY created_at"
        ).fetchall() if request.state.user.get("account_role", request.state.user.get("role")) == "admin" else []
        # The valuation concerns what you own (#125) — owned = 1.
        current_value = collection_value.owned_total(db)

    today = _today()
    current_year = str(today.year)

    by_key: dict[str, dict[str, int]] = {}
    book_by_year: dict[str, int] = {}
    for r in read_by_year:
        key = status_labels(r["media_type"]).key
        by_key.setdefault(key, {})
        by_key[key][r["y"]] = by_key[key].get(r["y"], 0) + r["c"]
        if r["media_type"] in BOOK_MEDIA_TYPES:
            book_by_year[r["y"]] = book_by_year.get(r["y"], 0) + r["c"]

    read_pairs = sorted(book_by_year.items())
    finished_this_year = [
        (by_key.get(k, {}).get(current_year, 0), labels.done_lower)
        for k, labels in (("read", READ_LABELS), ("watch", WATCH_LABELS), ("play", PLAY_LABELS))
    ]
    finished_this_year = [(n, word) for n, word in finished_this_year if n > 0]

    running = 0
    growth_pairs = []
    for r in growth_rows:
        running += r["c"]
        growth_pairs.append((r["m"], running))

    # Aggregate by first author (the authors column is a comma-joined string)
    author_counts: dict[str, int] = {}
    for r in author_rows:
        first = r["authors"].split(",")[0].strip()
        if first:
            author_counts[first] = author_counts.get(first, 0) + r["c"]
    top_authors = sorted(author_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]

    valuation_pairs = [(r["d"], r["total_value"]) for r in valuation_rows]

    from app.services import charts
    chart_read = charts.column_chart(
        read_pairs, empty_message="Mark media as completed to build this chart")
    chart_growth = charts.area_chart(growth_pairs, empty_message="No items yet")
    chart_authors = charts.hbar_chart(top_authors, empty_message="No authors yet")
    currency = get_currency()
    if currency.suffix:
        chart_value_prefix, chart_value_suffix = "", " " + currency.symbol
    else:
        chart_value_prefix, chart_value_suffix = currency.symbol, ""
    chart_valuation = (
        charts.area_chart(valuation_pairs, value_prefix=chart_value_prefix, value_suffix=chart_value_suffix,
                          empty_message="Run a batch valuation to start tracking value over time")
        if len(valuation_pairs) >= 2 else None
    )

    return request.app.state.templates.TemplateResponse(
        request,
        "stats.html",
        {
            "by_type": by_type,
            "by_location": by_location,
            "total": total,
            "owned_count": stats_owned,
            "wishlist_count": stats_wishlist,
            "with_covers": with_covers,
            "without_isbn": without_isbn,
            "recent": recent,
            "media_types": MEDIA_TYPES,
            "finished_this_year": finished_this_year,
            "current_year": current_year,
            "current_value": current_value,
            "chart_read": chart_read,
            "chart_growth": chart_growth,
            "chart_authors": chart_authors,
            "chart_valuation": chart_valuation,
            # January only: the previous year's wrap-up, as "<year> in Books".
            "year_in_books": str(today.year - 1) if today.month == 1 else None,
        },
    )


@router.get("/stats/wrapup")
async def stats_wrapup(request: Request, period: str | None = None,
                       _=Depends(require_catalogue_role("viewer")),
                       __=Depends(require_feature("stats"))):
    today = _today()
    with get_db() as db:
        # No `period` at all (the link from Stats) opens on the newest month with
        # content; a supplied but bad one still falls back to the current month.
        chosen = (wrapups.latest_period(db, today) if period is None
                  else wrapups.parse_period(period, today))
        summary = wrapups.summarize(db, chosen)
        first_year = wrapups.earliest_year(db, today)
    return request.app.state.templates.TemplateResponse(
        request,
        "stats_wrapup.html",
        {
            "period": chosen,
            "summary": summary,
            "period_options": wrapups.period_options(today, first_year),
        },
    )


@router.get("/logs")
async def logs(
    request: Request,
    level: str = "",
    module: str = "",
    q: str = "",
    page: int = Query(1, le=MAX_PAGE),
    _=Depends(require_role("admin")),
):
    per_page = 100
    conditions = []
    params: list = []

    if level:
        conditions.append("level = ?")
        params.append(level.upper())
    if module:
        conditions.append("module LIKE ?")
        params.append(f"%{module}%")
    if q:
        conditions.append("message LIKE ?")
        params.append(f"%{q}%")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    offset = (max(page, 1) - 1) * per_page

    with get_db() as db:
        total = db.execute(f"SELECT COUNT(*) as c FROM log_entries {where}", params).fetchone()["c"]
        entries = db.execute(
            f"SELECT * FROM log_entries {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()
        modules = [
            row["module"] for row in db.execute(
                "SELECT DISTINCT module FROM log_entries ORDER BY module"
            ).fetchall()
        ]

    return request.app.state.templates.TemplateResponse(
        request,
        "logs.html",
        {
            "entries": entries,
            "modules": modules,
            "total": total,
            "page": page,
            "per_page": per_page,
            "has_prev": page > 1,
            "has_next": (offset + per_page) < total,
            "filter_level": level,
            "filter_module": module,
            "filter_q": q,
        },
    )


BORROWER_ERROR_MESSAGES = {
    "active": "That borrower still has an active loan — check the item in before removing them.",
}


@router.get("/settings")
async def settings(request: Request, _=Depends(require_role("admin"))):
    from app.config import SECRET_ENV_VARS, is_env_override
    from app.database import get_all_settings
    from app.nav import hideable_tab_states
    from app.services import cover_queue, sync_jobs
    # Known codes only — never reflect the raw query param into the template.
    borrower_error_message = BORROWER_ERROR_MESSAGES.get(request.query_params.get("borrower_error"))
    with get_db() as db:
        settings = get_all_settings(db)
        locations = location_svc.legacy_locations(db)
        location_nodes = location_svc.location_tree(db)
        item_count = db.execute("SELECT COUNT(*) as c FROM items_live").fetchone()["c"]
        # Excludes items dismissed from the cover-review queue
        # (items.cover_review_dismissed, migration 32) — this figure and the
        # /cover-review entry point's count are the same number, and an item
        # marked "not available" there should stop being counted here too.
        missing_covers = db.execute(
            "SELECT COUNT(*) AS c FROM items_live WHERE cover_path IS NULL AND cover_review_dismissed = 0"
        ).fetchone()["c"]
        cover_queue_stats = cover_queue.stats()
        # Carries each borrower's *returned* loan count for the delete
        # confirmation's copy. Returned rows only: the dialog fires before the
        # POST, so before the active-loan guard — counting an open loan here
        # would tell the user a current loan is a "past loan record".
        borrowers = db.execute(
            "SELECT b.*, "
            "COALESCE(SUM(CASE WHEN c.checked_in IS NOT NULL THEN 1 ELSE 0 END), 0) "
            "AS returned_loan_count "
            "FROM borrowers b "
            "LEFT JOIN checkouts c ON c.borrower_id = b.id "
            "GROUP BY b.id ORDER BY b.name"
        ).fetchall()
        game_platforms_list = db.execute(
            "SELECT * FROM game_platforms ORDER BY sort_order, name"
        ).fetchall()
        share_links = db.execute(
            "SELECT * FROM share_links ORDER BY created_at DESC"
        ).fetchall()
        tags = tags_svc.list_tags_with_counts(db)
    # Iterate the env map, not `settings`: `get_all_settings` carries only keys
    # with a row, so an env-only credential is absent from it (G15) and a
    # comprehension over it can never see one. Keys here — `is_env_override`
    # takes a settings key, not a shell variable name.
    env_overrides = {k for k in SECRET_ENV_VARS if is_env_override(k)}
    # Both inputs to each hideable tab's visibility, for the Navigation card.
    # Deliberately called with no argument: `settings` above comes from
    # `get_all_settings`, which only carries keys that have a row in the
    # table — a credential supplied purely by env var (HARDCOVER_TOKEN) is
    # missing from it, and the card would claim Discover is unconfigured
    # while the nav bar shows it. The no-arg path reads the same env-aware
    # snapshot the nav itself uses.
    hideable_nav_tab_states = hideable_tab_states()
    # Outside the `with` above: the probes open their own connections (G112).
    from app.features import feature_rows
    features_rows = feature_rows()
    # From the same rows, so the marker and the list below it agree.
    profile_rows = features.profile_rows(features_rows)
    current_profile = next((p["name"] for p in profile_rows if p["current"]), None)
    # Never hand decrypted credentials to the template — it only needs to know
    # whether one is saved. Fields are write-only; blank submit keeps the value.
    from app.crypto import SENSITIVE_KEYS
    # Two flags, because for an env-only credential these differ. `secrets_saved`
    # means "there is a row in `settings`" — the right question for the clear
    # checkbox (only a row can be deleted) and the "Saved" placeholder.
    secrets_saved = {k: bool(settings.get(k)) for k in SENSITIVE_KEYS}
    # `secrets_present` means "a credential is available", which an env var
    # supplies with no row at all (G15, issue #39). It gates the Test buttons and
    # — via `data-abs-saved` — Audiobookshelf's Sync Now (issue #41), which reads
    # availability because /stream syncs from the stored credentials and never
    # from the form. It deliberately does not feed the clear checkbox: making
    # `secrets_saved` itself env-aware would offer a "Remove saved key" checkbox
    # that cannot remove an env credential.
    secrets_present = {k: secrets_saved[k] or k in env_overrides for k in SENSITIVE_KEYS}
    # The retained and native RomM names are one explicit credential choice.
    # Either available alias enables the existing form without exposing a value.
    romm_saved = any(secrets_saved[key] for key in ("romm_token", "romm_api_token"))
    romm_present = any(secrets_present[key] for key in ("romm_token", "romm_api_token"))
    for key in ("romm_token", "romm_api_token"):
        secrets_saved[key], secrets_present[key] = romm_saved, romm_present
    # `abs_url` needs its own flag: it has a SECRET_ENV_VARS entry but is not a
    # SENSITIVE_KEY (it is not a credential), so `secrets_present` has no member
    # for it — and both Audiobookshelf actions gate on the URL: Test on typed-or-
    # present, Sync Now on presence alone (issue #41).
    abs_url_present = bool(settings.get("abs_url")) or "abs_url" in env_overrides
    komga_url_present = bool(settings.get("komga_url")) or "komga_url" in env_overrides
    romm_url_present = bool(settings.get("romm_url")) or "romm_url" in env_overrides
    price_alert_last_run_display = price_alerts.describe_last_run(
        settings.get("price_alert_last_run"), settings.get("price_alert_last_summary"))
    release_radar_last_run_display = release_radar.describe_last_run(
        settings.get("release_radar_last_run"), settings.get("release_radar_last_summary"))
    for k in SENSITIVE_KEYS:
        if k in settings:
            settings[k] = ""

    # Seed detached integration-sync progress into the first Settings render.
    # The browser still polls for fresh values, but a transient failed/429
    # reconnect can no longer make an active job look idle after navigation.
    abs_sync_job = sync_jobs.get_status("audiobookshelf")
    komga_sync_job = sync_jobs.get_status("komga")
    romm_sync_job = sync_jobs.get_status("romm")

    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {"settings": settings, "locations": locations, "location_nodes": location_nodes,
         "item_count": item_count, "share_links": share_links,
         "borrowers": borrowers, "secrets_saved": secrets_saved,
         "secrets_present": secrets_present, "abs_url_present": abs_url_present,
         "komga_url_present": komga_url_present,
         "romm_url_present": romm_url_present,
         "game_platforms_list": game_platforms_list,
         "hideable_nav_tab_states": hideable_nav_tab_states,
         "feature_rows": features_rows,
         "profile_rows": profile_rows,
         "current_profile": current_profile,
         "borrower_error_message": borrower_error_message,
         "missing_covers": missing_covers, "cover_queue_stats": cover_queue_stats,
         "abs_sync_job": abs_sync_job, "komga_sync_job": komga_sync_job,
         "romm_sync_job": romm_sync_job,
         "tags": tags, "media_types": MEDIA_TYPES,
         "price_alert_last_run_display": price_alert_last_run_display,
         "price_alert_default_threshold": price_alerts.DEFAULT_THRESHOLD_PCT,
         "price_alert_max_threshold": price_alerts.MAX_THRESHOLD_PCT,
         "price_alert_default_cap": price_alerts.DEFAULT_NIGHTLY_CAP,
         "price_alert_max_cap": price_alerts.MAX_NIGHTLY_CAP,
         "release_radar_last_run_display": release_radar_last_run_display,
         "release_radar_default_cap": release_radar.DEFAULT_CAP,
         "release_radar_max_cap": release_radar.MAX_CAP,
         "buy_links_bookshop_hosts": buy_links_svc.BOOKSHOP_HOSTS,
         "buy_links_amazon_hosts": buy_links_svc.AMAZON_HOSTS},
    )
