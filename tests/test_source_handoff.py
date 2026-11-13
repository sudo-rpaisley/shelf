"""Cached eligibility, owner preferences and re-resolved browser handoff."""

import pytest

from app.services import item_write, media_groups, source_handoff
from tests.conftest import _create_user
from tests.test_availability import _item, _record, _source


def test_preference_is_account_action_and_live_format_component_scoped(db, viewer_user):
    uid = viewer_user["id"]
    other = _create_user("other", "password123", "Other", "viewer")
    printed, ebook, audio, film = [_item(db, name, kind) for name, kind in (
        ("Print", "book"), ("Ebook", "ebook"), ("Audio", "audiobook"), ("Adaptation", "digital_video"),
    )]
    media_groups.link_items(db, printed, ebook, link_type="format")
    media_groups.link_items(db, ebook, audio, link_type="format")
    media_groups.link_items(db, printed, film, link_type="adaptation")
    a, b = _source(db, name="A"), _source(db, name="B")
    for source_id in (a, b):
        _record(db, source_id, ebook, media_type="ebook")
    _record(db, b, audio, media_type="audiobook")
    _record(db, b, film, media_type="digital_video")
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    source_handoff.set_preference(db, uid, printed, "Read", f"source:{b}")
    db.commit()

    assert source_handoff.resolve(db, uid, printed, "Read")["source_name"] == "B"
    assert source_handoff.resolve(db, other["id"], printed, "Read")["source_name"] == "A"
    assert source_handoff.resolve(db, uid, printed, "Listen")["item_id"] == audio
    assert source_handoff.resolve(db, uid, printed, "Watch") is None
    with pytest.raises(ValueError):
        source_handoff.set_preference(db, uid, printed, "Watch", f"source:{b}")
    assert source_handoff.resolve(db, uid, printed, "download") is None


@pytest.mark.parametrize("change", ["disabled", "failed", "missing", "unsafe", "trashed"])
def test_dead_preference_falls_back_but_is_not_forgotten(db, viewer_user, change):
    item = _item(db, media_type="ebook")
    a, b = _source(db, name="A"), _source(db, name="B")
    _record(db, a, item, open_url="https://books.example/read/a")
    preferred = _record(db, b, item, open_url="https://books.example/read/b")
    source_handoff.set_preference(db, viewer_user["id"], item, "Read", f"source:{b}")
    if change == "disabled":
        db.execute("UPDATE source_instances SET enabled = 0 WHERE id = ?", (b,))
    elif change == "failed":
        db.execute("UPDATE source_instances SET last_status = 'error' WHERE id = ?", (b,))
    elif change == "missing":
        db.execute("UPDATE source_records SET availability = 'unavailable' WHERE id = ?", (preferred,))
    elif change == "unsafe":
        db.execute("UPDATE source_records SET open_url = 'https://books.example/read?opaque=secret' WHERE id = ?", (preferred,))
    else:
        item_write.trash_item(db, item)
        assert source_handoff.resolve(db, viewer_user["id"], item, "Read") is None
        return
    entry = source_handoff.context(db, viewer_user["id"], item)["source_handoff"]["actions"][0]
    assert entry["fallback"] and entry["preferred"] == f"source:{b}"
    assert entry["selected"]["url"] == "https://books.example/read/a"
    before = db.total_changes
    source_handoff.set_preference(db, viewer_user["id"], item, "Read", f"source:{b}")
    assert db.total_changes == before  # Saving the unavailable option retains it.
    db.execute("UPDATE source_instances SET enabled = 0 WHERE id = ?", (a,))
    assert source_handoff.resolve(db, viewer_user["id"], item, "Read") is None


def test_unknown_and_auth_bearing_urls_never_become_handoff_actions(db, viewer_user):
    item = _item(db)
    for index, url in enumerate(("https://x.example/read?token=bearer", "https://x.example/?unknown=bearer",
                                 "https://user:pass@x.example/read", "javascript:alert(1)")):
        _record(db, _source(db, name=str(index)), item, open_url=url)
    _record(db, _source(db, name="Unknown"), item, availability="unknown")
    result = source_handoff.context(db, viewer_user["id"], item)
    assert result["source_handoff"]["actions"] == []
    assert not any(row["eligible"] for row in result["rows"])


def test_preference_clear_merge_and_reader_do_not_write(db, viewer_user, monkeypatch):
    item, other = _item(db, "Kept"), _item(db, "Merged")
    a, b = _source(db, name="A"), _source(db, name="B")
    _record(db, a, item)
    _record(db, b, other, media_type="audiobook")
    source_handoff.set_preference(db, viewer_user["id"], item, "Read", f"source:{a}")
    source_handoff.set_preference(db, viewer_user["id"], other, "Listen", f"source:{b}")
    source_handoff.reparent(db, item, other)
    assert db.execute("SELECT COUNT(*) FROM user_source_preferences WHERE item_id = ?", (item,)).fetchone()[0] == 2
    source_handoff.set_preference(db, viewer_user["id"], item, "Read", "")
    assert db.execute("SELECT action FROM user_source_preferences").fetchone()[0] == "Listen"
    db.commit()
    before = db.total_changes
    db.execute("PRAGMA query_only = ON")
    try:
        assert source_handoff.context(db, viewer_user["id"], item)
        assert source_handoff.resolve(db, viewer_user["id"], item, "Read")
    finally:
        db.execute("PRAGMA query_only = OFF")
    assert db.total_changes == before


def test_explicit_row_source_rechecks_without_silently_using_fallback(db, viewer_user):
    item = _item(db, media_type="ebook")
    a, b = _source(db, name="A"), _source(db, name="B")
    _record(db, a, item, open_url="https://books.example/read/a")
    _record(db, b, item, open_url="https://books.example/read/b")
    assert source_handoff.resolve(db, viewer_user["id"], item, "Read", source_key=f"source:{b}")["source_name"] == "B"
    db.execute("UPDATE source_instances SET enabled = 0 WHERE id = ?", (b,))
    assert source_handoff.resolve(db, viewer_user["id"], item, "Read", source_key=f"source:{b}") is None
    assert source_handoff.resolve(db, viewer_user["id"], item, "Read")["source_name"] == "A"
    assert source_handoff.resolve(db, viewer_user["id"], item, "Read", source_key="https://evil.example") is None


@pytest.fixture
def f4_client(viewer_client, monkeypatch):
    """Only this worker's client installs unregistered routes; parent owns wiring."""
    from app.routers.source_progress import router

    app = viewer_client.app
    existing = {route.path for route in app.router.routes}
    additions = [route for route in router.routes if route.path not in existing]
    monkeypatch.setattr(app.router, "routes", [*app.router.routes, *additions])
    return viewer_client


def test_handoff_ui_has_owner_preference_csrf_login_and_cached_disclosure(f4_client, db):
    item = _item(db, media_type="ebook")
    _record(db, _source(db), item)
    db.commit()
    response = f4_client.get(f"/api/library/items/{item}/handoff")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "No live connection check" in response.text and "log in there" in response.text
    assert f'/api/library/items/{item}/open/Read' in response.text
    assert 'name="_csrf"' in response.text and 'name="source_key"' in response.text
    assert "My preferred source" in response.text and "My linked progress accounts" in response.text
    assert 'rel="noopener noreferrer"' in response.text


def test_redirect_rechecks_source_and_never_uses_supplied_url(f4_client, db, viewer_user):
    item = _item(db, media_type="ebook")
    source = _source(db)
    _record(db, source, item)
    db.commit()
    response = f4_client.get(f"/api/library/items/{item}/open/Read?url=https://evil.example", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "https://books.example/read/edition"
    db.execute("UPDATE source_instances SET enabled = 0 WHERE id = ?", (source,))
    db.commit()
    assert f4_client.get(f"/api/library/items/{item}/open/Read", follow_redirects=False).status_code == 404
    # Main's existing unconditional header currently overrides the route. Keep
    # this integration regression until the coordinator preserves stricter ones.
    assert response.headers["referrer-policy"] == "no-referrer"


def test_preference_routes_csrf_and_role_then_feature(f4_client, viewer_user, db):
    from app.features import set_feature_enabled

    item = _item(db, media_type="ebook")
    source = _source(db)
    _record(db, source, item)
    db.commit()
    url = f"/api/library/items/{item}/preferred-source"
    fields = {"action": "Read", "source_key": f"source:{source}", "user_id": "99999"}
    assert f4_client.post(url, data=fields, headers={"X-CSRF-Token": ""}).status_code == 403
    response = f4_client.post(url, data=fields, headers={"HX-Request": "true"})
    assert response.status_code == 200 and 'selected' in response.text
    row = db.execute("SELECT * FROM user_source_preferences").fetchone()
    assert row["user_id"] == viewer_user["id"]
    set_feature_enabled(db, "connected_library", False)
    db.commit()
    assert f4_client.post(url, data=fields).status_code == 403
    assert f4_client.get(f"/api/library/items/{item}/open/Read").status_code == 403
    assert f4_client.get(f"/api/library/items/{item}/handoff").status_code == 403


def test_all_f4_routes_declare_viewer_before_feature():
    from app.auth import ROLE_LEVELS
    from app.routers.source_progress import router

    for route in router.routes:
        dependencies = route.dependant.dependencies
        assert dependencies[0].call.__closure__[0].cell_contents == ROLE_LEVELS["viewer"]
        assert getattr(dependencies[1].call, "feature_key", None) == "connected_library"
