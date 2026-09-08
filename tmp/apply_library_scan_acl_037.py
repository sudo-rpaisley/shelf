from pathlib import Path

# Shared scanner helpers: duplicate handling must respect library visibility,
# and a visible existing item can be wishlisted personally without changing
# shared ownership. New wishlist rows capture both shared owned=0 and the
# acting user's personal Wishlist atomically.
path = Path("app/routers/items_common.py")
text = path.read_text()
text = text.replace(
    "from app.services import covers, detect, googlebooks, hardcover, national, openlibrary, provider_result\n",
    "from app.services import covers, detect, googlebooks, hardcover, libraries, national, openlibrary, provider_result, user_state\n",
    1,
)
anchor = '''def _toast_header(message: str, toast_type: str = "success") -> str:
    return json.dumps({"showToast": {"message": message, "type": toast_type}})


'''
addition = '''def _toast_header(message: str, toast_type: str = "success") -> str:
    return json.dumps({"showToast": {"message": message, "type": toast_type}})


def _default_library_edit_allowed(request: Request) -> bool:
    """Whether the acting user may create shared catalogue rows in Main Library."""
    actor = dict(request.state.user)
    with get_db() as db:
        return libraries.has_library_role(
            db, actor, libraries.DEFAULT_LIBRARY_ID, "editor"
        )


def _scan_duplicate_response(
    request: Request,
    templates,
    existing,
    barcode: str,
    *,
    mode: str,
    media_type: str | None = None,
):
    """Render a duplicate without disclosing an inaccessible catalogue row.

    A visible duplicate keeps Shelf's normal linked duplicate card. Wishlist
    mode is personal: an already-catalogued visible item is simply added to
    this user's Wishlist, without changing shared ``owned``. A duplicate in a
    library the actor cannot see returns only a generic conflict and never
    exposes title, item id, cover or media metadata.
    """
    row = dict(existing)
    item_id = int(row["id"])
    actor = dict(request.state.user)
    with get_db() as db:
        if not libraries.has_item_role(db, actor, item_id, "viewer"):
            _log_scan(barcode, media_type or "", "duplicate", None, mode)
            return templates.TemplateResponse(
                request,
                "fragments/scan_result.html",
                {
                    "status": "error",
                    "isbn": barcode,
                    "message": "This barcode cannot be added here",
                },
            )
        full = db.execute(
            "SELECT id, title, authors, cover_path, media_type, source "
            "FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if full is None:
            return templates.TemplateResponse(
                request,
                "fragments/scan_result.html",
                {"status": "error", "isbn": barcode, "message": "Item not found"},
            )
        full = dict(full)
        if mode == "wishlist":
            user_state.save_state(db, int(actor["id"]), item_id, wishlist=1)

    effective_type = media_type or full.get("media_type") or ""
    if mode == "wishlist":
        _log_scan(barcode, effective_type, "wishlisted", item_id, mode)
        return templates.TemplateResponse(
            request,
            "fragments/scan_result.html",
            {
                "status": "wishlisted",
                "isbn": barcode,
                "title": full["title"],
                "authors": full.get("authors"),
                "cover_path": full.get("cover_path"),
                "item_id": item_id,
                "source": full.get("source") or "catalogue",
                "media_type_label": MEDIA_TYPES.get(effective_type, effective_type),
            },
        )

    _log_scan(barcode, effective_type, "duplicate", item_id, mode)
    return templates.TemplateResponse(
        request,
        "fragments/scan_result.html",
        {
            "status": "duplicate",
            "isbn": barcode,
            "title": full["title"],
            "item_id": item_id,
        },
    )


'''
if anchor not in text:
    raise SystemExit("toast helper anchor not found")
text = text.replace(anchor, addition, 1)

old = '''def _save_item(metadata: dict, isbn13: str, media_type: str, location_id: int | None,
               source: str, hc_ids: dict) -> int:
    """Insert from scan metadata; `isbn13` is boundary-validated by every
    caller, and the funnel derives `isbn10`. Returns the new item ID."""
    with get_db() as db:
        return insert_item(
            db,
            title=metadata["title"],
            subtitle=metadata.get("subtitle"),
            authors=metadata.get("authors"),
            isbn=isbn13,
            media_type=media_type,
            publisher=metadata.get("publisher"),
            publish_year=metadata.get("publish_year"),
            page_count=metadata.get("page_count"),
            description=metadata.get("description"),
            series_name=metadata.get("series_name"),
            series_position=metadata.get("series_position"),
            location_id=location_id,
            source=source,
            language=metadata.get("language"),
            hardcover_book_id=hc_ids.get("hardcover_book_id"),
            hardcover_edition_id=hc_ids.get("hardcover_edition_id"),
        )
'''
new = '''def _save_item(
    metadata: dict,
    isbn13: str,
    media_type: str,
    location_id: int | None,
    source: str,
    hc_ids: dict,
    *,
    owned: int = 1,
    wishlist_user_id: int | None = None,
) -> int:
    """Insert scan metadata and optional personal Wishlist state atomically."""
    with get_db() as db:
        item_id = insert_item(
            db,
            title=metadata["title"],
            subtitle=metadata.get("subtitle"),
            authors=metadata.get("authors"),
            isbn=isbn13,
            media_type=media_type,
            publisher=metadata.get("publisher"),
            publish_year=metadata.get("publish_year"),
            page_count=metadata.get("page_count"),
            description=metadata.get("description"),
            series_name=metadata.get("series_name"),
            series_position=metadata.get("series_position"),
            location_id=location_id,
            source=source,
            language=metadata.get("language"),
            hardcover_book_id=hc_ids.get("hardcover_book_id"),
            hardcover_edition_id=hc_ids.get("hardcover_edition_id"),
            owned=owned,
        )
        if wishlist_user_id is not None:
            user_state.save_state(db, int(wishlist_user_id), item_id, wishlist=1)
        return item_id
'''
if old not in text:
    raise SystemExit("save item anchor not found")
text = text.replace(old, new, 1)

# UPC duplicate pre-check must not reveal a hidden row; a visible Wishlist scan
# should update personal state rather than stopping at a duplicate card.
old = '''    if existing:
        _log_scan(upc_norm, existing["media_type"], "duplicate", existing["id"], mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "duplicate", "isbn": upc_norm, "title": existing["title"], "item_id": existing["id"]},
        )

    # --- One UPC Item DB lookup, above the game/film fork.
'''
new = '''    if existing:
        return _scan_duplicate_response(
            request,
            templates,
            existing,
            upc_norm,
            mode=mode,
            media_type=existing["media_type"],
        )

    # A new shared catalogue row will land in Main Library. Refuse before any
    # outbound metadata work if the actor may not edit that library.
    if not _default_library_edit_allowed(request):
        return HTMLResponse("Forbidden", status_code=403)

    # --- One UPC Item DB lookup, above the game/film fork.
'''
if old not in text:
    raise SystemExit("UPC early duplicate anchor not found")
text = text.replace(old, new, 1)
# items_common needs HTMLResponse for the local permission refusal.
text = text.replace(
    "from fastapi import Request\n",
    "from fastapi import Request\nfrom fastapi.responses import HTMLResponse\n",
    1,
)
# Carry mode into every UPC manual-add fallback.
text = text.replace(
    '"locations": _manual_form_locations()},\n',
    '"locations": _manual_form_locations(), "mode": mode},\n',
)

# Save personal Wishlist state in the same transaction as UPC creation.
old = '''                item_id = insert_item(
                    db,
                    title=metadata["title"],
                    description=metadata.get("description"),
                    media_type=media_type,
                    publish_year=metadata.get("publish_year"),
                    location_id=location_id,
                    upc=upc_key,
                    source=source,
                    # Was a follow-up UPDATE in a second transaction; owned is
                    # an item-creation field, so it belongs in the insert.
                    owned=0 if mode == "wishlist" else 1,
                )
'''
new = old + '''                if mode == "wishlist":
                    user_state.save_state(
                        db, int(request.state.user["id"]), item_id, wishlist=1
                    )
'''
if old not in text:
    raise SystemExit("UPC insert anchor not found")
text = text.replace(old, new, 1)
old = '''    if existing:
        _log_scan(upc_norm, media_type, "duplicate", existing["id"], mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "duplicate", "isbn": upc_norm, "title": existing["title"],
             "item_id": existing["id"]},
        )
'''
new = '''    if existing:
        return _scan_duplicate_response(
            request, templates, existing, upc_norm, mode=mode, media_type=media_type
        )
'''
if old not in text:
    raise SystemExit("UPC race duplicate anchor not found")
text = text.replace(old, new, 1)
# Game insert + race duplicate have the same personal/visibility requirements.
old = '''                item_id = insert_item(
                    db,
                    title=game_title,
                    description=metadata.get("description") if metadata else None,
                    media_type="video_game",
                    publisher=metadata.get("publisher") if metadata else None,
                    publish_year=metadata.get("publish_year") if metadata else None,
                    series_name=metadata.get("series_name") if metadata else None,
                    platform=platform,
                    location_id=location_id,
                    upc=upc_key,
                    source=source,
                    owned=0 if mode == "wishlist" else 1,
                )
'''
new = old + '''                if mode == "wishlist":
                    user_state.save_state(
                        db, int(request.state.user["id"]), item_id, wishlist=1
                    )
'''
if old not in text:
    raise SystemExit("UPC game insert anchor not found")
text = text.replace(old, new, 1)
old = '''    if existing:
        _log_scan(upc_norm, "video_game", "duplicate", existing["id"], mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "duplicate", "isbn": upc_norm, "title": existing["title"],
             "item_id": existing["id"]},
        )
'''
new = '''    if existing:
        return _scan_duplicate_response(
            request,
            templates,
            existing,
            upc_norm,
            mode=mode,
            media_type="video_game",
        )
'''
if old not in text:
    raise SystemExit("UPC game duplicate anchor not found")
text = text.replace(old, new, 1)
path.write_text(text)

# Existing scan modes: lookup/quick-rate only need visibility because they do
# not mutate shared catalogue data; lend/return/move/inventory require library
# editor. Hidden items are deliberately reduced to the same not-owned surface.
path = Path("app/routers/items.py")
text = path.read_text()
text = text.replace(
    "from app.services import openlibrary, googlebooks, hardcover, covers, national\n",
    "from app.services import openlibrary, googlebooks, hardcover, covers, libraries, national, user_state\n",
    1,
)
old = '''        item = _find_item_by_barcode(lookup_barcode)
        # inventory mode handles not-found specially
        if mode == "inventory":
'''
new = '''        item = _find_item_by_barcode(lookup_barcode)
        actor = dict(request.state.user)
        if item:
            minimum_role = "viewer" if mode in {"lookup", "quick_rate"} else "editor"
            with get_db() as db:
                visible = libraries.has_item_role(db, actor, item["id"], "viewer")
                allowed = libraries.has_item_role(db, actor, item["id"], minimum_role)
            if not visible:
                item = None
            elif not allowed:
                return HTMLResponse("Forbidden", status_code=403)
        # inventory mode handles not-found specially
        if mode == "inventory":
'''
if old not in text:
    raise SystemExit("existing scan item anchor not found")
text = text.replace(old, new, 1)
old = '''        if mode == "quick_rate":
            return items_scan_modes._scan_mode_quick_rate(request, templates, item, raw)
'''
new = '''        if mode == "quick_rate":
            return items_scan_modes._scan_mode_quick_rate(
                request, templates, item, raw, user_id=int(actor["id"])
            )
'''
if old not in text:
    raise SystemExit("quick rate dispatch anchor not found")
text = text.replace(old, new, 1)

# ISBN duplicate handling is visibility-aware and Wishlist-aware. Only when no
# duplicate exists do we require editor access to Main Library before paying
# for provider lookup and creating a shared row.
old = '''    if existing:
        items_common._log_scan(isbn13, media_type, "duplicate", existing["id"], mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "duplicate", "isbn": isbn13, "title": existing["title"], "item_id": existing["id"]},
        )

    # Get optional metadata-provider credentials — and refuse a stale location
'''
new = '''    if existing:
        return items_common._scan_duplicate_response(
            request, templates, existing, isbn13, mode=mode, media_type=media_type
        )

    if not items_common._default_library_edit_allowed(request):
        return HTMLResponse("Forbidden", status_code=403)

    # Get optional metadata-provider credentials — and refuse a stale location
'''
if old not in text:
    raise SystemExit("ISBN duplicate anchor not found")
text = text.replace(old, new, 1)
# Carry mode to manual fallback.
old = '''                    "enrich_provider": scan_outcome.provider_label(cascade),
                    "locations": items_common._manual_form_locations(),
                },
'''
new = '''                    "enrich_provider": scan_outcome.provider_label(cascade),
                    "locations": items_common._manual_form_locations(),
                    "mode": mode,
                },
'''
if old not in text:
    raise SystemExit("ISBN manual fallback mode anchor not found")
text = text.replace(old, new, 1)
# Atomic scan save now owns wishlist creation; remove the legacy follow-up.
old = '''        item_id = items_common._save_item(metadata, isbn13, media_type, location_id, source, hc_ids)

        # Wishlist mode: set owned = 0
        if mode == "wishlist":
            with get_db() as db:
                update_item_fields(db, item_id, {"owned": 0})
'''
new = '''        item_id = items_common._save_item(
            metadata,
            isbn13,
            media_type,
            location_id,
            source,
            hc_ids,
            owned=0 if mode == "wishlist" else 1,
            wishlist_user_id=(
                int(request.state.user["id"]) if mode == "wishlist" else None
            ),
        )
'''
if old not in text:
    raise SystemExit("ISBN wishlist save anchor not found")
text = text.replace(old, new, 1)

# Manual fallback keeps the scan mode, writes personal Wishlist state for a
# new Wishlist item, and refuses new Main Library rows without library-editor
# rights. Duplicate handling uses the same no-leak helper as scanning.
old = '''    title = form.get("title", "").strip()
    if not title:
'''
new = '''    mode = (form.get("mode") or "add").strip()
    title = form.get("title", "").strip()
    if not title:
'''
if old not in text:
    raise SystemExit("manual mode anchor not found")
text = text.replace(old, new, 1)
old = '''    # Media type, platform and location are the funnel's to check (#54):
'''
new = '''    if not items_common._default_library_edit_allowed(request):
        return HTMLResponse("Forbidden", status_code=403)

    # Media type, platform and location are the funnel's to check (#54):
'''
if old not in text:
    raise SystemExit("manual default library permission anchor not found")
text = text.replace(old, new, 1)
old = '''                    language=language,
                    source="manual",
                )
'''
new = '''                    language=language,
                    source="manual",
                    owned=0 if mode == "wishlist" else 1,
                )
                if mode == "wishlist":
                    user_state.save_state(
                        db, int(request.state.user["id"]), item_id, wishlist=1
                    )
'''
if old not in text:
    raise SystemExit("manual insert wishlist anchor not found")
text = text.replace(old, new, 1)
old = '''    if existing:
        code = isbn13 or upc_code or ""
        items_common._log_scan(code, media_type, "duplicate", existing["id"])
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "duplicate", "isbn": code, "title": existing["title"],
             "item_id": existing["id"]},
        )
'''
new = '''    if existing:
        code = isbn13 or upc_code or ""
        return items_common._scan_duplicate_response(
            request, templates, existing, code, mode=mode, media_type=media_type
        )
'''
if old not in text:
    raise SystemExit("manual duplicate anchor not found")
text = text.replace(old, new, 1)
# Manual response semantics reflect Wishlist mode.
old = '''    items_common._log_scan(isbn13 or upc_code or "", media_type, "added", item_id)

    resp = templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {
            "status": "added",
'''
new = '''    result_status = "wishlisted" if mode == "wishlist" else "added"
    items_common._log_scan(
        isbn13 or upc_code or "", media_type, result_status, item_id, mode
    )

    resp = templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {
            "status": result_status,
'''
if old not in text:
    raise SystemExit("manual response status anchor not found")
text = text.replace(old, new, 1)

# The copy-from helper is a read projection: keep the global editor scanner
# gate but return only rows in libraries the actor can view.
old = '''@router.get("/items/suggest")
async def suggest_items(q: str = "", _=Depends(require_role("editor"))):
'''
new = '''@router.get("/items/suggest")
async def suggest_items(
    request: Request,
    q: str = "",
    _=Depends(require_role("editor")),
):
'''
if old not in text:
    raise SystemExit("suggest signature anchor not found")
text = text.replace(old, new, 1)
old = '''    with get_db() as db:
        rows = db.execute(
            "SELECT id, title, authors FROM items WHERE title LIKE ? "
            "ORDER BY title COLLATE NOCASE LIMIT 10",
            (f"{q}%",),
        ).fetchall()
'''
new = '''    with get_db() as db:
        access_sql, access_params = libraries.item_access_condition(
            dict(request.state.user), item_alias="i"
        )
        rows = db.execute(
            "SELECT i.id, i.title, i.authors FROM items i WHERE i.title LIKE ? "
            f"AND {access_sql} ORDER BY i.title COLLATE NOCASE LIMIT 10",
            [f"{q}%", *access_params],
        ).fetchall()
'''
if old not in text:
    raise SystemExit("suggest query anchor not found")
text = text.replace(old, new, 1)
old = '''@router.get("/items/{item_id}/copy-template")
async def copy_template(item_id: int, _=Depends(require_role("editor"))):
'''
new = '''@router.get("/items/{item_id}/copy-template")
async def copy_template(
    item_id: int,
    _role=Depends(require_role("editor")),
    _item=Depends(require_item_role("viewer")),
):
'''
if old not in text:
    raise SystemExit("copy template dependency anchor not found")
text = text.replace(old, new, 1)

# Recent scanner history may contain links to items in other libraries. Keep
# generic item-less operational entries but suppress linked private rows.
old = '''    with get_db() as db:
        scans = db.execute(
            "SELECT sl.*, i.title, i.authors, i.cover_path "
            "FROM scan_log sl LEFT JOIN items i ON sl.item_id = i.id "
            "WHERE sl.mode = ? ORDER BY sl.created_at DESC LIMIT 20",
            (mode,),
        ).fetchall()
'''
new = '''    with get_db() as db:
        access_sql, access_params = libraries.item_access_condition(
            dict(request.state.user), item_alias="i"
        )
        scans = db.execute(
            "SELECT sl.*, i.title, i.authors, i.cover_path "
            "FROM scan_log sl LEFT JOIN items i ON sl.item_id = i.id "
            f"WHERE sl.mode = ? AND (sl.item_id IS NULL OR ({access_sql})) "
            "ORDER BY sl.created_at DESC LIMIT 20",
            [mode, *access_params],
        ).fetchall()
'''
if old not in text:
    raise SystemExit("recent scans query anchor not found")
text = text.replace(old, new, 1)

# Inventory is a shared mutation workflow, so its missing list includes only
# rows the actor can edit, never visible-only or private catalogue items.
old = '''        items = db.execute(
            "SELECT id, title, authors, cover_path FROM items WHERE location_id = ? ORDER BY title",
            (location_id,),
        ).fetchall()
'''
new = '''        access_sql, access_params = libraries.item_access_condition(
            dict(request.state.user), item_alias="i", minimum_role="editor"
        )
        items = db.execute(
            "SELECT i.id, i.title, i.authors, i.cover_path FROM items i "
            f"WHERE i.location_id = ? AND {access_sql} ORDER BY i.title",
            [location_id, *access_params],
        ).fetchall()
'''
if old not in text:
    raise SystemExit("inventory missing query anchor not found")
path.write_text(text)

# Quick-rate is personal state, not a shared catalogue mutation.
path = Path("app/routers/items_scan_modes.py")
text = path.read_text()
text = text.replace(
    "from app.services.item_write import ItemValueError, update_item_fields\n",
    "from app.services import user_state\nfrom app.services.item_write import ItemValueError, update_item_fields\n",
    1,
)
old = '''def _scan_mode_quick_rate(request, templates, item: dict, raw: str):
    """Handle quick rate mode: mark item as read/completed."""
    from datetime import date
    with get_db() as db:
        update_item_fields(db, item["id"], {
            "reading_status": "read", "date_finished": date.today().isoformat(),
        })
'''
new = '''def _scan_mode_quick_rate(
    request, templates, item: dict, raw: str, *, user_id: int
):
    """Mark this item read for the acting user only."""
    with get_db() as db:
        user_state.set_reading_status(db, user_id, item["id"], "read")
'''
if old not in text:
    raise SystemExit("quick rate handler anchor not found")
path.write_text(text.replace(old, new, 1))

# Preserve wishlist mode through the manual fallback form.
path = Path("app/templates/fragments/scan_result.html")
text = path.read_text()
old = '''            <input type="hidden" name="isbn" value="{{ isbn }}">
            <input type="hidden" name="media_type" value="{{ media_type }}">
'''
new = '''            <input type="hidden" name="isbn" value="{{ isbn }}">
            <input type="hidden" name="media_type" value="{{ media_type }}">
            <input type="hidden" name="mode" value="{{ mode or 'add' }}">
'''
if old not in text:
    raise SystemExit("manual fallback hidden fields anchor not found")
path.write_text(text.replace(old, new, 1))

# Update the legacy quick-rate regression to the current per-user state model.
path = Path("tests/test_scan_modes.py")
text = path.read_text()
old = '''    def test_quick_rate_marks_as_read(self, admin_client, db):
        item_id = _insert_item(db, title="Rate Me", isbn="9780000000705")
        db.commit()
        resp = admin_client.post("/api/scan", data={
            "isbn": "9780000000705", "mode": "quick_rate",
        })
        assert resp.status_code == 200
        assert b"Marked as read" in resp.content
        assert "HX-Trigger" not in resp.headers

        with get_db() as check_db:
            row = check_db.execute("SELECT reading_status, date_finished FROM items WHERE id = ?", (item_id,)).fetchone()
        assert row["reading_status"] == "read"
        assert row["date_finished"] is not None
'''
new = '''    def test_quick_rate_marks_as_read(self, admin_client, admin_user, db):
        item_id = _insert_item(db, title="Rate Me", isbn="9780000000705")
        db.commit()
        resp = admin_client.post("/api/scan", data={
            "isbn": "9780000000705", "mode": "quick_rate",
        })
        assert resp.status_code == 200
        assert b"Marked as read" in resp.content
        assert "HX-Trigger" not in resp.headers

        with get_db() as check_db:
            legacy = check_db.execute(
                "SELECT reading_status, date_finished FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            personal = check_db.execute(
                "SELECT reading_status, date_finished FROM user_item_state "
                "WHERE user_id = ? AND item_id = ?",
                (admin_user["id"], item_id),
            ).fetchone()
        assert legacy["reading_status"] is None
        assert legacy["date_finished"] is None
        assert personal["reading_status"] == "read"
        assert personal["date_finished"] is not None
'''
if old not in text:
    raise SystemExit("legacy quick-rate test anchor not found")
path.write_text(text.replace(old, new, 1))
