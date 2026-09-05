import pytest

from app.services import (
    crossref_journals,
    googlebooks,
    issn_portal,
    provider_result,
    upcitemdb,
)


MAGAZINE_EAN = "9770161737008"  # Popular Science / ISSN 0161-7370
VW_MOTORING_EAN = "9770953616115"  # VW motoring / ISSN 0953-6167


@pytest.fixture(autouse=True)
def serial_catalogue_misses(monkeypatch):
    """Keep scan tests deterministic unless a test explicitly needs a provider."""
    async def _issn_lookup(issn, client):
        return provider_result.no_match("issn_portal")

    async def _crossref_lookup(issn, client):
        return provider_result.no_match("crossref")

    monkeypatch.setattr(issn_portal, "lookup", _issn_lookup)
    monkeypatch.setattr(crossref_journals, "lookup", _crossref_lookup)


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

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _lookup)


def test_auto_scan_identifies_publication_but_does_not_invent_issue(
    editor_client, db, monkeypatch, google_magazine_hit
):
    async def _upc_must_not_run(upc, client):
        raise AssertionError("UPC Item DB should only be a fallback after a magazine miss")

    monkeypatch.setattr(upcitemdb, "lookup", _upc_must_not_run)

    resp = editor_client.post(
        "/api/scan", data={"isbn": MAGAZINE_EAN, "media_type": "auto"}
    )

    assert resp.status_code == 200
    assert "Popular Science" in resp.text
    assert "ISSN 0161-7370" in resp.text
    assert "does not assume the carrier uniquely identifies a publication or issue" in resp.text
    assert 'name="issue_number"' in resp.text
    assert 'name="issue_date"' in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0


def test_977_overrides_wrong_media_hint_without_creating_wrong_item(
    editor_client, db, google_magazine_hit
):
    resp = editor_client.post(
        "/api/scan", data={"isbn": MAGAZINE_EAN, "media_type": "dvd"}
    )

    assert resp.status_code == 200
    assert "Popular Science" in resp.text
    assert "0161-7370" in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0


def test_issn_portal_identifies_vw_motoring_when_google_misses(
    editor_client, db, monkeypatch
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

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _google_miss)
    monkeypatch.setattr(issn_portal, "lookup", _issn_hit)
    monkeypatch.setattr(upcitemdb, "lookup", _upc_must_not_run)

    resp = editor_client.post(
        "/api/scan", data={"isbn": VW_MOTORING_EAN, "media_type": "auto"}
    )

    assert resp.status_code == 200
    assert "VW motoring" in resp.text
    assert "ISSN 0953-6167" in resp.text
    assert f'name="carrier_ean" value="{VW_MOTORING_EAN}"' in resp.text
    assert 'name="issue_number"' in resp.text
    assert 'name="issue_date"' in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0


def test_crossref_identifies_scholarly_serial_before_retail_fallback(
    editor_client, db, monkeypatch
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

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _google_miss)
    monkeypatch.setattr(crossref_journals, "lookup", _crossref_hit)
    monkeypatch.setattr(upcitemdb, "lookup", _upc_must_not_run)

    resp = editor_client.post(
        "/api/scan", data={"isbn": MAGAZINE_EAN, "media_type": "auto"}
    )

    assert resp.status_code == 200
    assert 'name="title" value="Example Research Journal"' in resp.text
    assert 'name="publisher" value="Example University Press"' in resp.text
    assert "ISSN 0161-7370" in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0


def test_google_miss_falls_back_to_retail_title_for_issue_form(
    editor_client, db, monkeypatch
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

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _google_miss)
    monkeypatch.setattr(upcitemdb, "lookup", _upc_hit)

    resp = editor_client.post(
        "/api/scan", data={"isbn": MAGAZINE_EAN, "media_type": "auto"}
    )

    assert resp.status_code == 200
    assert 'name="title" value="Popular Science Magazine"' in resp.text
    assert 'name="publisher" value="Bonnier"' in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0


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

    resp = editor_client.post(
        "/api/scan", data={"isbn": MAGAZINE_EAN, "media_type": "auto"}
    )

    assert resp.status_code == 200
    assert 'name="title" value=""' in resp.text
    assert f'value="{generic_title}"' not in resp.text
    assert "Barcode ISSN hint 0161-7370" in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0


def test_unknown_977_still_uses_magazine_issue_form(editor_client, db, monkeypatch):
    async def _google_miss(issn, client, *, api_key=None):
        return provider_result.no_match("google")

    async def _upc_miss(upc, client):
        return provider_result.no_match("upcitemdb")

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _google_miss)
    monkeypatch.setattr(upcitemdb, "lookup", _upc_miss)

    resp = editor_client.post(
        "/api/scan", data={"isbn": MAGAZINE_EAN, "media_type": "auto"}
    )

    assert resp.status_code == 200
    assert "Barcode ISSN hint 0161-7370" in resp.text
    assert f'name="carrier_ean" value="{MAGAZINE_EAN}"' in resp.text
    assert 'name="title" value=""' in resp.text
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

    scanned = editor_client.post(
        "/api/scan",
        data={"isbn": MAGAZINE_EAN + "05", "media_type": "auto"},
    )
    assert scanned.status_code == 200
    assert "Barcode ISSN hint 0161-7370 · add-on 05" in scanned.text

    added = editor_client.post(
        "/api/magazines/add",
        data={
            "title": "Popular Science",
            "issn": "0161-7370",
            "carrier_ean": MAGAZINE_EAN,
            "barcode_supplement": "05",
            "issue_number": "5",
        },
    )
    assert added.status_code == 200
    row = db.execute(
        "SELECT barcode_ean, barcode_supplement FROM periodical_issues"
    ).fetchone()
    assert row["barcode_ean"] == MAGAZINE_EAN
    assert row["barcode_supplement"] == "05"
