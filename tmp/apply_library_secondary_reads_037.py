from pathlib import Path

# Series: scope every projected item to the acting user's libraries and use
# personal Wishlist state instead of treating shared owned=0 as wishlisted.
path = Path("app/routers/series.py")
text = path.read_text()
text = text.replace(
    "from app.services import hardcover\n",
    "from app.services import hardcover, libraries\n",
    1,
)
start = text.index('@router.get("/series")')
end = text.index('@router.get("/api/series/check")', start)
series_page = '''@router.get("/series")
async def series_page(request: Request, _=Depends(require_role("viewer"))):
    templates = request.app.state.templates
    actor = dict(request.state.user)
    user_id = int(actor["id"])
    with get_db() as db:
        access_sql, access_params = libraries.item_access_condition(
            actor, item_alias="i"
        )
        rows = db.execute(
            f"""SELECT i.id, i.title, i.authors, i.cover_path,
                       i.series_name, i.series_position, i.owned,
                       COALESCE(uis.wishlist, 0) AS wishlist
                FROM items i
                LEFT JOIN user_item_state uis
                  ON uis.item_id = i.id AND uis.user_id = ?
                WHERE i.series_name IS NOT NULL
                  AND TRIM(i.series_name) != ''
                  AND {access_sql}
                ORDER BY i.series_name COLLATE NOCASE,
                         i.series_position IS NULL, i.series_position,
                         i.title COLLATE NOCASE""",
            [user_id, *access_params],
        ).fetchall()
        has_hardcover = bool(get_setting(db, "hardcover_token"))
        meta_rows = {
            r["name"]: dict(r)
            for r in db.execute(
                "SELECT name, description, complete, hc_total, hc_missing, hc_checked_at "
                "FROM series_meta"
            ).fetchall()
        }
        unassigned_base = (
            "(i.series_name IS NULL OR TRIM(i.series_name) = '') "
            f"AND i.media_type IN ({','.join('?' * len(UNASSIGNED_MEDIA_TYPES))})"
        )
        unassigned_total = db.execute(
            f"SELECT COUNT(*) FROM items i WHERE {unassigned_base} AND {access_sql}",
            [*UNASSIGNED_MEDIA_TYPES, *access_params],
        ).fetchone()[0]
        unassigned_items = [
            dict(r)
            for r in db.execute(
                f"""SELECT i.id, i.title, i.authors, i.cover_path,
                           i.series_name, i.series_position, i.owned,
                           COALESCE(uis.wishlist, 0) AS wishlist
                    FROM items i
                    LEFT JOIN user_item_state uis
                      ON uis.item_id = i.id AND uis.user_id = ?
                    WHERE {unassigned_base} AND {access_sql}
                    ORDER BY i.title COLLATE NOCASE LIMIT ?""",
                [
                    user_id,
                    *UNASSIGNED_MEDIA_TYPES,
                    *access_params,
                    UNASSIGNED_STRIP_CAP,
                ],
            ).fetchall()
        ]

    # Group by NOCASE identity, matching the case-insensitive series metadata
    # and mutation surfaces already used by Shelf.
    series: dict[str, dict] = {}
    for r in rows:
        entry = series.setdefault(
            r["series_name"].casefold(),
            {"name": r["series_name"], "items": [], "_spellings": {}},
        )
        entry["_spellings"][r["series_name"]] = (
            entry["_spellings"].get(r["series_name"], 0) + 1
        )
        entry["items"].append(dict(r))

    for entry in series.values():
        spellings = entry.pop("_spellings")
        entry["name"] = min(
            spellings.items(), key=lambda kv: (-kv[1], kv[0])
        )[0]

    meta_ci = {name.casefold(): meta for name, meta in meta_rows.items()}

    for entry in series.values():
        entry["owned_count"] = sum(1 for i in entry["items"] if i["owned"])
        entry["wishlist_count"] = sum(
            1 for i in entry["items"] if i["wishlist"]
        )
        entry["gaps"] = find_gaps(
            [i["series_position"] for i in entry["items"]]
        )
        meta = meta_ci.get(entry["name"].casefold())
        entry["description"] = meta["description"] if meta else None
        entry["complete"] = meta["complete"] if meta else None
        entry["hc_total"] = meta["hc_total"] if meta else None
        entry["hc_missing"] = meta["hc_missing"] if meta else None
        entry["hc_checked_at"] = meta["hc_checked_at"] if meta else None

    series_list = sorted(
        series.values(),
        key=lambda s: (-len(s["items"]), s["name"].casefold()),
    )

    return templates.TemplateResponse(
        request,
        "series.html",
        {
            "series_list": series_list,
            "has_hardcover": has_hardcover,
            "unassigned_items": unassigned_items,
            "unassigned_total": unassigned_total,
        },
    )


'''
text = text[:start] + series_page + text[end:]
start = text.index('@router.get("/api/series/check")')
end = text.index('def _upsert_series_check(', start)
check_series = '''@router.get("/api/series/check")
async def check_series(
    request: Request,
    name: str = "",
    _=Depends(require_role("viewer")),
):
    """Compare the acting user's accessible local series with Hardcover."""
    name = name.strip()
    if not name:
        return {"ok": False, "message": "Series name required"}

    actor = dict(request.state.user)
    user_id = int(actor["id"])
    with get_db() as db:
        token = get_setting(db, "hardcover_token")
        if not token:
            return {"ok": False, "message": "Hardcover integration not configured"}
        access_sql, access_params = libraries.item_access_condition(
            actor, item_alias="i"
        )
        local = db.execute(
            f"""SELECT i.title, i.owned, i.hardcover_book_id,
                       COALESCE(uis.wishlist, 0) AS wishlist
                FROM items i
                LEFT JOIN user_item_state uis
                  ON uis.item_id = i.id AND uis.user_id = ?
                WHERE i.series_name = ? COLLATE NOCASE
                  AND {access_sql}""",
            [user_id, name, *access_params],
        ).fetchall()

    books = await hardcover.get_series_books(name, token)
    if books is None:
        return {
            "ok": False,
            "message": "Series not found on Hardcover (or lookup failed)",
        }

    by_hc_id = {
        r["hardcover_book_id"]: r for r in local if r["hardcover_book_id"]
    }
    by_title = {r["title"].casefold().strip(): r for r in local}

    out = []
    for book in books:
        match = by_hc_id.get(book["hardcover_book_id"]) or by_title.get(
            book["title"].casefold().strip()
        )
        if match and match["owned"]:
            status = "owned"
        elif match and match["wishlist"]:
            status = "wishlist"
        else:
            status = "missing"
        out.append({**book, "status": status, "series_name": name})

    missing = sum(1 for book in out if book["status"] == "missing")

    # Only an accessible local series is allowed to populate the shared cache.
    if local:
        with get_db() as db:
            _upsert_series_check(db, name, len(out), missing)

    return {
        "ok": True,
        "series": name,
        "total": len(out),
        "missing": missing,
        "books": out,
    }


'''
text = text[:start] + check_series + text[end:]
path.write_text(text)

# Series presentation: personal Wishlist is explicit, never inferred from
# shared catalogue ownership.
path = Path("app/templates/series.html")
text = path.read_text()
old = "{{ s.owned_count }} owned{% if s['items']|length != s.owned_count %} · {{ s['items']|length - s.owned_count }} wishlisted{% endif %}"
new = "{{ s.owned_count }} owned{% if s.wishlist_count %} · {{ s.wishlist_count }} on your wishlist{% endif %}"
if old not in text:
    raise SystemExit("series count copy anchor not found")
text = text.replace(old, new, 1)
old = "{% if not item.owned %}\n                    <span class=\"absolute bottom-1 left-1 bg-shelf-warning/90 text-black text-[9px] px-1 rounded font-medium\">Wishlist</span>"
new = "{% if item.wishlist %}\n                    <span class=\"absolute bottom-1 left-1 bg-shelf-warning/90 text-black text-[9px] px-1 rounded font-medium\">Wishlist</span>"
if old not in text:
    raise SystemExit("series Wishlist badge anchor not found")
path.write_text(text.replace(old, new, 1))

# Music: its catalogue page and item routes must use the same library ACL as
# Browse/item detail. The refresh mutation is a direct item mutation, so make
# it item-editor aware while the file is being secured.
path = Path("app/routers/music.py")
text = path.read_text()
text = text.replace(
    "from app.auth import require_role\n",
    "from app.auth import require_item_role, require_role\n",
    1,
)
text = text.replace(
    "from app.services import covers, music_catalog, musicbrainz\n",
    "from app.services import covers, libraries, music_catalog, musicbrainz\n",
    1,
)
old = '''    placeholders, music_types = _music_types_sql()
    with get_db() as db:
        items = db.execute(
            f"""SELECT i.id, i.title, i.authors, i.media_type, i.cover_path,
                       i.publish_year, mr.format_summary, mr.catalog_number,
                       mr.release_date
                FROM items i
                LEFT JOIN music_releases mr ON mr.item_id = i.id
                WHERE i.media_type IN ({placeholders})
                ORDER BY i.authors COLLATE NOCASE, i.title COLLATE NOCASE, i.id
                LIMIT 250""",
            music_types,
        ).fetchall()
'''
new = '''    placeholders, music_types = _music_types_sql()
    actor = dict(request.state.user)
    with get_db() as db:
        access_sql, access_params = libraries.item_access_condition(
            actor, item_alias="i"
        )
        items = db.execute(
            f"""SELECT i.id, i.title, i.authors, i.media_type, i.cover_path,
                       i.publish_year, mr.format_summary, mr.catalog_number,
                       mr.release_date
                FROM items i
                LEFT JOIN music_releases mr ON mr.item_id = i.id
                WHERE i.media_type IN ({placeholders})
                  AND {access_sql}
                ORDER BY i.authors COLLATE NOCASE, i.title COLLATE NOCASE, i.id
                LIMIT 250""",
            [*music_types, *access_params],
        ).fetchall()
'''
if old not in text:
    raise SystemExit("music page query anchor not found")
text = text.replace(old, new, 1)
old = '''async def music_item_page(
    request: Request,
    item_id: int,
    _=Depends(require_role("viewer")),
):'''
new = '''async def music_item_page(
    request: Request,
    item_id: int,
    _=Depends(require_item_role("viewer")),
):'''
if old not in text:
    raise SystemExit("music item dependency anchor not found")
text = text.replace(old, new, 1)
old = '''async def refresh_music_release(
    item_id: int,
    _=Depends(require_role("editor")),
):'''
new = '''async def refresh_music_release(
    item_id: int,
    _=Depends(require_item_role("editor")),
):'''
if old not in text:
    raise SystemExit("music refresh dependency anchor not found")
path.write_text(text.replace(old, new, 1))

# Update the two legacy Series tests whose old meaning was owned=0 == Wishlist.
# Current product semantics make Wishlist personal.
path = Path("tests/test_series.py")
text = path.read_text()
text = text.replace(
    "from app.routers.series import find_gaps\n",
    "from app.routers.series import find_gaps\nfrom app.services import user_state\n",
    1,
)
old = '''    def test_wishlist_items_badged(self, admin_client, db):
        _insert_item(db, title="Want It", isbn="9789000003563", series_name="Solo", series_position=1, owned=0)
        db.execute("COMMIT")
        html = admin_client.get("/series").text
        assert "Solo" in html
        assert "1 wishlisted" in html
'''
new = '''    def test_wishlist_items_badged(self, admin_client, admin_user, db):
        item_id = _insert_item(
            db,
            title="Want It",
            isbn="9789000003563",
            series_name="Solo",
            series_position=1,
            owned=0,
        )
        user_state.save_state(db, admin_user["id"], item_id, wishlist=1)
        db.execute("COMMIT")
        html = admin_client.get("/series").text
        assert "Solo" in html
        assert "1 on your wishlist" in html
'''
if old not in text:
    raise SystemExit("legacy Series Wishlist page test anchor not found")
text = text.replace(old, new, 1)
old = '''    def test_classification(self, admin_client, db):
        self._seed(db)
        with patch("app.services.hardcover.get_series_books",
                   new=AsyncMock(return_value=self._hc_books())):
            data = admin_client.get("/api/series/check", params={"name": "Dune Saga"}).json()
'''
new = '''    def test_classification(self, admin_client, admin_user, db):
        self._seed(db)
        item_id = db.execute(
            "SELECT id FROM items WHERE title = 'Dune Messiah'"
        ).fetchone()["id"]
        user_state.save_state(db, admin_user["id"], item_id, wishlist=1)
        db.commit()
        with patch("app.services.hardcover.get_series_books",
                   new=AsyncMock(return_value=self._hc_books())):
            data = admin_client.get("/api/series/check", params={"name": "Dune Saga"}).json()
'''
if old not in text:
    raise SystemExit("legacy Series classification test anchor not found")
path.write_text(text.replace(old, new, 1))
