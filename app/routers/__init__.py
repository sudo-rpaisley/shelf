"""Router package initialisation.

Import the focused series-detail extension so it attaches its read-only route
to the existing series router before app.main mounts that router.
"""

from app.routers import series as series  # noqa: F401,E402
from app.routers import series_detail as series_detail  # noqa: F401,E402
