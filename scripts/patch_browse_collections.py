from pathlib import Path
import re


def replace(path: str, old: str, new: str, expected: int = 1) -> None:
    p = Path(path)
    text = p.read_text()
    actual = text.count(old)
    if actual != expected:
        raise RuntimeError(
            f"{path}: expected {expected} occurrence(s), found {actual}: {old[:120]!r}"
        )
    p.write_text(text.replace(old, new, expected))


def regex_replace(path: str, pattern: str, replacement: str, expected: int = 1) -> None:
    p = Path(path)
    text = p.read_text()
    updated, count = re.subn(pattern, replacement, text, count=expected, flags=re.DOTALL)
    if count != expected:
        raise RuntimeError(
            f"{path}: expected {expected} regex replacement(s), found {count}: {pattern[:120]!r}"
        )
    p.write_text(updated)


# ---------------------------------------------------------------------------
# Database: shared, user-curated collections and item membership
# ---------------------------------------------------------------------------
replace(
    "app/database.py",
    '''    (42, "Backfill primary item-series memberships",
     """INSERT INTO item_series (item_id, series_name, position, is_primary)
        SELECT id, TRIM(series_name), series_position, 1 FROM items
        WHERE series_name IS NOT NULL AND TRIM(series_name) != ''
        ON CONFLICT(item_id, series_name) DO UPDATE SET
            position = excluded.position, is_primary = 1"""),
)
''',
    '''    (42, "Backfill primary item-series memberships",
     """INSERT INTO item_series (item_id, series_name, position, is_primary)
        SELECT id, TRIM(series_name), series_position, 1 FROM items
        WHERE series_name IS NOT NULL AND TRIM(series_name) != ''
        ON CONFLICT(item_id, series_name) DO UPDATE SET
            position = excluded.position, is_primary = 1"""),
    (43, "Add collections table",
     """CREATE TABLE IF NOT EXISTS collections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        description TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )"""),
    (44, "Add collection item membership table",
     """CREATE TABLE IF NOT EXISTS collection_items (
        collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (collection_id, item_id)
    )"""),
    (45, "Index collection item memberships",
     "CREATE INDEX IF NOT EXISTS idx_collection_items_item ON collection_items(item_id)"),
)
''',
)
replace(
    "app/database.py",
    '''CREATE INDEX IF NOT EXISTS idx_item_series_name ON item_series(series_name COLLATE NOCASE);

-- Music is intentionally relational rather than a wide set of nullable
''',
    '''CREATE INDEX IF NOT EXISTS idx_item_series_name ON item_series(series_name COLLATE NOCASE);

-- Curated collections are intentionally separate from Series and Tags. They
-- are shared library groupings and an item may belong to any number of them.
CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (collection_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_collection_items_item ON collection_items(item_id);

-- Music is intentionally relational rather than a wide set of nullable
''',
)


# ---------------------------------------------------------------------------
# Navigation: Collection -> Browse, add Collections, global search field
# ---------------------------------------------------------------------------
replace(
    "app/nav.py",
    '''    {"key": "home", "label": "Home", "path": "/", "group": "primary"},
    {"key": "browse", "label": "Collection", "path": "/browse", "group": "primary"},
    {"key": "series", "label": "Series", "path": "/series", "group": "primary"},
''',
    '''    {"key": "home", "label": "Home", "path": "/", "group": "primary"},
    {"key": "browse", "label": "Browse", "path": "/browse", "group": "primary"},
    {"key": "collections", "label": "Collections", "path": "/collections", "group": "primary"},
    {"key": "series", "label": "Series", "path": "/series", "group": "primary"},
''',
)
replace(
    "app/nav.py",
    '''# Home, Collection and the page that controls visibility must stay reachable.
ALWAYS_VISIBLE = ("home", "browse", "settings")
''',
    '''# Home, Browse, Collections and the page that controls visibility must stay reachable.
ALWAYS_VISIBLE = ("home", "browse", "collections", "settings")
''',
)
replace(
    "app/nav.py",
    '''# Where the item-detail "back" link goes when arriving from somewhere other
# than Collection. This is a whitelist, not an echo: `back_target` is the only
''',
    '''# Where the item-detail "back" link goes when arriving from somewhere other
# than Browse. This is a whitelist, not an echo: `back_target` is the only
''',
)
replace(
    "app/nav.py",
    'DEFAULT_BACK_TARGET = ("/browse", "Back to collection")\n',
    'DEFAULT_BACK_TARGET = ("/browse", "Back to browse")\n',
)

replace(
    "app/templates/base.html",
    '''                        {% endif %}
                    </div>
                </div>

                {% if user %}
                <!-- Account actions stay separate from collection navigation on every screen size. -->
''',
    '''                        {% endif %}
                    </div>
                </div>

                <form action="/search" method="get" class="hidden lg:block flex-1 max-w-xs mx-4" data-testid="nav-search-form">
                    <label for="nav-search-desktop" class="sr-only">Search Shelf</label>
                    <input id="nav-search-desktop" data-nav-search-input type="search" name="query"
                           placeholder="Search Shelf..." autocomplete="off"
                           class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text focus:ring-2 focus:ring-shelf-accent focus:border-transparent outline-none placeholder-shelf-muted/50">
                </form>

                {% if user %}
                <!-- Account actions stay separate from library navigation on every screen size. -->
''',
)
replace(
    "app/templates/base.html",
    '''                {% endif %}
            </div>
        </div>
    </nav>
''',
    '''                {% endif %}
            </div>
            <form action="/search" method="get" class="lg:hidden pb-2" data-testid="nav-search-form-mobile">
                <label for="nav-search-mobile" class="sr-only">Search Shelf</label>
                <input id="nav-search-mobile" data-nav-search-input type="search" name="query"
                       placeholder="Search Shelf..." autocomplete="off"
                       class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text focus:ring-2 focus:ring-shelf-accent focus:border-transparent outline-none placeholder-shelf-muted/50">
            </form>
        </div>
    </nav>
''',
)
replace(
    "app/templates/base.html",
    '<div class="flex justify-between"><span class="text-shelf-muted">Go to Collection</span><kbd class="bg-shelf-hover px-2 py-0.5 rounded text-shelf-text font-mono">b</kbd></div>',
    '<div class="flex justify-between"><span class="text-shelf-muted">Go to Browse</span><kbd class="bg-shelf-hover px-2 py-0.5 rounded text-shelf-text font-mono">b</kbd></div>',
)
replace(
    "app/templates/base.html",
    '<h4 class="text-shelf-accent2 font-medium mb-2 text-xs uppercase tracking-wider">Collection Page</h4>',
    '<h4 class="text-shelf-accent2 font-medium mb-2 text-xs uppercase tracking-wider">Browse Page</h4>',
)

replace(
    "static/js/app.js",
    '''    if (e.key === '/' ) { e.preventDefault(); var q = document.querySelector('[name="q"]'); if (q) q.focus(); }
''',
    '''    if (e.key === '/' ) {
        e.preventDefault();
        var q = document.querySelector('[name="q"]');
        if (!q) {
            var navSearches = document.querySelectorAll('[data-nav-search-input]');
            for (var i = 0; i < navSearches.length; i++) {
                if (navSearches[i].offsetParent !== null) { q = navSearches[i]; break; }
            }
        }
        if (q) q.focus();
    }
''',
)


# ---------------------------------------------------------------------------
# Register the collections router
# ---------------------------------------------------------------------------
replace(
    "app/main.py",
    'from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, platforms, settings, sync, komga, romm, related, checkouts, valuation, hardcover, store, series, share, tags, intake, archive, music\n',
    'from app.routers import pages, items, items_covers, items_csv, items_catalog, locations, platforms, settings, sync, komga, romm, related, checkouts, valuation, hardcover, store, series, collections, share, tags, intake, archive, music\n',
)
replace(
    "app/main.py",
    '''app.include_router(series.router)
app.include_router(share.router)
''',
    '''app.include_router(series.router)
app.include_router(collections.router)
app.include_router(share.router)
''',
)


# ---------------------------------------------------------------------------
# Browse filtering + global nav search redirect
# ---------------------------------------------------------------------------
replace(
    "app/browse_filters.py",
    '''def _series(value):
    return "i.series_name = ? COLLATE NOCASE", [value]


@dataclass(frozen=True)
''',
    '''def _series(value):
    return "i.series_name = ? COLLATE NOCASE", [value]


def _collection(value):
    return (
        "i.id IN (SELECT ci.item_id FROM collection_items ci "
        "JOIN collections c ON c.id = ci.collection_id "
        "WHERE c.name = ? COLLATE NOCASE)",
        [value],
    )


@dataclass(frozen=True)
''',
)
replace(
    "app/browse_filters.py",
    '''    BrowseFilter("language", prefix="Language", condition=_column("i.language")),
    BrowseFilter("series", prefix="Series", condition=_series, quote_in_qs=True),
    # `view` is the odd one: client-owned state (localStorage) that is sent to
''',
    '''    BrowseFilter("language", prefix="Language", condition=_column("i.language")),
    BrowseFilter("series", prefix="Series", condition=_series, quote_in_qs=True),
    BrowseFilter("collection", prefix="Collection", condition=_collection, quote_in_qs=True),
    # `view` is the odd one: client-owned state (localStorage) that is sent to
''',
)

replace(
    "app/routers/pages.py",
    'from datetime import date, datetime, timedelta\n\n',
    'from datetime import date, datetime, timedelta\nfrom urllib.parse import urlencode\n\n',
)
replace(
    "app/routers/pages.py",
    '''            # Music has a release/pressing-aware catalogue of its own. Other
            # families enter the generic Collection with a family filter set.
''',
    '''            # Music has a release/pressing-aware catalogue of its own. Other
            # families enter Browse with a family filter set.
''',
)
replace(
    "app/routers/pages.py",
    '''

@router.get("/browse")
async def browse(
''',
    '''

@router.get("/search")
async def global_search(
    query: str = Query("", max_length=200),
    _=Depends(require_role("viewer")),
):
    """Small global search endpoint used by the persistent navigation bar."""
    value = query.strip()
    target = "/browse"
    if value:
        target += "?" + urlencode({"q": value})
    return RedirectResponse(url=target, status_code=303)


@router.get("/browse")
async def browse(
''',
)
replace(
    "app/routers/pages.py",
    '''    """The Collection page — first paint of the item grid and its filters.
''',
    '''    """The Browse page — first paint of the item grid and its filters.
''',
)
replace(
    "app/routers/pages.py",
    '''        from app.routers.tags import get_item_tags, get_all_tags
        item_tags = get_item_tags(db, item_id)
        all_tags = get_all_tags(db)

        reading_history = get_reading_history(db, item_id)
''',
    '''        from app.routers.tags import get_item_tags, get_all_tags
        item_tags = get_item_tags(db, item_id)
        all_tags = get_all_tags(db)

        from app.routers.collections import item_collection_context
        collection_context = item_collection_context(db, item_id)

        reading_history = get_reading_history(db, item_id)
''',
)
replace(
    "app/routers/pages.py",
    '''            "item_tags": item_tags,
            "all_tags": all_tags,
            "media_types": MEDIA_TYPES,
''',
    '''            "item_tags": item_tags,
            "all_tags": all_tags,
            "item_collections": collection_context["item_collections"],
            "available_collections": collection_context["available_collections"],
            "media_types": MEDIA_TYPES,
''',
)


# ---------------------------------------------------------------------------
# Browse and Home wording
# ---------------------------------------------------------------------------
replace(
    "app/templates/browse.html",
    '{% block title %}Collection — Shelf{% endblock %}',
    '{% block title %}Browse — Shelf{% endblock %}',
)
replace(
    "app/templates/browse.html",
    '''    <input type="hidden" name="series" value="{{ (initial_filters or {}).get('series', '') }}">
''',
    '''    <input type="hidden" name="series" value="{{ (initial_filters or {}).get('series', '') }}">
    <input type="hidden" name="collection" value="{{ (initial_filters or {}).get('collection', '') }}">
''',
)
replace(
    "app/templates/browse.html",
    '''    <!-- Collection heading -->
''',
    '''    <!-- Browse heading -->
''',
)
replace(
    "app/templates/browse.html",
    '''            <h1 class="text-2xl font-bold whitespace-nowrap">Collection <span id="collection-count" class="text-shelf-muted text-lg font-normal">({{ filtered_total }})</span></h1>
''',
    '''            <h1 class="text-2xl font-bold whitespace-nowrap">Browse <span id="collection-count" class="text-shelf-muted text-lg font-normal">({{ filtered_total }})</span></h1>
''',
)
replace(
    "app/templates/browse.html",
    '<label for="collection-search" class="sr-only">Search collection</label>',
    '<label for="collection-search" class="sr-only">Search library</label>',
)
replace(
    "app/templates/browse.html",
    '<label for="collection-sort" class="sr-only">Sort collection</label>',
    '<label for="collection-sort" class="sr-only">Sort browse results</label>',
)

replace(
    "app/templates/home.html",
    '<section class="flex flex-col lg:flex-row lg:items-center justify-between gap-4">',
    '<section>',
)
regex_replace(
    "app/templates/home.html",
    r'''\n        <form action="/browse" method="get" class="w-full lg:w-64">.*?</form>''',
    '',
)
replace(
    "app/templates/home.html",
    '''            <p class="text-shelf-muted mt-2">{{ total_items }} item{{ '' if total_items == 1 else 's' }} across your collection.</p>
''',
    '''            <p class="text-shelf-muted mt-2">{{ total_items }} item{{ '' if total_items == 1 else 's' }} in your library.</p>
''',
)
replace(
    "app/templates/home.html",
    'Start with what you are looking for, then narrow it down in Collection.',
    'Start with what you are looking for, then narrow it down in Browse.',
)
replace(
    "app/templates/home.html",
    '>Open Collection</a>',
    '>Open Browse</a>',
)

replace(
    "app/templates/item_detail.html",
    '''            {% include "fragments/item_tags.html" %}
            {% include "fragments/related_media.html" %}
''',
    '''            {% include "fragments/item_tags.html" %}
            {% include "fragments/item_collections.html" %}
            {% include "fragments/related_media.html" %}
''',
)


# ---------------------------------------------------------------------------
# Curated Collections router
# ---------------------------------------------------------------------------
Path("app/routers/collections.py").write_text(r'''"""Shared, user-curated collections.

Collections are deliberately different from Series (ordered publication
membership) and Tags (lightweight labels). A collection is an intentional
library grouping such as favourites, a project, a course list, or a theme.
"""

import re
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_role
from app.database import get_db

router = APIRouter()

MAX_COLLECTION_NAME = 100
MAX_COLLECTION_DESCRIPTION = 500


def normalize_collection_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()[:MAX_COLLECTION_NAME]


def normalize_collection_description(description: str | None) -> str | None:
    value = re.sub(r"\s+", " ", description or "").strip()[:MAX_COLLECTION_DESCRIPTION]
    return value or None


def get_all_collections(db) -> list:
    """All collections with membership counts, alphabetically."""
    return db.execute(
        "SELECT c.id, c.name, c.description, c.created_at, c.updated_at, "
        "COUNT(ci.item_id) AS item_count "
        "FROM collections c "
        "LEFT JOIN collection_items ci ON ci.collection_id = c.id "
        "GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
    ).fetchall()


def get_collection_cards(db) -> list[dict]:
    cards = []
    for row in get_all_collections(db):
        card = dict(row)
        card["preview_items"] = db.execute(
            "SELECT i.id, i.title, i.cover_path, i.media_type "
            "FROM collection_items ci JOIN items i ON i.id = ci.item_id "
            "WHERE ci.collection_id = ? "
            "ORDER BY ci.created_at DESC, i.id DESC LIMIT 4",
            (row["id"],),
        ).fetchall()
        cards.append(card)
    return cards


def get_item_collections(db, item_id: int) -> list:
    return db.execute(
        "SELECT c.id, c.name, c.description FROM collection_items ci "
        "JOIN collections c ON c.id = ci.collection_id "
        "WHERE ci.item_id = ? ORDER BY c.name COLLATE NOCASE",
        (item_id,),
    ).fetchall()


def item_collection_context(db, item_id: int) -> dict:
    current = get_item_collections(db, item_id)
    current_ids = {row["id"] for row in current}
    available = [row for row in get_all_collections(db) if row["id"] not in current_ids]
    return {"item_collections": current, "available_collections": available}


def _render_item_collections(request: Request, db, item_id: int):
    context = item_collection_context(db, item_id)
    context["item_id"] = item_id
    return request.app.state.templates.TemplateResponse(
        request, "fragments/item_collections.html", context,
    )


@router.get("/collections")
async def collections_page(request: Request, _=Depends(require_role("viewer"))):
    with get_db() as db:
        cards = get_collection_cards(db)
    return request.app.state.templates.TemplateResponse(
        request, "collections.html", {"collections": cards},
    )


@router.post("/api/collections")
async def create_collection(
    name: str = Form(...),
    description: str = Form(""),
    _=Depends(require_role("editor")),
):
    clean_name = normalize_collection_name(name)
    if not clean_name:
        return HTMLResponse("Collection name required", status_code=400)
    clean_description = normalize_collection_description(description)
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO collections (name, description) VALUES (?, ?)",
                (clean_name, clean_description),
            )
    except sqlite3.IntegrityError:
        return HTMLResponse("A collection with that name already exists", status_code=409)
    return RedirectResponse(url="/collections", status_code=303)


@router.post("/api/collections/{collection_id}")
async def update_collection(
    collection_id: int,
    name: str = Form(...),
    description: str = Form(""),
    _=Depends(require_role("editor")),
):
    clean_name = normalize_collection_name(name)
    if not clean_name:
        return HTMLResponse("Collection name required", status_code=400)
    clean_description = normalize_collection_description(description)
    try:
        with get_db() as db:
            result = db.execute(
                "UPDATE collections SET name = ?, description = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (clean_name, clean_description, collection_id),
            )
            if result.rowcount != 1:
                return HTMLResponse("Collection not found", status_code=404)
    except sqlite3.IntegrityError:
        return HTMLResponse("A collection with that name already exists", status_code=409)
    return RedirectResponse(url="/collections", status_code=303)


@router.delete("/api/collections/{collection_id}")
async def delete_collection(
    collection_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        result = db.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
        if result.rowcount != 1:
            return HTMLResponse("Collection not found", status_code=404)
    return HTMLResponse("")


@router.post("/api/items/{item_id}/collections")
async def add_item_to_collection(
    request: Request,
    item_id: int,
    collection_id: int = Form(...),
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        item = db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
        collection = db.execute(
            "SELECT id FROM collections WHERE id = ?", (collection_id,)
        ).fetchone()
        if not item:
            return HTMLResponse("Item not found", status_code=404)
        if not collection:
            return HTMLResponse("Collection not found", status_code=404)
        db.execute(
            "INSERT OR IGNORE INTO collection_items (collection_id, item_id) VALUES (?, ?)",
            (collection_id, item_id),
        )
        return _render_item_collections(request, db, item_id)


@router.delete("/api/items/{item_id}/collections/{collection_id}")
async def remove_item_from_collection(
    request: Request,
    item_id: int,
    collection_id: int,
    _=Depends(require_role("editor")),
):
    with get_db() as db:
        item = db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            return HTMLResponse("Item not found", status_code=404)
        result = db.execute(
            "DELETE FROM collection_items WHERE collection_id = ? AND item_id = ?",
            (collection_id, item_id),
        )
        if result.rowcount != 1:
            return HTMLResponse("Collection membership not found", status_code=404)
        return _render_item_collections(request, db, item_id)
''')


# ---------------------------------------------------------------------------
# Collections page and item-detail fragment
# ---------------------------------------------------------------------------
Path("app/templates/collections.html").write_text(r'''{% extends "base.html" %}
{% block title %}Collections — Shelf{% endblock %}
{% block content %}
{% set can_edit = user and user.role in ('admin', 'editor') %}
<div class="space-y-6">
    <section class="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-4">
        <div>
            <p class="text-xs uppercase tracking-wider text-shelf-muted mb-1">Library</p>
            <h1 class="text-3xl font-bold">Collections</h1>
            <p class="text-shelf-muted mt-2 max-w-2xl">Curated groups of items for favourites, projects, themes or anything else. Collections are separate from Series and Tags.</p>
        </div>
        {% if can_edit %}
        <details class="bg-shelf-card border border-shelf-border rounded-xl p-3 sm:w-80" {% if not collections %}open{% endif %}>
            <summary class="cursor-pointer text-sm font-medium text-shelf-accent2">New collection</summary>
            <form method="post" action="/api/collections" class="mt-3 space-y-3">
                <div>
                    <label for="new-collection-name" class="block text-xs text-shelf-muted mb-1">Name</label>
                    <input id="new-collection-name" name="name" type="text" maxlength="100" required
                           placeholder="e.g. Favourites"
                           class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">
                </div>
                <div>
                    <label for="new-collection-description" class="block text-xs text-shelf-muted mb-1">Description <span class="text-shelf-muted/70">optional</span></label>
                    <textarea id="new-collection-description" name="description" maxlength="500" rows="2"
                              class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none"></textarea>
                </div>
                <button type="submit" class="px-4 py-2 bg-shelf-accent text-white rounded-lg text-sm font-medium hover:bg-shelf-accent2 transition-colors">Create collection</button>
            </form>
        </details>
        {% endif %}
    </section>

    {% if collections %}
    <section class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4" data-testid="collections-grid">
        {% for collection in collections %}
        <article id="collection-card-{{ collection.id }}" class="bg-shelf-card border border-shelf-border rounded-xl overflow-hidden">
            <a href="/browse?collection={{ collection.name|urlencode }}" class="block p-4 hover:bg-shelf-hover transition-colors">
                {% if collection.preview_items %}
                <div class="grid grid-cols-4 gap-1 h-28 bg-shelf-bg rounded-lg overflow-hidden mb-4">
                    {% for preview in collection.preview_items %}
                    <div class="bg-shelf-hover flex items-center justify-center min-w-0 overflow-hidden">
                        {% if preview.cover_path %}
                        <img src="/{{ preview.cover_path }}" alt="" loading="lazy" class="w-full h-full object-contain">
                        {% else %}
                        <span class="text-xs text-shelf-muted px-1 text-center truncate">{{ preview.title }}</span>
                        {% endif %}
                    </div>
                    {% endfor %}
                    {% for _ in range(4 - collection.preview_items|length) %}
                    <div class="bg-shelf-hover"></div>
                    {% endfor %}
                </div>
                {% else %}
                <div class="h-28 bg-shelf-bg rounded-lg mb-4 flex items-center justify-center text-shelf-muted">
                    <svg class="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M4 7h5l2 2h9v10a2 2 0 01-2 2H6a2 2 0 01-2-2V7z"/></svg>
                </div>
                {% endif %}
                <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0">
                        <h2 class="text-lg font-semibold text-shelf-text truncate">{{ collection.name }}</h2>
                        <p class="text-sm text-shelf-muted mt-1">{{ collection.item_count }} item{{ '' if collection.item_count == 1 else 's' }}</p>
                    </div>
                    <svg class="w-4 h-4 text-shelf-accent2 mt-1 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/></svg>
                </div>
                {% if collection.description %}<p class="text-sm text-shelf-muted mt-3 line-clamp-2">{{ collection.description }}</p>{% endif %}
            </a>

            {% if can_edit %}
            <details class="border-t border-shelf-border px-4 py-3">
                <summary class="cursor-pointer text-xs text-shelf-muted hover:text-shelf-text">Manage collection</summary>
                <form method="post" action="/api/collections/{{ collection.id }}" class="mt-3 space-y-2">
                    <input name="name" type="text" maxlength="100" required value="{{ collection.name }}"
                           aria-label="Collection name"
                           class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">
                    <textarea name="description" maxlength="500" rows="2" aria-label="Collection description"
                              class="w-full bg-shelf-bg border border-shelf-border rounded-lg px-3 py-2 text-sm text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">{{ collection.description or '' }}</textarea>
                    <div class="flex items-center justify-between gap-2">
                        <button type="submit" class="px-3 py-1.5 bg-shelf-accent text-white rounded-lg text-xs hover:bg-shelf-accent2 transition-colors">Save</button>
                        <button type="button" hx-delete="/api/collections/{{ collection.id }}"
                                hx-target="#collection-card-{{ collection.id }}" hx-swap="outerHTML"
                                hx-confirm="Delete collection '{{ collection.name }}'? Items will stay in Shelf."
                                class="px-3 py-1.5 text-shelf-error text-xs hover:bg-shelf-error/10 rounded-lg transition-colors">Delete collection</button>
                    </div>
                </form>
            </details>
            {% endif %}
        </article>
        {% endfor %}
    </section>
    {% else %}
    <section class="bg-shelf-card border border-shelf-border rounded-xl p-8 text-center">
        <h2 class="text-lg font-semibold">No collections yet</h2>
        <p class="text-sm text-shelf-muted mt-2">{% if can_edit %}Create a collection above, then add items from their detail pages.{% else %}Collections created for this library will appear here.{% endif %}</p>
    </section>
    {% endif %}
</div>
{% endblock %}
''')

Path("app/templates/fragments/item_collections.html").write_text(r'''{% set can_edit = user and user.role in ('admin', 'editor') %}
<div id="item-collections" class="mt-5 pt-5 border-t border-shelf-border" data-testid="item-collections">
    <div class="flex items-center justify-between gap-3 mb-2">
        <h3 class="text-xs font-semibold uppercase tracking-wider text-shelf-muted">Collections</h3>
        <a href="/collections" class="text-xs text-shelf-accent2 hover:text-white transition-colors">Browse collections</a>
    </div>

    {% if item_collections %}
    <div class="flex flex-wrap gap-2 mb-3">
        {% for collection in item_collections %}
        <span class="inline-flex items-center gap-1 rounded-full bg-shelf-accent/10 border border-shelf-accent/30 text-shelf-accent2 text-xs pl-2.5 pr-1 py-1">
            <a href="/browse?collection={{ collection.name|urlencode }}" class="hover:text-white">{{ collection.name }}</a>
            {% if can_edit %}
            <button type="button" hx-delete="/api/items/{{ item_id }}/collections/{{ collection.id }}"
                    hx-target="#item-collections" hx-swap="outerHTML"
                    aria-label="Remove from {{ collection.name }}"
                    class="w-5 h-5 rounded-full text-shelf-muted hover:text-white hover:bg-shelf-hover">&times;</button>
            {% endif %}
        </span>
        {% endfor %}
    </div>
    {% else %}
    <p class="text-sm text-shelf-muted mb-3">This item is not in a collection.</p>
    {% endif %}

    {% if can_edit %}
        {% if available_collections %}
        <form hx-post="/api/items/{{ item_id }}/collections" hx-target="#item-collections" hx-swap="outerHTML"
              class="flex flex-wrap items-center gap-2">
            <label for="item-collection-select-{{ item_id }}" class="sr-only">Add to collection</label>
            <select id="item-collection-select-{{ item_id }}" name="collection_id" required
                    class="bg-shelf-bg border border-shelf-border rounded-lg px-3 py-1.5 text-sm text-shelf-text focus:ring-2 focus:ring-shelf-accent outline-none">
                <option value="">Add to collection...</option>
                {% for collection in available_collections %}<option value="{{ collection.id }}">{{ collection.name }}</option>{% endfor %}
            </select>
            <button type="submit" class="px-3 py-1.5 bg-shelf-hover border border-shelf-border text-shelf-text rounded-lg text-xs hover:border-shelf-accent/50 transition-colors">Add</button>
        </form>
        {% else %}
        <p class="text-xs text-shelf-muted">{% if item_collections %}This item is already in every collection.{% else %}<a href="/collections" class="text-shelf-accent2 hover:text-white">Create a collection</a> to start grouping items.{% endif %}</p>
        {% endif %}
    {% endif %}
</div>
''')


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
replace(
    "tests/test_nav.py",
    '''        "home", "browse", "series", "discover", "scan", "intake", "music",
        "store", "stats", "settings", "logs",
''',
    '''        "home", "browse", "collections", "series", "discover", "scan", "intake", "music",
        "store", "stats", "settings", "logs",
''',
)
replace(
    "tests/test_nav.py",
    '''def test_home_collection_and_settings_are_not_hideable():
    assert set(ALWAYS_VISIBLE) == {"home", "browse", "settings"}
''',
    '''def test_home_browse_collections_and_settings_are_not_hideable():
    assert set(ALWAYS_VISIBLE) == {"home", "browse", "collections", "settings"}
''',
)
replace(
    "tests/test_nav.py",
    '''    assert primary == ["home", "browse", "series", "discover"]
''',
    '''    assert primary == ["home", "browse", "collections", "series", "discover"]
''',
)
replace(
    "tests/test_nav.py",
    '''    assert keys == ["home", "browse", "series", "music", "store", "stats"]
''',
    '''    assert keys == ["home", "browse", "collections", "series", "music", "store", "stats"]
''',
)
replace(
    "tests/test_nav.py",
    '''    assert 'data-nav-tab="browse"' in html
    assert 'data-nav-tab="settings"' in html
''',
    '''    assert 'data-nav-tab="browse"' in html
    assert 'data-nav-tab="collections"' in html
    assert 'data-nav-tab="settings"' in html
''',
)

Path("tests/test_collections.py").write_text(r'''"""Browse naming, global nav search, and curated Collections."""


def _item(db, title, isbn):
    cur = db.execute(
        "INSERT INTO items (title, isbn, media_type, source) VALUES (?, ?, 'book', 'test')",
        (title, isbn),
    )
    return cur.lastrowid


def _collection(db, name, description=None):
    cur = db.execute(
        "INSERT INTO collections (name, description) VALUES (?, ?)",
        (name, description),
    )
    return cur.lastrowid


def test_collection_tables_exist_on_fresh_database(db):
    names = {
        row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "collections" in names
    assert "collection_items" in names


def test_collection_page_was_renamed_to_browse(admin_client):
    html = admin_client.get("/browse").text
    assert "<title>Browse — Shelf</title>" in html
    assert ">Browse <span id=\"collection-count\"" in html
    assert 'data-nav-tab="browse"' in html
    assert ">Browse</a>" in html


def test_collections_is_a_primary_navigation_destination(admin_client):
    html = admin_client.get("/collections").text
    assert "<title>Collections — Shelf</title>" in html
    assert 'data-nav-tab="collections"' in html
    assert "Collections are separate from Series and Tags" in html


def test_navigation_search_redirects_to_browse(admin_client):
    response = admin_client.get(
        "/search", params={"query": "The Left Hand of Darkness"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/browse?q=The+Left+Hand+of+Darkness"


def test_navigation_search_bar_is_rendered_on_desktop_and_mobile(admin_client):
    html = admin_client.get("/collections").text
    assert 'data-testid="nav-search-form"' in html
    assert 'data-testid="nav-search-form-mobile"' in html
    assert html.count('name="query"') == 2
    # The global search deliberately does not use name=q, so Browse's HTMX
    # filter registry never double-sends its live search value.
    assert html.count('name="q"') == 0


def test_editor_can_create_and_update_a_collection(editor_client, db):
    response = editor_client.post(
        "/api/collections",
        data={"name": "  Favourite   Science Fiction  ", "description": "Top shelf picks"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = db.execute("SELECT * FROM collections").fetchone()
    assert row["name"] == "Favourite Science Fiction"
    assert row["description"] == "Top shelf picks"

    response = editor_client.post(
        f"/api/collections/{row['id']}",
        data={"name": "SF Favourites", "description": "Best of the best"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    updated = db.execute("SELECT * FROM collections WHERE id = ?", (row["id"],)).fetchone()
    assert updated["name"] == "SF Favourites"
    assert updated["description"] == "Best of the best"


def test_collection_names_are_case_insensitively_unique(editor_client):
    assert editor_client.post(
        "/api/collections", data={"name": "Favourites", "description": ""}
    ).status_code == 200
    response = editor_client.post(
        "/api/collections", data={"name": "favourites", "description": ""},
        follow_redirects=False,
    )
    assert response.status_code == 409


def test_viewer_can_browse_but_cannot_change_collections(viewer_client, db):
    cid = _collection(db, "Shared list")
    db.commit()
    assert viewer_client.get("/collections").status_code == 200
    assert viewer_client.post(
        "/api/collections", data={"name": "Nope", "description": ""}
    ).status_code == 403
    assert viewer_client.delete(f"/api/collections/{cid}").status_code == 403


def test_item_membership_round_trip_and_detail_fragment(editor_client, db):
    item_id = _item(db, "Dune", "9780441172719")
    collection_id = _collection(db, "Desert worlds")
    db.commit()

    response = editor_client.post(
        f"/api/items/{item_id}/collections",
        data={"collection_id": collection_id},
    )
    assert response.status_code == 200
    assert "Desert worlds" in response.text
    membership = db.execute(
        "SELECT 1 FROM collection_items WHERE collection_id = ? AND item_id = ?",
        (collection_id, item_id),
    ).fetchone()
    assert membership

    detail = editor_client.get(f"/item/{item_id}").text
    assert 'data-testid="item-collections"' in detail
    assert "Desert worlds" in detail
    assert "/browse?collection=Desert%20worlds" in detail

    response = editor_client.delete(
        f"/api/items/{item_id}/collections/{collection_id}"
    )
    assert response.status_code == 200
    assert "This item is not in a collection" in response.text
    assert db.execute(
        "SELECT 1 FROM collection_items WHERE collection_id = ? AND item_id = ?",
        (collection_id, item_id),
    ).fetchone() is None


def test_browse_collection_filter_only_returns_members(admin_client, db):
    dune = _item(db, "Dune", "9780441172719")
    _item(db, "Neuromancer", "9780441569595")
    cid = _collection(db, "Desert worlds")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (cid, dune),
    )
    db.commit()

    html = admin_client.get("/browse", params={"collection": "Desert worlds"}).text
    assert "Dune" in html
    assert "Neuromancer" not in html
    assert 'name="collection" value="Desert worlds"' in html


def test_collection_cards_show_counts_and_link_into_browse(admin_client, db):
    item_id = _item(db, "Piranesi", "9781526622433")
    cid = _collection(db, "Short favourites", "Books I would reread")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (cid, item_id),
    )
    db.commit()

    html = admin_client.get("/collections").text
    assert "Short favourites" in html
    assert "1 item" in html
    assert "Books I would reread" in html
    assert "/browse?collection=Short%20favourites" in html


def test_deleting_collection_keeps_items_and_cascades_membership(editor_client, db):
    item_id = _item(db, "Solaris", "9780156027601")
    cid = _collection(db, "Temporary")
    db.execute(
        "INSERT INTO collection_items (collection_id, item_id) VALUES (?, ?)",
        (cid, item_id),
    )
    db.commit()

    response = editor_client.delete(f"/api/collections/{cid}")
    assert response.status_code == 200
    assert db.execute("SELECT 1 FROM collections WHERE id = ?", (cid,)).fetchone() is None
    assert db.execute(
        "SELECT 1 FROM collection_items WHERE collection_id = ?", (cid,)
    ).fetchone() is None
    assert db.execute("SELECT title FROM items WHERE id = ?", (item_id,)).fetchone()["title"] == "Solaris"
''')
