"""Small bundled catalogue for legacy periodicals that public APIs miss.

This is intentionally conservative. Entries are only added when Shelf has a
verified publication identity but the normal provider ladder cannot reliably
return it (for example because an historical ISSN is absent from catalogues or
an upstream linked-data endpoint blocks anonymous requests).

The catalogue is publication-level only. It must never infer issue numbers,
dates or barcode add-on semantics.
"""

import re


_PUBLICATIONS = {
    # Physical 977 carrier observed in Shelf testing: 9770953616115.
    # The carrier derives ISSN 0953-6167 and the publication is VW motoring.
    "0953-6167": {
        "title": "VW motoring",
        "publisher": None,
        "description": None,
        "language": "en",
        "issn": "0953-6167",
        "series_name": "VW motoring",
    },
}


def _canonical_issn(value: str | None) -> str | None:
    normalised = re.sub(r"[^0-9X]", "", (value or "").upper())
    if len(normalised) != 8 or not normalised[:7].isdigit():
        return None
    if not (normalised[-1].isdigit() or normalised[-1] == "X"):
        return None
    return f"{normalised[:4]}-{normalised[4:]}"


def lookup(issn: str | None) -> dict | None:
    """Return trusted bundled publication metadata for an exact ISSN."""
    canonical = _canonical_issn(issn)
    if canonical is None:
        return None
    metadata = _PUBLICATIONS.get(canonical)
    return dict(metadata) if metadata else None
