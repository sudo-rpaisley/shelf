"""Additive offline native-feed contracts. Authored, not runtime-qualified."""

from urllib.parse import parse_qs

import httpx
import pytest

from app.services import continue_connectors as svc
from app.services.source_progress import ProgressError


TOKEN = "synthetic-personal-continue-token"
ROOT = "https://native.invalid/proxy"
DATE = "2026-01-01T12:00:00Z"


def _source(provider, **fields):
    return {"provider": provider, "root": ROOT, "excluded": [], "kinds": {}, "libraries": [], **fields}


def _rom(number=7, **progress):
    return {"id": number, "platform_id": 4, "name": f"Native game {number}", "platform_display_name": "Handheld",
            "rom_user": {"user_id": 42, "rom_id": number, "last_played": DATE, "hidden": False,
                         "now_playing": False, "completion": 0, **progress}}


def _book(number="book-1", **progress):
    return {"id": number, "libraryId": "comics", "seriesId": "series", "seriesTitle": "Native series",
            "metadata": {"title": "Native comic"}, "media": {"pagesCount": 40},
            "readProgress": {"page": 13, "completed": False, "lastModified": DATE, **progress}}


def _audio(item="audio-1", *, episode=None, **values):
    return {"userId": "self", "libraryItemId": item, "episodeId": episode, "currentTime": 125.5,
            "duration": 1000, "isFinished": False, "hideFromContinueListening": False,
            "ebookProgress": 0.75, "lastUpdate": 1767268800000, **values}


async def _fetch(provider, pages, *, identity=None, fields=None):
    requests = []
    who_path = {"romm": "/api/users/me", "komga": "/api/v2/users/me", "jellyfin": "/Users/Me", "abs": "/api/me"}[provider]
    identity = identity or ({"id": 42} if provider == "romm" else {"Id": "self"} if provider == "jellyfin" else {"id": "self"})
    def send(request):
        assert request.method == "GET"
        assert TOKEN not in str(request.url)
        assert request.headers.get("X-Emby-Token" if provider == "jellyfin" else "X-API-Key" if provider == "komga" else "Authorization") == (
            TOKEN if provider in {"jellyfin", "komga"} else f"Bearer {TOKEN}")
        requests.append(request)
        path = request.url.path.removeprefix("/proxy")
        payload = identity if path == who_path else pages[path]
        return payload if isinstance(payload, httpx.Response) else httpx.Response(200, json=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(send), follow_redirects=False, trust_env=False) as client:
        reader = svc.Reader(client, _source(provider, **(fields or {})), TOKEN, lambda: None)
        result = await svc.fetch(reader, "42" if provider == "romm" else "self")
    return result, requests


@pytest.mark.asyncio
async def test_romm_uses_native_continue_query_and_does_not_invent_completion_or_playtime():
    result, calls = await _fetch("romm", {"/api/roms": {"items": [_rom(), _rom(8, completion=37, now_playing=True)]}})
    first, second = result["entries"]
    assert first["percent"] is None and first["position"] is None and first["unit"] is None
    assert first["updated_at"] == DATE and first["external_id"] == "7"
    assert second["percent"] == 37 and second["now_playing"] is True
    params = parse_qs(calls[-1].url.query.decode())
    assert params["last_played"] == ["true"] and params["order_by"] == ["last_played"]
    assert params["order_dir"] == ["desc"] and params["limit"] == ["24"]
    assert params["with_total"] == params["with_filter_values"] == params["with_rom_id_index"] == ["false"]
    assert len(calls) == 2 and not result["truncated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"user_id": 43}, {"rom_id": 8}])
async def test_romm_rejects_progress_owned_by_another_user_or_rom(change):
    with pytest.raises(ProgressError, match="identity_mismatch"):
        await _fetch("romm", {"/api/roms": {"items": [_rom(**change)]}})


@pytest.mark.asyncio
async def test_romm_honors_hidden_and_platform_scope_and_marks_bounded_snapshots():
    result, _ = await _fetch("romm", {"/api/roms": {"items": [_rom(hidden=True), _rom(8)]}}, fields={"excluded": ["4"]})
    assert result == {"entries": [], "truncated": False}
    result, _ = await _fetch("romm", {"/api/roms": {"items": [_rom(i) for i in range(1, 25)]}})
    assert len(result["entries"]) == 24 and result["truncated"]


@pytest.mark.asyncio
async def test_komga_uses_read_progress_sort_not_next_unread_ondeck_and_retains_pages():
    result, calls = await _fetch("komga", {"/api/v1/books": {"content": [_book()], "last": False}})
    row = result["entries"][0]
    assert (row["unit"], row["position"], row["total"], row["percent"]) == ("pages", 13, 40, 32.5)
    assert row["updated_at"] == DATE and row["library_id"] == "comics"
    params = parse_qs(calls[-1].url.query.decode())
    assert params == {"read_status": ["IN_PROGRESS"], "sort": ["readProgress.lastModified,desc"], "page": ["0"], "size": ["24"]}
    assert all("ondeck" not in request.url.path for request in calls) and result["truncated"]


@pytest.mark.asyncio
async def test_komga_naive_native_clock_is_displayable_but_not_given_an_invented_timezone():
    result, _ = await _fetch("komga", {"/api/v1/books": {"content": [_book(lastModified="2026-01-01T12:00:00")], "last": True}})
    assert result["entries"][0]["updated_at"] == "2026-01-01T12:00:00"
    assert result["entries"][0]["sort_at"] is None


@pytest.mark.asyncio
async def test_jellyfin_native_resume_keeps_episode_track_and_movie_separate_and_preserves_ticks():
    rows = [{"Id": kind, "Type": kind, "Name": kind, "RunTimeTicks": 1200 * 10_000_000,
             "UserData": {"Played": False, "PlaybackPositionTicks": 321 * 10_000_000, "LastPlayedDate": DATE}}
            for kind in ("Movie", "Episode", "Audio")]
    result, calls = await _fetch("jellyfin", {"/Users/self/Items/Resume": {"Items": rows, "TotalRecordCount": 3}})
    assert [row["kind"] for row in result["entries"]] == ["movie", "episode", "track"]
    assert all(row["position"] == 321 and row["unit"] == "seconds" for row in result["entries"])
    assert result["entries"][0]["native_position"] == 321 * 10_000_000
    params = parse_qs(calls[-1].url.query.decode())
    assert params["EnableUserData"] == ["true"] and "Fields" not in params
    assert "ParentId" not in params and all(row["library_id"] is None for row in result["entries"])


@pytest.mark.asyncio
async def test_selected_jellyfin_scope_is_bounded_and_each_query_has_its_actual_parent():
    result, calls = await _fetch("jellyfin", {"/Users/self/Items/Resume": {"Items": [], "TotalRecordCount": 0}},
                                 fields={"libraries": [f"library-{i}" for i in range(10)]})
    assert result == {"entries": [], "truncated": True}
    assert len(calls) == 9
    assert [parse_qs(call.url.query.decode())["ParentId"][0] for call in calls[1:]] == [f"library-{i}" for i in range(8)]


@pytest.mark.asyncio
async def test_jellyfin_book_never_derives_reading_progress_from_playback_ticks():
    book = {"Id": "book", "Type": "Book", "Name": "Native book", "RunTimeTicks": 1000,
            "UserData": {"Played": False, "PlaybackPositionTicks": 500, "LastPlayedDate": DATE}}
    result, _ = await _fetch("jellyfin", {"/Users/self/Items/Resume": {"Items": [book], "TotalRecordCount": 1}})
    row = result["entries"][0]
    assert row["percent"] is None and row["unit"] is None and row["position"] is None
    assert row["native_position"] == 500 and row["native_total"] == 1000


@pytest.mark.asyncio
async def test_abs_uses_personalized_membership_and_me_progress_without_ebook_audio_or_episode_conflation():
    book = {"id": "audio-1", "libraryId": "audio", "media": {"metadata": {"title": "Native audio", "authorName": "Author"}}}
    ebook = {"id": "ebook-1", "libraryId": "audio", "media": {"metadata": {"title": "Native ebook"}}}
    episode = {**book, "recentEpisode": {"id": "ep-1", "title": "Episode one"}}
    identity = {"id": "self", "mediaProgress": [_audio(), _audio("ebook-1", ebookProgress=0.3), _audio(episode="ep-1")]}
    shelves = [{"id": "continue-listening", "type": "book", "total": 1, "entities": [book]},
               {"id": "continue-reading", "type": "book", "total": 1, "entities": [ebook]},
               {"id": "continue-listening", "type": "episode", "total": 1, "entities": [episode]},
               {"id": "continue-series", "type": "series", "total": 1, "entities": [{"not": "resume progress"}]}]
    result, calls = await _fetch("abs", {"/api/libraries": {"libraries": [{"id": "audio"}]},
                                        "/api/libraries/audio/personalized": shelves}, identity=identity)
    audio, reading, podcast = result["entries"]
    assert (audio["unit"], audio["position"], audio["percent"]) == ("seconds", 125.5, 12.55)
    assert (reading["unit"], reading["position"], reading["total"], reading["percent"]) == ("fraction", 0.3, 1, 30)
    assert podcast["episode_id"] == "ep-1" and podcast["kind"] == "episode" and podcast["title"] == "Episode one"
    assert podcast["external_id"] == audio["external_id"] and len(calls) == 3
    assert all("items-in-progress" not in call.url.path for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("change,code", [({"hideFromContinueListening": True}, None), ({"isFinished": True}, None),
                                         ({"userId": "other"}, "identity_mismatch")])
async def test_abs_refuses_hidden_finished_or_other_users_progress(change, code):
    pages = {"/api/libraries": {"libraries": [{"id": "audio"}]}, "/api/libraries/audio/personalized": [
        {"id": "continue-listening", "type": "book", "total": 1, "entities": [
            {"id": "audio-1", "libraryId": "audio", "media": {"metadata": {"title": "Title"}}}]}]}
    identity = {"id": "self", "mediaProgress": [_audio(**change)]}
    if code:
        with pytest.raises(ProgressError, match=code):
            await _fetch("abs", pages, identity=identity)
    else:
        assert (await _fetch("abs", pages, identity=identity))[0]["entries"] == []


@pytest.mark.asyncio
async def test_abs_excluded_libraries_are_not_requested_and_missing_progress_is_not_zero():
    result, calls = await _fetch("abs", {"/api/libraries": {"libraries": [{"id": "audio"}]}},
                                 identity={"id": "self", "mediaProgress": []}, fields={"excluded": ["audio"]})
    assert result["entries"] == [] and len(calls) == 2
    with pytest.raises(ProgressError, match="malformed_response"):
        await _fetch("abs", {"/api/libraries": {"libraries": [{"id": "audio"}]}, "/api/libraries/audio/personalized": [
            {"id": "continue-reading", "type": "book", "total": 1, "entities": [{"id": "missing", "libraryId": "audio"}]}]},
            identity={"id": "self", "mediaProgress": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("response,code", [(httpx.Response(302, headers={"location": "https://elsewhere.invalid/"}), "unsafe_redirect"),
    (httpx.Response(401, text=TOKEN), "auth_failed"), (httpx.Response(429), "http_error"),
    (httpx.Response(200, headers={"content-length": str(svc.MAX_RESPONSE_BYTES + 1)}), "too_large"),
    (httpx.Response(200, content=b"x" * (svc.MAX_RESPONSE_BYTES + 1)), "too_large"),
    (httpx.Response(200, text="not json"), "malformed_response")])
async def test_feed_errors_are_bounded_redacted_and_not_retried(response, code):
    calls = []
    def send(request):
        calls.append(request)
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(send), follow_redirects=False, trust_env=False) as client:
        with pytest.raises(ProgressError, match=code) as error:
            await svc.Reader(client, _source("romm"), TOKEN, lambda: None).get("api/roms")
    assert len(calls) == 1 and TOKEN not in str(error.value)


@pytest.mark.asyncio
async def test_reflected_personal_secret_is_never_returned_as_native_metadata():
    rom = _rom()
    rom["name"] = TOKEN
    with pytest.raises(ProgressError, match="malformed_response"):
        await _fetch("romm", {"/api/roms": {"items": [rom]}})
