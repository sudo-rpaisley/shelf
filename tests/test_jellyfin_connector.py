"""Jellyfin boundary tests: every HTTP request is intercepted, with no live server."""

import asyncio
import json
import logging
import traceback
import zlib
from dataclasses import asdict
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
import respx

from app.services.source_connectors import jellyfin
from app.services.source_connectors.base import ConnectorError, safe_url

ROOT = "http://jellyfin.internal:8096/jellyfin"
USER = "user-123"
TOKEN = "test-only-jellyfin-secret"


def _source(options=None, **values):
    return {"base_url": ROOT, "options": options or {}, **values}


def _item(item_id="movie-1", kind="Movie", title="A Film", **values):
    return {"Id": item_id, "Type": kind, "Name": title, **values}


def _page(*items, total=..., start=None):
    data = {"Items": list(items)}
    if total is not None:
        data["TotalRecordCount"] = len(items) if total is ... else total
    if start is not None:
        data["StartIndex"] = start
    return httpx.Response(200, json=data)


@pytest.fixture
def server():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.get(f"{ROOT}/Users/Me", name="me").respond(200, json={"Id": USER})
        router.get(f"{ROOT}/Users/{USER}/Views", name="views").mock(
            return_value=_page({"Id": "library-1", "Name": "Visible library"}),
        )
        yield router


@pytest.mark.asyncio
async def test_paginated_items_use_header_auth_and_supported_fields(server, monkeypatch, caplog):
    monkeypatch.setattr(jellyfin, "PAGE_SIZE", 2)
    route = server.get(f"{ROOT}/Items").mock(side_effect=[
        _page(_item("1"), _item("2"), total=3, start=0),
        _page(_item("3"), total=3, start=2),
    ])
    caplog.set_level(logging.INFO, logger="httpx")

    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})

    assert [record.external_id for record in result.records] == ["1", "2", "3"]
    assert result.complete is True
    assert result.warnings == []
    assert [call.request.url.params["StartIndex"] for call in route.calls] == ["0", "2"]
    first = route.calls[0].request.url.params
    assert first["UserId"] == USER
    assert first["ParentId"] == "library-1"
    assert first["Recursive"] == "true"
    assert first["Limit"] == "2"
    assert set(first["IncludeItemTypes"].split(",")) == set(jellyfin.TYPE_MAP)
    assert {"ProviderIds", "Overview", "People"} <= set(first["Fields"].split(","))
    # These are intrinsic DTO properties, not valid ItemFields enum members.
    assert not {"ProductionYear", "RunTimeTicks"} & set(first["Fields"].split(","))
    assert first["EnableUserData"] == "false"
    assert first["EnableImages"] == "false"
    for call in server.calls:
        assert call.request.method == "GET"
        assert call.request.headers["X-Emby-Token"] == TOKEN
        assert call.request.headers["Accept-Encoding"] == "identity"
        assert "authorization" not in call.request.headers
        assert TOKEN not in str(call.request.url)
        assert call.request.url.host == "jellyfin.internal"
        assert call.request.url.path.startswith("/jellyfin/")
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(result)


@pytest.mark.asyncio
async def test_missing_totals_continue_full_pages_until_empty(server, monkeypatch):
    monkeypatch.setattr(jellyfin, "PAGE_SIZE", 1)
    route = server.get(f"{ROOT}/Items").mock(side_effect=[
        _page(_item("1"), total=None), _page(_item("2"), total=None), _page(total=None),
    ])
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert len(result.records) == 2
    assert result.complete
    assert [call.request.url.params["StartIndex"] for call in route.calls] == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_short_pages_with_explicit_total_do_not_stop_early(server, monkeypatch):
    monkeypatch.setattr(jellyfin, "PAGE_SIZE", 10)
    route = server.get(f"{ROOT}/Items").mock(side_effect=[
        _page(_item("1"), total=2), _page(_item("2"), total=2),
    ])
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert result.complete and len(result.records) == 2
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_repeated_page_returns_partial_not_complete(server, monkeypatch):
    monkeypatch.setattr(jellyfin, "PAGE_SIZE", 1)
    route = server.get(f"{ROOT}/Items").mock(side_effect=[
        _page(_item("1"), total=3), _page(_item("1"), total=3),
    ])
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert [record.external_id for record in result.records] == ["1"]
    assert result.complete is False
    assert "incomplete" in result.warnings
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_overlapping_pages_and_changing_totals_are_incomplete(server, monkeypatch):
    monkeypatch.setattr(jellyfin, "PAGE_SIZE", 2)
    server.get(f"{ROOT}/Items").mock(side_effect=[
        _page(_item("1"), _item("2"), total=3),
        _page(_item("2"), _item("3"), total=4),
    ])
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert [record.external_id for record in result.records] == ["1", "2", "3"]
    assert result.complete is False
    assert "incomplete" in result.warnings
    assert "duplicate_record" in result.warnings


@pytest.mark.asyncio
async def test_library_selection_is_user_visible_and_ids_deduplicate_across_libraries(server):
    server["views"].mock(return_value=_page(
        {"Id": "chosen-1"}, {"Id": "not-selected"}, {"Id": "chosen-2"},
    ))
    route = server.get(f"{ROOT}/Items").mock(side_effect=[
        _page(_item("shared"), _item("first-only")),
        _page(_item("shared"), _item("second-only")),
    ])
    result = await jellyfin.fetch_catalogue(
        _source({"user_id": USER, "library_ids": ["chosen-1", "chosen-1", "chosen-2"]}),
        {"token": TOKEN},
    )
    assert not server["me"].called
    assert [call.request.url.params["ParentId"] for call in route.calls] == ["chosen-1", "chosen-2"]
    assert [record.external_id for record in result.records] == ["shared", "first-only", "second-only"]
    assert result.records[0].library_id == "chosen-1"
    assert result.records[-1].library_id == "chosen-2"
    assert result.complete


@pytest.mark.asyncio
async def test_hidden_selected_library_is_never_fetched_even_with_an_admin_key(server):
    route = server.get(f"{ROOT}/Items").mock(return_value=_page(_item()))
    result = await jellyfin.fetch_catalogue(
        _source({"user_id": USER, "library_ids": ["library-1", "hidden-library"]}), {"token": TOKEN},
    )
    assert not result.complete
    assert len(result.records) == 1
    assert route.call_count == 1
    assert route.calls[0].request.url.params["ParentId"] == "library-1"
    assert "forbidden" in result.warnings


@pytest.mark.asyncio
async def test_no_selected_libraries_visible_is_an_actionable_error(server):
    with pytest.raises(ConnectorError) as caught:
        await jellyfin.fetch_catalogue(_source({"library_ids": ["hidden"]}), {"token": TOKEN})
    assert caught.value.code == "not_found"
    assert "library_ids" in caught.value.hint
    assert len(server.calls) == 2


@pytest.mark.asyncio
async def test_empty_visible_catalogue_is_a_complete_success(server):
    server.get(f"{ROOT}/Items").mock(return_value=_page())
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert result.complete and result.records == [] and result.warnings == []


@pytest.mark.asyncio
async def test_user_without_visible_libraries_is_a_complete_empty_success(server):
    server["views"].mock(return_value=_page())
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert result.complete and result.records == []
    assert len(server.calls) == 2


@pytest.mark.asyncio
async def test_only_exact_supported_types_are_imported_not_episodes_or_tracks(server):
    kinds = [*jellyfin.TYPE_MAP, "Episode", "Season", "Audio", "MusicArtist", "MusicVideo", "BoxSet", "Playlist", "Folder"]
    rows = [_item(str(index), kind) for index, kind in enumerate(kinds)]
    server.get(f"{ROOT}/Items").mock(return_value=_page(*rows))
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert [record.media_type for record in result.records] == [
        "digital_video", "tv_series", "digital_music", "ebook", "audiobook",
    ]
    assert result.complete
    assert [record.metadata["jellyfin_type"] for record in result.records] == list(jellyfin.TYPE_MAP)


@pytest.mark.asyncio
async def test_unicode_creators_isbn_and_whitelisted_metadata(server):
    server.get(f"{ROOT}/Items").mock(return_value=_page(
        _item("book", "Book", "  三体 — Cafe\u0301  ",
              People=[{"Type": "Author", "Name": "刘慈欣"}, {"Type": "Writer", "Name": "刘慈欣"}, {"Type": "Translator", "Name": "Not an author"}],
              ProviderIds={"ISBN": "ISBN: 978-0-306-40615-7", "Other": "ignored"},
              ProductionYear=2006, RunTimeTicks=1_234_000_000,
              Overview="<p> 第一章 </p>\n  Description.", SeriesName="地球往事", Container="epub",
              Genres=["科幻", "科幻"], Studios=[{"Name": "出版社"}]),
        _item("film", People=[{"Type": "Actor", "Name": "Not director"}, {"Type": "Director", "Name": "Agnès Varda"}]),
        _item("show", "Series", "映像", People=[{"Type": "Creator", "Name": "宮崎 駿"}]),
        _item("album", "MusicAlbum", "音楽", AlbumArtists=["坂本龍一", "坂本龍一"], Artists=["Do not replace album artist"]),
        _item("audio", "AudioBook", "Роман", Artists=["Лев Толстой"], Container="m4b"),
    ))
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    book, film, show, album, audio = result.records
    assert book.title == "三体 — Café"
    assert book.authors == "刘慈欣"
    assert book.isbn == "9780306406157"
    assert book.format == "EPUB"
    assert book.metadata == {
        "jellyfin_type": "Book", "year": 2006, "duration": 123,
        "description": "第一章 Description.", "series_name": "地球往事",
        "genres": ["科幻"], "studios": ["出版社"], "provider_ids": {"Isbn": "9780306406157"},
    }
    assert film.authors == "Agnès Varda" and film.isbn is None
    assert show.authors == "宮崎 駿" and show.metadata["series_name"] == "映像"
    assert album.authors == "坂本龍一"
    assert audio.authors == "Лев Толстой" and audio.format == "M4B"


@pytest.mark.asyncio
async def test_blank_titles_bad_ids_and_hostile_field_shapes_are_not_coerced(server):
    server.get(f"{ROOT}/Items").mock(return_value=_page(
        _item("blank", title=" \n\t"), _item("mapping-title", title={"name": "wrong"}),
        _item(None), _item("bad-kind", kind={"Movie": True}),
        _item("valid", People={"Type": "Director", "Name": "Not a list"},
              ProviderIds=[{"ISBN": "9780306406157"}], ProductionYear=True, RunTimeTicks="10000000",
              Overview={"Token": TOKEN}, Container=f"mp4?api_key={TOKEN}", Genres={"name": "x"}, Studios="x"),
        None,
    ))
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert [record.external_id for record in result.records] == ["valid"]
    assert result.records[0].metadata == {"jellyfin_type": "Movie"}
    assert result.records[0].authors == ""
    assert result.records[0].format == "Movie"
    assert not result.complete
    assert TOKEN not in repr(result)


@pytest.mark.asyncio
async def test_raw_payload_links_paths_tokens_and_images_are_never_copied_or_fetched(server):
    server.get(f"{ROOT}/Items").mock(return_value=_page(_item(
        "safe-id", title=f"A {TOKEN} Film", ServerId="server-123",
        People=[{"Type": "Director", "Name": f"Director {TOKEN}"}],
        Overview=f'<a href="http://other/images?api_key=another-secret">A link</a> {TOKEN} http://other/image?token=another-secret',
        AccessToken=TOKEN, Path=f"/private/{TOKEN}/film.mp4",
        ImageTags={"Primary": TOKEN}, PrimaryImageTag=TOKEN,
        RemoteTrailers=[{"Url": f"http://other/watch?api_key={TOKEN}"}],
        MediaSources=[{"Path": f"http://other/stream?api_key={TOKEN}"}],
        ProviderIds={"Imdb": "tt123456", "Tmdb": f"https://other/?token={TOKEN}", "Token": TOKEN},
        UserData={"PlaybackPositionTicks": 100, "AccessToken": TOKEN},
    )))
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    record = result.records[0]
    serialized = json.dumps(asdict(record), ensure_ascii=False)
    assert TOKEN not in serialized
    assert "another-secret" not in serialized
    assert "MediaSources" not in serialized and "UserData" not in serialized
    assert "/private/" not in serialized and "other/stream" not in serialized
    assert record.metadata["provider_ids"] == {"Imdb": "tt123456"}
    assert record.open_url == f"{ROOT}/web/index.html#!/details?id=safe-id"
    assert safe_url(record.open_url) == record.open_url
    assert len(server.calls) == 3


@pytest.mark.asyncio
async def test_basepath_browser_root_and_reserved_opaque_ids_are_preserved_and_encoded(server):
    item_id = "opaque/id ?&=三体#end"
    server.get(f"{ROOT}/Items").mock(return_value=_page(_item(item_id)))
    result = await jellyfin.fetch_catalogue(
        _source(browser_url="https://public.example/media/root/"), {"token": TOKEN},
    )
    record = result.records[0]
    assert record.external_id == item_id
    assert record.open_url == f"https://public.example/media/root/web/index.html#!/details?id={quote(item_id, safe='')}"
    assert safe_url(record.open_url) == record.open_url
    assert parse_qs(urlsplit(record.open_url).fragment.split("?", 1)[1]) == {"id": [item_id]}
    assert all(call.request.url.host == "jellyfin.internal" for call in server.calls)


@pytest.mark.asyncio
async def test_user_path_and_library_query_ids_cannot_inject_endpoints_or_query_parameters(server):
    user_id = "user/id?x=1#fragment"
    library_id = "library/id&UserId=someone-else"
    server.get(f"{ROOT}/Users/{quote(user_id, safe='')}/Views").mock(return_value=_page({"Id": library_id}))
    route = server.get(f"{ROOT}/Items").mock(return_value=_page(_item()))
    result = await jellyfin.fetch_catalogue(_source({"user_id": user_id, "library_ids": [library_id]}), {"token": TOKEN})
    assert result.complete
    assert route.calls[0].request.url.params.get_list("UserId") == [user_id]
    assert route.calls[0].request.url.params["ParentId"] == library_id
    assert result.records[0].library_id == library_id


@pytest.mark.asyncio
async def test_options_json_and_explicit_user_skip_users_me(server):
    server.get(f"{ROOT}/Items").mock(return_value=_page(_item()))
    source = {"base_url": ROOT, "options_json": json.dumps({"user_id": USER, "library_ids": ["library-1"]})}
    result = await jellyfin.fetch_catalogue(source, {"token": TOKEN})
    assert result.complete and len(result.records) == 1
    assert not server["me"].called


@pytest.mark.parametrize("options", [[], "bad", {"user_id": 123}, {"library_ids": "one"}, {"library_ids": [None]}, {"library_ids": [".."]}, {"unsupported_option": True}])
@pytest.mark.asyncio
async def test_invalid_option_shapes_fail_without_network(options):
    with respx.mock(assert_all_mocked=True) as router:
        with pytest.raises(ConnectorError) as caught:
            await jellyfin.fetch_catalogue({"base_url": ROOT, "options": options}, {"token": TOKEN})
        assert caught.value.code == "invalid_options"
        assert len(router.calls) == 0


@pytest.mark.asyncio
async def test_invalid_options_json_is_safe_and_no_outbound():
    with respx.mock(assert_all_mocked=True) as router:
        with pytest.raises(ConnectorError) as caught:
            await jellyfin.fetch_catalogue({"base_url": ROOT, "options_json": TOKEN}, {"token": TOKEN})
        assert caught.value.code == "invalid_options"
        assert TOKEN not in str(caught.value) + caught.value.hint
        assert len(router.calls) == 0


@pytest.mark.parametrize("root", ["ftp://host", "http:///no-host", "http://user:secret@host", "https://@host", "https://host?api_key=x", "https://host/#token=x", "https://host:99999", "https://host\\@other", "http://host/path\n/other", "https://[invalid"])
@pytest.mark.asyncio
async def test_invalid_roots_and_embedded_credentials_never_reach_network(root):
    with respx.mock(assert_all_mocked=True) as router:
        with pytest.raises(ConnectorError) as caught:
            await jellyfin.fetch_catalogue(_source(base_url=root), {"token": TOKEN})
        assert caught.value.code == "invalid_url"
        assert root not in str(caught.value) + caught.value.hint
        assert len(router.calls) == 0


@pytest.mark.parametrize("root", ["http://localhost:8096", "http://127.0.0.1:8096/jellyfin", "http://192.168.1.10:8096", "http://[::1]:8096"])
def test_lan_and_loopback_roots_are_intentionally_allowed(root):
    assert jellyfin._root(root, TOKEN) == root


@pytest.mark.parametrize("encoding_depth", [1, 2, 3])
def test_percent_encoded_reflected_credentials_are_rejected_or_redacted(encoding_depth):
    encoded = "".join(f"%{ord(character):02x}" for character in TOKEN)
    for _ in range(encoding_depth - 1):
        encoded = encoded.replace("%", "%25")
    assert jellyfin._text(f"Description {encoded}", TOKEN) == "[redacted]"
    assert jellyfin._identity(encoded, TOKEN) is None
    with pytest.raises(ConnectorError) as caught:
        jellyfin._root(f"http://jellyfin.internal/{encoded}", TOKEN)
    assert caught.value.code == "invalid_url"


def test_plain_text_credentials_in_url_queries_fragments_and_provider_key_paths_are_redacted():
    text = jellyfin._text(
        "http://example/path?api_key=not-ours "
        "http://example/web/#!/details?id=1&token=also-private "
        "http://example/devkey/another-private-key/image.jpg", TOKEN,
    )
    assert text == "[redacted link] [redacted link] [redacted link]"


def test_malformed_markup_and_unpaired_unicode_are_bounded_plain_text():
    assert jellyfin._text("<" * 100_000, TOKEN) == "<" * 1024
    assert jellyfin._text("三体\ud800\x00\nCafé", TOKEN) == "三体 Café"


@pytest.mark.parametrize("token", [None, "", "bad\nheader", "bad header", "é", 123])
@pytest.mark.asyncio
async def test_token_is_required_and_header_safe(token):
    with respx.mock(assert_all_mocked=True) as router:
        with pytest.raises(ConnectorError) as caught:
            await jellyfin.fetch_catalogue(_source(), {"token": token})
        assert caught.value.code == "invalid_credentials"
        assert len(router.calls) == 0


@pytest.mark.parametrize("payload", [None, {}, {"Id": None}, {"Id": TOKEN}, {"Id": 123}])
@pytest.mark.asyncio
async def test_admin_token_without_resolvable_user_requires_explicit_user_id(server, payload):
    server["me"].respond(200, content=json.dumps(payload))
    with pytest.raises(ConnectorError) as caught:
        await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert caught.value.code == "invalid_options"
    assert "options.user_id" in caught.value.hint
    assert len(server.calls) == 1


@pytest.mark.parametrize("status", [400, 404, 405, 501])
@pytest.mark.asyncio
async def test_unavailable_users_me_gives_actionable_user_id_hint(server, status):
    server["me"].respond(status, text=f"unsafe body {TOKEN}")
    with pytest.raises(ConnectorError) as caught:
        await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert caught.value.code == "invalid_options"
    assert "options.user_id" in caught.value.hint
    assert TOKEN not in caught.value.hint


@pytest.mark.parametrize("location", ["https://other.example/Items?token=hostile", "/jellyfin/final/Items"])
@pytest.mark.asyncio
async def test_redirects_never_forward_credentials(server, location):
    route = server.get(f"{ROOT}/Items").respond(302, headers={"Location": location})
    with pytest.raises(ConnectorError) as caught:
        await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert caught.value.code == "unsafe_redirect"
    assert "final server root" in caught.value.hint
    assert route.call_count == 1 and len(server.calls) == 3
    assert location not in str(caught.value) + caught.value.hint


@pytest.mark.parametrize(("status", "code"), [(401, "auth_failed"), (403, "auth_failed"), (429, "rate_limited"), (404, "http_error"), (500, "http_error")])
@pytest.mark.asyncio
async def test_status_errors_are_redacted_and_all_fail_raises(server, status, code, caplog):
    server.get(f"{ROOT}/Items").respond(status, text=f"Bearer {TOKEN} http://user:password@unsafe/")
    with pytest.raises(ConnectorError) as caught:
        await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert caught.value.code == code
    assert str(caught.value) == code
    assert TOKEN not in caught.value.hint + caplog.text
    assert "password@unsafe" not in caught.value.hint


@pytest.mark.parametrize("error", [httpx.ReadTimeout(TOKEN), httpx.ConnectError(TOKEN), httpx.RemoteProtocolError(TOKEN)])
@pytest.mark.asyncio
async def test_transport_errors_do_not_escape_or_leak_exception_chains(server, error):
    server.get(f"{ROOT}/Items").mock(side_effect=error)
    with pytest.raises(ConnectorError) as caught:
        await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert caught.value.code in {"timeout", "unreachable"}
    formatted = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in formatted
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html>not JSON</html>"),
    httpx.Response(200, json=[]), httpx.Response(200, json={}),
    httpx.Response(200, json={"Items": {}, "TotalRecordCount": 0}),
    httpx.Response(200, json={"Items": [], "TotalRecordCount": True}),
    httpx.Response(200, json={"Items": [], "TotalRecordCount": -1}),
])
@pytest.mark.asyncio
async def test_malformed_pages_are_not_misreported_as_empty_success(server, response):
    server.get(f"{ROOT}/Items").mock(return_value=response)
    with pytest.raises(ConnectorError) as caught:
        await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert caught.value.code == "malformed_response"


@pytest.mark.parametrize("second", [_page(total=2), _page(_item("2"), total=2, start=0), httpx.Response(503), httpx.Response(200, text="invalid")])
@pytest.mark.asyncio
async def test_failed_or_short_later_pages_keep_records_and_are_incomplete(server, monkeypatch, second):
    monkeypatch.setattr(jellyfin, "PAGE_SIZE", 1)
    server.get(f"{ROOT}/Items").mock(side_effect=[_page(_item("1"), total=2), second])
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert [record.external_id for record in result.records] == ["1"]
    assert not result.complete and result.warnings


@pytest.mark.asyncio
async def test_one_library_timeout_does_not_discard_other_libraries(server):
    server["views"].mock(return_value=_page({"Id": "bad"}, {"Id": "good"}))
    route = server.get(f"{ROOT}/Items").mock(side_effect=[httpx.ReadTimeout(TOKEN), _page(_item())])
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert len(result.records) == 1 and result.records[0].library_id == "good"
    assert not result.complete and route.call_count == 2
    assert TOKEN not in repr(result)


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.asyncio
async def test_global_auth_or_rate_failure_stops_other_libraries_but_keeps_partial(server, status):
    server["views"].mock(return_value=_page({"Id": "one"}, {"Id": "two"}, {"Id": "three"}))
    route = server.get(f"{ROOT}/Items").mock(side_effect=[_page(_item()), httpx.Response(status)])
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert not result.complete and len(result.records) == 1
    assert route.call_count == 2


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.read = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_response_byte_limit_is_enforced_while_streaming_before_tail_is_read(server, monkeypatch):
    monkeypatch.setattr(jellyfin, "MAX_RESPONSE_BYTES", 100)
    stream = _Chunks(b"x" * 60, b"x" * 60, b"tail must not be consumed")
    server.get(f"{ROOT}/Items").mock(return_value=httpx.Response(200, stream=stream))
    with pytest.raises(ConnectorError) as caught:
        await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert caught.value.code == "limit_reached"
    assert stream.read == 2 and stream.closed


@pytest.mark.asyncio
async def test_compressed_response_is_refused_before_body_read_or_decompression(server, monkeypatch):
    monkeypatch.setattr(jellyfin, "MAX_RESPONSE_BYTES", 100)
    compressor = zlib.compressobj(wbits=31)
    compressed = compressor.compress(b"x" * 1000) + compressor.flush()
    assert len(compressed) < 100
    stream = _Chunks(compressed)
    server.get(f"{ROOT}/Items").mock(return_value=httpx.Response(
        200, headers={"Content-Encoding": "gzip", "Content-Length": str(len(compressed))}, stream=stream,
    ))
    with pytest.raises(ConnectorError) as caught:
        await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert caught.value.code == "unsupported_encoding"
    assert stream.read == 0 and stream.closed


@pytest.mark.parametrize("encoding", ["deflate", "br", "zstd", "gzip, identity"])
@pytest.mark.asyncio
async def test_every_nonidentity_encoding_is_rejected_before_body_consumption(server, encoding):
    stream = _Chunks(b"must not be read or decoded")
    server.get(f"{ROOT}/Items").mock(return_value=httpx.Response(200, headers={"Content-Encoding": encoding}, stream=stream))
    with pytest.raises(ConnectorError) as caught:
        await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert caught.value.code == "unsupported_encoding"
    assert stream.read == 0 and stream.closed


@pytest.mark.asyncio
async def test_later_compressed_page_preserves_partial_records_without_reading_body(server, monkeypatch):
    monkeypatch.setattr(jellyfin, "PAGE_SIZE", 1)
    stream = _Chunks(b"never decode")
    route = server.get(f"{ROOT}/Items").mock(side_effect=[
        _page(_item("first"), total=2),
        httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream),
    ])
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert not result.complete and [r.external_id for r in result.records] == ["first"]
    assert "unsupported_encoding" in result.warnings
    assert route.call_count == 2 and stream.read == 0 and stream.closed


@pytest.mark.asyncio
async def test_content_length_limit_rejects_without_reading_body(server, monkeypatch):
    monkeypatch.setattr(jellyfin, "MAX_RESPONSE_BYTES", 100)
    stream = _Chunks(b"not read")
    server.get(f"{ROOT}/Items").mock(return_value=httpx.Response(200, headers={"Content-Length": "101"}, stream=stream))
    with pytest.raises(ConnectorError) as caught:
        await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert caught.value.code == "limit_reached"
    assert stream.read == 0 and stream.closed


@pytest.mark.asyncio
async def test_aggregate_byte_limit_returns_existing_partial_records(server, monkeypatch):
    first = _page(_item("first"), total=2)
    monkeypatch.setattr(jellyfin, "PAGE_SIZE", 1)
    # Include the default Users/Me and Views responses in the aggregate budget.
    me_bytes = len(server["me"].return_value.content)
    views_bytes = len(server["views"].return_value.content)
    monkeypatch.setattr(jellyfin, "MAX_TOTAL_BYTES", me_bytes + views_bytes + len(first.content) + 10)
    stream = _Chunks(b"x" * 11, b"not read")
    server.get(f"{ROOT}/Items").mock(side_effect=[first, httpx.Response(200, stream=stream)])
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert not result.complete and len(result.records) == 1
    assert stream.read == 1 and stream.closed


@pytest.mark.parametrize(("constant", "maximum"), [("MAX_PAGES", 1), ("MAX_ENTRIES", 1)])
@pytest.mark.asyncio
async def test_global_page_and_entry_limits_return_partial_results(server, monkeypatch, constant, maximum):
    monkeypatch.setattr(jellyfin, constant, maximum)
    monkeypatch.setattr(jellyfin, "PAGE_SIZE", 1)
    route = server.get(f"{ROOT}/Items").mock(return_value=_page(_item(), total=2))
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert not result.complete and len(result.records) == 1
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_library_limit_prevents_unbounded_view_fanout(server, monkeypatch):
    monkeypatch.setattr(jellyfin, "MAX_LIBRARIES", 1)
    server["views"].mock(return_value=_page({"Id": "one"}, {"Id": "two"}))
    route = server.get(f"{ROOT}/Items").mock(return_value=_page(_item()))
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert not result.complete and route.call_count == 1


@pytest.mark.asyncio
async def test_server_ignoring_page_limit_is_bounded_and_incomplete(server, monkeypatch):
    monkeypatch.setattr(jellyfin, "PAGE_SIZE", 1)
    server.get(f"{ROOT}/Items").mock(return_value=_page(_item("one"), _item("two")))
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert [record.external_id for record in result.records] == ["one"]
    assert not result.complete


@pytest.mark.asyncio
async def test_request_wall_timeout_is_bounded_even_with_a_stream_that_never_finishes(server, monkeypatch):
    monkeypatch.setattr(jellyfin, "REQUEST_TIMEOUT", 0.1)

    class SlowStream(_Chunks):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b"not reached"

    stream = SlowStream()
    server.get(f"{ROOT}/Items").mock(return_value=httpx.Response(200, stream=stream))
    with pytest.raises(ConnectorError) as caught:
        await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert caught.value.code == "timeout"
    assert stream.closed


@pytest.mark.asyncio
async def test_overall_timeout_preserves_already_fetched_records(server, monkeypatch):
    monkeypatch.setattr(jellyfin, "CATALOGUE_TIMEOUT", 0.25)
    monkeypatch.setattr(jellyfin, "PAGE_SIZE", 1)

    async def respond(request):
        if request.url.params["StartIndex"] == "0":
            return _page(_item("first"), total=2)
        await asyncio.Event().wait()

    server.get(f"{ROOT}/Items").mock(side_effect=respond)
    result = await jellyfin.fetch_catalogue(_source(), {"token": TOKEN})
    assert not result.complete and len(result.records) == 1
    assert "timeout" in result.warnings


@pytest.mark.asyncio
async def test_identical_titles_are_not_deduplicated_and_ids_remain_source_local(server):
    server.get(f"{ROOT}/Items").mock(return_value=_page(_item("id-1"), _item("id-2")))
    first = await jellyfin.fetch_catalogue(_source(id=1), {"token": TOKEN})
    second = await jellyfin.fetch_catalogue(_source(id=2), {"token": TOKEN})
    assert [record.external_id for record in first.records] == ["id-1", "id-2"]
    assert first.records == second.records
    assert len(first.records) == 2
