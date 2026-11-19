"""Authored panel-removal regressions; runtime qualification is still pending."""

import pytest

from tests.conftest import _insert_item
from tests.test_connected_layout import _Markup


def _seed_recent(db):
    ids = [_insert_item(db, title=f"Panel removal {media_type}", media_type=media_type, isbn=None)
           for media_type in ("book", "cd")]
    db.executemany("UPDATE items SET created_at = datetime('now') WHERE id = ?", ((item_id,) for item_id in ids))
    db.commit()
    return ids


def _snapshot(db):
    return {table: tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in ("items", "item_copies", "locations", "user_item_state", "user_item_events")}


@pytest.mark.parametrize("fixture", ("admin_client", "editor_client", "viewer_client"))
@pytest.mark.parametrize("populated", (False, True))
def test_stats_omits_recent_list_and_inapplicable_size_control_but_preserves_charts(fixture, populated, request, db):
    client = request.getfixturevalue(fixture)
    ids = _seed_recent(db) if populated else []
    before = _snapshot(db)
    response = client.get("/stats")
    assert response.status_code == 200
    markup = _Markup(response.text)
    main = markup.one("main")
    assert not [node for node in markup.find("h2", within=main)
                if node.text.strip() == "Recently Added (Last 30 Days)"]
    assert not [node for node in markup.elements if "data-cover-size-control" in node.attrs]
    assert "data-cover-size-account" not in markup.one("html").attrs
    assert [node for node in markup.elements if "data-command-palette" in node.attrs]
    for item_id in ids:
        assert not markup.find("a", within=main, href=f"/item/{item_id}?from=stats")
    assert {row["id"] for row in response.context["recent"]} == set(ids)
    assert response.context["total"] == len(ids)
    for testid in ("chart-read", "chart-growth", "chart-authors", "stats-finished-year", "stats-wrapup-link"):
        assert markup.testid(testid)
    for heading in ("By Media Type", "By Location"):
        assert [node for node in markup.find("h2", within=main) if node.text.strip() == heading]
    assert _snapshot(db) == before


@pytest.mark.parametrize("fixture", ("admin_client", "editor_client", "viewer_client"))
@pytest.mark.parametrize("populated", (False, True))
def test_home_omits_useful_places_but_keeps_shelves_summary_and_authorized_navigation(fixture, populated, request, db):
    client = request.getfixturevalue(fixture)
    ids = _seed_recent(db) if populated else []
    before = _snapshot(db)
    response = client.get("/")
    assert response.status_code == 200
    markup = _Markup(response.text)
    main = markup.one("main")
    assert not [node for node in markup.find("h2", within=main) if node.text.strip() == "Useful places"]
    assert not markup.find("a", within=main, href="/sources")
    assert not markup.find("a", within=main, href="/needs-attention")
    assert markup.testid("home-header") and markup.testid("home-summary")
    assert markup.testid("home-personal-library") and markup.testid("home-recent-items")
    assert {int(node.attrs["data-item-id"]) for node in markup.find("article", within=markup.testid("home-recent-items"))} == set(ids)
    assert len([node for node in markup.elements if "data-cover-size-control" in node.attrs]) == 1
    navigation = markup.one("nav", **{"aria-label": "Primary navigation"})
    for key in ("browse", "series", "stats", "sources", "activity"):
        assert markup.find("a", within=navigation, **{"data-nav-tab": key})
    assert bool(markup.find("a", within=navigation, **{"data-nav-tab": "needs-attention"})) == (fixture != "viewer_client")
    assert _snapshot(db) == before
