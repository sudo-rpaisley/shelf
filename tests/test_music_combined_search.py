"""New combined-catalogue regressions, authored only; no execution claimed.

All responses and release identities here are synthetic. Historical narrow-
fallback expectations remain unchanged and need an explicit policy migration.
"""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.services import discogs, item_write, music_matching, musicbrainz, openlibrary, provider_result
from tests.test_add_media_search import MUSIC_FORMATS, form, io, preview, release, search, snapshot  # noqa: F401


def _install(monkeypatch, *, kind="cd", mb=None, dg=None):
    order = []

    async def first(*args, **kwargs):
        order.append("musicbrainz")
        return deepcopy(mb) if mb is not None else provider_result.found("musicbrainz", [release(media_type=kind)])

    async def second(*args, **kwargs):
        order.append("discogs")
        return deepcopy(dg) if dg is not None else provider_result.found("discogs", [release("discogs", kind)])

    first, second = AsyncMock(side_effect=first), AsyncMock(side_effect=second)
    monkeypatch.setattr(musicbrainz, "search_releases", first)
    monkeypatch.setattr(discogs, "search_public_releases", second)
    return first, second, order


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", (*MUSIC_FORMATS, ""))
async def test_both_catalogues_are_searched_even_when_musicbrainz_finds_the_same_title(monkeypatch, kind):
    first, second, order = _install(monkeypatch, kind=kind or "cd")
    result = await music_matching.search_catalogue(object(), title="Hits Out of Hell", format=kind)
    assert order == ["musicbrainz", "discogs"]
    first.assert_awaited_once()
    second.assert_awaited_once()
    assert result.found and len(result.payload) == 2
    assert [row["provider"] for row in result.payload] == ["musicbrainz", "discogs"]
    assert result.payload[0]["provider_release_id"] == release()["musicbrainz_release_id"]
    assert result.payload[1]["provider_release_id"] == "77"
    assert result.payload[1]["source_url"] == "https://www.discogs.com/release/77"
    assert not result.payload[1].get("musicbrainz_release_id")
    assert music_matching.search_statuses(result) == (
        {"provider": "musicbrainz", "outcome": "found", "count": 1},
        {"provider": "discogs", "outcome": "found", "count": 1},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("options,field,value", [
    ({"title": "Album"}, None, "Album"),
    ({"artist": "An Artist"}, "artist", "An Artist"),
    ({"barcode": "012345678905"}, "barcode", "012345678905"),
    ({"catalog_number": "4026156"}, "catalog_number", "4026156"),
])
async def test_title_artist_barcode_and_catalogue_only_queries_all_reach_discogs(monkeypatch, options, field, value):
    first, second, order = _install(monkeypatch)
    result = await music_matching.search_catalogue(object(), format="cd", country="GB", **options)
    assert result.found and order == ["musicbrainz", "discogs"]
    if field is None:
        assert second.await_args.args[0] == value
    else:
        assert second.await_args.kwargs[field] == value
    assert second.await_args.kwargs["media_type"] == "cd"
    assert second.await_args.kwargs["country"] == "GB"
    assert second.await_args.kwargs["limit"] == 20
    assert first.await_count == second.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_provider", ["musicbrainz", "discogs"])
@pytest.mark.parametrize("outcome", ["rejected", "rate_limited", "transport_failed", "no_match"])
async def test_one_catalogues_failure_or_empty_result_keeps_the_others_hits_and_honest_outcome(monkeypatch, failed_provider, outcome):
    failed = provider_result.ProviderResult(outcome, failed_provider, status=200 if outcome == "no_match" else None)
    first, second, order = _install(monkeypatch, **{"mb" if failed_provider == "musicbrainz" else "dg": failed})
    result = await music_matching.search_catalogue(object(), title="Album", format="cd")
    assert result.found and len(result.payload) == 1
    assert result.payload[0]["provider"] != failed_provider
    assert order == ["musicbrainz", "discogs"] and first.await_count == second.await_count == 1
    statuses = {row["provider"]: row for row in music_matching.search_statuses(result)}
    assert statuses[failed_provider] == {"provider": failed_provider, "outcome": outcome, "count": 0}
    assert statuses[result.payload[0]["provider"]]["count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 404, 503])
async def test_unsuccessful_musicbrainz_http_status_is_not_a_no_match_but_discogs_still_runs(monkeypatch, status):
    first, second, order = _install(monkeypatch, mb=provider_result.no_match("musicbrainz", status=status))
    result = await music_matching.search_catalogue(object(), title="Album", format="cd")
    assert result.found and result.payload[0]["provider"] == "discogs"
    assert order == ["musicbrainz", "discogs"]
    assert music_matching.search_statuses(result)[0]["outcome"] == {
        401: "rejected", 429: "rate_limited", 404: "transport_failed", 503: "transport_failed",
    }[status]
    first.assert_awaited_once()
    second.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_provider", ["musicbrainz", "discogs"])
async def test_malformed_release_identity_is_a_visible_failure_without_discarding_the_other_catalogue(monkeypatch, failed_provider):
    malformed = provider_result.found(failed_provider, [{"title": "No release identity"}])
    _, _, order = _install(monkeypatch, **{"mb" if failed_provider == "musicbrainz" else "dg": malformed})
    result = await music_matching.search_catalogue(object(), title="Album", format="cd")
    assert result.found and len(result.payload) == 1 and result.payload[0]["provider"] != failed_provider
    assert order == ["musicbrainz", "discogs"]
    assert {row["provider"]: row["outcome"] for row in music_matching.search_statuses(result)}[failed_provider] == "transport_failed"


@pytest.mark.asyncio
async def test_two_empty_catalogues_are_distinct_from_a_failed_search(monkeypatch):
    _install(monkeypatch, mb=provider_result.found("musicbrainz", []), dg=provider_result.found("discogs", []))
    empty = await music_matching.search_catalogue(object(), title="Absent", format="cd")
    assert not empty.found and empty.outcome == "no_match"
    assert [row["outcome"] for row in music_matching.search_statuses(empty)] == ["no_match", "no_match"]
    _install(monkeypatch, mb=provider_result.transport_failed("musicbrainz"), dg=provider_result.rate_limited("discogs"))
    failed = await music_matching.search_catalogue(object(), title="Absent", format="cd")
    assert not failed.found and failed.outcome == "rate_limited" and failed.provider == "discogs"
    assert [row["outcome"] for row in music_matching.search_statuses(failed)] == ["transport_failed", "rate_limited"]


@pytest.mark.asyncio
async def test_per_provider_caps_do_not_cut_off_the_second_catalogue(monkeypatch):
    mb = [release(media_type="cd", musicbrainz_release_id=f"11111111-1111-4111-8111-{index:012d}") for index in range(35)]
    dg = [release("discogs", "cd", discogs_release_id=index + 1) for index in range(25)]
    _install(monkeypatch, mb=provider_result.found("musicbrainz", mb), dg=provider_result.found("discogs", dg))
    result = await music_matching.search_catalogue(object(), title="Album", format="cd")
    assert result.found and len(result.payload) == 50
    assert [row["provider"] for row in result.payload] == ["musicbrainz"] * 30 + ["discogs"] * 20
    assert [row["count"] for row in music_matching.search_statuses(result)] == [30, 20]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_provider", ["musicbrainz", "discogs"])
async def test_synthetic_provider_deadline_leaves_the_other_leg_available(monkeypatch, failed_provider):
    # This tests synthetic timeout classification, not actual clock/cancellation
    # settlement. Real deadline/resource qualification remains a separate gate.
    _install(monkeypatch)
    deadlines = []

    async def deadline(operation, *, timeout):
        deadlines.append(timeout)
        if len(deadlines) == (1 if failed_provider == "musicbrainz" else 2):
            operation.close()
            raise TimeoutError
        return await operation

    monkeypatch.setattr(music_matching.asyncio, "wait_for", deadline)
    result = await music_matching.search_catalogue(object(), title="Album", format="cd")
    assert deadlines == [12, 12] and result.found and len(result.payload) == 1
    assert result.payload[0]["provider"] != failed_provider
    assert {row["provider"]: row["outcome"] for row in music_matching.search_statuses(result)}[failed_provider] == "transport_failed"


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_start_the_next_provider(monkeypatch):
    first, second, _ = _install(monkeypatch)
    first.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await music_matching.search_catalogue(object(), title="Album", format="cd")
    second.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{}, {"title": "  ", "artist": " "}, {"title": "Album", "format": "book"}])
async def test_blank_or_non_music_search_does_not_spend_provider_requests(monkeypatch, options):
    first, second, _ = _install(monkeypatch)
    result = await music_matching.search_catalogue(object(), **options)
    assert not result.found and result.outcome == "no_match" and music_matching.search_statuses(result) == ()
    first.assert_not_awaited()
    second.assert_not_awaited()


@pytest.mark.parametrize("kind", MUSIC_FORMATS)
@pytest.mark.parametrize("method,query", [("title", "Hits Out of Hell"), ("barcode", "012345678905"), ("catalog_number", "4026156")])
def test_add_automatic_returns_both_provider_selections_without_search_writes(editor_client, db, io, kind, method, query):
    io.install(musicbrainz, "search_releases", provider_result.found("musicbrainz", [release(media_type=kind)]))
    io.install(discogs, "search_public_releases", provider_result.found("discogs", [release("discogs", kind)]))
    before = snapshot(db)
    response = search(editor_client, media_type=kind, method=method, q=query, artist="")
    assert response.status_code == 200
    add = response.context["add"]
    assert [row["provider"] for row in add["results"]] == ["musicbrainz", "discogs"]
    assert len({row["selection"] for row in add["results"]}) == 2
    assert [row["count"] for row in add["music_search_statuses"]] == [1, 1]
    assert 'data-music-provider="musicbrainz" data-search-outcome="found"' in response.text
    assert 'data-music-provider="discogs" data-search-outcome="found"' in response.text
    assert len(io.seen(musicbrainz, "search_releases")) == len(io.seen(discogs, "search_public_releases")) == 1
    assert snapshot(db) == before


def test_add_preserves_all_fifty_candidates_and_reports_partial_failure_alongside_hits(editor_client, db, io):
    rows = [release(media_type="cd", musicbrainz_release_id=f"11111111-1111-4111-8111-{index:012d}") for index in range(30)]
    io.install(musicbrainz, "search_releases", provider_result.found("musicbrainz", rows))
    io.install(discogs, "search_public_releases", provider_result.found("discogs", [release("discogs", "cd", discogs_release_id=index + 1) for index in range(20)]))
    before = snapshot(db)
    full = search(editor_client, media_type="cd", q="Album")
    assert full.status_code == 200 and len(full.context["add"]["results"]) == 50
    io.install(discogs, "search_public_releases", provider_result.rate_limited("discogs"))
    partial = search(editor_client, media_type="cd", q="Album")
    assert partial.status_code == 200 and len(partial.context["add"]["results"]) == 30
    assert partial.context["add"]["music_search_statuses"][1] == {"provider": "discogs", "outcome": "rate_limited", "count": 0}
    assert "Rate limit reached" in partial.text and "not a no-match result" in partial.text
    assert snapshot(db) == before


@pytest.mark.parametrize("provider", ["musicbrainz", "discogs"])
def test_explicit_single_provider_mode_never_contacts_the_other_catalogue(editor_client, db, io, provider):
    selected, method = (musicbrainz, "search_releases") if provider == "musicbrainz" else (discogs, "search_public_releases")
    io.install(selected, method, provider_result.found(provider, [release(provider, "cd")]))
    before = snapshot(db)
    response = search(editor_client, provider=provider, media_type="cd", q="Album")
    assert response.status_code == 200 and len(response.context["add"]["results"]) == 1
    assert [row["provider"] for row in response.context["add"]["results"]] == [provider]
    assert len(io.calls) == 1 and response.context["add"]["music_search_statuses"] == ()
    assert snapshot(db) == before


def test_both_result_selections_preview_only_their_exact_provider_release(editor_client, db, io):
    io.install(musicbrainz, "search_releases", provider_result.found("musicbrainz", [release(media_type="cd")]))
    io.install(discogs, "search_public_releases", provider_result.found("discogs", [release("discogs", "cd")]))
    io.install(musicbrainz, "lookup_release", provider_result.found("musicbrainz", release(media_type="cd")))
    io.install(discogs, "lookup_public_release", provider_result.found("discogs", release("discogs", "cd")))
    before = snapshot(db)
    response = search(editor_client, media_type="cd", q="Album")
    for index, provider in enumerate(("musicbrainz", "discogs")):
        candidate = preview(editor_client, response, index).context["add"]["preview"]
        assert candidate["provider"] == provider
        form(response, "add-media-preview", index)
    assert len(io.seen(musicbrainz, "lookup_release")) == len(io.seen(discogs, "lookup_public_release")) == 1
    assert snapshot(db) == before


@pytest.mark.parametrize("route", ["/music", "/api/music/match"])
def test_legacy_music_and_item_matching_show_both_catalogue_outcomes(editor_client, db, io, route):
    item_id = item_write.insert_item(db, title="Manual music item", media_type="cd", source="manual")
    db.commit()
    io.install(musicbrainz, "search_releases", provider_result.found("musicbrainz", [release(media_type="cd")]))
    io.install(discogs, "search_public_releases", provider_result.found("discogs", [release("discogs", "cd")]))
    before = snapshot(db)
    response = editor_client.get(route, params={"q": "Album", "format": "cd", **({"item_id": item_id, "search": 1} if route.endswith("match") else {})})
    assert response.status_code == 200
    rows = response.context["results" if route == "/music" else "match_results"]
    assert [row["provider"] for row in rows] == ["musicbrainz", "discogs"]
    assert response.text.count('data-testid="music-search-status"') == 1
    assert snapshot(db) == before


def test_non_music_search_does_not_broadcast_to_either_music_provider(editor_client, db, io):
    io.install(openlibrary, "search_books", provider_result.found("openlibrary", [{"isbn": "9780441172719", "title": "Dune", "authors": "Frank Herbert"}]))
    before = snapshot(db)
    response = search(editor_client, media_type="book", q="Dune")
    assert response.status_code == 200 and response.context["add"]["music_search_statuses"] == ()
    assert len(io.calls) == 1 and len(io.seen(openlibrary, "search_books")) == 1
    assert snapshot(db) == before
