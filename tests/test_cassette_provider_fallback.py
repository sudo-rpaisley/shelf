"""Independent cassette provider contracts; no live provider or credentials.

New production interfaces are imported/accessed inside tests, so their absence
on the RED baseline does not prevent the rest of the suite from collecting.
"""

from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest


@pytest.fixture
def catalogue():
    from tests.e2e.cassette_asgi import load_catalogue

    return load_catalogue()


@pytest.fixture
def boundary(monkeypatch):
    from app.services import outbound
    from tests.e2e.cassette_asgi import FixtureCatalogue

    fixture = FixtureCatalogue()
    monkeypatch.setattr(outbound, "fetch", fixture.fetch)
    yield fixture
    fixture.assert_clean()


async def _search_catalogue(**kwargs):
    from app.services import music_matching

    return await music_matching.search_catalogue(
        object(), title="Hits Out of Hell", artist="Meat Loaf", format="cassette", **kwargs,
    )


def test_projection_is_explicitly_abridged_not_a_raw_response(catalogue):
    assert "NOT recorded raw" in catalogue["provenance"]["kind"]
    assert "NOT public catalogue research" in catalogue["synthetic"]["notice"]
    assert list(catalogue["discogs_releases"]) == ["3935124", "8297774", "18670711"]
    evidence = catalogue["provenance"]["track_research"]
    assert evidence["source"].endswith("CASSETTE_TRACK_RESEARCH.json")
    assert "structured projection, not full raw responses" in evidence["method"]
    assert evidence["http_statuses"] == [200, 200, 200]
    assert evidence["anonymous_limit_per_minute"] == 25 and evidence["remaining_after_requests"] == [25, 24, 23]
    assert evidence["images_fetched"] is False and evidence["exact_user_pressing_selected"] is False
    for release in catalogue["discogs_releases"].values():
        assert release["images"] == []
        assert "identifiers" not in release and "barcode" not in release


def _assert_verified_normalized_release(release, release_id):
    from tests.e2e.cassette_asgi import verified_track_facts

    expected = verified_track_facts(release_id)
    assert release["discogs_release_id"] == release_id
    assert release["title"] == "Hits Out Of Hell" and release["artist_credit"] == "Meat Loaf"
    assert not release.get("musicbrainz_release_id") and not release.get("musicbrainz_release_group_id")
    assert len(release["media"]) == 1
    tracks = release["media"][0]["tracks"]
    assert [(track["number"], track["title"], track["duration_ms"]) for track in tracks] == [
        (number, title, duration_ms) for number, title, duration, duration_ms in expected
    ]
    assert [track["position"] for track in tracks] == list(range(1, len(expected) + 1))
    assert all(not track.get("musicbrainz_recording_id") for track in tracks)
    assert release["media"][0]["track_count"] == len(expected)
    if release_id == 8297774:
        assert len(tracks) == 12
        assert [track["number"] for track in tracks[-3:]] == ["B5-I", "B5-II", "B5-III"]
        assert all(track["duration_ms"] is None for track in tracks[-3:])
        assert all(track["title"] != "Paradise By The Dashboard Light" for track in tracks), "Do not duplicate the index as a track"
    elif release_id == 18670711:
        assert len(tracks) == 10 and all(track["duration_ms"] is None for track in tracks)
        assert tracks[2]["title"] == "Dead Ringer For Love" and tracks[4]["title"] == "Midnight At The Lost And Found"
        assert tracks[5]["title"] == "All Revved Up With No Place To Go"
        assert all(track["title"] != "Modern Girl" for track in tracks)
    else:
        assert len(tracks) == 10 and tracks[5]["title"] == "Modern Girl"
        assert all(track["title"] != "All Revved Up With No Place To Go" for track in tracks)
    return tracks


@pytest.mark.parametrize("release_id", [3935124, 8297774, 18670711])
def test_attributed_fixture_pins_exact_reported_track_order_durations_and_index_shape(catalogue, release_id):
    from tests.e2e.cassette_asgi import verified_track_facts

    release = catalogue["discogs_releases"][str(release_id)]
    assert release["title"] == "Hits Out Of Hell" and len(release["tracklist"]) == 10

    def flatten(entries):
        rows = []
        for entry in entries:
            if entry.get("sub_tracks"):
                rows.extend(flatten(entry["sub_tracks"]))
            else:
                assert entry["type_"] == "track"
                rows.append((entry["position"], entry["title"], entry["duration"]))
        return rows

    assert flatten(release["tracklist"]) == [
        (number, title, duration) for number, title, duration, duration_ms in verified_track_facts(release_id)
    ]
    if release_id == 8297774:
        index = release["tracklist"][-1]
        assert (index["position"], index["type_"], index["title"], index["duration"]) == (
            "", "index", "Paradise By The Dashboard Light", "8:28",
        )
        assert all(entry["duration"] == "" for entry in index["sub_tracks"])
    if release_id == 18670711:
        assert all(entry["duration"] == "" for entry in release["tracklist"])


@pytest.mark.parametrize("release_id", [3935124, 8297774, 18670711])
def test_researched_track_normalization_retains_sides_order_and_unknown_durations(catalogue, release_id):
    from app.services import discogs

    release = discogs.normalise_release(catalogue["discogs_releases"][str(release_id)])
    _assert_verified_normalized_release(release, release_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("release_id", [3935124, 8297774, 18670711])
async def test_each_public_lookup_returns_the_selected_researched_tracklist(boundary, release_id):
    from app.services import discogs

    result = await discogs.lookup_public_release(release_id, object())
    assert result.found and result.provider == "discogs"
    _assert_verified_normalized_release(result.payload, release_id)
    assert boundary.calls == [{"method": "GET", "url": f"https://api.discogs.com/releases/{release_id}",
                               "params": {}, "authorization_present": False}]


@pytest.mark.asyncio
async def test_legacy_helpers_still_require_a_token_and_never_make_anonymous_calls(monkeypatch):
    from app.services import discogs

    fetch = AsyncMock(side_effect=AssertionError("Token-only helpers must remain idle"))
    monkeypatch.setattr(discogs.outbound, "fetch", fetch)
    search = await discogs.search_releases("Hits Out of Hell", object(), token="", artist="Meat Loaf")
    detail = await discogs.lookup_release(3935124, object(), token="  ")
    assert search.outcome == detail.outcome == "no_credential"
    assert search.provider == detail.provider == "discogs"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "  ", "synthetic-configured-token"])
async def test_public_search_uses_fixed_host_exact_filters_no_blank_barcode(monkeypatch, catalogue, token):
    from app.services import discogs, provider_result

    fetch = AsyncMock(return_value=httpx.Response(200, json=catalogue["discogs_search"]))
    monkeypatch.setattr(discogs.outbound, "fetch", fetch)
    result = await discogs.search_public_releases(
        " Hits Out of Hell ", object(), token=token, artist=" Meat Loaf ",
        barcode=" ", catalog_number=" ", media_type="cassette", country="GB", limit=999,
    )
    assert isinstance(result, provider_result.ProviderResult) and result.found
    assert result.provider == "discogs" and len(result.payload) == 3
    fetch.assert_awaited_once()
    call = fetch.await_args
    assert call.args[1:] == ("GET", "https://api.discogs.com/database/search")
    assert call.kwargs["params"] == {
        "type": "release", "per_page": "20", "page": "1", "release_title": "Hits Out of Hell",
        "artist": "Meat Loaf", "format": "Cassette", "country": "GB",
    }
    headers = httpx.Headers(call.kwargs["headers"])
    assert headers["user-agent"] and headers["accept"] == "application/json"
    if token.strip():
        assert headers["authorization"] == "Discogs token=synthetic-configured-token"
    else:
        assert "authorization" not in headers


@pytest.mark.asyncio
async def test_public_lookup_omits_auth_and_retains_exact_research_identity(boundary):
    from app.services import discogs, provider_result

    result = await discogs.lookup_public_release(18670711, object())
    assert isinstance(result, provider_result.ProviderResult) and result.found
    assert result.provider == "discogs"
    assert result.payload["discogs_release_id"] == 18670711
    assert result.payload["title"] == "Hits Out Of Hell"
    assert result.payload["artist_credit"] == "Meat Loaf"
    assert result.payload["country"] == "Canada" and result.payload["release_date"] == "1995"
    assert result.payload["catalog_number"] == "BT 57615"
    assert result.payload["source"] == "discogs" and result.payload["images"] == []
    assert not result.payload.get("musicbrainz_release_id")
    assert not result.payload.get("musicbrainz_release_group_id")
    assert boundary.calls == [{"method": "GET", "url": "https://api.discogs.com/releases/18670711",
                               "params": {}, "authorization_present": False}]
    _assert_verified_normalized_release(result.payload, 18670711)


@pytest.mark.asyncio
@pytest.mark.parametrize("release_id", [0, -1, "abc", "../masters/24278", "https://www.discogs.com/master/24278", "3935124?x=1"])
async def test_public_lookup_rejects_nonrelease_identifiers_without_fetch(monkeypatch, release_id):
    from app.services import discogs

    fetch = AsyncMock(side_effect=AssertionError("Invalid identity reached transport"))
    monkeypatch.setattr(discogs.outbound, "fetch", fetch)
    result = await discogs.lookup_public_release(release_id, object())
    assert not result.found and result.provider == "discogs"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_blank_public_query_never_spends_a_request(monkeypatch):
    from app.services import discogs

    fetch = AsyncMock()
    monkeypatch.setattr(discogs.outbound, "fetch", fetch)
    result = await discogs.search_public_releases(" ", object(), artist=" ", barcode=" ", media_type="cassette")
    assert result.outcome == "no_match"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("operation", ["search", "lookup"])
async def test_rejected_configured_token_does_not_retry_anonymously(monkeypatch, status, operation):
    from app.services import discogs

    fetch = AsyncMock(return_value=httpx.Response(status, json={"message": "Synthetic rejection"}))
    monkeypatch.setattr(discogs.outbound, "fetch", fetch)
    if operation == "search":
        result = await discogs.search_public_releases("Hits Out of Hell", object(), token="synthetic-token", media_type="cassette")
    else:
        result = await discogs.lookup_public_release(3935124, object(), token="synthetic-token")
    assert result.outcome == "rejected" and result.provider == "discogs" and result.status == status
    fetch.assert_awaited_once()
    assert httpx.Headers(fetch.await_args.kwargs["headers"])["authorization"] == "Discogs token=synthetic-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], {"results": "synthetic non-list"}, {"unexpected": "synthetic missing results"}])
async def test_malformed_discogs_search_is_not_an_empty_success(monkeypatch, payload):
    from app.services import discogs

    fetch = AsyncMock(return_value=httpx.Response(200, json=payload))
    monkeypatch.setattr(discogs.outbound, "fetch", fetch)
    result = await discogs.search_public_releases("Hits Out of Hell", object(), artist="Meat Loaf", media_type="cassette")
    assert result.outcome == "transport_failed" and result.provider == "discogs"
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_search_caps_results_and_excludes_masters(monkeypatch, catalogue):
    from app.services import discogs

    row = catalogue["discogs_search"]["results"][0]
    rows = [{**row, "id": 900000 + index} for index in range(30)]
    rows.insert(0, {**row, "type": "master", "id": 24278})
    fetch = AsyncMock(return_value=httpx.Response(200, json={"results": rows}))
    monkeypatch.setattr(discogs.outbound, "fetch", fetch)
    result = await discogs.search_public_releases("Hits Out of Hell", object(), artist="Meat Loaf", media_type="cassette", limit=20)
    assert result.found and 0 < len(result.payload) <= 20
    assert all(candidate["discogs_release_id"] != 24278 for candidate in result.payload)
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_genuine_empty_musicbrainz_cassette_search_falls_back_once_with_distinct_attributed_pressings(boundary):
    result = await _search_catalogue(barcode="")
    assert result.found and result.provider == "discogs"
    assert len(boundary.calls) == 2
    assert boundary.calls[0]["url"] == "https://musicbrainz.org/ws/2/release/"
    assert boundary.calls[1]["url"] == "https://api.discogs.com/database/search"
    assert 'barcode:""' not in boundary.calls[0]["params"]["query"]
    assert "barcode" not in boundary.calls[1]["params"]
    assert not any(call["authorization_present"] for call in boundary.calls)
    candidates = result.payload
    assert {str(candidate["provider_release_id"]) for candidate in candidates} == {"3935124", "8297774", "18670711"}
    assert {candidate["country"] for candidate in candidates} == {"UK", "Europe", "Canada"}
    for candidate in candidates:
        assert candidate["provider"] == "discogs"
        assert candidate["source_url"] == f'https://www.discogs.com/release/{candidate["provider_release_id"]}'
        assert candidate["format_summary"] == "Cassette"
        assert candidate["shelf_media_type"] == "cassette"
        assert "format" in candidate["match_reasons"]
        assert not candidate.get("musicbrainz_release_id") and not candidate.get("musicbrainz_release_group_id")


@pytest.mark.asyncio
async def test_no_barcode_input_does_not_filter_out_candidates_that_have_a_barcode(boundary):
    fixture = boundary.catalogue
    rows = deepcopy(fixture["discogs_search"]["results"])
    rows[1]["barcode"] = [fixture["synthetic"]["barcode"]]
    boundary.discogs_search = {"status": 200, "json": {"results": rows}}
    result = await _search_catalogue()
    assert result.found and len(result.payload) == 3
    assert any(str(candidate["provider_release_id"]) == "8297774" for candidate in result.payload)
    assert "barcode" not in boundary.calls[-1]["params"]


@pytest.mark.asyncio
async def test_composite_search_title_is_not_split_to_invent_artist_identity_or_a_source_url(boundary):
    row = deepcopy(boundary.catalogue["discogs_search"]["results"][0])
    row.update(title=boundary.catalogue["synthetic"]["composite_title"], uri="https://evil.invalid/master/24278")
    boundary.discogs_search = {"status": 200, "json": {"results": [row]}}
    result = await _search_catalogue()
    assert result.found
    candidate = result.payload[0]
    assert not candidate.get("artist_credit"), "Search's composite display string is not structured artist evidence"
    assert candidate["source_url"] == "https://www.discogs.com/release/3935124"
    assert not candidate.get("musicbrainz_release_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["found_empty", "no_match"])
async def test_only_successful_empty_or_no_match_musicbrainz_result_permits_fallback(monkeypatch, catalogue, outcome):
    from app.services import discogs, music_matching, provider_result

    mb = provider_result.found("musicbrainz", []) if outcome == "found_empty" else provider_result.no_match("musicbrainz", status=200)
    original = AsyncMock(return_value=mb)
    fallback = AsyncMock(return_value=provider_result.found("discogs", [{"discogs_release_id": 3935124, "format_summary": "Cassette"}]))
    monkeypatch.setattr(music_matching, "search", original)
    monkeypatch.setattr(discogs, "search_public_releases", fallback)
    result = await _search_catalogue()
    assert result.found and result.provider == "discogs"
    original.assert_awaited_once()
    fallback.assert_awaited_once()
    assert fallback.await_args.args[0] == catalogue["query"]["title"]
    assert fallback.await_args.kwargs["artist"] == catalogue["query"]["artist"]
    assert fallback.await_args.kwargs["media_type"] == "cassette"


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"format": "cd"}, {"format": "vinyl"}, {"format": "digital_music"}, {"format": ""},
    {"title": ""}, {"title": "  "}, {"artist": ""}, {"artist": "  "},
])
async def test_ineligible_searches_do_not_start_discogs(monkeypatch, options):
    from app.services import discogs, music_matching, provider_result

    original = AsyncMock(return_value=provider_result.no_match("musicbrainz", status=200))
    fallback = AsyncMock()
    monkeypatch.setattr(music_matching, "search", original)
    monkeypatch.setattr(discogs, "search_public_releases", fallback)
    query = {"title": "Hits Out of Hell", "artist": "Meat Loaf", "format": "cassette", **options}
    result = await music_matching.search_catalogue(object(), **query)
    assert result.outcome == "no_match" and result.provider == "musicbrainz"
    fallback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"status": 429, "json": {"message": "Synthetic MusicBrainz quota"}},
    {"status": 403, "json": {"message": "Synthetic MusicBrainz rejection"}},
    {"status": 404, "json": {"message": "Synthetic endpoint failure, not an empty search"}},
    {"status": 200, "text": "{synthetic broken JSON"},
    {"status": 200, "json": {"releases": "synthetic malformed list"}},
    {"status": 200, "json": {"synthetic_missing_releases": True}},
    {"status": 503, "json": {"message": "Synthetic unavailable service"}},
])
async def test_musicbrainz_failure_or_malformed_response_never_falls_back(boundary, response):
    boundary.mb_search = response
    result = await _search_catalogue()
    assert not result.found and result.provider == "musicbrainz"
    assert len(boundary.calls) == 1, "Failure is not evidence of no cassette match"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["missing", "null", "false", "zero", "empty_text", "object", "root_list", "null_row", "text_row", "mixed_rows"])
async def test_musicbrainz_parser_rejects_malformed_shapes_before_catalogue_can_fall_back(boundary, shape):
    from app.services import musicbrainz

    payloads = {
        "missing": {}, "null": {"releases": None}, "false": {"releases": False},
        "zero": {"releases": 0}, "empty_text": {"releases": ""}, "object": {"releases": {}},
        "root_list": [], "null_row": {"releases": [None]}, "text_row": {"releases": ["synthetic invalid row"]},
        "mixed_rows": {"releases": [boundary.catalogue["synthetic"]["musicbrainz_release"], None]},
    }
    boundary.mb_search = {"status": 200, "json": payloads[shape]}
    parsed = await musicbrainz.search_releases("Hits Out of Hell", object(), artist="Meat Loaf", media_type="cassette")
    assert parsed.outcome == "transport_failed" and parsed.provider == "musicbrainz"
    assert len(boundary.calls) == 1
    result = await _search_catalogue()
    assert result.outcome == "transport_failed" and result.provider == "musicbrainz"
    assert len(boundary.calls) == 2 and all(call["url"] == "https://musicbrainz.org/ws/2/release/" for call in boundary.calls)


@pytest.mark.asyncio
async def test_musicbrainz_transport_failure_never_falls_back(boundary):
    boundary.mb_search = {"raise": httpx.ConnectError("Synthetic offline transport failure")}
    result = await _search_catalogue()
    assert result.outcome == "transport_failed" and result.provider == "musicbrainz"
    assert len(boundary.calls) == 1


@pytest.mark.asyncio
async def test_a_real_musicbrainz_candidate_wins_without_discogs(boundary):
    release = boundary.catalogue["synthetic"]["musicbrainz_release"]
    boundary.mb_search = {"status": 200, "json": {"releases": [release]}}
    result = await _search_catalogue()
    assert result.found and result.provider == "musicbrainz" and len(boundary.calls) == 1
    assert result.payload[0]["provider"] == "musicbrainz"
    assert result.payload[0]["provider_release_id"] == release["id"]
    assert result.payload[0]["source_url"] == f'https://musicbrainz.org/release/{release["id"]}'


@pytest.mark.asyncio
@pytest.mark.parametrize("case,outcome", [("rate_limited", "rate_limited"), ("rejected", "rejected"),
                                         ("malformed_json", "transport_failed"), ("malformed_discogs", "transport_failed")])
async def test_fallback_errors_name_discogs_and_do_not_become_empty_musicbrainz_results(boundary, case, outcome):
    boundary.discogs_search = boundary.catalogue["synthetic"][case]
    result = await _search_catalogue()
    assert result.outcome == outcome and result.provider == "discogs"
    assert len(boundary.calls) == 2


@pytest.mark.asyncio
async def test_existing_search_interface_remains_musicbrainz_only(monkeypatch):
    from app.services import music_matching, musicbrainz, provider_result

    lookup = AsyncMock(return_value=provider_result.found("musicbrainz", []))
    monkeypatch.setattr(musicbrainz, "search_releases", lookup)
    result = await music_matching.search(object(), title="Hits Out of Hell", artist="Meat Loaf", format="cassette")
    assert result.outcome == "no_match" and result.provider == "musicbrainz"
    lookup.assert_awaited_once()


def test_anonymous_discogs_global_interval_is_conservative():
    from app.config import HOST_RATE_LIMITS

    assert HOST_RATE_LIMITS["api.discogs.com"] >= 2.5


@pytest.mark.asyncio
async def test_fixture_boundary_fails_closed_for_unknown_destinations(boundary):
    with pytest.raises(AssertionError, match="does not authorize"):
        await boundary.fetch(object(), "GET", "https://unknown.invalid/releases/3935124")
    assert boundary.unexpected == boundary.calls
    # This is an explicit negative control of the test transport itself, not
    # an application request; acknowledge only this expected call.
    boundary.unexpected.pop()
