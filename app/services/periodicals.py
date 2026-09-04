"""Helpers for magazine and other periodical barcodes.

ISSN serials commonly use an EAN-13 beginning 977. The seven digits after
that prefix are the first seven digits of the ISSN; the ISSN check character
is reconstructed independently from the EAN check character. The two digits
before the EAN check digit are the serial variant value and must not be treated
as an issue number.

North American consumer magazines also commonly use a normal UPC-A carrier
followed by a 2- or 5-digit EAN/UPC add-on. That carrier does not encode an
ISSN, but the add-on is strong evidence that the barcode represents a concrete
periodical issue. Shelf preserves the add-on verbatim rather than guessing its
publisher-specific issue/date meaning.
"""

from dataclasses import dataclass

from app.services.upc import normalize_barcode


@dataclass(frozen=True, slots=True)
class PeriodicalBarcode:
    """Decoded periodical carrier and optional issue-discriminator add-on."""

    # Canonical EAN-13 storage form. UPC-A carriers are zero-padded.
    ean13: str
    # Present for 977 serial carriers; ordinary UPC-A/EAN periodicals may not
    # expose an ISSN in their retail barcode at all.
    issn: str | None
    # 977 serial variant. It has no universal issue-number meaning.
    variant: str | None
    supplement: str | None = None

    @property
    def full_code(self) -> str:
        return self.ean13 + (self.supplement or "")


def _valid_ean13(code: str) -> bool:
    if len(code) != 13 or not code.isdigit():
        return False
    total = sum(
        int(digit) * (3 if index % 2 else 1)
        for index, digit in enumerate(code[:12])
    )
    check = (10 - (total % 10)) % 10
    return check == int(code[-1])


def issn_from_seven_digits(stem: str) -> str | None:
    """Return the formatted ISSN for a seven-digit ISSN stem."""
    if len(stem) != 7 or not stem.isdigit():
        return None

    weighted = sum(
        int(digit) * weight
        for digit, weight in zip(stem, range(8, 1, -1))
    )
    value = (11 - (weighted % 11)) % 11
    check = "X" if value == 10 else str(value)
    return f"{stem[:4]}-{stem[4:]}{check}"


def parse_barcode(raw: str) -> PeriodicalBarcode | None:
    """Decode a periodical barcode and preserve its 2/5-digit add-on.

    Accepted forms:

    * 13-digit 977 serial carrier, with optional 2/5-digit add-on (15/18)
    * 12-digit UPC-A carrier plus a required 2/5-digit add-on (14/17)
    * non-ISBN 13-digit EAN carrier plus a required 2/5-digit add-on (15/18)

    A plain non-977 retail UPC/EAN is deliberately *not* classified as a
    periodical: without the extension there is no safe barcode-level signal.
    Likewise 978/979 + extension remains book territory rather than being
    reclassified as a magazine.
    """
    code = normalize_barcode(raw)

    carrier: str
    supplement: str | None
    if len(code) in (13, 15, 18):
        carrier = code[:13]
        supplement = code[13:] or None
    elif len(code) in (14, 17):
        # UPC-A (12 digits) + 2/5 digit extension. Canonicalise to EAN-13 so
        # the same issue matches whether a decoder reports UPC-A or EAN-13.
        carrier = "0" + code[:12]
        supplement = code[12:]
    else:
        return None

    if not _valid_ean13(carrier):
        return None
    if supplement is not None and len(supplement) not in (2, 5):
        return None

    if carrier.startswith("977"):
        issn = issn_from_seven_digits(carrier[3:10])
        if not issn:
            return None
        return PeriodicalBarcode(
            ean13=carrier,
            issn=issn,
            variant=carrier[10:12],
            supplement=supplement,
        )

    # A supplement is the evidence that an ordinary retail carrier is being
    # used for a periodical issue. Do not steal ISBN/EAN book extensions.
    if supplement is None or carrier.startswith(("978", "979")):
        return None

    return PeriodicalBarcode(
        ean13=carrier,
        issn=None,
        variant=None,
        supplement=supplement,
    )
