from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import magazine_google, provider_result


def _google_issue(volume_id="ps-2008-05", published="2008-05"):
    return {
        "id": volume_id,
        "volumeInfo": {
            "title": "Popular Science",
            "publisher": "Bonnier Corporation",
            "publishedDate": published,
            "pageCount": 128,
            "description": "Science and technology magazine.",
            "printType": "MAGAZINE",
            "industryIdentifiers": [
                {"type": "ISSN", "identifier": "0161-7370"},
            ],
            "imageLinks": {"thumbnail": "http://books.google.com/issue.jpg"},
            "language": "en",
        },
    }


@pytest.mark.asyncio
async def test_exact_issue_search_keeps_issue_date_and_cover():
    fake_fetch = AsyncMock(
        return_value=httpx.Response(200, json={"items": [_google_issue()]})
    )
    with patch("app.services.magazine_google.outbound.fetch", new=fake_fetch):
        result = await magazine_google.search_issues("Popular Science", object())

    assert result.found
    issue = result.payload[0]
    assert issue["google_volume_id"] == "ps-2008-05"
    assert issue["issue_date"] == "2008-05"
    assert issue["publish_year"] == 2008
    assert issue["cover_url"] == "https://books.google.com/issue.jpg"
    assert fake_fetch.await_args.kwargs["params"]["printType"] == "magazines"


def test_scan_page_title_search_routes_magazines_to_exact_issue_search(
    editor_client, monkeypatch
):
    async def _search(query, client, *, api_key=None, limit=10):
        assert query == "Popular Science"
        return provider_result.found("google", [{
            "google_volume_id": "ps-2008-05",
            "title": "Popular Science",
            "publisher": "Bonnier Corporation",
            "issn": "0161-7370",
            "issue_date": "2008-05",
            "cover_url": None,
        }])

    monkeypatch.setattr(magazine_google, "search_issues", _search)
    response = editor_client.get(
        "/api/title-search",
        params={"q": "Popular Science", "media_type": "magazine"},
    )

    assert response.status_code == 200
    assert "Popular Science" in response.text
    assert "2008-05" in response.text
    assert 'name="google_volume_id" value="ps-2008-05"' in response.text
    assert 'hx-post="/api/magazines/add"' in response.text


def test_selected_google_issue_creates_exact_issue_record(editor_client, db, monkeypatch):
    async def _lookup(volume_id, client, *, api_key=None):
        assert volume_id == "ps-2008-05"
        return provider_result.found("google", {
            "google_volume_id": volume_id,
            "title": "Popular Science",
            "publisher": "Bonnier Corporation",
            "description": "Science and technology magazine.",
            "issn": "0161-7370",
            "issue_date": "2008-05",
            "publish_year": 2008,
            "page_count": 128,
            "cover_url": None,
            "language": "en",
        })

    monkeypatch.setattr(magazine_google, "lookup_issue", _lookup)
    response = editor_client.post(
        "/api/magazines/add",
        data={"google_volume_id": "ps-2008-05"},
    )

    assert response.status_code == 200
    assert "Popular Science" in response.text
    item = db.execute("SELECT * FROM items WHERE media_type = 'magazine'").fetchone()
    assert item is not None
    assert item["publish_year"] == 2008
    assert item["upc"] is None
    issue = db.execute(
        "SELECT pi.issue_date, pi.google_volume_id, pp.issn "
        "FROM periodical_issues pi "
        "JOIN periodical_publications pp ON pp.id = pi.publication_id "
        "WHERE pi.item_id = ?",
        (item["id"],),
    ).fetchone()
    assert issue["issue_date"] == "2008-05"
    assert issue["google_volume_id"] == "ps-2008-05"
    assert issue["issn"] == "0161-7370"
