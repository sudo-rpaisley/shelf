from app.services import (
    crossref_journals,
    googlebooks,
    issn_portal,
    provider_result,
    upcitemdb,
)


VW_MOTORING_EAN = "9770953616115"
VW_MOTORING_ADDON = "01"
VW_MOTORING_SCAN = VW_MOTORING_EAN + VW_MOTORING_ADDON


def test_vw_motoring_977_plus_addon_resolves_publication_title(
    editor_client, db, monkeypatch
):
    """Regression for the Android camera scan reported on 4 September 2026."""

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

    async def _crossref_must_not_run(issn, client):
        raise AssertionError("Crossref should not run after an ISSN Portal hit")

    async def _upc_must_not_run(upc, client):
        raise AssertionError("Retail lookup should not run after an ISSN Portal hit")

    monkeypatch.setattr(googlebooks, "lookup_magazine_by_issn", _google_miss)
    monkeypatch.setattr(issn_portal, "lookup", _issn_hit)
    monkeypatch.setattr(crossref_journals, "lookup", _crossref_must_not_run)
    monkeypatch.setattr(upcitemdb, "lookup", _upc_must_not_run)

    resp = editor_client.post(
        "/api/scan",
        data={"isbn": VW_MOTORING_SCAN, "media_type": "auto"},
    )

    assert resp.status_code == 200
    assert 'name="title" value="VW motoring"' in resp.text
    assert "ISSN 0953-6167" in resp.text
    assert "barcode add-on 01" in resp.text
    assert f'name="carrier_ean" value="{VW_MOTORING_EAN}"' in resp.text
    assert f'name="barcode_supplement" value="{VW_MOTORING_ADDON}"' in resp.text
    assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0
