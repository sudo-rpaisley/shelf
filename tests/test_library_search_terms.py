"""Pure helper contracts; inherited DB fixture setup is outside call-time guards."""

import builtins
import io
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from dataclasses import FrozenInstanceError

import httpx
import pytest

from app.services import format_groups, library_catalog, library_search


@pytest.fixture
def no_runtime(monkeypatch, _isolated_db):
    """Deny runtime I/O/state/providers during actual calls, not fixture/import I/O.

    The repository's unchanged autouse fixture has already initialized its
    synthetic DB. This guard neither replaces nor claims purity for that setup.
    Attempts are recorded so swallowing a forbidden exception still fails.
    """
    from app import config, database, features, i18n
    from app.services import igdb, outbound

    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append("runtime access")
        raise AssertionError("Pure Library helpers must not access runtime state or I/O")

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "app" or name.startswith("app."):
            return forbidden()
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as guard:
        for owner, names in (
            (builtins, ("open",)), (io, ("open",)),
            (os, ("open", "stat", "listdir", "scandir", "system", "popen")),
            (Path, ("open", "stat", "mkdir")),
            (sqlite3, ("connect",)), (sqlite3.dbapi2, ("connect",)),
            (database, ("get_db", "init_db", "get_setting", "get_all_settings")),
            (config, ("get_setting_value", "is_env_override")),
            (features, ("feature_enabled",)), (i18n, ("gettext",)),
            (outbound, ("fetch",)), (igdb, ("search_games",)),
            (socket, ("getaddrinfo", "create_connection")),
            (socket.socket, ("connect", "connect_ex", "sendto")),
            (httpx.Client, ("request",)), (httpx.AsyncClient, ("request",)),
            (subprocess, ("Popen",)),
        ):
            for name in names:
                guard.setattr(owner, name, forbidden)
        guard.setattr(builtins, "__import__", guarded_import)
        yield
    assert not attempts, "Forbidden access occurred, even if the helper swallowed it"


def test_fresh_import_has_only_pure_application_dependencies():
    # Import-loader code reads are explicitly allowed here, separately from the
    # stronger call-time guard. Preload standard-library dependencies, then deny
    # ordinary file, DB, process, environment and socket entry points on import.
    script = r'''
import builtins, dataclasses, io, json, math, os, pathlib, re, socket, sqlite3, subprocess, sys, unicodedata
def forbidden(*args, **kwargs):
    raise AssertionError("Application import attempted runtime I/O")
for owner, names in (
    (builtins, ("open",)), (io, ("open",)), (os, ("open", "getenv", "system")),
    (pathlib.Path, ("open", "mkdir")), (sqlite3, ("connect",)),
    (sqlite3.dbapi2, ("connect",)), (subprocess, ("Popen",)),
    (socket, ("socket", "getaddrinfo", "create_connection")),
):
    for name in names:
        setattr(owner, name, forbidden)
from app.services import library_search
actual = sorted(name for name in sys.modules if name == "app" or name.startswith("app."))
assert actual == ["app", "app.services", "app.services.isbn", "app.services.library_search", "app.services.upc"], actual
print("pure import; module-loader reads allowed")
'''
    completed = subprocess.run([sys.executable, "-B", "-c", script],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout == "pure import; module-loader reads allowed\n"


@pytest.mark.parametrize("value,expected", [
    (None, ""), (0, "0"), (False, "false"), (True, "true"), (1.25, "1.25"),
    (" \t\n", " \t\n"), ("Café", "café"), ("CAFE\u0301", "café"), ("Cafe", "cafe"),
    ("Straße ẞ", "strasse ss"), ("Σσς", "σσσ"), ("İ", "i\u0307"),
    ("ǰ", "j\u030c"), ("ΐ", "ι\u0308\u0301"), ("K", "k"),
    ("Ａ① ﬃ", "ａ① ffi"), ("100%_done\\*?[]", "100%_done\\*?[]"),
    ("星の物語\x00\x85", "星の物語\x00\x85"),
])
def test_canonical_literal_values_and_original_differential(no_runtime, value, expected):
    assert library_search.canonical(value) == expected
    assert library_search.canonical(value) == library_catalog._fold(value)


def test_canonical_does_not_add_post_casefold_nfc_or_lose_falsey_objects(no_runtime):
    class Printed:
        def __bool__(self):
            return False

        def __str__(self):
            return " ǰ 0 False "

    assert library_search.canonical(Printed()) == " j\u030c 0 false "
    assert library_search.canonical(Printed()) == library_catalog._fold(Printed())
    for point in range(0, 0x110000, 997):
        value = chr(point) + "A\u030aǰİ%_"
        assert library_search.canonical(value) == library_catalog._fold(value)


@pytest.mark.parametrize("value,expected", [
    ("Café\u0301", "cafe"), ("Ａ① ﬃ", "a1 ffi"), ("ǰ ΐ İ", "j ι i"),
    ("Ø Ł 東京", "ø ł 東京"), ("  A\t%_\n", "  a\t%_\n"),
    (None, ""), (0, "0"), (False, "false"),
])
def test_forgiving_is_explicit_compatibility_and_mark_folding(no_runtime, value, expected):
    assert library_search.forgiving_fold(value) == expected


def test_preparation_matches_current_literal_body_without_provider_json(no_runtime):
    core = {
        "title": "  Café ǰ %_", "subtitle": "星", "authors": "ÉLOÏSE",
        "isbn": "9780441172710", "isbn10": "0441172718", "upc": "bad-UPC",
        "asin": "invalid-ASIN", "narrator": "Voix", "publisher": "Éditions",
        "series_name": "  E\u0301popée #1/2  ", "abs_id": 0,
        "hardcover_book_id": False, "hardcover_edition_id": 2.5,
        "hardcover_user_book_id": None,
    }
    tags = ["魔法 Étoile", "Café", "Café"]
    external = ["urn:出版:ÉDITION-9", "imdb:tt42", "invalid legacy id\x00"]
    document = library_search.prepare_document(core, tags, external)
    assert library_search.SEARCH_FIELDS == library_catalog._SEARCH_FIELDS
    assert document.body == (
        "\n".join(library_catalog._fold(core.get(key)) for key in library_catalog._SEARCH_FIELDS)
        + "\n" + "\n".join(library_catalog._fold(value) for value in tags + external)
    )
    assert document.title_key == "  café j\u030c %_"
    assert document.creator_key == "éloïse"
    assert document.series_key == "e\u0301popée #1/2"
    assert document.series_key == format_groups.series_name_key(core["series_name"])
    assert document.series_key != library_search.canonical(core["series_name"]).strip()
    assert document.body.count("café") == 3
    assert "9780441172710" in document.body  # Deliberately invalid legacy ISBN.
    assert "invalid legacy id\x00" in document.body
    assert core["title"] == "  Café ǰ %_" and len(tags) == 3 and len(external) == 3


@pytest.mark.parametrize("series", [None, "", "  Straße #1/2 ", "C++", "Ｅ́ #２", "E\u0301", "É"])
def test_series_identity_is_the_original_trimmed_casefold_not_canonical(no_runtime, series):
    document = library_search.prepare_document({"series_name": series}, (), ())
    assert document.series_key == format_groups.series_name_key(series)


def test_document_is_frozen_detached_and_keeps_zero_false_and_whitespace(no_runtime):
    core, tags, external = {"title": 0, "authors": False}, ["  TAG  "], ["ID"]
    document = library_search.prepare_document(core, tags, external)
    core["title"], tags[0], external[0] = "changed", "changed", "changed"
    assert document.title_key == "0" and document.creator_key == "false"
    assert document.body.endswith("\n  tag  \nid")
    assert hash(document) == hash(document)
    with pytest.raises(FrozenInstanceError):
        document.body = "changed"
    assert not hasattr(document, "__dict__")
    empty = library_search.prepare_document({}, (), ())
    assert empty.body == "\n" * 14
    assert empty.title_key == empty.creator_key == empty.series_key == ""


@pytest.mark.parametrize("core", [
    None, [], (("title", "x"),), {"metadata_json": '{"secret":"not indexed"}'},
    {"description": "prose"}, {"sql": "private text"}, {1: "x"},
    {"title": {"value": "x"}}, {"authors": ["x"]}, {"title": b"x"},
    {"title": object()}, {"title": float("inf")}, {"title": float("nan")},
    {"series_name": 0}, {"series_name": False}, {"title": 1 << 20000},
])
def test_document_refuses_unknown_fields_non_plain_or_nonfinite_data(no_runtime, core):
    with pytest.raises(ValueError, match="^invalid_document$"):
        library_search.prepare_document(core, (), ())


@pytest.mark.parametrize("values", ["tag", b"tag", None, {"tag"}, {"tag": "x"}, [None],
                                          [0], [False], [["nested"]], ["x" * 513]])
def test_extra_terms_have_explicit_collection_and_string_contract(no_runtime, values):
    for tags, external in ((values, ()), ((), values)):
        with pytest.raises(ValueError, match="^invalid_document$"):
            library_search.prepare_document({}, tags, external)


def test_document_rejects_iterators_and_subclasses_without_running_user_code(no_runtime):
    def not_iterated():
        pytest.fail("unbounded iterator was consumed")
        yield "x"

    class NotStringified:
        def __str__(self):
            pytest.fail("arbitrary object was stringified")

    class DictSubclass(dict):
        def items(self):
            pytest.fail("custom mapping was traversed")

    for core, tags in (({}, not_iterated()), ({"title": NotStringified()}, ()),
                       (DictSubclass(title="x"), ())):
        with pytest.raises(ValueError, match="^invalid_document$"):
            library_search.prepare_document(core, tags, ())


def test_term_and_document_bounds_precede_dedup_and_include_fold_expansion(no_runtime):
    for tags, external in ((["x"] * 256, ["y"] * 256), (["x" * 512], [])):
        library_search.prepare_document({}, tags, external)
    for tags, external in ((["x"] * 257, []), ([], ["x"] * 257), (["x" * 512] * 128, [])):
        with pytest.raises(ValueError, match="^invalid_document$"):
            library_search.prepare_document({}, tags, external)
    for title in ("x" * (64000 - 14), "ß" * ((64000 - 14) // 2)):
        assert len(library_search.prepare_document({"title": title}, (), ()).body) == 64000
        with pytest.raises(ValueError, match="^invalid_document$"):
            library_search.prepare_document({"title": title + "x"}, (), ())
    with pytest.raises(ValueError, match="^invalid_document$"):
        library_search.prepare_document({"title": "x" * 64001}, (), ())
    # Input is exactly 64000 characters, not bytes; canonical composition shrinks it.
    assert len(library_search.prepare_document({"title": "e\u0301" * 32000}, (), ()).body) == 32014


@pytest.mark.parametrize("body,expected", [
    ("", ()), ("a", ("a",)), ("aba", ("a", "b", "ab", "ba", "aba")),
    ("%_", ("%", "_", "%_")),
    ("a\nb", ("\n", "a", "b", "\nb", "a\n", "a\nb")),
    ("é\u0301", ("é", "\u0301", "é\u0301")),
    ("星\x00", ("\x00", "星", "星\x00")),
])
def test_grams_are_distinct_literal_codepoints_with_deterministic_order(no_runtime, body, expected):
    document = library_search.PreparedDocument(body, "", "", "")
    assert library_search.document_grams(document) == expected
    assert library_search.document_grams(document) == expected


def test_grams_cover_all_substrings_without_forgiving_or_tokenization(no_runtime):
    document = library_search.prepare_document({"title": "ǰ %_ 星"}, ["same", "same"], ())
    grams = library_search.document_grams(document)
    assert len(grams) == len(set(grams))
    assert grams == tuple(sorted(grams, key=lambda value: (len(value), value)))
    assert "j\u030c" in grams and "%_" in grams and "\nsa" in grams
    for start in range(len(document.body)):
        for width in (1, 2, 3):
            if start + width <= len(document.body):
                assert document.body[start:start + width] in grams


@pytest.mark.parametrize("document", [None, "body", {}, library_search.PreparedDocument(0, "", "", ""),
                                                library_search.PreparedDocument("x" * 64001, "", "", "")])
def test_grams_refuse_non_documents_or_unbounded_bodies(no_runtime, document):
    with pytest.raises(ValueError, match="^invalid_document$"):
        library_search.document_grams(document)
