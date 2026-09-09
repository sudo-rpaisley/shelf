from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1))


replace_once(
    "app/services/komga_books.py",
    "from __future__ import annotations\n\nfrom typing import Any\n",
    "from __future__ import annotations\n\nimport re\nfrom typing import Any\n",
)

replace_once(
    "app/services/komga_books.py",
    "PAGE_SIZE = 200\n\n\n",
    '''PAGE_SIZE = 200\n\n# Komga can derive ``seriesTitle`` as ``<Series> (<Volume>)`` from ComicInfo\n# metadata when its append-volume option is enabled. Shelf groups catalogue\n# items by series_name, so retaining that suffix would split one manga/comic\n# run into one group per volume. Four-digit publication-year suffixes are kept\n# because names such as ``Batman (2016)`` commonly identify distinct runs.\n_VOLUME_SERIES_RE = re.compile(r"^(?P<name>.+?)\\s+\\((?P<number>\\d+)\\)$")\n\n\ndef _canonical_series_name(value: Any) -> str | None:\n    """Return a stable Shelf grouping name for a Komga series title."""\n    text = str(value or "").strip()\n    if not text:\n        return None\n    match = _VOLUME_SERIES_RE.fullmatch(text)\n    if not match:\n        return text\n    number = match.group("number")\n    if len(number) == 4 and 1800 <= int(number) <= 2199:\n        return text\n    return match.group("name").strip() or text\n\n\n''',
)

replace_once(
    "app/services/komga_books.py",
    '        "series_name": str(book.get("seriesTitle") or "").strip() or None,\n',
    '        "series_name": _canonical_series_name(book.get("seriesTitle")),\n',
)

p = Path("tests/test_komga_books.py")
s = p.read_text().rstrip()
s += r'''


def test_normalise_book_strips_komga_volume_suffix_but_keeps_year_runs():
    def series_name(value):
        candidate = komga_books.normalise_book(
            {
                "id": f"book-{value}",
                "name": "Fallback",
                "seriesTitle": value,
                "metadata": {"title": "Issue"},
            },
            library_id="library-1",
            kind="manga",
        )
        return candidate["series_name"]

    assert series_name("One Piece (3)") == "One Piece"
    assert series_name("One Piece (108)") == "One Piece"
    assert series_name("  One Piece (21)  ") == "One Piece"
    assert series_name("One Piece (0021)") == "One Piece"
    assert series_name("Batman (2016)") == "Batman (2016)"
    assert series_name("Watchmen") == "Watchmen"
'''
p.write_text(s.rstrip() + "\n")

p = Path("tests/test_komga_records.py")
s = p.read_text()
s = s.replace(
    "from app.services import komga_records\n",
    "from app.services import komga_books, komga_records\n",
    1,
)
s = s.rstrip() + r'''


def test_resync_repairs_old_volume_suffix_series_name(db):
    first = komga_records.persist_candidate(
        db,
        _candidate(
            komga_id="one-piece-21",
            title="One Piece Vol. 21",
            series_name="One Piece (21)",
            series_position=21.0,
        ),
    )

    candidate = komga_books.normalise_book(
        {
            "id": "one-piece-21",
            "seriesId": "one-piece",
            "seriesTitle": "One Piece (21)",
            "metadata": {
                "title": "One Piece Vol. 21",
                "isbn": "9781974700523",
                "numberSort": 21,
            },
        },
        library_id="library-1",
        kind="manga",
    )
    second = komga_records.persist_candidate(db, candidate)

    row = db.execute(
        "SELECT series_name FROM items WHERE id = ?", (first["item_id"],)
    ).fetchone()
    assert second["action"] == "updated"
    assert second["item_id"] == first["item_id"]
    assert row["series_name"] == "One Piece"
'''
p.write_text(s.rstrip() + "\n")
