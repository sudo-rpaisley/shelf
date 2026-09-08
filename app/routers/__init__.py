"""Router package initialisation.

Import schema-only extension services here so their migrations are registered
before application startup calls ``init_db``.
"""

from app.services import libraries as libraries_service  # noqa: F401,E402
