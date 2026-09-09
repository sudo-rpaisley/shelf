from pathlib import Path


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one marker in {path}, got {count}: {old[:80]!r}")
    p.write_text(text.replace(old, new, 1))


def append_once(path, marker, addition):
    p = Path(path)
    text = p.read_text()
    if marker in text:
        return
    p.write_text(text.rstrip() + "\n\n\n" + addition.strip() + "\n")


# Shared service projections for item-detail assignment and Browse filtering.
append_once(
    "app/services/collections.py",
    "def accessible_options(",
    r'''
def accessible_options(db, user: dict) -> list[dict]:
    """Collections the user may see, labelled with their library."""
    library_ids = _visible_library_ids(db, user)
    if not library_ids:
        return []
    marks = ",".join("?" for _ in library_ids)
    rows = db.execute(
        "SELECT c.id, c.library_id, c.name, l.name AS library_name "
        "FROM collections c JOIN libraries l ON l.id = c.library_id "
        f"WHERE c.library_id IN ({marks}) "
        "ORDER BY l.name COLLATE NOCASE, c.name COLLATE NOCASE",
        library_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def item_options(db, user: dict, item_id: int) -> tuple[list[dict], bool]:
    """Same-library Collections for one visible item plus edit capability."""
    if not libraries.has_item_role(db, user, item_id, "viewer"):
        return [], False
    library_id = libraries.item_library_id(db, item_id)
    if library_id is None:
        return [], False
    rows = db.execute(
        "SELECT c.id, c.library_id, c.name, l.name AS library_name, "
        "EXISTS(SELECT 1 FROM collection_items ci "
        "       WHERE ci.collection_id = c.id AND ci.item_id = ?) AS selected "
        "FROM collections c JOIN libraries l ON l.id = c.library_id "
        "WHERE c.library_id = ? ORDER BY c.name COLLATE NOCASE",
        (item_id, library_id),
    ).fetchall()
    can_edit = libraries.has_item_role(db, user, item_id, "editor")
    return [dict(row) for row in rows], can_edit
''',
)

# Membership endpoints deliberately take collection_id from the form/path; the
# service remains the ACL and same-library authority.
append_once(
    "app/routers/collections.py",
    '@router.post("/api/items/{item_id}/collections")',
    r'''
@router.post("/api/items/{item_id}/collections")
async def add_item_to_collection(
    request: Request,
    item_id: int,
    collection_id: int = Form(...),
    _=Depends(require_role("viewer")),
):
    try:
        with get_db() as db:
            collection_service.add_item(
                db, dict(request.state.user), collection_id, item_id
            )
    except (PermissionError, ValueError, LookupError) as exc:
        return _error(exc)
    return RedirectResponse(url=f"/item/{item_id}", status_code=303)


@router.delete("/api/items/{item_id}/collections/{collection_id}")
async def remove_item_from_collection(
    request: Request,
    item_id: int,
    collection_id: int,
    _=Depends(require_role("viewer")),
):
    try:
        with get_db() as db:
            collection_service.remove_item(
                db, dict(request.state.user), collection_id, item_id
            )
    except (PermissionError, ValueError, LookupError) as exc:
        return _error(exc)
    return HTMLResponse("")
''',
)

# Item detail gets a same-library assignment projection. This deliberately uses
# per-library edit rights, not the user's legacy global role.
replace_once(
    "app/routers/pages.py",
    "        reading_history = get_reading_history(db, item_id)\n",
    '''        from app.services import collections as collection_service\n        collection_options, can_edit_collections = collection_service.item_options(\n            db, user, item_id\n        )\n        item_collections = [c for c in collection_options if c["selected"]]\n        available_collections = [c for c in collection_options if not c["selected"]]\n\n        reading_history = get_reading_history(db, item_id)\n''',
)
replace_once(
    "app/routers/pages.py",
    '            "all_tags": all_tags,\n',
    '            "all_tags": all_tags,\n            "item_collections": item_collections,\n            "available_collections": available_collections,\n            "can_edit_collections": can_edit_collections,\n',
)

# Collection is a first-class registry filter, so HTMX include lists, chips,
# URL state and pagination all inherit it automatically.
replace_once(
    "app/browse_filters.py",
    "\ndef _owned(value):\n",
    r'''
def _collection(value):
    try:
        collection_id = int(value)
    except (TypeError, ValueError):
        return _NEVER
    if not (_SQLITE_INT_MIN <= collection_id <= _SQLITE_INT_MAX):
        return _NEVER
    return (
        "i.id IN (SELECT ci.item_id FROM collection_items ci "
        "WHERE ci.collection_id = ?)",
        [collection_id],
    )


def _owned(value):
''',
)
replace_once(
    "app/browse_filters.py",
    '    BrowseFilter("media_family_filter", prefix="Family", condition=_media_family),\n',
    '    BrowseFilter("media_family_filter", prefix="Family", condition=_media_family),\n    BrowseFilter("collection", prefix="Collection", condition=_collection),\n',
)

# Browse only offers collection names from libraries the user can see. The
# normal item access predicate is still applied after the collection condition,
# so hand-edited hidden IDs cannot expose inaccessible rows.
replace_once(
    "app/routers/personal_browse.py",
    "from app.services import libraries, user_state_browse\n",
    "from app.services import libraries, user_state_browse\nfrom app.services import collections as collection_service\n",
)
replace_once(
    "app/routers/personal_browse.py",
    "        series_names, all_tags, item_languages, lent_out_count = _scoped_filter_options(db, user)\n",
    "        series_names, all_tags, item_languages, lent_out_count = _scoped_filter_options(db, user)\n        browse_collections = collection_service.accessible_options(db, user)\n",
)
replace_once(
    "app/routers/personal_browse.py",
    '        "media_types": MEDIA_TYPES,\n        "series_names": series_names,\n',
    '        "media_types": MEDIA_TYPES,\n        "browse_collections": browse_collections,\n        "series_names": series_names,\n',
)

# Compact specialist filter control. Library name stays visible because names
# may legitimately repeat in different libraries.
replace_once(
    "app/templates/browse.html",
    '''                </select>\n\n                <label for="type-filter" class="sr-only">Media type</label>\n''',
    '''                </select>\n\n                <label for="collection-filter" class="sr-only">Collection</label>\n                <select id="collection-filter" name="collection" data-testid="collection-filter"\n                        hx-get="/api/search" hx-trigger="change" hx-target="#item-grid"\n                        hx-include="{{ filter_includes('collection') }}"\n                        class="bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">\n                    <option value="" {{ 'selected' if not f.collection else '' }}>All Collections</option>\n                    {% for collection in browse_collections %}\n                    <option value="{{ collection.id }}" {{ 'selected' if f.collection == collection.id|string else '' }}>{{ collection.name }} — {{ collection.library_name }}</option>\n                    {% endfor %}\n                </select>\n\n                <label for="type-filter" class="sr-only">Media type</label>\n''',
)

# Item detail membership card.
replace_once(
    "app/templates/item_detail.html",
    '''            <section class="bg-shelf-card rounded-2xl border border-shelf-border p-5" data-testid="item-record-card">\n''',
    '''            {% if item_collections or available_collections %}\n            <section class="bg-shelf-card rounded-2xl border border-shelf-border p-5" data-testid="item-collections-card">\n                <div class="flex items-center justify-between gap-3 mb-3">\n                    <h2 class="text-lg font-semibold">Collections</h2>\n                    <a href="/collections" class="text-xs text-shelf-accent2 hover:text-shelf-accent">View all</a>\n                </div>\n                {% if item_collections %}\n                <div class="space-y-2" data-testid="item-collection-memberships">\n                    {% for collection in item_collections %}\n                    <div id="item-collection-{{ collection.id }}" class="flex items-center justify-between gap-2 bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2">\n                        <a href="/collections/{{ collection.id }}" class="text-sm text-shelf-accent2 hover:text-shelf-accent truncate">{{ collection.name }}</a>\n                        {% if can_edit_collections %}\n                        <button type="button" hx-delete="/api/items/{{ item.id }}/collections/{{ collection.id }}" hx-target="#item-collection-{{ collection.id }}" hx-swap="outerHTML" hx-confirm="Remove this item from '{{ collection.name }}'?" class="text-xs text-shelf-muted hover:text-shelf-error" aria-label="Remove from {{ collection.name }}">Remove</button>\n                        {% endif %}\n                    </div>\n                    {% endfor %}\n                </div>\n                {% else %}\n                <p class="text-sm text-shelf-muted">Not in a collection yet.</p>\n                {% endif %}\n\n                {% if can_edit_collections and available_collections %}\n                <form action="/api/items/{{ item.id }}/collections" method="post" class="flex gap-2 mt-3" data-testid="add-to-collection-form">\n                    <select name="collection_id" required class="min-w-0 flex-1 bg-shelf-bg border border-shelf-border rounded-lg px-2 py-1.5 text-sm text-shelf-text">\n                        {% for collection in available_collections %}<option value="{{ collection.id }}">{{ collection.name }}</option>{% endfor %}\n                    </select>\n                    <button type="submit" class="px-3 py-1.5 bg-shelf-accent text-white rounded-lg text-xs">Add</button>\n                </form>\n                {% endif %}\n            </section>\n            {% endif %}\n\n            <section class="bg-shelf-card rounded-2xl border border-shelf-border p-5" data-testid="item-record-card">\n''',
)

# Merge integrity: membership belongs to the kept catalogue identity after a
# duplicate is merged, with duplicate memberships collapsed safely.
replace_once(
    "app/services/item_merge.py",
    "\ndef reparent_children(db, keep_id: int, other_id: int) -> None:\n",
    r'''
def _reparent_collections(db, keep_id: int, other_id: int) -> None:
    db.execute(
        "INSERT OR IGNORE INTO collection_items (collection_id, item_id, created_at) "
        "SELECT collection_id, ?, created_at FROM collection_items WHERE item_id = ?",
        (keep_id, other_id),
    )
    db.execute("DELETE FROM collection_items WHERE item_id = ?", (other_id,))


def reparent_children(db, keep_id: int, other_id: int) -> None:
''',
)
replace_once(
    "app/services/item_merge.py",
    "    _reparent_copies(db, keep_id, other_id)\n",
    "    _reparent_copies(db, keep_id, other_id)\n    _reparent_collections(db, keep_id, other_id)\n",
)

Path("tests/test_collections_assignment_037.py").write_text(r'''"""Collections assignment, Browse filtering and merge integrity on 0.37."""

from app.services import collections, item_merge, libraries
from tests.conftest import _insert_item


def _new_collection(db, library_id, name):
    return db.execute(
        "INSERT INTO collections (library_id, name) VALUES (?, ?)",
        (library_id, name),
    ).lastrowid


def _item(db, title, isbn, library_id=1):
    item_id = _insert_item(db, title=title, isbn=isbn, media_type="book")
    libraries.assign_item(db, item_id, library_id)
    return item_id


def test_item_options_are_same_library_only_and_mark_membership(admin_user, db):
    other_library = libraries.create_library(db, "Other Library")["id"]
    item_id = _item(db, "Main", "9780000081018")
    selected = _new_collection(db, 1, "Selected")
    _new_collection(db, 1, "Available")
    _new_collection(db, other_library, "Other library collection")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (selected, item_id),
    )
    options, can_edit = collections.item_options(db, admin_user, item_id)
    assert can_edit is True
    assert {row["name"] for row in options} == {"Selected", "Available"}
    assert [row["name"] for row in options if row["selected"]] == ["Selected"]


def test_editor_can_add_and_remove_from_item_detail(editor_client, editor_user, db):
    item_id = _item(db, "Assigned", "9780000081025")
    collection_id = _new_collection(db, 1, "Course List")
    db.commit()

    before = editor_client.get(f"/item/{item_id}")
    assert before.status_code == 200
    assert 'data-testid="add-to-collection-form"' in before.text
    assert "Course List" in before.text

    added = editor_client.post(
        f"/api/items/{item_id}/collections",
        data={"collection_id": collection_id},
        follow_redirects=False,
    )
    assert added.status_code == 303
    assert db.execute(
        "SELECT 1 FROM collection_items WHERE collection_id = ? AND item_id = ?",
        (collection_id, item_id),
    ).fetchone()
    detail = editor_client.get(f"/item/{item_id}").text
    assert f'href="/collections/{collection_id}"' in detail
    assert f'hx-delete="/api/items/{item_id}/collections/{collection_id}"' in detail

    removed = editor_client.delete(
        f"/api/items/{item_id}/collections/{collection_id}"
    )
    assert removed.status_code == 200
    assert db.execute(
        "SELECT 1 FROM collection_items WHERE collection_id = ? AND item_id = ?",
        (collection_id, item_id),
    ).fetchone() is None


def test_global_viewer_with_library_editor_membership_gets_assignment_ui(
    viewer_client, viewer_user, db
):
    item_id = _item(db, "Project Item", "9780000081032")
    _new_collection(db, 1, "Project")
    libraries.set_membership(db, 1, viewer_user["id"], "editor")
    db.commit()
    html = viewer_client.get(f"/item/{item_id}").text
    assert 'data-testid="add-to-collection-form"' in html


def test_library_viewer_cannot_change_collection_membership(viewer_client, db):
    item_id = _item(db, "Read Only", "9780000081049")
    collection_id = _new_collection(db, 1, "Read Only Collection")
    db.commit()
    response = viewer_client.post(
        f"/api/items/{item_id}/collections",
        data={"collection_id": collection_id},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_cross_library_assignment_is_rejected_even_for_admin(admin_client, db):
    other_library = libraries.create_library(db, "Other Library")["id"]
    item_id = _item(db, "Main", "9780000081056")
    collection_id = _new_collection(db, other_library, "Other")
    db.commit()
    response = admin_client.post(
        f"/api/items/{item_id}/collections",
        data={"collection_id": collection_id},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert db.execute(
        "SELECT 1 FROM collection_items WHERE collection_id = ? AND item_id = ?",
        (collection_id, item_id),
    ).fetchone() is None


def test_browse_collection_filter_and_htmx_keep_membership_scope(admin_client, db):
    picked = _item(db, "Picked Item", "9780000081063")
    other = _item(db, "Other Item", "9780000081070")
    collection_id = _new_collection(db, 1, "Shortlist")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (collection_id, picked),
    )
    db.commit()

    first = admin_client.get(f"/browse?collection={collection_id}").text
    assert "Picked Item" in first
    assert "Other Item" not in first
    assert f'<option value="{collection_id}" selected>Shortlist — Main Library</option>' in first

    fragment = admin_client.get(
        f"/api/search?collection={collection_id}&sort=title_asc"
    ).text
    assert "Picked Item" in fragment
    assert "Other Item" not in fragment


def test_inaccessible_collection_id_does_not_bypass_library_scope(
    viewer_client, viewer_user, db
):
    private_library = libraries.create_library(db, "Private Library")["id"]
    private_item = _item(db, "Private Pick", "9780000081087", private_library)
    collection_id = _new_collection(db, private_library, "Private Collection")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (collection_id, private_item),
    )
    db.commit()
    html = viewer_client.get(f"/browse?collection={collection_id}").text
    assert "Private Pick" not in html
    assert "Private Collection" not in html


def test_merge_reparents_collection_membership_and_collapses_duplicate(admin_user, db):
    keep_id = _item(db, "Keep", "9780000081094")
    other_id = _item(db, "Merge", "9780000081100")
    collection_id = _new_collection(db, 1, "Merged Picks")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (collection_id, keep_id),
    )
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (collection_id, other_id),
    )
    db.commit()

    item_merge.reparent_children(db, keep_id, other_id)
    rows = db.execute(
        "SELECT item_id FROM collection_items WHERE collection_id = ?",
        (collection_id,),
    ).fetchall()
    assert [row["item_id"] for row in rows] == [keep_id]
''')

# Clean the temporary builder from the feature commit it creates.
Path(".github/tmp_build_collections_assignment_037.py").unlink()
Path(".github/workflows/tmp-build-collections-assignment-037.yml").unlink()
