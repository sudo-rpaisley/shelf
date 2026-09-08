from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1))


# pages.py: direct item/detail/edit reads and secondary projections.
replace_once(
    "app/routers/pages.py",
    "from app.services.home_dashboard import dashboard_summary\n",
    "from app.services.home_dashboard import dashboard_summary\nfrom app.services import libraries\n",
)
replace_once(
    "app/routers/pages.py",
    '''    back = nav.back_target(from_)\n    with get_db() as db:\n        item = db.execute(\n            "SELECT i.*, l.name as location_name FROM items i "\n''',
    '''    back = nav.back_target(from_)\n    user = dict(request.state.user)\n    with get_db() as db:\n        if not libraries.has_item_role(db, user, item_id, "viewer"):\n            return RedirectResponse(url="/browse", status_code=303)\n        access_sql, access_params = libraries.item_access_condition(user, item_alias="i")\n        item = db.execute(\n            "SELECT i.*, l.name as location_name FROM items i "\n''',
)
replace_once(
    "app/routers/pages.py",
    '''        linked_items = db.execute(\n            "SELECT i.id, i.title, i.media_type, i.abs_id FROM item_links il "\n            "JOIN items i ON (i.id = CASE WHEN il.item_a_id = ? THEN il.item_b_id ELSE il.item_a_id END) "\n            "WHERE (il.item_a_id = ? OR il.item_b_id = ?) AND il.link_type = 'format'",\n            (item_id, item_id, item_id),\n        ).fetchall()\n''',
    '''        linked_items = db.execute(\n            "SELECT i.id, i.title, i.media_type, i.abs_id FROM item_links il "\n            "JOIN items i ON (i.id = CASE WHEN il.item_a_id = ? THEN il.item_b_id ELSE il.item_a_id END) "\n            "WHERE (il.item_a_id = ? OR il.item_b_id = ?) AND il.link_type = 'format' "\n            f"AND ({access_sql})",\n            [item_id, item_id, item_id] + access_params,\n        ).fetchall()\n''',
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
replace_once(
    "app/routers/pages.py",
    '''    back = nav.back_target(from_)\n    with get_db() as db:\n        item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()\n        locations = db.execute(\n''',
    '''    back = nav.back_target(from_)\n    with get_db() as db:\n        if not libraries.has_item_role(db, dict(request.state.user), item_id, "editor"):\n            return RedirectResponse(url="/browse", status_code=303)\n        item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()\n        locations = db.execute(\n''',
)

# Personal state is private, but only for catalogue rows the account may see.
replace_once(
    "app/routers/personal_state.py",
    "from app.services import user_state\n",
    "from app.services import libraries, user_state\n",
)
replace_once(
    "app/routers/personal_state.py",
    '''    user_id = int(request.state.user["id"])\n    with get_db() as db:\n        item = db.execute(\n''',
    '''    user = dict(request.state.user)\n    user_id = int(user["id"])\n    with get_db() as db:\n        if not libraries.has_item_role(db, user, item_id, "viewer"):\n            return HTMLResponse("Not found", status_code=404)\n        item = db.execute(\n''',
)
# The same anchor occurs in the POST after the GET replacement consumed the first copy.
replace_once(
    "app/routers/personal_state.py",
    '''    user_id = int(request.state.user["id"])\n    with get_db() as db:\n        item = db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()\n''',
    '''    user = dict(request.state.user)\n    user_id = int(user["id"])\n    with get_db() as db:\n        if not libraries.has_item_role(db, user, item_id, "viewer"):\n            return HTMLResponse("Not found", status_code=404)\n        item = db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()\n''',
)

# Related-media graph traversal must not cross a hidden bridge.
media = Path("app/services/media_groups.py")
text = media.read_text()
start = text.index("def related_ids(")
end = text.index("\n\ndef direct_links", start)
replacement = '''def related_ids(\n    db,\n    item_id: int,\n    *,\n    include_self: bool = False,\n    visibility_sql: str | None = None,\n    visibility_params: list | tuple = (),\n) -> list[int]:\n    """Return the transitive item-link component containing ``item_id``.\n\n    With a visibility predicate, inaccessible nodes are removed from the graph\n    itself. A hidden B in A↔B↔C therefore cannot act as an invisible bridge.\n    The predicate must reference the ``i`` alias and remain parameter-bound.\n    """\n    if visibility_sql:\n        rows = db.execute(\n            f"""WITH RECURSIVE\n            visible(id) AS (\n                SELECT i.id FROM items i WHERE {visibility_sql}\n            ),\n            connected(id) AS (\n                SELECT ? WHERE EXISTS (SELECT 1 FROM visible WHERE id = ?)\n                UNION\n                SELECT CASE\n                         WHEN il.item_a_id = connected.id THEN il.item_b_id\n                         ELSE il.item_a_id\n                       END\n                  FROM item_links il\n                  JOIN connected\n                    ON il.item_a_id = connected.id OR il.item_b_id = connected.id\n                  JOIN visible v\n                    ON v.id = CASE\n                                WHEN il.item_a_id = connected.id THEN il.item_b_id\n                                ELSE il.item_a_id\n                              END\n            )\n            SELECT id FROM connected ORDER BY id""",\n            [*visibility_params, item_id, item_id],\n        ).fetchall()\n    else:\n        if not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():\n            return []\n        rows = db.execute(\n            """WITH RECURSIVE connected(id) AS (\n                   SELECT ?\n                   UNION\n                   SELECT CASE\n                            WHEN il.item_a_id = connected.id THEN il.item_b_id\n                            ELSE il.item_a_id\n                          END\n                   FROM item_links il\n                   JOIN connected\n                     ON il.item_a_id = connected.id OR il.item_b_id = connected.id\n               )\n               SELECT id FROM connected ORDER BY id""",\n            (item_id,),\n        ).fetchall()\n    ids = [row["id"] for row in rows]\n    if not include_self:\n        ids = [value for value in ids if value != item_id]\n    return ids\n\n\ndef related_items(\n    db,\n    item_id: int,\n    *,\n    include_self: bool = False,\n    visibility_sql: str | None = None,\n    visibility_params: list | tuple = (),\n):\n    """Hydrate a related-media component in stable display order."""\n    ids = related_ids(\n        db,\n        item_id,\n        include_self=include_self,\n        visibility_sql=visibility_sql,\n        visibility_params=visibility_params,\n    )\n    if not ids:\n        return []\n    placeholders = ",".join("?" for _ in ids)\n    return db.execute(\n        f"SELECT * FROM items WHERE id IN ({placeholders}) "\n        "ORDER BY title COLLATE NOCASE, media_type, id",\n        tuple(ids),\n    ).fetchall()\n'''
media.write_text(text[:start] + replacement + text[end:])

# The lazy Related Media panel is a read surface and must share the same graph boundary.
replace_once(
    "app/routers/related_media.py",
    "from app.services import media_groups\n",
    "from app.services import libraries, media_groups\n",
)
replace_once(
    "app/routers/related_media.py",
    '''def _panel_context(db, item_id: int) -> dict | None:\n    item = db.execute(\n        "SELECT id, title, media_type FROM items WHERE id = ?", (item_id,)\n    ).fetchone()\n    if not item:\n        return None\n\n    direct = {row["item_id"]: row for row in media_groups.direct_links(db, item_id)}\n    related = []\n    for row in media_groups.related_items(db, item_id):\n''',
    '''def _panel_context(db, item_id: int, user: dict) -> dict | None:\n    if not libraries.has_item_role(db, user, item_id, "viewer"):\n        return None\n    item = db.execute(\n        "SELECT id, title, media_type FROM items WHERE id = ?", (item_id,)\n    ).fetchone()\n    if not item:\n        return None\n\n    visibility_sql, visibility_params = libraries.item_access_condition(user, item_alias="i")\n    direct = {row["item_id"]: row for row in media_groups.direct_links(db, item_id)}\n    related = []\n    for row in media_groups.related_items(\n        db,\n        item_id,\n        visibility_sql=visibility_sql,\n        visibility_params=visibility_params,\n    ):\n''',
)
replace_once(
    "app/routers/related_media.py",
    '''def _render_panel(request: Request, item_id: int):\n    with get_db() as db:\n        context = _panel_context(db, item_id)\n''',
    '''def _render_panel(request: Request, item_id: int):\n    with get_db() as db:\n        context = _panel_context(db, item_id, dict(request.state.user))\n''',
)

# Existing personal-state route tests inserted raw unmapped rows. Under the ACL,
# explicitly model the upgraded Main Library instead of weakening production access.
replace_once(
    "tests/test_personal_state_routes_037.py",
    "from app.services import user_state\n",
    "from app.services import libraries, user_state\n",
)
replace_once(
    "tests/test_personal_state_routes_037.py",
    '''    item_id = db.execute(\n        "INSERT INTO items (title, media_type, source) VALUES (?, ?, 'test')",\n        (title, media_type),\n    ).lastrowid\n    # Client requests use their own SQLite connection. Commit fixture writes\n''',
    '''    item_id = db.execute(\n        "INSERT INTO items (title, media_type, source) VALUES (?, ?, 'test')",\n        (title, media_type),\n    ).lastrowid\n    libraries.assign_item(db, item_id, libraries.DEFAULT_LIBRARY_ID)\n    # Client requests use their own SQLite connection. Commit fixture writes\n''',
)

Path("tests/test_library_item_access_037.py").write_text(r'''"""Direct item-read security regressions for first-class libraries."""

from app.services import libraries, media_groups, user_state
from tests.conftest import _insert_item


def _private_item(db, *, title: str, isbn: str, series_name=None, series_position=None):
    library = libraries.create_library(db, f"Private {title}")
    item_id = _insert_item(
        db,
        title=title,
        isbn=isbn,
        series_name=series_name,
        series_position=series_position,
        _library_id=library["id"],
    )
    return library, item_id


def test_guessed_hidden_item_and_personal_state_behave_as_missing(
    db, viewer_client, viewer_user
):
    _, hidden = _private_item(db, title="Hidden direct item", isbn="9780000011001")
    user_state.save_state(db, viewer_user["id"], hidden, personal_notes="must stay hidden")
    db.commit()

    detail = viewer_client.get(f"/item/{hidden}", follow_redirects=False)
    assert detail.status_code == 303
    assert detail.headers["location"] == "/browse"

    get_state = viewer_client.get(f"/api/items/{hidden}/personal-state")
    assert get_state.status_code == 404
    assert "must stay hidden" not in get_state.text

    post_state = viewer_client.post(
        f"/api/items/{hidden}/personal-state",
        data={"personal_notes": "write through guessed id"},
    )
    assert post_state.status_code == 404
    state = user_state.get_state(db, viewer_user["id"], hidden)
    assert state["personal_notes"] == "must stay hidden"


def test_visible_main_library_item_still_opens(db, viewer_client):
    item_id = _insert_item(db, title="Visible direct item", isbn="9780000011002")
    db.commit()
    response = viewer_client.get(f"/item/{item_id}")
    assert response.status_code == 200
    assert "Visible direct item" in response.text


def test_global_editor_without_library_membership_cannot_open_edit_page(
    db, editor_client
):
    _, hidden = _private_item(db, title="Not this editor's item", isbn="9780000011003")
    db.commit()
    response = editor_client.get(f"/item/{hidden}/edit", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/browse"


def test_hidden_format_link_is_not_rendered_on_visible_item(db, viewer_client):
    visible = _insert_item(db, title="Visible physical", isbn="9780000011010")
    _, hidden = _private_item(db, title="Hidden digital", isbn="9780000011011")
    media_groups.link_items(db, visible, hidden, link_type="format")
    db.commit()

    html = viewer_client.get(f"/item/{visible}").text
    assert "Visible physical" in html
    assert "Hidden digital" not in html


def test_hidden_related_item_cannot_bridge_two_visible_items(db, viewer_client):
    first = _insert_item(db, title="Visible Alpha", isbn="9780000011020")
    _, hidden = _private_item(db, title="Secret Bridge", isbn="9780000011021")
    third = _insert_item(db, title="Visible Omega", isbn="9780000011022")
    media_groups.link_items(db, first, hidden, link_type="related")
    media_groups.link_items(db, hidden, third, link_type="related")
    db.commit()

    panel = viewer_client.get(f"/api/related-media/items/{first}/panel")
    assert panel.status_code == 200
    assert "Secret Bridge" not in panel.text
    assert "Visible Omega" not in panel.text


def test_item_series_progress_counts_only_accessible_siblings(db, viewer_client):
    first = _insert_item(
        db,
        title="Series One",
        isbn="9780000011030",
        series_name="Permission Saga",
        series_position=1,
    )
    _private_item(
        db,
        title="Secret Series Two",
        isbn="9780000011031",
        series_name="Permission Saga",
        series_position=2,
    )
    _insert_item(
        db,
        title="Series Three",
        isbn="9780000011032",
        series_name="Permission Saga",
        series_position=3,
    )
    db.commit()

    html = viewer_client.get(f"/item/{first}").text
    assert "Secret Series Two" not in html
    assert "you own 2 of 1–3" in html
    assert "missing #2" in html


def test_item_tag_suggestions_do_not_reveal_hidden_library_tags(db, editor_client):
    visible = _insert_item(db, title="Tagged visible", isbn="9780000011040")
    _, hidden = _private_item(db, title="Tagged hidden", isbn="9780000011041")
    public_tag = db.execute("INSERT INTO tags (name) VALUES ('Public Tag')").lastrowid
    secret_tag = db.execute("INSERT INTO tags (name) VALUES ('Classified Tag')").lastrowid
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (visible, public_tag))
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (hidden, secret_tag))
    db.commit()

    html = editor_client.get(f"/item/{visible}").text
    assert "Public Tag" in html
    assert "Classified Tag" not in html
''')
