from html import unescape
from html.parser import HTMLParser
from unittest.mock import AsyncMock

import pytest

from app import features
from app.services import (
    crossref_journals,
    googlebooks,
    issn_portal,
    libraries,
    lists,
    provider_result,
    upcitemdb,
    user_state,
)


MAGAZINE_EAN = "9770161737008"  # Popular Science / ISSN 0161-7370
VW_MOTORING_EAN = "9770953616115"  # VW motoring / ISSN 0953-6167


def _confirmation_fields(html):
    """Read the real native submit form, never obsolete hidden compatibility fields."""
    class ConfirmationForm(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active = False
            self.forms = 0
            self.fields = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                self.active = attrs.get("action") == "/api/periodicals/confirm" and attrs.get("method") == "post"
                self.forms += int(self.active)
            elif self.active and tag == "input" and attrs.get("name"):
                self.fields[attrs["name"]] = attrs.get("value", "")

        def handle_endtag(self, tag):
            if tag == "form":
                self.active = False

    parser = ConfirmationForm()
    parser.feed(html)
    assert parser.forms == 1
    return parser.fields


def _html_form(html, action, method):
    """Read successful controls from one real form, including selected options."""
    class Form(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active = False
            self.forms = []
            self.fields = {}
            self.controls = {}
            self.options = {}
            self.select = None
            self.ids = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get("id"):
                self.ids.append(attrs["id"])
            if tag == "form":
                self.active = attrs.get("action") == action and attrs.get("method") == method
                if self.active:
                    self.forms.append(attrs)
            elif self.active and tag in {"input", "select"} and attrs.get("name"):
                name = attrs["name"]
                self.controls[name] = attrs
                if tag == "input":
                    self.fields[name] = attrs.get("value", "")
                else:
                    self.select = name
                    self.options[name] = []
            elif self.active and tag == "option" and self.select:
                self.options[self.select].append(attrs.get("value", ""))
                if self.select not in self.fields or "selected" in attrs:
                    self.fields[self.select] = attrs.get("value", "")

        def handle_endtag(self, tag):
            if tag == "form":
                self.active = False
            elif tag == "select":
                self.select = None

    parser = Form()
    parser.feed(html)
    assert len(parser.forms) == 1
    return {"attrs": parser.forms[0], "fields": parser.fields, "controls": parser.controls,
            "options": parser.options, "ids": parser.ids}


def _legacy_confirmation_fields(html):
    return _html_form(html, "/api/magazines/add", "post")["fields"]


def _catalogue_snapshot(db):
    tables = ("items", "library_items", "item_copies", "periodical_publications", "periodical_issues",
              "user_item_state", "user_reading_log", "reading_log", "list_items", "item_tags", "tags")
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in tables}


@pytest.fixture(autouse=True)
def serial_catalogue_misses(monkeypatch):
    """Keep scan tests deterministic unless a test explicitly needs a provider."""
    stubs = {}
    for module, method, provider in (
        (googlebooks, "lookup_magazine_by_issn", "google"),
        (issn_portal, "lookup", "issn_portal"),
        (crossref_journals, "lookup", "crossref"),
        (upcitemdb, "lookup", "upcitemdb"),
    ):
        stub = AsyncMock(return_value=provider_result.no_match(provider))
        monkeypatch.setattr(module, method, stub)
        stubs[provider] = stub
    return stubs


@pytest.fixture
def google_magazine_hit(monkeypatch):
    async def _lookup(issn, client, *, api_key=None):
        assert issn == "0161-7370"
        return provider_result.found("google", {
            "title": "Popular Science",
            "publisher": "Bonnier Corporation",
            "description": "Science and technology magazine.",
            "issn": issn,
            "series_name": "Popular Science",
            "language": "en",
        })

    lookup = AsyncMock(side_effect=_lookup)
    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", lookup)
    return lookup


def test_auto_scan_identifies_publication_but_does_not_invent_issue(
    editor_client, db, monkeypatch, google_magazine_hit, serial_catalogue_misses
):
    async def _upc_must_not_run(upc, client):
        raise AssertionError("UPC Item DB should only be a fallback after a magazine miss")

    monkeypatch.setattr(upcitemdb, "lookup", _upc_must_not_run)

    resp = editor_client.get(
        "/api/magazines/assist/scan", params={"raw_barcode": MAGAZINE_EAN}
    )

    assert resp.status_code == 200
    assert "Popular Science" in resp.text
    assert "ISSN 0161-7370" in resp.text
    assert "does not assume the carrier uniquely identifies a publication or issue" in resp.text
    assert 'name="issue_number"' in resp.text
    assert 'name="issue_date"' in resp.text
    fields = _legacy_confirmation_fields(resp.text)
    assert fields["title"] == "Popular Science"
    assert fields["issn"] == "0161-7370"
    assert fields["carrier_ean"] == MAGAZINE_EAN
    assert (fields["issue_number"], fields["issue_date"]) == ("", "")
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0
    google_magazine_hit.assert_awaited_once()
    serial_catalogue_misses["issn_portal"].assert_not_awaited()
    serial_catalogue_misses["crossref"].assert_not_awaited()


def test_977_overrides_wrong_media_hint_without_creating_wrong_item(
    editor_client, db, google_magazine_hit, serial_catalogue_misses
):
    serial_catalogue_misses["issn_portal"].return_value = provider_result.found("issn_portal", {
        "title": "Popular Science", "issn": "0161-7370",
    })
    resp = editor_client.post(
        "/api/scan", data={"isbn": MAGAZINE_EAN, "media_type": "dvd"}
    )

    assert resp.status_code == 200
    assert "Popular Science" in resp.text
    assert "0161-7370" in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0
    fields = _confirmation_fields(resp.text)
    assert (fields["raw_barcode"], fields["publication_title"]) == (MAGAZINE_EAN, "Popular Science")
    serial_catalogue_misses["issn_portal"].assert_awaited_once()
    google_magazine_hit.assert_not_awaited()
    serial_catalogue_misses["crossref"].assert_not_awaited()
    serial_catalogue_misses["upcitemdb"].assert_not_awaited()


def test_issn_portal_identifies_vw_motoring_when_google_misses(
    editor_client, db, monkeypatch, serial_catalogue_misses
):
    async def _google_miss(issn, client, *, api_key=None):
        assert issn == "0953-6167"
        return provider_result.no_match("google")

    async def _issn_hit(issn, client):
        assert issn == "0953-6167"
        return provider_result.found("issn_portal", {
            "title": "VW motoring",
            "publisher": None,
            "description": None,
            "issn": issn,
            "series_name": "VW motoring",
            "language": None,
        })

    async def _upc_must_not_run(upc, client):
        raise AssertionError("Retail lookup must not run after an authoritative ISSN hit")

    serial_catalogue_misses["google"].side_effect = _google_miss
    serial_catalogue_misses["issn_portal"].side_effect = _issn_hit
    monkeypatch.setattr(upcitemdb, "lookup", _upc_must_not_run)

    resp = editor_client.get(
        "/api/magazines/assist/scan", params={"raw_barcode": VW_MOTORING_EAN}
    )

    assert resp.status_code == 200
    assert "VW motoring" in resp.text
    assert "ISSN 0953-6167" in resp.text
    fields = _legacy_confirmation_fields(resp.text)
    assert (fields["title"], fields["issn"], fields["carrier_ean"]) == (
        "VW motoring", "0953-6167", VW_MOTORING_EAN,
    )
    assert 'name="issue_number"' in resp.text
    assert 'name="issue_date"' in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0
    serial_catalogue_misses["google"].assert_awaited_once()
    serial_catalogue_misses["issn_portal"].assert_awaited_once()
    serial_catalogue_misses["crossref"].assert_not_awaited()


def test_crossref_identifies_scholarly_serial_before_retail_fallback(
    editor_client, db, monkeypatch, serial_catalogue_misses
):
    async def _google_miss(issn, client, *, api_key=None):
        return provider_result.no_match("google")

    async def _crossref_hit(issn, client):
        assert issn == "0161-7370"
        return provider_result.found("crossref", {
            "title": "Example Research Journal",
            "publisher": "Example University Press",
            "description": None,
            "issn": issn,
            "series_name": "Example Research Journal",
            "language": None,
        })

    async def _upc_must_not_run(upc, client):
        raise AssertionError("Retail lookup must not run after a Crossref ISSN hit")

    serial_catalogue_misses["google"].side_effect = _google_miss
    serial_catalogue_misses["crossref"].side_effect = _crossref_hit
    monkeypatch.setattr(upcitemdb, "lookup", _upc_must_not_run)

    resp = editor_client.get(
        "/api/magazines/assist/scan", params={"raw_barcode": MAGAZINE_EAN}
    )

    assert resp.status_code == 200
    fields = _legacy_confirmation_fields(resp.text)
    assert fields["title"] == "Example Research Journal"
    assert fields["publisher"] == "Example University Press"
    assert "ISSN 0161-7370" in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0
    for provider in ("google", "issn_portal", "crossref"):
        serial_catalogue_misses[provider].assert_awaited_once()


def test_google_miss_falls_back_to_retail_title_for_issue_form(
    editor_client, db, monkeypatch, serial_catalogue_misses
):
    async def _google_miss(issn, client, *, api_key=None):
        return provider_result.no_match("google")

    async def _upc_hit(upc, client):
        assert upc == MAGAZINE_EAN
        return provider_result.found("upcitemdb", {
            "title": "Popular Science Magazine",
            "category": "Magazines",
            "brand": "Bonnier",
            "images": [],
        })

    serial_catalogue_misses["google"].side_effect = _google_miss
    serial_catalogue_misses["upcitemdb"].side_effect = _upc_hit

    resp = editor_client.get(
        "/api/magazines/assist/scan", params={"raw_barcode": MAGAZINE_EAN}
    )

    assert resp.status_code == 200
    fields = _legacy_confirmation_fields(resp.text)
    assert fields["title"] == "Popular Science Magazine"
    assert fields["publisher"] == "Bonnier"
    assert fields["issn"] == ""
    assert "Barcode ISSN hint 0161-7370" in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0
    for lookup in serial_catalogue_misses.values():
        lookup.assert_awaited_once()


@pytest.mark.parametrize("generic_title", [
    "Magazine",
    "magazines",
    "Periodical",
    "Journal",
    "Newspaper",
    "Publication",
    "Serial",
])
def test_generic_retail_category_is_not_accepted_as_publication_title(
    editor_client, db, monkeypatch, generic_title
):
    async def _google_miss(issn, client, *, api_key=None):
        return provider_result.no_match("google")

    async def _upc_hit(upc, client):
        return provider_result.found("upcitemdb", {
            "title": generic_title,
            "category": "Magazines",
            "brand": None,
            "images": [],
        })

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _google_miss)
    monkeypatch.setattr(upcitemdb, "lookup", _upc_hit)

    resp = editor_client.get(
        "/api/magazines/assist/scan", params={"raw_barcode": MAGAZINE_EAN}
    )

    assert resp.status_code == 200
    fields = _legacy_confirmation_fields(resp.text)
    assert fields["title"] == ""
    assert f'value="{generic_title}"' not in resp.text
    assert fields["issn"] == ""
    assert "Barcode ISSN hint 0161-7370" in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0


def test_unknown_977_still_uses_magazine_issue_form(editor_client, db, monkeypatch):
    async def _google_miss(issn, client, *, api_key=None):
        return provider_result.no_match("google")

    async def _upc_miss(upc, client):
        return provider_result.no_match("upcitemdb")

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _google_miss)
    monkeypatch.setattr(upcitemdb, "lookup", _upc_miss)

    resp = editor_client.get(
        "/api/magazines/assist/scan", params={"raw_barcode": MAGAZINE_EAN}
    )

    assert resp.status_code == 200
    assert "Barcode ISSN hint 0161-7370" in resp.text
    fields = _legacy_confirmation_fields(resp.text)
    assert fields["carrier_ean"] == MAGAZINE_EAN
    assert fields["title"] == ""
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0


def test_two_issues_can_share_same_977_carrier(editor_client, db):
    first = editor_client.post(
        "/api/magazines/add",
        data={
            "title": "Popular Science",
            "issn": "0161-7370",
            "carrier_ean": MAGAZINE_EAN,
            "issue_number": "1",
            "issue_date": "2026-01-01",
        },
    )
    second = editor_client.post(
        "/api/magazines/add",
        data={
            "title": "Popular Science",
            "issn": "0161-7370",
            "carrier_ean": MAGAZINE_EAN,
            "issue_number": "2",
            "issue_date": "2026-02-01",
        },
    )

    assert first.status_code == 200 and "Issue 1" in first.text
    assert second.status_code == 200 and "Issue 2" in second.text
    items = db.execute(
        "SELECT id, media_type, upc FROM items ORDER BY id"
    ).fetchall()
    assert len(items) == 2
    assert all(row["media_type"] == "magazine" for row in items)
    assert all(row["upc"] is None for row in items)
    assert db.execute(
        "SELECT COUNT(*) AS c FROM periodical_publications"
    ).fetchone()["c"] == 1
    issues = db.execute(
        "SELECT issue_number, barcode_ean FROM periodical_issues ORDER BY issue_number"
    ).fetchall()
    assert [(row["issue_number"], row["barcode_ean"]) for row in issues] == [
        ("1", MAGAZINE_EAN),
        ("2", MAGAZINE_EAN),
    ]


def test_concatenated_supplement_is_preserved_for_issue(editor_client, db, monkeypatch):
    async def _google_miss(issn, client, *, api_key=None):
        return provider_result.no_match("google")

    async def _upc_miss(upc, client):
        return provider_result.no_match("upcitemdb")

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _google_miss)
    monkeypatch.setattr(upcitemdb, "lookup", _upc_miss)

    scanned = editor_client.get(
        "/api/magazines/assist/scan",
        params={"raw_barcode": MAGAZINE_EAN + "05"},
    )
    assert scanned.status_code == 200
    assert "Barcode ISSN hint 0161-7370 · add-on 05" in unescape(scanned.text)
    fields = _legacy_confirmation_fields(scanned.text)
    assert fields["carrier_ean"] == MAGAZINE_EAN
    assert fields["barcode_supplement"] == "05"
    assert (fields["issue_number"], fields["issue_date"]) == ("", "")
    fields.update(title="Popular Science", issn="0161-7370", issue_number="5")

    added = editor_client.post(
        "/api/magazines/add", data=fields, follow_redirects=False,
    )
    assert added.status_code == 200
    row = db.execute(
        "SELECT barcode_ean, barcode_supplement FROM periodical_issues"
    ).fetchone()
    assert row["barcode_ean"] == MAGAZINE_EAN
    assert row["barcode_supplement"] == "05"


@pytest.mark.parametrize("mode", ["add", "wishlist"])
@pytest.mark.parametrize("global_role", ["editor", "viewer"])
def test_magazine_page_barcode_form_reaches_google_hit_and_real_confirmation(
    editor_client, editor_user, viewer_user, db, google_magazine_hit,
    serial_catalogue_misses, mode, global_role,
):
    # A library editor can have a global viewer role; a global editor is not
    # implicitly allowed to spend credentials for another library.
    db.execute("UPDATE users SET role = ? WHERE id = ?", (global_role, editor_user["id"]))
    target = libraries.create_library(db, "Barcode destination")
    hidden = libraries.create_library(db, "Hidden barcode destination")
    libraries.remove_membership(db, libraries.DEFAULT_LIBRARY_ID, editor_user["id"])
    libraries.set_membership(db, target["id"], editor_user["id"], "editor")
    libraries.set_membership(db, target["id"], viewer_user["id"], "viewer")
    db.execute("INSERT INTO locations (id, name) VALUES (701, 'Barcode flat')")
    db.execute("INSERT INTO location_nodes (id, name) VALUES (701, 'Unrelated equal integer')")
    db.execute("INSERT INTO location_nodes (id, name, legacy_location_id) VALUES (702, 'Mapped barcode root', 701)")
    db.commit()
    before = _catalogue_snapshot(db)

    page = editor_client.get("/magazines")
    assert page.status_code == 200
    form = _html_form(page.text, "/api/magazines/assist/scan", "get")
    assert form["attrs"]["hx-get"] == form["attrs"]["action"]
    assert form["attrs"]["hx-target"] == "#magazine-barcode-result"
    assert form["attrs"]["hx-swap"] == "innerHTML"
    assert form["ids"].count("magazine-barcode-result") == 1
    assert "required" in form["controls"]["raw_barcode"]
    assert form["controls"]["raw_barcode"]["maxlength"] == "32"
    assert "required" in form["controls"]["library_id"]
    assert form["options"]["library_id"] == [str(target["id"])]
    assert str(hidden["id"]) not in form["options"]["library_id"]
    assert form["options"]["mode"] == ["add", "wishlist"]
    assert form["options"]["location_id"] == ["", "701"]
    params = {**form["fields"], "raw_barcode": MAGAZINE_EAN + "05", "mode": mode,
              "location_id": "701", "tags": "barcode-tag", "reading_status": "read"}
    scanned = editor_client.get(form["attrs"]["action"], params=params, headers={"HX-Request": "true"})
    assert scanned.status_code == 200
    assert scanned.template.name == "fragments/magazine_issue_form.html"
    confirmation = _html_form(scanned.text, "/api/magazines/add", "post")
    assert confirmation["attrs"]["hx-post"] == "/api/magazines/add"
    assert confirmation["attrs"]["hx-target"] == "closest .scan-result"
    assert confirmation["attrs"]["hx-swap"] == "outerHTML"
    fields = confirmation["fields"]
    assert fields["_csrf"]
    assert {key: fields[key] for key in (
        "title", "issn", "carrier_ean", "barcode_supplement", "issue_number", "issue_date",
        "google_volume_id", "mode", "library_id", "location_id", "tags", "reading_status",
        "location_node_id", "shelf_fill_origin",
    )} == {
        "title": "Popular Science", "issn": "0161-7370", "carrier_ean": MAGAZINE_EAN,
        "barcode_supplement": "05", "issue_number": "", "issue_date": "", "google_volume_id": "",
        "mode": mode, "library_id": str(target["id"]), "location_id": "701", "tags": "barcode-tag",
        "reading_status": "read", "location_node_id": "", "shelf_fill_origin": "",
    }
    assert _catalogue_snapshot(db) == before
    google_magazine_hit.assert_awaited_once()
    assert google_magazine_hit.await_args.args[0] == "0161-7370"
    for provider in ("issn_portal", "crossref", "upcitemdb"):
        serial_catalogue_misses[provider].assert_not_awaited()

    fields.update(volume="154", issue_number="8", issue_date="2026-04-01")
    saved = editor_client.post(confirmation["attrs"]["action"], data=fields)
    assert saved.status_code == 200
    assert f'data-scan-status="{"added" if mode == "add" else "wishlisted"}"' in saved.text
    db.commit()
    item = db.execute("SELECT * FROM items").fetchone()
    assert (item["title"], item["media_type"], item["publisher"], item["language"], item["description"]) == (
        "Popular Science", "magazine", "Bonnier Corporation", "en", "Science and technology magazine.",
    )
    assert (item["upc"], item["owned"], item["location_id"], item["reading_status"]) == (
        None, int(mode == "add"), 701, None,
    )
    assert libraries.item_library_id(db, item["id"]) == target["id"]
    publication = db.execute("SELECT * FROM periodical_publications").fetchone()
    assert (publication["title"], publication["issn"]) == ("Popular Science", "0161-7370")
    issue = db.execute("SELECT * FROM periodical_issues").fetchone()
    assert (issue["item_id"], issue["publication_id"], issue["volume"], issue["issue_number"],
            issue["issue_date"], issue["barcode_ean"], issue["barcode_supplement"]) == (
        item["id"], publication["id"], "154", "8", "2026-04-01", MAGAZINE_EAN, "05",
    )
    copies = db.execute("SELECT location_id FROM item_copies WHERE item_id = ?", (item["id"],)).fetchall()
    assert [row["location_id"] for row in copies] == ([702] if mode == "add" else [])
    assert lists.is_member(db, lists.WISHLIST, item["id"]) == (mode == "wishlist")
    assert [row[0] for row in db.execute(
        "SELECT t.name FROM tags t JOIN item_tags it ON it.tag_id = t.id WHERE it.item_id = ?", (item["id"],),
    )] == ["barcode-tag"]
    assert user_state.get_state(db, editor_user["id"], item["id"])["reading_status"] == "read"
    assert user_state.get_state(db, editor_user["id"], item["id"])["wishlist"] == 0
    other = user_state.get_state(db, viewer_user["id"], item["id"])
    assert (other["reading_status"], other["wishlist"], other["persisted"]) == (None, 0, False)
    google_magazine_hit.assert_awaited_once()  # Confirmation is not another provider lookup.


def test_magazine_barcode_form_is_absent_for_read_only_viewer(viewer_client, db):
    before = _catalogue_snapshot(db)
    page = viewer_client.get("/magazines")
    assert page.status_code == 200
    assert 'action="/api/magazines/assist/scan"' not in page.text
    assert _catalogue_snapshot(db) == before


@pytest.mark.parametrize("params", [
    {"raw_barcode": "not a barcode"}, {"raw_barcode": "9770161737009"},
    {"raw_barcode": "978016173700805"}, {"mode": "lookup"}, {"mode": "inventory"},
    {"location_id": "not-an-id"}, {"location_id": 703}, {"location_id": 888}, {"location_id": 2**70},
    {"location_node_id": 888, "shelf_fill_origin": "scan"},
    {"location_node_id": 2**70, "shelf_fill_origin": "scan"},
    {"location_node_id": 703}, {"shelf_fill_origin": "shelf_fill"},
    {"location_node_id": 703, "shelf_fill_origin": "https://example.test"},
    {"location_node_id": 703, "shelf_fill_origin": "scan", "mode": "wishlist"},
    {"location_node_id": 703, "shelf_fill_origin": "scan", "location_id": 888},
])
def test_legacy_barcode_invalid_context_is_visible_before_all_providers(
    editor_client, db, serial_catalogue_misses, params,
):
    db.execute("INSERT INTO location_nodes (id, name) VALUES (703, 'Node only; not a flat location')")
    db.commit()
    before = _catalogue_snapshot(db)
    response = editor_client.get("/api/magazines/assist/scan", params={"raw_barcode": MAGAZINE_EAN, **params},
                                 headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert 'data-scan-status="error"' in response.text
    for lookup in serial_catalogue_misses.values():
        lookup.assert_not_awaited()
    assert _catalogue_snapshot(db) == before
    assert db.execute("SELECT COUNT(*) FROM scan_log").fetchone()[0] == 0


@pytest.mark.parametrize("params", [{}, {"raw_barcode": "9" * 33}])
def test_legacy_barcode_query_requires_bounded_raw_input(editor_client, db, serial_catalogue_misses, params):
    before = _catalogue_snapshot(db)
    response = editor_client.get("/api/magazines/assist/scan", params=params)
    assert response.status_code == 422
    for lookup in serial_catalogue_misses.values():
        lookup.assert_not_awaited()
    assert _catalogue_snapshot(db) == before


@pytest.mark.parametrize("membership,status", [(None, 404), ("viewer", 403)])
def test_legacy_barcode_requires_editor_in_requested_library_before_lookup(
    editor_client, editor_user, db, serial_catalogue_misses, membership, status,
):
    target = libraries.create_library(db, "No barcode authority")
    if membership:
        libraries.set_membership(db, target["id"], editor_user["id"], membership)
    db.commit()
    before = _catalogue_snapshot(db)
    response = editor_client.get("/api/magazines/assist/scan", params={
        "raw_barcode": MAGAZINE_EAN, "library_id": target["id"],
    })
    assert response.status_code == status
    for lookup in serial_catalogue_misses.values():
        lookup.assert_not_awaited()
    assert _catalogue_snapshot(db) == before


def test_legacy_barcode_remains_optional_and_refuses_read_only_viewer(
    viewer_client, db, serial_catalogue_misses,
):
    before = _catalogue_snapshot(db)
    response = viewer_client.get("/api/magazines/assist/scan", params={"raw_barcode": MAGAZINE_EAN})
    assert response.status_code == 403
    for lookup in serial_catalogue_misses.values():
        lookup.assert_not_awaited()
    assert _catalogue_snapshot(db) == before


def test_legacy_barcode_feature_off_refuses_before_lookup(editor_client, db, serial_catalogue_misses):
    features.set_feature_enabled(db, "periodicals", False)
    db.commit()
    before = _catalogue_snapshot(db)
    response = editor_client.get("/api/magazines/assist/scan", params={"raw_barcode": MAGAZINE_EAN})
    assert response.status_code == 403
    assert "turned off" in response.text
    for lookup in serial_catalogue_misses.values():
        lookup.assert_not_awaited()
    assert _catalogue_snapshot(db) == before


def test_legacy_barcode_accepts_blank_flat_location_from_real_get_form(
    editor_client, db, google_magazine_hit,
):
    before = _catalogue_snapshot(db)
    response = editor_client.get("/api/magazines/assist/scan", params={
        "raw_barcode": MAGAZINE_EAN, "location_id": "",
    })
    assert response.status_code == 200
    assert _legacy_confirmation_fields(response.text)["location_id"] == ""
    google_magazine_hit.assert_awaited_once()
    assert _catalogue_snapshot(db) == before


def test_legacy_barcode_keeps_precise_node_continuation_until_real_confirmation(
    editor_client, db, google_magazine_hit, serial_catalogue_misses,
):
    features.set_feature_enabled(db, "shelf_fill", True)
    db.execute("INSERT INTO locations (id, name) VALUES (701, 'Legacy flat')")
    db.execute("INSERT INTO location_nodes (id, name) VALUES (701, 'Unrelated equal integer')")
    db.execute("INSERT INTO location_nodes (id, name, legacy_location_id) VALUES (702, 'Mapped root', 701)")
    db.execute("INSERT INTO location_nodes (id, name, parent_id) VALUES (703, 'Precise shelf', 702)")
    db.commit()
    before = _catalogue_snapshot(db)
    response = editor_client.get("/api/magazines/assist/scan", params={
        "raw_barcode": MAGAZINE_EAN + "05", "location_node_id": 703, "shelf_fill_origin": "shelf_fill",
        "library_id": libraries.DEFAULT_LIBRARY_ID, "tags": "node-magazine", "reading_status": "reading",
    }, headers={"HX-Request": "true"})
    assert response.status_code == 200
    fields = _legacy_confirmation_fields(response.text)
    assert (fields["location_id"], fields["location_node_id"], fields["shelf_fill_origin"]) == (
        "701", "703", "shelf_fill",
    )
    search = _html_form(response.text, "/api/magazines/search", "get")["fields"]
    for key in ("location_id", "location_node_id", "shelf_fill_origin", "library_id", "tags", "reading_status"):
        assert search[key] == fields[key]
    assert _catalogue_snapshot(db) == before
    fields.update(issue_number="9")
    saved = editor_client.post("/api/magazines/add", data=fields, headers={"HX-Request": "true"})
    assert saved.status_code == 200
    assert 'data-testid="shelf-fill-position"' in saved.text
    db.commit()
    assert db.execute("SELECT location_id FROM items").fetchone()[0] == 701
    copy = db.execute("SELECT location_id, position_order FROM item_copies").fetchone()
    assert copy["location_id"] == 703
    assert copy["position_order"] is not None
    google_magazine_hit.assert_awaited_once()
    for provider in ("issn_portal", "crossref", "upcitemdb"):
        serial_catalogue_misses[provider].assert_not_awaited()
