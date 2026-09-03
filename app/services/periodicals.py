"""Helpers for ISSN-based periodical barcodes.

Consumer magazines and other serials commonly use an EAN-13 beginning 977.
The seven digits after that prefix are the first seven digits of the ISSN;
the ISSN check character is reconstructed independently from the EAN check
character.  The two digits before the EAN check digit are the serial variant
value and must not be treated as an issue number.

This module is deliberately pure.  Provider lookup belongs in googlebooks.py;
scan routing belongs in items_common.py.
"""

from dataclasses import dataclass

from app.services.upc import normalize_barcode


@dataclass(frozen=True, slots=True)
class PeriodicalBarcode:
    """Decoded stable information from one 977 EAN-13 barcode."""

    ean13: str
    issn: str
    variant: str


def _valid_ean13(code: str) -> bool:
    if len(code) != 13 or not code.isdigit():
        return False
    total = sum(int(digit) * (3 if index % 2 else 1)
                for index, digit in enumerate(code[:12]))
    check = (10 - (total % 10)) % 10
    return check == int(code[-1])


def issn_from_seven_digits(stem: str) -> str | None:
    """Return the formatted ISSN for a seven-digit ISSN stem."""
    if len(stem) != 7 or not stem.isdigit():
        return None

    weighted = sum(int(digit) * weight
                   for digit, weight in zip(stem, range(8, 1, -1)))
    value = (11 - (weighted % 11)) % 11
    check = "X" if value == 10 else str(value)
    return f"{stem[:4]}-{stem[4:]}{check}"


def parse_barcode(raw: str) -> PeriodicalBarcode | None:
    """Decode a valid 977 EAN-13, otherwise return ``None``.

    Shelf's browser scanner currently supplies the main EAN symbol, not a
    separate 2/5-digit supplemental symbol, so this parser intentionally only
    accepts the 13-digit carrier code.  Supplemental issue handling can be
    added later without changing how the stable publication ISSN is derived.
    """
    code = normalize_barcode(raw)
    if not code.startswith("977") or not _valid_ean13(code):
        return None

    issn = issn_from_seven_digits(code[3:10])
    if not issn:
        return None
    return PeriodicalBarcode(ean13=code, issn=issn, variant=code[10:12])
