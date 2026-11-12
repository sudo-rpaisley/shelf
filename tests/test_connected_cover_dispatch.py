"""New screen-media types must never ask the book artwork providers."""

from unittest.mock import AsyncMock

import pytest

from app.services import covers, provider_result
from tests.conftest import _insert_item


@pytest.mark.asyncio
async def test_digital_video_searches_movie_posters_not_books(monkeypatch):
    book_search = AsyncMock(side_effect=AssertionError("A film is not a book"))
    monkeypatch.setattr(covers, "search_book_covers", book_search)
    movies = AsyncMock(return_value=provider_result.found("tmdb", [{"tmdb_id": 42, "title": "A film"}]))
    posters = AsyncMock(return_value=[{"file_path": "/poster.jpg", "iso_639_1": "en"}])
    monkeypatch.setattr(covers.tmdb, "search_movies", movies)
    monkeypatch.setattr(covers.tmdb, "search_posters", posters)
    result = await covers.search_covers({"media_type": "digital_video", "title": "A film"}, "A film", object(),
                                       creds={"tmdb_api_key": "fixture-key"})
    assert result.found and result.payload[0]["url"] == "https://image.tmdb.org/t/p/w500/poster.jpg"
    movies.assert_awaited_once()
    posters.assert_awaited_once()
    book_search.assert_not_awaited()
    assert covers.required_credentials("digital_video") == ("tmdb_api_key",)


@pytest.mark.asyncio
async def test_tv_series_is_explicitly_manual_without_any_provider_call(monkeypatch):
    for name in ("search_book_covers", "_tmdb_candidates", "_igdb_candidates"):
        monkeypatch.setattr(covers, name, AsyncMock(side_effect=AssertionError("Unsupported TV search must not call a provider")))
    result = await covers.search_covers({"media_type": "tv_series", "title": "A series"}, "A series", object(), creds={})
    assert not result.found and result.provider == "manual"


@pytest.mark.parametrize("media_type", ["digital_video", "tv_series"])
def test_screen_cover_picker_explains_setup_and_keeps_manual_controls(editor_client, db, monkeypatch, media_type):
    monkeypatch.setattr(covers, "search_book_covers", AsyncMock(side_effect=AssertionError("No book fallback")))
    item_id = _insert_item(db, title="A screen title", media_type=media_type, isbn=None)
    db.commit()
    response = editor_client.get(f"/api/items/{item_id}/cover-search")
    assert response.status_code == 200
    assert ("Digital video cover search needs a TMDb API key" if media_type == "digital_video"
            else "TV series cover search is not available") in response.text
    assert 'data-testid="cover-url"' in response.text and 'data-testid="cover-upload"' in response.text


@pytest.mark.parametrize("media_type", ["digital_video", "tv_series"])
def test_screen_isbn_retry_cannot_install_a_book_cover(editor_client, db, monkeypatch, media_type):
    download = AsyncMock(side_effect=AssertionError("A screen title must not use book ISBN artwork"))
    monkeypatch.setattr(covers, "download_cover", download)
    item_id = _insert_item(db, media_type=media_type)
    db.commit()
    response = editor_client.post(f"/api/items/{item_id}/retry-cover")
    assert response.status_code == 200 and response.json()["ok"] is False
    assert "image URL" in response.json()["message"]
    download.assert_not_awaited()


@pytest.mark.parametrize("media_type", ["digital_video", "tv_series"])
@pytest.mark.parametrize("has_preview", [False, True])
def test_manual_screen_add_does_not_adopt_or_download_isbn_artwork(editor_client, db, monkeypatch, media_type, has_preview):
    from app import config

    isbn = "9780441172719"
    preview = config.COVERS_DIR / f"preview_{isbn}.jpg"
    preview_bytes = b"\xff\xd8\xff" + b"book preview" * 100
    if has_preview:
        preview.write_bytes(preview_bytes)
    download = AsyncMock(return_value="covers/wrong-book-cover.jpg")
    monkeypatch.setattr(covers, "download_cover", download)
    response = editor_client.post("/api/items/manual", data={
        "title": "A screen work", "media_type": media_type, "isbn": isbn,
    })
    assert response.status_code == 200 and 'data-scan-status="added"' in response.text
    row = db.execute("SELECT id, cover_path FROM items_live WHERE title = 'A screen work'").fetchone()
    assert row["cover_path"] is None
    assert not (config.COVERS_DIR / f"{row['id']}.jpg").exists()
    download.assert_not_awaited()
    if has_preview:
        assert preview.read_bytes() == preview_bytes


@pytest.mark.parametrize("media_type", ["digital_video", "tv_series"])
def test_manual_screen_add_still_accepts_explicit_cover_uploads(editor_client, db, monkeypatch, media_type):
    from app import config

    content = b"\xff\xd8\xff" + b"chosen screen artwork" * 100
    download = AsyncMock(side_effect=AssertionError("An upload must not trigger book artwork"))
    monkeypatch.setattr(covers, "download_cover", download)
    response = editor_client.post("/api/items/manual", data={
        "title": "A screen work", "media_type": media_type, "isbn": "9780441172719",
    }, files={"cover": ("chosen.jpg", content, "image/jpeg")})
    assert response.status_code == 200 and 'data-scan-status="added"' in response.text
    row = db.execute("SELECT id, cover_path FROM items_live WHERE title = 'A screen work'").fetchone()
    assert row["cover_path"] == f"covers/{row['id']}.jpg"
    assert (config.COVERS_DIR / f"{row['id']}.jpg").read_bytes() == content
    download.assert_not_awaited()
