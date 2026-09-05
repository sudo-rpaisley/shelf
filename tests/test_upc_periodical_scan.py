import pytest

from app.services import periodicals, provider_result, upc as upc_svc, upcitemdb


# The exact UPC-A carrier shown in the reported Android camera scan. The issue
# beside it carries the two-digit add-on "01".
REPORTED_UPC = "312053616115"
REPORTED_EAN13 = "0312053616115"
REPORTED_SUPPLEMENT = "01"
REPORTED_FULL = REPORTED_UPC + REPORTED_SUPPLEMENT


def test_reported_upc_plus_two_digit_addon_is_a_periodical_barcode():
    serial = periodicals.parse_barcode(REPORTED_FULL)

    assert serial is not None
    assert serial.ean13 == REPORTED_EAN13
    assert serial.issn is None
    assert serial.variant is None
    assert serial.supplement == REPORTED_SUPPLEMENT
    # Internal full-code form is canonical EAN-13 + the preserved extension.
    assert serial.full_code == REPORTED_EAN13 + REPORTED_SUPPLEMENT


def test_plain_reported_upc_is_not_guessed_to_be_a_periodical():
    assert periodicals.parse_barcode(REPORTED_UPC) is None
    assert upc_svc.detect_barcode_type(REPORTED_UPC) == "upc"


@pytest.mark.parametrize("suffix", ["01", "12345"])
def test_upca_addons_enter_upc_dispatch(suffix):
    assert upc_svc.detect_barcode_type(REPORTED_UPC + suffix) == "upc"


def test_supplemented_upc_scan_opens_magazine_issue_form(
    editor_client, db, monkeypatch
):
    async def _upc_hit(upc, client):
        # UPC Item DB should receive native UPC-A, not the zero-padded storage
        # form and not the issue add-on.
        assert upc == REPORTED_UPC
        return provider_result.found(
            "upcitemdb",
            {
                "title": "Retro Car Monthly",
                "category": "Magazines",
                "brand": "Example Publishing",
                "images": [],
            },
        )

    monkeypatch.setattr(upcitemdb, "lookup", _upc_hit)

    resp = editor_client.post(
        "/api/scan",
        data={"isbn": REPORTED_FULL, "media_type": "auto"},
    )

    assert resp.status_code == 200
    assert 'name="title" value="Retro Car Monthly"' in resp.text
    assert f"Barcode {REPORTED_UPC}" in resp.text
    assert "add-on 01" in resp.text
    assert f'name="carrier_ean" value="{REPORTED_EAN13}"' in resp.text
    assert 'name="barcode_supplement" value="01"' in resp.text
    assert 'name="issn" value=""' in resp.text
    assert "ISSN None" not in resp.text
    assert "Find a magazine match" in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0


def test_supplemented_upc_issue_can_be_added_and_rescanned_as_duplicate(
    editor_client, db, monkeypatch
):
    added = editor_client.post(
        "/api/magazines/add",
        data={
            "title": "Retro Car Monthly",
            "carrier_ean": REPORTED_EAN13,
            "barcode_supplement": REPORTED_SUPPLEMENT,
            "issue_number": "1",
        },
    )
    assert added.status_code == 200
    assert "Retro Car Monthly" in added.text

    issue = db.execute(
        "SELECT barcode_ean, barcode_supplement FROM periodical_issues"
    ).fetchone()
    assert issue["barcode_ean"] == REPORTED_EAN13
    assert issue["barcode_supplement"] == REPORTED_SUPPLEMENT
    publication = db.execute(
        "SELECT title, issn FROM periodical_publications"
    ).fetchone()
    assert publication["title"] == "Retro Car Monthly"
    assert publication["issn"] is None

    async def _upc_must_not_run(upc, client):
        raise AssertionError("An exact known issue should resolve locally")

    monkeypatch.setattr(upcitemdb, "lookup", _upc_must_not_run)

    rescanned = editor_client.post(
        "/api/scan",
        data={"isbn": REPORTED_FULL, "media_type": "auto"},
    )
    assert rescanned.status_code == 200
    assert "duplicate" in rescanned.text
    assert "Retro Car Monthly" in rescanned.text


def test_next_addon_does_not_reuse_known_publication_as_barcode_authority(
    editor_client, db, monkeypatch
):
    first = editor_client.post(
        "/api/magazines/add",
        data={
            "title": "Retro Car Monthly",
            "publisher": "Example Publishing",
            "carrier_ean": REPORTED_EAN13,
            "barcode_supplement": "01",
            "issue_number": "1",
        },
    )
    assert first.status_code == 200

    async def _upc_miss(upc, client):
        assert upc == REPORTED_UPC
        return provider_result.no_match("upcitemdb")

    monkeypatch.setattr(upcitemdb, "lookup", _upc_miss)

    next_issue = editor_client.post(
        "/api/scan",
        data={"isbn": REPORTED_UPC + "02", "media_type": "auto"},
    )
    assert next_issue.status_code == 200
    assert 'name="title" value=""' in next_issue.text
    assert 'name="publisher" value=""' in next_issue.text
    assert "add-on 02" in next_issue.text
    assert "Find a magazine match" in next_issue.text
