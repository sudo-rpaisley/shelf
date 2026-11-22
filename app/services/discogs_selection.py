"""Persist the one Discogs release explicitly selected for a music item.

MusicBrainz remains Shelf's canonical release identity in ``music_releases``.
Discogs contributes only an optional exact-pressing reference, stored in the
existing ``music_identifiers`` table rather than a second release table.

The API response itself is deliberately not persisted here.  Discogs API data
is dynamic and attribution/freshness rules apply to displayed API content; a
future UI can resolve this durable release id on demand and short-cache the
result instead.
"""

from __future__ import annotations

from app.services import music_catalog
from app.services.catalogue_security import require_item_role
from app.services.item_write import _write_scope

IDENTIFIER_TYPE = "discogs_release_id"


def _release_id(value: int | str) -> int:
    try:
        release_id = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Discogs release id must be a positive integer") from exc
    if release_id <= 0:
        raise ValueError("Discogs release id must be a positive integer")
    return release_id


def get_selected_release_id(db, item_id: int) -> int | None:
    """Read a visible, unambiguous pressing; never disclose a hidden identity."""
    if require_item_role(db, item_id, "viewer") is None:
        return None
    try:
        return music_catalog.selected_discogs_release_id(db, item_id)
    except ValueError:
        # Preserve the optional getter contract, but never select the newest
        # or lowest-ID winner from conflicting/invalid retained identities.
        return None


def _clear_cached_response(db, item_id: int, release_id: int | None) -> None:
    """Invalidate pressing-specific cache while retaining manual/canonical data."""
    db.execute(
        "UPDATE music_releases SET discogs_release_id = ?, discogs_master_id = NULL, "
        "discogs_label = NULL, discogs_catalog_number = NULL, discogs_format_summary = NULL, "
        "discogs_genres = NULL, discogs_styles = NULL, discogs_notes = NULL, discogs_updated_at = NULL "
        "WHERE item_id = ?", (release_id, item_id),
    )
    db.execute(
        "DELETE FROM music_identifiers WHERE item_id = ? AND source = 'discogs' "
        "AND identifier_type != ?", (item_id, IDENTIFIER_TYPE),
    )


def set_selected_release_id(db, item_id: int, release_id: int | str) -> int:
    """Select one exact Discogs pressing for an existing MusicBrainz release.

    A ``music_releases`` row is required: Discogs enriches an already-catalogued
    Shelf music release and must never create a second canonical release model.
    Re-selecting replaces the previous Discogs release id while leaving all
    unrelated music identifiers untouched.
    """
    with _write_scope(db):
        require_item_role(db, item_id, "editor")
        release_id = _release_id(release_id)
        existing = db.execute(
            "SELECT musicbrainz_release_id, discogs_release_id FROM music_releases WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if not existing or not existing["musicbrainz_release_id"]:
            raise ValueError("Discogs selection requires an existing music release")
        rows = db.execute(
            "SELECT id FROM music_identifiers WHERE item_id = ? AND identifier_type = ?",
            (item_id, IDENTIFIER_TYPE),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("Saved Discogs selection is ambiguous; resolve its identifiers explicitly")
        if rows:
            # Re-selection is explicit, but the existing marker's ID is still
            # retained. Never delete unrelated/manual identifiers to fit it.
            db.execute(
                "UPDATE music_identifiers SET value = ?, description = ? WHERE id = ? AND item_id = ?",
                (str(release_id), "Selected Discogs release", rows[0]["id"], item_id),
            )
        else:
            music_catalog.add_identifier(db, item_id, IDENTIFIER_TYPE, str(release_id), "Selected Discogs release")
        if existing["discogs_release_id"] != release_id:
            _clear_cached_response(db, item_id, release_id)
        return release_id


def clear_selected_release_id(db, item_id: int) -> bool:
    """Clear the pressing and its response cache under one authorized savepoint."""
    with _write_scope(db):
        require_item_role(db, item_id, "editor")
        existing = db.execute(
            "SELECT discogs_release_id FROM music_releases WHERE item_id = ?", (item_id,),
        ).fetchone()
        cursor = db.execute(
            "DELETE FROM music_identifiers WHERE item_id = ? AND identifier_type = ?",
            (item_id, IDENTIFIER_TYPE),
        )
        _clear_cached_response(db, item_id, None)
        return cursor.rowcount > 0 or bool(existing and existing["discogs_release_id"] is not None)
