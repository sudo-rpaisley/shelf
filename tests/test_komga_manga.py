"""Komga Comic/Manga library classification regressions."""
import asyncio
import json

import httpx
import respx

from app.config import MEDIA_FAMILIES, MEDIA_TYPES
from app.media_types import is_digital_media, is_physical_media
from app.services import komga, media_groups
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


def _book(book_id="manga_1", title="Akira", isbn=ISBN):
    return {
        "id": book_id,
        "seriesId": "series_akira",
        "seriesTitle": "Akira",
        "media": {"pagesCount": 362},
        "metadata": {
            "title": title,
            "authors": [{"name": "Katsuhiro Otomo", "role": "writer"}],
            "isbn": isbn,
            "number": "1",
            "numberSort": 1.0,
            "releaseDate": "1984-09-14",
            "summary": "Neo-Tokyo.",
        },
    }


def _books_page(*books):
    return httpx.Response(
        200,
        json={"content": list(books), "last": True, "totalPages": 1, "number": 0},
    )


def _mock_library(library_id, name, *books):
    respx.get(f"{KOMGA}/api/v1/libraries").mock(
        return_value=httpx.Response(200, json=[{"id": library_id, "name": name}])
    )
    respx.post(f"{KOMGA}/api/v1/books/list").mock(return_value=_books_page(*books))
    for book in books:
        respx.get(f"{KOMGA}/api/v1/books/{book['id']}/thumbnail").mock(
            return_value=httpx.Response(404)
        )


class TestMangaMediaTypes:
    def test_manga_is_first_class_physical_and_digital_media(self):
        assert MEDIA_TYPES["manga"] == "Manga"
        assert MEDIA_TYPES["digital_manga"] == "Digital Manga"
        assert MEDIA_FAMILIES["manga"]["types"] == ("manga", "digital_manga")
        assert is_physical_media("manga") is True
        assert is_digital_media("digital_manga") is True

    def test_related_format_family_keeps_manga_separate_from_comics(self):
        assert media_groups.family_for("manga") == "manga"
        assert media_groups.family_for("digital_manga") == "manga"
        assert media_groups.family_for("comic") == "comic"
        assert media_groups.family_for("digital_comic") == "comic"


class TestKomgaLibraryTypeSettings:
    def test_library_name_suggests_manga_but_defaults_other_names_to_comic(self):
        assert komga.suggest_library_media_type("Manga") == "digital_manga"
        assert komga.suggest_library_media_type("Japanese Manga") == "digital_manga"
        assert komga.suggest_library_media_type("Graphic Novels") == "digital_comic"

    def test_explicit_mapping_overrides_name_suggestion(self, db):
        _set_setting(
            db,
            "komga_library_media_types",
            json.dumps({"lib_manga": "digital_comic", "lib_comics": "digital_manga"}),
        )
        mappings = komga.get_library_media_types()
        assert komga.library_media_type("lib_manga", "Manga", mappings) == "digital_comic"
        assert komga.library_media_type("lib_comics", "Comics", mappings) == "digital_manga"

    def test_invalid_mapping_values_are_ignored(self, db):
        _set_setting(
            db,
            "komga_library_media_types",
            json.dumps({"good": "digital_manga", "bad": "ebook"}),
        )
        assert komga.get_library_media_types() == {"good": "digital_manga"}

    @respx.mock
    def test_library_endpoint_returns_suggested_and_explicit_types(self, admin_client, db):
        _set_setting(db, "komga_url", KOMGA)
        _set_setting(db, "komga_api_key", KEY)
        _set_setting(
            db,
            "komga_library_media_types",
            json.dumps({"lib_comics": "digital_manga"}),
        )
        respx.get(f"{KOMGA}/api/v1/libraries").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": "lib_comics", "name": "Comics"},
                    {"id": "lib_manga", "name": "Manga"},
                ],
            )
        )

        data = admin_client.get("/api/sync/komga/libraries").json()
        by_id = {library["id"]: library for library in data["libraries"]}
        assert by_id["lib_comics"]["media_type"] == "digital_manga"
        assert by_id["lib_comics"]["explicit_media_type"] is True
        assert by_id["lib_manga"]["media_type"] == "digital_manga"
        assert by_id["lib_manga"]["explicit_media_type"] is False

    def test_saving_mapping_reclassifies_existing_items_without_duplicates(self, admin_client, db):
        item_id = _insert_item(
            db,
            title="Akira",
            isbn=ISBN,
            media_type="digital_comic",
            komga_id="manga_1",
            komga_library_id="lib_manga",
            source="komga",
        )
        db.execute("COMMIT")

        response = admin_client.post(
            "/api/sync/komga/libraries",
            json={
                "excluded": [],
                "media_types": {"lib_manga": "digital_manga"},
            },
        )
        data = response.json()
        assert data["ok"] is True
        assert data["reclassified"] == 1
        rows = db.execute("SELECT id, media_type FROM items").fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == item_id
        assert rows[0]["media_type"] == "digital_manga"

    def test_saving_mapping_rejects_unknown_media_type(self, admin_client):
        data = admin_client.post(
            "/api/sync/komga/libraries",
            json={"excluded": [], "media_types": {"lib": "ebook"}},
        ).json()
        assert data["ok"] is False


class TestKomgaMangaSync:
    @respx.mock
    def test_manga_named_library_imports_as_digital_manga(self, db):
        _mock_library("lib_manga", "Manga", _book())

        stats = asyncio.run(komga.sync(KOMGA, KEY))

        assert stats["added"] == 1
        row = db.execute(
            "SELECT media_type, komga_library_id, source FROM items"
        ).fetchone()
        assert row["media_type"] == "digital_manga"
        assert row["komga_library_id"] == "lib_manga"
        assert row["source"] == "komga"

    @respx.mock
    def test_explicit_comic_override_wins_for_manga_named_library(self, db):
        _set_setting(
            db,
            "komga_library_media_types",
            json.dumps({"lib_manga": "digital_comic"}),
        )
        _mock_library("lib_manga", "Manga", _book())

        stats = asyncio.run(komga.sync(KOMGA, KEY))

        assert stats["added"] == 1
        assert db.execute("SELECT media_type FROM items").fetchone()["media_type"] == "digital_comic"

    @respx.mock
    def test_mapping_change_updates_existing_item_once_then_stays_unchanged(self, db):
        _set_setting(
            db,
            "komga_library_media_types",
            json.dumps({"lib_manga": "digital_comic"}),
        )
        _mock_library("lib_manga", "Manga", _book())
        first = asyncio.run(komga.sync(KOMGA, KEY))
        assert first["added"] == 1

        _set_setting(
            db,
            "komga_library_media_types",
            json.dumps({"lib_manga": "digital_manga"}),
        )
        second = asyncio.run(komga.sync(KOMGA, KEY))
        third = asyncio.run(komga.sync(KOMGA, KEY))

        assert second["updated"] == 1
        assert third["unchanged"] == 1
        rows = db.execute("SELECT id, media_type FROM items").fetchall()
        assert len(rows) == 1
        assert rows[0]["media_type"] == "digital_manga"

    @respx.mock
    def test_physical_and_digital_manga_coexist_and_auto_link(self, db):
        physical_id = _insert_item(
            db,
            title="Akira",
            authors="Katsuhiro Otomo",
            isbn=ISBN,
            media_type="manga",
            source="manual",
        )
        db.execute("COMMIT")
        _mock_library("lib_manga", "Manga", _book())

        stats = asyncio.run(komga.sync(KOMGA, KEY))

        assert stats["added"] == 1
        digital = db.execute(
            "SELECT id, media_type FROM items WHERE komga_id = 'manga_1'"
        ).fetchone()
        assert digital["media_type"] == "digital_manga"
        link = db.execute(
            "SELECT item_a_id, item_b_id, link_type FROM item_links"
        ).fetchone()
        assert link["link_type"] == "format"
        assert {link["item_a_id"], link["item_b_id"]} == {physical_id, digital["id"]}

    @respx.mock
    def test_same_title_comic_does_not_auto_link_to_manga(self, db):
        comic_id = _insert_item(
            db,
            title="Akira",
            authors="Katsuhiro Otomo",
            isbn=ISBN,
            media_type="comic",
            source="manual",
        )
        db.execute("COMMIT")
        _mock_library("lib_manga", "Manga", _book())

        asyncio.run(komga.sync(KOMGA, KEY))

        digital = db.execute(
            "SELECT id FROM items WHERE komga_id = 'manga_1'"
        ).fetchone()
        assert db.execute(
            """SELECT 1 FROM item_links
               WHERE (item_a_id = ? AND item_b_id = ?)
                  OR (item_a_id = ? AND item_b_id = ?)""",
            (comic_id, digital["id"], digital["id"], comic_id),
        ).fetchone() is None
