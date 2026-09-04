"""Komga series identity regressions.

Komga is the source of truth for Komga-backed Digital Comics and Manga. Shelf
must keep Komga's series title and series ID intact instead of trying to merge
similarly named source series into a new canonical Shelf series.
"""

import asyncio

import httpx
import respx

from app.services.komga import sync

KOMGA = "http://komga.example:25600"
KEY = "test-api-key"


def _book(book_id: str, series_id: str, series_title: str, number: int, isbn: str) -> dict:
    return {
        "id": book_id,
        "libraryId": "lib_manga",
        "seriesId": series_id,
        "seriesTitle": series_title,
        "media": {"pagesCount": 200},
        "metadata": {
            "title": f"{series_title} book {number}",
            "authors": [{"name": "Example Author", "role": "writer"}],
            "isbn": isbn,
            "number": str(number),
            "numberSort": float(number),
        },
    }


def _mock_sync(books: list[dict]) -> None:
    respx.get(f"{KOMGA}/api/v1/libraries").mock(
        return_value=httpx.Response(200, json=[{"id": "lib_manga", "name": "Manga"}])
    )
    respx.post(f"{KOMGA}/api/v1/books/list").mock(
        return_value=httpx.Response(
            200,
            json={"content": books, "last": True, "totalPages": 1, "number": 0},
        )
    )
    for book in books:
        respx.get(f"{KOMGA}/api/v1/books/{book['id']}/thumbnail").mock(
            return_value=httpx.Response(404)
        )


@respx.mock
def test_sync_preserves_komga_series_titles_instead_of_canonicalising(db, admin_client):
    books = [
        _book("op_3", "series_3", "One Piece (3)", 1, "9780000000301"),
        _book("op_21", "series_21", "One Piece (21)", 1, "9780000000325"),
    ]
    _mock_sync(books)

    stats = asyncio.run(sync(KOMGA, KEY))
    assert stats["added"] == 2

    rows = db.execute(
        "SELECT series_name, komga_series_id, media_type FROM items ORDER BY komga_id"
    ).fetchall()
    assert [(row["series_name"], row["komga_series_id"]) for row in rows] == [
        ("One Piece (21)", "series_21"),
        ("One Piece (3)", "series_3"),
    ]
    assert {row["media_type"] for row in rows} == {"digital_manga"}

    html = admin_client.get("/browse?media_type_filter=digital_manga").text
    assert 'data-series-group="One Piece (3)"' in html
    assert 'data-series-group="One Piece (21)"' in html
    assert "One Piece (3)" in html
    assert "One Piece (21)" in html


@respx.mock
def test_same_title_different_komga_series_ids_stay_separate(db, admin_client):
    books = [
        _book("a1", "series_a", "Shared Name", 1, "9780000000400"),
        _book("a2", "series_a", "Shared Name", 2, "9780000000401"),
        _book("b1", "series_b", "Shared Name", 1, "9780000000402"),
    ]
    _mock_sync(books)
    asyncio.run(sync(KOMGA, KEY))

    html = admin_client.get("/browse?media_type_filter=digital_manga").text
    # Grid and list are both rendered, so each Komga series appears twice.
    assert html.count('data-series-group="Shared Name"') == 4
    assert "komga_series_id=series_a" in html
    assert "komga_series_id=series_b" in html

    detail = admin_client.get(
        "/series/detail?name=Shared%20Name&media_type=digital_manga&komga_series_id=series_a"
    ).text
    assert 'data-series-position="1.0"' in detail
    assert 'data-series-position="2.0"' in detail
    assert detail.count('data-series-position="') == 2
