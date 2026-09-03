"""Router package initialisation.

The main application already mounts ``series.router`` and ``pages.router``.
Importing focused extensions here lets them attach routes to those same
routers without adding more top-level router registration in ``app.main``.

Magazine barcode handling follows the same extension principle: the existing
``/api/scan`` route keeps ownership of scanning, while the focused magazine
module installs a small 977/ISSN dispatch wrapper around the shared UPC path.
"""

# Import the base routers first, then extensions which decorate them.
from app.routers import series as series  # noqa: F401,E402
from app.routers import series_detail as series_detail  # noqa: F401,E402
from app.routers import series_memberships as series_memberships  # noqa: F401,E402

from app.routers import pages as pages  # noqa: F401,E402
from app.routers import home_dashboard as home_dashboard  # noqa: F401,E402
from app.routers import location_tree as location_tree  # noqa: F401,E402

from app.routers import items_common as items_common  # noqa: F401,E402
from app.routers import items_magazines as items_magazines  # noqa: F401,E402

items_magazines.install_scan_dispatch()
