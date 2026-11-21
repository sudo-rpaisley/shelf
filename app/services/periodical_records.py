"""Publication identities, concrete issues and collection summaries.

Schema and migrations belong exclusively to app.database. Publication metadata
never supplies an issue number or date unless the provider explicitly has one.
"""

from app.database import ensure_periodical_schema as ensure_extended_schema
from app.services.periodicals import normalise_issn

ensure_schema = ensure_extended_schema


def find_publication(db, *, title: str, issn: str | None = None) -> int | None:
    """Match exact ISSN, or an unambiguous compatible title (never replace ISSN)."""
    ensure_schema(db)
    title = (title or "").strip()
    issn = normalise_issn(issn)
    if issn:
        row = db.execute(
            "SELECT id FROM periodical_publications WHERE issn = ? COLLATE NOCASE",
            (issn,),
        ).fetchone()
        if row:
            return row["id"]
    rows = db.execute(
        "SELECT id FROM periodical_publications WHERE title = ? COLLATE NOCASE "
        "AND (? IS NULL OR issn IS NULL) ORDER BY id",
        (title, issn),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("Publication title is ambiguous; supply its exact ISSN")
    return rows[0]["id"] if rows else None


def upsert_publication(db, *, title: str, issn: str | None = None,
                       publisher: str | None = None, language: str | None = None) -> int:
    title = (title or "").strip()
    if not title:
        raise ValueError("Periodical title is required")
    issn = normalise_issn(issn)
    publication_id = find_publication(db, title=title, issn=issn)
    if publication_id is not None:
        db.execute(
            "UPDATE periodical_publications SET title = ?, issn = COALESCE(?, issn), "
            "publisher = COALESCE(?, publisher), language = COALESCE(?, language), "
            "updated_at = datetime('now') WHERE id = ?",
            (title, issn, publisher or None, language or None, publication_id),
        )
        return publication_id
    return db.execute(
        "INSERT INTO periodical_publications (title, issn, publisher, language) "
        "VALUES (?, ?, ?, ?)", (title, issn, publisher or None, language or None),
    ).lastrowid


def find_duplicate_issue(db, publication_id: int | None, *, volume: str | None = None,
                         issue_number: str | None = None, issue_date: str | None = None,
                         barcode_ean: str | None = None, barcode_supplement: str | None = None,
                         google_volume_id: str | None = None) -> int | None:
    """Prefer any live exact issue over Trash; refuse ambiguous/conflicting evidence.

    The unjoined fallback intentionally finds a trashed issue for an explicit
    user restore. It never resurrects an issue itself or guesses from a carrier
    without a supplement.
    """
    ensure_schema(db)
    clean = lambda v: (v or "").strip() or None
    ean, supplement = clean(barcode_ean), clean(barcode_supplement)
    volume, issue_number, issue_date = clean(volume), clean(issue_number), clean(issue_date)
    strategies = []
    if clean(google_volume_id):
        strategies.append(("pi.google_volume_id = ?", (clean(google_volume_id),)))
    if ean and supplement:
        strategies.append(("pi.barcode_ean = ? AND pi.barcode_supplement = ?", (ean, supplement)))
    if publication_id is not None and issue_number:
        strategies.append((
            "pi.publication_id = ? AND COALESCE(pi.volume, '') = COALESCE(?, '') "
            "AND pi.issue_number = ? COLLATE NOCASE", (publication_id, volume, issue_number),
        ))
    if publication_id is not None and issue_date:
        strategies.append(("pi.publication_id = ? AND pi.issue_date = ?", (publication_id, issue_date)))
    for live_only in (True, False):
        join = " JOIN items_live i ON i.id = pi.item_id" if live_only else ""
        matches = set()
        for predicate, params in strategies:
            matches.update(row["item_id"] for row in db.execute(
                f"SELECT pi.item_id FROM periodical_issues pi{join} WHERE {predicate}", params,
            ).fetchall())
        if len(matches) > 1:
            raise ValueError("Issue evidence matches more than one catalogue item")
        if matches:
            return matches.pop()
    return None


def link_issue(db, *, item_id: int, publication_id: int, volume: str | None = None,
               issue_number: str | None = None, issue_date: str | None = None,
               barcode_ean: str | None = None, barcode_supplement: str | None = None,
               cover_date_label: str | None = None, google_volume_id: str | None = None) -> None:
    ensure_schema(db)
    if not db.execute("SELECT 1 FROM items_live WHERE id = ?", (item_id,)).fetchone():
        raise ValueError("Item not found")
    if not db.execute("SELECT 1 FROM periodical_publications WHERE id = ?", (publication_id,)).fetchone():
        raise ValueError("Publication not found")
    db.execute(
        "INSERT INTO periodical_issues "
        "(item_id, publication_id, volume, issue_number, issue_date, barcode_ean, "
        "barcode_supplement, cover_date_label, google_volume_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(item_id) DO UPDATE SET publication_id = excluded.publication_id, "
        "volume = excluded.volume, issue_number = excluded.issue_number, issue_date = excluded.issue_date, "
        "barcode_ean = excluded.barcode_ean, barcode_supplement = excluded.barcode_supplement, "
        "cover_date_label = excluded.cover_date_label, "
        "google_volume_id = COALESCE(excluded.google_volume_id, periodical_issues.google_volume_id), "
        "updated_at = datetime('now')",
        (item_id, publication_id, *((value or "").strip() or None for value in (
            volume, issue_number, issue_date, barcode_ean, barcode_supplement,
            cover_date_label, google_volume_id,
        ))),
    )


def list_publications(db) -> list[dict]:
    ensure_schema(db)
    rows = db.execute(
        "SELECT pp.id, pp.title, pp.issn, pp.publisher, pp.language, "
        "COUNT(i.id) AS issue_count, SUM(CASE WHEN i.owned = 1 THEN 1 ELSE 0 END) AS owned_issue_count, "
        "SUM(CASE WHEN i.owned = 0 THEN 1 ELSE 0 END) AS wishlist_issue_count, "
        "MIN(CASE WHEN i.id IS NOT NULL THEN pi.issue_date END) AS first_issue_date, "
        "MAX(CASE WHEN i.id IS NOT NULL THEN pi.issue_date END) AS latest_issue_date, "
        "(SELECT i2.cover_path FROM periodical_issues pi2 JOIN items_live i2 ON i2.id = pi2.item_id "
        "WHERE pi2.publication_id = pp.id AND i2.cover_path IS NOT NULL AND TRIM(i2.cover_path) != '' "
        "ORDER BY pi2.issue_date IS NULL, pi2.issue_date DESC, pi2.item_id DESC LIMIT 1) AS cover_path "
        "FROM periodical_publications pp LEFT JOIN periodical_issues pi ON pi.publication_id = pp.id "
        "LEFT JOIN items_live i ON i.id = pi.item_id "
        "GROUP BY pp.id HAVING COUNT(i.id) > 0 ORDER BY pp.title COLLATE NOCASE, pp.id"
    ).fetchall()
    return [dict(row) for row in rows]


def _numeric_issue_gaps(issues: list[dict]) -> list[dict]:
    groups: dict[str, set[int]] = {}
    for issue in issues:
        raw = str(issue.get("issue_number") or "").strip()
        if raw.isdigit():
            groups.setdefault(str(issue.get("volume") or "").strip(), set()).add(int(raw))
    gaps = []
    for volume, numbers in sorted(groups.items(), key=lambda entry: entry[0].casefold()):
        low, high = min(numbers), max(numbers)
        if len(numbers) < 2 or high - low > 250:
            continue
        missing = [number for number in range(low, high + 1) if number not in numbers]
        if missing:
            gaps.append({"volume": volume or None, "start": low, "end": high, "missing": missing})
    return gaps


def issues_for_publication(db, publication_id: int) -> list[dict]:
    ensure_schema(db)
    rows = db.execute(
        "SELECT pi.*, i.title, i.cover_path, i.owned, i.publish_year, "
        "(SELECT COUNT(*) FROM copies_live ic WHERE ic.item_id = i.id) AS copy_count "
        "FROM periodical_issues pi JOIN items_live i ON i.id = pi.item_id "
        "WHERE pi.publication_id = ? "
        "ORDER BY CASE WHEN pi.issue_date IS NULL OR TRIM(pi.issue_date) = '' THEN 1 ELSE 0 END, "
        "pi.issue_date DESC, CASE WHEN pi.issue_number GLOB '[0-9]*' "
        "AND pi.issue_number NOT GLOB '*[^0-9]*' THEN CAST(pi.issue_number AS INTEGER) END DESC, "
        "pi.issue_number COLLATE NOCASE DESC, i.id DESC", (publication_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def publication_catalogue(db, publication_id: int) -> dict | None:
    ensure_schema(db)
    publication = db.execute(
        "SELECT * FROM periodical_publications WHERE id = ?", (publication_id,),
    ).fetchone()
    issues = issues_for_publication(db, publication_id)
    if publication is None or not issues:
        return None
    owned = sum(1 for issue in issues if issue["owned"])
    return {
        "publication": dict(publication), "issues": issues, "issue_count": len(issues),
        "owned_issue_count": owned, "wishlist_issue_count": len(issues) - owned,
        "physical_copy_count": sum(int(issue["copy_count"] or 0) for issue in issues),
        "extra_copy_count": sum(max(int(issue["copy_count"] or 0) - 1, 0) for issue in issues),
        "potential_gaps": _numeric_issue_gaps(issues),
    }
