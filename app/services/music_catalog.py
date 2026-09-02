"""Persistence helpers for Shelf's music-specific relational model.

The generic ``items`` row remains the owned/catalogued object.  These helpers
store the exact release metadata beneath it and automatically link other owned
formats that MusicBrainz says belong to the same release group.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def save_release(db, item_id: int, release: dict) -> None:
    """Upsert release metadata and replace its provider-owned media/track tree.

    Collector-entered condition and edition-note columns are deliberately not
    touched by the conflict update: re-enriching from MusicBrainz must never
    erase information about the user's particular copy.
    """
    db.execute(
        """
        INSERT INTO music_releases (
            item_id, artist_credit, musicbrainz_release_id,
            musicbrainz_release_group_id, release_type, release_status,
            release_date, first_release_date, country, label, catalog_number,
            packaging, media_count, format_summary, metadata_source,
            metadata_updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            artist_credit = excluded.artist_credit,
            musicbrainz_release_id = excluded.musicbrainz_release_id,
            musicbrainz_release_group_id = excluded.musicbrainz_release_group_id,
            release_type = excluded.release_type,
            release_status = excluded.release_status,
            release_date = excluded.release_date,
            first_release_date = excluded.first_release_date,
            country = excluded.country,
            label = excluded.label,
            catalog_number = excluded.catalog_number,
            packaging = excluded.packaging,
            media_count = excluded.media_count,
            format_summary = excluded.format_summary,
            metadata_source = excluded.metadata_source,
            metadata_updated_at = excluded.metadata_updated_at
        """,
        (
            item_id,
            release.get("artist_credit"),
            release.get("musicbrainz_release_id"),
            release.get("musicbrainz_release_group_id"),
            release.get("release_type"),
            release.get("release_status"),
            release.get("release_date"),
            release.get("first_release_date"),
            release.get("country"),
            release.get("label"),
            release.get("catalog_number"),
            release.get("packaging"),
            release.get("media_count"),
            release.get("format_summary"),
            release.get("source") or "musicbrainz",
            _now(),
        ),
    )

    # The media/track hierarchy is provider-owned. Replacing it atomically is
    # simpler and safer than trying to diff tracklists where provider edits can
    # insert/reorder tracks. Foreign-key cascade clears music_tracks.
    db.execute("DELETE FROM music_media WHERE item_id = ?", (item_id,))
    for medium_index, medium in enumerate(release.get("media") or [], start=1):
        cursor = db.execute(
            """
            INSERT INTO music_media (item_id, position, format, title, track_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                item_id,
                medium.get("position") or medium_index,
                medium.get("format"),
                medium.get("title"),
                medium.get("track_count"),
            ),
        )
        medium_id = cursor.lastrowid
        for track_index, track in enumerate(medium.get("tracks") or [], start=1):
            db.execute(
                """
                INSERT INTO music_tracks (
                    medium_id, position, number, title, artist_credit,
                    duration_ms, musicbrainz_recording_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    medium_id,
                    track.get("position") or track_index,
                    track.get("number"),
                    track.get("title") or "Untitled",
                    track.get("artist_credit"),
                    track.get("duration_ms"),
                    track.get("musicbrainz_recording_id"),
                ),
            )

    _link_release_group_siblings(db, item_id, release.get("musicbrainz_release_group_id"))


def _link_release_group_siblings(db, item_id: int, release_group_id: str | None) -> None:
    """Use Shelf's existing format links for editions of the same album/work."""
    if not release_group_id:
        return
    siblings = db.execute(
        "SELECT item_id FROM music_releases "
        "WHERE musicbrainz_release_group_id = ? AND item_id != ?",
        (release_group_id, item_id),
    ).fetchall()
    for row in siblings:
        a, b = sorted((item_id, row["item_id"]))
        db.execute(
            "INSERT OR IGNORE INTO item_links (item_a_id, item_b_id, link_type) "
            "VALUES (?, ?, 'format')",
            (a, b),
        )


def get_release(db, item_id: int) -> dict | None:
    """Hydrate one music release including media, tracks and identifiers."""
    row = db.execute(
        "SELECT * FROM music_releases WHERE item_id = ?", (item_id,)
    ).fetchone()
    if not row:
        return None
    release = dict(row)
    media_rows = db.execute(
        "SELECT * FROM music_media WHERE item_id = ? ORDER BY position, id",
        (item_id,),
    ).fetchall()
    media = []
    for medium_row in media_rows:
        medium = dict(medium_row)
        medium["tracks"] = [
            dict(track)
            for track in db.execute(
                "SELECT * FROM music_tracks WHERE medium_id = ? ORDER BY position, id",
                (medium_row["id"],),
            ).fetchall()
        ]
        media.append(medium)
    release["media"] = media
    release["identifiers"] = [
        dict(identifier)
        for identifier in db.execute(
            "SELECT * FROM music_identifiers WHERE item_id = ? "
            "ORDER BY identifier_type COLLATE NOCASE, value COLLATE NOCASE",
            (item_id,),
        ).fetchall()
    ]
    return release


def update_copy_details(
    db,
    item_id: int,
    *,
    edition_notes: str | None,
    media_condition: str | None,
    packaging_condition: str | None,
    condition_notes: str | None,
) -> bool:
    """Update fields describing the user's copy, not provider metadata."""
    cursor = db.execute(
        """
        UPDATE music_releases
        SET edition_notes = ?, media_condition = ?, packaging_condition = ?,
            condition_notes = ?
        WHERE item_id = ?
        """,
        (
            edition_notes or None,
            media_condition or None,
            packaging_condition or None,
            condition_notes or None,
            item_id,
        ),
    )
    return cursor.rowcount > 0


def add_identifier(
    db, item_id: int, identifier_type: str, value: str, description: str | None = None
) -> None:
    identifier_type = (identifier_type or "").strip()
    value = (value or "").strip()
    if not identifier_type or not value:
        raise ValueError("identifier type and value are required")
    db.execute(
        "INSERT OR IGNORE INTO music_identifiers "
        "(item_id, identifier_type, value, description) VALUES (?, ?, ?, ?)",
        (item_id, identifier_type, value, (description or "").strip() or None),
    )


def remove_identifier(db, item_id: int, identifier_id: int) -> bool:
    cursor = db.execute(
        "DELETE FROM music_identifiers WHERE id = ? AND item_id = ?",
        (identifier_id, item_id),
    )
    return cursor.rowcount > 0
