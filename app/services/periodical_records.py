"""Persistence helpers for periodical publications and concrete issues."""

import re

from app.services import holdings


def normalise_issn(value: str | None) -> str | None:
    raw = re.sub(r"[^0-9X]", "", (value or "").upper())
    if len(raw) != 8:
        return None
    return f"{raw[:4]}-{raw[4:]}"


def ensure_extended_schema(db) -> None:
    """Install issue identity columns added after the holdings foundation."""
    holdings.ensure_foundation(db)
    columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(periodical_issues)").fetchall()
    }
    if "barcode_ean" not in columns:
        db.execute("ALTER TABLE periodical_issues ADD COLUMN barcode_ean TEXT")
    if "google_volume_id" not in columns:
        db.execute("ALTER TABLE periodical_issues ADD COLUMN google_volume_id TEXT")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_periodical_issues_barcode "
        "ON periodical_issues(barcode_ean, barcode_supplement)"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_periodical_issues_google_volume "
        "ON periodical_issues(google_volume_id) WHERE google_volume_id IS NOT NULL"
    )


def upsert_publication(
    db,
    *,
    title: str,
    issn: str | None = None,
    publisher: str | None = None,
    language: str | None = None,
) -> int:
    ensure_extended_schema(db)
    title = (title or "").strip()
    if not title:
        raise ValueError("Magazine title is required")
    issn = normalise_issn(issn)

    row = None
    if issn:
        row = db.execute(
            "SELECT id FROM periodical_publications WHERE issn = ? COLLATE NOCASE",
            (issn,),
        ).fetchone()
    if row is None:
        row = db.execute(
            "SELECT id FROM periodical_publications WHERE title = ? COLLATE NOCASE "
            "ORDER BY id LIMIT 1",
            (title,),
        ).fetchone()

    if row:
        publication_id = row["id"]
        db.execute(
            "UPDATE periodical_publications SET "
            "title = ?, issn = COALESCE(?, issn), "
            "publisher = COALESCE(?, publisher), language = COALESCE(?, language), "
            "updated_at = datetime('now') WHERE id = ?",
            (title, issn, publisher or None, language or None, publication_id),
        )
        return publication_id

    return db.execute(
        "INSERT INTO periodical_publications (title, issn, publisher, language) "
        "VALUES (?, ?, ?, ?)",
        (title, issn, publisher or None, language or None),
    ).lastrowid


def find_duplicate_issue(
    db,
    publication_id: int,
    *,
    volume: str | None = None,
    issue_number: str | None = None,
    issue_date: str | None = None,
    google_volume_id: str | None = None,
) -> int | None:
    ensure_extended_schema(db)
    if google_volume_id:
        row = db.execute(
            "SELECT item_id FROM periodical_issues WHERE google_volume_id = ?",
            (google_volume_id,),
        ).fetchone()
        if row:
            return row["item_id"]

    issue_number = (issue_number or "").strip() or None
    volume = (volume or "").strip() or None
    issue_date = (issue_date or "").strip() or None
    if issue_number:
        row = db.execute(
            "SELECT item_id FROM periodical_issues WHERE publication_id = ? "
            "AND COALESCE(volume, '') = COALESCE(?, '') "
            "AND issue_number = ? COLLATE NOCASE LIMIT 1",
            (publication_id, volume, issue_number),
        ).fetchone()
        if row:
            return row["item_id"]
    if issue_date:
        row = db.execute(
            "SELECT item_id FROM periodical_issues WHERE publication_id = ? "
            "AND issue_date = ? LIMIT 1",
            (publication_id, issue_date),
        ).fetchone()
        if row:
            return row["item_id"]
    return None


def link_issue(
    db,
    *,
    item_id: int,
    publication_id: int,
    volume: str | None = None,
    issue_number: str | None = None,
    issue_date: str | None = None,
    barcode_ean: str | None = None,
    barcode_supplement: str | None = None,
    cover_date_label: str | None = None,
    google_volume_id: str | None = None,
) -> None:
    ensure_extended_schema(db)
    db.execute(
        "INSERT INTO periodical_issues "
        "(item_id, publication_id, volume, issue_number, issue_date, barcode_ean, "
        "barcode_supplement, cover_date_label, google_volume_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(item_id) DO UPDATE SET "
        "publication_id = excluded.publication_id, volume = excluded.volume, "
        "issue_number = excluded.issue_number, issue_date = excluded.issue_date, "
        "barcode_ean = excluded.barcode_ean, "
        "barcode_supplement = excluded.barcode_supplement, "
        "cover_date_label = excluded.cover_date_label, "
        "google_volume_id = excluded.google_volume_id, updated_at = datetime('now')",
        (
            item_id,
            publication_id,
            (volume or "").strip() or None,
            (issue_number or "").strip() or None,
            (issue_date or "").strip() or None,
            (barcode_ean or "").strip() or None,
            (barcode_supplement or "").strip() or None,
            (cover_date_label or "").strip() or None,
            (google_volume_id or "").strip() or None,
        ),
    )


def list_publications(db) -> list[dict]:
    """Return publication-level catalogue cards with collection summaries."""
    ensure_extended_schema(db)
    rows = db.execute(
        "SELECT pp.id, pp.title, pp.issn, pp.publisher, pp.language, "
        "COUNT(pi.item_id) AS issue_count, "
        "SUM(CASE WHEN i.owned = 1 THEN 1 ELSE 0 END) AS owned_issue_count, "
        "SUM(CASE WHEN i.owned = 0 THEN 1 ELSE 0 END) AS wishlist_issue_count, "
        "MIN(pi.issue_date) AS first_issue_date, MAX(pi.issue_date) AS latest_issue_date, "
        "(SELECT i2.cover_path FROM periodical_issues pi2 "
        " JOIN items i2 ON i2.id = pi2.item_id "
        " WHERE pi2.publication_id = pp.id "
        " AND i2.cover_path IS NOT NULL AND TRIM(i2.cover_path) != '' "
        " ORDER BY CASE WHEN pi2.issue_date IS NULL THEN 1 ELSE 0 END, "
        " pi2.issue_date DESC, pi2.item_id DESC LIMIT 1) AS cover_path "
        "FROM periodical_publications pp "
        "LEFT JOIN periodical_issues pi ON pi.publication_id = pp.id "
        "LEFT JOIN items i ON i.id = pi.item_id "
        "GROUP BY pp.id, pp.title, pp.issn, pp.publisher, pp.language "
        "ORDER BY pp.title COLLATE NOCASE, pp.id"
    ).fetchall()
    return [dict(row) for row in rows]


def _numeric_issue_gaps(issues: list[dict]) -> list[dict]:
    """Find conservative internal gaps in numeric issue-number runs.

    Gaps are calculated only between the lowest and highest issue numbers we
    actually hold/track, and separately for each volume. Large spans are
    ignored because publisher renumbering would otherwise create noisy lists.
    """
    groups: dict[str, set[int]] = {}
    for issue in issues:
        raw = str(issue.get("issue_number") or "").strip()
        if not raw.isdigit():
            continue
        volume = str(issue.get("volume") or "").strip()
        groups.setdefault(volume, set()).add(int(raw))

    gaps = []
    for volume, numbers in sorted(groups.items(), key=lambda entry: entry[0].casefold()):
        if len(numbers) < 2:
            continue
        low, high = min(numbers), max(numbers)
        if high - low > 250:
            continue
        missing = [number for number in range(low, high + 1) if number not in numbers]
        if missing:
            gaps.append(
                {
                    "volume": volume or None,
                    "start": low,
                    "end": high,
                    "missing": missing,
                }
            )
    return gaps


def publication_catalogue(db, publication_id: int) -> dict | None:
    """Return one publication and its concrete issues in newest-first order."""
    ensure_extended_schema(db)
    publication = db.execute(
        "SELECT id, title, issn, publisher, language, created_at, updated_at "
        "FROM periodical_publications WHERE id = ?",
        (publication_id,),
    ).fetchone()
    if publication is None:
        return None

    rows = db.execute(
        "SELECT pi.item_id, pi.volume, pi.issue_number, pi.issue_date, "
        "pi.barcode_ean, pi.barcode_supplement, pi.cover_date_label, "
        "pi.google_volume_id, i.title, i.cover_path, i.owned, i.publish_year, "
        "(SELECT COUNT(*) FROM item_copies ic WHERE ic.item_id = i.id) AS copy_count "
        "FROM periodical_issues pi JOIN items i ON i.id = pi.item_id "
        "WHERE pi.publication_id = ? "
        "ORDER BY CASE WHEN pi.issue_date IS NULL OR TRIM(pi.issue_date) = '' "
        "THEN 1 ELSE 0 END, pi.issue_date DESC, "
        "CASE WHEN pi.issue_number GLOB '[0-9]*' "
        "AND pi.issue_number NOT GLOB '*[^0-9]*' "
        "THEN CAST(pi.issue_number AS INTEGER) END DESC, "
        "pi.issue_number COLLATE NOCASE DESC, i.id DESC",
        (publication_id,),
    ).fetchall()
    issues = [dict(row) for row in rows]

    owned_issue_count = sum(1 for issue in issues if issue["owned"])
    wishlist_issue_count = len(issues) - owned_issue_count
    physical_copy_count = sum(int(issue["copy_count"] or 0) for issue in issues)
    extra_copy_count = sum(max(int(issue["copy_count"] or 0) - 1, 0) for issue in issues)

    return {
        "publication": dict(publication),
        "issues": issues,
        "issue_count": len(issues),
        "owned_issue_count": owned_issue_count,
        "wishlist_issue_count": wishlist_issue_count,
        "physical_copy_count": physical_copy_count,
        "extra_copy_count": extra_copy_count,
        "potential_gaps": _numeric_issue_gaps(issues),
    }
