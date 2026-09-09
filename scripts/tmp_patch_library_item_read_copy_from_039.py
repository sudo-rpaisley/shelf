from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


# The manual-add copy picker is an item metadata read surface too. Suggestions
# must not reveal private titles, and a guessed copy-template id must be
# indistinguishable from an unknown id.
replace_once(
    "app/routers/items.py",
    '''@router.get("/items/suggest")\nasync def suggest_items(q: str = "", _=Depends(require_role("editor"))):\n''',
    '''@router.get("/items/suggest")\nasync def suggest_items(\n    request: Request, q: str = "", _=Depends(require_role("editor"))\n):\n''',
)
replace_once(
    "app/routers/items.py",
    '''    if not q:\n        return JSONResponse([])\n    with get_db() as db:\n        rows = db.execute(\n            "SELECT id, title, authors FROM items WHERE title LIKE ? "\n            "ORDER BY title COLLATE NOCASE LIMIT 10",\n            (f"{q}%",),\n        ).fetchall()\n''',
    '''    if not q:\n        return JSONResponse([])\n    actor = dict(request.state.user)\n    with get_db() as db:\n        access_sql, access_params = libraries.item_access_condition(\n            actor, item_alias="i", minimum_role="editor"\n        )\n        rows = db.execute(\n            "SELECT i.id, i.title, i.authors FROM items i WHERE i.title LIKE ? "\n            f"AND {access_sql} ORDER BY i.title COLLATE NOCASE LIMIT 10",\n            [f"{q}%", *access_params],\n        ).fetchall()\n''',
)
replace_once(
    "app/routers/items.py",
    '''@router.get("/items/{item_id}/copy-template")\nasync def copy_template(item_id: int, _=Depends(require_role("editor"))):\n''',
    '''@router.get("/items/{item_id}/copy-template")\nasync def copy_template(\n    request: Request, item_id: int, _=Depends(require_role("editor"))\n):\n''',
)
replace_once(
    "app/routers/items.py",
    '''    with get_db() as db:\n        row = db.execute(\n            """SELECT authors, publisher, publish_year, media_type, platform,\n               series_name, location_id FROM items WHERE id = ?""",\n            (item_id,),\n        ).fetchone()\n''',
    '''    actor = dict(request.state.user)\n    with get_db() as db:\n        access_sql, access_params = libraries.item_access_condition(\n            actor, item_alias="i", minimum_role="editor"\n        )\n        row = db.execute(\n            """SELECT i.authors, i.publisher, i.publish_year, i.media_type,\n               i.platform, i.series_name, i.location_id FROM items i\n               WHERE i.id = ? AND """ + access_sql,\n            [item_id, *access_params],\n        ).fetchone()\n''',
)

p = Path("tests/test_library_item_reads.py")
s = p.read_text().rstrip()
s += r'''


def test_copy_from_suggestions_hide_items_outside_editable_libraries(
    db, editor_client, editor_user
):
    libraries.set_membership(db, 1, editor_user["id"], "editor")
    visible = _item_in_library(db, 1, title="Copy Source Visible", authors="Visible")
    private = libraries.create_library(db, "Private copy source")
    hidden = _item_in_library(db, private["id"], title="Copy Source Hidden", authors="Hidden")
    db.commit()

    response = editor_client.get("/api/items/suggest", params={"q": "Copy Source"})
    assert response.status_code == 200
    payload = response.json()
    assert any(row["id"] == visible for row in payload)
    assert all(row["id"] != hidden for row in payload)
    assert all(row["title"] != "Copy Source Hidden" for row in payload)


def test_copy_template_hides_private_item_metadata(db, editor_client, editor_user):
    libraries.set_membership(db, 1, editor_user["id"], "editor")
    private = libraries.create_library(db, "Private copy template")
    hidden = _item_in_library(
        db,
        private["id"],
        title="Private Template",
        authors="Secret Author",
        publisher="Secret Publisher",
    )
    db.commit()

    response = editor_client.get(f"/api/items/{hidden}/copy-template")
    assert response.status_code == 404
    assert response.json() == {"error": "Not found"}
    assert "Secret Author" not in response.text
    assert "Secret Publisher" not in response.text
'''
p.write_text(s + "\n")
