from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1))


# This adapter is intended to run on top of the library Browse-scope PR, where
# pages.py already imports `libraries` and libraries.item_access_condition is
# available. Keep reads separate from write/edit authority for a focused PR.
pages = Path("app/routers/pages.py")
s = pages.read_text()
if "from app.services import item_copies, libraries" not in s:
    raise SystemExit("library Browse-scope prerequisite is missing")

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

Path("tests/test_library_item_reads.py").write_text(r'''"""Direct item-read security regressions for first-class libraries."""

from app.services import libraries
from tests.conftest import _insert_item


def _item_in_library(db, library_id: int, *, title: str, isbn: str, **fields) -> int:
    item_id = _insert_item(db, title=title, isbn=isbn, **fields)
    libraries.assign_item(db, item_id, library_id)
    return item_id


def test_guessed_hidden_item_redirects_without_rendering_title(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    private = libraries.create_library(db, "Private direct reads")
    hidden = _item_in_library(
        db, private["id"], title="Hidden direct item", isbn="9780000011001"
    )
    db.commit()

    response = viewer_client.get(f"/item/{hidden}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/browse"
    assert "Hidden direct item" not in response.text


def test_visible_main_library_item_still_opens(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    item_id = _item_in_library(
        db, 1, title="Visible direct item", isbn="9780000011002"
    )
    db.commit()
    response = viewer_client.get(f"/item/{item_id}")
    assert response.status_code == 200
    assert "Visible direct item" in response.text


def test_hidden_format_link_is_not_rendered_on_visible_item(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    visible = _item_in_library(
        db, 1, title="Visible physical", isbn="9780000011010"
    )
    private = libraries.create_library(db, "Hidden formats")
    hidden = _item_in_library(
        db, private["id"], title="Hidden digital", isbn="9780000011011"
    )
    db.execute(
        "INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, 'format')",
        (visible, hidden),
    )
    db.commit()

    html = viewer_client.get(f"/item/{visible}").text
    assert "Visible physical" in html
    assert "Hidden digital" not in html


def test_hidden_only_tags_are_not_item_edit_options(db, viewer_client, viewer_user):
    libraries.set_membership(db, 1, viewer_user["id"], "viewer")
    visible = _item_in_library(
        db, 1, title="Visible tagged item", isbn="9780000011028"
    )
    private = libraries.create_library(db, "Hidden tags")
    hidden = _item_in_library(
        db, private["id"], title="Hidden tagged item", isbn="9780000011029"
    )
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
        db,
        1,
        title="Saga One",
        isbn="9780000011035",
        series_name="Scoped Saga",
        series_position=1,
        owned=1,
    )
    private = libraries.create_library(db, "Hidden series")
    _item_in_library(
        db,
        private["id"],
        title="Saga Two Hidden",
        isbn="9780000011036",
        series_name="Scoped Saga",
        series_position=2,
        owned=1,
    )
    db.commit()

    html = viewer_client.get(f"/item/{visible}").text
    assert "Saga Two Hidden" not in html
    assert "You own 2" not in html
''')
