"""Komga sync, library selection, mapping and collision regressions."""
import asyncio
import json

import httpx
import respx

from app.services.komga import get_excluded_libraries, sync
from tests.conftest import _insert_item

KOMGA = "http://komga.example:25600"
KEY = "test-api-key"
ISBN = "9780000000998"


def _set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.execute("COMMIT")


def _libraries():
    return httpx.Response(
        200,
        json=[
            {"id": "lib_comics", "name": "Comics"},
            {"id": "lib_manga", "name": "Manga"},
        ],
    )


def _book(book_id="book_1", title="Watchmen", isbn=ISBN, library_id="lib_comics"):
    return {
        "id": book_id,
        "libraryId": library_id,
        "seriesId": "series_1",
        "seriesTitle": "Watchmen",
        "media": {"pagesCount": 416, "mediaType": "application/zip", "mediaProfile": "DIVINA"},
        "metadata": {
            "title": title,
            "authors": [
                {"name": "Alan Moore", "role": "writer"},
                {"name": "Dave Gibbons", "role": "penciller"},
            ],
            "isbn": isbn,
            "number": "1",
            "numberSort": 1.0,
            "releaseDate": "1987-09-01",
            "summary": "A landmark graphic novel.",
        },
    }


def _books_page(*books, last=True, total_pages=1):
    return httpx.Response(
        200,
        json={
            "content": list(books),
            "last": last,
            "totalPages": total_pages,
            "number": 0,
        },
    )


def _mock_single_library(*books):
    respx.get(f"{KOMGA}/api/v1/libraries").mock(
        return_value=httpx.Response(200, json=[{"id": "lib_comics", "name": "Comics"}])
    )
    respx.post(f"{KOMGA}/api/v1/books/list").mock(return_value=_books_page(*books))


class TestKomgaSettings:
    def test_excluded_libraries_default_and_json(self, db):
        assert get_excluded_libraries() == set()
        _set_setting(db, "komga_excluded_libraries", json.dumps(["lib_manga"]))
        assert get_excluded_libraries() == {"lib_manga"}

    def test_garbage_excluded_setting_is_safe(self, db):
        _set_setting(db, "komga_excluded_libraries", "not-json")
        assert get_excluded_libraries() == set()


class TestKomgaSync:
    @respx.mock
    def test_maps_komga_book_to_comic_metadata(self, db):
        _mock_single_library(_book())
        cover = respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            return_value=httpx.Response(404)
        )

        stats = asyncio.run(sync(KOMGA, KEY))

        assert stats == {
            "added": 1,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "errors": 0,
        }
        row = db.execute(
            """SELECT title, authors, isbn, media_type, series_name,
                      series_position, publish_year, page_count, description,
                      komga_id, komga_library_id, komga_series_id, source
               FROM items"""
        ).fetchone()
        assert row["title"] == "Watchmen"
        assert row["authors"] == "Alan Moore, Dave Gibbons"
        assert row["isbn"] == ISBN
        assert row["media_type"] == "comic"
        assert row["series_name"] == "Watchmen"
        assert row["series_position"] == 1.0
        assert row["publish_year"] == 1987
        assert row["page_count"] == 416
        assert row["description"] == "A landmark graphic novel."
        assert row["komga_id"] == "book_1"
        assert row["komga_library_id"] == "lib_comics"
        assert row["komga_series_id"] == "series_1"
        assert row["source"] == "komga"
        assert cover.calls[0].request.headers["X-API-Key"] == KEY

        listing = respx.calls[1].request
        assert listing.headers["X-API-Key"] == KEY
        payload = json.loads(listing.content)
        conditions = payload["condition"]["allOf"]
        assert {"libraryId": {"operator": "is", "value": "lib_comics"}} in conditions
        assert {"deleted": {"operator": "isFalse"}} in conditions

    @respx.mock
    def test_excluded_library_is_not_listed_for_sync(self, db):
        _set_setting(db, "komga_excluded_libraries", json.dumps(["lib_manga"]))
        respx.get(f"{KOMGA}/api/v1/libraries").mock(return_value=_libraries())
        list_route = respx.post(f"{KOMGA}/api/v1/books/list").mock(
            return_value=_books_page(_book())
        )
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            return_value=httpx.Response(404)
        )

        stats = asyncio.run(sync(KOMGA, KEY))

        assert stats["added"] == 1
        assert list_route.call_count == 1
        assert b"lib_comics" in list_route.calls[0].request.content
        assert b"lib_manga" not in list_route.calls[0].request.content

    @respx.mock
    def test_adopts_existing_manual_comic_by_isbn_and_second_sync_is_unchanged(self, db):
        existing_id = _insert_item(
            db, title="Manual Watchmen", isbn=ISBN, media_type="comic"
        )
        db.execute("COMMIT")
        _mock_single_library(_book())
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            return_value=httpx.Response(404)
        )

        first = asyncio.run(sync(KOMGA, KEY))
        assert first["added"] == 0 and first["updated"] == 1
        row = db.execute(
            "SELECT id, komga_id, komga_library_id, source FROM items"
        ).fetchone()
        assert row["id"] == existing_id
        assert row["komga_id"] == "book_1"
        assert row["komga_library_id"] == "lib_comics"
        assert row["source"] == "manual"

        db.execute(
            "UPDATE items SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
            (existing_id,),
        )
        db.execute("COMMIT")
        second = asyncio.run(sync(KOMGA, KEY))
        assert second["unchanged"] == 1
        assert db.execute(
            "SELECT updated_at FROM items WHERE id = ?", (existing_id,)
        ).fetchone()["updated_at"] == "2000-01-01 00:00:00"

    @respx.mock
    def test_duplicate_komga_isbn_is_skipped(self, db):
        _mock_single_library(
            _book("book_1", "First", ISBN),
            _book("book_2", "Duplicate", ISBN),
        )
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            return_value=httpx.Response(404)
        )

        stats = asyncio.run(sync(KOMGA, KEY))
        assert stats["added"] == 1
        assert stats["skipped"] == 1
        assert db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 1


class TestKomgaLibraryEndpoints:
    @respx.mock
    def test_list_libraries_uses_saved_api_key(self, admin_client, db):
        _set_setting(db, "komga_url", KOMGA)
        _set_setting(db, "komga_api_key", KEY)
        _set_setting(db, "komga_excluded_libraries", json.dumps(["lib_manga"]))
        route = respx.get(f"{KOMGA}/api/v1/libraries").mock(return_value=_libraries())

        data = admin_client.get("/api/sync/komga/libraries").json()
        assert data["ok"] is True
        by_id = {library["id"]: library for library in data["libraries"]}
        assert by_id["lib_comics"]["included"] is True
        assert by_id["lib_manga"]["included"] is False
        assert route.calls[0].request.headers["X-API-Key"] == KEY

    def test_save_library_selection_validates_shape(self, admin_client):
        ok = admin_client.post(
            "/api/sync/komga/libraries", json={"excluded": ["lib_manga"]}
        )
        assert ok.json()["ok"] is True
        bad = admin_client.post(
            "/api/sync/komga/libraries", json={"excluded": [123]}
        )
        assert bad.json()["ok"] is False

    def test_cleanup_deletes_synced_but_detaches_adopted_items(self, admin_client, db):
        _set_setting(db, "komga_excluded_libraries", json.dumps(["lib_manga"]))
        synced = _insert_item(
            db,
            title="Synced",
            isbn=None,
            media_type="comic",
            komga_id="k1",
            komga_library_id="lib_manga",
            source="komga",
        )
        adopted = _insert_item(
            db,
            title="Manual",
            isbn=None,
            media_type="comic",
            komga_id="k2",
            komga_library_id="lib_manga",
            source="manual",
        )
        keep = _insert_item(
            db,
            title="Keep",
            isbn=None,
            media_type="comic",
            komga_id="k3",
            komga_library_id="lib_comics",
            source="komga",
        )
        db.execute("COMMIT")

        data = admin_client.post("/api/sync/komga/libraries/cleanup").json()
        assert data == {"ok": True, "deleted": 1, "detached": 1}
        assert db.execute(
            "SELECT 1 FROM items WHERE id = ?", (synced,)
        ).fetchone() is None
        adopted_row = db.execute(
            "SELECT komga_id, komga_library_id FROM items WHERE id = ?", (adopted,)
        ).fetchone()
        assert adopted_row["komga_id"] is None
        assert adopted_row["komga_library_id"] is None
        assert db.execute(
            "SELECT komga_id FROM items WHERE id = ?", (keep,)
        ).fetchone()["komga_id"] == "k3"
