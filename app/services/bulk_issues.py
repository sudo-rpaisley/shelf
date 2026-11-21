"""Add ranges of physical comic or magazine issues without scanning each copy.

Bulk creation is deliberately add-only. Existing issue identities are never
changed or deleted; they are skipped, so a broad range is safe for a partially
catalogued run. Comics are scoped by Shelf series. Magazines are scoped by the
concrete publication record and, when supplied, volume.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services import item_copies, item_write, location_tree, periodical_records
from app.services.catalogue_security import require_item_role, require_library_role
from app.services.item_write import insert_item


MAX_ISSUES_PER_BATCH = 500


@dataclass(frozen=True)
class BulkIssueResult:
    created: int
    skipped: int
    first_issue: int
    last_issue: int


def _parse_positive_issue(value: int | str, label: str) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a whole number") from exc
    if number < 1:
        raise ValueError(f"{label} must be 1 or higher")
    return number


def _issue_range(first_issue: int | str, last_issue: int | str) -> tuple[int, int]:
    first = _parse_positive_issue(first_issue, "From issue")
    last = _parse_positive_issue(last_issue, "To issue")
    if first > last:
        raise ValueError("From issue must not be higher than to issue")
    if last - first + 1 > MAX_ISSUES_PER_BATCH:
        raise ValueError(f"Add at most {MAX_ISSUES_PER_BATCH} issues at a time")
    return first, last


def _numeric_issue(value) -> int | None:
    """Return a positive integer issue number, accepting harmless leading zeroes."""
    raw = str(value or "").strip()
    if not raw.isdigit():
        return None
    number = int(raw)
    return number if number >= 1 else None


def _legacy_location_for_copy(db, copy_location_id: int | None) -> int | None:
    """Translate NODE placement only through its explicit LEGACY mapping."""
    return location_tree.node_to_legacy_location(db, copy_location_id)


def _preferred_series_location(
    db, series_name: str, library_id: int
) -> tuple[int | None, int | None]:
    """Return the common precise location for owned physical copies in a comic series."""
    row = db.execute(
        "SELECT c.location_id, COUNT(*) AS copy_count, MIN(c.id) AS first_copy "
        "FROM copies_live c JOIN items_live i ON i.id = c.item_id "
        "JOIN library_items li ON li.item_id = i.id "
        "WHERE i.series_name = ? COLLATE NOCASE AND i.media_type = 'comic' "
        "AND li.library_id = ? AND i.owned = 1 AND c.location_id IS NOT NULL "
        "GROUP BY c.location_id ORDER BY copy_count DESC, first_copy ASC LIMIT 1",
        (series_name, library_id),
    ).fetchone()
    copy_location_id = row["location_id"] if row else None
    return copy_location_id, _legacy_location_for_copy(db, copy_location_id)


def _preferred_publication_location(
    db, publication_id: int, library_id: int
) -> tuple[int | None, int | None]:
    """Return the common precise location for owned copies in one publication."""
    periodical_records.ensure_extended_schema(db)
    row = db.execute(
        "SELECT c.location_id, COUNT(*) AS copy_count, MIN(c.id) AS first_copy "
        "FROM copies_live c "
        "JOIN items_live i ON i.id = c.item_id "
        "JOIN periodical_issues pi ON pi.item_id = i.id "
        "JOIN library_items li ON li.item_id = i.id "
        "WHERE pi.publication_id = ? AND li.library_id = ? "
        "AND i.media_type = 'magazine' AND i.owned = 1 "
        "AND c.location_id IS NOT NULL "
        "GROUP BY c.location_id ORDER BY copy_count DESC, first_copy ASC LIMIT 1",
        (publication_id, library_id),
    ).fetchone()
    copy_location_id = row["location_id"] if row else None
    return copy_location_id, _legacy_location_for_copy(db, copy_location_id)


def _source_library(db, rows, label: str) -> int:
    """Preflight every live visible source before deriving metadata or placement.

    Name-only range APIs cannot choose between two visible security libraries.
    Never pick the first row or silently fall back to Main Library. Hidden and
    trashed issues are not sources, nor do they contribute duplicate counts.
    Callers hold the whole-range write scope before entering this helper.
    """
    library_ids = {row["library_id"] for row in rows}
    if not rows or None in library_ids:
        raise ValueError(f"That {label} has no visible live library source")
    if len(library_ids) != 1:
        raise ValueError(f"That {label} is ambiguous across visible libraries")
    library_id = library_ids.pop()
    require_library_role(db, library_id, "editor")
    for row in rows:
        require_item_role(db, row["id"], "editor")
    return library_id


def _comic_context(db, series_name: str) -> dict:
    name = (series_name or "").strip()
    if not name:
        raise ValueError("Series name is required")
    rows = db.execute(
        "SELECT i.id, i.series_name, i.publisher, i.language, li.library_id "
        "FROM items_live i LEFT JOIN library_items li ON li.item_id = i.id "
        "WHERE i.series_name = ? COLLATE NOCASE AND i.media_type = 'comic' "
        "ORDER BY (i.publisher IS NULL), (i.language IS NULL), i.id",
        (name,),
    ).fetchall()
    if not rows:
        raise ValueError("That physical comic series does not exist")
    library_id = _source_library(db, rows, "physical comic series")
    row = rows[0]

    copy_location_id, legacy_location_id = _preferred_series_location(
        db, row["series_name"], library_id
    )
    return {
        "series_name": row["series_name"],
        "publisher": row["publisher"],
        "language": row["language"],
        "library_id": library_id,
        "location_id": legacy_location_id,
        "copy_location_id": copy_location_id,
    }


def _publication_context(db, publication_id: int) -> dict:
    periodical_records.ensure_extended_schema(db)
    row = db.execute(
        "SELECT id, title, publisher, language FROM periodical_publications WHERE id = ?",
        (publication_id,),
    ).fetchone()
    if row is None:
        raise ValueError("That magazine publication does not exist")

    sources = db.execute(
        "SELECT i.id, li.library_id FROM periodical_issues pi "
        "JOIN items_live i ON i.id = pi.item_id "
        "LEFT JOIN library_items li ON li.item_id = i.id "
        "WHERE pi.publication_id = ? AND i.media_type = 'magazine' ORDER BY i.id",
        (publication_id,),
    ).fetchall()
    library_id = _source_library(db, sources, "magazine publication")

    copy_location_id, legacy_location_id = _preferred_publication_location(
        db, publication_id, library_id
    )
    return {
        "publication_id": row["id"],
        "title": row["title"],
        "publisher": row["publisher"],
        "language": row["language"],
        "library_id": library_id,
        "location_id": legacy_location_id,
        "copy_location_id": copy_location_id,
    }


def _existing_comic_positions(db, series_name: str, library_id: int) -> set[int]:
    out: set[int] = set()
    rows = db.execute(
        "SELECT i.series_position FROM items_live i "
        "JOIN library_items li ON li.item_id = i.id "
        "WHERE i.series_name = ? COLLATE NOCASE AND i.media_type = 'comic' "
        "AND li.library_id = ? AND i.series_position IS NOT NULL",
        (series_name, library_id),
    ).fetchall()
    for row in rows:
        try:
            position = float(row["series_position"])
        except (TypeError, ValueError):
            continue
        if position.is_integer() and position >= 1:
            out.add(int(position))
    return out


def _existing_magazine_issues(
    db, publication_id: int, volume: str | None, library_id: int
) -> set[int]:
    out: set[int] = set()
    rows = db.execute(
        "SELECT pi.issue_number FROM periodical_issues pi "
        "JOIN items_live i ON i.id = pi.item_id "
        "JOIN library_items li ON li.item_id = i.id "
        "WHERE pi.publication_id = ? AND li.library_id = ? "
        "AND i.media_type = 'magazine' "
        "AND COALESCE(TRIM(pi.volume), '') = COALESCE(?, '') "
        "AND pi.issue_number IS NOT NULL",
        (publication_id, library_id, volume),
    ).fetchall()
    for row in rows:
        number = _numeric_issue(row["issue_number"])
        if number is not None:
            out.add(number)
    return out


def _set_precise_copy_location(db, item_id: int, location_id: int | None) -> None:
    location_id = location_tree.validated_node_id(db, location_id)
    if location_id is None:
        return
    primary = db.execute(
        "SELECT id FROM copies_live WHERE item_id = ? AND is_primary = 1", (item_id,)
    ).fetchone()
    if primary is not None:
        item_copies.update_copy(db, primary["id"], {"location_id": location_id})


def add_comic_issue_range(
    db,
    *,
    series_name: str,
    first_issue: int | str,
    last_issue: int | str,
) -> BulkIssueResult:
    """Add every missing integer issue position in one physical comic series."""
    first, last = _issue_range(first_issue, last_issue)
    with item_write._write_scope(db):
        context = _comic_context(db, series_name)
        canonical_name = context["series_name"]
        existing = _existing_comic_positions(db, canonical_name, context["library_id"])

        created = 0
        skipped = 0
        for number in range(first, last + 1):
            if number in existing:
                skipped += 1
                continue

            item_id = insert_item(
                db,
                library_id=context["library_id"],
                title=f"{canonical_name} #{number}",
                media_type="comic",
                publisher=context["publisher"],
                language=context["language"],
                series_name=canonical_name,
                series_position=float(number),
                location_id=context["location_id"],
                owned=1,
                source="bulk_issue_range",
            )
            _set_precise_copy_location(db, item_id, context["copy_location_id"])
            existing.add(number)
            created += 1

    return BulkIssueResult(created, skipped, first, last)


def add_magazine_issue_range(
    db,
    *,
    publication_id: int,
    first_issue: int | str,
    last_issue: int | str,
    volume: str | None = None,
) -> BulkIssueResult:
    """Add missing issue numbers to one magazine publication and volume.

    Issue-number identity follows the magazine catalogue: publication + volume
    + issue number. This allows numbering to restart in a later volume without
    incorrectly treating the new run as duplicates.
    """
    first, last = _issue_range(first_issue, last_issue)
    clean_volume = (volume or "").strip() or None
    with item_write._write_scope(db):
        context = _publication_context(db, publication_id)
        existing = _existing_magazine_issues(
            db, publication_id, clean_volume, context["library_id"]
        )

        created = 0
        skipped = 0
        for number in range(first, last + 1):
            if number in existing:
                skipped += 1
                continue

            item_id = insert_item(
                db,
                library_id=context["library_id"],
                title=context["title"],
                media_type="magazine",
                publisher=context["publisher"],
                language=context["language"],
                series_name=context["title"],
                location_id=context["location_id"],
                owned=1,
                source="bulk_issue_range",
            )
            _set_precise_copy_location(db, item_id, context["copy_location_id"])
            periodical_records.link_issue(
                db,
                item_id=item_id,
                publication_id=publication_id,
                volume=clean_volume,
                issue_number=str(number),
            )
            existing.add(number)
            created += 1

    return BulkIssueResult(created, skipped, first, last)
