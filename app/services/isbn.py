import re


def normalize_isbn(isbn: str) -> str:
    return re.sub(r"[^0-9X]", "", isbn.upper())


def isbn10_to_isbn13(isbn10: str) -> str | None:
    if len(isbn10) != 10:
        return None
    digits = "978" + isbn10[:9]
    check = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    check = (10 - (check % 10)) % 10
    return digits + str(check)


def validate_isbn10(s: str) -> bool:
    if not isinstance(s, str) or not s:
        return False
    isbn = normalize_isbn(s)
    if len(isbn) != 10:
        return False
    if not isbn[:9].isdigit():
        return False
    if not (isbn[9].isdigit() or isbn[9] == "X"):
        return False
    digits = [10 if c == "X" else int(c) for c in isbn]
    total = sum((10 - i) * d for i, d in enumerate(digits))
    return total % 11 == 0


def validate_isbn13(s: str) -> bool:
    if not isinstance(s, str) or not s:
        return False
    isbn = normalize_isbn(s)
    if len(isbn) != 13 or not isbn.isdigit():
        return False
    if not (isbn.startswith("978") or isbn.startswith("979")):
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(isbn))
    return total % 10 == 0


def to_isbn13(raw: str) -> str | None:
    """Return Shelf's canonical 13-digit barcode/ISBN representation.

    The 12-digit branch intentionally preserves Shelf's historical UPC-A
    compatibility: callers that need to distinguish UPC from ISBN do that
    before calling this helper.  ISBN-shaped inputs are stricter: an ISBN-10
    or 978/979 ISBN-13 must have a valid checksum before it can be returned.
    This prevents a mistyped ISBN from becoming a persistent catalogue key
    while keeping the legacy UPC normalisation behaviour intact.
    """
    isbn = normalize_isbn(raw)
    # UPC-A (12 digits) -> EAN-13 by prepending 0. Keep this legacy behaviour;
    # barcode-type aware callers route UPCs before treating the result as ISBN.
    if len(isbn) == 12 and isbn.isdigit():
        return "0" + isbn
    if len(isbn) == 13 and isbn.isdigit():
        # 978/979 is definitively ISBN-13, so require its checksum. Other
        # EAN-13 values retain the historical pass-through used by legacy
        # compatibility code and are not treated as ISBN by detect_barcode_type.
        if isbn.startswith(("978", "979")):
            return isbn if validate_isbn13(isbn) else None
        return isbn
    if len(isbn) == 10:
        return isbn10_to_isbn13(isbn) if validate_isbn10(isbn) else None
    return None


def isbn13_to_isbn10(isbn13: str) -> str | None:
    if len(isbn13) != 13 or not isbn13.startswith("978"):
        return None
    body = isbn13[3:12]
    total = sum(int(d) * (10 - i) for i, d in enumerate(body))
    check = (11 - (total % 11)) % 11
    check_char = "X" if check == 10 else str(check)
    return body + check_char


def canonical_isbn_pair(raw: str) -> tuple[str, str | None] | None:
    """Validate an ISBN and return its canonical ISBN-13/ISBN-10 pair.

    This is for persistence boundaries where the value is known to be an
    ISBN, not a generic retail barcode.  A 979 ISBN has no ISBN-10 equivalent,
    so the second element is ``None``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    isbn = normalize_isbn(raw)
    if validate_isbn10(isbn):
        isbn13 = isbn10_to_isbn13(isbn)
        return (isbn13, isbn) if isbn13 else None
    if validate_isbn13(isbn):
        return isbn, isbn13_to_isbn10(isbn)
    return None
