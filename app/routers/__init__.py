"""Router package composition for Music plus optional Discogs enrichment."""

from app.routers import discogs as _discogs
from app.routers import music as _music
from app.routers import pages as _pages
from app.services import music_covers as _music_covers

_music_covers.register_cover_art_archive()
_pages.router.include_router(_music.router)
_pages.router.include_router(_discogs.router)
