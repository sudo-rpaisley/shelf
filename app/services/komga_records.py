"""The single Komga transaction adapter: inline identity, sidecar and holdings.

New imports retain Shelf's digital media types. Existing exact attachments,
including physical editions from earlier integrations, are never moved to a
different item. Plural provider records remain plural; an inline ID is only a
compatibility representative, not the complete holding set.
"""

from __future__ import annotations

from typing import Any

from app.services import holdings, isbn as isbn_svc, series_memberships
from app.services.catalogue_security import require_item_role
from app.services.item_write import IdentifierInTrash, _write_scope, insert_item, update_item_fields

KOMGA_KINDS = frozenset({"comic", "manga"})


class KomgaPersistenceError(ValueError):
    """A candidate cannot be safely represented in Shelf."""


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _canonical_isbn(value: Any) -> str | None:
    raw = _clean(value)
    pair = isbn_svc.canonical_isbn_pair(raw) if raw else None
    return pair[0] if pair else None


def _kind(candidate: dict[str, Any]) -> str:
    kind = str(candidate.get("library_kind") or "").strip().casefold()
    if kind not in KOMGA_KINDS:
        raise KomgaPersistenceError("Komga library must be explicitly classified as Comic or Manga")
    return kind


def _shelf_media_type(kind: str) -> str:
    return {"comic": "digital_comic", "manga": "digital_manga"}[kind]


def _attachment(db, komga_id: str):
    # Physical reads deliberately include Trash/hidden identities: views would
    # turn an occupied identity into a duplicate insert. Never disclose a hidden
    # match or pick a live/lowest-id winner when attachments disagree.
    rows = db.execute(
        "SELECT id FROM items WHERE komga_id = ? UNION "
        "SELECT item_id FROM komga_records WHERE komga_id = ? UNION "
        "SELECT item_id FROM digital_holdings WHERE provider = 'komga' AND external_id = ?",
        (komga_id, komga_id, komga_id),
    ).fetchall()
    if len(rows) > 1:
        raise KomgaPersistenceError("Komga identity has conflicting catalogue attachments")
    if not rows:
        return None
    item = db.execute("SELECT * FROM items WHERE id = ?", (rows[0][0],)).fetchone()
    if item is None:
        raise KomgaPersistenceError("Komga identity has an incomplete catalogue attachment")
    if item["deleted_at"] is None:
        require_item_role(db, item["id"], "editor")
    return item


def _existing_record(db, komga_id: str):
    return db.execute(
        "SELECT kr.*, i.source, i.media_type, i.deleted_at FROM komga_records kr "
        "JOIN items i ON i.id = kr.item_id WHERE kr.komga_id = ?", (komga_id,),
    ).fetchone()


def _isbn_match(db, isbn: str | None, media_type: str):
    if not isbn:
        return None
    rows = db.execute(
        "SELECT * FROM items_live WHERE isbn = ? AND media_type = ?", (isbn, media_type),
    ).fetchall()
    if len(rows) > 1:
        raise KomgaPersistenceError("Komga ISBN matches more than one edition")
    return rows[0] if rows else None


def _provider_fields(candidate: dict[str, Any], media_type: str) -> dict[str, Any]:
    return {
        "title": _clean(candidate.get("title")), "authors": _clean(candidate.get("authors")),
        "isbn": _canonical_isbn(candidate.get("isbn")),
        "series_name": _clean(candidate.get("series_name")),
        "series_position": candidate.get("series_position"),
        "publish_year": candidate.get("publish_year"),
        "description": _clean(candidate.get("description")), "page_count": candidate.get("page_count"),
        "media_type": media_type,
    }


def persist_candidate(db, candidate: dict[str, Any]) -> dict[str, Any]:
    """Atomically reconcile an exact identity; refuse ambiguity without partial writes."""
    try:
        with _write_scope(db):
            return _persist_candidate(db, candidate)
    except IdentifierInTrash as exc:
        # Update/reclassification may hit a Trash slot after staging the sidecar.
        # Translate only AFTER the adapter's savepoint has undone every write.
        # Insert retains its distinct non-restoring in_trash result below.
        raise KomgaPersistenceError("Komga update conflicts with a trashed identifier") from exc


def _upsert_record(db, komga_id, item_id, library_id, series_id, kind) -> None:
    db.execute(
        "INSERT INTO komga_records (komga_id, item_id, library_id, series_id, kind) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(komga_id) DO UPDATE SET library_id = excluded.library_id, "
        "series_id = excluded.series_id, kind = excluded.kind, updated_at = datetime('now')",
        (komga_id, item_id, library_id, series_id, kind),
    )


def _persist_candidate(db, candidate: dict[str, Any]) -> dict[str, Any]:
    komga_id = _clean(candidate.get("komga_id"))
    library_id = _clean(candidate.get("komga_library_id"))
    title = _clean(candidate.get("title"))
    if not komga_id or not library_id or not title:
        raise KomgaPersistenceError("Komga candidate is missing id, library or title")
    kind = _kind(candidate)
    media_type = _shelf_media_type(kind)
    fields = _provider_fields(candidate, media_type)
    existing = _attachment(db, komga_id)
    if existing is not None and existing["deleted_at"] is not None:
        return {"item_id": existing["id"], "action": "in_trash"}

    attached = existing is not None
    if existing is None:
        existing = _isbn_match(db, fields["isbn"], media_type)
        if existing is not None:
            require_item_role(db, existing["id"], "editor")
            # ISBN identifies an edition, not which of several provider files
            # belongs to it. Never absorb a second unconfirmed provider ID.
            claimed = db.execute(
                "SELECT 1 FROM komga_records WHERE item_id = ? UNION "
                "SELECT 1 FROM digital_holdings WHERE item_id = ? AND provider = 'komga' LIMIT 1",
                (existing["id"], existing["id"]),
            ).fetchone()
            if existing["komga_id"] or claimed:
                raise KomgaPersistenceError("Komga ISBN is already attached to another provider record")
    adopted = existing is not None and existing["source"] != "komga"
    if existing is None:
        try:
            item_id = insert_item(db, {
                **fields, "source": "komga", "owned": 1, "komga_id": komga_id,
                "komga_library_id": library_id,
                "komga_series_id": _clean(candidate.get("komga_series_id")),
            }, restore_trashed=False)
        except IdentifierInTrash as exc:
            return {"item_id": exc.item_id, "action": "in_trash"}
        action = "created"
        _upsert_record(db, komga_id, item_id, library_id, _clean(candidate.get("komga_series_id")), kind)
    else:
        item_id = existing["id"]
        desired = {}
        # Exact historical physical attachment wins. Never convert the edition
        # to digital, detach it or overwrite its title in order to migrate it.
        provider_owned = existing["source"] == "komga" and existing["media_type"] in {
            "digital_comic", "digital_manga",
        }
        if provider_owned:
            kinds = {row["kind"] for row in db.execute(
                "SELECT kind FROM komga_records WHERE item_id = ? AND komga_id != ?",
                (item_id, komga_id),
            ).fetchall()}
            if kinds and kinds != {kind}:
                raise KomgaPersistenceError("Plural Komga holdings have incompatible library classifications")
            desired = {name: value for name, value in fields.items() if value is not None}
        else:
            previous_record = _existing_record(db, komga_id)
            previous_kind = (previous_record["kind"] if previous_record is not None
                             else existing["media_type"].removeprefix("digital_"))
            if previous_kind in KOMGA_KINDS and previous_kind != kind:
                raise KomgaPersistenceError("Komga reclassification would change a manually catalogued item")
            desired = {name: value for name, value in fields.items()
                       if name not in {"title", "media_type", "isbn"}
                       and existing[name] in (None, "") and value is not None}
        other_holding = db.execute(
            "SELECT 1 FROM komga_records WHERE item_id = ? AND komga_id != ? UNION "
            "SELECT 1 FROM digital_holdings WHERE item_id = ? AND provider = 'komga' AND external_id != ? LIMIT 1",
            (item_id, komga_id, item_id, komga_id),
        ).fetchone()
        if existing["komga_id"] == komga_id or (not existing["komga_id"] and not other_holding):
            desired.update(komga_id=komga_id, komga_library_id=library_id,
                           komga_series_id=_clean(candidate.get("komga_series_id")))
        desired = {key: value for key, value in desired.items() if existing[key] != value}
        # Core updates project holdings immediately. Stage the provider-owned
        # sidecar first, so the core projection sees the SAME new scope as the
        # inline update; both remain inside this transaction's write lock.
        _upsert_record(db, komga_id, item_id, library_id, _clean(candidate.get("komga_series_id")), kind)
        if desired:
            update_item_fields(db, item_id, desired)
        action = "adopted" if not attached else "updated" if desired else "unchanged"
    series_memberships.add_metadata_memberships(db, item_id, series_memberships.metadata_memberships(candidate))
    holdings.sync_item(db, item_id)
    return {"item_id": item_id, "action": action, "adopted": adopted}


def records_for_item(db, item_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(
        "SELECT kr.komga_id, kr.library_id, kr.series_id, kr.kind FROM komga_records kr "
        "JOIN items_live i ON i.id = kr.item_id WHERE kr.item_id = ? ORDER BY kr.library_id, kr.komga_id",
        (item_id,),
    ).fetchall()]


def detach_record(db, komga_id: str) -> None:
    with _write_scope(db):
        _detach_record(db, komga_id)


def _detach_record(db, komga_id: str) -> None:
    item = _attachment(db, str(komga_id))
    if item is None:
        return
    require_item_role(db, item["id"], "editor", include_deleted=True)
    db.execute("DELETE FROM komga_records WHERE komga_id = ?", (str(komga_id),))
    if item["komga_id"] == str(komga_id):
        update_item_fields(db, item["id"], {"komga_id": None, "komga_library_id": None, "komga_series_id": None})
    db.execute("DELETE FROM digital_holdings WHERE item_id = ? AND provider = 'komga' AND external_id = ?",
               (item["id"], str(komga_id)))
    holdings.sync_item(db, item["id"])
