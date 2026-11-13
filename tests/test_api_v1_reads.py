"""Stable v1 reads: actual route/schema/auth, SQL paging and visibility boundaries."""

import json
from contextlib import contextmanager
from datetime import date, timedelta
from urllib.parse import quote

import pytest

from app.api_v1_schemas import (
    ItemDetailResponse, ItemPage, ItemQuery, ListPage, ListQuery, ListSummary, Loan, LoanPage,
    LoanQuery, OpenAPIDocument, PagingQuery, SeriesDetailResponse, SeriesItemsQuery,
    SeriesPage, SeriesQuery, UpcomingPage, UpcomingQuery,
)
from app.database import get_db, set_setting
from app.services import api_v1 as reads
from app.services import api_tokens, item_write, lists, release_radar
from tests.conftest import _insert_borrower, _insert_item
from tests.test_features_gate import assert_json_403, off


PREFIX = "/api/v1"


def _item(db, title, **fields):
    return _insert_item(db, title=title, **{"isbn": None, **fields})


def _loan(db, item_id, borrower, **fields):
    fields = {"item_id": item_id, "borrower_id": borrower, "checked_out": "2026-01-01 00:00:00",
              "notes": "DO-NOT-EXPOSE-LOAN-NOTES", **fields}
    return db.execute(f"INSERT INTO checkouts ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                      list(fields.values())).lastrowid


def _source(db, item_id, *, url="https://books.example/read?id=42", name="OPDS", **fields):
    source_id = db.execute(
        "INSERT INTO source_instances (provider, name, base_url, browser_url, credentials_enc, last_error) "
        "VALUES ('opds', ?, 'http://books:8080/catalog', 'https://books.example/library', ?, ?)",
        (name, "DO-NOT-EXPOSE-CIPHERTEXT", "DO-NOT-EXPOSE-SOURCE-ERROR"),
    ).lastrowid
    fields = {"source_id": source_id, "item_id": item_id, "external_id": "DO-NOT-EXPOSE-EXTERNAL-ID",
              "title": "Remote title", "media_type": "ebook", "open_url": url,
              "metadata_json": '{"password":"DO-NOT-EXPOSE-PROVIDER-JSON"}', **fields}
    db.execute(f"INSERT INTO source_records ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
               list(fields.values()))
    return source_id


@pytest.fixture
def catalogue(db):
    a = _item(db, "Same", owned=1, authors="A Writer", series_name="Saga / One", series_position=1,
              isbn="9780000000101", isbn10="0000000101", upc="0123456789012", asin="B012345678",
              description="Catalogue synopsis", notes="DO-NOT-EXPOSE-ITEM-NOTES", language="en",
              cover_path="cover-1.jpg", manual_value=10.555, estimated_value=5.0)
    b = _item(db, "same", owned=0, wishlisted=True, authors="B Writer", series_name="saga / one", series_position=3)
    c = _item(db, "Zulu", owned=0, authors=None, series_name="Other", series_position=None)
    t = _item(db, "Trashed", owned=0, wishlisted=True, series_name="Hidden series")
    borrower = _insert_borrower(db)
    late = _loan(db, a, borrower, due_date=(date.today() - timedelta(days=1)).isoformat())
    returned = _loan(db, b, borrower, checked_in="2026-02-01", due_date="2026-01-15")
    fallback = _loan(db, c, borrower)
    hidden_loan = _loan(db, t, borrower)
    db.execute("INSERT INTO lists (slug, name) VALUES ('shared', 'Shared')")
    db.execute("INSERT INTO lists (slug, name) VALUES ('hidden', 'DO-NOT-EXPOSE-HIDDEN-LIST')")
    lists.add(db, "shared", a)
    lists.add(db, "shared", b)
    lists.add(db, "hidden", t)
    db.execute("INSERT INTO series_meta (name, description, complete) VALUES ('Saga / One', 'Local series synopsis', 1)")
    source = _source(db, a)
    watch = db.execute("INSERT INTO release_watches (series_name) VALUES ('Saga / One')").lastrowid
    release_radar.upsert_rows(db, watch, [
        {"hardcover_book_id": 100 + n, "title": title, "release_date": release_date,
         "authors": "A Writer", "cover_url": "https://images.example/cover.jpg", "series_position": n + 4}
        for n, (title, release_date) in enumerate([
            ("Soon", (date.today() + timedelta(days=3)).isoformat()),
            ("Later", (date.today() + timedelta(days=5)).isoformat()), ("Undated", None),
        ])
    ])
    item_write.trash_item(db, t)
    db.commit()
    return {"a": a, "b": b, "c": c, "trash": t, "late": late, "returned": returned,
            "fallback": fallback, "hidden_loan": hidden_loan, "borrower": borrower,
            "source": source, "watch": watch}


def _paths(seed):
    return {
        "/items": ItemPage,
        f"/items/{seed['a']}": ItemDetailResponse,
        "/loans": LoanPage,
        f"/loans/{seed['late']}": Loan,
        "/wishlist": ItemPage,
        "/lists": ListPage,
        "/lists/wishlist": ListSummary,
        "/lists/wishlist/items": ItemPage,
        "/series": SeriesPage,
        "/series/" + quote("Saga / One", safe=""): SeriesDetailResponse,
        "/upcoming": UpcomingPage,
        "/schema": OpenAPIDocument,
    }


def _page(body, total, *, page=1, page_size=50):
    assert set(body) == {"data", "total", "page", "page_size", "pages", "next_page"}
    assert body["total"] == total
    assert body["page"] == page
    assert body["page_size"] == page_size
    assert body["pages"] == (total + page_size - 1) // page_size
    expected = page + 1 if page < min(body["pages"], 10_000) else None
    assert body["next_page"] == expected


def _assert_private(body):
    text = json.dumps(body)
    assert "DO-NOT-EXPOSE" not in text
    for key in ("credentials_enc", "metadata_json", "snapshot_json", "notes", "reading_status",
                "user_item_state", "user_item_events", "user_id", "last_error", "external_id"):
        assert f'"{key}"' not in text


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
def test_every_read_endpoint_has_an_explicit_validating_schema_and_head(request, catalogue, role):
    client = request.getfixturevalue(role + "_client")
    for path, model in _paths(catalogue).items():
        response = client.get(PREFIX + path)
        assert response.status_code == 200, (path, response.text)
        assert model.model_validate(response.json()).model_dump(mode="json") == response.json()
        assert response.headers["cache-control"] == "no-store"
        if path != "/schema":
            _assert_private(response.json())
        head = client.head(PREFIX + path)
        assert head.status_code == 200, path
        assert head.content == b""
        assert head.headers["content-type"].startswith("application/json")
        assert head.headers["cache-control"] == "no-store"


def test_item_pages_have_true_totals_unique_ties_and_no_whole_catalogue_hydration(viewer_client, db, catalogue, monkeypatch):
    hydrated = []
    original = reads._item

    def record(row, **kwargs):
        hydrated.append(row["id"])
        return original(row, **kwargs)

    monkeypatch.setattr(reads, "_item", record)
    first = viewer_client.get(PREFIX + "/items?page_size=1").json()
    second = viewer_client.get(PREFIX + "/items?page_size=1&page=2").json()
    _page(first, 3, page_size=1)
    _page(second, 3, page=2, page_size=1)
    assert [first["data"][0]["id"], second["data"][0]["id"]] == [catalogue["a"], catalogue["b"]]
    assert hydrated == [catalogue["a"], catalogue["b"]]
    descending = viewer_client.get(PREFIX + "/items?sort=title&order=desc").json()["data"]
    assert [row["id"] for row in descending] == [catalogue["c"], catalogue["a"], catalogue["b"]]
    creator = viewer_client.get(PREFIX + "/items?sort=authors&order=desc").json()["data"]
    assert [row["id"] for row in creator] == [catalogue["b"], catalogue["a"], catalogue["c"]]
    beyond = viewer_client.get(PREFIX + "/items?page=10000&page_size=100").json()
    _page(beyond, 3, page=10000, page_size=100)
    assert beyond["data"] == []


@pytest.mark.parametrize("query, expected", [
    ("owned=true", ["a"]), ("owned=false", ["b", "c"]),
    ("wishlisted=true", ["b"]), ("wishlisted=false", ["a", "c"]),
    ("q=writer&owned=false&media_type=book", ["b"]),
    ("q=9780000000101", ["a"]), ("q=0000000101", ["a"]),
    ("q=0123456789012", ["a"]), ("q=B012345678", ["a"]),
    ("media_type=vinyl", []), ("q=%27%20OR%201%3D1--", []),
])
def test_item_filters_intersect_and_identifiers_are_searchable(viewer_client, catalogue, query, expected):
    body = viewer_client.get(PREFIX + "/items?" + query).json()
    _page(body, len(expected))
    assert [row["id"] for row in body["data"]] == [catalogue[key] for key in expected]


@pytest.mark.parametrize("literal", ["%", "_", "\\"])
def test_search_wildcards_are_literal(viewer_client, db, literal):
    wanted = _item(db, "Title with " + literal)
    _item(db, "Unrelated")
    db.commit()
    response = viewer_client.get(PREFIX + "/items", params={"q": literal})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["data"]] == [wanted]


def test_wishlist_alias_uses_membership_and_lists_count_visible_items(viewer_client, catalogue):
    alias = viewer_client.get(PREFIX + "/wishlist").json()
    assert alias == viewer_client.get(PREFIX + "/lists/wishlist/items").json()
    assert [row["id"] for row in alias["data"]] == [catalogue["b"]]
    body = viewer_client.get(PREFIX + "/lists?page_size=1").json()
    _page(body, 2, page_size=1)
    assert body["data"][0]["slug"] == "shared"
    assert body["data"][0]["item_count"] == 2
    assert viewer_client.get(PREFIX + "/lists/wishlist").json()["item_count"] == 1
    assert viewer_client.get(PREFIX + "/lists/hidden").status_code == 404
    assert viewer_client.get(PREFIX + "/lists/hidden/items").status_code == 404
    page = viewer_client.get(PREFIX + "/lists/shared/items?owned=false&page_size=1").json()
    _page(page, 1, page_size=1)
    assert page["data"][0]["id"] == catalogue["b"]


def test_empty_wishlist_remains_addressable_without_personal_lists(viewer_client):
    body = viewer_client.get(PREFIX + "/lists").json()
    assert len(body["data"]) == 1
    assert body["data"][0]["slug"] == "wishlist"
    assert body["data"][0]["item_count"] == 0
    _page(viewer_client.get(PREFIX + "/wishlist").json(), 0)


def test_series_index_case_identity_details_slashes_and_positions(viewer_client, catalogue):
    body = viewer_client.get(PREFIX + "/series?sort=item_count&order=desc&page_size=1").json()
    _page(body, 2, page_size=1)
    assert body["data"] == [{"name": "Saga / One", "item_count": 2, "owned_count": 1,
                             "wishlist_count": 1, "complete": True}]
    path = PREFIX + "/series/" + quote("SAGA / ONE", safe="")
    detail = viewer_client.get(path + "?page_size=1&page=2").json()
    assert detail["description"] == "Local series synopsis"
    _page(detail["items"], 2, page=2, page_size=1)
    assert detail["items"]["data"][0]["id"] == catalogue["b"]
    filtered = viewer_client.get(PREFIX + "/series?q=Other").json()
    _page(filtered, 1)
    assert filtered["data"][0]["name"] == "Other"


@pytest.mark.parametrize("status, keys", [
    ("open", ["late", "fallback"]), ("returned", ["returned"]),
    ("overdue", ["late", "fallback"]), ("all", ["late", "returned", "fallback"]),
])
def test_loan_filters_overdue_and_visibility(viewer_client, catalogue, status, keys):
    body = viewer_client.get(PREFIX + "/loans", params={"status": status, "page_size": 1, "page": 1}).json()
    _page(body, len(keys), page_size=1)
    ids = []
    for page in range(1, len(keys) + 1):
        data = viewer_client.get(PREFIX + "/loans", params={"status": status, "page_size": 1, "page": page}).json()["data"]
        ids.extend(row["id"] for row in data)
    assert ids == [catalogue[key] for key in keys]
    detail = viewer_client.get(PREFIX + f"/loans/{catalogue['returned']}").json()
    assert detail["overdue"] is False


def test_loan_item_borrower_filters_and_disabled_fallback(viewer_client, db, catalogue):
    body = viewer_client.get(PREFIX + f"/loans?item_id={catalogue['a']}&borrower_id={catalogue['borrower']}").json()
    _page(body, 1)
    assert body["data"][0]["id"] == catalogue["late"]
    set_setting(db, "lending_overdue_days", "0")
    db.commit()
    body = viewer_client.get(PREFIX + "/loans?status=overdue").json()
    assert [row["id"] for row in body["data"]] == [catalogue["late"]]


def test_upcoming_pages_filters_held_dismissed_and_orphan_watches(viewer_client, db, catalogue):
    _item(db, "Soon", owned=1, hardcover_book_id=100)
    orphan = db.execute("INSERT INTO release_watches (series_name) VALUES ('Private orphan')").lastrowid
    release_radar.upsert_rows(db, orphan, [{"hardcover_book_id": 400, "title": "DO-NOT-EXPOSE-ORPHAN"}])
    release_radar.upsert_rows(db, catalogue["watch"], [
        {"hardcover_book_id": 700, "title": "Dismissed"},
        {"hardcover_book_id": 701, "title": " SAME "},
    ])
    db.execute("UPDATE upcoming_releases SET dismissed = 1 WHERE hc_book_id = 700")
    db.commit()
    body = viewer_client.get(PREFIX + "/upcoming?page_size=1").json()
    _page(body, 2, page_size=1)
    assert body["data"][0]["title"] == "Later"
    undated = viewer_client.get(PREFIX + "/upcoming?dated=false").json()
    assert [row["title"] for row in undated["data"]] == ["Undated"]
    bound = (date.today() + timedelta(days=5)).isoformat()
    dated = viewer_client.get(PREFIX + "/upcoming", params={"release_from": bound, "release_to": bound,
                                                            "series_name": "SAGA / ONE"}).json()
    assert [row["title"] for row in dated["data"]] == ["Later"]
    _assert_private(body)


@pytest.mark.parametrize("path", ["/items", "/loans", "/wishlist", "/lists", "/series", "/upcoming"])
@pytest.mark.parametrize("query", ["page=0", "page=10001", "page=1.5", "page_size=0", "page_size=101",
                                  "page_size=many", "sort=id;DROP%20TABLE%20items", "order=random", "user_id=1"])
def test_invalid_paging_sorting_or_unknown_queries_are_422(viewer_client, path, query):
    response = viewer_client.get(PREFIX + path + "?" + query)
    assert response.status_code == 422, response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path, query", [
    ("/items", "media_type=music"), ("/items", "owned=maybe"),
    ("/items", "q=" + "x" * 201), ("/loans", "status=active"),
    ("/loans", "item_id=0"), ("/loans", "borrower_id=9223372036854775808"),
    ("/upcoming", "release_from=not-a-date"),
    ("/upcoming", "release_from=2027-02-01&release_to=2027-01-01"),
    ("/upcoming", "series_name="), ("/lists", "q=" + "x" * 201),
])
def test_invalid_endpoint_filters_are_422(viewer_client, path, query):
    assert viewer_client.get(PREFIX + path + "?" + query).status_code == 422


@pytest.mark.parametrize("path", ["/items/0", "/items/9223372036854775808", "/loans/not-an-id",
                                  "/loans/-1", "/items/1?page_size=101", "/series/One?sort=authors"])
def test_detail_ids_and_queries_are_bounded(viewer_client, path):
    assert viewer_client.get(PREFIX + path).status_code == 422


@pytest.mark.parametrize("feature, label, paths", [
    ("lending", "Lending", ["/loans", "/loans/1"]),
    ("series", "Series", ["/series", "/series/Hidden"]),
    ("release_radar", "Release radar", ["/upcoming"]),
    ("hardcover", "Hardcover", ["/upcoming"]),
])
def test_dedicated_feature_routes_refuse_get_and_head(viewer_client, feature, label, paths):
    off(feature)
    for path in paths:
        assert_json_403(viewer_client.get(PREFIX + path), feature, label)
        head = viewer_client.head(PREFIX + path)
        assert head.status_code == 403
        assert head.content == b""


def test_api_feature_refuses_every_new_read_and_schema(viewer_client, catalogue):
    off("api")
    for path in _paths(catalogue):
        assert_json_403(viewer_client.get(PREFIX + path), "api", "API tokens")
        assert viewer_client.head(PREFIX + path).status_code == 403


def test_disabled_item_sections_are_present_null_and_do_not_run_source_helper(viewer_client, catalogue, monkeypatch):
    off("valuation", "connected_library", "series")

    def not_called(*args):
        raise AssertionError("Disabled sources section was read")

    monkeypatch.setattr(reads, "source_page", not_called)
    body = viewer_client.get(PREFIX + f"/items/{catalogue['a']}").json()
    assert set(body) == {"item", "value", "sources"}
    assert body["sources"] is body["value"] is body["item"]["series"] is None
    assert viewer_client.get(PREFIX + "/items").json()["data"][0]["series"] is None
    assert viewer_client.get(PREFIX + "/wishlist").status_code == 200


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,test", "https://user:DO-NOT-EXPOSE-PASSWORD@books.example/read",
    "https://books.example/read?api_key=DO-NOT-EXPOSE-KEY",
    "https://books.example/#/read?access_token=DO-NOT-EXPOSE-TOKEN",
    "https://books.example/read?id=42;token=DO-NOT-EXPOSE-TOKEN",
    "https://books.example:invalid/read", "//outside.example/read", "file:///private",
])
def test_source_and_upcoming_urls_are_credential_free(viewer_client, db, url):
    item_id = _item(db, "Local", series_name="Local")
    _source(db, item_id, url=url)
    watch = db.execute("INSERT INTO release_watches (series_name) VALUES ('Local')").lastrowid
    # Raw seed deliberately bypasses radar's weaker https-only input check.
    db.execute("INSERT INTO upcoming_releases (watch_id, hc_book_id, title, cover_url) VALUES (?, 99, 'New', ?)",
               (watch, url))
    db.commit()
    detail = viewer_client.get(PREFIX + f"/items/{item_id}").json()
    upcoming = viewer_client.get(PREFIX + "/upcoming").json()
    assert detail["sources"]["data"][0]["url"] is None
    assert upcoming["data"][0]["cover_url"] is None
    _assert_private(detail)
    _assert_private(upcoming)


def test_item_detail_source_paging_safe_relative_urls_legacy_links_and_feature_flags(viewer_client, db, catalogue):
    _source(db, catalogue["a"], name="Books", url="read/99")
    db.execute("UPDATE items SET abs_id = 'id/1?token=DO-NOT-EXPOSE-ID' WHERE id = ?", (catalogue["a"],))
    set_setting(db, "abs_url", "http://abs:13378")
    set_setting(db, "abs_public_url", "https://audio.example")
    db.commit()
    detail = viewer_client.get(PREFIX + f"/items/{catalogue['a']}?page_size=1&page=1").json()
    _page(detail["sources"], 3, page_size=1)
    url = detail["sources"]["data"][0]["url"]
    assert url == "https://audio.example/item/id%2F1%3Ftoken%3DDO-NOT-EXPOSE-ID"
    # IDs are path-encoded, not an authentication-bearing query; no raw ID field.
    second = viewer_client.get(PREFIX + f"/items/{catalogue['a']}?page_size=1&page=2").json()
    assert second["sources"]["data"][0]["url"] == "https://books.example/library/read/99"
    assert second["value"] == {"amount": 10.55, "currency": "USD"}
    assert second["item"]["cover_url"] == "/covers/cover-1.jpg"
    off("abs_sync")
    assert viewer_client.get(PREFIX + f"/items/{catalogue['a']}").json()["sources"]["total"] == 2


def test_disabled_source_has_no_url_and_unsafe_cover_paths_are_not_returned(viewer_client, db, catalogue):
    db.execute("UPDATE source_instances SET enabled = 0 WHERE id = ?", (catalogue["source"],))
    db.execute("UPDATE items SET cover_path = '/private/DO-NOT-EXPOSE-PATH.jpg' WHERE id = ?", (catalogue["a"],))
    db.commit()
    detail = viewer_client.get(PREFIX + f"/items/{catalogue['a']}").json()
    assert detail["sources"]["data"][0]["url"] is None
    assert detail["sources"]["data"][0]["state"] == "unknown"
    assert detail["item"]["cover_url"] is None
    _assert_private(detail)


def test_tokens_read_all_new_endpoints_without_refresh_or_other_users_activity(client, admin_user, viewer_user, db, catalogue):
    for user in (admin_user, viewer_user):
        db.execute("INSERT INTO user_item_state (user_id, item_id, notes, on_list) VALUES (?, ?, ?, 1)",
                   (user["id"], catalogue["a"], "DO-NOT-EXPOSE-PERSONAL-NOTES"))
        db.execute("INSERT INTO user_item_events (user_id, item_id, event_type, snapshot_json) VALUES (?, ?, 'note', ?)",
                   (user["id"], catalogue["a"], '{"notes":"DO-NOT-EXPOSE-PERSONAL-HISTORY"}'))
        db.execute("INSERT INTO library_saved_views (user_id, name, filters_json) VALUES (?, ?, '{}')",
                   (user["id"], "DO-NOT-EXPOSE-PERSONAL-VIEW"))
    token = api_tokens.create(db, admin_user["id"], "read-api")["token"]
    db.commit()
    client.cookies.clear()
    for path in _paths(catalogue):
        for method in ("GET", "HEAD"):
            response = client.request(method, PREFIX + path, headers={"Authorization": "Bearer " + token})
            assert response.status_code == 200, (method, path, response.text)
            assert "set-cookie" not in response.headers
            if method == "HEAD":
                assert response.content == b""
            elif path != "/schema":
                _assert_private(response.json())
        for method in ("POST", "PATCH", "DELETE"):
            response = client.request(method, PREFIX + path, headers={"Authorization": "Bearer " + token})
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
    assert api_tokens.resolve(token)["role"] == "viewer"


def test_anonymous_bad_bearer_and_revoked_credentials_do_not_fall_back_or_redirect(admin_client, admin_user, db, catalogue):
    token = api_tokens.create(db, admin_user["id"], "revoked")
    db.commit()
    api_tokens.revoke(db, token["id"])
    db.commit()
    cookie = admin_client.cookies.get("access_token")
    for path in _paths(catalogue):
        admin_client.cookies.set("access_token", cookie)
        for credential in ("Bearer bad", "Bearer " + token["token"]):
            response = admin_client.get(PREFIX + path, headers={"Authorization": credential}, follow_redirects=False)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert "location" not in response.headers
        admin_client.cookies.clear()
        for method in ("GET", "HEAD"):
            response = admin_client.request(method, PREFIX + path, follow_redirects=False)
            assert response.status_code == 401
            assert "location" not in response.headers


def test_hidden_trashed_and_unknown_resources_have_identical_404s(viewer_client, catalogue):
    for hidden, unknown in ((f"/items/{catalogue['trash']}", "/items/999999"),
                            (f"/loans/{catalogue['hidden_loan']}", "/loans/999999"),
                            ("/lists/hidden", "/lists/unknown"),
                            ("/series/Hidden%20series", "/series/Unknown")):
        one = viewer_client.get(PREFIX + hidden)
        two = viewer_client.get(PREFIX + unknown)
        assert one.status_code == two.status_code == 404
        assert one.json() == two.json()
        assert viewer_client.head(PREFIX + hidden).status_code == 404


def test_server_scoped_live_view_is_used_for_all_new_reads(viewer_client, db, monkeypatch):
    from app.routers import api_v1

    visible = _item(db, "Visible", owned=0, wishlisted=True)
    hidden = _item(db, "DO-NOT-EXPOSE-HIDDEN-ITEM", owned=0, wishlisted=True,
                   series_name="DO-NOT-EXPOSE-HIDDEN-SERIES")
    borrower = _insert_borrower(db)
    hidden_loan = _loan(db, hidden, borrower)
    db.execute("INSERT INTO lists (slug, name) VALUES ('hidden', 'DO-NOT-EXPOSE-HIDDEN-LIST')")
    lists.add(db, "hidden", hidden)
    _source(db, hidden)
    watch = db.execute("INSERT INTO release_watches (series_name) VALUES ('DO-NOT-EXPOSE-HIDDEN-SERIES')").lastrowid
    release_radar.upsert_rows(db, watch, [{"hardcover_book_id": 900, "title": "DO-NOT-EXPOSE-HIDDEN-RELEASE"}])
    db.commit()

    @contextmanager
    def scoped_db():
        with get_db() as connection:
            connection.execute("DROP VIEW items_live")
            connection.execute(f"CREATE TEMP VIEW items_live AS SELECT * FROM items WHERE deleted_at IS NULL AND id != {hidden}")
            yield connection

    monkeypatch.setattr(api_v1, "get_db", scoped_db)
    for path in ("/items", "/wishlist", "/lists", "/lists/wishlist/items", "/series", "/loans?status=all", "/upcoming"):
        response = viewer_client.get(PREFIX + path)
        assert response.status_code == 200
        _assert_private(response.json())
    assert viewer_client.get(PREFIX + "/items").json()["total"] == 1
    assert viewer_client.get(PREFIX + "/items").json()["data"][0]["id"] == visible
    assert viewer_client.get(PREFIX + "/lists/wishlist").json()["item_count"] == 1
    for path in (f"/items/{hidden}", f"/loans/{hidden_loan}", "/lists/hidden", "/series/DO-NOT-EXPOSE-HIDDEN-SERIES"):
        assert viewer_client.get(PREFIX + path).status_code == 404


def test_count_and_page_share_a_snapshot_under_a_concurrent_insert(viewer_client, db, monkeypatch):
    from app.routers import api_v1

    first = _item(db, "First")
    db.commit()
    inserted = []

    class Connection:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def execute(self, statement, parameters=()):
            cursor = self.wrapped.execute(statement, parameters)
            if statement.startswith("SELECT COUNT(*) FROM items_live i") and not inserted:
                with get_db() as writer:
                    inserted.append(_item(writer, "Concurrent"))
            return cursor

    @contextmanager
    def concurrent_db():
        with get_db() as connection:
            yield Connection(connection)

    monkeypatch.setattr(api_v1, "get_db", concurrent_db)
    body = viewer_client.get(PREFIX + "/items").json()
    _page(body, 1)
    assert [row["id"] for row in body["data"]] == [first]
    assert inserted
    assert viewer_client.get(PREFIX + "/items").json()["total"] == 2


def test_published_schema_covers_only_v1_resolves_refs_and_has_unique_operations(viewer_client, catalogue):
    document = viewer_client.get(PREFIX + "/schema").json()
    assert document["openapi"].startswith("3.1.")
    paths = document["paths"]
    assert len(paths) == len(_paths(catalogue)) + 1  # existing stats
    assert all(path.startswith(PREFIX + "/") for path in paths)
    assert "/api/settings/backup" not in paths
    operation_ids = []
    for path, methods in paths.items():
        assert set(methods) == {"get"}
        operation = methods["get"]
        operation_ids.append(operation["operationId"])
        assert operation["security"] == [{"BearerToken": []}, {"SessionCookie": []}]
        assert {"200", "401", "403"} <= operation["responses"].keys()
        assert operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert len(operation_ids) == len(set(operation_ids))

    def resolve_refs(value):
        if isinstance(value, list):
            for entry in value:
                resolve_refs(entry)
        if isinstance(value, dict):
            if "$ref" in value:
                target = document
                for part in value["$ref"].removeprefix("#/").split("/"):
                    target = target[part]
                assert target
            for entry in value.values():
                resolve_refs(entry)

    resolve_refs(document)
    params = {row["name"]: row["schema"] for row in paths[PREFIX + "/items"]["get"]["parameters"]}
    assert params["page_size"]["maximum"] == 100
    assert params["page"]["maximum"] == 10000
    assert params["q"]["maxLength"] == 200
    assert document["components"]["schemas"]["ItemDetailResponse"]["required"] == ["item", "value", "sources"]


def test_all_documented_sort_keys_are_executable_and_repeatable(viewer_client, catalogue):
    choices = {
        "/items": ("title", "authors", "created_at", "id"),
        "/wishlist": ("title", "authors", "created_at", "id"),
        "/loans": ("checked_out", "due_date", "title", "id"),
        "/lists": ("name", "created_at", "item_count"),
        "/series": ("name", "item_count", "owned_count", "wishlist_count"),
        "/series/Saga%20%2F%20One": ("position", "title", "id"),
        "/upcoming": ("release_date", "title", "series_name"),
    }
    for path, keys in choices.items():
        for sort in keys:
            for order in ("asc", "desc"):
                params = {"sort": sort, "order": order, "page_size": 1}
                first = viewer_client.get(PREFIX + path, params=params)
                second = viewer_client.get(PREFIX + path, params=params)
                assert first.status_code == second.status_code == 200, (path, params, first.text)
                assert first.json() == second.json()


def test_paging_bound_never_emits_an_unrequestable_next_page():
    body = reads._page(ItemPage, [], 1_000_001, PagingQuery(page=10000, page_size=100))
    assert body.pages == 10001
    assert body.next_page is None


def test_all_legacy_and_provider_alias_source_flags_are_respected(viewer_client, db):
    item_id = _item(db, "Mixed holdings", abs_id="audio/42")
    for prefix in ("abs", "komga", "romm"):
        set_setting(db, prefix + "_url", "http://" + prefix + ":8080")
    for provider in ("abs", "abs_sync", "audiobookshelf", "komga", "romm"):
        source_id = _source(db, item_id, name=provider)
        db.execute("UPDATE source_instances SET provider = ? WHERE id = ?", (provider, source_id))
    db.execute("INSERT INTO komga_records (komga_id, item_id, library_id, kind) VALUES ('comic/42', ?, 'local', 'comic')", (item_id,))
    db.execute("INSERT INTO romm_records (romm_id, item_id, platform_id) VALUES ('game/42', ?, 'local')", (item_id,))
    db.commit()
    body = viewer_client.get(PREFIX + f"/items/{item_id}").json()
    _page(body["sources"], 8)
    urls = {row["key"]: row["url"] for row in body["sources"]["data"]}
    assert urls["abs"] == "http://abs:8080/item/audio%2F42"
    assert urls["komga"] == "http://komga:8080/book/comic%2F42"
    assert urls["romm"] == "http://romm:8080/rom/game%2F42"
    off("abs_sync", "komga", "romm")
    body = viewer_client.get(PREFIX + f"/items/{item_id}").json()
    _page(body["sources"], 0)


def test_explicit_unavailability_survives_source_failure(viewer_client, db):
    item_id = _item(db, "Unavailable")
    _source(db, item_id, availability="unavailable")
    db.commit()
    body = viewer_client.get(PREFIX + f"/items/{item_id}").json()
    assert body["sources"]["data"][0]["state"] == "unavailable"
    _assert_private(body)


def test_new_read_services_do_not_write_catalogue_or_activity(db, catalogue):
    before = db.total_changes
    reads.item_page(db, ItemQuery())
    reads.item_detail(db, catalogue["a"], PagingQuery())
    reads.loan_page(db, LoanQuery())
    reads.loan_detail(db, catalogue["late"])
    reads.list_page(db, ListQuery())
    reads.list_detail(db, "wishlist")
    reads.series_page(db, SeriesQuery())
    reads.series_detail(db, "Saga / One", SeriesItemsQuery())
    reads.upcoming_page(db, UpcomingQuery())
    assert db.total_changes == before


def test_role_refusal_precedes_feature_refusal_on_all_new_reads(viewer_client, catalogue, monkeypatch):
    from app import auth

    # All real account roles can read v1, and the users CHECK correctly forbids
    # lesser roles. Inject that request principal to exercise the role guard's
    # refusal/order without weakening the database or the production dependency.
    original = auth.get_current_user

    def lesser_principal(request):
        principal = original(request)
        return {**principal, "role": "unknown"} if principal else None

    monkeypatch.setattr(auth, "get_current_user", lesser_principal)
    off("api", "lending", "series", "release_radar", "hardcover")
    for path in _paths(catalogue):
        response = viewer_client.get(PREFIX + path)
        assert response.status_code == 403
        assert response.text == "Forbidden"
        assert "feature_disabled" not in response.text
