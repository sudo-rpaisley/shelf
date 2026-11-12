"""Library HTTP/UI boundaries, saved-view account isolation and native forms."""

import html
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from app import features
from app.auth import create_token
from app.services import library_catalog
from tests.conftest import _create_user
from tests.test_library_catalog import _copy, _item, _link, _record, _source


def _sign_in(client, user):
    client.cookies.set("access_token", create_token(user["id"], user["username"], user["role"], user["display_name"]))


def _saved_id(db, user_id):
    return db.execute("SELECT id FROM library_saved_views WHERE user_id = ?", (user_id,)).fetchone()[0]


@pytest.mark.parametrize("client_fixture", ["viewer_client", "editor_client", "admin_client"])
def test_every_authenticated_role_can_read_library(request, client_fixture, db):
    client = request.getfixturevalue(client_fixture)
    item = _item(db, "Readable to viewers 星")
    db.commit()
    page = client.get("/library")
    assert page.status_code == 200
    assert 'data-testid="library-page"' in page.text
    assert "Readable to viewers 星" in page.text
    response = client.get("/api/library/items")
    assert response.status_code == 200
    assert response.json()["groups"][0]["matching_ids"] == [item]
    assert response.headers["cache-control"] == "no-store"


def test_get_form_presets_and_connected_links_are_reachable(viewer_client):
    page = viewer_client.get("/library")
    assert '<form method="get" action="/library"' in page.text
    for key in library_catalog.FILTER_KEYS:
        assert f'name="{key}"' in page.text
    for label in ("All", "Available digitally", "On my shelves", "Wishlist"):
        assert label in page.text
    assert 'href="/sources"' in page.text
    assert 'href="/activity"' in page.text
    assert 'href="/needs-attention"' not in page.text


def test_editor_has_needs_attention_link(editor_client):
    assert 'href="/needs-attention"' in editor_client.get("/library").text


def test_fragment_displays_full_group_and_matches_without_bulk_selection(viewer_client, db):
    paper = _item(db, "Paper title", media_type="book")
    digital = _item(db, "Digital matching title", media_type="ebook")
    _link(db, paper, digital)
    _record(db, _source(db), digital)
    _copy(db, paper)
    db.commit()
    response = viewer_client.get("/api/library/search", params={"availability": "digital"})
    assert response.status_code == 200
    assert "<!DOCTYPE" not in response.text
    assert "1 matching item in 1 group" in response.text
    assert f'data-member-ids="{paper},{digital}"' in response.text
    assert f'data-matching-ids="{digital}"' in response.text
    assert f'data-library-member="{paper}" data-matches="false"' in response.text
    assert f'data-library-member="{digital}" data-matches="true"' in response.text
    assert "<details" in response.text and "<summary" in response.text
    assert "1 physical copy" in response.text
    assert 'type="checkbox"' not in response.text
    assert "bulk" not in response.text.lower()
    assert "<script" not in response.text


def test_items_view_does_not_display_nonmatching_siblings(viewer_client, db):
    paper, digital = _item(db, "Paper"), _item(db, "Digital", media_type="ebook")
    _link(db, paper, digital)
    db.commit()
    result = viewer_client.get("/api/library/items", params={"view": "items", "media_type": "ebook"}).json()
    assert result["groups"] == []
    assert [item["id"] for item in result["items"]] == [digital]
    assert (result["matching_count"], result["group_count"]) == (1, 1)


def test_html_links_preserve_filters_and_paginate_distinct_groups(viewer_client, db):
    for n in range(4):
        paper = _item(db, f"Étoile {n}", series_name="Saga #1/2")
        ebook = _item(db, f"Étoile {n}", series_name="Saga #1/2", media_type="ebook")
        _link(db, paper, ebook)
    db.commit()
    params = {"q": "ÉTOILE", "series": "Saga #1/2", "family": "book", "page_size": "2"}
    first = viewer_client.get("/library", params=params)
    next_url = html.unescape(re.search(r'<a href="([^"]+)" rel="next"', first.text).group(1))
    query = parse_qs(urlsplit(next_url).query)
    assert query["q"] == ["ÉTOILE"]
    assert query["series"] == ["saga #1/2"]
    assert query["family"] == ["book"] and query["page_size"] == ["2"]
    assert query["page"] == ["2"]
    second = viewer_client.get(next_url)
    first_ids = set(re.findall(r'data-library-group="(\d+)"', first.text))
    second_ids = set(re.findall(r'data-library-group="(\d+)"', second.text))
    assert len(first_ids) == len(second_ids) == 2
    assert first_ids.isdisjoint(second_ids)
    assert "8 matching items in 4 groups" in second.text


def test_local_series_drilldown_roundtrips_hashes_fractions_and_unicode(viewer_client, db):
    later = _item(db, "Later", series_name="星 #1/2", series_position=2)
    earlier = _item(db, "Earlier", series_name="星 #1/2", series_position=0.5)
    _item(db, "Different series", series_name="星 1/2", series_position=1)
    db.commit()
    index = viewer_client.get("/library", params={"view": "series"})
    assert 'data-library-series="星 #1/2"' in index.text
    assert "series=%E6%98%9F+%231%2F2" in index.text
    detail = viewer_client.get("/api/library/items", params={"view": "series", "series": "星 #1/2"}).json()
    assert [g["representative"]["id"] for g in detail["groups"]] == [earlier, later]


def test_source_instance_and_availability_filters_are_not_provenance_filters(viewer_client, db):
    item = _item(db, "Source holding", source="manual")
    fake = _item(db, "Only provenance", source="opds")
    first, second = _source(db), _source(db)
    _record(db, first, item, state="unavailable")
    _record(db, second, item, state="available")
    db.commit()
    assert viewer_client.get("/api/library/items", params={"source": f"source:{first}", "availability": "available"}).json()["matching_count"] == 0
    good = viewer_client.get("/api/library/items", params={"source": f"source:{second}", "availability": "available"}).json()
    assert good["groups"][0]["matching_ids"] == [item]
    assert fake not in good["groups"][0]["member_ids"]
    page = viewer_client.get("/library").text
    assert f'value="source:{first}"' in page and f'value="source:{second}"' in page


def test_media_type_and_availability_must_match_same_group_member(viewer_client, db):
    paper, ebook = _item(db, "Paper"), _item(db, "Digital", media_type="ebook")
    _link(db, paper, ebook)
    _record(db, _source(db), ebook)
    db.commit()
    result = viewer_client.get("/api/library/items", params={"media_type": "book", "availability": "digital"}).json()
    assert (result["matching_count"], result["group_count"]) == (0, 0)


def test_cards_escape_target_text_declare_cover_shapes_and_expose_no_credentials(viewer_client, db):
    item = _item(db, '<script>alert("title")</script>', media_type="cd", cover_path="covers/cover.jpg")
    source = _source(db, name="Music source", credentials_enc="encrypted-private-marker",
                     last_error="secret-remote-exception-marker")
    _record(db, source, item)
    db.execute("UPDATE source_records SET metadata_json = ?", (json.dumps({"private": "raw-metadata-marker"}),))
    db.commit()
    page = viewer_client.get("/library")
    assert '<script>alert("title")</script>' not in page.text
    assert "&lt;script&gt;" in page.text
    assert 'data-cover-shape="square"' in page.text
    api = viewer_client.get("/api/library/items")
    for marker in ("encrypted-private-marker", "secret-remote-exception-marker", "raw-metadata-marker"):
        assert marker not in page.text and marker not in api.text


def test_viewer_can_save_only_allowlisted_scalar_filters(viewer_client, viewer_user, db):
    response = viewer_client.post("/api/library/views", data={
        "name": " My Étoile view ", "q": "星", "media_type": "ebook", "family": "book",
        "availability": "digital", "view": "items", "sort": "title", "page_size": "48",
        "user_id": "999", "sql": "DROP TABLE items", "next": "https://evil.test", "page": "90",
    }, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/library"
    row = db.execute("SELECT * FROM library_saved_views").fetchone()
    assert row["user_id"] == viewer_user["id"] and row["name"] == "My Étoile view"
    filters = json.loads(row["filters_json"])
    assert set(filters) == library_catalog.FILTER_KEYS
    assert filters["q"] == "星" and filters["page_size"] == "48"
    assert "sql" not in filters and "page" not in filters
    landing = viewer_client.get("/library")
    assert "Personal view saved." in landing.text
    assert "Personal view saved." not in viewer_client.get("/library").text


def test_same_name_updates_only_current_users_view_case_insensitively(viewer_client, viewer_user, db):
    other = _create_user("another", "password123", "Another", "viewer")
    viewer_client.post("/api/library/views", data={"name": "My books", "q": "first"})
    own_id = _saved_id(db, viewer_user["id"])
    _sign_in(viewer_client, other)
    viewer_client.post("/api/library/views", data={"name": "MY BOOKS", "q": "private-other-query"})
    _sign_in(viewer_client, viewer_user)
    viewer_client.post("/api/library/views", data={"name": "my BOOKS", "q": "updated"})
    assert _saved_id(db, viewer_user["id"]) == own_id
    rows = db.execute("SELECT user_id, filters_json FROM library_saved_views").fetchall()
    assert len(rows) == 2
    assert {r["user_id"]: json.loads(r["filters_json"])["q"] for r in rows} == {
        viewer_user["id"]: "updated", other["id"]: "private-other-query",
    }


def test_saved_view_load_list_and_delete_are_scoped_to_current_account(viewer_client, viewer_user, db):
    other = _create_user("another", "password123", "Another", "viewer")
    own_item = _item(db, "Own search result")
    db.commit()
    viewer_client.post("/api/library/views", data={"name": "My own view", "q": "Own"})
    own_id = _saved_id(db, viewer_user["id"])
    _sign_in(viewer_client, other)
    viewer_client.post("/api/library/views", data={"name": "Private other view", "q": "private-query"})
    other_id = _saved_id(db, other["id"])
    _sign_in(viewer_client, viewer_user)
    own = viewer_client.get("/api/library/items", params={"saved_view": own_id}).json()
    assert own["groups"][0]["matching_ids"] == [own_item]
    assert own["filters"]["q"] == "Own"
    foreign = viewer_client.get("/api/library/items", params={"saved_view": other_id}).json()
    assert foreign["saved_view"] is None and foreign["filters"]["q"] == ""
    page = viewer_client.get("/library", params={"saved_view": other_id})
    assert "Private other view" not in page.text and "private-query" not in page.text
    assert "My own view" in page.text
    refused = viewer_client.post(f"/api/library/views/{other_id}/delete", follow_redirects=False)
    assert refused.status_code == 303 and refused.headers["location"] == "/library"
    assert db.execute("SELECT 1 FROM library_saved_views WHERE id = ?", (other_id,)).fetchone()
    deleted = viewer_client.post(f"/api/library/views/{own_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303 and deleted.headers["location"] == "/library"
    assert db.execute("SELECT 1 FROM library_saved_views WHERE id = ?", (own_id,)).fetchone() is None


def test_explicit_saved_view_overrides_and_empty_values_survive_pagination(viewer_client, viewer_user, db):
    for n in range(4):
        _item(db, f"Book {n}")
    _item(db, "Saved ebook", media_type="ebook")
    db.commit()
    viewer_client.post("/api/library/views", data={"name": "Digital", "q": "Saved", "media_type": "ebook",
                                                  "availability": "digital", "page_size": "2"})
    view_id = _saved_id(db, viewer_user["id"])
    params = {"saved_view": view_id, "q": "", "media_type": "book", "availability": ""}
    page = viewer_client.get("/library", params=params)
    assert "4 matching items in 4 groups" in page.text
    next_url = html.unescape(re.search(r'<a href="([^"]+)" rel="next"', page.text).group(1))
    query = parse_qs(urlsplit(next_url).query, keep_blank_values=True)
    assert query["saved_view"] == [str(view_id)]
    assert query["q"] == [""] and query["availability"] == [""]
    assert query["media_type"] == ["book"]
    assert "4 matching items in 4 groups" in viewer_client.get(next_url).text


@pytest.mark.parametrize("name", ["", "   ", "x" * 81, "bad\x00name"])
def test_invalid_saved_view_names_are_rejected_without_writes(viewer_client, db, name):
    response = viewer_client.post("/api/library/views", data={"name": name}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/library"
    assert db.execute("SELECT COUNT(*) FROM library_saved_views").fetchone()[0] == 0


def test_invalid_filter_values_and_json_bodies_cannot_store_a_query(viewer_client, viewer_user, db):
    response = viewer_client.post("/api/library/views", data={
        "name": "Safe", "view": "sql", "media_type": "bogus", "source": "source:1 OR 1=1",
        "availability": "offline", "sort": "DROP TABLE items", "family": "remote", "page_size": "1000000",
    }, follow_redirects=False)
    assert response.headers["location"] == "/library"
    saved = library_catalog.load_saved_view(db, viewer_user["id"], _saved_id(db, viewer_user["id"]))
    assert saved["filters"] == library_catalog.DEFAULT_FILTERS
    response = viewer_client.post("/api/library/views", json={"name": "No JSON", "query": "SELECT *"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/library"
    assert db.execute("SELECT COUNT(*) FROM library_saved_views").fetchone()[0] == 1


def test_corrupt_saved_json_is_safely_treated_as_default_filters(viewer_client, viewer_user, db):
    view_id = db.execute("INSERT INTO library_saved_views (user_id, name, filters_json) VALUES (?, 'Old view', '{bad')",
                         (viewer_user["id"],)).lastrowid
    db.commit()
    response = viewer_client.get("/api/library/items", params={"saved_view": view_id})
    assert response.status_code == 200
    assert response.json()["filters"] == library_catalog.DEFAULT_FILTERS


def test_saved_view_limit_is_per_account_and_allows_same_name_update(viewer_client, viewer_user, db):
    other = _create_user("another", "password123", "Another", "viewer")
    db.execute("BEGIN IMMEDIATE")
    for n in range(library_catalog.MAX_SAVED_VIEWS):
        library_catalog.save_view(db, viewer_user["id"], f"View {n}", {})
    db.commit()
    refused = viewer_client.post("/api/library/views", data={"name": "Too many"}, follow_redirects=False)
    assert refused.status_code == 303
    assert db.execute("SELECT COUNT(*) FROM library_saved_views WHERE user_id = ?", (viewer_user["id"],)).fetchone()[0] == 50
    viewer_client.post("/api/library/views", data={"name": "VIEW 0", "q": "updated"})
    assert json.loads(db.execute("SELECT filters_json FROM library_saved_views WHERE user_id = ? AND name = 'View 0'", (viewer_user["id"],)).fetchone()[0])["q"] == "updated"
    _sign_in(viewer_client, other)
    assert viewer_client.post("/api/library/views", data={"name": "Allowed"}, follow_redirects=False).status_code == 303
    assert db.execute("SELECT COUNT(*) FROM library_saved_views WHERE user_id = ?", (other["id"],)).fetchone()[0] == 1


def test_native_saved_view_forms_accept_csrf_field_and_refuse_missing_token(viewer_client, viewer_user, db):
    token = viewer_client.cookies.get("csrf_token")
    viewer_client.headers.pop("X-CSRF-Token", None)
    missing = viewer_client.post("/api/library/views", data={"name": "Missing token"}, follow_redirects=False)
    assert missing.status_code == 403
    accepted = viewer_client.post("/api/library/views", data={"name": "Native form", "_csrf": token}, follow_redirects=False)
    assert accepted.status_code == 303 and accepted.headers["location"] == "/library"
    view_id = _saved_id(db, viewer_user["id"])
    assert viewer_client.post(f"/api/library/views/{view_id}/delete", data={"_csrf": "wrong"}).status_code == 403
    assert db.execute("SELECT COUNT(*) FROM library_saved_views").fetchone()[0] == 1
    deleted = viewer_client.post(f"/api/library/views/{view_id}/delete", data={"_csrf": token}, follow_redirects=False)
    assert deleted.status_code == 303 and deleted.headers["location"] == "/library"


def test_connected_library_feature_gates_all_reads_and_writes(viewer_client, db):
    features.set_feature_enabled(db, "connected_library", False)
    db.commit()
    page = viewer_client.get("/library")
    assert page.status_code == 200 and "Connected library is turned off" in page.text
    assert 'data-testid="library-page"' not in page.text
    for path in ("/api/library/items", "/api/library/search"):
        response = viewer_client.get(path)
        assert response.status_code == 403 and response.json()["feature"] == "connected_library"
    for path in ("/api/library/views", "/api/library/views/1/delete"):
        assert viewer_client.post(path, data={"name": "Not saved"}).status_code == 403
    assert db.execute("SELECT COUNT(*) FROM library_saved_views").fetchone()[0] == 0


def test_bearer_tokens_do_not_authenticate_the_unversioned_library(client, viewer_user, db):
    from app.services import api_tokens

    created = api_tokens.create(db, viewer_user["id"], "dashboard")
    db.commit()
    client.cookies.clear()
    for path in ("/library", "/api/library/items", "/api/library/search"):
        response = client.get(path, headers={"Authorization": f"Bearer {created['token']}"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"


def test_browse_remains_separate_and_uses_its_existing_provenance_filter(viewer_client, db):
    item = _item(db, "Still in Browse", source="komga")
    db.commit()
    assert "Still in Browse" in viewer_client.get("/browse").text
    assert "Still in Browse" in viewer_client.get("/api/search?source_filter=komga").text
    assert viewer_client.get("/api/library/items?source=komga").json()["matching_count"] == 0
    assert viewer_client.get("/api/library/items").json()["groups"][0]["matching_ids"] == [item]
