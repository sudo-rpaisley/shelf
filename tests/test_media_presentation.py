"""Pure presentation vocabulary, strict selector unions and native artwork boxes."""

import ast
import builtins
import sqlite3
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app import config, media_presentation as presentation


CANONICAL_TYPES = (
    "book", "audiobook", "ebook", "magazine", "dvd", "digital_video",
    "tv_series", "vinyl", "cassette", "cd", "digital_music", "comic",
    "manga", "video_game",
)
FAMILIES = (
    ("books", "Books", None, ("book", "ebook", "audiobook")),
    ("comics", "Comics", None, ("comic", "manga")),
    ("music", "Music", "music", ("cd", "vinyl", "cassette", "digital_music")),
    ("films", "Films", None, ("dvd", "digital_video")),
    ("tv", "TV", None, ("tv_series",)),
    ("games", "Games", None, ("video_game",)),
    ("periodicals", "Periodicals", "periodicals", ("magazine",)),
)
ARTWORK = {
    "book": ("portrait", "2 / 3"),
    "audiobook": ("square", "1 / 1"),
    "ebook": ("portrait", "2 / 3"),
    "magazine": ("portrait", "2 / 3"),
    "dvd": ("portrait", "2 / 3"),
    "digital_video": ("portrait", "2 / 3"),
    "tv_series": ("portrait", "2 / 3"),
    "vinyl": ("square", "1 / 1"),
    "cassette": ("cassette", "5 / 8"),
    "cd": ("square", "1 / 1"),
    "digital_music": ("square", "1 / 1"),
    "comic": ("portrait", "2 / 3"),
    "manga": ("portrait", "2 / 3"),
    "video_game": ("game", "3 / 4"),
}


def test_families_cover_all_fourteen_canonical_types_once_in_presentation_order():
    assert tuple(config.MEDIA_TYPES) == CANONICAL_TYPES
    nodes = presentation.family_nodes()
    assert isinstance(nodes, tuple)
    assert [(node.key, node.label, node.origin_tab) for node in nodes] == [
        (key, label, origin_tab) for key, label, origin_tab, _ in FAMILIES
    ]
    selectors = []
    media_types = []
    for node, (key, _, _, formats) in zip(nodes, FAMILIES, strict=True):
        assert isinstance(node, presentation.FamilyNode)
        assert node.selector == f"family:{key}"
        assert isinstance(node.formats, tuple)
        assert tuple(child.media_type for child in node.formats) == formats
        selectors.append(node.selector)
        for child in node.formats:
            assert isinstance(child, presentation.FormatNode)
            assert child.selector == f"format:{child.media_type}"
            assert child.label == config.MEDIA_TYPES[child.media_type]
            selectors.append(child.selector)
            media_types.append(child.media_type)
    assert len(selectors) == len(set(selectors)) == 21
    assert len(media_types) == len(set(media_types)) == 14
    assert set(media_types) == set(CANONICAL_TYPES)


@pytest.mark.parametrize("key,label,origin_tab,formats", FAMILIES)
def test_each_family_expands_in_canonical_not_display_order(key, label, origin_tab, formats):
    expected = tuple(value for value in CANONICAL_TYPES if value in formats)
    assert presentation.expand_selectors([f"family:{key}"]) == expected
    intent = presentation.filter_intent([f"family:{key}"])
    assert intent.media_types == expected
    assert intent.sort == "recent"
    assert intent.view == "items"
    assert intent.mixed is False


@pytest.mark.parametrize("media_type", CANONICAL_TYPES)
def test_every_format_selector_is_unambiguous(media_type):
    assert presentation.expand_selectors([f"format:{media_type}"]) == (media_type,)


def test_unions_deduplicate_and_ignore_selection_order_without_broadening():
    selectors = [
        "format:video_game", "family:music", "format:cd", "family:books",
        "format:book", "family:music",
    ]
    expected = (
        "book", "audiobook", "ebook", "vinyl", "cassette", "cd",
        "digital_music", "video_game",
    )
    assert presentation.expand_selectors(selectors) == expected
    assert presentation.expand_selectors(tuple(reversed(selectors))) == expected
    intent = presentation.filter_intent(selectors)
    selectors.append("format:dvd")
    assert intent.media_types == expected  # No caller-owned mutable collection survives.


@pytest.mark.parametrize("selectors", [[], (), ["all"]])
def test_mixed_means_no_filter_including_unknown_legacy_types(selectors):
    assert presentation.expand_selectors(selectors) == ()
    intent = presentation.filter_intent(selectors)
    assert intent.media_types == ()
    assert intent.mixed is True
    # An empty filter must not be materialised as only the fourteen known types.
    assert intent.media_types != CANONICAL_TYPES


def test_explicit_union_of_all_known_families_is_still_a_filter():
    selectors = [f"family:{key}" for key, _, _, _ in FAMILIES]
    intent = presentation.filter_intent(selectors)
    assert intent.media_types == CANONICAL_TYPES
    assert intent.mixed is False


@pytest.mark.parametrize("selectors", [
    None, 42, "", "all", b"all", {"family:books": True}, {"family:books"},
    [""], [" "], ["ALL"], ["all", "all"], ["all", "format:book"],
    ["format:book", "all"], ["family:book"], ["family:Books"], ["family:video"],
    ["book"], ["format:music"], ["format:CD"], ["format:unknown"],
    ["format:book", ""], ["format:book", None], ["format:book", 7],
    ["format:book", {}], ["family:books", "format:unknown"],
    [" family:books"], ["format:book "], ["format:book\n"],
])
def test_invalid_selections_raise_instead_of_silently_becoming_mixed(selectors):
    with pytest.raises(ValueError):
        presentation.expand_selectors(selectors)
    with pytest.raises(ValueError):
        presentation.filter_intent(selectors)


def test_aliases_remain_input_only_and_are_not_presentation_selectors():
    for alias, canonical in config.MEDIA_TYPE_ALIASES.items():
        assert config.canonical_media_type(alias) == canonical
        with pytest.raises(ValueError):
            presentation.expand_selectors([f"format:{alias}"])
    assert config.BOOK_MEDIA_TYPES == frozenset({"book", "audiobook", "ebook", "comic", "manga"})
    from app.services import library_catalog

    assert library_catalog.FAMILIES["book"][1] == config.BOOK_MEDIA_TYPES


def test_selector_limit_is_checked_before_deduplication():
    assert presentation.expand_selectors(["format:cd"] * 32) == ("cd",)
    with pytest.raises(ValueError):
        presentation.expand_selectors(["format:cd"] * 33)
    with pytest.raises(ValueError):
        presentation.filter_intent(["format:cd"] * 33)


@pytest.mark.parametrize("media_type,expected", ARTWORK.items())
def test_all_native_artwork_tokens_are_finite_owned_contain_specs(media_type, expected):
    spec = presentation.artwork_spec(media_type)
    assert isinstance(spec, presentation.ArtworkSpec)
    assert (spec.shape, spec.ratio) == expected
    assert spec.fit == "contain"
    assert config.cover_shape(media_type) == spec.shape


def test_cassette_is_a_portrait_j_card_not_a_square_cd_or_a_film_poster():
    cassette = presentation.artwork_spec("cassette")
    assert cassette == presentation.ArtworkSpec("cassette", "5 / 8")
    assert presentation.artwork_spec("cd") == presentation.ArtworkSpec("square", "1 / 1")
    assert presentation.artwork_spec("dvd") == presentation.ArtworkSpec("portrait", "2 / 3")


@pytest.mark.parametrize("media_type", CANONICAL_TYPES)
def test_unknown_subtypes_keep_the_native_type_default(media_type):
    for subtype in (None, "", "unknown", "landscape-shell", "1 / 0; color:red"):
        assert presentation.artwork_spec(media_type, subtype) == presentation.artwork_spec(media_type)


@pytest.mark.parametrize("media_type", [None, "", "legacy_unknown", "1 / 0; color:red"])
def test_unknown_media_never_supplies_css_or_a_new_subtype_policy(media_type):
    assert presentation.artwork_spec(media_type, "square") == presentation.ArtworkSpec("portrait", "2 / 3")


def test_config_compatibility_uses_registry_authority(monkeypatch):
    assert config.SQUARE_COVER_MEDIA_TYPES == frozenset({"audiobook", "vinyl", "cd", "digital_music"})
    assert config.SQUARE_COVER_MEDIA_TYPES == frozenset(
        value for value in config.MEDIA_TYPES if presentation.artwork_spec(value).shape == "square"
    )
    calls = []

    def replacement(media_type):
        calls.append(media_type)
        return presentation.ArtworkSpec("game", "3 / 4")

    monkeypatch.setattr(presentation, "artwork_spec", replacement)
    assert config.cover_shape("book") == "game"
    assert calls == ["book"]


def test_dtos_and_nested_results_are_immutable():
    family = presentation.family_nodes()[0]
    intent = presentation.filter_intent([family.selector])
    for node, attribute in (
        (family, "label"), (family.formats[0], "label"),
        (presentation.artwork_spec("cd"), "ratio"), (intent, "sort"),
    ):
        with pytest.raises((FrozenInstanceError, AttributeError)):
            setattr(node, attribute, "changed")
    assert isinstance(intent.media_types, tuple)
    with pytest.raises(TypeError):
        family.formats[0] = family.formats[0]


def test_labels_and_expansion_resolve_canonical_config_at_call_time(monkeypatch):
    original = presentation.family_nodes()
    values = dict(reversed(tuple(config.MEDIA_TYPES.items())))
    values["book"] = "Trusted replacement label"
    monkeypatch.setattr(config, "MEDIA_TYPES", values)
    assert presentation.family_nodes()[0].formats[0].label == "Trusted replacement label"
    assert original[0].formats[0].label == "Book"
    assert presentation.expand_selectors(["family:books"]) == ("ebook", "audiobook", "book")


def test_all_display_labels_are_marked_literals_not_dynamic_translations():
    marked = set()
    for module in (config, presentation):
        tree = ast.parse(Path(module.__file__).read_text())
        marked.update(
            node.args[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "N_" and node.args
            and isinstance(node.args[0], ast.Constant)
        )
    assert {label for _, label, _, _ in FAMILIES} <= marked
    assert set(config.MEDIA_TYPES.values()) <= marked


def test_registry_never_reads_user_data_permissions_images_or_translations(monkeypatch):
    from app import database, features, i18n, nav

    def forbidden(*args, **kwargs):
        raise AssertionError("Pure presentation must not read user data or runtime authority")

    with monkeypatch.context() as isolated:
        for target, attribute in (
            (builtins, "open"), (Path, "open"), (Path, "stat"),
            (sqlite3, "connect"), (database, "get_db"),
            (features, "feature_enabled"), (nav, "visible_tabs"), (i18n, "gettext"),
        ):
            isolated.setattr(target, attribute, forbidden)
        nodes = presentation.family_nodes()
        assert presentation.expand_selectors([nodes[0].selector]) == ("book", "audiobook", "ebook")
        assert presentation.filter_intent([]).mixed is True
        assert presentation.artwork_spec("cassette", "User / provider supplied name").ratio == "5 / 8"


def test_filter_intent_preserves_supported_explicit_sort_and_view():
    for sort in ("recent", "title", "author", "series"):
        for view in ("items", "groups", "series"):
            intent = presentation.filter_intent(["format:book"], sort=sort, view=view)
            assert (intent.sort, intent.view) == (sort, view)


@pytest.mark.parametrize("value", [None, [], True, "", "unknown", "https://example.invalid/"])
def test_filter_intent_rejects_unknown_or_nonscalar_sort_and_view(value):
    with pytest.raises(ValueError):
        presentation.filter_intent([], sort=value)
    with pytest.raises(ValueError):
        presentation.filter_intent([], view=value)


@pytest.mark.parametrize("order", [
    ("app.config", "app.media_presentation"),
    ("app.media_presentation", "app.config"),
])
def test_both_fresh_import_orders_avoid_cycles_and_application_boot(order):
    source = f"""
import importlib
import sys
for name in {order!r}:
    importlib.import_module(name)
from app import config, media_presentation
assert config.cover_shape('cassette') == 'cassette'
assert media_presentation.family_nodes()[0].formats[0].label == config.MEDIA_TYPES['book']
assert not {{'app.main', 'app.database', 'app.nav', 'app.features'}}.intersection(sys.modules)
print('fresh import order OK')
"""
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "fresh import order OK"
