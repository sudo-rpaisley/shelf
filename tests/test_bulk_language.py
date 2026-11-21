"""POST /api/items/bulk-update with a `language` update (issue #145).

PHASE4 D5: library editor authority, not a global Admin/Editor ceiling. Legacy
negative definition names remain; their actual grants are explicitly read-only.
"""
import pytest

from tests.conftest import _insert_item

URL = "/api/items/bulk-update"


def _seed(db, n=3, language="en"):
    ids = [
        _insert_item(db, title=f"Lang {i}", isbn=f"97891000{i:05d}", language=language)
        for i in range(n)
    ]
    db.commit()
    return ids


def _langs(db, ids):
    return [
        db.execute("SELECT language FROM items WHERE id = ?", (i,)).fetchone()["language"]
        for i in ids
    ]


def _post(client, ids, value):
    return client.post(URL, json={"item_ids": ids, "updates": {"language": value}})


def test_sets_language_on_all_selected(admin_client, db):
    ids = _seed(db)
    assert _post(admin_client, ids, "ja").json()["ok"] is True
    assert _langs(db, ids) == ["ja", "ja", "ja"]


@pytest.mark.parametrize("value", ["__clear__", "", "   "])
def test_clear_and_blank_store_null(admin_client, db, value):
    ids = _seed(db)
    assert _post(admin_client, ids, value).json()["ok"] is True
    assert _langs(db, ids) == [None, None, None]


@pytest.mark.parametrize("client_name", ["editor_client", "viewer_client"])
def test_non_admin_is_refused(request, db, client_name):
    from app.services import libraries

    client = request.getfixturevalue(client_name)
    user = request.getfixturevalue(client_name.replace("_client", "_user"))
    libraries.set_membership(db, 1, user["id"], "viewer")
    ids = _seed(db)
    before = list(db.iterdump())
    assert _post(client, ids, "ja").status_code == 403
    assert _langs(db, ids) == ["en", "en", "en"]
    assert list(db.iterdump()) == before


def test_browse_renders_language_control_for_admin(admin_client, db):
    _seed(db, n=1)
    assert 'data-testid="bulk-language"' in admin_client.get("/browse").text


def test_browse_omits_language_control_for_editor(editor_client, db):
    from app.services import libraries

    # D5: a global Editor with only a read-only library grant is not an editor
    # of this catalogue. The permitted inverse-role control is separate.
    user_id = db.execute("SELECT id FROM users WHERE username = 'editor'").fetchone()[0]
    libraries.set_membership(db, 1, user_id, "viewer")
    _seed(db, n=1)
    before = list(db.iterdump())
    assert 'data-testid="bulk-language"' not in editor_client.get("/browse").text
    assert list(db.iterdump()) == before
