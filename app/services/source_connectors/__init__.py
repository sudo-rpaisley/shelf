"""Explicit read-only source capabilities; adapters are imported lazily."""

from dataclasses import dataclass
from importlib import import_module

from app.i18n import N_
from app.services.source_connectors.base import ConnectorError


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    syncable: bool
    manual_links: bool = False
    credential_fields: tuple[str, ...] = ()

    async def fetch_catalogue(self, source: dict, credentials: dict):
        if not self.syncable:
            raise ConnectorError("manual_only")
        adapter = import_module(f"app.services.source_connectors.{self.id}")
        return await adapter.fetch_catalogue(source, credentials)


PROVIDERS = {
    "opds": Provider("opds", N_("OPDS catalogue"), True,
                     credential_fields=("username", "password")),
    "jellyfin": Provider("jellyfin", N_("Jellyfin"), True,
                         credential_fields=("token",)),
    "links": Provider("links", N_("External library links"), False, manual_links=True),
}


def get_provider(provider_id: str) -> Provider:
    try:
        return PROVIDERS[provider_id]
    except (KeyError, TypeError):
        raise ConnectorError("unsupported_provider") from None
