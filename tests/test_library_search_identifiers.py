"""Snapshot parity and conservative typed identity keys; no provider/DB calls."""

import json
from dataclasses import FrozenInstanceError

import pytest

from app.services import isbn, library_catalog, library_search, upc
from tests.test_library_search_terms import no_runtime  # Shared call-time guard, not conftest changes.


pytestmark = pytest.mark.usefixtures("no_runtime")


def test_snapshot_fixed_allowlist_is_exactly_the_existing_catalogue():
    assert library_search._IDENTIFIER_KEYS == library_catalog._IDENTIFIER_KEYS == frozenset({
        "isbn", "isbn10", "isbn13", "upc", "ean", "asin", "imdb", "tmdb", "tvdb",
        "igdb", "discogs", "musicbrainz", "musicbrainzalbum", "musicbrainzreleasegroup",
        "musicbrainzrelease", "openlibrary", "googlebooks", "hardcover", "goodreads",
    })
    for key in sorted(library_catalog._IDENTIFIER_KEYS):
        payload = json.dumps({key: f"raw-{key}"})
        expected = (f"raw-{key}", f"{key}:raw-{key}")
        assert library_search.snapshot_identifiers(payload) == expected
        assert tuple(library_catalog._snapshot_identifiers(payload)) == expected


@pytest.mark.parametrize("payload", [
    None, 0, False, b'{"imdb":"tt1"}', {}, [], "", "{", "null", "false", "42", '"text"',
    "[]", '[{"imdb":"tt1"}]', '{"imdb":"tt1"} trailing', '{"imdb":}',
    "[" * 2000 + "0" + "]" * 2000, "{" * 2000,
    '{"provider_ids":[{"imdb":"tt1"}],"identifiers":null,"external_ids":"tt2"}',
    '{"imdb":true,"tmdb":1.0,"isbn":null,"asin":["B0D1234567"],"upc":{"ean":"x"}}',
])
def test_snapshot_invalid_shape_and_deep_json_match_original_empty(payload):
    assert library_search.snapshot_identifiers(payload) == ()
    assert library_catalog._snapshot_identifiers(payload) == []


def test_snapshot_order_duplicates_raw_values_and_container_depth_are_unchanged():
    payload = json.dumps({
        "external_ids": {"imdb": "tt1", "asin": "  b0d1234567  "},
        "imdb": "tt1", "tmdb": 0, "isbn13": " CAFE\u0301 ",
        "identifiers": {"imdb": "tt1", "tvdb": -3},
        "provider_ids": {"imdb": "tt1", "identifiers": {"tmdb": "nested-not-indexed"}},
        "description": "not-arbitrary-prose", "credentials": "synthetic-not-a-secret",
        "other": {"imdb": "deep-not-indexed"},
    }, ensure_ascii=False)
    expected = (
        "tt1", "imdb:tt1", "0", "tmdb:0", "CAFE\u0301", "isbn13:CAFE\u0301",
        "tt1", "imdb:tt1", "tt1", "imdb:tt1", "-3", "tvdb:-3",
        "tt1", "imdb:tt1", "b0d1234567", "asin:b0d1234567",
    )
    assert library_search.snapshot_identifiers(payload) == expected
    assert tuple(library_catalog._snapshot_identifiers(payload)) == expected


def test_snapshot_json_duplicate_key_behavior_and_typename_alias_refusal():
    payload = ('{"imdb":"first","tmdb":2,"imdb":"last"," IS_BN-13 ":"raw",'
               '"identifiers":{"MusicBrainz_Release-Group":"rg","IMDB_ID":"no",'
               '"isbn_10":"ten","isbn_type":"no","isbn13_id":"no",'
               '"musicbrainz_release_group_id":"no","abs_id":"no",'
               '"hardcover_book_id":"no","komga":"no","romm":"no",'
               '"İmdb":"no","ｉｍｄｂ":"no","im db":"no","im\\u0000db":"no"},'
               '"Provider_Ids":{"imdb":"wrong-container"}}')
    expected = ("last", "imdb:last", "2", "tmdb:2", "raw", "isbn13:raw",
                "rg", "musicbrainzreleasegroup:rg", "ten", "isbn10:ten")
    assert library_search.snapshot_identifiers(payload) == expected
    assert tuple(library_catalog._snapshot_identifiers(payload)) == expected
    # Key stripping intentionally differs from value control rejection.
    payload = json.dumps({"\timdb\n": "tt1"})
    assert library_search.snapshot_identifiers(payload) == ("tt1", "imdb:tt1")
    assert tuple(library_catalog._snapshot_identifiers(payload)) == ("tt1", "imdb:tt1")


@pytest.mark.parametrize("value,expected", [
    (" " * 511 + "x", ("x", "imdb:x")), (" " * 512 + "x", ()),
    ("x" * 512, ("x" * 512, "imdb:" + "x" * 512)), ("x" * 513, ()),
    (" " * 512, ()), ("\ttt1", ()), ("tt1\n", ()), ("tt\x001", ()),
    ("\x85tt1", ()), ("\u2003tt1\u2003", ("tt1", "imdb:tt1")),
    ("tt\u200d1", ("tt\u200d1", "imdb:tt\u200d1")),
    (0, ("0", "imdb:0")), (-42, ("-42", "imdb:-42")),
    (False, ()), (True, ()), (1.0, ()), ([], ()), ({}, ()), (None, ()),
])
def test_snapshot_scalar_type_character_bound_and_prestrip_control_rules(value, expected):
    payload = json.dumps({"imdb": value}, ensure_ascii=False)
    assert library_search.snapshot_identifiers(payload) == expected
    assert tuple(library_catalog._snapshot_identifiers(payload)) == expected


def test_snapshot_payload_limit_is_characters_not_bytes():
    shell = '{"imdb":"tt1","ignored":""}'
    payload = shell[:-2] + "星" * (64000 - len(shell)) + shell[-2:]
    assert len(payload) == 64000 and len(payload.encode("utf-8")) > 64000
    assert library_search.snapshot_identifiers(payload) == ("tt1", "imdb:tt1")
    assert library_catalog._snapshot_identifiers(payload) == ["tt1", "imdb:tt1"]
    for oversize in (payload + " ", " " * 64001):
        assert library_search.snapshot_identifiers(oversize) == ()
        assert library_catalog._snapshot_identifiers(oversize) == []


def test_snapshot_integer_bounds_nonfinite_json_and_every_cc_character():
    for number, expected in ((10 ** 511, (str(10 ** 511), "tmdb:" + str(10 ** 511))),
                             (10 ** 512, ())):
        payload = json.dumps({"tmdb": number})
        assert library_search.snapshot_identifiers(payload) == expected
        assert tuple(library_catalog._snapshot_identifiers(payload)) == expected
    for payload in ('{"imdb":NaN,"tmdb":Infinity,"tvdb":-Infinity}',
                    '{"imdb":' + "9" * 10000 + '}'):
        assert library_search.snapshot_identifiers(payload) == ()
        assert library_catalog._snapshot_identifiers(payload) == []
    for point in (*range(32), *range(127, 160)):
        payload = json.dumps({"imdb": chr(point) + "tt1"})
        assert library_search.snapshot_identifiers(payload) == ()
        assert library_catalog._snapshot_identifiers(payload) == []


@pytest.mark.parametrize("exception", [TypeError, ValueError, RecursionError])
def test_snapshot_preserves_all_original_json_exception_handling(monkeypatch, exception):
    def refused(*args, **kwargs):
        raise exception("synthetic parser refusal")

    monkeypatch.setattr(json, "loads", refused)
    assert library_search.snapshot_identifiers("{}") == ()
    assert library_catalog._snapshot_identifiers("{}") == []


def test_snapshot_exact_builtin_value_types_and_nonstring_key_filter(monkeypatch):
    class StringSubclass(str):
        pass

    class IntSubclass(int):
        pass

    monkeypatch.setattr(json, "loads", lambda payload: {
        1: "ignored", "imdb": StringSubclass("tt1"), "tmdb": IntSubclass(2), "tvdb": 3,
    })
    assert library_search.snapshot_identifiers("{}") == ("3", "tvdb:3")
    assert library_catalog._snapshot_identifiers("{}") == ["3", "tvdb:3"]


def test_typed_identifier_kinds_are_finite_explicit_and_not_snapshot_aliases():
    assert library_search.IDENTIFIER_KINDS == frozenset({
        "isbn", "isbn10", "isbn13", "upc", "ean", "asin", "imdb", "tmdb", "tvdb",
        "igdb", "discogs", "musicbrainz", "musicbrainzalbum", "musicbrainzreleasegroup",
        "musicbrainzrelease", "openlibrary", "googlebooks", "hardcover", "goodreads",
        "abs_id", "hardcover_book_id", "hardcover_edition_id", "hardcover_user_book_id",
    })
    assert library_search.validated_identifiers([]) == library_search.IdentifierKeys((), ())


@pytest.mark.parametrize("kind,value", [
    ("isbn", "054792822X"), ("isbn10", "0-547-92822-x"),
    ("isbn", "9780547928227"), ("isbn13", "978 0 547 92822 7"),
])
def test_valid_isbn10_and_13_have_one_checksum_valid_equivalent_key(kind, value):
    result = library_search.validated_identifiers([(kind, value)])
    assert result.exact == ((kind, value),)
    assert result.keys == (("isbn", "9780547928227"),)
    assert isbn.canonical_isbn_pair(value) == ("9780547928227", "054792822X")


def test_isbn979_has_no_ten_digit_equivalent_and_named_lengths_are_enforced():
    assert library_search.validated_identifiers([("isbn13", "9791234567896")]).keys == (
        ("isbn", "9791234567896"),
    )
    for entry in (("isbn10", "9780547928227"), ("isbn13", "054792822X")):
        assert library_search.validated_identifiers([entry]).keys == ()


@pytest.mark.parametrize("value", [
    "9780441172710", "0441172718", "04411X2717", "012345678905", "4006381333931",
    "978054792822", "97805479282270", "ISBN 9780547928227", "id9780547928227",
    "https://example.test/9780547928227", "９７８０５４７９２８２２７", "9780547928227\n",
    "9780547928227\x00", "9780547928227\u200d", "", " ", 9780547928227,
])
def test_bad_or_decorated_isbn_stays_exact_without_identity(value):
    result = library_search.validated_identifiers([("isbn", value)])
    assert result.exact == (("isbn", str(value)),)
    assert result.keys == ()


@pytest.mark.parametrize("upca,ean13", [
    ("012345678905", "0012345678905"), ("036000291452", "0036000291452"),
    ("078073003501", "0078073003501"),
])
def test_only_checksum_valid_upca_and_zero_prefixed_ean13_unify(upca, ean13):
    assert upc.validate_upc(upca) and upc.validate_ean13(ean13)
    for kind, value in (("upc", upca), ("upc", ean13), ("ean", ean13)):
        assert library_search.validated_identifiers([(kind, value)]).keys == (("ean", ean13),)
    assert library_search.validated_identifiers([("upc", " " + upca[:6] + "-" + upca[6:] + " ")]).keys == (
        ("ean", ean13),
    )
    # One leading zero, not arbitrary lstrip('0'); 11/14 digits never acquire identity.
    for value in (upca[1:], "0" + ean13):
        assert library_search.validated_identifiers([("upc", value)]).keys == ()


def test_nonzero_ean_remains_whole_and_bookland_is_not_a_retail_identity():
    assert library_search.validated_identifiers([("ean", "4006381333931")]).keys == (
        ("ean", "4006381333931"),
    )
    for kind, value in (("ean", "012345678905"), ("upc", "9780306406157"),
                        ("ean", "9791234567896")):
        assert library_search.validated_identifiers([(kind, value)]).keys == ()


@pytest.mark.parametrize("kind,valid", [("isbn", "9780547928227"), ("upc", "078073003501"),
                                       ("ean", "4006381333931")])
def test_every_incorrect_check_digit_is_literal_only(kind, valid):
    for digit in "0123456789":
        value = valid[:-1] + digit
        keys = library_search.validated_identifiers([(kind, value)]).keys
        assert bool(keys) is (digit == valid[-1])


@pytest.mark.parametrize("value", [
    "078073003502", "4006381333932", "UPC 078073003501", "id078073003501",
    "https://example.test/078073003501", "０７８０７３００３５０１", "078073003501\n",
    "078073003501\x00", "", " ", 78073003501,
])
def test_invalid_retail_variants_are_not_repaired_into_identity(value):
    result = library_search.validated_identifiers([("upc", value)])
    assert result.exact == (("upc", str(value)),)
    assert result.keys == ()


@pytest.mark.parametrize("value,expected", [
    (" b0d1234567 ", "B0D1234567"), ("\tb0d1234567\t", "B0D1234567"),
    ("054792822X", "054792822X"), ("B0D1234567\n", None),
    ("B0D1234567\x00", None), ("B0D123456", None), ("B0D12345678", None),
    ("B0D123456-", None), (1234567890, None), ("", None),
])
def test_asin_reuses_existing_cleaning_without_isbn_conflation(value, expected):
    result = library_search.validated_identifiers([("asin", value)])
    assert isbn.clean_asin(value) == expected
    assert result.keys == ((("asin", expected),) if expected else ())
    assert result.exact == (("asin", str(value)),)


def test_opaque_provider_ids_preserve_accents_punctuation_namespace_and_raw_exact():
    values = [("imdb", " ÉDITION-9 "), ("imdb", "E\u0301DITION-9"), ("imdb", "edition-9"),
              ("tmdb", "ÉDITION-9"), ("tmdb", 0), ("abs_id", "Ａ-9"),
              ("hardcover_book_id", 42), ("hardcover_edition_id", 42)]
    result = library_search.validated_identifiers(values)
    assert result.exact == tuple((kind, str(value)) for kind, value in values)
    assert result.keys == (
        ("abs_id", "ａ-9"), ("hardcover_book_id", "42"), ("hardcover_edition_id", "42"),
        ("imdb", "edition-9"), ("imdb", "édition-9"), ("tmdb", "0"), ("tmdb", "édition-9"),
    )
    assert library_search.forgiving_fold("édition-9") == "edition-9"
    for kind in ("imdb", "tmdb", "musicbrainzreleasegroup", "hardcover_book_id"):
        result = library_search.validated_identifiers([(kind, "ǰ")])
        assert result.keys == ((kind, "j\u030c"),)


@pytest.mark.parametrize("value", ["\timdb1", "imdb1\n", "im\x00db1", "\x85imdb1", "", " " * 512])
def test_opaque_controls_are_rejected_before_strip_but_literals_survive(value):
    result = library_search.validated_identifiers([("imdb", value)])
    assert result.exact == (("imdb", value),)
    assert result.keys == ()


def test_invalid_legacy_identifiers_remain_searchable_literal_document_text_not_identity():
    core = {"isbn": "9780441172710", "isbn10": "0441172718", "upc": "078073003502",
            "asin": "B0D123456-"}
    external = ["id078073003501", "ISBN 9780547928227"]
    document = library_search.prepare_document(core, (), external)
    typed = list(core.items()) + [("upc", external[0]), ("isbn", external[1])]
    assert library_search.validated_identifiers(typed).keys == ()
    for value in list(core.values()) + external:
        assert library_search.canonical(value) in document.body


@pytest.mark.parametrize("inputs", [
    None, {}, "imdb:tt1", {("imdb", "tt1")}, [("imdb",)], [("imdb", "tt1", "extra")],
    [{"kind": "imdb", "value": "tt1"}], [(None, "tt1")], [(1, "tt1")],
    [("IMDB", "tt1")], [(" imdb", "tt1")], [("isbn_13", "9780547928227")],
    [("musicbrainz_release_group_id", "x")], [("external_id", "ambiguous-source-id")],
    [("title", "A title")], [("imdb", False)], [("imdb", 1.0)], [("imdb", None)],
    [("imdb", {})], [("imdb", b"tt1")], [("imdb", ["tt1"])],
    [("imdb", " " + "x" * 512)], [("imdb", 1 << 20000)],
])
def test_bad_identifier_shape_kind_type_and_size_are_fixed_refusals(inputs):
    with pytest.raises(ValueError, match="^invalid_identifiers$"):
        library_search.validated_identifiers(inputs)


def test_identifier_limits_precede_trim_dedup_and_user_iterator_execution():
    result = library_search.validated_identifiers([("imdb", " " * 511 + "x")] * 256)
    assert len(result.exact) == 256 and result.keys == (("imdb", "x"),)
    with pytest.raises(ValueError, match="^invalid_identifiers$"):
        library_search.validated_identifiers([("imdb", "x")] * 257)

    def not_iterated():
        pytest.fail("unbounded identifiers iterator was consumed")
        yield ("imdb", "tt1")

    class CustomText(str):
        def __str__(self):
            pytest.fail("identifier subclass was stringified")

    for values in (not_iterated(), [("imdb", CustomText("tt1"))]):
        with pytest.raises(ValueError, match="^invalid_identifiers$"):
            library_search.validated_identifiers(values)


def test_identifier_outputs_are_frozen_detached_ordered_and_hashable():
    inputs = [["imdb", "  TT1 "], ["imdb", "  TT1 "], ["asin", "bad"]]
    result = library_search.validated_identifiers(inputs)
    inputs[0][1] = "changed"
    assert result.exact == (("imdb", "  TT1 "), ("imdb", "  TT1 "), ("asin", "bad"))
    assert result.keys == (("imdb", "tt1"),)
    assert hash(result) == hash(result)
    with pytest.raises(FrozenInstanceError):
        result.keys = ()
    assert not hasattr(result, "__dict__")


def test_identity_validation_calls_actual_existing_pure_validators(monkeypatch):
    calls = []
    for module, name in ((isbn, "canonical_isbn_pair"), (upc, "canonical_retail_barcode"),
                         (isbn, "clean_asin")):
        original = getattr(module, name)

        def record(value, *, _original=original, _name=name):
            calls.append(_name)
            return _original(value)

        monkeypatch.setattr(module, name, record)
    result = library_search.validated_identifiers([
        ("isbn", "054792822X"), ("upc", "078073003501"), ("asin", " b0d1234567 "),
        ("imdb", "078073003501"),
    ])
    assert calls == ["canonical_isbn_pair", "canonical_retail_barcode", "clean_asin"]
    assert result.keys == (("asin", "B0D1234567"), ("ean", "0078073003501"),
                           ("imdb", "078073003501"), ("isbn", "9780547928227"))


@pytest.mark.parametrize("value", ["\u1d2c", "\U0001d400"])
def test_compatibility_capitals_stay_distinct_in_canonical_and_opaque_keys(value):
    assert library_search.canonical(value) == value
    assert library_search.canonical("A") == library_search.canonical("a") == "a"
    assert library_search.canonical(value) != library_search.canonical("A")
    result = library_search.validated_identifiers([("imdb", value), ("imdb", "A"), ("imdb", "a")])
    assert result.exact == (("imdb", value), ("imdb", "A"), ("imdb", "a"))
    assert result.keys == (("imdb", "a"), ("imdb", value))
