"""Bounded main-only surface regressions; run after parent integration/freeze.

Real central schema, writers, scoped views and HTTP dispatch are used. Provider
results are stubbed. Named fault-injection tests wrap the real writers, and
helper-only tests capture template contexts; security/schema are not replaced.
No existing lineage assertion is changed by this module.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request

from app import features
from app.auth import _ResponseException, create_token
from app.database import set_setting
from app.routers import items_catalog, magazines, pages, romm_catalog
from app.services import api_tokens, bulk_issues, catalogue_security as security
from app.services import item_copies, item_write, libraries, lists, periodical_records
from app.services import provider_result, user_state


@contextmanager
def _acting(db, user):
    token = security.set_actor(user)
    try:
        security.install_catalogue_views(db)
        yield
    finally:
        security.reset_actor(token)
        security.install_catalogue_views(db)


def _user(db, name="surface-user", role="viewer"):
    user_id = db.execute(
        "INSERT INTO users (username, password, role) VALUES (?, 'not-a-login', ?)",
        (name, role),
    ).lastrowid
    return {"id": user_id, "username": name, "role": role}


def _sign_in(client, user):
    client.cookies.set("access_token", create_token(user["id"], user["username"], user["role"]))


def _issue(db, kind, *, library_id=1, number=1, publication_id=None, **fields):
    item_id = item_write.insert_item(
        db, title=f"Surface Monthly #{number}", media_type=kind,
        series_name="Surface Monthly", library_id=library_id,
        series_position=float(number) if kind == "comic" else None, **fields,
    )
    if kind == "magazine":
        if publication_id is None:
            publication_id = periodical_records.upsert_publication(db, title="Surface Monthly")
        periodical_records.link_issue(
            db, item_id=item_id, publication_id=publication_id, volume="7", issue_number=str(number),
        )
    return item_id, publication_id


def _add_range(db, kind, publication_id=None, first=1, last=3):
    if kind == "comic":
        return bulk_issues.add_comic_issue_range(
            db, series_name="Surface Monthly", first_issue=first, last_issue=last,
        )
    return bulk_issues.add_magazine_issue_range(
        db, publication_id=publication_id, volume="7", first_issue=first, last_issue=last,
    )


def _snapshot(db):
    return tuple(db.iterdump())


def _request(user=None):
    request = Request({
        "type": "http", "method": "GET", "scheme": "https", "path": "/api/title-search",
        "query_string": b"", "headers": [], "server": ("testserver", 443),
        "client": ("testclient", 5000),
        "app": SimpleNamespace(state=SimpleNamespace(templates=SimpleNamespace(
            TemplateResponse=lambda request, name, context: context,
        ))),
    })
    if user is not None:
        request.state.user = user
    return request


def test_explicit_surface_routers_do_not_alias_or_replace_canonical_handlers(client):
    assert magazines.router is not items_catalog.router
    assert magazines.page_router is not pages.router
    assert {route.path for route in magazines.router.routes} == {
        "/api/magazines/search", "/api/magazines/select-search-result", "/api/magazines/add",
        "/api/magazines/assist/scan",
    }
    assert {route.path for route in magazines.page_router.routes} == {
        "/magazines", "/magazines/publications/{publication_id}",
    }
    for path, endpoint in (
        ("/api/title-search", items_catalog.title_search),
        ("/api/magazines/assist/scan", magazines.assist_magazine_barcode),
        ("/magazines", magazines.magazine_publications),
        ("/magazines/publications/{publication_id}", magazines.magazine_publication_detail),
        ("/romm", romm_catalog.romm_index),
        ("/romm/library", romm_catalog.romm_library),
    ):
        matches = [route for route in client.app.routes if getattr(route, "path", None) == path]
        assert len(matches) == 1, path
        assert matches[0].endpoint is endpoint
        if path == "/api/magazines/assist/scan":
            assert matches[0].methods == {"GET"}


@pytest.mark.parametrize("router,feature", [
    (magazines.router, "periodicals"), (magazines.page_router, "periodicals"),
    (romm_catalog.router, "romm"),
])
def test_surface_dependencies_authenticate_before_feature_gate(router, feature):
    for route in router.routes:
        dependencies = route.dependant.dependencies
        assert dependencies[0].call.__module__ == "app.services.catalogue_security"
        assert getattr(dependencies[1].call, "feature_key", None) == feature


@pytest.mark.parametrize("feature,method,path,status", [
    ("periodicals", "get", "/magazines", 200),
    ("periodicals", "get", "/magazines/publications/1", 200),
    ("periodicals", "get", "/api/magazines/search?q=Surface", 403),
    ("periodicals", "get", "/api/title-search?media_type=magazine&q=Surface", 403),
    ("periodicals", "get", "/api/title-search?media_type=magazine", 403),
    ("periodicals", "get", "/api/magazines/select-search-result?google_volume_id=one", 403),
    ("periodicals", "post", "/api/magazines/add", 403),
    ("romm", "get", "/romm", 200),
    ("romm", "get", "/romm/library", 200),
])
def test_disabled_surfaces_refuse_before_provider_or_catalogue_work(
    editor_client, db, monkeypatch, feature, method, path, status,
):
    lookup = AsyncMock(side_effect=AssertionError("disabled surface contacted Google"))
    monkeypatch.setattr(magazines.magazine_google, "lookup_issue", lookup)
    monkeypatch.setattr(magazines.magazine_google, "search_issues", lookup)
    monkeypatch.setattr(items_catalog.openlibrary, "search_books", lookup)
    features.set_feature_enabled(db, feature, False)
    db.commit()
    response = getattr(editor_client, method)(path, follow_redirects=False)
    assert response.status_code == status
    assert "turned off" in response.text
    lookup.assert_not_awaited()
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_direct_magazine_helper_requires_actor_auth_and_feature(editor_user, db, monkeypatch):
    lookup = AsyncMock(side_effect=AssertionError("unguarded helper contacted Google"))
    monkeypatch.setattr(magazines.magazine_google, "search_issues", lookup)
    with pytest.raises(RuntimeError, match="authenticated catalogue actor"):
        await magazines.magazine_search_results(_request(editor_user), q="Surface")
    with pytest.raises(_ResponseException) as exc:
        await magazines.magazine_search_results(_request(), q="Surface")
    assert exc.value.response.status_code == 401
    features.set_feature_enabled(db, "periodicals", False)
    db.commit()
    with _acting(db, editor_user), pytest.raises(_ResponseException) as exc:
        await magazines.magazine_search_results(_request(editor_user), q="Surface")
    assert exc.value.response.status_code == 403
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_magazine_helper_retains_all_continuation_fields(editor_user, db, monkeypatch):
    lookup = AsyncMock(return_value=provider_result.found("google", [{
        "google_volume_id": "exact-issue", "title": "Surface Monthly", "issue_date": "2024-05",
    }]))
    monkeypatch.setattr(magazines.magazine_google, "search_issues", lookup)
    with _acting(db, editor_user):
        context = await magazines.magazine_search_results(
            _request(editor_user), q=" Surface Monthly ", location_id=17, mode="wishlist",
            carrier_ean="9770953616115", barcode_supplement="04", issn_hint="0953-6167",
            for_scan=True, library_id=1, tags="magazine-tag", reading_status="read",
        )
    assert context["query"] == "Surface Monthly"
    assert context["results"][0]["google_volume_id"] == "exact-issue"
    assert {key: context[key] for key in (
        "selected_location_id", "mode", "carrier_ean", "barcode_supplement", "issn_hint",
        "for_scan", "library_id", "selected_library_id", "tags", "reading_status",
    )} == {
        "selected_location_id": 17, "mode": "wishlist", "carrier_ean": "9770953616115",
        "barcode_supplement": "04", "issn_hint": "0953-6167", "for_scan": True,
        "library_id": 1, "selected_library_id": 1, "tags": "magazine-tag", "reading_status": "read",
    }
    assert lookup.await_args.args[0] == "Surface Monthly"


def test_canonical_magazine_title_search_uses_exact_issue_helper(editor_client, monkeypatch):
    lookup = AsyncMock(return_value=provider_result.found("google", [{
        "google_volume_id": "canonical-exact", "title": "Surface Monthly", "issue_date": "2024-05",
    }]))
    monkeypatch.setattr(magazines.magazine_google, "search_issues", lookup)
    monkeypatch.setattr(items_catalog.openlibrary, "search_books", AsyncMock(
        side_effect=AssertionError("magazine title search fell through to books"),
    ))
    response = editor_client.get("/api/title-search", params={
        "q": "Surface Monthly", "media_type": "magazine", "location_id": 17,
        "mode": "wishlist", "carrier_ean": "9770953616115", "barcode_supplement": "04",
        "issn_hint": "0953-6167", "for_scan": True, "library_id": 1,
        "tags": "surface-tag", "reading_status": "read",
    })
    assert response.status_code == 200
    assert response.template.name == "fragments/magazine_search_results.html"
    assert response.context["selected_location_id"] == 17
    assert response.context["mode"] == "wishlist"
    assert response.context["barcode_supplement"] == "04"
    assert response.context["library_id"] == 1
    assert response.context["tags"] == "surface-tag"
    assert response.context["reading_status"] == "read"


@pytest.mark.parametrize("method,path", [
    ("get", "/api/magazines/select-search-result"), ("post", "/api/magazines/add"),
])
def test_google_selection_cannot_substitute_a_different_provider_identity(
    editor_client, db, monkeypatch, method, path,
):
    lookup = AsyncMock(return_value=provider_result.found("google", {
        "google_volume_id": "different-id", "title": "Do not add", "issue_date": "2024-05",
    }))
    monkeypatch.setattr(magazines.magazine_google, "lookup_issue", lookup)
    kwargs = {"params" if method == "get" else "data": {"google_volume_id": "chosen-id"}}
    response = getattr(editor_client, method)(path, **kwargs)
    assert response.status_code == 200
    assert 'data-scan-status="error"' in response.text
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM periodical_publications").fetchone()[0] == 0


def test_magazine_add_uses_library_editor_authority_and_shared_not_personal_wishlist(client, db):
    actor = _user(db)
    library = libraries.create_library(db, "Surface destination")
    libraries.set_membership(db, library["id"], actor["id"], "editor")
    db.commit()
    _sign_in(client, actor)
    response = client.post("/api/magazines/add", data={
        "title": "Surface Monthly", "issue_number": "4", "library_id": library["id"],
        "mode": "wishlist", "issn": "1396-5360", "carrier_ean": "9770953616115",
        "barcode_supplement": "04",
    })
    assert response.status_code == 200
    assert 'data-scan-status="wishlisted"' in response.text
    item = db.execute("SELECT * FROM items").fetchone()
    assert item["media_type"] == "magazine"
    assert item["owned"] == 0
    assert item["upc"] is None
    assert libraries.item_library_id(db, item["id"]) == library["id"]
    assert lists.is_member(db, lists.WISHLIST, item["id"])
    assert user_state.get_state(db, actor["id"], item["id"])["wishlist"] == 0
    issue = db.execute("SELECT * FROM periodical_issues").fetchone()
    assert issue["barcode_ean"] == "9770953616115"
    assert issue["barcode_supplement"] == "04"
    publication = db.execute("SELECT * FROM periodical_publications").fetchone()
    assert publication["issn"] == "1396-5360"  # carrier ISSN remains only a hint


def test_magazine_add_does_not_promote_global_editor_over_target_library(editor_client, editor_user, db):
    library = libraries.create_library(db, "Surface read-only destination")
    libraries.set_membership(db, library["id"], editor_user["id"], "viewer")
    db.commit()
    response = editor_client.post("/api/magazines/add", data={
        "title": "Surface Monthly", "issue_number": "1", "library_id": library["id"],
    })
    assert response.status_code == 403
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM periodical_publications").fetchone()[0] == 0


@pytest.mark.parametrize("fields", [
    {"issn": "0161-7371"}, {"issue_date": "2024-02-31"},
    {"barcode_supplement": "04"}, {"carrier_ean": "9770953616115", "barcode_supplement": "4"},
])
def test_invalid_manual_confirmation_leaves_no_item_or_publication(editor_client, db, fields):
    response = editor_client.post("/api/magazines/add", data={
        "title": "Surface Monthly", "issue_number": "1", **fields,
    })
    assert response.status_code == 200
    assert 'data-scan-status="error"' in response.text
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM periodical_publications").fetchone()[0] == 0


def test_magazine_add_retains_csrf_boundary(editor_client, db):
    response = editor_client.post("/api/magazines/add", headers={"X-CSRF-Token": ""}, data={
        "title": "Surface Monthly", "issue_number": "1",
    })
    assert response.status_code == 403
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


@pytest.mark.parametrize("trashed", [False, True])
def test_hidden_issue_identity_refuses_without_disclosure_or_metadata_changes(editor_client, db, trashed):
    hidden_library = libraries.create_library(db, "Never disclose this library")
    hidden, publication_id = _issue(db, "magazine", library_id=hidden_library["id"])
    if trashed:
        item_write.trash_item(db, hidden)
    db.commit()
    before = _snapshot(db)
    response = editor_client.post("/api/magazines/add", data={
        "title": "Surface Monthly", "issue_number": "1", "volume": "7", "publisher": "Not applied",
    })
    assert response.status_code == 200
    assert "This issue identity is unavailable" in response.text
    assert f"/item/{hidden}" not in response.text
    assert "Never disclose" not in response.text
    assert 'data-scan-status="duplicate"' not in response.text
    assert _snapshot(db) == before
    assert db.execute("SELECT publisher FROM periodical_publications WHERE id = ?", (publication_id,)).fetchone()[0] is None


def test_visible_trashed_magazine_uses_native_restore_action_without_rewriting(editor_client, db):
    item, _ = _issue(db, "magazine")
    item_write.trash_item(db, item)
    db.commit()
    before = _snapshot(db)
    response = editor_client.post("/api/magazines/add", data={
        "title": "Surface Monthly", "issue_number": "1", "volume": "7", "publisher": "Not applied",
    })
    assert response.status_code == 200
    assert 'data-scan-status="in_trash"' in response.text
    assert f'action="/api/trash/items/{item}/restore"' in response.text
    assert _snapshot(db) == before


def test_conflicting_google_and_manual_issue_evidence_refuses_every_write(editor_client, db, monkeypatch):
    _, publication_id = _issue(db, "magazine", number=1)
    second, _ = _issue(db, "magazine", number=2, publication_id=publication_id)
    periodical_records.link_issue(
        db, item_id=second, publication_id=publication_id, issue_number="2", volume="7",
        google_volume_id="second-exact",
    )
    db.commit()
    before = _snapshot(db)
    monkeypatch.setattr(magazines.magazine_google, "lookup_issue", AsyncMock(
        return_value=provider_result.found("google", {
            "google_volume_id": "second-exact", "title": "Surface Monthly",
        }),
    ))
    response = editor_client.post("/api/magazines/add", data={
        "google_volume_id": "second-exact", "issue_number": "1", "volume": "7",
    })
    assert response.status_code == 200
    assert "Issue evidence matches more than one catalogue item" in response.text
    assert _snapshot(db) == before


@pytest.mark.parametrize("trashed", [False, True])
def test_visible_manual_match_cannot_mask_hidden_exact_provider_collision(editor_client, db, monkeypatch, trashed):
    _, publication_id = _issue(db, "magazine", number=1)
    hidden_library = libraries.create_library(db, "Hidden exact identity")
    hidden, _ = _issue(db, "magazine", library_id=hidden_library["id"], number=2, publication_id=publication_id)
    periodical_records.link_issue(
        db, item_id=hidden, publication_id=publication_id, issue_number="2", volume="7",
        google_volume_id="hidden-exact",
    )
    if trashed:
        item_write.trash_item(db, hidden)
    db.commit()
    before = _snapshot(db)
    monkeypatch.setattr(magazines.magazine_google, "lookup_issue", AsyncMock(
        return_value=provider_result.found("google", {
            "google_volume_id": "hidden-exact", "title": "Surface Monthly",
        }),
    ))
    response = editor_client.post("/api/magazines/add", data={
        "google_volume_id": "hidden-exact", "issue_number": "1", "volume": "7",
    })
    assert response.status_code == 200
    assert "This issue identity is unavailable" in response.text
    assert f"/item/{hidden}" not in response.text
    assert 'data-scan-status="duplicate"' not in response.text
    assert _snapshot(db) == before


def test_carrier_plus_supplement_duplicate_does_not_create_an_empty_publication(editor_client, db):
    item, publication_id = _issue(db, "magazine")
    periodical_records.link_issue(
        db, item_id=item, publication_id=publication_id, volume="7", issue_number="1",
        barcode_ean="9770953616115", barcode_supplement="04",
    )
    db.commit()
    before = _snapshot(db)
    response = editor_client.post("/api/magazines/add", data={
        "title": "Different submitted publication", "issue_number": "55",
        "carrier_ean": "9770953616115", "barcode_supplement": "04",
    })
    assert response.status_code == 200
    assert 'data-scan-status="duplicate"' in response.text
    assert f'href="/item/{item}"' in response.text
    assert _snapshot(db) == before


def test_magazine_issue_link_failure_rolls_back_new_publication_item_and_copy(editor_client, db, monkeypatch):
    def fail_link(*args, **kwargs):
        raise ValueError("surface issue-link refusal")

    monkeypatch.setattr(periodical_records, "link_issue", fail_link)
    response = editor_client.post("/api/magazines/add", data={
        "title": "Surface Monthly", "issue_number": "1",
    })
    assert response.status_code == 200
    assert "surface issue-link refusal" in response.text
    for table in ("items", "item_copies", "periodical_publications", "periodical_issues"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["comic", "magazine"])
def test_bulk_inherits_only_live_visible_library_and_explicit_node_mapping(db, kind):
    actor = _user(db)
    selected = libraries.create_library(db, "Surface selected library")
    hidden_library = libraries.create_library(db, "Surface hidden library")
    libraries.set_membership(db, selected["id"], actor["id"], "editor")
    db.execute("INSERT INTO locations (id, name) VALUES (801, 'Surface flat')")
    db.execute("INSERT INTO location_nodes (id, name) VALUES (801, 'Unrelated equal integer')")
    db.execute("INSERT INTO location_nodes (id, name, legacy_location_id) VALUES (802, 'Mapped root', 801)")
    db.execute("INSERT INTO location_nodes (id, name, parent_id) VALUES (803, 'Precise shelf', 802)")
    source, publication_id = _issue(db, kind, library_id=selected["id"], location_id=801)
    primary = db.execute("SELECT id FROM copies_live WHERE item_id = ?", (source,)).fetchone()[0]
    item_copies.update_copy(db, primary, {"location_id": 803})
    removed_copy = item_copies.add_copy(db, source, {"location_id": 801})
    item_copies.trash_copy(db, removed_copy)
    hidden, _ = _issue(db, kind, library_id=hidden_library["id"], number=2, publication_id=publication_id)
    for _ in range(3):
        item_copies.add_copy(db, hidden, {"location_id": 801})
    deleted, _ = _issue(db, kind, library_id=selected["id"], number=3, publication_id=publication_id)
    item_write.trash_item(db, deleted)
    with _acting(db, actor):
        result = _add_range(db, kind, publication_id)
        rows = db.execute(
            "SELECT i.id, li.library_id, i.location_id, c.location_id AS node_id "
            "FROM items_live i JOIN library_items li ON li.item_id = i.id "
            "JOIN copies_live c ON c.item_id = i.id AND c.is_primary = 1 "
            "WHERE i.source = 'bulk_issue_range' ORDER BY i.id",
        ).fetchall()
    assert (result.created, result.skipped, result.first_issue, result.last_issue) == (2, 1, 1, 3)
    assert len(rows) == 2
    assert {(row["library_id"], row["location_id"], row["node_id"]) for row in rows} == {
        (selected["id"], 801, 803),
    }
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (deleted,)).fetchone()[0]
    assert db.execute("SELECT deleted_at FROM item_copies WHERE id = ?", (removed_copy,)).fetchone()[0]


@pytest.mark.parametrize("kind", ["comic", "magazine"])
def test_bulk_locks_and_preflights_every_source_before_first_insert(db, kind, monkeypatch):
    actor = _user(db)
    libraries.set_membership(db, 1, actor["id"], "editor")
    first, publication_id = _issue(db, kind, number=1)
    second, _ = _issue(db, kind, number=2, publication_id=publication_id)
    db.commit()
    seen = []
    authorize = bulk_issues.require_item_role
    insert = bulk_issues.insert_item

    def check_authority(db, item_id, role):
        assert db.in_transaction
        seen.append(item_id)
        return authorize(db, item_id, role)

    def check_insert(db, **fields):
        assert db.in_transaction
        assert set(seen) == {first, second}
        assert fields["library_id"] == 1
        return insert(db, **fields)

    monkeypatch.setattr(bulk_issues, "require_item_role", check_authority)
    monkeypatch.setattr(bulk_issues, "insert_item", check_insert)
    with _acting(db, actor):
        result = _add_range(db, kind, publication_id)
    assert (result.created, result.skipped) == (1, 2)
    assert set(seen) == {first, second}


@pytest.mark.parametrize("kind", ["comic", "magazine"])
def test_bulk_refuses_ambiguous_same_name_visible_libraries_before_writing(db, kind):
    actor = _user(db)
    library = libraries.create_library(db, "Another visible source")
    libraries.set_membership(db, 1, actor["id"], "editor")
    libraries.set_membership(db, library["id"], actor["id"], "editor")
    _, publication_id = _issue(db, kind)
    _issue(db, kind, library_id=library["id"], number=2, publication_id=publication_id)
    before = _snapshot(db)
    with _acting(db, actor), pytest.raises(ValueError, match="ambiguous across visible libraries"):
        _add_range(db, kind, publication_id)
    assert _snapshot(db) == before


@pytest.mark.parametrize("kind", ["comic", "magazine"])
@pytest.mark.parametrize("source_state", ["hidden", "trashed", "read-only"])
def test_bulk_cannot_derive_authority_from_global_metadata_or_inaccessible_sources(db, kind, source_state):
    actor = _user(db, role="editor")
    library = libraries.create_library(db, "No bulk authority")
    libraries.set_membership(db, 1, actor["id"], "editor")
    source, publication_id = _issue(db, kind, library_id=library["id"])
    if source_state != "hidden":
        libraries.set_membership(db, library["id"], actor["id"], "viewer" if source_state == "read-only" else "editor")
    if source_state == "trashed":
        item_write.trash_item(db, source)
    before = _snapshot(db)
    with _acting(db, actor):
        if source_state == "read-only":
            with pytest.raises(HTTPException) as exc:
                _add_range(db, kind, publication_id)
            assert exc.value.status_code == 403
        else:
            with pytest.raises(ValueError):
                _add_range(db, kind, publication_id)
    assert _snapshot(db) == before


@pytest.mark.parametrize("kind", ["comic", "magazine"])
def test_bulk_late_failure_rolls_back_entire_range_without_committing_caller_work(db, kind, monkeypatch):
    source, publication_id = _issue(db, kind)
    db.commit()
    item_write.update_item_fields(db, source, {"notes": "pending caller work"})
    before = _snapshot(db)
    insert = bulk_issues.insert_item
    calls = 0

    def fail_second(db, **fields):
        nonlocal calls
        assert db.in_transaction
        calls += 1
        item_id = insert(db, **fields)
        if calls == 2:
            raise ValueError("late surface failure")
        return item_id

    monkeypatch.setattr(bulk_issues, "insert_item", fail_second)
    with pytest.raises(ValueError, match="late surface failure"):
        _add_range(db, kind, publication_id, first=2, last=3)
    assert calls == 2
    assert db.in_transaction
    assert _snapshot(db) == before
    db.rollback()
    assert db.execute("SELECT notes FROM items WHERE id = ?", (source,)).fetchone()[0] is None


def test_publication_pages_omit_hidden_and_trashed_issues_and_copy_history(viewer_client, db):
    source, publication_id = _issue(db, "magazine")
    visible_copy = db.execute("SELECT id FROM copies_live WHERE item_id = ?", (source,)).fetchone()[0]
    removed = item_copies.add_copy(db, source)
    item_copies.trash_copy(db, removed)
    hidden_library = libraries.create_library(db, "Hidden publication issues")
    _issue(db, "magazine", library_id=hidden_library["id"], number=2, publication_id=publication_id)
    deleted, _ = _issue(db, "magazine", number=3, publication_id=publication_id)
    item_write.trash_item(db, deleted)
    db.commit()
    response = viewer_client.get(f"/magazines/publications/{publication_id}")
    assert response.status_code == 200
    assert [issue["item_id"] for issue in response.context["issues"]] == [source]
    assert response.context["physical_copy_count"] == 1
    assert response.context["potential_gaps"] == []
    assert response.context["can_edit_publication"] is False
    assert response.context["can_bulk_add"] is False
    assert db.execute("SELECT id FROM copies_live WHERE item_id = ?", (source,)).fetchone()[0] == visible_copy


def test_romm_pages_preserve_plural_legacy_visibility_quoted_links_and_paging(viewer_client, db, monkeypatch):
    monkeypatch.setattr(romm_catalog, "PER_PAGE", 2)
    alpha = item_write.insert_item(
        db, title="Alpha Surface", media_type="digital_game", romm_id="legacy /?&", romm_platform_id="p-one",
    )
    db.execute("INSERT INTO romm_records (romm_id, item_id, platform_id) VALUES (?, ?, ?)",
               ("sidecar /?&", alpha, "p-one"))
    for title in ("Beta Surface", "Gamma Surface"):
        item_write.insert_item(db, title=title, media_type="digital_game", romm_id=title, romm_platform_id="p-one")
    hidden_library = libraries.create_library(db, "Hidden RomM")
    item_write.insert_item(db, title="Hidden Surface Game", library_id=hidden_library["id"],
                           media_type="digital_game", romm_id="hidden", romm_platform_id="hidden-platform")
    deleted = item_write.insert_item(db, title="Trashed Surface Game", media_type="digital_game",
                                     romm_id="trashed", romm_platform_id="trashed-platform")
    item_write.trash_item(db, deleted)
    set_setting(db, "romm_url", "https://server.example")
    set_setting(db, "romm_public_url", "https://public.example/mount")
    db.commit()
    first = viewer_client.get("/romm/library")
    assert first.status_code == 200
    assert first.context["total"] == 4
    assert first.context["page_count"] == 2
    assert [game["romm_id"] for game in first.context["games"]] == ["legacy /?&", "sidecar /?&"]
    assert [game["romm_url"] for game in first.context["games"]] == [
        "https://public.example/mount/rom/legacy%20%2F%3F%26",
        "https://public.example/mount/rom/sidecar%20%2F%3F%26",
    ]
    assert {row["platform_id"] for row in first.context["platforms"]} == {"p-one"}
    second = viewer_client.get("/romm/library?page=2")
    assert [game["title"] for game in second.context["games"]] == ["Beta Surface", "Gamma Surface"]
    response = viewer_client.get("/romm/library", params={
        "q": "No results & /", "platform": "p&/?", "page": 999,
    }, follow_redirects=False)
    assert response.status_code == 303
    destination = urlsplit(response.headers["location"])
    assert destination.path == "/romm/library"
    assert parse_qs(destination.query) == {"q": ["No results & /"], "platform": ["p&/?"], "page": ["1"]}


@pytest.mark.parametrize("public", ["javascript:alert(1)", "https://user:password@example.test", "https://example.test/?q=secret"])
def test_romm_never_emits_unsafe_configured_browser_root(public):
    assert romm_catalog._browser_root("https://server.example", public) == ""


@pytest.mark.asyncio
async def test_romm_admin_account_token_retains_read_visibility_not_manage_capability(admin_user, db):
    library = libraries.create_library(db, "Token-visible RomM")
    item_write.insert_item(db, title="Token Surface Game", library_id=library["id"],
                           media_type="digital_game", romm_id="token-rom", romm_platform_id="token-platform")
    minted = api_tokens.create(db, admin_user["id"], "Surface read-only token")
    db.commit()
    principal = api_tokens.resolve(minted["token"])
    assert principal["role"] == "viewer"
    assert principal["account_role"] == "admin"
    with _acting(db, principal):
        # Rendering capability only: native HTTP bearer path restrictions remain
        # authoritative and are NOT bypassed by adding a new token route here.
        context = await romm_catalog.romm_library(_request(principal), q="", platform="", page=1)
    assert context["can_manage_romm"] is False
    assert [game["romm_id"] for game in context["games"]] == ["token-rom"]


def test_romm_index_is_the_only_explicit_viewer_alias(viewer_client):
    response = viewer_client.get("/romm", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/romm/library"


def test_bulk_limit_and_duplicate_counts_remain_add_only(db):
    _, publication_id = _issue(db, "magazine", number=1)
    before = _snapshot(db)
    with pytest.raises(ValueError, match="at most 500"):
        _add_range(db, "magazine", publication_id, first=1, last=501)
    assert _snapshot(db) == before
    result = _add_range(db, "magazine", publication_id, first=1, last=2)
    again = _add_range(db, "magazine", publication_id, first=1, last=2)
    assert (result.created, result.skipped) == (1, 1)
    assert (again.created, again.skipped) == (0, 2)
