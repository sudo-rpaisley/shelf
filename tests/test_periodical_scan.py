from unittest.mock import AsyncMock, patch

import pytest

from app.services import periodical_scan, provider_result


POPULAR_SCIENCE_EAN = "9770161737008"


@pytest.fixture(autouse=True)
def no_other_serial_legs():
    google = AsyncMock(side_effect=AssertionError("native serial scan contacted Google"))
    crossref = AsyncMock(side_effect=AssertionError("native serial scan contacted Crossref"))
    retail = AsyncMock(side_effect=AssertionError("native serial scan contacted UPC Item DB"))
    with patch("app.services.periodical_scan.googlebooks.lookup_magazine_by_issn", new=google), \
         patch("app.services.periodical_scan.crossref_journals.lookup", new=crossref), \
         patch("app.services.periodical_scan.upcitemdb.lookup", new=retail):
        yield
    for lookup in (google, crossref, retail):
        lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_barcode_never_calls_issn_provider():
    lookup = AsyncMock()
    with patch("app.services.periodical_scan.issn_portal.lookup", new=lookup):
        result = await periodical_scan.resolve_serial_barcode("4006381333931", object())

    assert result.outcome == "no_match"
    assert result.provider == "periodical_barcode"
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_977_scan_resolves_publication_but_not_issue_identity():
    lookup = AsyncMock(return_value=provider_result.found("issn_portal", {
        "title": "Popular Science",
        "publisher": "Bonnier",
        "language": "en",
        "issn": "0161-7370",
    }))

    with patch("app.services.periodical_scan.issn_portal.lookup", new=lookup):
        result = await periodical_scan.resolve_serial_barcode(
            POPULAR_SCIENCE_EAN + "05", object()
        )

    assert result.found
    assert (result.provider, result.status) == ("issn_portal", 200)
    assert result.payload["publication_title"] == "Popular Science"
    assert result.payload["issn"] == "0161-7370"
    assert result.payload["barcode_ean"] == POPULAR_SCIENCE_EAN
    assert result.payload["barcode_supplement"] == "05"
    assert result.payload["barcode_variant"] == "00"
    assert result.payload["issue_number"] is None
    assert result.payload["issue_date"] is None
    assert result.payload["requires_issue_confirmation"] is True
    lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_failure_keeps_decoded_barcode_for_manual_confirmation():
    lookup = AsyncMock(return_value=provider_result.transport_failed("issn_portal"))

    with patch("app.services.periodical_scan.issn_portal.lookup", new=lookup):
        result = await periodical_scan.resolve_serial_barcode(POPULAR_SCIENCE_EAN, object())

    assert result.outcome == "transport_failed"
    assert (result.provider, result.status) == ("issn_portal", None)
    assert result.payload["issn"] == "0161-7370"
    assert result.payload["publication_title"] is None
    assert result.payload["requires_issue_confirmation"] is True
    lookup.assert_awaited_once()


@pytest.mark.parametrize("raw", ["", "9770161737009", "978016173700805", "400638133393105", "07099211206205"])
@pytest.mark.asyncio
async def test_native_resolver_rejects_invalid_and_non_issn_periodicals_without_requests(raw):
    lookup = AsyncMock(side_effect=AssertionError("invalid/non-ISSN input contacted Portal"))
    with patch("app.services.periodical_scan.issn_portal.lookup", new=lookup):
        result = await periodical_scan.resolve_serial_barcode(raw, object())
    assert (result.outcome, result.provider, result.status, result.payload) == (
        "no_match", "periodical_barcode", None, None,
    )
    lookup.assert_not_awaited()


@pytest.mark.parametrize("result", [
    provider_result.no_match("issn_portal", status=404),
    provider_result.rate_limited("issn_portal", status=429),
    provider_result.rejected("issn_portal", status=403),
])
@pytest.mark.asyncio
async def test_native_resolver_retains_portal_quality_and_decoded_context(result):
    lookup = AsyncMock(return_value=result)
    with patch("app.services.periodical_scan.issn_portal.lookup", new=lookup):
        resolved = await periodical_scan.resolve_serial_barcode(POPULAR_SCIENCE_EAN + "05", object())
    assert (resolved.outcome, resolved.provider, resolved.status) == (result.outcome, result.provider, result.status)
    assert resolved.payload["publication_title"] is None
    assert resolved.payload["publication_issn"] is None
    assert resolved.payload["issn"] == "0161-7370"
    assert (resolved.payload["barcode_ean"], resolved.payload["barcode_supplement"],
            resolved.payload["barcode_variant"]) == (POPULAR_SCIENCE_EAN, "05", "00")
    assert resolved.payload["issue_number"] is resolved.payload["issue_date"] is None
    lookup.assert_awaited_once()


@pytest.mark.parametrize("metadata", [
    {"title": "Wrong Serial", "issn": "0953-6167"},
    {"title": "Missing ISSN"},
    {"title": "Invalid ISSN", "issn": "0161-7371"},
    *({"title": title, "issn": "0161-7370"} for title in ("Magazines", "  ", 123, True, None, ["Title"])),
])
@pytest.mark.asyncio
async def test_native_resolver_rejects_unusable_portal_hits_without_fallback(metadata):
    lookup = AsyncMock(return_value=provider_result.found("issn_portal", metadata, status=200))
    with patch("app.services.periodical_scan.issn_portal.lookup", new=lookup):
        result = await periodical_scan.resolve_serial_barcode(POPULAR_SCIENCE_EAN + "05", object())
    assert (result.outcome, result.provider, result.status) == ("no_match", "issn_portal", 200)
    assert result.payload["publication_title"] is result.payload["publication_issn"] is None
    assert result.payload["issn"] == "0161-7370"
    assert (result.payload["barcode_ean"], result.payload["barcode_supplement"]) == (POPULAR_SCIENCE_EAN, "05")
    assert result.payload["issue_number"] is result.payload["issue_date"] is None
    lookup.assert_awaited_once()


def test_attach_issue_uses_user_confirmed_issue_fields_without_inferring_supplement(db):
    item_id = db.execute(
        "INSERT INTO items (title, media_type, owned) VALUES (?, 'magazine', 1)",
        ("Popular Science — March 2026",),
    ).lastrowid
    candidate = {
        "publication_title": "Popular Science",
        "publisher": "Bonnier",
        "language": "en",
        "issn": "0161-7370",
        "barcode_ean": POPULAR_SCIENCE_EAN,
        "barcode_supplement": "05",
    }

    publication_id = periodical_scan.attach_issue_identity(
        db,
        item_id=item_id,
        candidate=candidate,
        issue_number="3",
        volume="154",
        issue_date="2026-03-01",
        cover_date_label="March 2026",
    )

    publication = db.execute(
        "SELECT * FROM periodical_publications WHERE id = ?", (publication_id,)
    ).fetchone()
    issue = db.execute(
        "SELECT * FROM periodical_issues WHERE item_id = ?", (item_id,)
    ).fetchone()

    assert publication["title"] == "Popular Science"
    assert publication["issn"] == "0161-7370"
    assert issue["issue_number"] == "3"
    assert issue["volume"] == "154"
    assert issue["barcode_supplement"] == "05"
    assert issue["issue_number"] != issue["barcode_supplement"]


def test_attach_issue_requires_publication_title_when_lookup_did_not_find_one(db):
    item_id = db.execute(
        "INSERT INTO items (title, media_type, owned) VALUES (?, 'magazine', 1)",
        ("Unknown issue",),
    ).lastrowid

    with pytest.raises(ValueError, match="publication title"):
        periodical_scan.attach_issue_identity(
            db,
            item_id=item_id,
            candidate={
                "issn": "0161-7370",
                "barcode_ean": POPULAR_SCIENCE_EAN,
                "publication_title": None,
            },
        )
