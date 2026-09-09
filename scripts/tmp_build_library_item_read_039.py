from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1))


# This adapter runs on top of the library Browse-scope PR, where pages.py
# already imports libraries and the central bound access predicate exists.
pages = Path("app/routers/pages.py")
if "from app.services import item_copies, libraries" not in pages.read_text():
    raise SystemExit("library Browse-scope prerequisite is missing")

# Main item detail: a hidden item follows the same established missing-item
# redirect as an unknown item, while secondary rows on a visible detail page
# are drawn only from libraries the actor may read.
replace_once(
    "app/routers/pages.py",
    '''    back = nav.back_target(from_)\n    with get_db() as db:\n        item = db.execute(\n''',
    '''    back = nav.back_target(from_)\n    user = dict(request.state.user)\n    with get_db() as db:\n        if not libraries.has_item_role(db, user, item_id, "viewer"):\n            return RedirectResponse(url="/browse", status_code=303)\n        access_sql, access_params = libraries.item_access_condition(\n            user, item_alias="i"\n        )\n        item = db.execute(\n''',
)
replace_once(
    "app/routers/pages.py",
    '''            "WHERE (il.item_a_id = ? OR il.item_b_id = ?) AND il.link_type = 'format'",\n            (item_id, item_id, item_id),\n''',
    '''            "WHERE (il.item_a_id = ? OR il.item_b_id = ?) AND il.link_type = 'format' "\n            f"AND ({access_sql})",\n            [item_id, item_id, item_id] + access_params,\n''',
)
replace_once(
    "app/routers/pages.py",
    '''        from app.routers.tags import get_item_tags, get_all_tags\n        item_tags = get_item_tags(db, item_id)\n        all_tags = get_all_tags(db)\n''',
    '''        from app.routers.tags import get_item_tags\n        item_tags = get_item_tags(db, item_id)\n        all_tags = db.execute(\n            "SELECT t.id, t.name, COUNT(DISTINCT it.item_id) AS count "\n            "FROM tags t JOIN item_tags it ON it.tag_id = t.id "\n            "JOIN items i ON i.id = it.item_id "\n            f"WHERE {access_sql} GROUP BY t.id, t.name ORDER BY t.name COLLATE NOCASE",\n            access_params,\n        ).fetchall()\n''',
)
replace_once(
    "app/routers/pages.py",
    '''            siblings = db.execute(\n                "SELECT owned, series_position FROM items "\n                "WHERE series_name = ? COLLATE NOCASE",\n                (item["series_name"],),\n            ).fetchall()\n''',
    '''            siblings = db.execute(\n                "SELECT i.owned, i.series_position FROM items i "\n                f"WHERE i.series_name = ? COLLATE NOCASE AND ({access_sql})",\n                [item["series_name"]] + access_params,\n            ).fetchall()\n''',
)

# Cover polling intentionally returns a settled placeholder for a missing item
# so a deletion mid-poll does not cause an HTMX error. Treat an inaccessible
# item identically rather than turning the endpoint into an existence oracle.
replace_once(
    "app/routers/items_covers.py",
    "from app.services import covers, cover_queue, manual_cover, openlibrary, scan_outcome\n",
    "from app.services import covers, cover_queue, libraries, manual_cover, openlibrary, scan_outcome\n",
)
replace_once(
    "app/routers/items_covers.py",
    '''    templates = request.app.state.templates\n    attempt = max(0, min(attempt, MAX_COVER_POLLS))\n    with get_db() as db:\n        row = db.execute(\n            "SELECT cover_path FROM items WHERE id = ?", (item_id,)\n        ).fetchone()\n''',
    '''    templates = request.app.state.templates\n    attempt = max(0, min(attempt, MAX_COVER_POLLS))\n    actor = dict(request.state.user)\n    with get_db() as db:\n        row = db.execute(\n            "SELECT cover_path FROM items WHERE id = ?", (item_id,)\n        ).fetchone()\n        if row and not libraries.has_item_role(db, actor, item_id, "viewer"):\n            row = None\n''',
)

# A specialised music item page is another direct item read. Scope the item
# query itself so hidden and unknown rows both take the existing missing path.
replace_once(
    "app/routers/music.py",
    "from app.services import covers, music_catalog, musicbrainz\n",
    "from app.services import covers, libraries, music_catalog, musicbrainz\n",
)
replace_once(
    "app/routers/music.py",
    '''    with get_db() as db:\n        item = db.execute(\n            "SELECT * FROM items WHERE id = ?", (item_id,)\n        ).fetchone()\n        if not item or item["media_type"] not in MUSIC_MEDIA_TYPES:\n''',
    '''    actor = dict(request.state.user)\n    with get_db() as db:\n        access_sql, access_params = libraries.item_access_condition(\n            actor, item_alias="i"\n        )\n        item = db.execute(\n            f"SELECT i.* FROM items i WHERE i.id = ? AND {access_sql}",\n            [item_id, *access_params],\n        ).fetchone()\n        if not item or item["media_type"] not in MUSIC_MEDIA_TYPES:\n''',
)

# Provider action lookups can disclose a private server URL for a guessed item
# id. Preserve the endpoint's existing {ok:false,url:null} shape for a hidden
# row, exactly as for a row with no provider action.
for path, service_import in (
    ("app/routers/komga.py", "komga_libraries, komga_sync"),
    ("app/routers/romm.py", "romm_client, romm_sync"),
):
    replace_once(path, "from app.database import get_db\n" if "from app.database import get_db\n" in Path(path).read_text() else "from app.auth import require_role\n",
                 "from app.auth import require_role\nfrom app.database import get_db\n" if "from app.database import get_db\n" not in Path(path).read_text() else "from app.database import get_db\n")
    replace_once(
        path,
        f"from app.services import {service_import}\n",
        f"from app.services import libraries, {service_import}\n",
    )
    replace_once(
        path,
        '''@router.get("/items/{item_id}/action", dependencies=[Depends(require_role("viewer"))])\nasync def item_action(item_id: int):\n    try:\n''',
        '''@router.get("/items/{item_id}/action", dependencies=[Depends(require_role("viewer"))])\nasync def item_action(request: Request, item_id: int):\n    actor = dict(request.state.user)\n    with get_db() as db:\n        if not libraries.has_item_role(db, actor, item_id, "viewer"):\n            return {"ok": False, "url": None}\n    try:\n''',
    )

# Covers used to bypass AuthMiddleware entirely. Once libraries can be private,
# the static file itself must honour the same item boundary as the page that
# references it. Keep StaticFiles' ETag/Last-Modified and Cache-Control logic.
replace_once(
    "app/main.py",
    '_SKIP_AUTH_PREFIXES = ("/static/", "/covers/", "/share/")\n',
    '_SKIP_AUTH_PREFIXES = ("/static/", "/share/")\n',
)
replace_once(
    "app/main.py",
    '''class CacheControlStaticFiles(StaticFiles):\n    def file_response(self, *args, **kwargs):\n        response = super().file_response(*args, **kwargs)\n        response.headers["Cache-Control"] = "no-cache"\n        return response\n\n\nstatic_dir = Path(__file__).parent.parent / "static"\n''',
    '''class CacheControlStaticFiles(StaticFiles):\n    def file_response(self, *args, **kwargs):\n        response = super().file_response(*args, **kwargs)\n        response.headers["Cache-Control"] = "no-cache"\n        return response\n\n\nclass LibraryCoverStaticFiles(CacheControlStaticFiles):\n    """Serve cached item covers only to actors who may read that item."""\n\n    async def get_response(self, path: str, scope):\n        user = (scope.get("state") or {}).get("user")\n        if not user:\n            return Response("Not found", status_code=404)\n        try:\n            item_id = int(Path(path).stem)\n        except (TypeError, ValueError):\n            return Response("Not found", status_code=404)\n\n        from app.services import libraries\n        with get_db() as db:\n            if not libraries.has_item_role(db, dict(user), item_id, "viewer"):\n                return Response("Not found", status_code=404)\n        return await super().get_response(path, scope)\n\n\nstatic_dir = Path(__file__).parent.parent / "static"\n''',
)
replace_once(
    "app/main.py",
    'app.mount("/covers", CacheControlStaticFiles(directory=str(COVERS_DIR)), name="covers")\n',
    'app.mount("/covers", LibraryCoverStaticFiles(directory=str(COVERS_DIR)), name="covers")\n',
)

# Existing caching regression now uses a real item-backed cover. Static JS stays
# public; covers no longer do.
replace_once(
    "tests/test_static_caching.py",
    '''    def test_covers_mount_sends_no_cache(self, client):\n        covers_dir = Path(_covers_mount_directory())\n        covers_dir.mkdir(parents=True, exist_ok=True)\n        cover = covers_dir / "test-static-caching.jpg"\n        cover.write_bytes(b"\\xff\\xd8\\xff\\xe0fakejpg")\n        try:\n            resp = client.get("/covers/test-static-caching.jpg")\n            assert resp.status_code == 200\n            assert "no-cache" in resp.headers["cache-control"]\n        finally:\n            cover.unlink(missing_ok=True)\n''',
    '''    def test_covers_mount_sends_no_cache(self, admin_client, db):\n        from tests.conftest import _insert_item\n\n        item_id = _insert_item(db, title="Static cache cover")\n        db.commit()\n        covers_dir = Path(_covers_mount_directory())\n        covers_dir.mkdir(parents=True, exist_ok=True)\n        cover = covers_dir / f"{item_id}.jpg"\n        cover.write_bytes(b"\\xff\\xd8\\xff\\xe0fakejpg")\n        try:\n            resp = admin_client.get(f"/covers/{item_id}.jpg")\n            assert resp.status_code == 200\n            assert "no-cache" in resp.headers["cache-control"]\n        finally:\n            cover.unlink(missing_ok=True)\n''',
)
# This older regression only needs one genuinely auth-skipped static prefix as
# its control case. /covers is no longer such a prefix; /static remains one.
p = Path("tests/test_cover_review.py")
s = p.read_text()
s = s.replace(
    'client.get("/covers/nothing-here.jpg", follow_redirects=False)',
    'client.get("/static/nothing-here.css", follow_redirects=False)',
    1,
)
s = s.replace(
    '"/covers/ is auth-skipped and statically mounted — this is exactly "',
    '"/static/ remains auth-skipped and statically mounted — this is exactly "',
    1,
)
p.write_text(s)

Path("tests/test_library_item_reads.py").write_text(r'''"""Direct item-read security regressions for first-class libraries."""

from pathlib import Path

from app.services import libraries
from tests.conftest import _insert_item


def _item_in_library(db, library_id: int, *, title: str, **fields) -> int:
    item_id = _insert_item(db, title=title, isbn=None, **fields)
    libraries.assign_item(db, item_id, library_id)
    return item_id


def _cover_mount_directory() -> Path:
    from app.main import app

    return Path(next(r for r in app.routes if getattr(r, "name", None) == "covers").app.directory)


def test_guessed_hidden_item_redirects_without_rendering_title(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    private = libraries.create_library(db, "Private direct reads")
    hidden = _item_in_library(db, private["id"], title="Hidden direct item")
    db.commit()

    response = viewer_client.get(f"/item/{hidden}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/browse"
    assert "Hidden direct item" not in response.text


def test_visible_main_library_item_still_opens(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    item_id = _item_in_library(db, 1, title="Visible direct item")
    db.commit()
    response = viewer_client.get(f"/item/{item_id}")
    assert response.status_code == 200
    assert "Visible direct item" in response.text


def test_hidden_format_link_is_not_rendered_on_visible_item(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    visible = _item_in_library(db, 1, title="Visible physical")
    private = libraries.create_library(db, "Hidden formats")
    hidden = _item_in_library(db, private["id"], title="Hidden digital")
    db.execute(
        "INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'format')",
        (visible, hidden),
    )
    db.commit()

    html = viewer_client.get(f"/item/{visible}").text
    assert "Visible physical" in html
    assert "Hidden digital" not in html


def test_hidden_only_tags_are_not_item_options(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    visible = _item_in_library(db, 1, title="Visible tagged item")
    private = libraries.create_library(db, "Hidden tags")
    hidden = _item_in_library(db, private["id"], title="Hidden tagged item")
    visible_tag = db.execute("INSERT INTO tags (name) VALUES ('Visible Tag')").lastrowid
    hidden_tag = db.execute("INSERT INTO tags (name) VALUES ('Hidden Tag')").lastrowid
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (visible, visible_tag))
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (hidden, hidden_tag))
    db.commit()

    html = viewer_client.get(f"/item/{visible}").text
    assert "Visible Tag" in html
    assert "Hidden Tag" not in html


def test_series_progress_ignores_hidden_library_siblings(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    visible = _item_in_library(
        db, 1, title="Saga One", series_name="Scoped Saga", series_position=1, owned=1
    )
    private = libraries.create_library(db, "Hidden series")
    _item_in_library(
        db, private["id"], title="Saga Two Hidden",
        series_name="Scoped Saga", series_position=2, owned=1,
    )
    db.commit()

    html = viewer_client.get(f"/item/{visible}").text
    assert "Saga Two Hidden" not in html
    assert "You own 2" not in html


def test_hidden_cover_poll_matches_missing_placeholder(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    private = libraries.create_library(db, "Hidden cover poll")
    hidden = _item_in_library(db, private["id"], title="Hidden cover")
    db.execute(
        "UPDATE items SET cover_path = ? WHERE id = ?",
        (f"covers/{hidden}.jpg", hidden),
    )
    db.commit()

    hidden_response = viewer_client.get(f"/api/items/{hidden}/cover-status")
    missing_response = viewer_client.get("/api/items/999999/cover-status")
    assert hidden_response.status_code == missing_response.status_code == 200
    assert hidden_response.text == missing_response.text.replace("999999", str(hidden))
    assert f"covers/{hidden}.jpg" not in hidden_response.text


def test_cover_file_itself_obeys_library_access(db, viewer_client, viewer_user, admin_client):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    private = libraries.create_library(db, "Private cover files")
    hidden = _item_in_library(db, private["id"], title="Secret Artwork")
    db.commit()

    covers_dir = _cover_mount_directory()
    covers_dir.mkdir(parents=True, exist_ok=True)
    cover = covers_dir / f"{hidden}.jpg"
    cover.write_bytes(b"secret-cover")
    try:
        denied = viewer_client.get(f"/covers/{hidden}.jpg")
        allowed = admin_client.get(f"/covers/{hidden}.jpg")
        assert denied.status_code == 404
        assert allowed.status_code == 200
        assert allowed.content == b"secret-cover"
    finally:
        cover.unlink(missing_ok=True)


def test_hidden_music_item_uses_same_missing_path(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    private = libraries.create_library(db, "Private music")
    hidden = _item_in_library(db, private["id"], title="Secret Album", media_type="cd")
    db.commit()

    hidden_response = viewer_client.get(f"/music/item/{hidden}", follow_redirects=False)
    missing_response = viewer_client.get("/music/item/999999", follow_redirects=False)
    assert hidden_response.status_code == missing_response.status_code == 303
    assert hidden_response.headers["location"] == f"/item/{hidden}"
    assert missing_response.headers["location"] == "/item/999999"


def test_provider_item_actions_do_not_leak_hidden_urls(
    db, viewer_client, viewer_user, monkeypatch
):
    from app.services import komga_sync, romm_sync

    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    private = libraries.create_library(db, "Private providers")
    hidden = _item_in_library(db, private["id"], title="Secret Provider Item")
    db.commit()

    monkeypatch.setattr(komga_sync, "item_action", lambda _item_id: "https://komga.private/item")
    monkeypatch.setattr(romm_sync, "item_action", lambda _item_id: "https://romm.private/item")

    for prefix in ("komga", "romm"):
        response = viewer_client.get(f"/api/{prefix}/items/{hidden}/action")
        assert response.status_code == 200
        assert response.json() == {"ok": False, "url": None}
''')
