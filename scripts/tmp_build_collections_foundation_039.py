from pathlib import Path
import subprocess

COLLECTIONS_SHA = "5b5a3535b0b2d16650d4957ccefd366e74964dcd"
EXPECTED_CONFLICTS = {
    "README.md",
    "app/database.py",
    "app/main.py",
    "app/nav.py",
    "static/css/app.css",
    "static/sw.js",
    "tests/test_nav.py",
    "tests/test_navigation_polish_037.py",
}


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, check=check)


result = run("git", "cherry-pick", COLLECTIONS_SHA, check=False)
if result.returncode:
    conflicts = set(
        subprocess.check_output(
            ["git", "diff", "--name-only", "--diff-filter=U"], text=True
        ).splitlines()
    )
    if conflicts != EXPECTED_CONFLICTS:
        raise SystemExit(
            f"unexpected Collections conflict set: {sorted(conflicts)}"
        )

    keep_upstream = [
        "README.md",
        "app/database.py",
        "app/main.py",
        "app/nav.py",
        "static/css/app.css",
        "static/sw.js",
        "tests/test_nav.py",
    ]
    run("git", "checkout", "--ours", "--", *keep_upstream)
    run("git", "rm", "tests/test_navigation_polish_037.py")

    # Current upstream uses migrations 1-32 and the prerequisite library PR
    # uses 33-40, so Collections starts at 41.
    p = Path("app/database.py")
    s = p.read_text()
    marker = '''    (40, "Index catalogue items by library",
     "CREATE INDEX IF NOT EXISTS idx_library_items_library ON library_items(library_id)"),'''
    if marker not in s:
        raise SystemExit("migration 40 marker not found")
    migrations = marker + '''
    (41, "Add library-scoped collections",
     """CREATE TABLE IF NOT EXISTS collections (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            library_id  INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            name        TEXT NOT NULL COLLATE NOCASE,
            description TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(library_id, name)
        )"""),
    (42, "Add collection item memberships",
     """CREATE TABLE IF NOT EXISTS collection_items (
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (collection_id, item_id)
        )"""),
    (43, "Index collection memberships by item",
     "CREATE INDEX IF NOT EXISTS idx_collection_items_item ON collection_items(item_id)"),
    (44, "Index collections by library",
     "CREATE INDEX IF NOT EXISTS idx_collections_library ON collections(library_id, name COLLATE NOCASE)"),'''
    s = s.replace(marker, migrations, 1)

    fresh_anchor = "CREATE TABLE IF NOT EXISTS game_platforms ("
    if fresh_anchor not in s:
        raise SystemExit("fresh schema anchor not found")
    fresh = '''CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id  INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    name        TEXT NOT NULL COLLATE NOCASE,
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(library_id, name)
);

CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (collection_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_collection_items_item
    ON collection_items(item_id);
CREATE INDEX IF NOT EXISTS idx_collections_library
    ON collections(library_id, name COLLATE NOCASE);

'''
    s = s.replace(fresh_anchor, fresh + fresh_anchor, 1)
    p.write_text(s)

    p = Path("app/main.py")
    s = p.read_text()
    router_import = (
        "from app.routers import pages, items, items_covers, cover_review, "
        "cover_review_actions, items_csv, items_catalog, locations, location_order, "
        "platforms, settings, sync, checkouts, valuation, hardcover, store, series, "
        "share, tags, intake, archive, shelf_fill, romm, komga, periodicals, music"
    )
    if router_import not in s:
        raise SystemExit("router import anchor not found")
    s = s.replace(router_import, router_import + ", collections", 1)
    include = "app.include_router(series.router)\n"
    if include not in s:
        raise SystemExit("series router anchor not found")
    s = s.replace(include, include + "app.include_router(collections.router)\n", 1)
    p.write_text(s)

    p = Path("app/nav.py")
    s = p.read_text()
    browse = '    {"key": "browse", "label": "Browse", "path": "/browse"},\n'
    collection = (
        '    {"key": "collections", "label": "Collections", '
        '"path": "/collections", "roles": ("admin", "editor", "viewer")},\n'
    )
    if browse not in s:
        raise SystemExit("Browse nav anchor not found")
    p.write_text(s.replace(browse, browse + collection, 1))

    p = Path("tests/test_nav.py")
    s = p.read_text()
    s = s.replace(
        "def test_registry_covers_the_twelve_tabs():",
        "def test_registry_covers_the_registered_tabs():",
        1,
    )
    old = '        "browse", "scan", "intake", "shelf-fill", "store", "series",\n'
    new = (
        '        "browse", "collections", "scan", "intake", "shelf-fill", '
        '"store", "series",\n'
    )
    if old not in s:
        raise SystemExit("nav registry expectation anchor not found")
    p.write_text(s.replace(old, new, 1))

    run("git", "mv", "tests/test_collections_037.py", "tests/test_collections.py")
    p = Path("tests/test_collections.py")
    p.write_text(
        p.read_text().replace(
            "0.37 library-scoped Collections foundation",
            "Library-scoped Collections foundation",
            1,
        )
    )

    run("git", "add", "-A")
    run("git", "cherry-pick", "--continue")

run("git", "diff", "--check")
