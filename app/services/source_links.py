"""Explicit removal of manual availability links, never catalogue items."""

from app.services.source_connectors.base import ConnectorError


def detach(db, source_id: int, record_id: int) -> None:
    """Remove a live item's manually attached link inside the caller's lock.

    Machine-synced identities must be managed by their source scope instead;
    deleting one here would only make the next sync silently recreate it.
    """
    row = db.execute(
        "SELECT r.id, s.provider FROM source_records r "
        "JOIN source_instances s ON s.id = r.source_id "
        "JOIN items_live i ON i.id = r.item_id WHERE r.source_id = ? AND r.id = ?",
        (source_id, record_id),
    ).fetchone()
    if row is None:
        raise ConnectorError("not_found")
    if row["provider"] != "links":
        raise ConnectorError("manual_only")
    db.execute("DELETE FROM source_records WHERE source_id = ? AND id = ?", (source_id, record_id))
