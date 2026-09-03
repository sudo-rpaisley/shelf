from pathlib import Path


def replace(path: str, old: str, new: str, expected: int = 1) -> None:
    p = Path(path)
    text = p.read_text()
    actual = text.count(old)
    if actual != expected:
        raise RuntimeError(f"{path}: expected {expected} match(es), found {actual}")
    p.write_text(text.replace(old, new, expected))


# Komga's membership reconciliation must run while its per-item database
# context is still open. The first automatic-series patch left this block one
# indentation level too shallow.
replace(
    "app/services/komga.py",
    '''                series_memberships_svc.add_metadata_memberships(
                    db,
                    item_id,
                    [{"name": series_name, "position": series_position}] if series_name else [],
                )

                # Metadata progress is independent of cover I/O. Queue the cover
''',
    '''                    series_memberships_svc.add_metadata_memberships(
                        db,
                        item_id,
                        [{"name": series_name, "position": series_position}] if series_name else [],
                    )

                # Metadata progress is independent of cover I/O. Queue the cover
''',
)

# Keep items_common below its deliberate 900-line god-object ceiling. The
# automatic-series change added a tiny metadata carry-through, so collapse it
# without changing behaviour rather than raising the architectural guard.
replace(
    "app/routers/items_common.py",
    '''                if hc_data.get("series_memberships"):
                    metadata["series_memberships"] = hc_data["series_memberships"]
''',
    '''                metadata["series_memberships"] = hc_data.get("series_memberships") or metadata.get("series_memberships")
''',
)
replace(
    "app/routers/items_common.py",
    '''    metadata = None
    source = "manual"
    legs: list[provider_result.ProviderResult] = []

    # National-bibliography routing: for registration groups with an
''',
    '''    metadata = None
    source = "manual"
    legs: list[provider_result.ProviderResult] = []
    # National-bibliography routing: for registration groups with an
''',
)
replace(
    "app/routers/items_common.py",
    '''    isbn10 = metadata.get("isbn10") or isbn_svc.isbn13_to_isbn10(isbn13)
    loc_id = location_id if location_id and location_id > 0 else None

    with get_db() as db:
''',
    '''    isbn10 = metadata.get("isbn10") or isbn_svc.isbn13_to_isbn10(isbn13)
    loc_id = location_id if location_id and location_id > 0 else None
    with get_db() as db:
''',
)
