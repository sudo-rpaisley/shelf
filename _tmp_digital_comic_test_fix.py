from pathlib import Path

p = Path("tests/test_detect.py")
text = p.read_text()
old = '_BOOK_FAMILY_HINTS_FOR_TEST = ["book", "kids_book", "audiobook", "ebook", "comic"]'
new = '_BOOK_FAMILY_HINTS_FOR_TEST = ["book", "kids_book", "audiobook", "ebook", "comic", "digital_comic"]'
if old not in text:
    raise SystemExit("test_detect book-family anchor not found")
p.write_text(text.replace(old, new, 1))
