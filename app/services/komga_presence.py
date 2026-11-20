"""Caller-transaction-owned Komga scan evidence; no catalogue/history writes.

Only exact current library IDs are reconciled. A changed server/library namespace
needs review, not a title-based mapping. Configuration material is compared in
memory only, never copied to this cache, logs, progress or request payloads.
"""

import json
import uuid

from app.services.komga_libraries import KomgaError


_SETTINGS = ("komga_url", "komga_api_key", "komga_library_kinds", "komga_excluded_libraries")


def revision(db) -> tuple:
    values = dict(db.execute("SELECT key, value FROM settings WHERE key IN (?, ?, ?, ?)", _SETTINGS))
    return tuple(values.get(key, "") for key in _SETTINGS)


def _locked(db):
    if not db.in_transaction:
        raise ValueError("Komga scan evidence requires the caller's write transaction")


def current(db, expected, token=None):
    _locked(db)
    if revision(db) != expected:
        raise KomgaError("Komga settings changed during sync; run Sync Now again")
    if token is not None:
        row = db.execute("SELECT token FROM komga_scan_generation WHERE slot = 1").fetchone()
        if row is None or row[0] != token:
            raise KomgaError("A newer Komga sync superseded this run")


def start(db, expected) -> str:
    current(db, expected)
    token = uuid.uuid4().hex
    db.execute("INSERT INTO komga_scan_generation (slot, token) VALUES (1, ?) "
               "ON CONFLICT(slot) DO UPDATE SET token = excluded.token", (token,))
    return token


def reset(db):
    _locked(db)
    db.execute("UPDATE komga_presence SET availability = 'unknown' WHERE availability != 'unknown'")
    # Also fence a scope change that is subsequently changed back to identical
    # settings: an earlier in-flight traversal must not regain authority.
    db.execute("UPDATE komga_scan_generation SET token = ? WHERE slot = 1", (uuid.uuid4().hex,))


def observed(db, komga_id, library_id, server):
    _locked(db)
    db.execute(
        "INSERT INTO komga_presence (komga_id, library_id, server_url, availability, last_seen_at) "
        "VALUES (?, ?, ?, 'available', datetime('now')) ON CONFLICT(komga_id) DO UPDATE SET "
        "library_id = excluded.library_id, server_url = excluded.server_url, availability = 'available', "
        "last_seen_at = excluded.last_seen_at, checked_at = datetime('now')",
        (komga_id, library_id, server),
    )


def reconcile(db, library_id, server, seen, *, expected, token) -> int:
    """Caller certifies a complete, warning-free traversal before calling.

    Atomic upsert of missing live holdings only; no per-record existence probes,
    ID reassignment, item mutation, implicit commit or new connection.
    """
    current(db, expected, token)
    missing = db.execute(
        "SELECT r.komga_id FROM komga_records r JOIN items_live i ON i.id = r.item_id "
        "WHERE r.library_id = ? AND r.komga_id NOT IN (SELECT value FROM json_each(?)) "
        "AND NOT EXISTS (SELECT 1 FROM komga_presence p WHERE p.komga_id = r.komga_id "
        "AND p.library_id = r.library_id AND p.server_url = ? AND p.availability = 'unavailable')",
        (library_id, json.dumps(sorted(seen)), server),
    ).fetchall()
    for row in missing:
        db.execute(
            "INSERT INTO komga_presence (komga_id, library_id, server_url, availability) "
            "VALUES (?, ?, ?, 'unavailable') ON CONFLICT(komga_id) DO UPDATE SET "
            "library_id = excluded.library_id, server_url = excluded.server_url, "
            "availability = 'unavailable', checked_at = datetime('now')",
            (row[0], library_id, server),
        )
    return len(missing)


def states(db, identities) -> dict:
    rows = db.execute(
        "SELECT komga_id, availability FROM komga_cached_availability "
        "WHERE presence_id IS NOT NULL AND komga_id IN (SELECT value FROM json_each(?))", (json.dumps(list(identities)),),
    ).fetchall()
    return {row[0]: row[1] for row in rows}
