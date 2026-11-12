"""Cross-feature connected-library wiring, privacy and merge regressions."""

from pathlib import Path

import pytest

from app import nav
from app.services import item_merge, item_write
from tests.conftest import _insert_item
from tests.test_features_gate import off


def test_activity_navigation_groups_preserve_visibility_and_account_destinations():
    tabs = nav.visible_tabs({"id": 1, "role": "admin"})
    grouped = nav.grouped_tabs(tabs)
    assert [group["key"] for group in grouped] == ["library", "add", "explore"]
    actual = [tab["key"] for group in grouped for tab in group["tabs"]]
    expected = [tab["key"] for tab in tabs if not tab["menu"]]
    assert set(actual) == set(expected)
    assert len(actual) == len(set(actual))
    assert "activity" not in actual and "sources" not in actual
    assert "library" in actual and "scan" in actual
    off("connected_library")
    assert "library" not in [tab["key"] for group in nav.grouped_tabs(nav.visible_tabs({"role": "admin"})) for tab in group["tabs"]]


@pytest.mark.parametrize("url", ["/library", "/sources", "/activity", "/needs-attention"])
def test_connected_pages_are_registered_and_reachable(admin_client, url):
    response = admin_client.get(url)
    assert response.status_code == 200
    assert 'data-nav-tab="library"' in response.text
    assert 'data-testid="nav-activity-groups"' in response.text


@pytest.mark.parametrize("url", ["/library", "/sources", "/activity"])
def test_viewers_can_use_library_sources_and_their_own_activity(viewer_client, url):
    assert viewer_client.get(url).status_code == 200


def test_maintenance_is_not_exposed_to_viewers(viewer_client):
    response = viewer_client.get("/needs-attention", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/browse"
    assert 'data-nav-tab="needs-attention"' not in viewer_client.get("/library").text


@pytest.mark.parametrize("url", ["/library", "/sources", "/activity", "/needs-attention"])
def test_connected_pages_still_require_a_session(client, admin_user, url):
    response = client.get(url, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_disabling_connected_library_removes_item_and_home_controls(admin_client, db):
    item_id = _insert_item(db, isbn=None)
    db.commit()
    enabled = admin_client.get(f"/item/{item_id}")
    assert 'data-testid="available-here"' in enabled.text
    assert 'data-testid="personal-library"' in enabled.text
    off("connected_library")
    detail = admin_client.get(f"/item/{item_id}")
    assert detail.status_code == 200
    assert 'data-testid="available-here"' not in detail.text
    assert 'data-testid="personal-library"' not in detail.text
    for html in (detail.text, admin_client.get("/").text):
        for path in ("/library", "/sources", "/activity", "/needs-attention"):
            assert f'href="{path}"' not in html
    assert admin_client.get("/browse").status_code == 200


def test_connected_rendering_makes_no_remote_requests(admin_client, db, monkeypatch):
    import httpx

    def forbidden_network(*args, **kwargs):
        raise AssertionError("Rendering a connected-library page must not call remote services")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_network)
    item_id = _insert_item(db, title="Offline-readable catalogue", isbn=None)
    db.commit()
    for url in ("/library", "/sources", "/activity", "/needs-attention", f"/item/{item_id}"):
        assert admin_client.get(url).status_code == 200


def test_personalized_item_and_home_responses_are_not_cacheable(viewer_client, db):
    item_id = _insert_item(db, isbn=None)
    db.commit()
    for path in ("/", f"/item/{item_id}", "/activity", "/library", "/sources"):
        response = viewer_client.get(path)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("media_type", ["digital_video", "tv_series"])
def test_digital_screen_title_lookup_does_not_search_books(admin_client, media_type, monkeypatch):
    from app.services import openlibrary

    async def forbidden_search(*args, **kwargs):
        raise AssertionError("A digital-screen item must not be looked up as a book")

    monkeypatch.setattr(openlibrary, "search_books", forbidden_search)
    response = admin_client.get("/api/title-search", params={"q": "A title", "media_type": media_type})
    assert response.status_code == 200
    assert 'data-testid="unsupported-title-search"' in response.text


def test_merge_preserves_connected_records_and_personal_history(db, admin_user):
    user_id = admin_user["id"]
    keep = _insert_item(db, title="Kept edition", isbn=None)
    other = _insert_item(db, title="Duplicate edition", isbn=None)
    source = db.execute("INSERT INTO source_instances (provider, name, base_url) VALUES ('opds', 'Books', 'http://books.local/opds')").lastrowid
    for item_id, external_id in ((keep, "first"), (other, "second")):
        db.execute("INSERT INTO source_records (source_id, item_id, external_id, title, media_type) VALUES (?, ?, ?, 'Edition', 'ebook')", (source, item_id, external_id))
        db.execute("INSERT INTO user_item_events (user_id, item_id, event_type, snapshot_json) VALUES (?, ?, 'updated', '{}')", (user_id, item_id))
    db.execute("INSERT INTO user_item_state (user_id, item_id, status, progress, notes, favourite, updated_at) VALUES (?, ?, 'in_progress', 20, 'First note', 1, '2026-01-01')", (user_id, keep))
    db.execute("INSERT INTO user_item_state (user_id, item_id, status, progress, notes, on_list, updated_at) VALUES (?, ?, 'finished', 100, 'Second note', 1, '2026-02-01')", (user_id, other))
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    item_merge.reparent_children(db, keep, other)
    db.execute("DELETE FROM items WHERE id = ?", (other,))
    assert db.execute("SELECT COUNT(*) FROM source_records WHERE item_id = ?", (keep,)).fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM user_item_events WHERE item_id = ? AND event_type = 'updated'", (keep,)).fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM user_item_events WHERE item_id = ? AND event_type = 'merged'", (keep,)).fetchone()[0] == 1
    state = db.execute("SELECT * FROM user_item_state WHERE user_id = ? AND item_id = ?", (user_id, keep)).fetchone()
    assert state["status"] == "finished" and state["progress"] == 100
    assert "First note" in state["notes"] and "Second note" in state["notes"]
    assert state["favourite"] == 1 and state["on_list"] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_full_database_backup_carries_new_state_without_persistent_views(db, admin_user, tmp_path):
    item_id = _insert_item(db, isbn=None)
    db.execute("INSERT INTO user_item_state (user_id, item_id, notes) VALUES (?, ?, 'Private reading note')", (admin_user["id"], item_id))
    db.commit()
    destination = tmp_path / "connected-backup.db"
    db.execute("VACUUM INTO ?", (str(destination),))
    import sqlite3
    with sqlite3.connect(destination) as backup:
        assert backup.execute("SELECT notes FROM user_item_state").fetchone()[0] == "Private reading note"
        assert backup.execute("SELECT name FROM sqlite_master WHERE type IN ('view', 'trigger')").fetchall() == []


def test_private_connected_pages_are_not_service_worker_precache_entries():
    worker = (Path(__file__).parents[1] / "static" / "sw.js").read_text()
    for path in ("/library", "/activity", "/sources", "/needs-attention"):
        assert f"'{path}'" not in worker
        assert f'"{path}"' not in worker
