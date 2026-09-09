from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


# Migrations 41-44 belong to the Collections foundation. Public sharing is the
# next schema-owning slice so it gets 45-47 without colliding with sibling PRs.
replace_once(
    "app/database.py",
    '''    (44, "Index collections by library",\n     "CREATE INDEX IF NOT EXISTS idx_collections_library ON collections(library_id, name COLLATE NOCASE)"),\n)\n''',
    '''    (44, "Index collections by library",\n     "CREATE INDEX IF NOT EXISTS idx_collections_library ON collections(library_id, name COLLATE NOCASE)"),\n    (45, "Scope public share links to a library",\n     "ALTER TABLE share_links ADD COLUMN library_id INTEGER REFERENCES libraries(id) ON DELETE CASCADE"),\n    (46, "Backfill existing share links to Main Library",\n     "UPDATE share_links SET library_id = 1 WHERE library_id IS NULL"),\n    (47, "Index public share links by library",\n     "CREATE INDEX IF NOT EXISTS idx_share_links_library ON share_links(library_id, created_at)"),\n)\n''',
)

replace_once(
    "app/database.py",
    '''CREATE TABLE IF NOT EXISTS share_links (\n    id         INTEGER PRIMARY KEY AUTOINCREMENT,\n    token      TEXT NOT NULL UNIQUE,\n    scope      TEXT NOT NULL DEFAULT 'wishlist',\n    label      TEXT,\n    created_at TEXT NOT NULL DEFAULT (datetime('now'))\n);\n''',
    '''CREATE TABLE IF NOT EXISTS share_links (\n    id         INTEGER PRIMARY KEY AUTOINCREMENT,\n    token      TEXT NOT NULL UNIQUE,\n    scope      TEXT NOT NULL DEFAULT 'wishlist',\n    label      TEXT,\n    library_id INTEGER REFERENCES libraries(id) ON DELETE CASCADE,\n    created_at TEXT NOT NULL DEFAULT (datetime('now'))\n);\nCREATE INDEX IF NOT EXISTS idx_share_links_library\n    ON share_links(library_id, created_at);\n''',
)

# Replace the small share router wholesale so the public page and public cover
# route share one token/library/scope contract.
Path("app/routers/share.py").write_text(r'''"""Public read-only share links scoped to one Shelf library.

``/share/<token>`` remains intentionally unauthenticated. The token authorises
only the selected library and share scope; it never grants general catalogue
access. Public cover images use the same token boundary rather than the global
``/covers`` mount.
"""
from pathlib import Path
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app.auth import require_role
from app.config import COVERS_DIR
from app.database import get_db

router = APIRouter()

SCOPES = ("wishlist", "collection")
SHARE_ITEM_CAP = 500


def _share_link(db, token: str):
    return db.execute(
        "SELECT sl.*, l.name AS library_name FROM share_links sl "
        "LEFT JOIN libraries l ON l.id = sl.library_id WHERE sl.token = ?",
        (token,),
    ).fetchone()


def _share_owned(scope: str) -> int:
    return 0 if scope == "wishlist" else 1


@router.get("/share/{token}")
async def share_page(request: Request, token: str):
    templates = request.app.state.templates
    with get_db() as db:
        link = _share_link(db, token)
        if not link or link["library_id"] is None:
            return HTMLResponse(
                "Not found", status_code=404, headers={"X-Robots-Tag": "noindex"}
            )

        owned = _share_owned(link["scope"])
        # Minimal field set on purpose. Library identity is used only to bound
        # the query and label the public page; locations, loans, values, notes
        # and identifiers remain private.
        items = db.execute(
            "SELECT i.id, i.title, i.authors, i.cover_path, i.media_type, "
            "i.publish_year, i.series_name, i.series_position "
            "FROM items i JOIN library_items li ON li.item_id = i.id "
            "WHERE li.library_id = ? AND i.owned = ? "
            "ORDER BY i.title COLLATE NOCASE LIMIT ?",
            (link["library_id"], owned, SHARE_ITEM_CAP),
        ).fetchall()

    resp = templates.TemplateResponse(
        request,
        "share.html",
        {"link": link, "items": items, "scope": link["scope"]},
    )
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@router.get("/share/{token}/cover/{item_id}.jpg")
async def share_cover(token: str, item_id: int):
    """Serve one cover only when the share token exposes that exact item."""
    with get_db() as db:
        link = _share_link(db, token)
        if not link or link["library_id"] is None:
            return HTMLResponse("Not found", status_code=404)
        row = db.execute(
            "SELECT i.cover_path FROM items i "
            "JOIN library_items li ON li.item_id = i.id "
            "WHERE i.id = ? AND li.library_id = ? AND i.owned = ?",
            (item_id, link["library_id"], _share_owned(link["scope"])),
        ).fetchone()

    if not row or not row["cover_path"]:
        return HTMLResponse("Not found", status_code=404)
    filename = Path(str(row["cover_path"])).name
    if filename != f"{item_id}.jpg":
        return HTMLResponse("Not found", status_code=404)
    cover = COVERS_DIR / filename
    if not cover.is_file():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(cover, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@router.post("/api/share")
async def create_share_link(
    scope: str = Form("wishlist"),
    label: str = Form(""),
    library_id: int = Form(1),
    _=Depends(require_role("admin")),
):
    if scope not in SCOPES:
        return JSONResponse(
            {"ok": False, "message": "Invalid share scope"}, status_code=400
        )
    with get_db() as db:
        library = db.execute(
            "SELECT id FROM libraries WHERE id = ? AND is_archived = 0",
            (library_id,),
        ).fetchone()
        if not library:
            return JSONResponse(
                {"ok": False, "message": "Library not found"}, status_code=404
            )
        token = secrets.token_urlsafe(16)
        db.execute(
            "INSERT INTO share_links (token, scope, label, library_id) VALUES (?, ?, ?, ?)",
            (token, scope, label.strip()[:100] or None, library_id),
        )
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/api/share/{link_id}/delete")
async def revoke_share_link(link_id: int, _=Depends(require_role("admin"))):
    with get_db() as db:
        cursor = db.execute("DELETE FROM share_links WHERE id = ?", (link_id,))
        if cursor.rowcount != 1:
            return JSONResponse(
                {"ok": False, "message": "Share link not found"}, status_code=404
            )
    return RedirectResponse(url="/settings", status_code=303)
''')

# Settings must show which library an existing link exposes and offer an
# explicit library selector for new links.
replace_once(
    "app/routers/pages.py",
    "from app.services import cover_queue\n",
    "from app.services import cover_queue, libraries\n",
)
replace_once(
    "app/routers/pages.py",
    '''        share_links = db.execute(\n            "SELECT * FROM share_links ORDER BY created_at DESC"\n        ).fetchall()\n''',
    '''        share_links = db.execute(\n            "SELECT sl.*, l.name AS library_name FROM share_links sl "\n            "LEFT JOIN libraries l ON l.id = sl.library_id "\n            "ORDER BY sl.created_at DESC"\n        ).fetchall()\n        share_libraries = libraries.list_libraries(db)\n''',
)
replace_once(
    "app/routers/pages.py",
    '''{"settings": settings, "locations": locations, "item_count": item_count, "share_links": share_links,\n''',
    '''{"settings": settings, "locations": locations, "item_count": item_count, "share_links": share_links,\n         "share_libraries": share_libraries,\n''',
)

replace_once(
    "app/templates/share.html",
    '''                · shared read-only via <span class="text-shelf-accent2 font-medium">Shelf</span>\n''',
    '''                · {{ link.library_name or 'Main Library' }} · shared read-only via <span class="text-shelf-accent2 font-medium">Shelf</span>\n''',
)
replace_once(
    "app/templates/share.html",
    '''                    <img src="/{{ item.cover_path }}" alt="" loading="lazy">\n''',
    '''                    <img src="/share/{{ link.token }}/cover/{{ item.id }}.jpg" alt="" loading="lazy">\n''',
)

replace_once(
    "app/templates/fragments/settings/data.html",
    '''        <p class="text-sm text-shelf-muted mb-4">Public read-only links to your wishlist (for gift ideas) or collection. Anyone with the link can view titles, authors, and covers — never locations, loans, or values. Revoke a link at any time.</p>\n''',
    '''        <p class="text-sm text-shelf-muted mb-4">Public read-only links to one selected library's wishlist (for gift ideas) or owned collection. Anyone with the link can view titles, authors, and covers — never locations, loans, or values. Revoke a link at any time.</p>\n''',
)
replace_once(
    "app/templates/fragments/settings/data.html",
    '''                <span class="text-sm">{{ sl.label or 'Untitled link' }}</span>\n                <code class="text-xs text-shelf-muted truncate max-w-[220px]">/share/{{ sl.token }}</code>\n''',
    '''                <span class="text-sm">{{ sl.label or 'Untitled link' }}</span>\n                <span class="text-xs text-shelf-muted">{{ sl.library_name or 'Main Library' }}</span>\n                <code class="text-xs text-shelf-muted truncate max-w-[220px]">/share/{{ sl.token }}</code>\n''',
)
replace_once(
    "app/templates/fragments/settings/data.html",
    '''            <select name="scope" class="bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text">\n                <option value="wishlist">Wishlist</option>\n                <option value="collection">Collection</option>\n            </select>\n            <input type="text" name="label" placeholder="Label (e.g. Dan's gift ideas)..." maxlength="100"\n''',
    '''            <select name="scope" class="bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text">\n                <option value="wishlist">Wishlist</option>\n                <option value="collection">Collection</option>\n            </select>\n            <select name="library_id" class="bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text">\n                {% for library in share_libraries %}\n                <option value="{{ library.id }}"{% if library.id == 1 %} selected{% endif %}>{{ library.name }}</option>\n                {% endfor %}\n            </select>\n            <input type="text" name="label" placeholder="Label (e.g. Dan's gift ideas)..." maxlength="100"\n''',
)

# Adapt existing share tests and add privacy/cover/migration regressions.
p = Path("tests/test_share_links.py")
s = p.read_text()
s = s.replace(
    'def _create_link(admin_client, scope="wishlist", label="Test Link"):\n    resp = admin_client.post("/api/share", data={"scope": scope, "label": label},\n                             follow_redirects=False)\n',
    'def _create_link(admin_client, scope="wishlist", label="Test Link", library_id=1):\n    resp = admin_client.post(\n        "/api/share",\n        data={"scope": scope, "label": label, "library_id": library_id},\n        follow_redirects=False,\n    )\n',
    1,
)
s = s.rstrip() + r'''


class TestLibraryScopedSharing:
    def test_main_library_link_does_not_expose_private_library_item(
        self, admin_client, client, db
    ):
        from app.services import libraries

        _insert_item(db, title="Shared Main Item", isbn=None, owned=1)
        private = libraries.create_library(db, "Private Share Library")
        hidden = _insert_item(db, title="Private Hidden Item", isbn=None, owned=1)
        libraries.assign_item(db, hidden, private["id"])
        db.commit()

        link = _create_link(admin_client, scope="collection", library_id=1)
        client.cookies.clear()
        html = client.get(f"/share/{link['token']}").text
        assert "Shared Main Item" in html
        assert "Private Hidden Item" not in html

    def test_selected_private_library_link_exposes_only_that_library(
        self, admin_client, client, db
    ):
        from app.services import libraries

        _insert_item(db, title="Main Owned Item", isbn=None, owned=1)
        private = libraries.create_library(db, "Shared Private Library")
        private_item = _insert_item(db, title="Selected Library Item", isbn=None, owned=1)
        libraries.assign_item(db, private_item, private["id"])
        db.commit()

        link = _create_link(
            admin_client, scope="collection", library_id=private["id"]
        )
        client.cookies.clear()
        html = client.get(f"/share/{link['token']}").text
        assert "Selected Library Item" in html
        assert "Main Owned Item" not in html
        assert "Shared Private Library" in html

    def test_invalid_or_archived_library_cannot_be_shared(self, admin_client, db):
        from app.services import libraries

        before = db.execute("SELECT COUNT(*) FROM share_links").fetchone()[0]
        missing = admin_client.post(
            "/api/share",
            data={"scope": "collection", "library_id": 999999},
            follow_redirects=False,
        )
        assert missing.status_code == 404

        archived = libraries.create_library(db, "Archived Share Library")
        db.execute("UPDATE libraries SET is_archived = 1 WHERE id = ?", (archived["id"],))
        db.commit()
        response = admin_client.post(
            "/api/share",
            data={"scope": "collection", "library_id": archived["id"]},
            follow_redirects=False,
        )
        assert response.status_code == 404
        assert db.execute("SELECT COUNT(*) FROM share_links").fetchone()[0] == before

    def test_public_cover_route_is_bound_to_token_library_and_scope(
        self, admin_client, client, db
    ):
        from app.routers import share as share_router
        from app.services import libraries

        visible = _insert_item(db, title="Shared Cover Item", isbn=None, owned=1)
        hidden_library = libraries.create_library(db, "Hidden Cover Library")
        hidden = _insert_item(db, title="Hidden Cover Item", isbn=None, owned=1)
        libraries.assign_item(db, hidden, hidden_library["id"])
        db.execute(
            "UPDATE items SET cover_path = ? WHERE id = ?",
            (f"covers/{visible}.jpg", visible),
        )
        db.execute(
            "UPDATE items SET cover_path = ? WHERE id = ?",
            (f"covers/{hidden}.jpg", hidden),
        )
        db.commit()

        share_router.COVERS_DIR.mkdir(parents=True, exist_ok=True)
        visible_file = share_router.COVERS_DIR / f"{visible}.jpg"
        hidden_file = share_router.COVERS_DIR / f"{hidden}.jpg"
        visible_file.write_bytes(b"visible-cover")
        hidden_file.write_bytes(b"hidden-cover")
        try:
            link = _create_link(admin_client, scope="collection", library_id=1)
            client.cookies.clear()
            ok = client.get(f"/share/{link['token']}/cover/{visible}.jpg")
            assert ok.status_code == 200
            assert ok.content == b"visible-cover"
            assert "no-cache" in ok.headers["cache-control"]
            assert client.get(
                f"/share/{link['token']}/cover/{hidden}.jpg"
            ).status_code == 404
        finally:
            visible_file.unlink(missing_ok=True)
            hidden_file.unlink(missing_ok=True)

    def test_share_migrations_backfill_existing_links_to_main_library(self):
        import sqlite3
        from app import database

        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE libraries (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO libraries (id) VALUES (1)")
        conn.execute(
            "CREATE TABLE share_links ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL UNIQUE, "
            "scope TEXT NOT NULL DEFAULT 'wishlist', label TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO share_links (token, scope, label, created_at) "
            "VALUES ('legacy-token', 'wishlist', 'Legacy', datetime('now'))"
        )
        migrations = {
            number: sql for number, _description, sql in database.MIGRATIONS
            if 45 <= number <= 47
        }
        assert sorted(migrations) == [45, 46, 47]
        for number in (45, 46, 47):
            conn.execute(migrations[number])
        row = conn.execute(
            "SELECT library_id FROM share_links WHERE token = 'legacy-token'"
        ).fetchone()
        assert row[0] == 1
'''
p.write_text(s + "\n")
