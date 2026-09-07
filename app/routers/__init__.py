"""Router package composition for the Related Media UI contribution."""

from app.routers import pages as _pages
from app.routers import related_media as _related_media

_pages.router.include_router(_related_media.router)
