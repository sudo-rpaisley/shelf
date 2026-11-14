"""Pure Library text/identifier foundations, not an index or a query planner.

Only ``canonical`` and ``snapshot_identifiers`` promise existing catalogue
semantics. The other helpers are deliberately unwired foundations: no search
mode, identity boost, storage, readiness or performance claim is enabled here.
Inputs to preparation/validation are bounded built-in plain data, not database
rows, provider objects, JSON snapshots or arbitrary iterators. Limits refuse
with fixed ValueError codes; they never silently truncate a document.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass

from app.services import isbn, upc


# Deliberately mirror the current catalogue, without importing its runtime graph.
SEARCH_FIELDS = (
    "title", "subtitle", "authors", "isbn", "isbn10", "upc", "asin",
    "narrator", "series_name", "publisher", "abs_id", "hardcover_book_id",
    "hardcover_edition_id", "hardcover_user_book_id",
)
_IDENTIFIER_KEYS = frozenset({
    "isbn", "isbn10", "isbn13", "upc", "ean", "asin", "imdb", "tmdb", "tvdb",
    "igdb", "discogs", "musicbrainz", "musicbrainzalbum", "musicbrainzreleasegroup",
    "musicbrainzrelease", "openlibrary", "googlebooks", "hardcover", "goodreads",
})
_ISBN_KINDS = frozenset({"isbn", "isbn10", "isbn13"})
_RETAIL_KINDS = frozenset({"upc", "ean"})
_OPAQUE_KINDS = (_IDENTIFIER_KEYS - _ISBN_KINDS - _RETAIL_KINDS - {"asin"}) | {
    "abs_id", "hardcover_book_id", "hardcover_edition_id", "hardcover_user_book_id",
}
IDENTIFIER_KINDS = _ISBN_KINDS | _RETAIL_KINDS | {"asin"} | _OPAQUE_KINDS
MAX_DOCUMENT_CHARACTERS = 64_000
MAX_TERMS = 256
MAX_TERM_CHARACTERS = 512
MAX_IDENTIFIER_INPUTS = 256
MAX_IDENTIFIER_CHARACTERS = 512


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    """Canonical literal body and existing, distinct ordering/series keys."""

    body: str
    title_key: str
    creator_key: str
    series_key: str


@dataclass(frozen=True, slots=True)
class IdentifierKeys:
    """Raw ordered (kind, text) pairs and sorted distinct validated identities.

    ``exact`` retains spelling, whitespace, invalid content and duplicates.
    ``keys`` uses ("isbn", ISBN13), ("ean", EAN13), ("asin", cleaned ASIN),
    or (original opaque kind, stripped NFC/casefold value). No title matching,
    provider-kind aliases or cross-kind opaque equivalences are inferred.
    """

    exact: tuple[tuple[str, str], ...]
    keys: tuple[tuple[str, str], ...]


def canonical(value: object) -> str:
    """Exactly the catalogue's NFC-then-casefold, including falsey scalars.

    No trimming, accent/compatibility folding or normalization *after* casefold.
    As with the original, non-None objects use their own ``str`` implementation.
    """
    return unicodedata.normalize("NFC", str(value) if value is not None else "").casefold()


def forgiving_fold(value: object) -> str:
    """Opt-in NFKD/mark removal for future descriptive-text search, NOT identity.

    Compatibility forms (e.g. fullwidth text) collapse, and all Unicode mark
    categories are removed. This is not transliteration (e.g. ø/ł stay ø/ł),
    does not strip whitespace, and must not replace canonical or series/ID keys.
    """
    return "".join(char for char in unicodedata.normalize("NFKD", canonical(value)).casefold()
                   if not unicodedata.category(char).startswith("M"))


def snapshot_identifiers(payload: object) -> tuple[str, ...]:
    """Extract exactly today's bounded named snapshot fields, in original order.

    The 64000 limit counts characters, not UTF-8 bytes. Values are only exact
    built-in str/int (not bool), at most 512 characters BEFORE stripping, with
    Cc rejected BEFORE stripping. Root, provider_ids, identifiers, external_ids
    dictionaries are visited in that order; duplicates survive. No prose or
    arbitrary nested JSON is traversed. Key normalization is not an alias map.
    """
    if not isinstance(payload, str) or len(payload) > 64_000:
        return ()
    try:
        metadata = json.loads(payload)
    except (TypeError, ValueError, RecursionError):
        return ()
    if not isinstance(metadata, dict):
        return ()
    result = []
    dictionaries = [metadata]
    for field in ("provider_ids", "identifiers", "external_ids"):
        if isinstance(metadata.get(field), dict):
            dictionaries.append(metadata[field])
    for values in dictionaries:
        for key, value in values.items():
            if not isinstance(key, str):
                continue
            key = key.strip().casefold().replace("_", "").replace("-", "")
            if key in _IDENTIFIER_KEYS and type(value) in (str, int):
                value = str(value)
                if len(value) > 512 or any(unicodedata.category(c) == "Cc" for c in value):
                    continue
                value = value.strip()
                if value:
                    result.extend((value, f"{key}:{value}"))
    return tuple(result)


def _document_scalar(value: object) -> str:
    if value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("invalid_document")
    if type(value) is float and not math.isfinite(value):
        raise ValueError("invalid_document")
    try:
        text = str(value) if value is not None else ""
    except ValueError:
        raise ValueError("invalid_document") from None
    if len(text) > MAX_DOCUMENT_CHARACTERS:
        raise ValueError("invalid_document")
    return text


def _document_terms(values: object) -> tuple[str, ...]:
    if type(values) not in (list, tuple) or len(values) > MAX_TERMS:
        raise ValueError("invalid_document")
    if any(type(value) is not str or len(value) > MAX_TERM_CHARACTERS for value in values):
        raise ValueError("invalid_document")
    return tuple(values)


def prepare_document(core_fields: dict, tags: list | tuple,
                     external_ids: list | tuple) -> PreparedDocument:
    """Prepare only SEARCH_FIELDS plus explicit tags/external ID *strings*.

    core_fields must be a built-in dict with a subset of the exact field names;
    missing fields are None. Values are built-in str/int/bool, finite float or
    None, each <=64000 characters before folding; series_name must be str/None,
    matching the existing series contract.
    Each tag/ID collection is a built-in list/tuple of <=256 strings, each <=512
    characters. The complete canonical body, INCLUDING separators and casefold
    expansion, must be <=64000 characters. Controls/invalid legacy identifiers
    remain literal body text, just as in the catalogue; no identities are guessed.

    Title/creator use canonical; series intentionally uses strip().casefold()
    WITHOUT NFC, preserving the current series identity (including NFD spelling).
    """
    if type(core_fields) is not dict or any(
        type(key) is not str or key not in SEARCH_FIELDS for key in core_fields
    ):
        raise ValueError("invalid_document")
    series = core_fields.get("series_name")
    if series is not None and type(series) is not str:
        raise ValueError("invalid_document")
    fields = tuple(canonical(_document_scalar(core_fields.get(key))) for key in SEARCH_FIELDS)
    extra = _document_terms(tags) + _document_terms(external_ids)
    body = "\n".join(fields) + "\n" + "\n".join(canonical(value) for value in extra)
    if len(body) > MAX_DOCUMENT_CHARACTERS:
        raise ValueError("invalid_document")
    return PreparedDocument(body, fields[0], fields[2], (series or "").strip().casefold())


def document_grams(document: PreparedDocument) -> tuple[str, ...]:
    """Distinct literal 1/2/3-codepoint substrings, sorted by length then value.

    Includes whitespace, separators, combining marks, controls and wildcards;
    no tokenization, normalization or forgiving folding is applied here.
    """
    if (type(document) is not PreparedDocument or type(document.body) is not str
            or len(document.body) > MAX_DOCUMENT_CHARACTERS):
        raise ValueError("invalid_document")
    return tuple(sorted({document.body[start:start + width]
                         for width in (1, 2, 3)
                         for start in range(len(document.body) - width + 1)},
                        key=lambda gram: (len(gram), gram)))


def _identity(kind: str, value: str | int) -> tuple[str, str] | None:
    if kind in _OPAQUE_KINDS:
        text = str(value)
        if any(unicodedata.category(char) == "Cc" for char in text):
            return None
        key = canonical(text.strip())
        return (kind, key) if key else None
    if type(value) is not str:
        return None
    if kind in _ISBN_KINDS:
        # The historical ISBN normalizer discards arbitrary letters. Do not let
        # a URL, provider ID or prose containing valid digits acquire identity.
        if not re.fullmatch(r"[0-9Xx -]+", value):
            return None
        code = isbn.normalize_isbn(value)
        if (kind == "isbn10" and len(code) != 10) or (kind == "isbn13" and len(code) != 13):
            return None
        pair = isbn.canonical_isbn_pair(code)
        return ("isbn", pair[0]) if pair else None
    if kind in _RETAIL_KINDS:
        if not re.fullmatch(r"[0-9 -]+", value):
            return None
        code = upc.normalize_barcode(value)
        if kind == "ean" and len(code) != 13:
            return None
        valid, ean13 = upc.canonical_retail_barcode(code)
        # Only the validated UPC-A <-> zero-prefixed EAN13 pair is unified.
        # The remaining leading zeros are significant; Bookland stays ISBN-only.
        return ("ean", ean13) if valid and ean13 else None
    cleaned = isbn.clean_asin(value)
    return ("asin", cleaned) if cleaned else None


def validated_identifiers(inputs: list | tuple) -> IdentifierKeys:
    """Validate <=256 explicit (kind, value) built-in list/tuple pairs.

    Kinds are exact, case-sensitive IDENTIFIER_KINDS; no typename/key aliases.
    Values are built-in str/int, <=512 characters before any cleaning; bool,
    float, None and nested objects are refused with invalid_identifiers, as are
    unknown kinds/oversize collections. Integers can identify opaque providers
    but are NOT coerced into ISBN/UPC/ASIN identities. Invalid *content* remains
    in exact with no identity key (including controls and checksum failures).
    ISBN accepts ASCII digits/X, spaces/hyphens; isbn10/isbn13 enforce length.
    UPC accepts 12/13 digits, EAN only 13, with ASCII spaces/hyphens; Bookland
    is excluded. ASIN uses existing clean_asin, including its spaces/tabs rule.
    Opaque values reject Cc before trimming and retain accents and punctuation.
    Source-instance-scoped IDs are intentionally not given a global kind here.
    """
    if type(inputs) not in (list, tuple) or len(inputs) > MAX_IDENTIFIER_INPUTS:
        raise ValueError("invalid_identifiers")
    exact, keys = [], set()
    for entry in inputs:
        if type(entry) not in (list, tuple) or len(entry) != 2:
            raise ValueError("invalid_identifiers")
        kind, value = entry
        if type(kind) is not str or kind not in IDENTIFIER_KINDS or type(value) not in (str, int):
            raise ValueError("invalid_identifiers")
        try:
            text = str(value)
        except ValueError:
            raise ValueError("invalid_identifiers") from None
        if len(text) > MAX_IDENTIFIER_CHARACTERS:
            raise ValueError("invalid_identifiers")
        exact.append((kind, text))
        identity = _identity(kind, value)
        if identity is not None:
            keys.add(identity)
    return IdentifierKeys(tuple(exact), tuple(sorted(keys)))
