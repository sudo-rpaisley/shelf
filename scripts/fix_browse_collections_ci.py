from pathlib import Path


def replace(path: str, old: str, new: str, expected: int = 1) -> None:
    p = Path(path)
    text = p.read_text()
    actual = text.count(old)
    if actual != expected:
        raise RuntimeError(f"{path}: expected {expected} match(es), found {actual}: {old[:100]!r}")
    p.write_text(text.replace(old, new, expected))


# Carry the automatic-series parent fixes into this stacked branch so its tree
# stays identical to #75 for those files once the base advances.
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

# The user-facing destination is Browse now, so the established back-link
# contract should test the new label rather than preserve old copy.
path = Path("tests/test_back_nav.py")
text = path.read_text()
if "Back to collection" not in text:
    raise RuntimeError("tests/test_back_nav.py: old Browse label not found")
path.write_text(text.replace("Back to collection", "Back to browse"))

# Home no longer owns a second search form; the persistent navigation does.
replace(
    "tests/test_home.py",
    '''    assert 'action="/browse"' in response.text
    assert '<a href="/" class="text-lg font-bold text-shelf-accent2 tracking-tight mr-6">Shelf</a>' in response.text
''',
    '''    assert 'data-testid="nav-search-form"' in response.text
    assert 'action="/search"' in response.text
    assert '<a href="/" class="text-lg font-bold text-shelf-accent2 tracking-tight mr-6">Shelf</a>' in response.text
''',
)

# Keep the full search field in a second nav row at laptop widths, where the
# expanded desktop tabs plus account menu would otherwise squeeze it. At XL
# it moves into the main row.
replace(
    "app/templates/base.html",
    'class="hidden lg:block flex-1 max-w-xs mx-4" data-testid="nav-search-form"',
    'class="hidden xl:block flex-1 max-w-xs mx-4" data-testid="nav-search-form"',
)
replace(
    "app/templates/base.html",
    'class="lg:hidden pb-2" data-testid="nav-search-form-mobile"',
    'class="xl:hidden pb-2" data-testid="nav-search-form-mobile"',
)
