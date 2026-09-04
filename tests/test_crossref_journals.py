from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import crossref_journals


def _payload(issn="2049-3630", title="Journal of Open Research Software"):
    return {
        "status": "ok",
        "message-type": "journal",
        "message": {
            "title": title,
            "publisher": "Ubiquity Press",
            "ISSN": [issn],
        },
    }


@pytest.mark.asyncio
async def test_lookup_reads_exact_crossref_journal():
    fake_fetch = AsyncMock(return_value=httpx.Response(200, json=_payload()))
    with patch("app.services.crossref_journals.outbound.fetch", new=fake_fetch):
        result = await crossref_journals.lookup("20493630", object())
    assert result.found
    assert result.provider == "crossref"
    assert result.payload["title"] == "Journal of Open Research Software"
    assert result.payload["publisher"] == "Ubiquity Press"
    assert result.payload["issn"] == "2049-3630"
    assert fake_fetch.await_args.args[2].endswith("/journals/2049-3630")


@pytest.mark.asyncio
async def test_lookup_rejects_mismatched_returned_issn():
    fake_fetch = AsyncMock(return_value=httpx.Response(200, json=_payload(issn="0953-6167")))
    with patch("app.services.crossref_journals.outbound.fetch", new=fake_fetch):
        result = await crossref_journals.lookup("2049-3630", object())
    assert result.outcome == "no_match"


@pytest.mark.parametrize(("status", "outcome"), [(404, "no_match"), (429, "rate_limited")])
@pytest.mark.asyncio
async def test_lookup_preserves_http_outcomes(status, outcome):
    fake_fetch = AsyncMock(return_value=httpx.Response(status))
    with patch("app.services.crossref_journals.outbound.fetch", new=fake_fetch):
        result = await crossref_journals.lookup("2049-3630", object())
    assert result.outcome == outcome


@pytest.mark.asyncio
async def test_lookup_preserves_transport_failure():
    fake_fetch = AsyncMock(side_effect=httpx.ConnectError("offline"))
    with patch("app.services.crossref_journals.outbound.fetch", new=fake_fetch):
        result = await crossref_journals.lookup("2049-3630", object())
    assert result.outcome == "transport_failed"
