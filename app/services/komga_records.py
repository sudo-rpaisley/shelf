"""Persist Komga availability without making "digital" a content type.

Shelf's catalogue item describes the work/edition. Komga is a digital holding
of that item, while physical copies are represented separately by the physical
copy model. Komga's Comic/Manga library classification is retained on the
provider record; when Shelf has no first-class Manga media type, Manga maps to
Comic rather than making an integration silently extend the global media-type
contract.

The table is local to this validation slice so it does not consume an upstream
migration number while other schema proposals are in flight. A final upstream
PR can move the accepted shape into the normal migration sequence.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.config import MEDIA_TYPES
from app.services import isbn as isbn_svc
from app.services import metadata_policy
from app.services.item_write import IdentifierInTrash, insert_item, update_item_fields

KOMGA_KINDS = frozenset({"comic", "manga"})


class KomgaPersistenceError(ValueError):
    """A candidate cannot be safely represented in Shelf."""


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _canonical_isbn(value: Any) -> str | None:
    """Return provider ISBN as canonical ISBN-13, dropping malformed metadata."""
    raw = _clean(value)
    if raw is None:
        return None
    pair = isbn_svc.canonical_isbn_pair(raw)
    return pair[0] if pair is not None else None


def _kind(candidate: dict[str, Any]) -> str:
    kind = str(candidate.get("library_kind") or "").strip().casefold()
    if kind not in KOMGA_KINDS:
        raise KomgaPersistenceError("Komga library must be classified as Comic or Manga")
    return kind


def _shelf_media_type(kind: str) -> str:
    """Project a Komga library kind onto Shelf's current media-type contract.

    Manga is an integration-level library classification whether or not Shelf
    has a first-class Manga type. If that product decision lands separately,
    the mapping starts using it automatically; otherwise Manga remains a Comic
    catalogue item while the provider record keeps ``kind='manga'``.
    """
    if kind == "manga" and "manga" in MEDIA_TYPES:
        return "manga"
    return "comic"


def _existing_record(db, komga_id: str):
    """The record for `komga_id`, whether or not its item is in Trash.

    The join is part of the read (G107): joining `items_live` is exactly what
    would hide a trashed item's record, and `komga_id` is the PRIMARY KEY of
    `komga_records` — so a hidden record means the caller inserts the item
    again, the record INSERT trips the PK, and `get_db()` rolls the whole
    block back. Physical, carrying `deleted_at`, so the caller can skip it.

    No live-first ordering is needed: `komga_id` is a primary key, so there
    is at most one record and no live twin for a trashed one to shadow.
    """
    return db.execute(
        "SELECT kr.*, i.source, i.media_type, i.deleted_at FROM komga_records kr "
        "JOIN items i ON i.id = kr.item_id WHERE kr.komga_id = ?",
        (komga_id,),
    ).fetchone()


def _isbn_match(db, isbn: str | None, media_type: str):
    if not isbn:
        return None
    return db.execute(
        "SELECT * FROM items_live WHERE isbn = ? AND media_type = ? ORDER BY id LIMIT 1",
        (isbn, media_type),
    ).fetchone()


def _provider_fields(candidate: dict[str, Any], media_type: str) -> dict[str, Any]:
    return {
        "title": _clean(candidate.get("title")),
        "authors": _clean(candidate.get("authors")),
        "isbn": _canonical_isbn(candidate.get("isbn")),
        "series_name": _clean(candidate.get("series_name")),
        "series_position": candidate.get("series_position"),
        "publish_year": candidate.get("publish_year"),
        "description": _clean(candidate.get("description")),
        "page_count": candidate.get("page_count"),
        "media_type": media_type,
    }


def _fill_missing_fields(db, item_id: int, candidate: dict[str, Any]) -> None:
    """Keep the adopted-manual-item contract, without claiming its metadata."""
    source_key = f"komga:{_clean(candidate.get('komga_id'))}"
    observed = _provider_fields(candidate, _shelf_media_type(_kind(candidate)))
    fields = {name: observed[name] for name in (
        "title",
        "authors",
        "series_name",
        "series_position",
        "publish_year",
        "description",
        "page_count",
    )}
    updates = metadata_policy.metadata_updates(
        db, item_id, source_key, fields, fill_only=True,
    )
    if updates:
        update_item_fields(db, item_id, updates)
    metadata_policy.record_observation(
        db, item_id, source_key, observed, applied_fields=updates,
    )


def _check_reclassification(existing, media_type: str) -> None:
    """A library-kind change cannot reclassify an adopted manual item.

    A Comic↔Manga change may only require changing the provider record, when
    both kinds project onto the same Shelf media type. Since #105 added
    ``manga`` to MEDIA_TYPES they usually do not, so the Shelf media type
    may change too. Validate the manual-item guard here, but leave the actual
    type AND identifiers to one safely planned final write, never a temporary
    type-only identity that may collide with an unrelated edition.
    """
    if existing["media_type"] != media_type and existing["source"] != "komga":
        raise KomgaPersistenceError(
            "This Komga holding is attached to a manually catalogued item; "
            "change its Comic/Manga type explicitly in Shelf before re-syncing"
        )


def persist_candidate(db, candidate: dict[str, Any]) -> dict[str, Any]:
    """Create/update one Komga holding and return the persistence decision.

    Identity rules are intentionally conservative:

    * an existing ``komga_id`` always updates the item it already owns;
    * otherwise an exact ISBN match of the same Shelf media type is adopted;
    * title/series similarity is never enough to adopt an existing item;
    * a new item is created when no strong identifier exists.

    When adopting a manual/physical catalogue item, Komga fills only blank
    descriptive fields and never changes that row's source. When updating a
    row originally created by Komga, only metadata still equal to that
    holding's last applied value may be refreshed. Legacy baselines are unknown.
    """
    komga_id = _clean(candidate.get("komga_id"))
    library_id = _clean(candidate.get("komga_library_id"))
    title = _clean(candidate.get("title"))
    if not komga_id or not library_id or not title:
        raise KomgaPersistenceError("Komga candidate is missing id, library or title")

    kind = _kind(candidate)
    media_type = _shelf_media_type(kind)
    fields = _provider_fields(candidate, media_type)
    isbn = fields["isbn"]
    source_key = f"komga:{komga_id}"

    metadata_policy.begin_write(db)
    existing = _existing_record(db, komga_id)
    if existing is not None and existing["deleted_at"] is not None:
        return {"item_id": existing["item_id"], "action": "in_trash"}

    if existing is not None:
        _check_reclassification(existing, media_type)
        item_id = existing["item_id"]
        if existing["source"] == "komga":
            updates = metadata_policy.metadata_updates(db, item_id, source_key, fields)
            try:
                if updates:
                    update_item_fields(db, item_id, updates)
            except IdentifierInTrash as exc:
                if "media_type" in updates:
                    raise KomgaPersistenceError(
                        "Changing this Komga library between Comic and Manga would collide "
                        "with an existing Shelf edition"
                    ) from exc
                raise KomgaPersistenceError(
                    "Refreshing this Komga holding would give it an ISBN an "
                    "item in Trash already holds"
                ) from exc
            except sqlite3.IntegrityError as exc:
                raise KomgaPersistenceError(
                    "Refreshing this Komga holding would collide with an existing Shelf edition"
                ) from exc
            metadata_policy.record_observation(db, item_id, source_key, fields, applied_fields=updates)
        else:
            _fill_missing_fields(db, item_id, candidate)
        db.execute(
            "UPDATE komga_records SET library_id = ?, series_id = ?, kind = ?, "
            "updated_at = datetime('now') WHERE komga_id = ?",
            (
                library_id,
                _clean(candidate.get("komga_series_id")),
                kind,
                komga_id,
            ),
        )
        return {
            "item_id": item_id,
            "action": "updated",
            "adopted": existing["source"] != "komga",
        }

    match = _isbn_match(db, isbn, media_type)
    adopted = match is not None
    if match is not None:
        item_id = match["id"]
        _fill_missing_fields(db, item_id, candidate)
    else:
        try:
            item_id = insert_item(
                db,
                {
                    **fields,
                    "source": "komga",
                    "owned": 1,
                },
                restore_trashed=False,
            )
        except IdentifierInTrash as exc:
            # A trashed ISBN twin with no Komga record of its own. Refused
            # before any write, and reported with the blocking row's id so
            # the sync loop's item_id read succeeds (claude-R3).
            return {"item_id": exc.item_id, "action": "in_trash"}
        except sqlite3.IntegrityError:
            # A concurrent/external writer may have created the same exact
            # edition after our lookup. Never fall back to a title guess.
            match = _isbn_match(db, isbn, media_type)
            if match is None:
                raise
            item_id = match["id"]
            adopted = True
            _fill_missing_fields(db, item_id, candidate)

    if not adopted:
        metadata_policy.record_initial(db, item_id, source_key, fields)
    db.execute(
        "INSERT INTO komga_records (komga_id, item_id, library_id, series_id, kind) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            komga_id,
            item_id,
            library_id,
            _clean(candidate.get("komga_series_id")),
            kind,
        ),
    )
    return {
        "item_id": item_id,
        "action": "adopted" if adopted else "created",
        "adopted": adopted,
    }


def records_for_item(db, item_id: int) -> list[dict[str, Any]]:
    """Return every Komga holding attached to an item."""
    rows = db.execute(
        "SELECT komga_id, library_id, series_id, kind FROM komga_records "
        "WHERE item_id = ? ORDER BY library_id, komga_id",
        (item_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def detach_record(db, komga_id: str) -> None:
    """Forget a Komga holding without deleting the catalogue item itself."""
    db.execute("DELETE FROM komga_records WHERE komga_id = ?", (str(komga_id),))
