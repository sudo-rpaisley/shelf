"""Router package initialisation.

The main application already mounts ``series.router``.  Importing the focused
series-detail extension here lets it attach its read-only route to that same
router without adding another top-level router registration in ``app.main``.
"""

# Import the base router first, then the extension which decorates it.
from app.routers import series as series  # noqa: F401,E402
from app.routers import series_detail as series_detail  # noqa: F401,E402
