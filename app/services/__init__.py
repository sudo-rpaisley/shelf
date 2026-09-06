"""Service package compatibility wiring for the integrated fork."""

# Upstream 0.34 centralised the ISBN/book-family declaration in config, while
# the fork's media expansion left detect.py with one extra digital-only hint.
# Keep detection tied to the central declaration so a scanned ISBN can never
# be treated as a digital-comic barcode hint. This can disappear once the
# literal in detect.py is replaced by BOOK_MEDIA_TYPES directly.
from app.config import BOOK_MEDIA_TYPES
from app.services import detect as detect

detect._BOOK_FAMILY_HINTS = frozenset(BOOK_MEDIA_TYPES)
