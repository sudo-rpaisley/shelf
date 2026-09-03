"""Router package initialisation.

The main application already mounts ``series.router``.  Importing the focused
series extensions here lets them attach their routes to that same router
without adding more top-level router registration in ``app.main``.

Magazine barcode handling follows the same extension principle: the existing
``/api/scan`` route keeps ownership of scanning, while the focused magazine
module installs a small 977/ISSN dispatch wrapper around the shared UPC path.
"""

# Import the base router first, then extensions which decorate it.
from app.routers import series as series  # noqa: F401,E402
from app.routers import series_detail as series_detail  # noqa: F401,E402
from app.routers import series_memberships as series_memberships  # noqa: F401,E402

from app.routers import items_common as items_common  # noqa: F401,E402
from app.routers import items_magazines as items_magazines  # noqa: F401,E402

items_magazines.install_scan_dispatch()
