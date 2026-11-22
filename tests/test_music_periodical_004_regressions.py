"""Candidate004 connector boundaries; real writers, native forms and strict oracles."""

from html import unescape
from unittest.mock import AsyncMock

import httpx
import pytest

from app import config, features
from app.database import get_db
from app.routers import settings as settings_router
from app.services import (
    crossref_journals, discogs, googlebooks, issn_portal, libraries, music_catalog,
    musicbrainz, outbound, periodical_google, periodical_records, periodical_scan,
    provider_result, upcitemdb,
)
from app.services.item_write import insert_item, trash_item
from tests.test_magazine_scan import _confirmation_fields, _legacy_confirmation_fields


BARCODE = "977016173700805"
ISSN = "0161-7370"
TABLES = ("items", "library_items", "item_copies", "periodical_publications", "periodical_issues",
          "music_releases", "music_media", "music_tracks", "music_identifiers", "list_items", "user_item_state")


def _snapshot(db):
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in TABLES}


@pytest.fixture
def ladder(monkeypatch):
    stubs = {}
    for module, method, name in (
        (googlebooks, "lookup_magazine_by_issn", "google"), (issn_portal, "lookup", "issn_portal"),
        (crossref_journals, "lookup", "crossref"), (upcitemdb, "lookup", "upcitemdb"),
    ):
        stub = AsyncMock(return_value=provider_result.no_match(name))
        monkeypatch.setattr(module, method, stub)
        stubs[name] = stub
    return stubs


@pytest.mark.asyncio
async def test_canonical_ladder_requires_exact_issn_and_keeps_order(ladder):
    calls = []

    async def google(issn, client, *, api_key=None):
        calls.append("google")
        assert issn == ISSN and api_key == "typed-test-key"
        return provider_result.found("google", {"title": "Wrong Serial", "issn": "0953-6167"})

    async def portal(issn, client):
        calls.append("issn_portal")
        assert issn == ISSN
        return provider_result.no_match("issn_portal")

    async def crossref(issn, client):
        calls.append("crossref")
        assert issn == ISSN
        return provider_result.found("crossref", {"title": "Exact Serial", "issn": ISSN,
                                                  "issue_number": "invented", "issue_date": "2026-01-01"})

    ladder["google"].side_effect = google
    ladder["issn_portal"].side_effect = portal
    ladder["crossref"].side_effect = crossref
    result = await periodical_scan.resolve_barcode(BARCODE, object(), google_api_key="typed-test-key")
    assert calls == ["google", "issn_portal", "crossref"]
    assert (result.provider, result.outcome) == ("crossref", "found")
    assert result.payload["publication_title"] == "Exact Serial"
    assert result.payload["barcode_ean"] == BARCODE[:13]
    assert result.payload["barcode_supplement"] == "05"
    assert result.payload["issue_number"] is result.payload["issue_date"] is None
    ladder["upcitemdb"].assert_not_awaited()


@pytest.mark.asyncio
async def test_exhausted_ladder_retains_actionable_failure_and_decoded_payload(ladder):
    ladder["google"].return_value = provider_result.transport_failed("google")
    ladder["upcitemdb"].return_value = provider_result.found("upcitemdb", {"title": "  Magazines  "})
    result = await periodical_scan.resolve_barcode(BARCODE, object())
    assert (result.provider, result.outcome) == ("google", "transport_failed")
    assert result.payload["publication_title"] is None
    assert result.payload["issn"] == ISSN
    assert result.payload["barcode_supplement"] == "05"
    for stub in ladder.values():
        stub.assert_awaited_once()


@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_core_scan_assist_and_confirm_remain_usable_with_every_feature_off(
    editor_client, db, monkeypatch, ladder, mode,
):
    features.apply_profile(db, "minimal")
    db.commit()
    assert all(not features.feature_enabled(key) for key in features.FEATURES)
    before = _snapshot(db)
    scan = editor_client.post("/api/scan", data={"isbn": BARCODE, "media_type": "dvd", "mode": mode})
    assert scan.status_code == 200
    fields = _confirmation_fields(scan.text)
    assert fields["raw_barcode"] == BARCODE
    assert fields["publication_title"] == ""
    assert _snapshot(db) == before

    issue = {"google_volume_id": "exact-issue", "title": "Confirmed Serial", "issn": ISSN,
             "issue_date": "2026-04-01", "publisher": "Test Publisher", "language": "en"}
    search = AsyncMock(return_value=provider_result.found("google", [issue]))
    lookup = AsyncMock(return_value=provider_result.found("google", issue))
    monkeypatch.setattr(periodical_google, "search_issues", search)
    monkeypatch.setattr(periodical_google, "lookup_issue", lookup)
    params = {"raw_barcode": BARCODE, "mode": mode, "library_id": libraries.DEFAULT_LIBRARY_ID}
    assisted = editor_client.get("/api/periodicals/assist/search", params={**params, "q": "Confirmed Serial"})
    assert assisted.status_code == 200
    assert "exact-issue" in assisted.text
    selected = editor_client.get("/api/periodicals/assist/select", params={**params, "volume_id": "exact-issue"})
    assert selected.status_code == 200
    fields = _confirmation_fields(selected.text)
    assert fields["raw_barcode"] == BARCODE
    assert fields["publication_title"] == "Confirmed Serial"
    assert fields["issue_date"] == "2026-04-01"
    assert _snapshot(db) == before
    search.assert_awaited_once()
    lookup.assert_awaited_once()

    fields.update(issue_number="42")
    confirmed = editor_client.post("/api/periodicals/confirm", data=fields, follow_redirects=True)
    assert confirmed.status_code == 200
    assert [response.status_code for response in confirmed.history] == [303]
    row = db.execute("SELECT barcode_ean, barcode_supplement, issue_number, issue_date FROM periodical_issues").fetchone()
    assert tuple(row) == (BARCODE[:13], "05", "42", "2026-04-01")
    item = db.execute("SELECT id, owned FROM items").fetchone()
    assert confirmed.history[0].headers["location"] == f"/item/{item['id']}"
    assert confirmed.url.path == f"/item/{item['id']}"
    assert 'data-testid="item-hero"' in confirmed.text
    assert "Confirmed Serial — No. 42" in unescape(confirmed.text)
    assert 'data-testid="feature-disabled"' not in confirmed.text
    assert ('data-testid="wishlist-badge"' in confirmed.text) == (mode == "wishlist")
    assert item["owned"] == (1 if mode == "add" else 0)
    assert libraries.item_library_id(db, item["id"]) == libraries.DEFAULT_LIBRARY_ID
    assert db.execute("SELECT COUNT(*) FROM item_copies").fetchone()[0] == (1 if mode == "add" else 0)
    assert db.execute("SELECT COUNT(*) FROM list_items").fetchone()[0] == (1 if mode == "wishlist" else 0)
    after_save = _snapshot(db)
    # The real core editor displays the retained printed identity even with
    # optional publication pages off; the item detail itself displays the issue.
    edit = editor_client.get(f"/item/{item['id']}/edit", follow_redirects=True)
    assert edit.status_code == 200
    assert edit.history == []
    assert f'name="magazine_barcode_ean" value="{BARCODE[:13]}"' in edit.text
    assert 'name="magazine_barcode_supplement" value="05"' in edit.text
    publication_id = db.execute("SELECT publication_id FROM periodical_issues").fetchone()[0]
    for path in ("/periodicals", f"/periodicals/{publication_id}"):
        gated = editor_client.get(path)
        assert gated.status_code == 200  # Native disabled-page response, NOT the saved issue.
        assert 'data-testid="feature-disabled"' in gated.text
        assert "Confirmed Serial — No. 42" not in unescape(gated.text)
    assert _snapshot(db) == after_save
    optional = editor_client.get(f"/api/music/items/{item['id']}/discogs")
    assert optional.status_code == 403
    assert optional.json()["feature"] == "music"


@pytest.mark.parametrize("mode", ["add", "wishlist"])
def test_confirm_retains_native_publication_destination_when_feature_is_on(editor_client, db, mode):
    features.set_feature_enabled(db, "periodicals", True)
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM periodical_issues").fetchone()[0] == 0
    response = editor_client.post("/api/periodicals/confirm", data={
        "raw_barcode": BARCODE, "publication_title": "Enabled Serial", "issue_number": "42", "mode": mode,
    }, follow_redirects=True)
    assert response.status_code == 200
    assert [hop.status_code for hop in response.history] == [303]
    issue = db.execute("SELECT item_id, publication_id, barcode_ean, barcode_supplement FROM periodical_issues").fetchone()
    assert (issue["barcode_ean"], issue["barcode_supplement"]) == (BARCODE[:13], "05")
    assert response.history[0].headers["location"] == f"/periodicals/{issue['publication_id']}"
    assert response.url.path == f"/periodicals/{issue['publication_id']}"
    assert f'href="/item/{issue["item_id"]}"' in response.text
    assert "Enabled Serial — No. 42" in unescape(response.text)
    assert "add-on 05" in response.text
    assert 'data-testid="feature-disabled"' not in response.text


@pytest.fixture
def malformed_title_wire(monkeypatch):
    """Stub only outbound I/O; all four real provider parsers stay installed."""
    portal_url = "http://127.0.0.1:43210/review-issn/" + ISSN
    monkeypatch.setenv("SHELF_ISSN_PORTAL_URL", "http://127.0.0.1:43210/review-issn/{issn}")

    def configure(*, hit=None, retail_title=None, crossref_status=404):
        calls = []

        async def respond(client, method, url, **kwargs):
            assert method == "GET"
            if url == googlebooks.VOLUMES_URL:
                calls.append("google")
                assert kwargs["params"] == {"q": ISSN, "printType": "magazines", "maxResults": "10"}
                return httpx.Response(200, json={"items": [{"volumeInfo": {
                    "printType": "MAGAZINE", "title": 123,
                    "industryIdentifiers": [{"type": "ISSN", "identifier": ISSN}],
                }}]})
            if url == portal_url:
                calls.append("issn_portal")
                if hit == "issn_portal":
                    return httpx.Response(200, json={"@graph": [{
                        "@id": f"resource/ISSN/{ISSN}", "identifier": ISSN, "mainTitle": "Validated Serial",
                    }]})
                return httpx.Response(404)
            if url == crossref_journals.RESOURCE_URL.format(issn=ISSN):
                calls.append("crossref")
                if hit == "crossref":
                    return httpx.Response(200, json={"message": {"ISSN": [ISSN], "title": "Validated Serial"}})
                return httpx.Response(crossref_status)
            if url == config.upc_lookup_url():
                calls.append("upcitemdb")
                assert kwargs["params"] == {"upc": BARCODE[:13]}
                return httpx.Response(200, json={"items": [{"title": retail_title}]})
            calls.append("unexpected")
            raise AssertionError(f"Unexpected provider URL: {url}")

        fetch = AsyncMock(side_effect=respond)
        monkeypatch.setattr(outbound, "fetch", fetch)
        return calls, fetch

    return configure


@pytest.mark.parametrize("hit", ["issn_portal", "crossref"])
@pytest.mark.asyncio
async def test_numeric_google_wire_title_falls_through_to_real_serial_parser(db, malformed_title_wire, hit):
    calls, fetch = malformed_title_wire(hit=hit)
    before = _snapshot(db)
    result = await periodical_scan.resolve_barcode(BARCODE, object())
    expected = ["google", "issn_portal"] + (["crossref"] if hit == "crossref" else [])
    assert calls == expected
    assert fetch.await_count == len(expected)
    assert (result.outcome, result.provider, result.status) == ("found", hit, 200)
    assert result.payload["publication_title"] == "Validated Serial"
    assert result.payload["issn"] == ISSN
    assert (result.payload["barcode_ean"], result.payload["barcode_supplement"]) == (BARCODE[:13], "05")
    assert result.payload["issue_number"] is result.payload["issue_date"] is None
    assert _snapshot(db) == before


@pytest.mark.parametrize("retail_title", [123, ["Magazine"], {"name": "Magazine"}, True, None])
def test_malformed_google_and_retail_wire_titles_leave_manual_confirmation_without_writes(
    editor_client, db, malformed_title_wire, retail_title,
):
    calls, fetch = malformed_title_wire(retail_title=retail_title)
    before = _snapshot(db)
    response = editor_client.get("/api/magazines/assist/scan", params={"raw_barcode": BARCODE})
    assert response.status_code == 200
    fields = _legacy_confirmation_fields(response.text)
    assert fields["title"] == ""
    assert fields["issn"] == ""
    assert (fields["carrier_ean"], fields["barcode_supplement"]) == (BARCODE[:13], "05")
    assert (fields["issue_number"], fields["issue_date"]) == ("", "")
    assert "Barcode ISSN hint " + ISSN in response.text
    assert calls == ["google", "issn_portal", "crossref", "upcitemdb"]
    assert fetch.await_count == 4
    assert _snapshot(db) == before


@pytest.mark.parametrize("portal_case", ["404", "429", "wrong-issn", "generic", "numeric", "malformed"])
def test_native_portal_only_wire_refusals_keep_manual_confirmation_without_writes(
    editor_client, db, monkeypatch, portal_case,
):
    """Separate native HTTP oracle; legacy malformed-title ladder stays above."""
    monkeypatch.setenv("SHELF_ISSN_PORTAL_URL", "http://127.0.0.1:43210/native-issn/{issn}")
    other_legs = [AsyncMock(side_effect=AssertionError("native 977 invoked another provider")) for _ in range(3)]
    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", other_legs[0])
    monkeypatch.setattr(crossref_journals, "lookup", other_legs[1])
    monkeypatch.setattr(upcitemdb, "lookup", other_legs[2])

    async def respond(client, method, url, **kwargs):
        assert (method, url) == ("GET", "http://127.0.0.1:43210/native-issn/" + ISSN)
        assert kwargs["params"] == {"format": "json"}
        if portal_case in {"404", "429"}:
            return httpx.Response(int(portal_case))
        if portal_case == "malformed":
            return httpx.Response(200, content=b"not json")
        target = "0953-6167" if portal_case == "wrong-issn" else ISSN
        title = {"wrong-issn": "Wrong Serial", "generic": "  Magazines  ", "numeric": 123}[portal_case]
        return httpx.Response(200, json={"@graph": [{"identifier": target, "mainTitle": title}]})

    fetch = AsyncMock(side_effect=respond)
    monkeypatch.setattr(outbound, "fetch", fetch)
    before = _snapshot(db)
    response = editor_client.post("/api/scan", data={"isbn": BARCODE, "media_type": "dvd"})
    assert response.status_code == 200
    fields = _confirmation_fields(response.text)
    assert fields["publication_title"] == ""
    assert fields["publication_issn"] == ISSN
    assert fields["raw_barcode"] == BARCODE
    assert (fields["issue_number"], fields["issue_date"]) == ("", "")
    if portal_case == "429":
        assert "rate-limiting requests" in response.text
    else:
        assert "Enter the publication title below" in response.text
    fetch.assert_awaited_once()
    for leg in other_legs:
        leg.assert_not_awaited()
    assert _snapshot(db) == before


@pytest.mark.parametrize(("crossref_status", "outcome", "provider", "status"), [
    (404, "no_match", "google", 200),
    (429, "rate_limited", "crossref", 429),
])
@pytest.mark.asyncio
async def test_invalid_wire_titles_preserve_exact_ladder_outcomes(
    db, malformed_title_wire, crossref_status, outcome, provider, status,
):
    calls, fetch = malformed_title_wire(retail_title=123, crossref_status=crossref_status)
    before = _snapshot(db)
    result = await periodical_scan.resolve_barcode(BARCODE, object())
    assert (result.outcome, result.provider, result.status) == (outcome, provider, status)
    assert result.payload["publication_title"] is None
    assert result.payload["issn"] == ISSN
    assert (result.payload["barcode_ean"], result.payload["barcode_supplement"]) == (BARCODE[:13], "05")
    assert calls == ["google", "issn_portal", "crossref", "upcitemdb"]
    assert fetch.await_count == 4
    assert _snapshot(db) == before


def test_core_feature_exemption_does_not_remove_library_or_viewer_guards(viewer_client, db, monkeypatch):
    features.apply_profile(db, "minimal")
    db.commit()
    provider = AsyncMock(side_effect=AssertionError("a viewer cannot spend provider credentials"))
    monkeypatch.setattr(periodical_google, "search_issues", provider)
    monkeypatch.setattr(periodical_google, "lookup_issue", provider)
    before = _snapshot(db)
    params = {"raw_barcode": BARCODE, "q": "Hidden Serial", "volume_id": "private"}
    for path in ("/api/periodicals/assist/search", "/api/periodicals/assist/select"):
        assert viewer_client.get(path, params=params).status_code == 403
    response = viewer_client.post("/api/periodicals/confirm", data={
        "raw_barcode": BARCODE, "publication_title": "Forbidden Serial", "issue_number": "1",
    })
    assert response.status_code == 403
    provider.assert_not_awaited()
    assert _snapshot(db) == before


def test_core_feature_exemption_does_not_authorize_an_inaccessible_library(editor_client, db, monkeypatch):
    private = libraries.create_library(db, "Inaccessible Serials")
    features.apply_profile(db, "minimal")
    db.commit()
    provider = AsyncMock(side_effect=AssertionError("library authorization must precede provider work"))
    monkeypatch.setattr(periodical_google, "search_issues", provider)
    monkeypatch.setattr(periodical_google, "lookup_issue", provider)
    before = _snapshot(db)
    params = {"raw_barcode": BARCODE, "q": "Private Serial", "volume_id": "private", "library_id": private["id"]}
    for path in ("/api/periodicals/assist/search", "/api/periodicals/assist/select"):
        assert editor_client.get(path, params=params).status_code == 404
    response = editor_client.post("/api/periodicals/confirm", data={
        "raw_barcode": BARCODE, "publication_title": "Private Serial", "issue_number": "1", "library_id": private["id"],
    })
    assert response.status_code == 404
    provider.assert_not_awaited()
    assert _snapshot(db) == before


@pytest.mark.parametrize("client_name", ["admin_client", "viewer_client"])
def test_native_admin_empty_publication_capability_never_leaks_to_ordinary_reader(db, request, client_name):
    client = request.getfixturevalue(client_name)
    empty_id = periodical_records.upsert_publication(db, title="Empty Admin Quarterly")
    private = libraries.create_library(db, "Hidden Serials")
    hidden_id = insert_item(db, title="Private Issue", media_type="magazine", library_id=private["id"])
    hidden_publication = periodical_records.upsert_publication(db, title="Hidden Library Serial")
    periodical_records.link_issue(db, item_id=hidden_id, publication_id=hidden_publication, issue_number="1")
    live_id = insert_item(db, title="Visible Issue", media_type="magazine", library_id=libraries.DEFAULT_LIBRARY_ID)
    visible = periodical_records.upsert_publication(db, title="Visible Serial")
    periodical_records.link_issue(db, item_id=live_id, publication_id=visible, issue_number="1")
    trashed = insert_item(db, title="Trashed Issue", media_type="magazine", library_id=libraries.DEFAULT_LIBRARY_ID)
    periodical_records.link_issue(db, item_id=trashed, publication_id=visible, issue_number="2")
    trash_item(db, trashed)
    db.commit()
    before = _snapshot(db)
    response = client.get("/periodicals")
    assert response.status_code == 200
    assert "Visible Serial" in response.text
    assert "1 issue" in response.text
    assert "2 issues" not in response.text
    if client_name == "admin_client":
        assert "Empty Admin Quarterly" in response.text
        assert f'href="/periodicals/{empty_id}"' in response.text
        assert "0 issues" in response.text
        assert "Hidden Library Serial" in response.text
    else:
        assert "Empty Admin Quarterly" not in response.text
        assert "Hidden Library Serial" not in response.text
        assert "Private Issue" not in response.text
    assert _snapshot(db) == before


def _music(db):
    item_id = insert_item(db, title="Original Manual Title", media_type="vinyl",
                          library_id=libraries.DEFAULT_LIBRARY_ID)
    music_catalog.save_release(db, item_id, {
        "musicbrainz_release_id": "retained-mbid", "artist_credit": "Manual Artist",
        "catalog_number": "MANUAL CAT", "source": "musicbrainz", "media": [],
    })
    music_catalog.update_copy_details(db, item_id, edition_notes="My pressing", media_condition="VG+",
                                      packaging_condition="VG", condition_notes="Retained wear")
    music_catalog.add_identifier(db, item_id, "matrix_runout", "MANUAL MATRIX")
    settings_router._upsert_setting(db, "discogs_token", "test-discogs-token")
    db.commit()
    return item_id


@pytest.mark.parametrize("payload", [{"discogs_master_id": 123}, {"discogs_release_id": 999}, {"discogs_release_id": 0}])
def test_selection_requires_the_validated_concrete_release_without_writes(editor_client, db, monkeypatch, payload):
    item_id = _music(db)
    lookup = AsyncMock(return_value=provider_result.found("discogs", payload))
    monkeypatch.setattr(discogs, "lookup_release", lookup)
    before = _snapshot(db)
    response = editor_client.post(f"/api/music/items/{item_id}/discogs/select", data={"release_id": "123"})
    assert response.status_code == 502
    lookup.assert_awaited_once()
    assert _snapshot(db) == before


def test_selection_alone_preserves_manual_and_musicbrainz_fields(editor_client, db, monkeypatch):
    item_id = _music(db)
    before = dict(db.execute("SELECT * FROM music_releases WHERE item_id = ?", (item_id,)).fetchone())
    lookup = AsyncMock(return_value=provider_result.found("discogs", {
        "discogs_release_id": 123, "label": "API Label", "notes": "Unstored API notes",
        "identifiers": [{"identifier_type": "matrix_runout", "value": "API MATRIX"}],
    }))
    monkeypatch.setattr(discogs, "lookup_release", lookup)
    response = editor_client.post(f"/api/music/items/{item_id}/discogs/select", data={"release_id": "123"}, follow_redirects=False)
    assert response.status_code == 303
    after = dict(db.execute("SELECT * FROM music_releases WHERE item_id = ?", (item_id,)).fetchone())
    assert after == {**before, "discogs_release_id": 123}
    assert [tuple(row) for row in db.execute(
        "SELECT identifier_type, value, source FROM music_identifiers WHERE item_id = ? ORDER BY identifier_type", (item_id,),
    )] == [("discogs_release_id", "123", "manual"), ("matrix_runout", "MANUAL MATRIX", "manual")]


def test_selection_rechecks_library_authority_after_lookup(editor_client, editor_user, db, monkeypatch):
    item_id = _music(db)

    async def revoke(release_id, client, *, token):
        with get_db() as conn:
            libraries.set_membership(conn, libraries.DEFAULT_LIBRARY_ID, editor_user["id"], "viewer")
        return provider_result.found("discogs", {"discogs_release_id": 123})

    monkeypatch.setattr(discogs, "lookup_release", revoke)
    before = _snapshot(db)
    response = editor_client.post(f"/api/music/items/{item_id}/discogs/select", data={"release_id": "123"})
    assert response.status_code == 403
    assert _snapshot(db) == before


@pytest.mark.parametrize("release_id", ["0", "-1", "master/123", "abc"])
def test_invalid_selection_ids_never_call_discogs(editor_client, db, monkeypatch, release_id):
    item_id = _music(db)
    lookup = AsyncMock(side_effect=AssertionError("invalid ID must not contact Discogs"))
    monkeypatch.setattr(discogs, "lookup_release", lookup)
    before = _snapshot(db)
    response = editor_client.post(f"/api/music/items/{item_id}/discogs/select", data={"release_id": release_id})
    assert response.status_code == 400
    lookup.assert_not_awaited()
    assert _snapshot(db) == before


def test_new_music_release_keeps_native_detail_redirect(editor_client, db, monkeypatch):
    release = {"title": "New Album", "musicbrainz_release_id": "new-release", "media": [{"format": "CD"}]}
    monkeypatch.setattr(musicbrainz, "lookup_release", AsyncMock(return_value=provider_result.found("musicbrainz", release)))
    monkeypatch.setattr(musicbrainz, "cover_art", AsyncMock(return_value=provider_result.found("coverartarchive", [])))
    response = editor_client.post("/api/music/add", data={"release_id": "new-release"}, follow_redirects=False)
    assert response.status_code == 303
    row = db.execute("SELECT id, title, media_type FROM items").fetchone()
    assert (row["title"], row["media_type"]) == ("New Album", "cd")
    assert response.headers["location"] == f"/music/item/{row['id']}"
