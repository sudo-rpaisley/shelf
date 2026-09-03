"""Temporary wrapper for the automatic-series branch patch.

The two Hardcover series extraction blocks are semantically identical but have
different indentation because one lives inside a loop.  The generated patch
script originally treated them as two identical textual occurrences.  Adjust
that one patch in-memory, add the loop-indented variant, then execute the rest
unchanged.  This file is deleted with the other temporary branch tooling once
the real code has been committed.
"""

from pathlib import Path

source_path = Path("scripts/patch_auto_series.py")
source = source_path.read_text()

needle = '''replace(
    "app/services/hardcover.py",
    \'\'\'        # Extract series
        series_name = None
        series_position = None
        book_series = book.get("book_series", [])
        if book_series:
            s = book_series[0]
            series_name = s.get("series", {}).get("name")
            series_position = s.get("position")
\'\'\',
    \'\'\'        # Extract every explicit series; the first remains the legacy primary.
        series_memberships = _book_series_memberships(book)
        series_name = series_memberships[0]["name"] if series_memberships else None
        series_position = series_memberships[0]["position"] if series_memberships else None
\'\'\',
    expected=2,
)
'''

replacement = '''replace(
    "app/services/hardcover.py",
    \'\'\'        # Extract series
        series_name = None
        series_position = None
        book_series = book.get("book_series", [])
        if book_series:
            s = book_series[0]
            series_name = s.get("series", {}).get("name")
            series_position = s.get("position")
\'\'\',
    \'\'\'        # Extract every explicit series; the first remains the legacy primary.
        series_memberships = _book_series_memberships(book)
        series_name = series_memberships[0]["name"] if series_memberships else None
        series_position = series_memberships[0]["position"] if series_memberships else None
\'\'\',
)
replace(
    "app/services/hardcover.py",
    \'\'\'    # Extract series
    series_name = None
    series_position = None
    book_series = book.get("book_series", [])
    if book_series:
        s = book_series[0]
        series_name = s.get("series", {}).get("name")
        series_position = s.get("position")
\'\'\',
    \'\'\'    # Extract every explicit series; the first remains the legacy primary.
    series_memberships = _book_series_memberships(book)
    series_name = series_memberships[0]["name"] if series_memberships else None
    series_position = series_memberships[0]["position"] if series_memberships else None
\'\'\',
)
'''

if source.count(needle) != 1:
    raise RuntimeError("Could not locate the Hardcover dual-occurrence patch")
source = source.replace(needle, replacement, 1)

exec(compile(source, str(source_path), "exec"), {"__name__": "__main__", "__file__": str(source_path)})
