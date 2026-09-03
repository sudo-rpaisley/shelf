"""Series completion tracking. See .devdocs/archive/completed/SERIES_TRACKING.md.

/series groups the library by series_name with local gap inference;
/api/series/check consults Hardcover for the full series so missing volumes
can be added to the wishlist via the existing add-to-shelf endpoint.
"""
import logging

from fastapi import APIRouter, Depends, Form, Request

from app.auth import require_role
from app.database import gc_orphaned_series_meta, get_db, get_setting
from app.services import hardcover

logger = logging.getLogger(__name__)

router = APIRouter()

# Longest accepted series name. Matches the CSV importer's _CSV_MAX_TEXT
# (app/routers/items.py) — the column's only other length guard — rather than
# introducing a second, different limit for the same field.
MAX_SERIES_NAME = 1000

# Media types that can belong to a series. Deliberately a local literal,
# not an import of items.BOOK_MEDIA_TYPES / synopsis.BOOK_MEDIA_TYPES (which
# already disagree about comics) — see docs: issue #31 design plan §1.
UNASSIGNED_MEDIA_TYPES = ("book", "kids_book", "audiobook", "ebook", "comic", "digital_comic")
# Covers shown in the Unassigned strip; the heading always shows the true total.
UNASSIGNED_STRIP_CAP = 12


def _sync_item_series_memberships(db) -> None:
    """Reconcile legacy primary series fields into ``item_series``.

    Migrations backfill existing libraries at startup. This lightweight
    reconciliation also covers imports/tests/older edit paths that may write
    ``items.series_name`` directly after startup, while preserving secondary
    memberships.
    """
    db.execute(
        """CREATE TABLE IF NOT EXISTS item_series (
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            series_name TEXT NOT NULL COLLATE NOCASE,
            position REAL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (item_id, series_name)
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_item_series_name "
        "ON item_series(series_name COLLATE NOCASE)"
    )
    db.execute(
        "DELETE FROM item_series WHERE is_primary = 1 AND NOT EXISTS ("
        "SELECT 1 FROM items i WHERE i.id = item_series.item_id "
        "AND i.series_name IS NOT NULL AND TRIM(i.series_name) != '' "
        "AND TRIM(i.series_name) = item_series.series_name COLLATE NOCASE"
        ")"
    )
    db.execute(
        "INSERT INTO item_series (item_id, series_name, position, is_primary) "
        "SELECT id, TRIM(series_name), series_position, 1 FROM items "
        "WHERE series_name IS NOT NULL AND TRIM(series_name) != '' "
        "ON CONFLICT(item_id, series_name) DO UPDATE SET "
        "position = excluded.position, is_primary = 1"
    )


def _promote_primary_membership(db, item_id: int) -> None:
    """Promote the oldest remaining membership and sync legacy fields."""
    replacement = db.execute(
        "SELECT series_name, position FROM item_series WHERE item_id = ? "
        "ORDER BY created_at, series_name COLLATE NOCASE LIMIT 1",
        (item_id,),
    ).fetchone()
    if replacement:
        db.execute(
            "UPDATE item_series SET is_primary = CASE "
            "WHEN series_name = ? COLLATE NOCASE THEN 1 ELSE 0 END "
            "WHERE item_id = ?",
            (replacement["series_name"], item_id),
        )
        db.execute(
            "UPDATE items SET series_name = ?, series_position = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (replacement["series_name"], replacement["position"], item_id),
        )
    else:
        db.execute(
            "UPDATE items SET series_name = NULL, series_position = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (item_id,),
        )


def find_gaps(positions: list) -> list[int]:
    """Missing integer positions between 1 and the highest whole-numbered
    position. Fractional positions (novellas: 2.5) are ignored for gap math."""
    ints = set()
    for p in positions:
        if p is None:
            continue
        try:
            f = float(p)
        except (TypeError, ValueError):
            continue
        if f.is_integer() and f >= 1:
            ints.add(int(f))
    if not ints:
        return []
    return [n for n in range(1, max(ints) + 1) if n not in ints]


@router.get("/series")
async def series_page(request: Request, _=Depends(require_role("viewer"))):
    templates = request.app.state.templates
    with get_db() as db:
        _sync_item_series_memberships(db)
        rows = db.execute(
            "SELECT i.id, i.title, i.authors, i.cover_path, "
            "s.series_name, s.position AS series_position, "
            "i.owned, i.reading_status FROM item_series s "
            "JOIN items i ON i.id = s.item_id "
            "ORDER BY s.series_name COLLATE NOCASE, "
            "s.position IS NULL, s.position, i.title COLLATE NOCASE"
        ).fetchall()
        has_hardcover = bool(get_setting(db, "hardcover_token"))
        meta_rows = {
            r["name"]: dict(r)
            for r in db.execute(
                "SELECT name, description, complete, hc_total, hc_missing, hc_checked_at "
                "FROM series_meta"
            ).fetchall()
        }
        _unassigned_where = (
            "NOT EXISTS (SELECT 1 FROM item_series s WHERE s.item_id = items.id) "
            f"AND media_type IN ({','.join('?' * len(UNASSIGNED_MEDIA_TYPES))})"
        )
        unassigned_total = db.execute(
            f"SELECT COUNT(*) FROM items WHERE {_unassigned_where}",
            UNASSIGNED_MEDIA_TYPES,
        ).fetchone()[0]
        unassigned_items = [dict(r) for r in db.execute(
            "SELECT id, title, authors, cover_path, series_name, series_position, "
            f"owned, reading_status FROM items WHERE {_unassigned_where} "
            "ORDER BY title COLLATE NOCASE LIMIT ?",
            (*UNASSIGNED_MEDIA_TYPES, UNASSIGNED_STRIP_CAP),
        ).fetchall()]

    # Group by NOCASE identity, not raw spelling: series_meta.name, the rename
    # and disband endpoints, /api/series/check and the item detail page's
    # progress line are all case-insensitive, so grouping by raw spelling here
    # was the odd one out — it split "Dune Saga" / "dune saga" into two cards
    # that every other surface treats as one series.
    series: dict[str, dict] = {}
    for r in rows:
        entry = series.setdefault(
            r["series_name"].casefold(),
            {"name": r["series_name"], "items": [], "_spellings": {}},
        )
        entry["_spellings"][r["series_name"]] = entry["_spellings"].get(r["series_name"], 0) + 1
        entry["items"].append(dict(r))

    for entry in series.values():
        # Display spelling: the most common variant, ties broken by binary sort
        # order so the choice is deterministic across runs.
        spellings = entry.pop("_spellings")
        entry["name"] = min(spellings.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    # series_meta.name is COLLATE NOCASE, so match case-insensitively here too
    meta_ci = {name.casefold(): meta for name, meta in meta_rows.items()}

    for entry in series.values():
        entry["owned_count"] = sum(1 for i in entry["items"] if i["owned"])
        entry["gaps"] = find_gaps([i["series_position"] for i in entry["items"]])
        meta = meta_ci.get(entry["name"].casefold())
        entry["description"] = meta["description"] if meta else None
        entry["complete"] = meta["complete"] if meta else None
        entry["hc_total"] = meta["hc_total"] if meta else None
        entry["hc_missing"] = meta["hc_missing"] if meta else None
        entry["hc_checked_at"] = meta["hc_checked_at"] if meta else None

    # Largest series first; ties alphabetical
    series_list = sorted(series.values(), key=lambda s: (-len(s["items"]), s["name"].casefold()))

    return templates.TemplateResponse(
        request, "series.html",
        {
            "series_list": series_list,
            "has_hardcover": has_hardcover,
            "unassigned_items": unassigned_items,
            "unassigned_total": unassigned_total,
        },
    )


@router.get("/api/series/check")
async def check_series(name: str = "", _=Depends(require_role("viewer"))):
    """Compare a local series against Hardcover's full listing."""
    name = name.strip()
    if not name:
        return {"ok": False, "message": "Series name required"}

    with get_db() as db:
        _sync_item_series_memberships(db)
        token = get_setting(db, "hardcover_token")
        if not token:
            return {"ok": False, "message": "Hardcover integration not configured"}
        local = db.execute(
            "SELECT i.title, i.owned, i.hardcover_book_id FROM item_series s "
            "JOIN items i ON i.id = s.item_id "
            "WHERE s.series_name = ? COLLATE NOCASE",
            (name,),
        ).fetchall()

    books = await hardcover.get_series_books(name, token)
    if books is None:
        return {"ok": False, "message": "Series not found on Hardcover (or lookup failed)"}

    by_hc_id = {r["hardcover_book_id"]: r for r in local if r["hardcover_book_id"]}
    by_title = {r["title"].casefold().strip(): r for r in local}

    out = []
    for b in books:
        match = by_hc_id.get(b["hardcover_book_id"]) or by_title.get(b["title"].casefold().strip())
        if match:
            status = "owned" if match["owned"] else "wishlist"
        else:
            status = "missing"
        out.append({**b, "status": status, "series_name": name})

    missing = sum(1 for b in out if b["status"] == "missing")

    # Cache the result only for a series the library actually holds. This is a
    # viewer-role GET, so without the guard any name Hardcover recognises would
    # let a viewer create series_meta rows for series that do not exist here —
    # and a check against an empty local set is meaningless anyway (every book
    # would read as "missing"). Same not-found rule the complete endpoint uses.
    if local:
        with get_db() as db:
            _upsert_series_check(db, name, len(out), missing)

    return {"ok": True, "series": name, "total": len(out), "missing": missing, "books": out}


def _upsert_series_check(db, name: str, hc_total: int, hc_missing: int) -> None:
    """Persist a Hardcover series-check result (check_series's cache fill).

    Deliberately separate from _upsert_series_description: this write only
    happens on a successful Hardcover lookup and must never disturb the
    human-authored synopsis (description/source) or the manual completeness
    override (complete) — it sets ONLY hc_total, hc_missing, hc_checked_at.
    """
    _upsert_series_check_row(db, name, hc_total, hc_missing, None)


def _upsert_series_check_row(db, name: str, hc_total, hc_missing, checked_at) -> None:
    """_upsert_series_check with an explicit check timestamp.

    `checked_at=None` stamps the write as happening now (a fresh check). A
    rename carrying an existing result across passes the original timestamp,
    so the card keeps saying when the series was really checked.
    """
    db.execute(
        "INSERT INTO series_meta (name, hc_total, hc_missing, hc_checked_at) "
        "VALUES (?, ?, ?, COALESCE(?, datetime('now'))) "
        "ON CONFLICT(name) DO UPDATE SET "
        "hc_total = excluded.hc_total, "
        "hc_missing = excluded.hc_missing, "
        "hc_checked_at = excluded.hc_checked_at",
        (name, hc_total, hc_missing, checked_at),
    )


def _gc_empty_series_meta(db, name: str) -> None:
    """Drop a series_meta row that no longer carries anything.

    Distinct from database.gc_orphaned_series_meta, which drops rows no item
    references any more. This one drops rows whose every meaningful column is
    empty — the state a cleared synopsis leaves behind on a series that was
    never marked complete and never checked against Hardcover.
    """
    db.execute(
        "DELETE FROM series_meta WHERE name = ? COLLATE NOCASE "
        "AND description IS NULL AND complete IS NULL "
        "AND hc_total IS NULL AND hc_missing IS NULL AND hc_checked_at IS NULL",
        (name,),
    )


def _upsert_series_description(db, name: str, description: str, source: str) -> None:
    """Shared upsert used by both the manual-edit and Hardcover-fetch endpoints."""
    db.execute(
        "INSERT INTO series_meta (name, description, source, updated_at) "
        "VALUES (?, ?, ?, datetime('now')) "
        "ON CONFLICT(name) DO UPDATE SET "
        "description = excluded.description, "
        "source = excluded.source, "
        "updated_at = excluded.updated_at",
        (name, description, source),
    )


@router.post("/api/series/{name:path}/description")
async def set_series_description(name: str, description: str = Form(""),
                                  _=Depends(require_role("editor"))):
    """Upsert the free-text synopsis for a series in series_meta.

    `{name:path}` (not a plain `{name}`) so series names containing a slash
    (e.g. "Foo / Bar") round-trip through the URL — a bare path param stops
    matching at the first `/`.

    An empty/whitespace description clears the synopsis rather than storing an
    empty string, and drops the row entirely once nothing else is left on it.
    This keeps the table free of empty rows and matches the orphan-GC direction
    planned for series_name changes on items (mirrors the tag GC in tags.py's
    remove_tag). The row can no longer be deleted outright: since #15 it also
    carries the completeness override and the cached Hardcover check, and
    clearing a synopsis must not silently discard those.
    """
    name = name.strip()
    if not name:
        return {"ok": False, "message": "Series name required"}

    description = (description or "").strip()

    with get_db() as db:
        if description:
            _upsert_series_description(db, name, description, "manual")
        else:
            db.execute(
                "UPDATE series_meta SET description = NULL, source = NULL, "
                "updated_at = datetime('now') WHERE name = ? COLLATE NOCASE",
                (name,),
            )
            _gc_empty_series_meta(db, name)
        return {"ok": True, "name": name, "description": description or None}


def _upsert_series_complete(db, name: str, complete: int | None) -> None:
    """Set or clear the manual completeness override.

    Independent of _upsert_series_description and _upsert_series_check —
    sets ONLY the complete column, never description/source/hc_*.
    """
    db.execute(
        "INSERT INTO series_meta (name, complete) "
        "VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET complete = excluded.complete",
        (name, complete),
    )


@router.post("/api/series/{name:path}/complete")
async def set_series_complete(name: str, complete: str = Form(...),
                              _=Depends(require_role("editor"))):
    """Set or clear the manual "series complete" override in series_meta.

    This is the top-priority signal in the three-state completeness model
    (manual override > stored Hardcover check > local gap detection):
    Hardcover's series data is often wrong or sparse (novellas, omnibuses),
    so the user must be able to declare done-ness regardless of what a check
    says.

    `{name:path}` (not a plain `{name}`) so series names containing a slash
    (e.g. "Foo / Bar") round-trip through the URL — a bare path param stops
    matching at the first `/`, same reason as the description, rename, and
    remove-all endpoints.

    Form contract: `complete=1` marks the series manually complete;
    `complete=0` clears the override back to auto (NULL, i.e. fall through
    to the stored Hardcover check or local gap detection). No other values
    are accepted.
    """
    name = name.strip()
    if not name:
        return {"ok": False, "message": "Series name required"}

    complete = (complete or "").strip()
    if complete not in ("0", "1"):
        return {"ok": False, "message": "complete must be '0' or '1'"}
    value = 1 if complete == "1" else None

    with get_db() as db:
        _sync_item_series_memberships(db)
        count = db.execute(
            "SELECT COUNT(*) AS c FROM item_series "
            "WHERE series_name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()["c"]
        if not count:
            return {"ok": False, "message": "Series not found"}

        _upsert_series_complete(db, name, value)

        return {"ok": True, "name": name, "complete": bool(value)}


@router.post("/api/series/{name:path}/rename")
async def rename_series(name: str, new_name: str = Form(""),
                        _=Depends(require_role("editor"))):
    """Move every item in a series to another series name.

    Renaming onto a name that already has books *merges* the two — the direct
    fix for the duplicate series records Hardcover produces (three "Dune").
    `series_position` is deliberately left alone: two books legitimately
    ending up at #1 is better than silently renumbering the user's data, and
    the existing gap detection surfaces the result on the merged card.

    `{name:path}` for the same slash-in-name reason as the description
    endpoints. Editor role matches update_item — an editor can already do
    this one item at a time.
    """
    name = name.strip()
    new_name = (new_name or "").strip()
    if not name:
        return {"ok": False, "message": "Series name required"}
    if not new_name:
        return {"ok": False, "message": "New series name required"}
    if len(new_name) > MAX_SERIES_NAME:
        return {"ok": False,
                "message": f"Series name too long (max {MAX_SERIES_NAME} chars)"}
    # NOCASE is how every other series lookup matches, so a case-only edit is
    # a no-op at the database level rather than a rename.
    if name.casefold() == new_name.casefold():
        return {"ok": False, "message": "That is already the series name"}

    with get_db() as db:
        _sync_item_series_memberships(db)
        source_rows = db.execute(
            "SELECT item_id, position, is_primary FROM item_series "
            "WHERE series_name = ? COLLATE NOCASE ORDER BY item_id",
            (name,),
        ).fetchall()
        count = len(source_rows)
        if not count:
            return {"ok": False, "message": "Series not found"}

        merged = bool(db.execute(
            "SELECT 1 FROM item_series WHERE series_name = ? COLLATE NOCASE LIMIT 1",
            (new_name,),
        ).fetchone())
        src_meta = db.execute(
            "SELECT description, source, complete, hc_total, hc_missing, hc_checked_at "
            "FROM series_meta WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        dst_meta = db.execute(
            "SELECT description, complete, hc_total, hc_missing, hc_checked_at "
            "FROM series_meta WHERE name = ? COLLATE NOCASE",
            (new_name,),
        ).fetchone()

        for source in source_rows:
            target = db.execute(
                "SELECT position, is_primary FROM item_series "
                "WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
                (source["item_id"], new_name),
            ).fetchone()
            if target:
                position = target["position"] if target["position"] is not None else source["position"]
                is_primary = int(bool(target["is_primary"] or source["is_primary"]))
                db.execute(
                    "UPDATE item_series SET position = ?, is_primary = ? "
                    "WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
                    (position, is_primary, source["item_id"], new_name),
                )
                db.execute(
                    "DELETE FROM item_series WHERE item_id = ? "
                    "AND series_name = ? COLLATE NOCASE",
                    (source["item_id"], name),
                )
            else:
                position = source["position"]
                is_primary = int(source["is_primary"])
                db.execute(
                    "UPDATE item_series SET series_name = ? "
                    "WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
                    (new_name, source["item_id"], name),
                )

            if is_primary:
                db.execute(
                    "UPDATE item_series SET is_primary = CASE "
                    "WHEN series_name = ? COLLATE NOCASE THEN 1 ELSE 0 END "
                    "WHERE item_id = ?",
                    (new_name, source["item_id"]),
                )
                db.execute(
                    "UPDATE items SET series_name = ?, series_position = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (new_name, position, source["item_id"]),
                )

        src_desc = (src_meta["description"] or "").strip() if src_meta else ""
        dst_desc = (dst_meta["description"] or "").strip() if dst_meta else ""
        if src_desc and not dst_desc:
            _upsert_series_description(db, new_name, src_meta["description"], src_meta["source"])

        if src_meta and src_meta["complete"] is not None and (not dst_meta or dst_meta["complete"] is None):
            _upsert_series_complete(db, new_name, src_meta["complete"])

        if src_meta and src_meta["hc_checked_at"] is not None and (not dst_meta or dst_meta["hc_checked_at"] is None):
            _upsert_series_check_row(
                db, new_name, src_meta["hc_total"], src_meta["hc_missing"], src_meta["hc_checked_at"]
            )

        gc_orphaned_series_meta(db, name)
        return {"ok": True, "name": new_name, "merged": merged, "count": count}


@router.post("/api/series/{name:path}/remove-all")
async def remove_all_from_series(name: str, _=Depends(require_role("editor"))):
    """Disband a series: clear series_name on every item that belongs to it.

    The items themselves are untouched — only the series link is dropped.
    `{name:path}` for the same slash-in-name reason as the description and
    rename endpoints; editor role matches them too.
    """
    name = name.strip()
    if not name:
        return {"ok": False, "message": "Series name required"}

    with get_db() as db:
        _sync_item_series_memberships(db)
        rows = db.execute(
            "SELECT item_id, is_primary FROM item_series "
            "WHERE series_name = ? COLLATE NOCASE",
            (name,),
        ).fetchall()
        count = len(rows)
        if not count:
            return {"ok": False, "message": "Series not found"}

        primary_item_ids = [row["item_id"] for row in rows if row["is_primary"]]
        db.execute(
            "DELETE FROM item_series WHERE series_name = ? COLLATE NOCASE",
            (name,),
        )
        for item_id in primary_item_ids:
            _promote_primary_membership(db, item_id)

        gc_orphaned_series_meta(db, name)
        return {"ok": True, "count": count}


@router.post("/api/series/{name:path}/fetch-description")
async def fetch_series_description(name: str, _=Depends(require_role("editor"))):
    """Fetch a series' synopsis from Hardcover and persist it (source='hardcover').

    Mirrors check_series's token lookup. Never writes a row on failure — a
    missing token, an unfound series, or an empty/failed lookup all just
    return {"ok": False, ...}.
    """
    name = name.strip()
    if not name:
        return {"ok": False, "message": "Series name required"}

    with get_db() as db:
        token = get_setting(db, "hardcover_token")
        if not token:
            return {"ok": False, "message": "Hardcover integration not configured"}

    description = await hardcover.get_series_description(name, token)
    if not description:
        # Not a failure: most Hardcover series carry no description at all.
        # Flagged as `empty` so the UI can say so plainly instead of showing
        # an error, which reads as "the button is broken".
        return {"ok": False, "empty": True,
                "message": "Hardcover has no synopsis for this series — add one below"}

    with get_db() as db:
        _upsert_series_description(db, name, description, "hardcover")

    return {"ok": True, "description": description}
