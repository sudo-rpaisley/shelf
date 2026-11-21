"""Author-name matching for metadata lookups.

Title searches return adaptations, study guides and abridgements alongside
the real work, so every lookup path checks the result's author before
trusting it. That check has to tolerate the ways the same person's name is
written across sources:

    Stanislaw Lem        vs  Stanisław Lem            (ASCII-ised diacritic)
    Richard P. Feynman   vs  Richard Phillips Feynman (initial vs full name)
    James Duane          vs  James J. Duane           (dropped middle initial)

Photo intake leans on this hardest, because the vision model transcribes
whatever is printed on the spine — which is where stripped accents and
abbreviated middle names come from in the first place.
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Latin letters written with a stroke or bar rather than a combining accent.
# NFKD decomposes é into e + U+0301, but ł is an indivisible code point, so
# these have to be folded by hand or Polish/Nordic/Croatian names never match.
_STROKED = str.maketrans({
    "ł": "l", "Ł": "L",
    "ø": "o", "Ø": "O",
    "đ": "d", "Đ": "D",
    "ħ": "h", "Ħ": "H",
    "ı": "i", "İ": "I",
    "ß": "ss",
    "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE",
    "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "TH",
})


def normalize(name: str) -> list[str]:
    """Fold a single name to lowercase word tokens, in any script.

    Punctuation is dropped rather than split on, so "Feynman!" and
    "Feynman" agree and "R.P." becomes two initials rather than one blob.
    Splitting is on runs of non-word characters (Unicode-aware), not on an
    `[a-z0-9]` allowlist — the allowlist used to delete every non-Latin
    letter outright, so a CJK, Cyrillic or Arabic name normalized to `[]`
    and could never match or be identified. Latin input tokenizes exactly
    as before this change.
    """
    decomposed = unicodedata.normalize("NFKD", name.translate(_STROKED))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", stripped.casefold()).split()


def join_names(names: Iterable[object]) -> str | None:
    """Build the comma-joined author string stored on an item.

    Accepts an iterable of names. Each element that is a ``str`` is
    stripped; anything else (None, a dict, an int) is dropped rather than
    raising. A bare ``str`` argument itself raises ``TypeError`` — iterating
    a string yields characters, so ``join_names("Frank Herbert")`` would
    store "F, r, a, n, k, …"; callers holding one name pass ``[name]``.
    A bare mapping raises for the same reason: iterating it yields its keys,
    so ``join_names({"name": "Frank Herbert"})`` would store "name".

    Blanks left after stripping are dropped. Exact repeats (post-strip,
    case-sensitive) are dropped, first occurrence wins — deliberately not
    :func:`matches`, which treats "J. Smith" and "John Smith" as one person
    (right for validating a lookup, wrong for deciding a book has one
    contributor or two; see GOTCHAS G22).

    Order is the caller's order; several call sites read position 0
    (``split(",")[0]``) as the primary author. Returns None, never "",
    when nothing survives.
    """
    if isinstance(names, str):
        raise TypeError("join_names expects an iterable of names, not a string")
    if isinstance(names, Mapping):
        raise TypeError("join_names expects an iterable of names, not a mapping")
    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str):
            continue
        stripped = name.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        result.append(stripped)
    return ", ".join(result) or None


def _given_compatible(wanted: str, found: str) -> bool:
    """Whether two given-name tokens can be the same person.

    An initial matches the name it abbreviates; anything else must agree
    outright, so "Richard" never matches "Robert".
    """
    if wanted == found:
        return True
    if len(wanted) == 1:
        return found.startswith(wanted)
    if len(found) == 1:
        return wanted.startswith(found)
    return False


def _one_matches(wanted: list[str], found: list[str]) -> bool:
    """Compare two single normalized names."""
    if not wanted or not found:
        return False
    # The surname has to agree exactly — it is the part sources spell alike,
    # and relaxing it is what would let a study guide's author slip through.
    if wanted[-1] != found[-1]:
        return False
    wanted_given, found_given = wanted[:-1], found[:-1]
    if not wanted_given or not found_given:
        # One side is a bare surname ("Wickman" vs "Gino Wickman").
        return True
    # Middle names and initials are dropped so freely that only the leading
    # given name is worth insisting on.
    return _given_compatible(wanted_given[0], found_given[0])


def matches(wanted: str | None, found: str | None) -> bool:
    """Whether the wanted item's first author appears among the found authors.

    `found` is a comma-joined author list as :func:`join_names` builds it;
    matching any one of its entries is enough.
    """
    if not wanted:
        return True  # nothing to check against
    if not found:
        return False
    first = normalize(wanted.split(",")[0])
    if not first:
        return False
    return any(_one_matches(first, normalize(part)) for part in found.split(","))


# Bump this when `parse`'s rule changes; a boot step re-derives the
# `item_authors` index for every DB whose recorded version is older.
PARSER_VERSION = 2

_GENERATIONAL_SUFFIXES = {"jr.", "jr", "sr.", "sr", "ii", "iii", "iv"}
# A segment ending " - <one word>" is a role, e.g. "Ken Liu - translator".
# `\S+` cannot itself span whitespace, so this can never capture more than
# one word — "Humble Book Bundle - A.I. by Packt" has no hyphen directly
# before a single trailing token, so it does not match.
_ROLE_RE = re.compile(r"^(.*\S)\s-\s*(\S+)$")
# ...and only when that word is a credit role. Version 1 took any word, so
# "Humble Tech Book Bundle - LLM, …" (11 prod rows) showed "· llm" as a role.
# A word not listed here stays part of the name, as written.
_ROLE_WORDS = frozenset({
    "adaptation", "adapter", "adaptor", "afterword", "artist", "colorist",
    "commentary", "compiler", "composer", "conductor", "contributor",
    "cover", "editor", "editors", "foreword", "illustrations", "illustrator",
    "inker", "introduction", "introductions", "letterer", "narrator",
    "penciller", "performer", "photographer", "photographs", "preface",
    "producer", "reader", "translation", "translator",
})
_LEADING_AND_RE = re.compile(r"^and\s+", re.IGNORECASE)


def name_key(name: str) -> str:
    """The identity key for a single name — never :func:`matches`.

    Per G22, `matches()` treats "J. Smith" and "John Smith" as one person,
    which is right for validating a metadata lookup and wrong for deciding
    whether two stored authors are the same entity. This is the only key
    :func:`parse` uses for identity.
    """
    return " ".join(normalize(name))


@dataclass(frozen=True, slots=True)
class ParsedAuthor:
    """One entry recovered from a stored `authors` string by :func:`parse`."""

    name: str
    role: str | None
    name_key: str
    position: int


def _collapse_ws(segment: str) -> str:
    return re.sub(r"\s+", " ", segment.strip())


def parse(authors: str | None) -> list[ParsedAuthor]:
    """Split a stored `authors` string into ordered, deduplicated entries.

    The inverse of :func:`join_names`, following the measured table in
    issue #117's design and nothing more:

    - `None` or a blank string parses to `[]`.
    - Segments are split on `,`, each stripped with inner whitespace runs
      collapsed to one space.
    - A segment that is only a generational suffix (`Jr.`, `Jr`, `Sr.`,
      `Sr`, `II`, `III`, `IV`) rejoins the previous segment as
      "<name>, <suffix>", exactly as written.
    - A segment ending " - <one word>" splits into that name and a
      lowercased `role` when the word is in `_ROLE_WORDS`; an unlisted
      word, a trailing " -" with nothing after it, or more than one
      trailing word, is not a role and stays part of the name.
    - A leading "and " (case-insensitive) is dropped from a segment; `&`
      and an inner " and " never split a segment, since there is no comma
      to split on.
    - "Last, First" is deliberately NOT inverted — the string alone cannot
      distinguish it from two co-authors ("Williams, Robin" parses to two
      entries; this is a documented limit, not a bug — fix it by editing
      the stored string to "Robin Williams").
    - Blank segments, and segments whose `name_key` is empty, are dropped.
    - A segment whose `name_key` repeats an earlier one in the same string
      is dropped; the earlier occurrence (and its role) wins.
    - `position` is 0-based over the surviving entries, in source order.
    """
    if not authors:
        return []

    raw_segments = [_collapse_ws(part) for part in authors.split(",")]

    # A trailing generational suffix rejoins the name before it.
    merged: list[str] = []
    for segment in raw_segments:
        if segment.lower() in _GENERATIONAL_SUFFIXES and merged:
            merged[-1] = f"{merged[-1]}, {segment}"
        else:
            merged.append(segment)

    entries: list[ParsedAuthor] = []
    seen_keys: set[str] = set()
    for segment in merged:
        segment = _LEADING_AND_RE.sub("", segment)
        role_match = _ROLE_RE.match(segment)
        if role_match and role_match.group(2).lower() in _ROLE_WORDS:
            name, role = role_match.group(1), role_match.group(2).lower()
        else:
            name, role = segment, None
        key = name_key(name)
        if not name or not key or key in seen_keys:
            continue
        seen_keys.add(key)
        entries.append(ParsedAuthor(name=name, role=role, name_key=key, position=len(entries)))
    return entries
