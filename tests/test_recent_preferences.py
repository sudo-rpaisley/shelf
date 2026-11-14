"""Pure recent-document coverage, not persistence/ownership/CAS acceptance."""

import copy
import json
import re

import pytest

from app.services import recent_preferences as prefs
from app import config, media_presentation


def recent(mode="mixed", groups=None, **extra):
    return {"version": 1, "mode": mode, "groups": [] if groups is None else groups, **extra}


def group(name="My row", selectors=None, **extra):
    return {"name": name, "selectors": [] if selectors is None else selectors, **extra}


def saved(*groups, **extra):
    return prefs.merge_preferences(None, {"recent": recent("custom", list(groups)), **extra})


@pytest.mark.parametrize("mode", ["mixed", "family", "format", "custom"])
def test_all_modes_round_trip_without_inventing_rows(mode):
    document = {"recent": recent(mode)}
    merged = prefs.merge_preferences(None, document)
    loaded = prefs.normalize_preferences(merged)
    assert merged == document
    assert loaded.values == document
    assert loaded.fallback_reason is None
    assert loaded.groups == ()
    assert loaded.recent_hidden is False


@pytest.mark.parametrize("document", [None, {}, {"view": "items"}])
def test_missing_recent_is_an_explicit_read_only_fallback(document):
    before = copy.deepcopy(document)
    loaded = prefs.normalize_preferences(document)
    assert loaded.values["recent"] == recent()
    assert loaded.fallback_reason == "absent"
    assert document == before
    if document is not None:
        assert "recent" not in document


@pytest.mark.parametrize("value", [None, False, 0, "", [], {}, {"version": True},
                                   recent(version="1"), recent(mode="CUSTOM"),
                                   recent(groups=False), recent(groups={})])
def test_corrupt_recent_falls_back_on_load_but_is_never_a_valid_save(value):
    document = {"view": "items", "recent": value, "future": {"enabled": False}}
    before = copy.deepcopy(document)
    loaded = prefs.normalize_preferences(document)
    assert loaded.fallback_reason == "corrupt"
    assert loaded.values == {**document, "recent": recent()}
    with pytest.raises(prefs.PreferenceValidationError):
        prefs.merge_preferences(None, document)
    assert document == before


@pytest.mark.parametrize("version", [0, 2, -1, 100])
def test_unsupported_version_is_distinct_from_corruption(version):
    document = {"recent": recent(version=version), "sort": "recent"}
    loaded = prefs.normalize_preferences(document)
    assert loaded.fallback_reason == "unsupported_version"
    assert loaded.values == {"recent": recent(), "sort": "recent"}
    with pytest.raises(prefs.PreferenceValidationError, match="unsupported_version"):
        prefs.merge_preferences(None, document)


@pytest.mark.parametrize("document", [False, 0, [], "", "false", "0", "[]", "null", "{"])
def test_invalid_whole_documents_are_not_absent(document):
    loaded = prefs.normalize_preferences(document)
    assert loaded.fallback_reason == "corrupt"
    assert loaded.values == {"recent": recent()}
    with pytest.raises(prefs.PreferenceValidationError):
        prefs.merge_preferences(document, {"sort": "recent"})


@pytest.mark.parametrize("shelves, hidden", [([], True), (["continue"], True),
                                           (["recent"], False), (False, False),
                                           (None, False)])
@pytest.mark.parametrize("value", [None, recent(version=2), recent("custom", [group()])])
def test_fallback_does_not_reset_an_explicit_f2_shelf_hide(shelves, hidden, value):
    source = {"shelves": shelves, "recent": value}
    loaded = prefs.normalize_preferences(json.dumps(source))
    assert loaded.fallback_reason in {"corrupt", "unsupported_version"}
    assert loaded.recent_hidden is hidden
    assert loaded.values["shelves"] == shelves
    assert source["recent"] == value


def test_absent_recent_also_respects_explicit_empty_shelves():
    loaded = prefs.normalize_preferences({"shelves": []})
    assert loaded.fallback_reason == "absent"
    assert loaded.recent_hidden is True
    assert loaded.values["shelves"] == []


def test_old_f2_field_save_preserves_the_whole_existing_document():
    existing = saved(group(selectors=["family:books"]), view="groups", sort="title",
                     page_size="24", default_saved_view="7", shelves=["recent", "saved:7"],
                     default_filters={"family": "book", "q": "", "future_filter": False},
                     future={"zero": 0, "false": False, "empty": [], "null": None})
    existing["recent"]["future_recent"] = {"zero": 0, "value": "\u2603"}
    existing["recent"]["groups"][0]["future_group"] = [0, False, ""]
    before = copy.deepcopy(existing)
    # These are the validated values built by the frozen C105 F2 writer.
    old_writer = {"view": "items", "sort": "recent", "page_size": "48",
                  "default_saved_view": "", "shelves": []}
    merged = prefs.merge_preferences(json.dumps(existing), old_writer)
    assert merged == {**existing, **old_writer}
    assert merged["recent"] == existing["recent"]
    assert merged["default_filters"] == existing["default_filters"]
    assert merged["future"] == {"zero": 0, "false": False, "empty": [], "null": None}
    assert existing == before
    assert prefs.normalize_preferences(merged).recent_hidden is True


@pytest.mark.parametrize("old_recent", [None, False, 0, "", [], {},
                                       recent(version=2, future={"raw": "  <tag>  "})])
def test_an_omitted_recent_is_not_reconstructed_even_if_unreadable(old_recent):
    existing = {"recent": old_recent, "future": "keep"}
    assert prefs.merge_preferences(existing, {"shelves": []}) == {
        "recent": old_recent, "future": "keep", "shelves": [],
    }


def test_old_writer_does_not_persist_the_load_fallback():
    existing = {"view": "groups", "shelves": []}
    loaded = prefs.normalize_preferences(existing)
    assert loaded.values["recent"] == recent()
    assert prefs.merge_preferences(existing, {"view": "items"}) == {
        "view": "items", "shelves": [],
    }


def test_explicit_empty_custom_groups_and_unrelated_false_values_are_not_defaults():
    existing = saved(group(selectors=["format:vinyl"]), shelves=["recent"],
                     history={"playtime": 7, "enabled": True, "events": [1]})
    changes = {"recent": recent("custom"), "shelves": [], "default_saved_view": "",
               "history": {"playtime": 0, "enabled": False, "events": []}}
    merged = prefs.merge_preferences(existing, changes)
    assert merged == {**existing, **changes}
    loaded = prefs.normalize_preferences(merged)
    assert loaded.values["recent"] == recent("custom")
    assert loaded.groups == ()
    assert loaded.fallback_reason is None
    assert loaded.recent_hidden is True


def test_union_uses_real_registry_canonical_order_and_deduplicates_within_one_row():
    selectors = ["format:vinyl", "family:books", "format:book", "family:comics", "format:vinyl"]
    document = saved(group("Print and records", selectors))
    loaded = prefs.normalize_preferences(document)
    row, = loaded.groups
    assert row.media_types == media_presentation.expand_selectors(selectors).media_types
    assert row.media_types == tuple(t for t in config.MEDIA_TYPES if t in {
        "book", "audiobook", "ebook", "vinyl", "comic", "manga",
    })
    assert row.selectors == ("format:vinyl", "family:books", "format:book", "family:comics")
    assert len(row.media_types) == len(set(row.media_types))
    assert row.mixed is False


@pytest.mark.parametrize("selectors", [[], ["all"]])
def test_empty_and_all_selectors_are_explicit_unfiltered_mixed(selectors):
    document = saved(group(selectors=selectors))
    row, = prefs.normalize_preferences(document).groups
    assert row.selectors == ()
    assert row.media_types == ()
    assert row.mixed is True  # Not a filter to the fourteen known formats.
    assert document["recent"]["groups"][0]["selectors"] == []


def test_every_canonical_format_is_filtered_not_mixed_and_aliases_are_not_selectors():
    selectors = [f"format:{media_type}" for media_type in reversed(config.MEDIA_TYPES)]
    row, = prefs.normalize_preferences(saved(group(selectors=selectors))).groups
    assert row.media_types == tuple(config.MEDIA_TYPES)
    assert row.mixed is False
    for alias in config.MEDIA_TYPE_ALIASES:
        with pytest.raises(prefs.PreferenceValidationError):
            saved(group(selectors=[f"format:{alias}"]))


@pytest.mark.parametrize("selector", ["family:books", "family:comics", "family:music",
                                      "family:films", "family:tv", "family:games",
                                      "family:periodicals"])
def test_all_presentation_families_use_the_published_registry(selector):
    row, = prefs.normalize_preferences(saved(group(selectors=[selector]))).groups
    assert row.media_types == media_presentation.expand_selectors([selector]).media_types
    assert row.media_types


@pytest.mark.parametrize("selectors", [["all", "format:book"], ["all", "all"],
                                       ["family:book"], ["family:video"],
                                       ["format:unknown"], ["format:BOOK"], [""],
                                       [False], [0], [None], [{}], "format:book", (), None])
def test_invalid_nonempty_selections_are_refused_not_broadened(selectors):
    row = group()
    row["selectors"] = selectors
    document = {"recent": recent("custom", [row])}
    before = copy.deepcopy(document)
    with pytest.raises(prefs.PreferenceValidationError):
        prefs.merge_preferences(None, document)
    assert prefs.normalize_preferences(document).fallback_reason == "corrupt"
    assert document == before


@pytest.mark.parametrize("first, second", [
    (["family:music"], ["format:vinyl", "format:cassette", "format:cd", "format:digital_music"]),
    (["family:books", "family:comics"], ["format:book", "format:audiobook", "format:ebook",
                                         "format:comic", "format:manga"]),
    ([], ["all"]),
    (["format:vinyl", "format:book"], ["format:book", "format:vinyl", "format:vinyl"]),
])
def test_duplicate_effective_groups_are_rejected_even_when_names_differ(first, second):
    with pytest.raises(prefs.PreferenceValidationError, match="duplicate_group"):
        saved(group("First", first), group("Second", second))


def test_ordered_rows_can_intentionally_overlap_including_mixed_and_vinyl():
    document = saved(group("Mixed", ["all"]), group("Vinyl", ["format:vinyl"]),
                     group("Music", ["family:music"]))
    rows = prefs.normalize_preferences(document).groups
    assert [row.name for row in rows] == ["Mixed", "Vinyl", "Music"]
    assert rows[0].mixed
    assert rows[1].media_types == ("vinyl",)
    assert "vinyl" in rows[2].media_types
    assert len({row.key for row in rows}) == 3


def test_issued_keys_are_deterministic_public_presentation_ids_and_retained_on_edits():
    first = saved(group("Before", ["family:books"]), group("Second", ["format:vinyl"]))
    repeat = saved(group("Different name", ["family:books"]), group("Also renamed", ["format:vinyl"]))
    rows = first["recent"]["groups"]
    assert [r["key"] for r in rows] == [r["key"] for r in repeat["recent"]["groups"]]
    assert all(re.fullmatch(r"recent:[0-9a-f]{32}", row["key"]) for row in rows)
    changed = recent("custom", [
        {**rows[1], "name": "Moved first", "selectors": ["family:music"]},
        {**rows[0], "name": "Moved second", "selectors": ["family:comics"]},
    ])
    merged = prefs.merge_preferences(first, {"recent": changed})
    assert [r["key"] for r in merged["recent"]["groups"]] == [rows[1]["key"], rows[0]["key"]]
    assert [r.name for r in prefs.normalize_preferences(merged).groups] == ["Moved first", "Moved second"]


def test_new_key_issuance_does_not_collide_with_an_existing_row_edited_to_other_selectors():
    first = saved(group(selectors=["family:books"]))
    old = first["recent"]["groups"][0]
    changes = {"recent": recent("custom", [
        {**old, "selectors": ["format:vinyl"]}, group("New books", ["family:books"]),
    ])}
    merged = prefs.merge_preferences(first, changes)
    old_key, new_key = [row["key"] for row in merged["recent"]["groups"]]
    assert old_key == old["key"]
    assert old_key != new_key
    assert prefs.normalize_preferences(merged).fallback_reason is None


@pytest.mark.parametrize("key", ["", "recent:1", "recent:" + "A" * 32,
                                 "item:42", "steam:123", 1, True, None])
def test_submitted_keys_must_be_valid_existing_server_issued_keys(key):
    with pytest.raises(prefs.PreferenceValidationError):
        saved(group(key=key))


def test_well_shaped_but_unrecognized_keys_are_not_accepted_from_payload():
    existing = saved(group(selectors=["format:vinyl"]))
    invented = "recent:" + "f" * 32
    assert invented != existing["recent"]["groups"][0]["key"]
    with pytest.raises(prefs.PreferenceValidationError, match="unknown_group_key"):
        prefs.merge_preferences(existing, {"recent": recent("custom", [group(key=invented)])})
    key = existing["recent"]["groups"][0]["key"]
    with pytest.raises(prefs.PreferenceValidationError, match="unknown_group_key"):
        prefs.merge_preferences(None, {"recent": recent("custom", [group(key=key)])})


def test_duplicate_keys_are_rejected_independently_of_effective_selection():
    existing = saved(group(selectors=["format:vinyl"]))
    key = existing["recent"]["groups"][0]["key"]
    with pytest.raises(prefs.PreferenceValidationError, match="duplicate_group_key"):
        prefs.merge_preferences(existing, {"recent": recent("custom", [
            group("Books", ["family:books"], key=key), group("Comics", ["family:comics"], key=key),
        ])})


def test_load_never_issues_a_missing_stored_key():
    document = {"recent": recent("custom", [group(selectors=["family:books"])])}
    loaded = prefs.normalize_preferences(document)
    assert loaded.fallback_reason == "corrupt"
    assert loaded.values["recent"] == recent()
    assert "key" not in document["recent"]["groups"][0]


def test_unknown_future_recent_and_group_fields_survive_an_explicit_new_writer_save():
    existing = saved(group(selectors=["family:books"]))
    existing["recent"]["future_recent"] = {"enabled": False}
    existing["recent"]["groups"][0]["future_group"] = [0, "", False]
    key = existing["recent"]["groups"][0]["key"]
    merged = prefs.merge_preferences(existing, {"recent": recent("custom", [
        group("Renamed", ["family:comics"], key=key),
    ])})
    assert merged["recent"]["future_recent"] == {"enabled": False}
    assert merged["recent"]["groups"][0]["future_group"] == [0, "", False]


def test_hostile_names_remain_raw_userdata_not_gettext_or_preescaped(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("User-defined row names must not enter gettext")

    monkeypatch.setattr("app.i18n.gettext", forbidden)
    name = '  <img src=x onerror="alert(1)"> & %(title)s  '
    document = saved(group(name, ["family:books"]))
    row, = prefs.normalize_preferences(document).groups
    assert row.name == name
    assert document["recent"]["groups"][0]["name"] == name


@pytest.mark.parametrize("name", ["", "  ", False, 0, None, [], "x" * 81, "a\x00b", "a\nb"])
def test_invalid_group_names_are_rejected(name):
    with pytest.raises(prefs.PreferenceValidationError):
        saved(group(name))


def test_names_accept_eighty_unicode_characters_without_normalizing_user_data():
    name = "\u754c" * 80
    document = saved(group(name))
    assert prefs.normalize_preferences(document).groups[0].name == name


def test_twelve_groups_and_thirty_two_selectors_are_bounded_before_deduplication():
    groups = [group(str(index), [f"format:{media_type}"]) for index, media_type in
              enumerate(list(config.MEDIA_TYPES)[:12])]
    assert len(prefs.normalize_preferences(saved(*groups)).groups) == 12
    with pytest.raises(prefs.PreferenceValidationError):
        saved(*groups, group("Thirteenth", ["format:video_game"]))
    assert prefs.normalize_preferences(saved(group(selectors=["format:book"] * 32))).groups[0].media_types == ("book",)
    with pytest.raises(prefs.PreferenceValidationError):
        saved(group(selectors=["format:book"] * 33))


@pytest.mark.parametrize("mode", ["mixed", "family", "format"])
def test_noncustom_modes_do_not_silently_discard_configured_groups(mode):
    with pytest.raises(prefs.PreferenceValidationError):
        prefs.merge_preferences(None, {"recent": recent(mode, [group()])})


@pytest.mark.parametrize("raw", [
    '{"recent":{"version":1,"version":1,"mode":"mixed","groups":[]}}',
    '{"recent":{"version":1,"mode":"custom","groups":[{"name":"a","name":"b","selectors":[]}]}}',
    '{"future":{"zero":0,"zero":false}}',
    '{"shelves":[],"shelves":["recent"]}',
])
def test_raw_json_duplicate_fields_are_rejected_at_every_level(raw):
    assert prefs.normalize_preferences(raw).fallback_reason == "corrupt"
    with pytest.raises(prefs.PreferenceValidationError, match="duplicate_key"):
        prefs.merge_preferences(None, raw)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "not a JSON key"},
                                  (1, 2), {"set"}, b"not-json", "\ud800"])
def test_non_json_values_and_nonfinite_numbers_are_rejected(value):
    with pytest.raises(prefs.PreferenceValidationError):
        prefs.merge_preferences(None, {"future": value})


@pytest.mark.parametrize("raw", ['{"future":NaN}', '{"future":Infinity}', b"\xff"])
def test_non_json_raw_constants_and_invalid_utf8_are_rejected(raw):
    assert prefs.normalize_preferences(raw).fallback_reason == "corrupt"
    with pytest.raises(prefs.PreferenceValidationError):
        prefs.merge_preferences(None, raw)


def test_whole_document_and_recent_payload_limits_are_utf8_bytes_not_characters():
    assert prefs.MAX_PREFERENCES_BYTES == 65_536
    assert prefs.MAX_RECENT_BYTES == 32_768
    too_large = {"future": "\u754c" * (prefs.MAX_PREFERENCES_BYTES // 3 + 1)}
    assert len(too_large["future"]) < prefs.MAX_PREFERENCES_BYTES
    with pytest.raises(prefs.PreferenceValidationError, match="payload_too_large"):
        prefs.merge_preferences(None, too_large)
    too_large_recent = {"recent": recent(future="x" * prefs.MAX_RECENT_BYTES)}
    assert prefs.normalize_preferences(too_large_recent).fallback_reason == "corrupt"
    with pytest.raises(prefs.PreferenceValidationError, match="payload_too_large"):
        prefs.merge_preferences(None, too_large_recent)
    raw = " " * (prefs.MAX_PREFERENCES_BYTES + 1)
    with pytest.raises(prefs.PreferenceValidationError, match="payload_too_large"):
        prefs.merge_preferences(None, raw)


def test_merged_payload_is_bounded_not_just_each_input():
    existing = {"first": "x" * 40_000}
    with pytest.raises(prefs.PreferenceValidationError, match="payload_too_large"):
        prefs.merge_preferences(existing, {"second": "y" * 40_000})


def test_fixed_depth_limit_applies_to_raw_json_and_python_documents():
    assert prefs.MAX_JSON_DEPTH == 12
    shallow = {"future": "[[[ literal brackets in a string ]]]"}
    assert prefs.merge_preferences(None, json.dumps(shallow)) == shallow
    nested = 0
    for _ in range(prefs.MAX_JSON_DEPTH):
        nested = [nested]
    document = {"future": nested}
    for value in (document, json.dumps(document), "[" * 2_000 + "0" + "]" * 2_000):
        assert prefs.normalize_preferences(value).fallback_reason == "corrupt"
        with pytest.raises(prefs.PreferenceValidationError, match="json_too_deep"):
            prefs.merge_preferences(None, value)


def test_cyclic_inputs_are_rejected_without_mutating_them():
    document = {}
    document["future"] = document
    with pytest.raises(prefs.PreferenceValidationError):
        prefs.merge_preferences(None, document)
    assert document["future"] is document


def test_valid_raw_json_bytes_load_and_all_results_are_detached_from_inputs():
    existing = saved(group(selectors=["family:books"]), future={"list": [0, False, ""]})
    changes = {"sort": "recent", "new_future": {"list": []}}
    before_existing, before_changes = copy.deepcopy(existing), copy.deepcopy(changes)
    loaded = prefs.normalize_preferences(json.dumps(existing).encode("utf-8"))
    merged = prefs.merge_preferences(existing, changes)
    assert loaded.fallback_reason is None
    assert loaded.values == existing
    loaded.values["recent"]["groups"][0]["selectors"].append("format:vinyl")
    merged["future"]["list"].append("changed")
    merged["new_future"]["list"].append("changed")
    assert existing == before_existing
    assert changes == before_changes
