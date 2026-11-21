"""Post-freeze HTTP regressions for the Shelf Fill review fixes.

DO NOT run while integration writers are active. These require the real central
schema, authenticated client, native item/copy writers and scoped views. Only
provider results and the explicitly named failure injection are patched.
"""

from html.parser import HTMLParser
from unittest.mock import AsyncMock

import pytest

from app import features
from app.routers import items_common, magazines, periodicals, shelf_fill
from app.services import item_write, provider_result, user_state


BARCODE = "977095361611504"


class _Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.select = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"action": attrs.get("action"), "fields": {}}
            self.forms.append(self.current)
        elif self.current is not None and tag == "input" and attrs.get("name"):
            self.current["fields"][attrs["name"]] = attrs.get("value", "")
        elif self.current is not None and tag == "select":
            self.select = attrs.get("name")
        elif self.current is not None and tag == "option" and self.select:
            if self.select not in self.current["fields"] or "selected" in attrs:
                self.current["fields"][self.select] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None
        elif tag == "select":
            self.select = None


def _shelf(db):
    # NODE 7041 deliberately collides with an unrelated FLAT id. The explicit
    # mapping is 7040 -> 7001, and precise child 7041 must never enter the flat
    # item seam. Use high IDs to leave normal fixture seed identities alone.
    db.execute("INSERT INTO locations(id,name) VALUES(7001,'Review legacy root'),(7041,'Wrong equal integer')")
    db.execute("INSERT INTO location_nodes(id,name,legacy_location_id) VALUES(7040,'Review root',7001)")
    db.execute("INSERT INTO location_nodes(id,name,parent_id) VALUES(7041,'Review precise shelf',7040)")
    db.commit()
    return 7041


def _continuation(node, origin="shelf_fill"):
    return {"location_node_id": node, "shelf_fill_origin": origin}


def test_committed_add_placement_refusal_retains_real_id_and_native_retry(editor_client, db, monkeypatch):
    node = _shelf(db)
    metadata = {"title": "Review saved item", "authors": "Reviewer"}
    monkeypatch.setattr(items_common, "_lookup_metadata", AsyncMock(return_value=(
        metadata, "openlibrary", {}, provider_result.found("openlibrary", metadata),
    )))
    monkeypatch.setattr(shelf_fill.items.cover_queue, "enqueue", lambda *args, **kwargs: None)
    def fail_place(*args, **kwargs):
        raise ValueError("review placement refused")

    monkeypatch.setattr(shelf_fill, "_place_item", fail_place)
    response = editor_client.post("/api/shelf-fill/scan", headers={"HX-Request": "true"}, data={
        "isbn": "9780306406157", "media_type": "book", **_continuation(node),
    })
    assert response.status_code == 200
    item = db.execute("SELECT id FROM items WHERE title='Review saved item'").fetchone()
    assert item is not None, "the inner Add really committed; rollback cannot be claimed here"
    assert response.context["status"] == "saved_unplaced"
    assert response.context["item_id"] == item["id"]
    assert response.context["location_node_id"] == node
    form = next(f for f in _Forms(response.text).forms if f["action"] == "/api/shelf-fill/place")
    assert form["fields"]["item_id"] == str(item["id"])
    assert form["fields"]["location_node_id"] == str(node)
    assert form["fields"]["_csrf"] == editor_client.cookies.get("csrf_token")
    assert "isbn" not in form["fields"], "retry must place by actual ID, never re-Add"
    assert "shelf-fill-position" not in response.text
    retry = editor_client.post(form["action"], headers={"HX-Request": "true"}, data=form["fields"])
    assert retry.context["status"] == "saved_unplaced"
    assert retry.context["item_id"] == item["id"], "a failed native/HTMX retry still retains the saved ID"
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_no_insert_refusal_is_not_saved_unplaced_and_never_places(editor_client, db, monkeypatch):
    node = _shelf(db)
    calls = []
    monkeypatch.setattr(shelf_fill, "_place_item", lambda *args, **kwargs: calls.append(args))
    response = editor_client.post("/api/shelf-fill/scan", headers={"HX-Request": "true"}, data={
        "isbn": "9780306406158", "media_type": "book", **_continuation(node),
    })
    assert response.status_code == 200
    assert 'data-scan-status="error"' in response.text
    assert "data-saved-item-id" not in response.text
    assert not calls
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


@pytest.mark.parametrize("origin,template", [("scan", "scan.html"), ("shelf_fill", "shelf_fill.html")])
@pytest.mark.parametrize("existing", [False, True])
def test_native_periodical_confirmation_places_new_or_existing_at_exact_node(
    editor_client, db, origin, template, existing,
):
    node = _shelf(db)
    data = {"raw_barcode": BARCODE, "publication_title": "Review Monthly", "issue_number": "4",
            "location_id": 7041, "_csrf": editor_client.cookies.get("csrf_token"), **_continuation(node, origin)}
    if existing:
        # Ordinary confirmation keeps its pre-existing redirect contract.
        first = editor_client.post("/api/periodicals/confirm", data={
            k: v for k, v in data.items() if k not in {"location_node_id", "shelf_fill_origin"}
        }, follow_redirects=False)
        assert first.status_code == 303
        db.execute("UPDATE item_copies SET condition='handwritten grade', notes='keep this text'")
        db.commit()
    # No JS/HTMX CSRF header: only the rendered native form token authorizes.
    response = editor_client.post("/api/periodicals/confirm", headers={"X-CSRF-Token": ""},
                                  data=data, follow_redirects=False)
    assert response.status_code == 200
    assert response.template.name == template
    assert response.context["initial_shelf_node"] == node
    assert f'data-shelf-fill-placed-node="{node}"' in response.text
    assert 'data-testid="shelf-fill-position"' in response.text
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    item = db.execute("SELECT id, location_id FROM items").fetchone()
    assert item["location_id"] == 7001, "NODE integer must not enter the FLAT item seam"
    copy = db.execute("SELECT location_id, position_order FROM copies_live WHERE item_id=?", (item["id"],)).fetchone()
    assert tuple(copy) == (node, 1)
    if existing:
        assert tuple(db.execute("SELECT condition,notes FROM copies_live").fetchone()) == (
            "handwritten grade", "keep this text",
        )


@pytest.mark.parametrize("route,data", [
    ("/api/periodicals/confirm", {"raw_barcode": BARCODE, "publication_title": "Review Monthly", "issue_number": "4"}),
    ("/api/magazines/add", {"title": "Review Monthly", "issue_number": "4", "carrier_ean": BARCODE[:13],
                            "barcode_supplement": BARCODE[13:]}),
])
def test_confirmation_late_placement_refusal_rolls_back_issue_publication_item_and_copy(
    editor_client, db, monkeypatch, route, data,
):
    node = _shelf(db)
    before = tuple(db.iterdump())
    real_place = shelf_fill._place_item

    def fail_after_physical_write(db, *args, **kwargs):
        assert db.in_transaction
        real_place(db, *args, **kwargs)
        raise ValueError("review late placement refusal")

    monkeypatch.setattr(shelf_fill, "_place_item", fail_after_physical_write)
    response = editor_client.post(route, headers={"HX-Request": "true"}, data={
        **data, **_continuation(node),
    })
    assert response.status_code == 200
    assert 'data-scan-status="error"' in response.text
    assert "data-saved-item-id" not in response.text, "all writes rolled back; nothing was accepted"
    assert tuple(db.iterdump()) == before


def test_google_magazine_selection_and_confirmation_keep_defaults_and_place(editor_client, editor_user, db, monkeypatch):
    node = _shelf(db)
    issue = {"google_volume_id": "review-exact", "title": "Review Monthly", "issue_date": "2024-05",
             "publisher": "Review Publisher", "description": "Issue description", "language": "en", "page_count": 99}
    monkeypatch.setattr(magazines.magazine_google, "lookup_issue", AsyncMock(
        return_value=provider_result.found("google", issue),
    ))
    selected = editor_client.get("/api/magazines/select-search-result", params={
        "google_volume_id": "review-exact", "carrier_ean": BARCODE[:13], "barcode_supplement": BARCODE[13:],
        "library_id": 1, "tags": "Review tag", "reading_status": "read", "location_id": 7001,
        **_continuation(node, "scan"),
    })
    assert selected.status_code == 200
    forms = _Forms(selected.text).forms
    search, confirm = forms
    for form in (search, confirm):
        assert form["fields"]["library_id"] == "1"
        assert form["fields"]["tags"] == "Review tag"
        assert form["fields"]["reading_status"] == "read"
        assert form["fields"]["location_node_id"] == str(node)
        assert form["fields"]["shelf_fill_origin"] == "scan"
        assert form["fields"]["barcode_supplement"] == "04"
        assert form["fields"]["location_id"] == "7001"
    response = editor_client.post(confirm["action"], headers={"HX-Request": "true"}, data=confirm["fields"])
    assert response.status_code == 200
    assert f'data-shelf-fill-placed-node="{node}"' in response.text
    item = db.execute("SELECT * FROM items").fetchone()
    assert (item["title"], item["description"], item["language"], item["page_count"], item["location_id"]) == (
        "Review Monthly", "Issue description", "en", 99, 7001,
    )
    assert user_state.get_state(db, editor_user["id"], item["id"])["reading_status"] == "read"
    copy = db.execute("SELECT location_id,position_order FROM copies_live WHERE item_id=?", (item["id"],)).fetchone()
    assert tuple(copy) == (node, 1)


def test_native_periodical_assisted_search_select_and_confirm_keep_the_original_node(editor_client, db, monkeypatch):
    node = _shelf(db)
    issue = {"google_volume_id": "review-periodical", "title": "Review Monthly", "issue_date": "2024-05-01"}
    monkeypatch.setattr(periodicals.periodical_google, "search_issues", AsyncMock(
        return_value=provider_result.found("google", [issue]),
    ))
    monkeypatch.setattr(periodicals.periodical_google, "lookup_issue", AsyncMock(
        return_value=provider_result.found("google", issue),
    ))
    results = editor_client.get("/api/periodicals/assist/search", params={
        "q": "Review Monthly", "raw_barcode": BARCODE, "location_id": 7001,
        "library_id": 1, "tags": "Review tag", "reading_status": "read", **_continuation(node, "scan"),
    })
    assert results.status_code == 200
    select = next(f for f in _Forms(results.text).forms if f["action"] == "/api/periodicals/assist/select")
    selected = editor_client.get(select["action"], params=select["fields"])
    assert selected.status_code == 200
    confirm = next(f for f in _Forms(selected.text).forms if f["action"] == "/api/periodicals/confirm")
    assert confirm["fields"]["location_id"] == "7001"
    assert confirm["fields"]["location_node_id"] == str(node)
    assert confirm["fields"]["shelf_fill_origin"] == "scan"
    assert confirm["fields"]["tags"] == "Review tag"
    confirm["fields"]["issue_number"] = "4"
    response = editor_client.post(confirm["action"], headers={"X-CSRF-Token": ""},
                                  data=confirm["fields"], follow_redirects=False)
    assert response.status_code == 200
    assert response.template.name == "scan.html"
    assert tuple(db.execute("SELECT location_id,position_order FROM copies_live").fetchone()) == (node, 1)


def test_native_restore_continuation_renders_real_csrf(editor_client, db):
    node = _shelf(db)
    item_id = item_write.insert_item(db, title="Review Trash", media_type="book", isbn="9780306406157")
    item_write.trash_item(db, item_id)
    db.commit()
    response = editor_client.post("/api/shelf-fill/scan", headers={"HX-Request": "true"}, data={
        "isbn": "9780306406157", **_continuation(node, "scan"),
    })
    assert 'data-scan-status="in_trash"' in response.text
    form = next(f for f in _Forms(response.text).forms if f["action"] == f"/api/trash/items/{item_id}/restore")
    assert form["fields"]["_csrf"] == editor_client.cookies.get("csrf_token")
    assert form["fields"]["location_node_id"] == str(node)
    assert form["fields"]["shelf_fill_origin"] == "scan"


@pytest.mark.parametrize("route,data", [
    ("/api/periodicals/confirm", {"raw_barcode": BARCODE, "publication_title": "Review Monthly", "issue_number": "4"}),
    ("/api/magazines/add", {"title": "Review Monthly", "issue_number": "4"}),
])
@pytest.mark.parametrize("refusal", ["feature_off", "invalid_origin"])
def test_continuations_refuse_before_any_provider_or_write(editor_client, db, monkeypatch, route, data, refusal):
    node = _shelf(db)
    lookup = AsyncMock(side_effect=AssertionError("refused continuation contacted provider"))
    monkeypatch.setattr(magazines.magazine_google, "lookup_issue", lookup)
    origin = "https://untrusted.invalid" if refusal == "invalid_origin" else "scan"
    if refusal == "feature_off":
        features.set_feature_enabled(db, "shelf_fill", False)
        db.commit()
    before = tuple(db.iterdump())
    response = editor_client.post(route, headers={"HX-Request": "true"}, data={
        **data, **_continuation(node, origin),
    })
    assert response.status_code == (403 if refusal == "feature_off" else 200)
    assert "data-saved-item-id" not in response.text
    assert tuple(db.iterdump()) == before
    lookup.assert_not_awaited()
