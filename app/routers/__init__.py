"""Router package composition for self-contained feature contributions."""

from app.routers import music as _music
from app.routers import pages as _pages

_pages.router.include_router(_music.router)
