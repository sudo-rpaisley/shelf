"""Recover missing singleton bindings from exact prior import provenance only.

No title/series matching, automatic item merge, schema installation, commits,
remote I/O or credential reads. Current bindings always win. Call only when a
binding is missing, under the same write transaction as persistence. This also
sees Trash: a lost binding must never turn a deleted item into a new live one.
"""

_PREFIXES = {"komga": "komga:", "romm": "romm:", "audiobookshelf": "audiobookshelf:"}


class ImportBindingConflict(ValueError):
    """More than one catalogue identity needs explicit review, not a guess."""


def previous_item(db, provider: str, external_id: str):
    if provider not in _PREFIXES or not isinstance(external_id, str) or not external_id.strip():
        raise ValueError("Invalid import identity")
    if not db.in_transaction:
        raise ValueError("Import identity recovery requires the caller's transaction")
    rows = db.execute(
        "SELECT DISTINCT i.* FROM item_metadata_provenance p "
        "JOIN items i ON i.id = p.item_id WHERE p.source_key = ? "
        "ORDER BY i.deleted_at IS NOT NULL, i.id LIMIT 2",
        (_PREFIXES[provider] + external_id,),
    ).fetchall()
    if len(rows) > 1:
        raise ImportBindingConflict("Imported identity has multiple catalogue targets; review before rescanning")
    return rows[0] if rows else None
