"""Router package composition for self-contained feature contributions."""

from app import nav as _nav
from app.routers import music as _music
from app.routers import pages as _pages

_pages.router.include_router(_music.router)

if not any(tab.get("key") == "music" for tab in _nav.NAV_TABS):
    _nav.NAV_TABS.insert(1, {"key": "music", "label": "Music", "path": "/music"})
    _nav.HIDEABLE_TABS = [
        tab for tab in _nav.NAV_TABS if tab["key"] not in _nav.ALWAYS_VISIBLE
    ]
    _nav.HIDEABLE_KEYS = frozenset(tab["key"] for tab in _nav.HIDEABLE_TABS)
