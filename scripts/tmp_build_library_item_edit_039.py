from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


# One item-local dependency keeps the denial semantics consistent across HTML
# pages, HTMX fragments and JSON/API mutations. A visible item with insufficient
# edit rights remains a normal 403; an inaccessible item is hidden behind the
# same missing-item surface as a guessed id.
replace_once(
    "app/auth.py",
    '''def _raise_auth_required(request: Request):\n''',
    '''def require_item_role(minimum_role: str):\n    """FastAPI dependency for access governed by an item's Shelf library."""\n    if minimum_role not in ("viewer", "editor"):\n        raise ValueError("Item role must be viewer or editor")\n\n    async def _dependency(request: Request):\n        user = getattr(request.state, "user", None)\n        if not user:\n            _raise_auth_required(request)\n\n        raw_item_id = request.path_params.get("item_id")\n        try:\n            item_id = int(raw_item_id)\n        except (TypeError, ValueError):\n            _raise_item_not_found(request)\n\n        from app.services import libraries\n        with get_db() as db:\n            actor = dict(user)\n            exists = db.execute(\n                "SELECT 1 FROM items WHERE id = ?", (item_id,)\n            ).fetchone() is not None\n            if not exists:\n                return user\n            allowed = libraries.has_item_role(\n                db, actor, item_id, minimum_role\n            )\n            visible = libraries.has_item_role(db, actor, item_id, "viewer")\n\n        if not allowed:\n            if minimum_role == "editor" and visible:\n                _raise_insufficient_role(request)\n            _raise_item_not_found(request)\n        return user\n\n    return _dependency\n\n\ndef _raise_item_not_found(request: Request):\n    """Hide inaccessible item identities behind the established missing surface."""\n    if request.headers.get("HX-Request") or request.url.path.startswith("/api/"):\n        raise _ResponseException(HTMLResponse("Not found", status_code=404))\n    raise _ResponseException(RedirectResponse(url="/browse", status_code=303))\n\n\ndef _raise_auth_required(request: Request):\n''',
)

# Item edit page.
replace_once(
    "app/routers/pages.py",
    "from app.auth import require_role\n",
    "from app.auth import require_item_role, require_role\n",
)
replace_once(
    "app/routers/pages.py",
    '_=Depends(require_role("editor")),\n):\n    back = nav.back_target(from_)\n',
    '_=Depends(require_item_role("editor")),\n):\n    back = nav.back_target(from_)\n',
)

# Core item mutations. Do not change global/manual-add/admin endpoints in this
# module; only routes whose path contains one concrete item id become local.
replace_once(
    "app/routers/items.py",
    "from app.auth import require_role\n",
    "from app.auth import require_item_role, require_role\n",
)
for old, new in (
    ('async def update_item(request: Request, item_id: int, _=Depends(require_role("editor"))):',
     'async def update_item(request: Request, item_id: int, _=Depends(require_item_role("editor"))):'),
    ('async def set_reading_status(request: Request, item_id: int, status: str = Form(""), _=Depends(require_role("viewer"))):',
     'async def set_reading_status(request: Request, item_id: int, status: str = Form(""), _=Depends(require_item_role("viewer"))):'),
    ('async def fetch_synopsis(item_id: int, _=Depends(require_role("editor"))):',
     'async def fetch_synopsis(item_id: int, _=Depends(require_item_role("editor"))):'),
    ('async def delete_item(item_id: int, _=Depends(require_role("editor"))):',
     'async def delete_item(item_id: int, _=Depends(require_item_role("editor"))):'),
):
    replace_once("app/routers/items.py", old, new)

# Every editor-gated route in items_covers.py is item-specific; bulk sweeps are
# admin-only and therefore untouched by this replacement.
p = Path("app/routers/items_covers.py")
s = p.read_text()
s = s.replace(
    "from app.auth import require_role\n",
    "from app.auth import require_item_role, require_role\n",
    1,
)
editor_count = s.count('Depends(require_role("editor"))')
if editor_count < 5:
    raise SystemExit(f"unexpected item cover editor-route count: {editor_count}")
s = s.replace(
    'Depends(require_role("editor"))',
    'Depends(require_item_role("editor"))',
)
p.write_text(s)

# Tags are all item-local mutations.
p = Path("app/routers/tags.py")
s = p.read_text()
s = s.replace(
    "from app.auth import require_role\n",
    "from app.auth import require_item_role, require_role\n",
    1,
)
if s.count('Depends(require_role("editor"))') != 2:
    raise SystemExit("unexpected tag editor-route count")
s = s.replace(
    'Depends(require_role("editor"))',
    'Depends(require_item_role("editor"))',
)
p.write_text(s)

# Provider mutations that update one existing Shelf item.
replace_once(
    "app/routers/hardcover.py",
    "from app.auth import require_role\n",
    "from app.auth import require_item_role, require_role\n",
)
replace_once(
    "app/routers/hardcover.py",
    'async def push_to_hardcover(item_id: int, _=Depends(require_role("editor"))):',
    'async def push_to_hardcover(item_id: int, _=Depends(require_item_role("editor"))):',
)

# The direct-read parent already imports library visibility into music.py. The
# refresh is the item-local write counterpart; adding a new release remains a
# global editor action and is intentionally unchanged.
replace_once(
    "app/routers/music.py",
    "from app.auth import require_role\n",
    "from app.auth import require_item_role, require_role\n",
)
replace_once(
    "app/routers/music.py",
    '_=Depends(require_role("editor")),\n):\n    with get_db() as db:\n        item = db.execute(\n            "SELECT * FROM items WHERE id = ?", (item_id,)\n        ).fetchone()\n        release = music_catalog.get_release(db, item_id) if item else None\n',
    '_=Depends(require_item_role("editor")),\n):\n    with get_db() as db:\n        item = db.execute(\n            "SELECT * FROM items WHERE id = ?", (item_id,)\n        ).fetchone()\n        release = music_catalog.get_release(db, item_id) if item else None\n',
)

Path("tests/test_library_item_edit_039.py").write_text(r'''"""Item mutation permissions follow the item's Shelf library."""

from app.services import libraries
from tests.conftest import _insert_item


def _item(db, library_id: int, title: str, **fields) -> int:
    item_id = _insert_item(db, title=title, isbn=None, **fields)
    libraries.assign_item(db, item_id, library_id)
    return item_id


def test_editor_can_open_edit_page_in_editable_library(db, editor_client, editor_user):
    libraries.set_membership(db, 1, editor_user["id"], "editor")
    item_id = _item(db, 1, "Editable item")
    db.commit()
    response = editor_client.get(f"/item/{item_id}/edit")
    assert response.status_code == 200
    assert "Editable item" in response.text


def test_editor_cannot_open_or_update_hidden_library_item(db, editor_client, editor_user):
    libraries.set_membership(db, 1, editor_user["id"], "editor")
    private = libraries.create_library(db, "Private edits")
    hidden = _item(db, private["id"], "Hidden edit target")
    db.commit()

    page = editor_client.get(f"/item/{hidden}/edit", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/browse"

    update = editor_client.post(
        f"/api/items/{hidden}",
        data={"title": "Leaked update"},
        follow_redirects=False,
    )
    assert update.status_code == 404
    row = db.execute("SELECT title FROM items WHERE id = ?", (hidden,)).fetchone()
    assert row["title"] == "Hidden edit target"


def test_library_viewer_cannot_edit_visible_item(db, editor_client, editor_user):
    private = libraries.create_library(db, "Viewer-only edit")
    item_id = _item(db, private["id"], "Viewer-only item")
    libraries.set_membership(db, private["id"], editor_user["id"], "viewer")
    db.commit()

    response = editor_client.get(f"/item/{item_id}/edit", follow_redirects=False)
    assert response.status_code in (303, 403)


def test_hidden_tag_mutation_is_blocked_before_write(db, editor_client, editor_user):
    libraries.set_membership(db, 1, editor_user["id"], "editor")
    private = libraries.create_library(db, "Private tags")
    hidden = _item(db, private["id"], "Hidden tag target")
    db.commit()

    response = editor_client.post(
        f"/api/items/{hidden}/tags", data={"name": "Should Not Exist"}
    )
    assert response.status_code == 404
    assert db.execute(
        "SELECT 1 FROM tags WHERE name = 'Should Not Exist'"
    ).fetchone() is None


def test_hidden_cover_remove_is_blocked_before_write(db, editor_client, editor_user):
    libraries.set_membership(db, 1, editor_user["id"], "editor")
    private = libraries.create_library(db, "Private covers")
    hidden = _item(db, private["id"], "Hidden cover target")
    db.execute("UPDATE items SET cover_path = ? WHERE id = ?", (f"covers/{hidden}.jpg", hidden))
    db.commit()

    response = editor_client.post(f"/api/items/{hidden}/cover-remove")
    assert response.status_code == 404
    assert db.execute(
        "SELECT cover_path FROM items WHERE id = ?", (hidden,)
    ).fetchone()["cover_path"] == f"covers/{hidden}.jpg"


def test_reading_status_uses_viewer_membership_not_global_role(db, viewer_client, viewer_user):
    private = libraries.create_library(db, "Readable status")
    visible = _item(db, private["id"], "Readable status item")
    libraries.set_membership(db, private["id"], viewer_user["id"], "viewer")
    hidden_library = libraries.create_library(db, "Hidden status")
    hidden = _item(db, hidden_library["id"], "Hidden status item")
    db.commit()

    ok = viewer_client.post(
        f"/api/items/{visible}/reading-status", data={"status": "reading"}
    )
    assert ok.status_code == 200
    denied = viewer_client.post(
        f"/api/items/{hidden}/reading-status", data={"status": "reading"}
    )
    assert denied.status_code == 404
    assert db.execute(
        "SELECT reading_status FROM items WHERE id = ?", (hidden,)
    ).fetchone()["reading_status"] is None


def test_admin_retains_item_mutation_recovery_access(db, admin_client):
    private = libraries.create_library(db, "Admin edit recovery")
    item_id = _item(db, private["id"], "Admin editable")
    db.commit()
    response = admin_client.get(f"/item/{item_id}/edit")
    assert response.status_code == 200
''')
