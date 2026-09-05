"""Router package initialisation.

The main application already mounts ``series.router`` and ``pages.router``.
Importing focused extensions here lets them attach routes to those same
routers without adding more top-level router registration in ``app.main``.

Magazine barcode handling follows the same extension principle: the existing
``/api/scan`` route keeps ownership of scanning, while focused periodical and
personal-state modules adapt the narrow workflows they own without enlarging
the shared routers.
"""

# Register focused schema extensions before application startup calls init_db.
from app.services import libraries as libraries_service  # noqa: F401,E402

# Import the base routers first, then extensions which decorate them.
from app.routers import series as series  # noqa: F401,E402
from app.routers import series_detail as series_detail  # noqa: F401,E402
from app.routers import series_bulk_issues as series_bulk_issues  # noqa: F401,E402
from app.routers import series_memberships as series_memberships  # noqa: F401,E402

from app.routers import pages as pages  # noqa: F401,E402
from app.routers import home_dashboard as home_dashboard  # noqa: F401,E402
from app.routers import location_tree as location_tree  # noqa: F401,E402
from app.routers import item_copies as item_copies  # noqa: F401,E402
from app.routers import attention as attention  # noqa: F401,E402
from app.routers import user_state as user_state  # noqa: F401,E402
from app.routers import user_state_stats as user_state_stats  # noqa: F401,E402

from app.routers import items_common as items_common  # noqa: F401,E402
from app.routers import items_magazines as items_magazines  # noqa: F401,E402
from app.routers import magazines as magazines  # noqa: F401,E402
from app.routers import shelf_fill as shelf_fill  # noqa: F401,E402

items_magazines.install_scan_dispatch()

# Load the large items router once its lower-level scan dispatchers are ready,
# then apply focused route replacements in increasing specificity: personal
# state first, Browse/search visibility, direct/core reads, then one final
# personal-state guard which removes both historical route generations.
from app.routers import items as items  # noqa: F401,E402
from app.routers import user_state_items as user_state_items  # noqa: F401,E402
from app.routers import library_access as library_access  # noqa: F401,E402
from app.routers import library_item_access as library_item_access  # noqa: F401,E402
from app.routers import library_item_guard as library_item_guard  # noqa: F401,E402
