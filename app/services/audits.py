"""Shelf audits — server-side sessions, their scan entries, and the report.

An audit is a session (`audit_sessions`) plus the scans recorded into it
(`audit_entries`). The entries are the record; the report is **computed**
from them against the catalogue as it stands now, never stored, so an old
audit's Missing count moves when the collection does — and the page says so.

**A NULL foreign key never carries domain meaning here.** Both tables point at
rows that can be deleted (`ON DELETE SET NULL`), so what kind of audit a
session is reads from `scope`, and whether an entry named one copy reads from
`copy_specific` — never from a NULL. A deleted location must not turn its audit
into a whole-library one, and a purged copy must not turn its entry into an
item-level one that confirms every sibling.

Entry security_library_id/security_user_id are plain enduring snapshots, not
nullable lifecycle FKs. Purge must never turn a private scan into public history.

**The expected set is the old Show Missing Items query** (removed with that
button), moved here and widened from one row per item to one row per copy
(`expected_rows`). The
inventory scan arm (`app/routers/scan_inventory.py`) answers "is it here?" by
the same rule — copies first, the `items.location_id` seam only for an item
with no live copy row (G86) — and the two must keep agreeing, or an audit
contradicts its own scans.

Every read of the item and copy relations goes through the `items_live` /
`copies_live` views (`make check-deleted`). Writes take the caller's
connection and transaction: the scan arm and the report's actions hold
`BEGIN IMMEDIATE` around the decision and the entry together (G18, G118).
The missing-selection preflight deliberately reads physical copies only to
distinguish an unauthorized existing target from an unknown native no-op.
Nothing in this module logs (G3).
"""

from dataclasses import dataclass, field
from typing import Any

from app.database import get_db
from app.media_types import is_physical_media
from app.services import libraries
from app.services.catalogue_security import current_actor, require_item_role
from app.services.location_tree import location_path, validated_node_id
from fastapi import HTTPException

SCOPES = ("location", "library")
WHOLE_LIBRARY = "Whole library"

#: The results an entry may carry — the inventory arm's statuses, minus
#: `error`, which records nothing.
RESULTS = ("confirmed", "relocated", "elsewhere", "not_owned", "in_trash")

SECTIONS = ("found", "moved", "elsewhere", "loan", "missing", "unexpected")


class AuditError(ValueError):
    """A request the audit cannot honour — the message is user-facing."""


# ── Sessions ────────────────────────────────────────────────────────────────

_SESSION_SELECT = (
    "SELECT s.id, s.location_id, s.started_at, s.finished_at, s.notes, "
    "       s.scope, s.location_name, "
    "       (SELECT COUNT(*) FROM audit_entries e WHERE e.session_id = s.id) AS scans "
    "FROM audit_sessions s "
)


def _entry_visible(db, entry) -> bool:
    """Intersect current authority with enduring, non-FK entry provenance.

    Purging an item sets item_id to NULL, but never turns its private raw code
    into a globally readable unmatched scan. Unknown historical provenance is
    admin-only; a genuinely unmatched scan also belongs to its recorded actor.
    No actor is the established trusted non-HTTP service context.
    """
    actor = current_actor()
    if actor is None:
        return True
    if entry["item_id"] is not None:
        try:
            require_item_role(db, entry["item_id"], "viewer", include_deleted=True)
        except HTTPException as exc:
            if exc.status_code not in (403, 404):
                raise
            return False
    if entry["security_library_id"] is not None:
        return libraries.has_library_role(db, actor, entry["security_library_id"], "viewer")
    if entry["item_id"] is not None:
        return True  # Still-bound legacy entry: current item authority was proven.
    if libraries._account_admin(actor):
        # Includes the underlying account's read scope for a viewer-capped token,
        # without changing ANY write authorization or promoting its role.
        return True
    return (entry["result"] == "not_owned"
            and entry["security_user_id"] == int(actor["id"]))


def _visible_scan_count(db, audit_id: int) -> int:
    return sum(_entry_visible(db, row) for row in db.execute(
        "SELECT item_id, result, security_library_id, security_user_id "
        "FROM audit_entries WHERE session_id = ?", (audit_id,)
    ).fetchall())


def _session_dict(db, row) -> dict[str, Any] | None:
    if row is None:
        return None
    session = dict(row)
    session["scans"] = _visible_scan_count(db, session["id"])
    session["location_node_id"] = session["location_id"]
    session["location_deleted"] = (
        session["scope"] == "location" and session["location_id"] is None
    )
    session["label"] = (
        f"{session['location_name']} (deleted)"
        if session["location_deleted"] else session["location_name"]
    )
    session["is_open"] = session["finished_at"] is None
    return session


def create_session(db, *, scope: str, location_id: int | None) -> int:
    """Start an audit and return its id. Caller holds the write lock.

    `scope="location"` needs a location that exists; its name is snapshotted
    into `location_name` so the audit keeps a name after the location goes.
    `scope="library"` stores no location. Raises `AuditError` otherwise.
    """
    from app.services.item_copies import write_lock
    write_lock(db)
    if scope == "library":
        location_id, name = None, WHOLE_LIBRARY
    elif scope == "location":
        row = None
        if location_id is not None and location_id > 0:
            row = db.execute(
                "SELECT name FROM location_nodes WHERE id = ?", (location_id,)
            ).fetchone()
        if row is None:
            raise AuditError("Pick a location to audit, or start a whole-library audit")
        name = " / ".join(part["name"] for part in location_path(db, location_id))
    else:
        raise AuditError("Pick a location to audit, or start a whole-library audit")
    cursor = db.execute(
        "INSERT INTO audit_sessions (scope, location_id, location_name) VALUES (?, ?, ?)",
        (scope, location_id, name),
    )
    return cursor.lastrowid


def get_session(db, audit_id: int | None) -> dict[str, Any] | None:
    """The session row (open or finished) with derived labels, or None."""
    if not audit_id:
        return None
    return _session_dict(db,
        db.execute(_SESSION_SELECT + "WHERE s.id = ?", (audit_id,)).fetchone()
    )


def open_sessions(db) -> list[dict[str, Any]]:
    """Every unfinished session, newest first — the strip's Join list."""
    rows = db.execute(
        _SESSION_SELECT + "WHERE s.finished_at IS NULL ORDER BY s.started_at DESC, s.id DESC"
    ).fetchall()
    return [_session_dict(db, r) for r in rows]


def list_sessions(db, limit: int = 100) -> list[dict[str, Any]]:
    """The `/audits` list: newest first, capped. No expected-set work per row."""
    rows = db.execute(
        "SELECT s.id, s.location_id, s.started_at, s.finished_at, s.notes, "
        "       s.scope, s.location_name, COUNT(e.id) AS scans, "
        "       CAST(ROUND((julianday(COALESCE(s.finished_at, datetime('now')))"
        "                   - julianday(s.started_at)) * 1440) AS INTEGER) AS minutes "
        "FROM audit_sessions s LEFT JOIN audit_entries e ON e.session_id = s.id "
        "GROUP BY s.id ORDER BY s.started_at DESC, s.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_session_dict(db, r) for r in rows]


def finish_session(db, audit_id: int) -> bool:
    """Stamp `finished_at` on an open session. False when absent or already finished."""
    from app.services.item_copies import write_lock
    write_lock(db)
    return db.execute(
        "UPDATE audit_sessions SET finished_at = datetime('now') "
        "WHERE id = ? AND finished_at IS NULL",
        (audit_id,),
    ).rowcount > 0


def set_notes(db, audit_id: int, notes: str | None) -> bool:
    """Replace a session's notes. False when the session does not exist."""
    from app.services.item_copies import write_lock
    write_lock(db)
    return db.execute(
        "UPDATE audit_sessions SET notes = ? WHERE id = ?", (notes or None, audit_id)
    ).rowcount > 0


def delete_session(db, audit_id: int) -> bool:
    """Delete a session; its entries go with it (`ON DELETE CASCADE`).

    Missing marks the audit set stay: they are copy state, not audit state.
    """
    from app.services.item_copies import write_lock
    write_lock(db)
    return db.execute(
        "DELETE FROM audit_sessions WHERE id = ?", (audit_id,)
    ).rowcount > 0


def open_session_for_write(db, audit_id: int | None) -> dict[str, Any] | None:
    """The session if it exists and is still open, else None.

    **Call it inside the caller's `BEGIN IMMEDIATE`** (G18): a scan racing
    Finish must see the finished row and write nothing.
    """
    session = get_session(db, audit_id)
    if session is None or not session["is_open"]:
        return None
    return session


def closed_session_message(db, audit_id: int | None) -> str:
    """The refusal for a scan into a session `open_session_for_write` refused."""
    if get_session(db, audit_id) is None:
        return "That audit was deleted — start a new one"
    return "That audit is finished — start a new one"


def scan_location_refusal(session: dict[str, Any], location_id: int | None) -> str | None:
    """Why a scan at `location_id` may not join `session`, or None if it may.

    A location audit takes scans only at its own location — `#location` on
    the Scan page stays editable, so the server is the check. A library audit
    takes any location (the scan's location is where the user stands).
    """
    if session["scope"] != "location":
        return None
    if session["location_deleted"]:
        return "That audit's location was deleted — start a new one"
    if location_id != session["location_id"]:
        return (
            f"This audit is for {session['location_name']} — switch the location "
            "back, or finish it and start a new one"
        )
    return None


# ── Entries ─────────────────────────────────────────────────────────────────

def add_entry(
    db, audit_id: int, *, item_id: int | None, copy_id: int | None,
    location_id: int | None, raw_code: str, result: str,
) -> int:
    """Record one scan into a session, on the caller's connection.

    Runs inside the decision's transaction (G118), so the entry and the
    relocation it describes commit or roll back together. `copy_specific` is
    set exactly when the entry names a copy, and survives that copy's purge.
    """
    if result not in RESULTS:
        raise ValueError(f"unknown audit entry result: {result!r}")
    from app.services.item_copies import write_lock
    write_lock(db)
    # Inventory callers enforce open_session_for_write under this same lock.
    # Report resolution is also legitimate AFTER Finish, so the common entry
    # funnel must not prohibit appending that resolution to a finished session.
    session = get_session(db, audit_id)
    if session is None:
        raise AuditError(closed_session_message(db, audit_id))
    refusal = scan_location_refusal(session, location_id)
    if refusal:
        raise AuditError(refusal)
    validated_node_id(db, location_id)
    security_library_id = None
    if item_id is not None:
        require_item_role(db, item_id, "editor", include_deleted=result == "in_trash")
        relation = "items" if result == "in_trash" else "items_live"
        if not db.execute(f"SELECT 1 FROM {relation} WHERE id = ?", (item_id,)).fetchone():
            raise AuditError("Item not found")
        # Snapshot the actual library mapping, including an item in Trash. These
        # plain integer columns intentionally survive item/library/user deletion;
        # never reconstruct provenance later from a barcode or a location ID.
        mapping = db.execute(
            "SELECT library_id FROM library_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        security_library_id = mapping["library_id"] if mapping is not None else None
    if copy_id is not None:
        relation = "item_copies" if result == "in_trash" else "copies_live"
        copy = db.execute(f"SELECT item_id FROM {relation} WHERE id = ?", (copy_id,)).fetchone()
        if copy is None or item_id is None or copy["item_id"] != item_id:
            raise AuditError("Copy not found")
    actor = current_actor()
    security_user_id = int(actor["id"]) if actor is not None else None
    cursor = db.execute(
        "INSERT INTO audit_entries "
        "(session_id, item_id, copy_id, location_id, raw_code, result, copy_specific, "
        "security_library_id, security_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (audit_id, item_id, copy_id, location_id, raw_code, result,
         1 if copy_id is not None else 0, security_library_id, security_user_id),
    )
    return cursor.lastrowid


def record_standalone(audit_id: int, **entry) -> str | None:
    """Record an entry for a scan arm that holds no connection of its own.

    The `not_owned` and `in_trash` arms write nothing else, so they have no
    transaction to share. Opens its own connection, takes the write lock
    first, re-reads the session under it and applies the location rule.
    Returns None on success, or the refusal to render as the `error` card.

    **Never call this inside another `with get_db()` block** (G112): the
    caller's open transaction would hold the write lock this one waits on.
    """
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        session = open_session_for_write(db, audit_id)
        if session is None:
            message = closed_session_message(db, audit_id)
            db.rollback()
            return message
        refusal = scan_location_refusal(session, entry.get("location_id"))
        if refusal:
            db.rollback()
            return refusal
        try:
            add_entry(db, audit_id, **entry)
        except ValueError as exc:
            db.rollback()
            return str(exc)
    return None


# ── The expected set ────────────────────────────────────────────────────────

def expected_rows(db, location_id: int | None) -> list[dict[str, Any]]:
    """What should be on the shelf: one row per live copy, plus copy-less items.

    `location_id=None` is the whole library: every live copy that has a
    location, plus every copy-less live item whose seam names one. A copy or
    item with no location is expected nowhere — no shelf can hold it.
    """
    # item_copies is the source of truth for what is expected on a shelf —
    # a copy there is expected whether or not it is the primary (issue
    # #116), and a copy elsewhere is not expected here even when the
    # items.location_id seam still says it is.
    #
    # The fallback arm is for items with no copy row at all, which is a
    # real state and not only a test artefact: migration 26 and
    # backfill_legacy_locations both create copies for `owned = 1` rows
    # only, deliberately, so an upgraded database's *wishlist* item with
    # a location has none. The audit has never filtered on `owned`, so an
    # inner join alone would silently drop those rows from every audit.
    # For an item with no copies the seam is the only answer there is.
    # UNION arms convert independently; NEVER coalesce integers from the two
    # incompatible FK domains. The fallback joins by the explicit legacy map.
    if location_id is None:
        copy_on, seam, params = "c.location_id IS NOT NULL", "n.id IS NOT NULL", ()
    else:
        copy_on, seam, params = "c.location_id = ?", "n.id = ?", (location_id, location_id)
    rows = db.execute(
        "SELECT i.id AS item_id, i.title, i.authors, i.cover_path, i.media_type, "
        "       c.id AS copy_id, c.copy_number, c.copy_barcode, c.missing_since, "
        "       c.location_id AS copy_location_id, "
        "       c.location_id AS location_id, n.name AS location_name "
        "FROM copies_live c JOIN items_live i ON i.id = c.item_id "
        "JOIN location_nodes n ON n.id = c.location_id "
        f"WHERE {copy_on} UNION ALL "
        "SELECT i.id AS item_id, i.title, i.authors, i.cover_path, i.media_type, "
        "       NULL AS copy_id, NULL AS copy_number, NULL AS copy_barcode, NULL AS missing_since, "
        "       NULL AS copy_location_id, n.id AS location_id, n.name AS location_name "
        "FROM items_live i JOIN location_nodes n ON n.legacy_location_id = i.location_id "
        f"WHERE {seam} "
        "AND NOT EXISTS (SELECT 1 FROM copies_live c2 WHERE c2.item_id = i.id) "
        "ORDER BY title, copy_number",
        params,
    ).fetchall()
    result = []
    for row in rows:
        if row["copy_id"] is None and not is_physical_media(row["media_type"]):
            continue
        data = dict(row)
        data["location_node_id"] = row["location_id"]
        data["location_name"] = " / ".join(p["name"] for p in location_path(db, row["location_id"]))
        result.append(data)
    return result


def expected_for_session(db, session: dict[str, Any]) -> list[dict[str, Any]]:
    """The expected set a session reads — by `scope`, never by a NULL.

    A location audit whose location was since deleted expects nothing: its
    entries still render, but it never widens into the whole library.
    """
    if session["scope"] == "library":
        return expected_rows(db, None)
    if session["location_id"] is None:
        return []
    return expected_rows(db, session["location_id"])


def mark_selected_missing(db, session: dict[str, Any], copy_ids) -> int:
    """Mark only reported missing copies, with an all-target ACL preflight.

    Unknown IDs and authorized nonmissing IDs remain native no-ops. An EXISTING
    submitted copy whose parent is now hidden/read-only (even in Trash) refuses
    the entire selection generically BEFORE any mark, including after revocation.
    """
    from app.services import item_copies

    item_copies.write_lock(db)
    selected = set(copy_ids)
    for copy_id in sorted(selected):
        # Physical read solely for authorization: a scoped view would silently
        # hide a revoked target and partially commit the rest of the selection.
        copy = db.execute(
            "SELECT item_id FROM item_copies WHERE id = ?", (copy_id,)
        ).fetchone()
        if copy is None:
            continue
        try:
            require_item_role(db, copy["item_id"], "editor", include_deleted=True)
        except HTTPException as exc:
            if exc.status_code not in (403, 404):
                raise
            raise AuditError("Those copies cannot be updated") from exc
    targets = sorted(selected & build_report(db, session).missing_copy_ids())
    db.execute("SAVEPOINT audit_missing_marks")
    try:
        changed = sum(item_copies.mark_missing(db, copy_id) for copy_id in targets)
    except Exception:
        db.execute("ROLLBACK TO audit_missing_marks")
        db.execute("RELEASE audit_missing_marks")
        raise
    db.execute("RELEASE audit_missing_marks")
    return changed


# ── The report ──────────────────────────────────────────────────────────────

@dataclass
class Report:
    """Six sections of lines. Found, On loan and Missing count copies (a
    copy-less item counts one); Moved here, Elsewhere and Unexpected count
    lines. `scans` is every entry recorded, duplicates included."""

    session: dict[str, Any]
    found: list[dict] = field(default_factory=list)
    moved: list[dict] = field(default_factory=list)
    elsewhere: list[dict] = field(default_factory=list)
    loan: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    unexpected: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    scans: int = 0

    def missing_copy_ids(self) -> set[int]:
        return {c["copy_id"] for line in self.missing for c in line["copies"]}


def _line(row: dict, **extra) -> dict[str, Any]:
    line = {
        "item_id": row.get("item_id"),
        "title": row.get("title"),
        "authors": row.get("authors"),
        "media_type": row.get("media_type"),
        "cover_path": row.get("cover_path"),
        "location_id": row.get("location_id"),
        "location_name": row.get("location_name"),
        "copies": [],
        "count": 0,
        "deleted": False,
    }
    line.update(extra)
    return line


def _copy(row: dict) -> dict[str, Any]:
    return {
        "copy_id": row["copy_id"],
        "copy_number": row["copy_number"],
        "copy_barcode": row["copy_barcode"],
        "missing_since": row["missing_since"],
    }


def _group_rows(rows: list[dict]) -> list[dict]:
    """Expected rows → lines, one per item and location, copies listed."""
    lines: dict[tuple, dict] = {}
    for row in rows:
        key = (row["item_id"], row["location_id"])
        line = lines.get(key)
        if line is None:
            line = lines[key] = _line(row)
        line["count"] += 1
        if row["copy_id"] is not None:
            line["copies"].append(_copy(row))
    return list(lines.values())


def _entry_line(entry: dict, **extra) -> dict[str, Any]:
    """A line built from an entry — a moved, elsewhere or unexpected row."""
    values = {"count": 1, "raw_code": entry["raw_code"], "entry_id": entry["id"],
              "deleted": entry["item_id"] is None}
    values.update(extra)
    return _line({
        "item_id": entry["live_item_id"],
        "title": entry["title"],
        "authors": entry["authors"],
        "media_type": entry["media_type"],
        "cover_path": entry["cover_path"],
        "location_id": entry["location_id"],
        "location_name": entry["location_name"],
    }, **values)


def build_report(db, session: dict[str, Any]) -> Report:
    """Reconcile a session's entries against its expected set.

    Matching (impl plan decision 9):

    - an entry with `copy_specific = 1` accounts for that copy's expected row
      only, and for none at all once the copy is purged;
    - an entry with `copy_specific = 0` accounts for every expected row of its
      item **at the entry's location**;
    - `confirmed` → Found; `relocated` → Moved here, and the rows it accounts
      for leave Found and Missing;
    - `elsewhere` → Seen here, filed elsewhere, unless a later `relocated`
      entry names the same item at the same location;
    - unaccounted rows whose item is out on an open loan → On loan; the rest
      → Missing;
    - `not_owned` (grouped by code, with a scan count) and `in_trash` →
      Unexpected;
    - an entry whose item was purged still counts, as "a record since deleted".
    - a location audit whose location was deleted expects nothing, so its
      `confirmed` entries render as Found straight from the entries.
    """
    report = Report(session=session)
    expected = expected_for_session(db, session)
    orphaned = session["scope"] == "location" and session["location_id"] is None
    entries = [dict(r) for r in db.execute(
        "SELECT e.id, e.item_id, e.copy_id, e.location_id, e.raw_code, e.result, "
        "       e.security_library_id, e.security_user_id, "
        "       e.copy_specific, e.scanned_at, l.name AS location_name, "
        "       i.id AS live_item_id, i.title, i.authors, i.media_type, i.cover_path, "
        "       c.id AS live_copy_id, c.copy_number "
        "FROM audit_entries e "
        "LEFT JOIN items_live i ON i.id = e.item_id "
        "LEFT JOIN copies_live c ON c.id = e.copy_id "
        "LEFT JOIN location_nodes l ON l.id = e.location_id "
        "WHERE e.session_id = ? ORDER BY e.id",
        (session["id"],),
    ).fetchall() if _entry_visible(db, r)]
    report.scans = len(entries)
    on_loan = {r["item_id"] for r in db.execute(
        "SELECT DISTINCT item_id FROM checkouts WHERE checked_in IS NULL"
    ).fetchall()}

    # Which expected rows each kind of evidence accounts for.
    by_copy: dict[int, set[str]] = {}
    by_item_here: dict[tuple, set[str]] = {}
    for e in entries:
        if e["result"] not in ("confirmed", "relocated") or e["item_id"] is None:
            continue
        if e["copy_specific"]:
            if e["copy_id"] is not None:
                by_copy.setdefault(e["copy_id"], set()).add(e["result"])
        else:
            by_item_here.setdefault((e["item_id"], e["location_id"]), set()).add(e["result"])

    found_by_copy, found_by_item, unaccounted = [], [], []
    for row in expected:
        copy_hit = by_copy.get(row["copy_id"], set()) if row["copy_id"] is not None else set()
        item_hit = by_item_here.get((row["item_id"], row["location_id"]), set())
        if "relocated" in copy_hit or "relocated" in item_hit:
            continue  # listed under Moved here, from its entry
        if "confirmed" in copy_hit:
            found_by_copy.append(row)
        elif "confirmed" in item_hit:
            found_by_item.append(row)
        else:
            unaccounted.append(row)

    # Found: a labelled copy is its own line; an item-level confirmation is
    # one line per item and location with its count ("2 copies").
    report.found = [_line(r, copies=[_copy(r)], count=1) for r in found_by_copy]
    report.found += _group_rows(found_by_item)
    report.loan = _group_rows([r for r in unaccounted if r["item_id"] in on_loan])
    report.missing = _group_rows([r for r in unaccounted if r["item_id"] not in on_loan])

    moved: dict[tuple, dict] = {}
    elsewhere: dict[tuple, dict] = {}
    not_owned: dict[str, dict] = {}
    trashed: dict[tuple, dict] = {}
    deleted_found: dict[tuple, dict] = {}
    for e in entries:
        result = e["result"]
        if result == "relocated":
            key = (("copy", e["copy_id"]) if e["copy_specific"] and e["copy_id"] is not None
                   else ("item", e["item_id"], e["location_id"], e["raw_code"]))
            if key not in moved:
                copy_number = e["copy_number"] if e["live_copy_id"] is not None else None
                moved[key] = _entry_line(e, copy_number=copy_number)
        elif result == "elsewhere":
            later = any(
                o["result"] == "relocated" and o["id"] > e["id"]
                and o["item_id"] == e["item_id"] and o["location_id"] == e["location_id"]
                for o in entries
            ) if e["item_id"] is not None else False
            if later:
                continue
            key = (e["item_id"] if e["item_id"] is not None else e["raw_code"], e["location_id"])
            elsewhere[key] = _entry_line(e)  # the latest entry wins (resolve uses its id)
        elif result == "not_owned":
            line = not_owned.get(e["raw_code"])
            if line is None:
                not_owned[e["raw_code"]] = _entry_line(e, kind="not_owned", deleted=False)
            else:
                line["count"] += 1
        elif result == "in_trash":
            key = (e["item_id"] if e["item_id"] is not None else e["raw_code"],)
            if key not in trashed:
                trashed[key] = _entry_line(e, kind="in_trash")
        elif result == "confirmed" and e["item_id"] is None:
            # The item was purged since: it still counts, as a deleted record.
            key = (e["raw_code"], e["location_id"])
            if key not in deleted_found:
                deleted_found[key] = _entry_line(e)
        elif result == "confirmed" and orphaned:
            # The audited location was deleted, so nothing is expected and the
            # matching above found nothing — Found renders from the entry.
            key = ("item", e["item_id"], e["copy_id"] if e["copy_specific"] else None)
            if key not in deleted_found:
                copy_number = e["copy_number"] if e["live_copy_id"] is not None else None
                deleted_found[key] = _entry_line(e, copy_number=copy_number)

    report.found += list(deleted_found.values())
    report.moved = list(moved.values())
    report.elsewhere = list(elsewhere.values())
    for line in report.elsewhere:
        if line["item_id"] is not None:
            line["item_copies"] = [dict(r) for r in db.execute(
                "SELECT c.id AS copy_id, c.copy_number, c.copy_barcode, c.is_primary, "
                "       c.location_id, l.name AS location_name "
                "FROM copies_live c LEFT JOIN location_nodes l ON l.id = c.location_id "
                "WHERE c.item_id = ? ORDER BY c.copy_number, c.id",
                (line["item_id"],),
            ).fetchall()]
        else:
            line["item_copies"] = []
    report.unexpected = list(not_owned.values()) + list(trashed.values())

    report.counts = {
        "found": sum(line["count"] for line in report.found),
        "moved": len(report.moved),
        "elsewhere": len(report.elsewhere),
        "loan": sum(line["count"] for line in report.loan),
        "missing": sum(line["count"] for line in report.missing),
        "unexpected": len(report.unexpected),
    }
    return report


def summary_counts(report: Report) -> dict[str, int]:
    """The Scan-page strip's numbers — the report's counts, not a second tally."""
    counts = dict(report.counts)
    counts["scans"] = report.scans
    counts["unaccounted"] = counts["missing"] + counts["loan"]
    return counts
