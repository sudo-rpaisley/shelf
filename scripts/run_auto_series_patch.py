"""Temporary wrapper for the automatic-series branch patch.

A few target blocks are semantically identical but have different indentation
from the first draft of the generated patch. Adjust those textual patches
in-memory, then execute the main patch script. This file is deleted with the
other temporary branch tooling once the real code has been committed.
"""

from pathlib import Path

source_path = Path("scripts/patch_auto_series.py")
source = source_path.read_text()

hardcover_needle = '''replace(
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

hardcover_replacement = '''replace(
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

if source.count(hardcover_needle) != 1:
    raise RuntimeError("Could not locate the Hardcover dual-occurrence patch")
source = source.replace(hardcover_needle, hardcover_replacement, 1)

items_common_needle = '''replace(
    "app/routers/items_common.py",
    \'\'\'        series_name=metadata.get("series_name"),
        series_position=metadata.get("series_position"),
        location_id=loc_id,
\'\'\',
    \'\'\'        series_name=metadata.get("series_name"),
        series_position=metadata.get("series_position"),
        series_memberships=metadata.get("series_memberships"),
        location_id=loc_id,
\'\'\',
)
'''

items_common_replacement = '''replace(
    "app/routers/items_common.py",
    \'\'\'            series_name=metadata.get("series_name"),
            series_position=metadata.get("series_position"),
            location_id=loc_id,
\'\'\',
    \'\'\'            series_name=metadata.get("series_name"),
            series_position=metadata.get("series_position"),
            series_memberships=metadata.get("series_memberships"),
            location_id=loc_id,
\'\'\',
)
'''

if source.count(items_common_needle) != 1:
    raise RuntimeError("Could not locate the items_common series-save patch")
source = source.replace(items_common_needle, items_common_replacement, 1)

exec(compile(source, str(source_path), "exec"), {"__name__": "__main__", "__file__": str(source_path)})
