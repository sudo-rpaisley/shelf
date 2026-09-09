from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1))


# Music catalogue listing: the specialised item page is already scoped by the
# preceding direct-read slice; this PR scopes the aggregate catalogue itself.
if "libraries" not in Path("app/routers/music.py").read_text().split("\n", 20)[-1]:
    pass
# Direct-read parent already imports libraries in music.py. Fail loudly if it
# does not, because stacking on the wrong parent would otherwise duplicate code.
if "from app.services import covers, libraries, music_catalog, musicbrainz" not in Path("app/routers/music.py").read_text():
    raise SystemExit("direct item-read prerequisite is missing from music.py")
replace_once(
    "app/routers/music.py",
    '''    placeholders, music_types = _music_types_sql()\n    with get_db() as db:\n        items = db.execute(\n''',
    '''    placeholders, music_types = _music_types_sql()\n    actor = dict(request.state.user)\n    with get_db() as db:\n        access_sql, access_params = libraries.item_access_condition(\n            actor, item_alias="i"\n        )\n        items = db.execute(\n''',
)
replace_once(
    "app/routers/music.py",
    '''                WHERE i.media_type IN ({placeholders})\n                ORDER BY i.authors COLLATE NOCASE, i.title COLLATE NOCASE, i.id\n                LIMIT 250""",\n            music_types,\n''',
    '''                WHERE i.media_type IN ({placeholders})\n                  AND {access_sql}\n                ORDER BY i.authors COLLATE NOCASE, i.title COLLATE NOCASE, i.id\n                LIMIT 250""",\n            [*music_types, *access_params],\n''',
)

# Series and the Unassigned strip must use one access predicate. Hardcover
# checks are allowed only when the acting user actually sees a local member of
# that series; hidden-only and nonexistent series share the same response.
replace_once(
    "app/routers/series.py",
    "from app.services import hardcover\n",
    "from app.services import hardcover, libraries\n",
)
replace_once(
    "app/routers/series.py",
    '''async def series_page(request: Request, _=Depends(require_role("viewer"))):\n    templates = request.app.state.templates\n    with get_db() as db:\n        rows = db.execute(\n            "SELECT id, title, authors, cover_path, series_name, series_position, "\n            "owned, reading_status FROM items WHERE series_name IS NOT NULL "\n            "AND TRIM(series_name) != '' "\n            "ORDER BY series_name COLLATE NOCASE, "\n            "series_position IS NULL, series_position, title COLLATE NOCASE"\n        ).fetchall()\n''',
    '''async def series_page(request: Request, _=Depends(require_role("viewer"))):\n    templates = request.app.state.templates\n    actor = dict(request.state.user)\n    with get_db() as db:\n        access_sql, access_params = libraries.item_access_condition(\n            actor, item_alias="i"\n        )\n        rows = db.execute(\n            "SELECT i.id, i.title, i.authors, i.cover_path, i.series_name, "\n            "i.series_position, i.owned, i.reading_status FROM items i "\n            "WHERE i.series_name IS NOT NULL AND TRIM(i.series_name) != '' "\n            f"AND {access_sql} "\n            "ORDER BY i.series_name COLLATE NOCASE, "\n            "i.series_position IS NULL, i.series_position, i.title COLLATE NOCASE",\n            access_params,\n        ).fetchall()\n''',
)
replace_once(
    "app/routers/series.py",
    '''        _unassigned_where = (\n            "(series_name IS NULL OR TRIM(series_name) = '') "\n            f"AND media_type IN ({','.join('?' * len(UNASSIGNED_MEDIA_TYPES))})"\n        )\n        unassigned_total = db.execute(\n            f"SELECT COUNT(*) FROM items WHERE {_unassigned_where}",\n            UNASSIGNED_MEDIA_TYPES,\n        ).fetchone()[0]\n        unassigned_items = [dict(r) for r in db.execute(\n            "SELECT id, title, authors, cover_path, series_name, series_position, "\n            f"owned, reading_status FROM items WHERE {_unassigned_where} "\n            "ORDER BY title COLLATE NOCASE LIMIT ?",\n            (*UNASSIGNED_MEDIA_TYPES, UNASSIGNED_STRIP_CAP),\n        ).fetchall()]\n''',
    '''        unassigned_where = (\n            "(i.series_name IS NULL OR TRIM(i.series_name) = '') "\n            f"AND i.media_type IN ({','.join('?' * len(UNASSIGNED_MEDIA_TYPES))})"\n        )\n        unassigned_total = db.execute(\n            f"SELECT COUNT(*) FROM items i WHERE {unassigned_where} AND {access_sql}",\n            [*UNASSIGNED_MEDIA_TYPES, *access_params],\n        ).fetchone()[0]\n        unassigned_items = [dict(r) for r in db.execute(\n            "SELECT i.id, i.title, i.authors, i.cover_path, i.series_name, "\n            f"i.series_position, i.owned, i.reading_status FROM items i WHERE {unassigned_where} "\n            f"AND {access_sql} ORDER BY i.title COLLATE NOCASE LIMIT ?",\n            [*UNASSIGNED_MEDIA_TYPES, *access_params, UNASSIGNED_STRIP_CAP],\n        ).fetchall()]\n''',
)
replace_once(
    "app/routers/series.py",
    '''@router.get("/api/series/check")\nasync def check_series(name: str = "", _=Depends(require_role("viewer"))):\n    """Compare a local series against Hardcover's full listing."""\n''',
    '''@router.get("/api/series/check")\nasync def check_series(\n    request: Request, name: str = "", _=Depends(require_role("viewer"))\n):\n    """Compare an accessible local series against Hardcover's full listing."""\n''',
)
replace_once(
    "app/routers/series.py",
    '''    with get_db() as db:\n        token = get_setting(db, "hardcover_token")\n        if not token:\n            return {"ok": False, "message": "Hardcover integration not configured"}\n        local = db.execute(\n            "SELECT title, owned, hardcover_book_id FROM items "\n            "WHERE series_name = ? COLLATE NOCASE",\n            (name,),\n        ).fetchall()\n\n    books = await hardcover.get_series_books(name, token)\n''',
    '''    actor = dict(request.state.user)\n    with get_db() as db:\n        token = get_setting(db, "hardcover_token")\n        if not token:\n            return {"ok": False, "message": "Hardcover integration not configured"}\n        access_sql, access_params = libraries.item_access_condition(\n            actor, item_alias="i"\n        )\n        local = db.execute(\n            "SELECT i.title, i.owned, i.hardcover_book_id FROM items i "\n            f"WHERE i.series_name = ? COLLATE NOCASE AND {access_sql}",\n            [name, *access_params],\n        ).fetchall()\n    if not local:\n        return {"ok": False, "message": "Series not found"}\n\n    books = await hardcover.get_series_books(name, token)\n''',
)

Path("tests/test_library_secondary_reads_039.py").write_text(r'''"""Library scoping for Series and Music aggregate read surfaces."""

from app.database import set_setting
from app.services import libraries
from tests.conftest import _insert_item


def _item(db, library_id: int, title: str, **fields) -> int:
    item_id = _insert_item(db, title=title, isbn=None, **fields)
    libraries.assign_item(db, item_id, library_id)
    return item_id


def test_series_page_and_unassigned_strip_hide_private_items(
    db, viewer_client, viewer_user
):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    _item(db, 1, "Visible Saga One", series_name="Visible Saga", series_position=1)
    _item(db, 1, "Visible Unassigned")
    private = libraries.create_library(db, "Private Series")
    _item(db, private["id"], "Hidden Saga One", series_name="Hidden Saga", series_position=1)
    _item(db, private["id"], "Hidden Unassigned")
    db.commit()

    html = viewer_client.get("/series").text
    assert "Visible Saga" in html
    assert "Visible Unassigned" in html
    assert "Hidden Saga" not in html
    assert "Hidden Unassigned" not in html


def test_music_page_hides_private_library_releases(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    _item(db, 1, "Visible Album", media_type="cd", authors="Visible Artist")
    private = libraries.create_library(db, "Private Music")
    _item(db, private["id"], "Hidden Album", media_type="vinyl", authors="Hidden Artist")
    db.commit()

    html = viewer_client.get("/music").text
    assert "Visible Album" in html
    assert "Hidden Album" not in html
    assert "Hidden Artist" not in html


def test_hidden_only_series_check_matches_nonexistent_and_skips_provider(
    db, viewer_client, viewer_user, monkeypatch
):
    from app.services import hardcover

    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    private = libraries.create_library(db, "Hidden Hardcover Series")
    _item(db, private["id"], "Hidden Check", series_name="Secret Saga")
    set_setting(db, "hardcover_token", "test-token")
    db.commit()

    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("Hardcover must not be called for an inaccessible series")

    monkeypatch.setattr(hardcover, "get_series_books", should_not_run)
    hidden = viewer_client.get("/api/series/check", params={"name": "Secret Saga"})
    missing = viewer_client.get("/api/series/check", params={"name": "No Such Saga"})
    assert hidden.status_code == missing.status_code == 200
    assert hidden.json() == missing.json() == {"ok": False, "message": "Series not found"}


def test_admin_retains_global_series_and_music_visibility(db, admin_client):
    private = libraries.create_library(db, "Admin Secondary Reads")
    _item(db, private["id"], "Admin Hidden Saga", series_name="Admin Saga")
    _item(db, private["id"], "Admin Hidden Album", media_type="cd")
    db.commit()

    assert "Admin Hidden Saga" in admin_client.get("/series").text
    assert "Admin Hidden Album" in admin_client.get("/music").text
''')
