"""Pure local caption links using the existing creator index identity rules.

No DB reads, writes, feature decisions, remote IDs, matching search or supplied
URLs. Unmatched punctuation/roles/spacing remain raw display text, not HTML.
"""

from dataclasses import dataclass
import re
from urllib.parse import urlencode

from app.services import authors


MAX_CREDIT_TEXT = 5000
MAX_CREDIT_LINKS = 64
MAX_CREATOR_KEY = 1000
MAX_SERIES_NAME = 1000


@dataclass(frozen=True, slots=True)
class CreditPart:
    text: str
    href: str | None = None


@dataclass(frozen=True, slots=True)
class CreditPreview:
    parts: tuple[CreditPart, ...]
    truncated: bool = False


def creator_preview(parts: tuple[CreditPart, ...]) -> CreditPreview:
    """Keep one native creator link; further credits belong on the item page.

    Do not leave clipped, focusable links behind an ellipsis. Single credits,
    including their raw role/punctuation, and unparsed plain text stay intact.
    The complete ``creator_parts``/``creator_text`` remain separate inputs.
    """
    linked = [index for index, part in enumerate(parts) if part.href]
    if len(linked) < 2:
        return CreditPreview(parts)
    return CreditPreview(parts[:linked[0] + 1], truncated=True)


def creator_parts(text: str) -> tuple[CreditPart, ...]:
    """Link parsed names without rewriting even one display character.

Only whitespace/comma spacing folded by the existing parser is matched back to
the raw credit. Unmatched/deduplicated names remain plain, never fuzzy guesses.
"""
    if not isinstance(text, str):
        raise ValueError("creator text must be text")
    text = str(text)  # Drop any str-subclass HTML trust before making spans.
    if not text:
        return ()
    if len(text) > MAX_CREDIT_TEXT:
        return (CreditPart(text),)
    parts, cursor = [], 0
    for credit in authors.parse(text)[:MAX_CREDIT_LINKS]:
        if not credit.name_key or len(credit.name_key) > MAX_CREATOR_KEY:
            continue
        pattern = re.escape(credit.name).replace(r"\ ", r"\s+").replace(r",\s+", r",\s*")
        # A short name must not match the middle of another name or a leading
        # 'and'. The parser's identity is unchanged; this only locates display.
        match = re.search(r"(?<!\w)" + pattern + r"(?!\w)", text[cursor:])
        if match is None:
            continue
        start, end = cursor + match.start(), cursor + match.end()
        if start > cursor:
            parts.append(CreditPart(text[cursor:start]))
        href = "/browse/creator?" + urlencode({"key": credit.name_key})
        parts.append(CreditPart(text[start:end], href))
        cursor = end
    if cursor < len(text):
        parts.append(CreditPart(text[cursor:]))
    return tuple(parts)


def series_href(text: str) -> str | None:
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_SERIES_NAME
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in text)):
        return None
    return "/series?" + urlencode({"name": text})
