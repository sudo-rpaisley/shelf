"""Upcoming stays usable with Series off; only Series-owned controls disappear.

All renders go through the real route and template globals. Flags are persisted
through the normal feature setter, not injected into a hand-built template.
"""

import json
import re
from html.parser import HTMLParser
from urllib.parse import quote

import pytest

from app.features import feature_enabled, set_feature_enabled
from tests.conftest import _assert_ownership_partition, _insert_item
from tests.test_features_gate import NAV, assert_disabled_page, assert_json_403, off

SERIES = "Gate & Radar"
TITLES = ["Gate Wishlist", "Gate Dismiss", "Gate Undated"]
HX = {"HX-Request": "true"}


class _Tags(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(dict(attrs))


def _tags(html, testid):
    return [attrs for attrs in _Tags(html).tags if attrs.get("data-testid") == testid]


def _body(response):
    assert response.status_code == 200
    match = re.search(r"<main\b[^>]*>(.*?)</main>", response.text, re.S)
    assert match, "the real page body did not render"
    return match.group(1)


def _titles(html):
    return re.findall(r'data-testid="upcoming-row-title">([^<]*)<', html)


def _watches(db):
    return [dict(row) for row in db.execute("SELECT * FROM release_watches ORDER BY id")]


def _releases(db):
    return [dict(row) for row in db.execute("SELECT * FROM upcoming_releases ORDER BY id")]


def _series_flag(db, enabled):
    set_feature_enabled(db, "series", enabled)
    db.commit()


@pytest.fixture
def announcements(db):
    _insert_item(db, title="Gate First", isbn=None, series_name=SERIES, owned=1)
    watch_id = db.execute(
        "INSERT INTO release_watches (series_name, checked_at, check_state) "
        "VALUES (?, '2026-11-01 12:00:00', 'ok')", (SERIES,),
    ).lastrowid
    ids = []
    for hc_id, title, position, date in (
        (91001, TITLES[0], 2, "2098-01-01"),
        (91002, TITLES[1], 3, "2099-01-01"),
        (91003, TITLES[2], 4, None),
    ):
        ids.append(db.execute(
            "INSERT INTO upcoming_releases (watch_id, hc_book_id, title, authors, "
            "series_position, release_date) VALUES (?, ?, ?, 'Gate Writer', ?, ?)",
            (watch_id, hc_id, title, position, date),
        ).lastrowid)
    db.commit()  # G48: requests must see populated rows, not a vacuous empty page.
    return ids


@pytest.mark.parametrize("client_fixture", ["editor_client", "viewer_client"])
@pytest.mark.parametrize("series_on", [False, True], ids=["series-off", "series-on"])
@pytest.mark.parametrize("populated", [False, True], ids=["empty", "announcements"])
def test_series_backlink_tracks_the_flag_even_on_an_empty_page(
    request, db, announcements, client_fixture, series_on, populated,
):
    client = request.getfixturevalue(client_fixture)
    _series_flag(db, series_on)
    if not populated:
        db.execute("DELETE FROM upcoming_releases")
        db.commit()
    before = (_watches(db), _releases(db))

    html = _body(client.get("/upcoming"))

    assert ('<a href="/series"' in html) is series_on
    assert "feature-disabled" not in html
    assert _titles(html) == (TITLES if populated else [])
    assert bool(_tags(html, "upcoming-empty")) is not populated
    assert "Powered by Hardcover" in html
    assert (_watches(db), _releases(db)) == before


@pytest.mark.parametrize("client_fixture,can_edit", [
    ("editor_client", True), ("admin_client", True), ("viewer_client", False),
])
@pytest.mark.parametrize("series_on", [False, True], ids=["series-off", "series-on"])
def test_actions_follow_their_own_features_and_role(
    request, db, announcements, client_fixture, can_edit, series_on,
):
    client = request.getfixturevalue(client_fixture)
    _series_flag(db, series_on)
    before = (_watches(db), _releases(db))

    html = _body(client.get("/upcoming"))

    assert _titles(html) == TITLES
    assert len(_tags(html, "upcoming-row-stop")) == (3 if can_edit and series_on else 0)
    for testid in ("upcoming-row-wishlist", "upcoming-row-dismiss"):
        assert len(_tags(html, testid)) == (3 if can_edit else 0)
    rows = _tags(html, "upcoming-row")
    assert len(rows) == 3
    assert all((row.get("x-data") == "upcomingRow") is can_edit for row in rows)
    if can_edit:
        assert rows[0]["data-title"] == TITLES[0]
        assert rows[0]["data-series-name"] == SERIES
        assert rows[0]["data-hardcover-book-id"] == "91001"
    assert (_watches(db), _releases(db)) == before


def test_series_off_keeps_wishlist_and_dismiss_working(editor_client, db, announcements):
    off("series")
    before = _watches(db)
    html = _body(editor_client.get("/upcoming"))
    row = _tags(html, "upcoming-row")[0]
    response = editor_client.post("/api/hardcover/add-to-shelf", json={
        "title": row["data-title"], "authors": row["data-authors"],
        "cover_url": row["data-cover-url"] or None,
        "hardcover_book_id": int(row["data-hardcover-book-id"]),
        "series_name": row["data-series-name"],
        "series_position": float(row["data-series-position"]),
    })
    assert response.status_code == 200 and response.json()["ok"] is True
    item = db.execute(
        "SELECT i.title, i.owned, i.series_name, i.series_position, l.slug "
        "FROM items_live i JOIN list_items li ON li.item_id = i.id "
        "JOIN lists l ON l.id = li.list_id WHERE i.id = ?",
        (response.json()["item_id"],),
    ).fetchone()
    assert tuple(item) == (TITLES[0], 0, SERIES, 2.0, "wishlist")
    _assert_ownership_partition(db)

    dismiss = _tags(html, "upcoming-row-dismiss")[1]
    response = editor_client.post(dismiss["hx-post"], headers=HX)
    assert response.status_code == 200 and response.content == b""
    assert db.execute("SELECT dismissed FROM upcoming_releases WHERE id = ?",
                      (announcements[1],)).fetchone()[0] == 1
    assert _titles(_body(editor_client.get("/upcoming"))) == [TITLES[2]]
    assert _watches(db) == before
    assert feature_enabled("series") is False
    assert feature_enabled("release_radar") and feature_enabled("hardcover")


def test_series_flag_round_trip_changes_only_the_controls(editor_client, db, announcements):
    before = (_watches(db), _releases(db))
    for enabled in (True, False, True):
        _series_flag(db, enabled)
        html = _body(editor_client.get("/upcoming"))
        assert ('<a href="/series"' in html) is enabled
        assert len(_tags(html, "upcoming-row-stop")) == (3 if enabled else 0)
        assert len(_tags(html, "upcoming-row-wishlist")) == 3
        assert len(_tags(html, "upcoming-row-dismiss")) == 3
        assert _titles(html) == TITLES
        assert (_watches(db), _releases(db)) == before


def test_series_off_does_not_bypass_the_watch_endpoint_gate(editor_client, db, announcements):
    off("series")
    before = (_watches(db), _releases(db))
    response = editor_client.post(f"/api/series/{quote(SERIES)}/watch", data={"watch": "0"})
    assert_json_403(response, "series", "Series")
    assert (_watches(db), _releases(db)) == before


def test_series_on_stop_watching_keeps_its_existing_contract(editor_client, db, announcements):
    html = _body(editor_client.get("/upcoming"))
    stop = _tags(html, "upcoming-row-stop")[0]
    assert stop["hx-post"] == "/api/series/Gate%20%26%20Radar/watch"
    assert stop["hx-swap"] == "none" and "hx-target" not in stop
    response = editor_client.post(stop["hx-post"], data=json.loads(stop["hx-vals"]), headers=HX)
    assert response.status_code == 200 and response.content == b""
    assert response.headers["HX-Refresh"] == "true"
    assert _watches(db) == [] and _releases(db) == []
    assert _tags(_body(editor_client.get("/upcoming")), "upcoming-empty")


@pytest.mark.parametrize("series_on", [False, True], ids=["series-off", "series-on"])
def test_viewer_cannot_mutate_any_upcoming_action(viewer_client, db, announcements, series_on):
    _series_flag(db, series_on)
    before = (_watches(db), _releases(db))
    assert viewer_client.post(f"/api/upcoming/{announcements[0]}/dismiss").status_code == 403
    assert viewer_client.post(
        f"/api/series/{quote(SERIES)}/watch", data={"watch": "0"},
    ).status_code == 403
    assert viewer_client.post("/api/hardcover/add-to-shelf", json={
        "title": TITLES[0], "hardcover_book_id": 91001,
    }).status_code == 403
    assert (_watches(db), _releases(db)) == before
    assert db.execute("SELECT COUNT(*) FROM items_live").fetchone()[0] == 1


@pytest.mark.parametrize("client_fixture", ["editor_client", "viewer_client"])
@pytest.mark.parametrize("series_on", [False, True], ids=["series-off", "series-on"])
@pytest.mark.parametrize("key,label", [("hardcover", "Hardcover"), ("release_radar", "Release radar")])
def test_required_page_features_still_refuse_without_touching_watches(
    request, db, announcements, client_fixture, series_on, key, label,
):
    client = request.getfixturevalue(client_fixture)
    _series_flag(db, series_on)
    off(key)
    before = (_watches(db), _releases(db))
    response = client.get("/upcoming", headers=NAV)
    assert_disabled_page(response, label, enable_form=False)
    for testid in ("upcoming-row", "upcoming-row-wishlist", "upcoming-row-dismiss", "upcoming-row-stop"):
        assert not _tags(response.text, testid)
    if client_fixture == "editor_client":
        assert_json_403(client.post(f"/api/upcoming/{announcements[0]}/dismiss"), key, label)
    assert (_watches(db), _releases(db)) == before
