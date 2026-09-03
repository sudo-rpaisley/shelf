from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import googlebooks


def _magazine_payload(identifier="0161-7370"):
    return {
        "items": [{
            "volumeInfo": {
                "title": "Popular Science",
                "publisher": "Bonnier Corporation",
                "publishedDate": "2008-05",
                "pageCount": 128,
                "description": "Science and technology magazine.",
                "printType": "MAGAZINE",
                "industryIdentifiers": [
                    {"type": "ISSN", "identifier": identifier},
                ],
                "imageLinks": {
                    "thumbnail": "http://books.google.com/arbitrary-issue.jpg",
                },
                "language": "en",
            }
        }]
    }


@pytest.mark.asyncio
async def test_magazine_lookup_uses_magazine_filter_and_exact_issn():
    fake_fetch = AsyncMock(
        return_value=httpx.Response(200, json=_magazine_payload())
    )

    with patch("app.services.googlebooks.outbound.fetch", new=fake_fetch):
        result = await googlebooks.lookup_magazine_by_issn(
            "0161-7370", object(), api_key="key"
        )

    assert result.found
    assert result.payload["title"] == "Popular Science"
    assert result.payload["issn"] == "0161-7370"
    assert result.payload["series_name"] == "Popular Science"
    assert result.payload["publisher"] == "Bonnier Corporation"
    assert result.payload["language"] == "en"

    params = fake_fetch.await_args.kwargs["params"]
    assert params["q"] == "0161-7370"
    assert params["printType"] == "magazines"
    assert params["maxResults"] == "10"


def test_magazine_result_does_not_claim_arbitrary_issue_metadata():
    # A bare 977 carrier identifies the serial publication, not necessarily
    # the particular digitised Google issue returned by the search.
    pass


@pytest.mark.asyncio
async def test_magazine_result_does_not_claim_arbitrary_issue_metadata():
    fake_fetch = AsyncMock(
        return_value=httpx.Response(200, json=_magazine_payload())
    )

    with patch("app.services.googlebooks.outbound.fetch", new=fake_fetch):
        result = await googlebooks.lookup_magazine_by_issn("0161-7370", object())

    assert result.found
    assert "publish_year" not in result.payload
    assert "page_count" not in result.payload
    assert "cover_url" not in result.payload


@pytest.mark.asyncio
async def test_magazine_lookup_rejects_title_match_with_wrong_issn():
    fake_fetch = AsyncMock(
        return_value=httpx.Response(200, json=_magazine_payload("9999-9999"))
    )

    with patch("app.services.googlebooks.outbound.fetch", new=fake_fetch):
        result = await googlebooks.lookup_magazine_by_issn("0161-7370", object())

    assert result.outcome == "no_match"


@pytest.mark.asyncio
async def test_magazine_lookup_rejects_non_magazine_result_even_with_issn():
    payload = _magazine_payload()
    payload["items"][0]["volumeInfo"]["printType"] = "BOOK"
    fake_fetch = AsyncMock(return_value=httpx.Response(200, json=payload))

    with patch("app.services.googlebooks.outbound.fetch", new=fake_fetch):
        result = await googlebooks.lookup_magazine_by_issn("0161-7370", object())

    assert result.outcome == "no_match"


@pytest.mark.parametrize(("status", "outcome"), [
    (400, "rejected"),
    (401, "rejected"),
    (403, "rejected"),
    (429, "rate_limited"),
])
@pytest.mark.asyncio
async def test_magazine_lookup_preserves_provider_failures(status, outcome):
    fake_fetch = AsyncMock(return_value=httpx.Response(status))
    with patch("app.services.googlebooks.outbound.fetch", new=fake_fetch):
        result = await googlebooks.lookup_magazine_by_issn(
            "0161-7370", object(), api_key="key"
        )
    assert result.outcome == outcome
