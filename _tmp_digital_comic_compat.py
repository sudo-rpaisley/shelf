from pathlib import Path


def patch(path, old, new):
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old!r}")
    p.write_text(text.replace(old, new, 1))


patch(
    "app/routers/series.py",
    'UNASSIGNED_MEDIA_TYPES = ("book", "kids_book", "audiobook", "ebook", "comic")',
    'UNASSIGNED_MEDIA_TYPES = ("book", "kids_book", "audiobook", "ebook", "comic", "digital_comic")',
)

patch(
    "app/routers/items_catalog.py",
    'BOOK_MEDIA_TYPES = {"book", "kids_book", "audiobook", "ebook", "comic"}',
    'BOOK_MEDIA_TYPES = {"book", "kids_book", "audiobook", "ebook", "comic", "digital_comic"}',
)

patch(
    "app/services/detect.py",
    '_BOOK_FAMILY_HINTS = frozenset({"book", "kids_book", "audiobook", "ebook", "comic"})',
    '_BOOK_FAMILY_HINTS = frozenset({"book", "kids_book", "audiobook", "ebook", "comic", "digital_comic"})',
)
