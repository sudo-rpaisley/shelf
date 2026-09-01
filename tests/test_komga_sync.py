"""Komga sync, library selection, mapping, cover and scale regressions."""
import asyncio
import json

import httpx
import respx

from app.services import covers, komga as komga_service
from app.services.komga import PAGE_SIZE, get_excluded_libraries, sync
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


def _books_page(*books, last=True, total_pages=1, number=0):
    return httpx.Response(
        200,
        json={
            "content": list(books),
            "last": last,
            "totalPages": total_pages,
            "number": number,
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
    def test_maps_komga_book_to_digital_comic_metadata(self, db):
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
            "covers": 0,
            "cover_errors": 0,
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
        assert row["media_type"] == "digital_comic"
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
        assert listing.url.params["size"] == str(PAGE_SIZE)
        payload = json.loads(listing.content)
        conditions = payload["condition"]["allOf"]
        assert {"libraryId": {"operator": "is", "value": "lib_comics"}} in conditions
        assert {"deleted": {"operator": "isFalse"}} in conditions

    @respx.mock
    def test_imports_komga_thumbnail_as_cover(self, db):
        _mock_single_library(_book())
        image = b"\xff\xd8" + (b"cover-data" * 150)
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            return_value=httpx.Response(200, content=image, headers={"Content-Type": "image/jpeg"})
        )

        stats = asyncio.run(sync(KOMGA, KEY))

        row = db.execute("SELECT id, cover_path FROM items").fetchone()
        assert stats["covers"] == 1
        assert stats["cover_errors"] == 0
        assert row["cover_path"] == f"covers/{row['id']}.jpg"
        assert (covers.COVERS_DIR / f"{row['id']}.jpg").read_bytes() == image
        cover_request = respx.calls[-1].request
        assert cover_request.headers["X-API-Key"] == KEY
        assert cover_request.headers["Accept"] == "image/jpeg"

    @respx.mock
    def test_missing_cover_is_retried_on_later_sync(self, db):
        _mock_single_library(_book())
        route = respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            side_effect=[
                httpx.Response(404),
                httpx.Response(200, content=b"x" * 1200),
            ]
        )
        first = asyncio.run(sync(KOMGA, KEY))
        second = asyncio.run(sync(KOMGA, KEY))
        assert first["covers"] == 0
        assert second["covers"] == 1
        assert route.call_count == 2
        assert db.execute("SELECT cover_path FROM items").fetchone()["cover_path"]

    @respx.mock
    def test_cover_timeout_does_not_abort_remaining_books(self, db):
        _mock_single_library(
            _book("book_1", "One", "9780000000103"),
            _book("book_2", "Two", "9780000000110"),
        )
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(
            side_effect=httpx.ReadTimeout("slow cover")
        )
        respx.get(f"{KOMGA}/api/v1/books/book_2/thumbnail").mock(
            return_value=httpx.Response(200, content=b"y" * 1200)
        )

        stats = asyncio.run(sync(KOMGA, KEY))
        assert stats["added"] == 2
        assert stats["cover_errors"] == 1
        assert stats["covers"] == 1
        rows = db.execute("SELECT title, cover_path FROM items ORDER BY title").fetchall()
        assert rows[0]["cover_path"] is None
        assert rows[1]["cover_path"] is not None

    @respx.mock
    def test_progress_advances_before_cover_batch_is_drained(self, db, monkeypatch):
        _mock_single_library(
            _book("book_1", "One", "9780000000103"),
            _book("book_2", "Two", "9780000000110"),
        )
        events = []

        async def fake_cover(client, komga_url, api_key, komga_id, item_id):
            events.append(f"cover:{komga_id}")
            return "missing"

        async def progress(current, total, title, status):
            events.append(f"progress:{current}")

        monkeypatch.setattr(komga_service, "_download_cover", fake_cover)
        stats = asyncio.run(sync(KOMGA, KEY, on_progress=progress))

        assert stats["added"] == 2
        assert events[:2] == ["progress:1", "progress:2"]
        assert set(events[2:]) == {"cover:book_1", "cover:book_2"}

    @respx.mock
    def test_cover_has_total_wall_clock_deadline(self, db, monkeypatch):
        _mock_single_library(_book())

        async def trickling_cover(client, komga_url, api_key, komga_id, item_id):
            await asyncio.sleep(1)
            return "downloaded"

        monkeypatch.setattr(komga_service, "_download_cover", trickling_cover)
        monkeypatch.setattr(komga_service, "COVER_WALL_TIMEOUT", 0.01)

        stats = asyncio.run(sync(KOMGA, KEY))

        assert stats["added"] == 1
        assert stats["covers"] == 0
        assert stats["cover_errors"] == 1

    @respx.mock
    def test_paginates_large_library_without_repeating_page(self, db):
        respx.get(f"{KOMGA}/api/v1/libraries").mock(
            return_value=httpx.Response(200, json=[{"id": "lib_comics", "name": "Comics"}])
        )
        listing = respx.post(f"{KOMGA}/api/v1/books/list").mock(
            side_effect=[
                _books_page(_book("book_1", "One", None), last=False, total_pages=2, number=0),
                _books_page(_book("book_2", "Two", None), last=True, total_pages=2, number=1),
            ]
        )
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(return_value=httpx.Response(404))
        respx.get(f"{KOMGA}/api/v1/books/book_2/thumbnail").mock(return_value=httpx.Response(404))

        stats = asyncio.run(sync(KOMGA, KEY))
        assert stats["added"] == 2
        assert listing.call_count == 2
        assert listing.calls[0].request.url.params["page"] == "0"
        assert listing.calls[1].request.url.params["page"] == "1"

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
    def test_physical_comic_and_komga_copy_coexist_and_link(self, db):
        physical_id = _insert_item(
            db,
            title="Physical Watchmen",
            isbn=ISBN,
            media_type="comic",
            source="manual",
        )
        db.execute("COMMIT")
        _mock_single_library(_book())
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(return_value=httpx.Response(404))

        first = asyncio.run(sync(KOMGA, KEY))
        assert first["added"] == 1
        rows = db.execute(
            "SELECT id, media_type, komga_id, source FROM items ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["id"] == physical_id
        assert rows[0]["media_type"] == "comic"
        assert rows[0]["komga_id"] is None
        assert rows[0]["source"] == "manual"
        assert rows[1]["media_type"] == "digital_comic"
        assert rows[1]["komga_id"] == "book_1"
        assert rows[1]["source"] == "komga"
        link = db.execute(
            "SELECT item_a_id, item_b_id FROM item_links"
        ).fetchone()
        assert {link["item_a_id"], link["item_b_id"]} == {physical_id, rows[1]["id"]}

        db.execute(
            "UPDATE items SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
            (rows[1]["id"],),
        )
        db.execute("COMMIT")
        second = asyncio.run(sync(KOMGA, KEY))
        assert second["unchanged"] == 1
        assert db.execute(
            "SELECT updated_at FROM items WHERE id = ?", (rows[1]["id"],)
        ).fetchone()["updated_at"] == "2000-01-01 00:00:00"

    @respx.mock
    def test_repairs_legacy_physical_item_that_was_adopted(self, db):
        physical_id = _insert_item(
            db,
            title="Old Adopted Watchmen",
            isbn=ISBN,
            media_type="comic",
            source="manual",
            komga_id="book_1",
            komga_library_id="lib_comics",
        )
        db.execute("COMMIT")
        _mock_single_library(_book())
        respx.get(f"{KOMGA}/api/v1/books/book_1/thumbnail").mock(return_value=httpx.Response(404))

        stats = asyncio.run(sync(KOMGA, KEY))
        assert stats["added"] == 1
        physical = db.execute(
            "SELECT media_type, komga_id, source FROM items WHERE id = ?", (physical_id,)
        ).fetchone()
        assert physical["media_type"] == "comic"
        assert physical["komga_id"] is None
        assert physical["source"] == "manual"
        digital = db.execute(
            "SELECT media_type, komga_id, source FROM items WHERE id != ?", (physical_id,)
        ).fetchone()
        assert digital["media_type"] == "digital_comic"
        assert digital["komga_id"] == "book_1"
        assert digital["source"] == "komga"

    @respx.mock
    def test_duplicate_komga_digital_isbn_is_skipped(self, db):
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
        assert by_id["lib_comics"]["media_type"] == "digital_comic"
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
            media_type="digital_comic",
            komga_id="k1",
            komga_library_id="lib_manga",
            source="komga",
        )
        adopted = _insert_item(
            db,
            title="Manual Digital",
            isbn=None,
            media_type="digital_comic",
            komga_id="k2",
            komga_library_id="lib_manga",
            source="manual",
        )
        keep = _insert_item(
            db,
            title="Keep",
            isbn=None,
            media_type="digital_comic",
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
