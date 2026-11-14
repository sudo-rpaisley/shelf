"""Reachable semantics-preserving delegation, not a persistent search index."""

import json

import pytest

from app.services import library_catalog, library_search
from tests.conftest import _insert_item


@pytest.mark.parametrize("value,expected", (
    (None, ""), (0, "0"), (False, "false"), ("  A  ", "  a  "),
    ("CAFE\u0301 Straße", "café strasse"), ("\u1d2c", "\u1d2c"),
    ("\U0001d400", "\U0001d400"), ("100%_", "100%_"),
))
def test_live_fold_keeps_explicit_canonical_compatibility_vectors(value, expected):
    assert library_catalog._fold(value) == expected


def test_catalogue_fold_forwards_the_original_value_to_the_real_helper_once(monkeypatch):
    calls = []
    original = library_search.canonical

    class Printed:
        def __str__(self):
            calls.append("str")
            return "A\u030A"

    value = Printed()

    def observed(actual):
        assert actual is value
        calls.append("canonical")
        return original(actual)

    monkeypatch.setattr(library_search, "canonical", observed)
    assert library_catalog._fold(value) == "å"
    assert calls == ["canonical", "str"]


def test_snapshot_wrapper_keeps_its_ordered_fresh_list_and_delegates_once(monkeypatch):
    payload = json.dumps({
        "ImDb": "tt42", "provider_ids": {"ASIN": "B0SNAP", "tmdb": 0},
        "identifiers": {"ASIN": "B0SNAP"}, "credentials": "do-not-index",
    })
    expected = ["tt42", "imdb:tt42", "B0SNAP", "asin:B0SNAP", "0", "tmdb:0", "B0SNAP", "asin:B0SNAP"]
    calls, original = [], library_search.snapshot_identifiers

    def observed(actual):
        calls.append(actual)
        return original(actual)

    monkeypatch.setattr(library_search, "snapshot_identifiers", observed)
    first = library_catalog._snapshot_identifiers(payload)
    assert type(first) is list and first == expected and calls == [payload]
    first.append("caller-owned modification")
    second = library_catalog._snapshot_identifiers(payload)
    assert type(second) is list and second == expected and first is not second
    assert calls == [payload, payload]


def test_real_catalogue_uses_only_canonical_and_named_snapshot_helpers_not_index_foundations(db, viewer_client, monkeypatch):
    wanted = _insert_item(db, title="Café source", isbn=None, media_type="book")
    _insert_item(db, title="Cafe other", isbn=None, media_type="book")
    source = db.execute(
        "INSERT INTO source_instances (name, provider, base_url) "
        "VALUES ('Synthetic', 'opds', 'https://books.example.test')",
    ).lastrowid
    payload = json.dumps({"provider_ids": {"Tmdb": "7654321"}, "credentials": "never-search-this"})
    db.execute(
        "INSERT INTO source_records (source_id, item_id, external_id, title, media_type, metadata_json) "
        "VALUES (?, ?, 'fixture-record', 'Synthetic source title', 'book', ?)",
        (source, wanted, payload),
    )
    db.commit()
    before = tuple(db.iterdump())
    calls = {"canonical": [], "snapshot": []}
    canonical, snapshot = library_search.canonical, library_search.snapshot_identifiers

    def observe_canonical(value):
        calls["canonical"].append(value)
        return canonical(value)

    def observe_snapshot(value):
        calls["snapshot"].append(value)
        return snapshot(value)

    def not_enabled(*args, **kwargs):
        raise AssertionError("Forgiving, identifier ranking and index preparation remain unwired")

    monkeypatch.setattr(library_search, "canonical", observe_canonical)
    monkeypatch.setattr(library_search, "snapshot_identifiers", observe_snapshot)
    for name in ("forgiving_fold", "prepare_document", "document_grams", "validated_identifiers"):
        monkeypatch.setattr(library_search, name, not_enabled)
    result = library_catalog.search_catalog(db, {"q": "TMDB:7654321", "view": "items"})
    assert [item["id"] for item in result["items"]] == [wanted]
    assert "TMDB:7654321" in calls["canonical"] and calls["snapshot"] == [payload]
    assert library_catalog.search_catalog(db, {"q": "never-search-this"})["matching_count"] == 0
    calls["canonical"].clear()
    calls["snapshot"].clear()
    response = viewer_client.get("/api/library/search", params={"q": "TMDB:7654321", "view": "items"})
    assert response.status_code == 200 and f'data-library-member="{wanted}"' in response.text
    assert "TMDB:7654321" in calls["canonical"] and calls["snapshot"] == [payload]
    assert tuple(db.iterdump()) == before
    assert not db.in_transaction
