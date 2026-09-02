"""Komga series-name normalization and Browse grouping regressions."""

import asyncio

import httpx
import respx

from app.services.komga import _canonical_series_name, sync
from tests.conftest import _insert_item

KOMGA = "http://komga.example:25600"
KEY = "test-api-key"


def _book(book_id: str, volume: int, isbn: str) -> dict:
    return {
        "id": book_id,
        "libraryId": "lib_manga",
        "seriesId": f"series_{volume}",
        "seriesTitle": f"One Piece ({volume})",
        "media": {"pagesCount": 200},
        "metadata": {
            "title": f"One Piece Vol. {volume}",
            "authors": [{"name": "Eiichiro Oda", "role": "writer"}],
            "isbn": isbn,
            "number": str(volume),
            "numberSort": float(volume),
        },
    }


def test_canonical_series_name_strips_komga_volume_suffix_but_keeps_year_runs():
    assert _canonical_series_name("One Piece (3)") == "One Piece"
    assert _canonical_series_name("One Piece (108)") == "One Piece"
    assert _canonical_series_name("  One Piece (21)  ") == "One Piece"
    assert _canonical_series_name("Batman (2016)") == "Batman (2016)"
    assert _canonical_series_name("Watchmen") == "Watchmen"
    assert _canonical_series_name(None) is None


@respx.mock
def test_komga_volume_series_repair_into_one_browse_group(db, admin_client):
    books = [
        _book("op_3", 3, "9780000000301"),
        _book("op_21", 21, "9780000000325"),
        _book("op_25", 25, "9780000000349"),
    ]

    # Simulate an existing installation synced before the normalization fix.
    old_item = _insert_item(
        db,
        title="One Piece Vol. 3",
        authors="Eiichiro Oda",
        isbn="9780000000301",
        media_type="digital_comic",
        series_name="One Piece (3)",
        series_position=3.0,
        page_count=200,
        komga_id="op_3",
        komga_library_id="lib_manga",
        komga_series_id="series_3",
        source="komga",
    )
    db.commit()

    respx.get(f"{KOMGA}/api/v1/libraries").mock(
        return_value=httpx.Response(
            200, json=[{"id": "lib_manga", "name": "Manga"}]
        )
    )
    respx.post(f"{KOMGA}/api/v1/books/list").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": books,
                "last": True,
                "totalPages": 1,
                "number": 0,
            },
        )
    )
    for book in books:
        respx.get(f"{KOMGA}/api/v1/books/{book['id']}/thumbnail").mock(
            return_value=httpx.Response(404)
        )

    stats = asyncio.run(sync(KOMGA, KEY))

    assert stats["updated"] == 1
    assert stats["added"] == 2
    rows = db.execute(
        "SELECT id, series_name FROM items WHERE komga_id IS NOT NULL ORDER BY id"
    ).fetchall()
    assert len(rows) == 3
    assert {row["series_name"] for row in rows} == {"One Piece"}
    assert db.execute(
        "SELECT series_name FROM items WHERE id = ?", (old_item,)
    ).fetchone()["series_name"] == "One Piece"

    html = admin_client.get("/browse?media_type_filter=digital_comic").text
    # The grid and list variants are both rendered; Alpine shows one at a time.
    assert html.count('data-series-group="One Piece"') == 2
    assert "3 in series" in html

    detail_html = admin_client.get(
        "/series/detail?name=One%20Piece&media_type=digital_comic"
    ).text
    assert "3 volumes" in detail_html
    assert "Vol. 3" in detail_html
    assert "Vol. 21" in detail_html
    assert "Vol. 25" in detail_html
