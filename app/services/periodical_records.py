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
