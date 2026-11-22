"""Compatibility handoff for physical and plural digital holdings.

Schema, lineage and the one-time foundation seed belong to app.database. This
module neither defines DDL nor resets/replays the foundation marker. In
particular an ensure/read must not recreate an intentionally absent copy.
"""

from collections.abc import Iterable, Mapping
from contextlib import contextmanager

from app.database import ensure_holdings_schema

ensure_schema = ensure_holdings_schema
install_schema = ensure_holdings_schema
ensure_foundation = ensure_holdings_schema

_REQUIRED_TABLES = frozenset({
    "location_nodes", "item_copies", "digital_holdings",
    "periodical_publications", "periodical_issues",
})
_PROVIDER_FIELDS = (
    ("audiobookshelf", "abs_id", "abs_library_id"),
    ("komga", "komga_id", "komga_library_id"),
    ("romm", "romm_id", "romm_platform_id"),
)
_SIDECARS = {
    "komga": ("komga_records", "komga_id", "library_id"),
    "romm": ("romm_records", "romm_id", "platform_id"),
}


@contextmanager
def _projection_scope(db):
    db.execute("SAVEPOINT holdings_projection")
    try:
        yield
    except Exception:
        db.execute("ROLLBACK TO holdings_projection")
        db.execute("RELEASE holdings_projection")
        raise
    else:
        db.execute("RELEASE holdings_projection")


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _identity_available(db, item_id, provider, external_id):
    # Deliberate physical reads: predict identity collisions including Trash and
    # hidden libraries, but never return the blocker, its title or its status.
    if db.execute(
        "SELECT 1 FROM digital_holdings WHERE provider = ? AND external_id = ? AND item_id != ?",
        (provider, external_id, item_id),
    ).fetchone():
        return False
    inline = next((id_key for name, id_key, _ in _PROVIDER_FIELDS if name == provider), None)
    columns = {r["name"] for r in db.execute("PRAGMA table_info(items)").fetchall()}
    if inline in columns and db.execute(
        f"SELECT 1 FROM items WHERE {inline} = ? AND id != ?", (external_id, item_id)
    ).fetchone():
        return False
    sidecar = _SIDECARS.get(provider)
    if sidecar is not None:
        table, id_key, _ = sidecar
        if _has_table(db, table) and db.execute(
            f"SELECT 1 FROM {table} WHERE {id_key} = ? AND item_id != ?", (external_id, item_id)
        ).fetchone():
            return False
    return True


def schema_available(db) -> bool:
    rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return _REQUIRED_TABLES <= {row["name"] for row in rows}


def ensure_legacy_location_nodes(db) -> None:
    """Explicit DML projection of flat locations, not an item/copy backfill.

    Equality of IDs/names is never a mapping. A conflicting unrelated root is
    refused rather than attached to the flat row by guesswork.
    """
    from app.services.location_tree import legacy_location_to_node

    ensure_schema(db)
    for row in db.execute("SELECT id FROM locations ORDER BY sort_order, id").fetchall():
        legacy_location_to_node(db, row["id"], create=True)


def reconcile_digital_holdings(db, item_id: int, provider: str,
                               identities: Iterable[Mapping]) -> None:
    """Upsert explicit translated identities, preserving every other holding.

    Provider translation/detach policy is the connector owner's responsibility.
    An inline NULL is NOT evidence to delete plural ownership. This helper
    never starts another transaction, calls a provider or rewrites inline IDs.
    """
    from app.services.item_copies import write_lock
    from app.services.catalogue_security import require_item_role

    write_lock(db)
    require_item_role(db, item_id, "editor")
    if not db.execute("SELECT 1 FROM items_live WHERE id = ?", (item_id,)).fetchone():
        raise ValueError("Item not found")
    values = {}
    for identity in identities:
        raw_id = identity.get("external_id")
        external_id = "" if raw_id is None else str(raw_id).strip()
        if not provider or not external_id:
            raise ValueError("A digital holding requires a provider and external identity")
        if not _identity_available(db, item_id, provider, external_id):
            raise ValueError("Digital identity is unavailable")
        metadata = tuple(identity.get(key) for key in ("library_id", "format", "source_url"))
        previous = values.get(external_id)
        if previous is not None:
            if any(old is not None and new is not None and old != new
                   for old, new in zip(previous, metadata)):
                raise ValueError("Digital holding projection is ambiguous")
            metadata = tuple(new if new is not None else old for old, new in zip(previous, metadata))
        values[external_id] = metadata
    with _projection_scope(db):
        for external_id, metadata in values.items():
            db.execute(
                "INSERT INTO digital_holdings "
                "(item_id, provider, external_id, library_id, format, source_url) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(item_id, provider, external_id) DO UPDATE SET "
                "library_id = COALESCE(excluded.library_id, digital_holdings.library_id), "
                "format = COALESCE(excluded.format, digital_holdings.format), "
                "source_url = COALESCE(excluded.source_url, digital_holdings.source_url), "
                "updated_at = datetime('now')", (item_id, provider, external_id, *metadata),
            )


def _sync_provider(db, item, provider: str, id_key: str, library_key: str) -> None:
    keys = set(item.keys())
    identities = {}

    def add(external_id, library_id):
        external_id = str(external_id or "").strip()
        if not external_id:
            return
        library_id = str(library_id) if library_id is not None else None
        previous = identities.get(external_id)
        if previous is not None and previous["library_id"] not in (None, library_id) and library_id is not None:
            raise ValueError("Digital holding projection is ambiguous")
        identities[external_id] = {"external_id": external_id,
                                   "library_id": library_id or (previous or {}).get("library_id")}

    if id_key in keys:
        add(item[id_key], item[library_key] if library_key in keys else None)
    sidecar = _SIDECARS.get(provider)
    if sidecar is not None:
        table, external_key, scope_key = sidecar
        if _has_table(db, table):
            for row in db.execute(
                f"SELECT {external_key}, {scope_key} FROM {table} WHERE item_id = ?", (item["id"],)
            ).fetchall():
                add(row[external_key], row[scope_key])
    if identities:
        reconcile_digital_holdings(db, item["id"], provider, identities.values())


def sync_item(db, item_id: int, *, create_copy: bool = False) -> None:
    """Single writer handoff, on the caller's locked transaction.

    Core insertion/explicit acquisition passes ``create_copy=True``; updates
    use the default. This distinction preserves archive zero-copy states and
    intentional absence after the foundation seed. Existing precise copies
    always win over flat item placement, and copy Trash never creates a new ID.
    """
    from app.services import item_copies
    from app.services.catalogue_security import require_item_role

    ensure_schema(db)
    item_copies.write_lock(db)
    require_item_role(db, item_id, "editor")
    item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return
    with _projection_scope(db):
        item_copies.sync_primary_location(
            db, item_id, item["location_id"], create_copy=create_copy
        )
        for provider, id_key, library_key in _PROVIDER_FIELDS:
            _sync_provider(db, item, provider, id_key, library_key)


sync_item_holding = sync_item


def sync_all_holdings(db) -> None:
    """Retired sweep entry point: central schema preparation only, never reseed."""
    ensure_schema(db)
