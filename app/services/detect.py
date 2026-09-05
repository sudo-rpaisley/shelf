"""Media-type detection for a freshly scanned item.

Pure functions only — no I/O, no `httpx`, no DB, no imports from
`app.routers`. `app.config` is fine to import; it is pure data.

Four tiers, tried in order, each one only allowed to act on evidence it
actually has:

1. Barcode prefix — an ISBN (978/979 EAN-13, or ISBN-10) is a book-family
   item. A UPC/EAN that is *not* an ISBN carries no format information by
   itself (a UPC is issued to the retail product, not to "books" or
   "discs"), so it falls through to the next tier instead of deciding here.
2. Title markers, in six arms: platform names (PS5, Nintendo Switch,
   PC CD, ...) say video_game; retail video format tags ([DVD], Blu-ray, ...)
   say dvd; software-medium tags (CD-ROM) say video_game; explicit vinyl
   packaging tags say vinyl; explicit cassette tags say cassette; audio-CD
   tags say cd. The order is deliberately conservative: a DVD bundle that
   includes a soundtrack LP/CD/cassette must still file as the video format
   printed on the product, and CD-ROM must beat the bare CD token.
3. Category — only categories that name the product medium itself may decide:
   video-game software, vinyl records, music/audio cassettes, and music CDs.
   No category ever decides dvd, and no category merely naming a console or
   platform decides video_game.
4. No signal, in three parts. **First**, recognised hardware: a title
   carrying a hardware word *and* a platform marker (`_is_hardware_title`)
   is a console, controller or headset. That is a weaker answer than a
   detection and a stronger one than nothing — it says what the item is
   *not*, which is enough for a caller to decline a film search it would
   otherwise lose (`signal="hardware"`). It sits above the hint branch
   deliberately: a dropdown choice asserts what the item *is*, not that a
   film search on a title containing "Console" will match. Then a deliberate
   non-book hint stands (`signal="hinted"`); otherwise resolve to a concrete
   `MEDIA_TYPES` member anyway and say in the reason that it is a fallback,
   never a detection (`signal="none"`). The `media_type` is **always** a
   `MEDIA_TYPES` member — never `"auto"`, never an unchecked hint string.

The return is a `Detection`, not a bare tuple: the tier that decided is worth
more to a caller than the verdict alone. `_scan_upc` reads `signal` to skip
the TMDb ladder for hardware, because that ladder's shortest rung answers
"PlayStation" with a confident match for a different film (`G46` — missing
enrichment is recoverable, wrong is not).

G46 (see GOTCHAS.md): this module reads the *raw* scanned title, never a
shortened search-query rung. `app/services/upcitemdb.py`'s `search_queries`
ladder strips exactly the platform/format markers tier 2 matches on — by the
time a title reaches its shortest rung, "[DVD]" and "(PC DVD)" are already
gone. Callers must pass the title as scanned, not a ladder rung.
"""

import re
from dataclasses import dataclass
from typing import Literal

from app.config import MEDIA_TYPES

# How detection reached its verdict. Callers branch on what the answer is
# *worth*, never on which tier produced it — a tier number is a fact about
# this module's internal order and would invite `tier >= 4` comparisons that
# silently acquire meaning when a tier is inserted.
Signal = Literal["detected", "hinted", "hardware", "none"]

# The same names as `Signal`, in the same order, as a runtime value. Keep it
# and the `Literal` in step — a test pins that they agree.
SIGNALS: tuple[str, ...] = ("detected", "hinted", "hardware", "none")


@dataclass(frozen=True, slots=True)
class Detection:
    """What detection decided, and how much that verdict is worth.

    Deliberately **no** `__bool__`, unlike `ProviderResult`. That class raises
    because every pre-existing caller was written `if result:` and a dataclass
    is always truthy. Every caller here *unpacks a tuple*, which a dataclass
    refuses loudly on its own — a raising `__bool__` would defend nothing.
    """

    media_type: str   # always a MEDIA_TYPES member, as today
    reason: str       # the card's prose, as today
    signal: str       # a SIGNALS member


# Hints that mean "this is some kind of book" and are honoured as-is when the
# barcode is an ISBN. Not every MEDIA_TYPES key is book-family — discs, games
# and music formats are physical/digital media, not books, even though they
# are valid hints on a non-ISBN scan.
_BOOK_FAMILY_HINTS = frozenset({"book", "kids_book", "audiobook", "ebook", "comic", "digital_comic"})

# --- Tier 2: title markers -------------------------------------------------
#
# Platform names checked first, video-format tags second, software media
# third, then the music media. "Alice Madness Returns (PC DVD)" is a game
# whose title carries "DVD"; platform-first preserves it. "Purple Rain
# [DVD/CD Combo]" is a film bundle that carries "CD"; video-format-first
# preserves it. The same rule applies to a DVD bundled with an LP/cassette.
#
# Two prohibitions, both learned from the probe sample, apply to tier 3:
#   - No category value ever decides dvd. Sampled discs were categorised as
#     Electronics > Video > Televisions.
#   - No category that merely names a platform ever decides video_game. A
#     platform category describes the shelf the product sits on, not whether
#     it is software; it held a cartridge and a console in the same sample.
_PLATFORM_MARKERS = [
    "Nintendo Switch", "Wii U", "Nintendo 3DS",
    "PlayStation 5", "PlayStation 4", "PlayStation 3", "PlayStation",
    "PS5", "PS4", "PS3",
    "Xbox Series X", "Xbox One", "Xbox 360", "Xbox",
    "PC DVD", "PC CD",
]

_HARDWARE_TERMS = ["console", "controller", "headset"]


def _is_hardware_title(title: str) -> bool:
    """A hardware word *conjoined with* a platform marker.

    A hardware word alone is not enough — `Console Wars`, `Air Traffic
    Controller` and `The Controller 2019` are films. The conjunction is also
    what the suppression below has always meant in practice: `is_hardware`
    can only change the outcome when a platform marker would otherwise have
    matched, so requiring both is behaviour-preserving for tier 2 while
    being narrow enough to act on at tier 4.

    Deliberately narrow. `Sony PULSE 3D Wireless Headset` names no member of
    `_PLATFORM_MARKERS` and is a **known, accepted** false negative; widening
    either table to catch it re-opens the three film titles above.
    """
    if not any(_contains_marker(title, term) for term in _HARDWARE_TERMS):
        return False
    return any(_contains_marker(title, marker) for marker in _PLATFORM_MARKERS)


_FORMAT_MARKERS = [
    "[DVD]", "Blu-ray", "Bluray", "4K Ultra HD", "4K UHD", "UHD", "DVD",
]

# Software-medium tags, checked *after* video-format tags and before any music
# medium. `_contains_marker` treats the hyphen as a word boundary, so the bare
# CD marker also matches inside CD-ROM; this arm prevents a PC game becoming a
# music disc.
_MEDIUM_MARKERS = ["CD-ROM"]

# Vinyl detection is intentionally stricter than a bare word "Vinyl". The
# existing adversary tests include a film titled exactly `Vinyl`; accepting
# that token alone would turn an explicit known false positive into a
# confident classification. Retail format phrases/tags are much stronger.
_VINYL_MARKERS = [
    "[Vinyl]", "(Vinyl)", "Vinyl LP", "12\" Vinyl", "10\" Vinyl", "7\" Vinyl",
    "12-inch Vinyl", "10-inch Vinyl", "7-inch Vinyl",
]

# Same principle for cassette: medium-specific retail phrases, not the bare
# word in arbitrary prose. Category detection below catches records whose
# product title contains no format suffix at all.
_CASSETTE_MARKERS = [
    "Audio Cassette", "Cassette Tape", "[Cassette]", "(Cassette)",
]

# Audio tags, checked after CD-ROM and after the more-specific vinyl/cassette
# media. Most specific first so the reason names the fuller tag when present.
_AUDIO_MARKERS = ["Audio CD", "Compact Disc", "CD"]


def _contains_marker(text: str, marker: str) -> bool:
    """Case-insensitive substring match with word-ish boundaries.

    Plain `in` would let a short token like "PS4" or "UHD" fire on a
    coincidental substring inside a longer word; the lookaround here
    requires the character on either side of the match (if any) to be
    non-alphanumeric, which also does the right thing for punctuation-only
    markers like "[DVD]".
    """
    pattern = r"(?<![A-Za-z0-9])" + re.escape(marker.lower()) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text.lower()) is not None


def _match_title_markers(title: str) -> Detection | None:
    """Tier 2. A `Detection`, or None if the title says nothing."""
    if not _is_hardware_title(title):
        for marker in _PLATFORM_MARKERS:
            if _contains_marker(title, marker):
                return Detection("video_game", (
                    f"Title names the {marker} platform — filed as Video Game."
                ), "detected")
    for marker in _FORMAT_MARKERS:
        if _contains_marker(title, marker):
            return Detection("dvd", (
                f"Title carries a '{marker}' format tag — filed as DVD / Blu-ray."
            ), "detected")
    for marker in _MEDIUM_MARKERS:
        if _contains_marker(title, marker):
            return Detection("video_game", (
                f"Title carries a '{marker}' software-medium tag — filed as Video Game."
            ), "detected")
    for marker in _VINYL_MARKERS:
        if _contains_marker(title, marker):
            return Detection("vinyl", (
                f"Title carries a '{marker}' music-format tag — filed as Vinyl."
            ), "detected")
    for marker in _CASSETTE_MARKERS:
        if _contains_marker(title, marker):
            return Detection("cassette", (
                f"Title carries a '{marker}' music-format tag — filed as Cassette."
            ), "detected")
    for marker in _AUDIO_MARKERS:
        if _contains_marker(title, marker):
            return Detection("cd", (
                f"Title carries a '{marker}' audio tag — filed as CD."
            ), "detected")
    return None


def _category_decides_video_game(category: str) -> bool:
    """Only an actual video-game *software* category decides a game."""
    return "video game software" in category.lower()


def _category_decides_vinyl(category: str) -> bool:
    """A category explicitly naming vinyl records, not generic music."""
    value = category.lower()
    return "vinyl record" in value or "vinyl records" in value


def _category_decides_cassette(category: str) -> bool:
    """A category explicitly naming the cassette medium."""
    value = category.lower()
    return "music cassette" in value or "audio cassette" in value


def _category_decides_cd(category: str) -> bool:
    """A category explicitly naming Music CDs."""
    return "music cd" in category.lower()


def detect_media_type(
    barcode_type: str, hint: str, title: str | None, category: str | None,
) -> Detection:
    """Return a `Detection`. `reason` is what the scan card shows.

    `signal` says how much the verdict is worth: `detected` if a tier 1-3
    rule fired, `hardware` if the title names console hardware, `hinted` if
    the user's non-book dropdown choice stood, `none` if nothing at all did.

    `media_type` is always a member of `app.config.MEDIA_TYPES` — never
    `"auto"`, never a hint passed through unchecked. `title` must be the raw
    scanned title, not a shortened search-query rung (G46).
    """
    hint = hint if hint in MEDIA_TYPES else None

    # Tier 1: barcode prefix. Only an ISBN decides anything at this tier.
    if barcode_type == "isbn":
        if hint in _BOOK_FAMILY_HINTS:
            return Detection(hint, (
                f"Hint '{MEDIA_TYPES[hint]}' confirmed by ISBN barcode."
            ), "detected")
        if hint is not None:
            return Detection("book", (
                f"ISBN barcodes are books — overriding the "
                f"'{MEDIA_TYPES[hint]}' hint to Book."
            ), "detected")
        return Detection("book", "ISBN barcode — filed as Book.", "detected")

    # Tier 2: title markers.
    if title:
        matched = _match_title_markers(title)
        if matched is not None:
            return matched

    # Tier 3: categories that name the medium itself. Reached only when tier 2
    # found nothing, so a category can never override a stronger title signal.
    if category and _category_decides_video_game(category):
        return Detection("video_game", (
            f"Category '{category}' names video game software — filed as Video Game."
        ), "detected")
    if category and _category_decides_vinyl(category):
        return Detection("vinyl", (
            f"Category '{category}' names vinyl records — filed as Vinyl."
        ), "detected")
    if category and _category_decides_cassette(category):
        return Detection("cassette", (
            f"Category '{category}' names music cassettes — filed as Cassette."
        ), "detected")
    if category and _category_decides_cd(category):
        return Detection("cd", (
            f"Category '{category}' names music CDs — filed as CD."
        ), "detected")

    # Tier 4, first: recognised console hardware. Shelf still has no hardware
    # media type; the signal is what lets the caller decline a false film lookup.
    if title and _is_hardware_title(title):
        return Detection("dvd", (
            "Title names console hardware, not a film or a game — filed as "
            "DVD / Blu-ray. Change it on the item if that's wrong."
        ), "hardware")

    # Tier 4: no signal. A deliberate non-book hint stands. This is especially
    # important for unusual music products where a retail title/category does
    # not expose the physical format at all.
    if hint is not None and hint not in _BOOK_FAMILY_HINTS:
        return Detection(hint, (
            f"Nothing in the barcode or the product record said otherwise — "
            f"kept your '{MEDIA_TYPES[hint]}' choice."
        ), "hinted")

    if barcode_type == "upc":
        return Detection("dvd", (
            "UPC barcode carried no usable title or category signal — filed "
            "as DVD / Blu-ray. Change it on the item if that's wrong."
        ), "none")
    return Detection("dvd", (
        "Couldn't tell from the barcode — filed as DVD / Blu-ray. Change it "
        "on the item if that's wrong."
    ), "none")
