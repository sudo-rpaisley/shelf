"""Local Library semantics: format graph, Unicode, holdings and page units."""

import json

import pytest

from app import features
from app.services import format_groups, item_copies, library_catalog
from tests.conftest import _insert_item, _insert_location


def _item(db, title="A title", **fields):
    return _insert_item(db, title=title, isbn=None, **fields)


def _link(db, a, b, kind="format"):
    db.execute("INSERT INTO item_links (item_a_id, item_b_id, link_type) VALUES (?, ?, ?)", (a, b, kind))


def _source(db, name="Same source name", provider="opds", enabled=1, **fields):
    values = {"name": name, "provider": provider, "base_url": "https://books.example.test",
              "enabled": enabled, **fields}
    return db.execute(
        f"INSERT INTO source_instances ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
        list(values.values()),
    ).lastrowid


def _record(db, source_id, item_id, *, state="available", external_id=None):
    db.execute(
        "INSERT INTO source_records (source_id, item_id, external_id, title, media_type, availability, open_url) "
        "VALUES (?, ?, ?, 'Synced title', 'ebook', ?, '/books/one')",
        (source_id, item_id, external_id or f"external-{item_id}", state),
    )


def _copy(db, item_id, **fields):
    return item_copies.insert_copy(db, {"item_id": item_id, "copy_number": 1, **fields})


def _matching_ids(result):
    if result["result_kind"] == "items":
        return [item["id"] for item in result["items"]]
    return [item_id for group in result["groups"] for item_id in group["matching_ids"]]


def test_format_components_are_undirected_transitive_and_cycle_safe(db):
    a, b, c, singleton = [_item(db, str(n)) for n in range(4)]
    _link(db, b, a)
    _link(db, b, c)
    _link(db, c, a)
    assert format_groups.format_ids(db, b) == [a, b, c]
    assert format_groups.format_ids(db, b, False) == [a, c]
    assert format_groups.format_components(db) == [(a, b, c), (singleton,)]
    assert format_groups.format_ids(db, singleton) == [singleton]
    assert format_groups.format_ids(db, 99999) == []


def test_adaptations_related_edges_and_identical_titles_do_not_group(db):
    a, b, c, d, e = [_item(db, "Same title") for _ in range(5)]
    _link(db, a, b)
    _link(db, b, c, "adaptation")
    _link(db, c, d, "related")
    assert format_groups.format_components(db) == [(a, b), (c,), (d,), (e,)]
    result = library_catalog.search_catalog(db)
    assert result["matching_count"] == 5
    assert result["group_count"] == 4


def test_trash_is_not_a_format_bridge_or_a_starting_point(db):
    a, bridge, c = [_item(db, str(n)) for n in range(3)]
    _link(db, a, bridge)
    _link(db, bridge, c)
    db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (bridge,))
    assert format_groups.format_ids(db, a) == [a]
    assert format_groups.format_ids(db, c) == [c]
    assert format_groups.format_ids(db, bridge) == []
    assert format_groups.format_components(db) == [(a,), (c,)]
    assert library_catalog.search_catalog(db)["matching_count"] == 2


def test_long_components_have_no_depth_truncation(db):
    ids = [_item(db, f"Edition {n}") for n in range(1100)]
    for a, b in zip(ids, ids[1:]):
        _link(db, a, b)
    assert format_groups.format_ids(db, ids[-1]) == ids
    assert format_groups.format_components(db) == [tuple(ids)]


@pytest.mark.parametrize("name,key", [
    (None, ""), ("  Straße #1/2  ", "strasse #1/2"),
    ("C++", "c++"), ("  星の物語 #２  ", "星の物語 #２"),
    ("ÉPOPÉE", "épopée"), ("シリーズ/外伝", "シリーズ/外伝"),
])
def test_series_keys_preserve_punctuation_and_scripts(name, key):
    assert format_groups.series_name_key(name) == key


@pytest.mark.parametrize("query", [
    "STRASSE", "ÉLOÏSE", "星の物語", "NARRATRICE", "ÉDITIONS", "Saga #1/2",
    "9781234567897", "123456789X", "0000001234567", "B0ASINTEST", "HC987654321",
])
def test_unicode_and_all_core_search_fields(db, query):
    wanted = _item(db, "Straße 星の物語", authors="Éloïse", narrator="Narratrice",
                   publisher="Éditions", series_name="Saga #1/2", isbn10="123456789X",
                   upc="0000001234567", asin="B0ASINTEST", abs_id="HC987654321")
    db.execute("UPDATE items SET isbn = '9781234567897' WHERE id = ?", (wanted,))
    _item(db, "An unrelated title")
    result = library_catalog.search_catalog(db, {"q": query})
    assert _matching_ids(result) == [wanted]


def test_canonical_unicode_and_casefold_are_not_ascii_folding(db):
    accented = _item(db, "Café")
    plain = _item(db, "Cafe")
    assert _matching_ids(library_catalog.search_catalog(db, {"q": "CAFE\u0301"})) == [accented]
    assert _matching_ids(library_catalog.search_catalog(db, {"q": "cafe"})) == [plain]
    assert library_catalog.search_catalog(db, {"q": "漢字"})["matching_count"] == 0


def test_tags_and_source_and_legacy_external_ids_are_searchable(db):
    tagged = _item(db, "Tagged")
    tag_id = db.execute("INSERT INTO tags (name) VALUES ('魔法 Étoile')").lastrowid
    db.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (tagged, tag_id))
    synced = _item(db, "Synced")
    _record(db, _source(db), synced, external_id="urn:出版:ÉDITION-9")
    komga = _item(db, "Komga", media_type="comic")
    db.execute("INSERT INTO komga_records (komga_id, item_id, library_id, kind) VALUES ('KOMGA-X', ?, 'L1', 'comic')", (komga,))
    romm = _item(db, "RomM", media_type="video_game")
    db.execute("INSERT INTO romm_records (romm_id, item_id, platform_id) VALUES ('ROMM-X', ?, 'P1')", (romm,))
    music = _item(db, "Music", media_type="cd")
    db.execute("INSERT INTO music_identifiers (item_id, identifier_type, value) VALUES (?, 'discogs', 'DISCOGS-XYZ')", (music,))
    for query, expected in (("étoile", tagged), ("出版:édition", synced), ("komga-x", komga),
                            ("romm-x", romm), ("discogs-xyz", music)):
        assert _matching_ids(library_catalog.search_catalog(db, {"q": query})) == [expected]


def test_query_tokens_share_one_item_and_sql_wildcards_are_literal(db):
    one = _item(db, "Red", authors="Blue")
    two = _item(db, "Red")
    three = _item(db, "100% done_a")
    assert _matching_ids(library_catalog.search_catalog(db, {"q": "red blue"})) == [one]
    assert _matching_ids(library_catalog.search_catalog(db, {"q": "%"})) == [three]
    assert _matching_ids(library_catalog.search_catalog(db, {"q": "_"})) == [three]
    assert two not in _matching_ids(library_catalog.search_catalog(db, {"q": "blue"}))


def test_normalized_snapshot_provider_identifiers_are_searchable_not_arbitrary_json(db):
    item = _item(db, "Jellyfin title")
    _record(db, _source(db, provider="jellyfin"), item)
    db.execute("UPDATE source_records SET metadata_json = ?", (json.dumps({
        "provider_ids": {"Imdb": "tt7654321", "Tmdb": "998877", "Isbn13": "9781000000001"},
        "identifiers": {"ASIN": "B0SNAPSHOT"},
        "credentials": "do-not-index-me", "description": "not-a-raw-json-search",
    }),))
    for query in ("tt7654321", "imdb:tt7654321", "998877", "9781000000001", "b0snapshot"):
        assert _matching_ids(library_catalog.search_catalog(db, {"q": query})) == [item]
    for query in ("do-not-index-me", "not-a-raw-json-search"):
        assert library_catalog.search_catalog(db, {"q": query})["matching_count"] == 0


def test_full_live_members_and_matching_representative(db, monkeypatch):
    paper = _item(db, "A paper title", media_type="book")
    audio = _item(db, "Z matching audio", media_type="audiobook", narrator="Wanted narrator")
    film = _item(db, "A matching film", media_type="dvd", narrator="Wanted narrator")
    _link(db, paper, audio)
    _link(db, audio, film, "adaptation")
    from app.services import media_groups
    monkeypatch.setattr(media_groups, "related_ids", lambda *a, **kw: pytest.fail("all-edge grouping used"))
    result = library_catalog.search_catalog(db, {"q": "Wanted", "family": "book"})
    assert (result["matching_count"], result["group_count"]) == (1, 1)
    group = result["groups"][0]
    assert group["member_ids"] == [paper, audio]
    assert group["matching_ids"] == [audio]
    assert group["representative"]["id"] == audio
    assert {m["id"]: m["matches"] for m in group["members"]} == {paper: False, audio: True}
    assert _matching_ids(library_catalog.search_catalog(db, {"view": "items", "q": "Wanted", "family": "book"})) == [audio]


def test_compound_filters_cannot_be_satisfied_by_different_group_members(db):
    paper = _item(db, "Only paper", media_type="book", owned=1)
    digital = _item(db, "Only digital", media_type="ebook", owned=0, wishlisted=True)
    _link(db, paper, digital)
    _record(db, _source(db), digital)
    for filters in (
        {"media_type": "book", "availability": "digital"},
        {"q": "paper", "wishlist": "yes"},
        {"ownership": "owned", "wishlist": "yes"},
        {"family": "video", "media_type": "ebook"},
    ):
        assert library_catalog.search_catalog(db, filters)["matching_count"] == 0


def test_instance_identity_and_availability_must_match_the_same_holding(db):
    item = _item(db)
    first, second = _source(db), _source(db)
    _record(db, first, item, state="unavailable")
    _record(db, second, item, state="available")
    assert library_catalog.search_catalog(db, {"source": f"source:{first}", "availability": "available"})["matching_count"] == 0
    assert _matching_ids(library_catalog.search_catalog(db, {"source": f"source:{second}", "availability": "available"})) == [item]
    assert _matching_ids(library_catalog.search_catalog(db, {"source": f"source:{first}", "availability": "unavailable"})) == [item]
    choices = library_catalog.search_catalog(db)["source_choices"]
    assert {c["value"] for c in choices if c["name"] == "Same source name"} == {f"source:{first}", f"source:{second}"}


def test_source_provenance_and_owned_flag_do_not_create_holdings(db):
    provenance = _item(db, "Provenance only", source="komga", owned=1)
    unowned = _item(db, "Neither", owned=0)
    assert library_catalog.search_catalog(db, {"source": "komga"})["matching_count"] == 0
    assert library_catalog.search_catalog(db, {"availability": "available"})["matching_count"] == 0
    assert library_catalog.search_catalog(db, {"availability": "physical"})["matching_count"] == 0
    assert set(_matching_ids(library_catalog.search_catalog(db, {"availability": "unknown"}))) == {provenance, unowned}
    assert _matching_ids(library_catalog.search_catalog(db, {"ownership": "neither"})) == [unowned]
    assert library_catalog.search_catalog(db, {"wishlist": "yes"})["matching_count"] == 0


def test_physical_digital_missing_lent_unknown_and_unavailable_are_distinct(db):
    physical = _item(db, "Shelf")
    _copy(db, physical, location_id=_insert_location(db, "Living room"))
    missing = _item(db, "Missing")
    _copy(db, missing, missing_since="2026-01-01")
    lent = _item(db, "Loan")
    _copy(db, lent)
    borrower = db.execute("INSERT INTO borrowers (name) VALUES ('A friend')").lastrowid
    db.execute("INSERT INTO checkouts (item_id, borrower_id) VALUES (?, ?)", (lent, borrower))
    digital, unavailable, unknown = [_item(db, name) for name in ("Digital", "Unavailable", "Unknown")]
    source = _source(db)
    for item, state in ((digital, "available"), (unavailable, "unavailable"), (unknown, "unknown")):
        _record(db, source, item, state=state)
    for value, expected in (("physical", [physical]), ("digital", [digital]),
                            ("missing", [missing]), ("lent", [lent]),
                            ("unavailable", [unavailable]), ("unknown", [unknown])):
        assert _matching_ids(library_catalog.search_catalog(db, {"availability": value})) == expected
    assert set(_matching_ids(library_catalog.search_catalog(db, {"availability": "available"}))) == {physical, digital}


@pytest.mark.parametrize("fields", [{"enabled": 0}, {"last_status": "error"}])
def test_disabled_or_failed_source_is_unknown_not_available_or_absent(db, fields):
    item = _item(db)
    source = _source(db, **fields)
    _record(db, source, item)
    assert library_catalog.search_catalog(db, {"source": f"source:{source}", "availability": "available"})["matching_count"] == 0
    assert _matching_ids(library_catalog.search_catalog(db, {"source": f"source:{source}", "availability": "unknown"})) == [item]
    assert library_catalog.search_catalog(db, {"availability": "unavailable"})["matching_count"] == 0


def test_integration_features_gate_legacy_and_instance_holdings(db):
    item = _item(db, abs_id="ABS-X")
    source = _source(db, provider="audiobookshelf")
    _record(db, source, item)
    features.set_feature_enabled(db, "abs_sync", False)
    db.commit()
    result = library_catalog.search_catalog(db)
    assert result["groups"][0]["representative"]["holdings"] == []
    assert "abs" not in {c["value"] for c in result["source_choices"]}
    assert f"source:{source}" not in {c["value"] for c in result["source_choices"]}
    assert library_catalog.search_catalog(db, {"source": f"source:{source}"})["matching_count"] == 0


def test_trashed_items_and_individually_trashed_copies_never_leak(db):
    live = _item(db, "Live")
    copy_id = _copy(db, live)
    db.execute("UPDATE item_copies SET deleted_at = datetime('now') WHERE id = ?", (copy_id,))
    dead = _item(db, "Dead")
    _copy(db, dead)
    _record(db, _source(db), dead)
    db.execute("UPDATE items SET deleted_at = datetime('now') WHERE id = ?", (dead,))
    result = library_catalog.search_catalog(db)
    assert _matching_ids(result) == [live]
    assert result["groups"][0]["representative"]["copy_count"] == 0
    assert library_catalog.search_catalog(db, {"availability": "available"})["matching_count"] == 0


def test_grouping_and_counts_precede_pagination_without_duplicate_groups(db):
    pairs = []
    for n in range(7):
        a, b = _item(db, f"Title {n}"), _item(db, f"Title {n}", media_type="ebook")
        _link(db, a, b)
        pairs.append((a, b))
    seen = []
    for page in range(1, 5):
        result = library_catalog.search_catalog(db, {"page_size": "2"}, page=page)
        assert (result["matching_count"], result["group_count"], result["pages"]) == (14, 7, 4)
        seen.extend(tuple(g["member_ids"]) for g in result["groups"])
    assert seen == pairs
    assert len(set(seen)) == 7
    assert library_catalog.search_catalog(db, {"page_size": "2"}, page=9999)["page"] == 4
    items = library_catalog.search_catalog(db, {"page_size": "2", "view": "items"})
    assert items["pages"] == 7
    assert items["group_count"] == 7


def test_stable_ties_use_item_ids_not_graph_traversal_order(db):
    ids = [_item(db, "Same title", authors="Same creator") for _ in range(6)]
    for a, b in ((ids[5], ids[4]), (ids[3], ids[2]), (ids[1], ids[0])):
        _link(db, a, b)
    result = library_catalog.search_catalog(db, {"page_size": "2"})
    assert [g["id"] for g in result["groups"]] == [ids[0], ids[2]]
    assert [g["id"] for g in library_catalog.search_catalog(db, {"page_size": "2"}, page=2)["groups"]] == [ids[4]]


def test_local_series_keys_and_numeric_fraction_positions(db):
    expected = []
    for title, position in (("Z half", "1/2"), ("Y one", 1), ("X one quarter", 1.25),
                            ("W mixed", "1 1/2"), ("V two", 2), ("U ten", 10),
                            ("A unknown", None), ("B unknown", "not a position")):
        expected.append(_item(db, title, series_name="  Saga #1/2 星  ", series_position=position))
    other = _item(db, "Another series", series_name="Saga 1/2 星", series_position=1)
    _item(db, "No series", series_name=None)
    result = library_catalog.search_catalog(db, {"view": "series"})
    assert [s["key"] for s in result["series_results"]] == ["saga #1/2 星", "saga 1/2 星"]
    assert result["matching_count"] == 9
    detail = library_catalog.search_catalog(db, {"view": "series", "series": "SAGA #1/2 星"})
    assert _matching_ids(detail) == expected
    assert other not in _matching_ids(detail)


def test_local_series_do_not_use_provider_series_ids(db):
    for name in ("C#", "C", "星", "Épopée"):
        item = _item(db, name, media_type="comic", series_name=name)
        db.execute("INSERT INTO komga_records (komga_id, item_id, library_id, series_id, kind) "
                   "VALUES (?, ?, 'L', 'same-remote-series', 'comic')", (f"K-{item}", item))
    result = library_catalog.search_catalog(db, {"view": "series"})
    assert {s["key"] for s in result["series_results"]} == {"c#", "c", "星", "épopée"}


def test_filter_validation_discards_unknown_keys_non_scalars_and_unsafe_values():
    result = library_catalog.normalize_filters({
        "q": {"sql": "SELECT *"}, "view": "malicious", "sort": "id DESC; DROP TABLE items",
        "media_type": ["book"], "family": "unknown", "wishlist": "owned",
        "source": "source:1 OR 1=1", "availability": "offline", "ownership": "all",
        "page_size": "99999999", "series": "bad\x00name", "user_id": "42", "sql": "DROP TABLE items",
    })
    assert result == library_catalog.DEFAULT_FILTERS
    assert library_catalog.normalize_filters({"q": "x" * 301})["q"] == ""


def test_saved_filters_and_links_have_an_explicit_allowlist(db, viewer_user):
    db.execute("BEGIN IMMEDIATE")
    library_catalog.save_view(db, viewer_user["id"], "Unicode 星", {"q": "Étoile", "user_id": 99,
                                                                   "next": "https://evil.test", "page": 80})
    row = db.execute("SELECT filters_json FROM library_saved_views").fetchone()
    saved = json.loads(row["filters_json"])
    assert set(saved) == library_catalog.FILTER_KEYS
    assert saved["q"] == "Étoile"
    assert "next" not in library_catalog.library_url(saved)
    url = library_catalog.library_url({"q": "", "series": "C# / 星"}, saved_view=1, page=2)
    assert "q=" in url and "saved_view=1" in url and "page=2" in url
    assert "series=c%23+%2F+%E6%98%9F" in url
