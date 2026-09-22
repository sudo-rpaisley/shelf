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
    """Fold a single name to lowercase ASCII-ish word tokens.

    Punctuation is dropped rather than split on, so "Feynman!" and
    "Feynman" agree and "R.P." becomes two initials rather than one blob.
    """
    decomposed = unicodedata.normalize("NFKD", name.translate(_STROKED))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", stripped.casefold()).split()


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
