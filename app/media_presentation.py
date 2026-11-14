"""Pure media presentation: families, strict selectors and native artwork tokens.

This vocabulary does not replace config's canonical types or Library's legacy
families. It reads no user data, permissions, feature flags, images or providers,
and builds no URLs. Labels are English msgids, translated only when rendered.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType

from app.i18n import N_


@dataclass(frozen=True, slots=True)
class FormatNode:
    selector: str
    media_type: str
    label: str


@dataclass(frozen=True, slots=True)
class FamilyNode:
    key: str
    selector: str
    label: str
    origin_tab: str | None
    formats: tuple[FormatNode, ...]


@dataclass(frozen=True, slots=True)
class ArtworkSpec:
    """Owned CSS aspect-ratio tokens for centered, contained (never cropped) art."""

    shape: str
    ratio: str
    fit: str = "contain"


@dataclass(frozen=True, slots=True)
class FilterIntent:
    """An empty media_types tuple / mixed=True means no media-type filter."""

    media_types: tuple[str, ...]
    sort: str = "recent"
    view: str = "items"
    mixed: bool = False


# Presentation order is independent of canonical expansion order. In particular,
# Books and Comics are separate here, not a redefinition of saved family=book.
_FAMILIES = (
    ("books", N_("Books"), None, ("book", "ebook", "audiobook")),
    ("comics", N_("Comics"), None, ("comic", "manga")),
    ("music", N_("Music"), "music", ("cd", "vinyl", "cassette", "digital_music")),
    ("films", N_("Films"), None, ("dvd", "digital_video")),
    ("tv", N_("TV"), None, ("tv_series",)),
    ("games", N_("Games"), None, ("video_game",)),
    ("periodicals", N_("Periodicals"), "periodicals", ("magazine",)),
)

_PORTRAIT = ArtworkSpec("portrait", "2 / 3")
_SQUARE = ArtworkSpec("square", "1 / 1")
_ARTWORK_BY_TYPE = MappingProxyType({
    "cd": _SQUARE,
    "vinyl": _SQUARE,
    "digital_music": _SQUARE,
    "audiobook": _SQUARE,
    "cassette": ArtworkSpec("cassette", "5 / 8"),
    "video_game": ArtworkSpec("game", "3 / 4"),
})
_MAX_SELECTORS = 32
_SORTS = ("recent", "title", "author", "series")
_VIEWS = ("items", "groups", "series")


def family_nodes() -> tuple[FamilyNode, ...]:
    """Return immutable families with trusted format labels resolved at call time."""
    # Deliberately lazy: config delegates artwork policy here during its import.
    from app import config

    return tuple(
        FamilyNode(
            key, f"family:{key}", label, origin_tab,
            tuple(FormatNode(f"format:{value}", value, config.MEDIA_TYPES[value]) for value in formats),
        )
        for key, label, origin_tab, formats in _FAMILIES
    )


def expand_selectors(selectors: Sequence[str]) -> tuple[str, ...]:
    """Expand exact selectors in canonical config order; reject malformed input.

    Only an empty sequence or the sole selector 'all' means no filter, including
    unknown legacy types. Aliases, bare strings, whitespace and mixed 'all' are
    not repaired into a broader selection. The bound applies before deduplication.
    """
    if isinstance(selectors, (str, bytes, bytearray)) or not isinstance(selectors, Sequence):
        raise ValueError("Media selectors must be a sequence of strings")
    if len(selectors) > _MAX_SELECTORS:
        raise ValueError("At most 32 media selectors are allowed")
    if any(not isinstance(selector, str) for selector in selectors):
        raise ValueError("Every media selector must be a string")
    if not selectors or (len(selectors) == 1 and selectors[0] == "all"):
        return ()
    if "all" in selectors:
        raise ValueError("The all selector must be used alone")

    from app import config

    families = {f"family:{key}": formats for key, _, _, formats in _FAMILIES}
    selected = set()
    for selector in selectors:
        if selector in families:
            selected.update(families[selector])
        elif selector.startswith("format:") and selector[7:] in config.MEDIA_TYPES:
            selected.add(selector[7:])
        else:
            raise ValueError("Unknown media selector")
    return tuple(value for value in config.MEDIA_TYPES if value in selected)


def artwork_spec(media_type: str | None, known_subtype: str | None = None) -> ArtworkSpec:
    """Return the native type box; unregistered subtypes retain that default.

    Subtype text and image dimensions never become CSS. Unknown types use 2:3;
    cassette shell/landscape art stays intact within its default 5:8 J-card box.
    """
    return _ARTWORK_BY_TYPE.get(media_type, _PORTRAIT)


def filter_intent(
    selectors: Sequence[str], sort: str = "recent", view: str = "items",
) -> FilterIntent:
    """Build a filter intent, not a URL, query, saved view or permission decision."""
    media_types = expand_selectors(selectors)
    if not isinstance(sort, str) or sort not in _SORTS:
        raise ValueError("Unknown media sort")
    if not isinstance(view, str) or view not in _VIEWS:
        raise ValueError("Unknown media view")
    return FilterIntent(media_types, sort, view, mixed=not media_types)
