from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import holdings, magazine_google, periodical_records, provider_result


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


def _seed_issue(
    db,
    publication_id,
    issue_number,
    issue_date,
    *,
    volume="",
    owned=1,
    title="Popular Science",
    cover_path=None,
):
    item_id = db.execute(
        "INSERT INTO items "
        "(title, media_type, source, owned, cover_path, publish_year, series_name) "
        "VALUES (?, 'magazine', 'test', ?, ?, ?, ?)",
        (
            title,
            owned,
            cover_path,
            int(issue_date[:4]) if issue_date else None,
            title,
        ),
    ).lastrowid
    periodical_records.link_issue(
        db,
        item_id=item_id,
        publication_id=publication_id,
        volume=volume,
        issue_number=issue_number,
        issue_date=issue_date,
        cover_date_label=issue_date,
    )
    holdings.sync_item_holding(db, item_id)
    return item_id


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


def test_publication_catalogue_summarises_issues_copies_and_internal_gaps(db):
    publication_id = periodical_records.upsert_publication(
        db,
        title="Popular Science",
        issn="0161-7370",
        publisher="Bonnier Corporation",
        language="en",
    )
    first_id = _seed_issue(
        db, publication_id, "1", "2024-01", volume="7", cover_path="covers/first.jpg"
    )
    _seed_issue(db, publication_id, "3", "2024-03", volume="7")
    _seed_issue(db, publication_id, "4", "2024-04", volume="7", owned=0)
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, is_primary) VALUES (?, 2, 0)",
        (first_id,),
    )

    catalogue = periodical_records.publication_catalogue(db, publication_id)

    assert catalogue is not None
    assert [issue["issue_number"] for issue in catalogue["issues"]] == ["4", "3", "1"]
    assert catalogue["issue_count"] == 3
    assert catalogue["owned_issue_count"] == 2
    assert catalogue["wishlist_issue_count"] == 1
    assert catalogue["physical_copy_count"] == 3
    assert catalogue["extra_copy_count"] == 1
    assert catalogue["potential_gaps"] == [
        {"volume": "7", "start": 1, "end": 4, "missing": [2]}
    ]


def test_publication_gap_detection_keeps_volume_sequences_separate(db):
    publication_id = periodical_records.upsert_publication(
        db, title="Test Monthly", issn="2049-3630"
    )
    _seed_issue(db, publication_id, "1", "2023-01", volume="1", title="Test Monthly")
    _seed_issue(db, publication_id, "3", "2023-03", volume="1", title="Test Monthly")
    _seed_issue(db, publication_id, "1", "2024-01", volume="2", title="Test Monthly")
    _seed_issue(db, publication_id, "2", "2024-02", volume="2", title="Test Monthly")

    catalogue = periodical_records.publication_catalogue(db, publication_id)

    assert catalogue["potential_gaps"] == [
        {"volume": "1", "start": 1, "end": 3, "missing": [2]}
    ]


def test_magazine_publications_page_groups_issues_for_viewers(viewer_client, db):
    publication_id = periodical_records.upsert_publication(
        db, title="Popular Science", issn="0161-7370", publisher="Bonnier Corporation"
    )
    _seed_issue(db, publication_id, "42", "2024-06")

    response = viewer_client.get("/magazines")

    assert response.status_code == 200
    assert 'data-testid="magazine-publications"' in response.text
    assert "Popular Science" in response.text
    assert "ISSN 0161-7370" in response.text
    assert f'href="/magazines/publications/{publication_id}"' in response.text
    assert "1 issue" in response.text


def test_magazine_publication_page_shows_copies_and_possible_gaps(viewer_client, db):
    publication_id = periodical_records.upsert_publication(
        db, title="Popular Science", issn="0161-7370"
    )
    first_id = _seed_issue(db, publication_id, "1", "2024-01", volume="9")
    third_id = _seed_issue(db, publication_id, "3", "2024-03", volume="9")
    db.execute(
        "INSERT INTO item_copies (item_id, copy_number, is_primary) VALUES (?, 2, 0)",
        (first_id,),
    )

    response = viewer_client.get(f"/magazines/publications/{publication_id}")

    assert response.status_code == 200
    assert 'data-testid="magazine-publication-header"' in response.text
    assert "Possible missing issue numbers" in response.text
    assert "#2" in response.text
    assert "2 copies" in response.text
    assert f'href="/item/{third_id}"' in response.text


def test_missing_magazine_publication_redirects_to_catalogue(viewer_client):
    response = viewer_client.get(
        "/magazines/publications/999999",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/magazines"


def test_home_magazines_family_opens_publication_catalogue(viewer_client):
    response = viewer_client.get("/")

    assert response.status_code == 200
    assert 'data-media-family="magazines"' in response.text
    assert 'href="/magazines"' in response.text
