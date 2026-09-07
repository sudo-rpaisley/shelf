"""Router package composition for self-contained integrations."""

from app.routers import pages as _pages
from app.routers import romm as _romm
from app.routers import romm_catalog as _romm_catalog

_pages.router.include_router(_romm.router)
_pages.router.include_router(_romm_catalog.router)
