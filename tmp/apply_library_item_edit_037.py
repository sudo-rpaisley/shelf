from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"{label}: anchor not found in {path}")
    p.write_text(text.replace(old, new, 1))


def replace_all(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"{label}: anchor not found in {path}")
    p.write_text(text.replace(old, new))


# One dependency owns the item-specific permission policy. It deliberately
# does not impose the user's global viewer/editor level: a global viewer may be
# an editor of one library, while a global editor with no membership must not
# gain access to every library. Site admins remain global through libraries.py.
auth = Path("app/auth.py")
text = auth.read_text()
anchor = "\ndef _raise_auth_required(request: Request):\n"
helper = '''\ndef require_item_role(minimum_role: str):
    """FastAPI dependency for one item governed by its Shelf library.

    Authentication remains app-wide. For non-admins, the item's library
    membership is authoritative for item-local viewer/editor rights. A denied
    or unmapped item is treated as missing so guessed IDs do not reveal which
    private catalogue rows exist.
    """
    if minimum_role not in ("viewer", "editor"):
        raise ValueError("Item role must be viewer or editor")

    async def _dependency(request: Request):
        user = getattr(request.state, "user", None)
        if not user:
            _raise_auth_required(request)

        raw_item_id = request.path_params.get("item_id")
        try:
            item_id = int(raw_item_id)
        except (TypeError, ValueError):
            _raise_item_not_found(request)

        from app.services import libraries
        with get_db() as db:
            allowed = libraries.has_item_role(
                db, dict(user), item_id, minimum_role
            )
        if not allowed:
            _raise_item_not_found(request)
        return user

    return _dependency


def _raise_item_not_found(request: Request):
    """Hide private or unmapped catalogue IDs behind the missing-item surface."""
    if request.headers.get("HX-Request") or request.url.path.startswith("/api/"):
        raise _ResponseException(HTMLResponse("Not found", status_code=404))
    raise _ResponseException(RedirectResponse(url="/browse", status_code=303))

'''
if "def require_item_role(" not in text:
    if anchor not in text:
        raise SystemExit("auth helper anchor not found")
    auth.write_text(text.replace(anchor, helper + anchor, 1))

# Item edit page: the item-library editor role, not the global editor role, is
# authoritative. Keep the existing in-route check as defence in depth.
replace_once(
    "app/routers/pages.py",
    "from app.auth import require_role",
    "from app.auth import require_item_role, require_role",
    "pages import",
)
replace_once(
    "app/routers/pages.py",
    '''@router.get("/item/{item_id}/edit")
async def item_edit(
    request: Request,
    item_id: int,
    from_: str = Query("", alias="from"),
    error: str | None = Query(None),
    _=Depends(require_role("editor")),
):''',
    '''@router.get("/item/{item_id}/edit")
async def item_edit(
    request: Request,
    item_id: int,
    from_: str = Query("", alias="from"),
    error: str | None = Query(None),
    _=Depends(require_item_role("editor")),
):''',
    "item edit page dependency",
)

# Direct item mutation endpoints.
replace_once(
    "app/routers/items.py",
    "from app.auth import require_role",
    "from app.auth import require_item_role, require_role",
    "items import",
)
for old, new, label in [
    (
        'async def update_item(request: Request, item_id: int, _=Depends(require_role("editor"))):',
        'async def update_item(request: Request, item_id: int, _=Depends(require_item_role("editor"))):',
        "item save",
    ),
    (
        'async def set_reading_status(request: Request, item_id: int, status: str = Form(""), _=Depends(require_role("viewer"))):',
        'async def set_reading_status(request: Request, item_id: int, status: str = Form(""), _=Depends(require_item_role("viewer"))):',
        "legacy reading status",
    ),
    (
        'async def fetch_synopsis(item_id: int, _=Depends(require_role("editor"))):',
        'async def fetch_synopsis(item_id: int, _=Depends(require_item_role("editor"))):',
        "fetch synopsis",
    ),
    (
        'async def delete_item(item_id: int, _=Depends(require_role("editor"))):',
        'async def delete_item(item_id: int, _=Depends(require_item_role("editor"))):',
        "delete item",
    ),
]:
    replace_once("app/routers/items.py", old, new, label)

# Every editor route in the cover module is item-local; the admin bulk retry is
# intentionally unchanged. Cover-status is left with its settled-placeholder
# semantics for a deleted mid-poll item and contains no catalogue metadata.
replace_once(
    "app/routers/items_covers.py",
    "from app.auth import require_role",
    "from app.auth import require_item_role, require_role",
    "covers import",
)
replace_all(
    "app/routers/items_covers.py",
    'Depends(require_role("editor"))',
    'Depends(require_item_role("editor"))',
    "cover editor dependencies",
)

# Tag mutations are all item-local.
replace_once(
    "app/routers/tags.py",
    "from app.auth import require_role",
    "from app.auth import require_item_role, require_role",
    "tags import",
)
replace_all(
    "app/routers/tags.py",
    'Depends(require_role("editor"))',
    'Depends(require_item_role("editor"))',
    "tag editor dependencies",
)

# Related Media: source item requires editor rights, candidate discovery is
# scoped to editor-accessible libraries, and both ends must be editable before
# a relation may be created or removed.
replace_once(
    "app/routers/related_media.py",
    "from app.auth import require_role",
    "from app.auth import require_item_role, require_role",
    "related media import",
)
replace_all(
    "app/routers/related_media.py",
    'Depends(require_role("editor"))',
    'Depends(require_item_role("editor"))',
    "related media editor dependencies",
)
replace_once(
    "app/routers/related_media.py",
    '''    with get_db() as db:
        if not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            return HTMLResponse("Item not found", status_code=404)
        candidates = media_groups.search_candidates(db, item_id, q, limit=20)
''',
    '''    user = dict(request.state.user)
    with get_db() as db:
        visibility_sql, visibility_params = libraries.item_access_condition(
            user, item_alias="i", minimum_role="editor"
        )
        candidates = media_groups.search_candidates(
            db,
            item_id,
            q,
            limit=20,
            visibility_sql=visibility_sql,
            visibility_params=visibility_params,
        )
''',
    "related media scoped search",
)
replace_once(
    "app/routers/related_media.py",
    '''    with get_db() as db:
        if not _both_items_exist(db, item_id, other_item_id):
            return HTMLResponse("Item not found", status_code=404)
        try:
            media_groups.link_items(
''',
    '''    user = dict(request.state.user)
    with get_db() as db:
        if (
            not libraries.has_item_role(db, user, item_id, "editor")
            or not libraries.has_item_role(db, user, other_item_id, "editor")
        ):
            return HTMLResponse("Item not found", status_code=404)
        try:
            media_groups.link_items(
''',
    "related media link both sides",
)
replace_once(
    "app/routers/related_media.py",
    '''    with get_db() as db:
        if not _both_items_exist(db, item_id, other_item_id):
            return HTMLResponse("Item not found", status_code=404)
        if not media_groups.unlink_items(db, item_id, other_item_id):
''',
    '''    user = dict(request.state.user)
    with get_db() as db:
        if (
            not libraries.has_item_role(db, user, item_id, "editor")
            or not libraries.has_item_role(db, user, other_item_id, "editor")
        ):
            return HTMLResponse("Item not found", status_code=404)
        if not media_groups.unlink_items(db, item_id, other_item_id):
''',
    "related media unlink both sides",
)

# Candidate query accepts an optional central visibility predicate. Defaults
# preserve service callers outside the ACL UI.
replace_once(
    "app/services/media_groups.py",
    '''def search_candidates(db, item_id: int, query: str, *, limit: int = 20):
    """Find catalogue items not already in this item's related-media group."""
    if not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
        return []
    excluded = sorted(related_ids(db, item_id, include_self=True))
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    placeholders = ",".join("?" for _ in excluded)
    rows = db.execute(
        f"""SELECT * FROM items
            WHERE (title LIKE ? COLLATE NOCASE
               OR authors LIKE ? COLLATE NOCASE
               OR series_name LIKE ? COLLATE NOCASE)
              AND id NOT IN ({placeholders})
            ORDER BY title COLLATE NOCASE, media_type, id
            LIMIT ?""",
        (like, like, like, *excluded, limit),
    ).fetchall()
    return rows''',
    '''def search_candidates(
    db,
    item_id: int,
    query: str,
    *,
    limit: int = 20,
    visibility_sql: str | None = None,
    visibility_params: list | tuple = (),
):
    """Find catalogue items outside this related group, optionally ACL-scoped."""
    if not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
        return []
    excluded = sorted(related_ids(db, item_id, include_self=True))
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    placeholders = ",".join("?" for _ in excluded)
    visibility_clause = f" AND ({visibility_sql})" if visibility_sql else ""
    rows = db.execute(
        f"""SELECT i.* FROM items i
            WHERE (i.title LIKE ? COLLATE NOCASE
               OR i.authors LIKE ? COLLATE NOCASE
               OR i.series_name LIKE ? COLLATE NOCASE)
              AND i.id NOT IN ({placeholders})
              {visibility_clause}
            ORDER BY i.title COLLATE NOCASE, i.media_type, i.id
            LIMIT ?""",
        (like, like, like, *excluded, *visibility_params, limit),
    ).fetchall()
    return rows''',
    "media group candidate visibility",
)

# Single-item Hardcover push is an item mutation. Bulk import/export remains a
# separate multi-item access-scope problem for the next ACL sweep.
replace_once(
    "app/routers/hardcover.py",
    "from app.auth import require_role",
    "from app.auth import require_item_role, require_role",
    "hardcover import",
)
replace_once(
    "app/routers/hardcover.py",
    'async def push_to_hardcover(item_id: int, _=Depends(require_role("editor"))):',
    'async def push_to_hardcover(item_id: int, _=Depends(require_item_role("editor"))):',
    "hardcover single push",
)
