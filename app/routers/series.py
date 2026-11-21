"""Series completion tracking. See .devdocs/archive/completed/SERIES_TRACKING.md.

/series groups the library by series_name with local gap inference;
/api/series/check consults Hardcover for the full series so missing volumes
can be added to the wishlist via the existing add-to-shelf endpoint.
"""
import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response

from app.features import feature_enabled, require_feature
from app.config import BOOK_MEDIA_TYPES
from app.database import gc_orphaned_series_meta, get_db, get_setting
from app.routers import items_common
from app.services import lists
from app.services import buy_links, hardcover
from app.services.item_write import update_item_fields
from app.database import ensure_series_memberships_schema
from app.services.catalogue_security import require_item_role, require_catalogue_role as require_role

logger = logging.getLogger(__name__)

router = APIRouter()

# Longest accepted series name. Matches the CSV importer's _CSV_MAX_TEXT
# (app/routers/items.py) — the column's only other length guard — rather than
# introducing a second, different limit for the same field.
MAX_SERIES_NAME = 1000

# Media types that can belong to a series. Keep this derived from the canonical
# config declaration so new book-family types cannot silently disappear here.
UNASSIGNED_MEDIA_TYPES = tuple(sorted(BOOK_MEDIA_TYPES))
# Covers shown in the Unassigned strip; the heading always shows the true total.
UNASSIGNED_STRIP_CAP = 12


_sync_item_series_memberships = ensure_series_memberships_schema

# Read-only compatibility projection. Old primary-only rows remain usable even
# when no membership was materialized; GETs must never backfill shared data.
LIVE_SERIES_MEMBERSHIPS = """(
    SELECT s.item_id, s.series_name, s.position, s.is_primary
    FROM item_series s JOIN items_live member ON member.id = s.item_id
    UNION ALL
    SELECT member.id, TRIM(member.series_name), member.series_position, 1
    FROM items_live member
    WHERE TRIM(COALESCE(member.series_name, '')) != ''
      AND NOT EXISTS (SELECT 1 FROM item_series existing
                      WHERE existing.item_id = member.id
                        AND existing.series_name = TRIM(member.series_name) COLLATE NOCASE)
)"""

# Only for a non-disclosing ordering/collision census. Never render these rows
# before individual visibility/liveness checks (Continue stops at a blocker).
PHYSICAL_SERIES_MEMBERSHIPS = LIVE_SERIES_MEMBERSHIPS.replace("items_live", "items")


def _require_series_role(db, name, role="editor"):
    """Shared series metadata cannot be used to edit a hidden library by name.

    The complete source set is authorized under the same lock; a name spanning
    a hidden/read-only library refuses atomically rather than partly renaming.
    """
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    else:
        # Upgrade a caller's deferred transaction before the authority census.
        db.execute("UPDATE series_meta SET name = name WHERE 0")
    ensure_series_memberships_schema(db)
    # Deliberate physical census: hidden and trashed members must prevent a
    # partial shared-name edit. The primary legacy seam is checked as well so
    # a stale/missing membership cannot become an authorization bypass.
    rows = db.execute(
        "SELECT i.id, i.deleted_at FROM items i WHERE TRIM(i.series_name) = ? COLLATE NOCASE "
        "OR EXISTS (SELECT 1 FROM item_series s WHERE s.item_id = i.id "
        "AND s.series_name = ? COLLATE NOCASE) ORDER BY i.id", (name, name),
    ).fetchall()
    for row in rows:
        require_item_role(db, row["id"], role, include_deleted=True)
    if any(row["deleted_at"] is not None for row in rows):
        raise HTTPException(409, "Series has unavailable members")
    return [row["id"] for row in rows]


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
        update_item_fields(db, item_id, {"series_name": replacement["series_name"], "series_position": replacement["position"]})
    else:
        update_item_fields(db, item_id, {"series_name": None, "series_position": None})


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
async def series_page(request: Request, _=Depends(require_role("viewer")),
                      __=Depends(require_feature("series"))):
    templates = request.app.state.templates
    with get_db() as db:
        _sync_item_series_memberships(db)
        rows = db.execute(
            "SELECT i.id, i.title, i.authors, i.cover_path, "
            "s.series_name, s.position AS series_position, "
            f"i.owned, i.reading_status, {lists.WISHLISTED_SQL} AS wishlisted FROM {LIVE_SERIES_MEMBERSHIPS} s "
            "JOIN items_live i ON i.id = s.item_id "
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
            "TRIM(COALESCE(i.series_name, '')) = '' "
            "AND NOT EXISTS (SELECT 1 FROM item_series s WHERE s.item_id = i.id) "
            f"AND media_type IN ({','.join('?' * len(UNASSIGNED_MEDIA_TYPES))})"
        )
        watches = {
            r["series_name"].casefold(): r["check_state"]
            for r in db.execute("SELECT series_name, check_state FROM release_watches").fetchall()
        }
        unassigned_total = db.execute(
            f"SELECT COUNT(*) FROM items_live i WHERE {_unassigned_where}",
            UNASSIGNED_MEDIA_TYPES,
        ).fetchone()[0]
        unassigned_items = [dict(r) for r in db.execute(
            "SELECT id, title, authors, cover_path, series_name, series_position, "
            f"owned, reading_status, {lists.WISHLISTED_SQL} AS wishlisted "
            f"FROM items_live i WHERE {_unassigned_where} "
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
        entry["wishlist_count"] = sum(1 for i in entry["items"] if i["wishlisted"])
        entry["gaps"] = find_gaps([i["series_position"] for i in entry["items"]])
        meta = meta_ci.get(entry["name"].casefold())
        entry["description"] = meta["description"] if meta else None
        entry["complete"] = meta["complete"] if meta else None
        entry["hc_total"] = meta["hc_total"] if meta else None
        entry["hc_missing"] = meta["hc_missing"] if meta else None
        entry["hc_checked_at"] = meta["hc_checked_at"] if meta else None
        key = entry["name"].casefold()
        entry["watched"] = key in watches
        entry["watch_state"] = watches.get(key) or ""

    # Largest series first; ties alphabetical
    series_list = sorted(series.values(), key=lambda s: (-len(s["items"]), s["name"].casefold()))

    return templates.TemplateResponse(
        request, "series.html",
        {
            "series_list": series_list,
            "has_hardcover": has_hardcover,
            # The radar needs both flags; every watch control hides otherwise.
            "radar_on": feature_enabled("release_radar") and feature_enabled("hardcover"),
            "unassigned_items": unassigned_items,
            "unassigned_total": unassigned_total,
        },
    )


@router.get("/api/series/check")
async def check_series(name: str = "", _=Depends(require_role("viewer")),
                       __=Depends(require_feature("series"))):
    """Compare a local series against Hardcover's full listing."""
    name = name.strip()
    if not name:
        return {"ok": False, "message": "Series name required"}

    with get_db() as db:
        _sync_item_series_memberships(db)
        authorized_ids = _require_series_role(db, name, "viewer")
        token = get_setting(db, "hardcover_token")
        if not token:
            return {"ok": False, "message": "Hardcover integration not configured"}
        on = feature_enabled("buy_links")
        cfg = buy_links.configured(db) if on else None
        local = db.execute(
            f"SELECT i.id, i.title, i.owned, i.hardcover_book_id, {lists.WISHLISTED_SQL} AS wishlisted FROM {LIVE_SERIES_MEMBERSHIPS} s "
            "JOIN items_live i ON i.id = s.item_id "
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
            status = "owned" if match["owned"] else "wishlist" if match["wishlisted"] else "missing"
        else:
            status = "missing"
        row = {**b, "status": status, "series_name": name}
        if status == "missing":
            row["buy_links"] = [
                asdict(l) for l in buy_links.for_book(
                    cfg, title=b["title"], authors=b.get("authors") or "", isbn13=None)
            ] if cfg else []
        out.append(row)

    missing = sum(1 for b in out if b["status"] == "missing")

    # A remote-only lookup can classify every volume as missing, but this
    # viewer-role GET may cache results only for a series held locally. The
    # physical membership census above still refuses hidden/unavailable names
    # before the provider call, and cache writes reauthorize after that call.
    if local:
        with get_db() as db:
            if _require_series_role(db, name, "viewer") != authorized_ids:
                return {"ok": False, "message": "Series changed; check again"}
            _upsert_series_check(db, name, len(out), missing)

    # The CSP build refuses arrow functions, so the template cannot ask
    # "does any row carry a Bookshop link"; the server answers it here.
    links = [l for b in out for l in b.get("buy_links", ())]
    disclosure = {"bookshop": any(l["store"] == "bookshop" for l in links)} if links else None

    return {"ok": True, "series": name, "total": len(out), "missing": missing,
            "books": out, "buy_links_disclosure": disclosure}


@router.post("/api/series/watch-all")
async def watch_all_complete_series(_=Depends(require_role("editor")),
                                    __=Depends(require_feature("series")),
                                    ___=Depends(require_feature("release_radar")),
                                    ____=Depends(require_feature("hardcover"))):
    """Watch every series whose last Hardcover check found nothing missing.

    Declared above every `{name:path}` route (G119). `hc_missing = 0` is the
    only stored Hardcover fact about completeness; a manual "complete" flag
    does not count. Called by HTMX only: an empty body (the button carries
    `hx-swap="none"`, G54), a toast, and a refresh when anything was added.
    """
    candidates = (
        "FROM series_meta m WHERE m.hc_missing = 0 "
        f"AND EXISTS (SELECT 1 FROM {LIVE_SERIES_MEMBERSHIPS} s "
        "WHERE s.series_name = m.name COLLATE NOCASE)"
    )
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        names = [row[0] for row in db.execute("SELECT m.name " + candidates)]
        for name in names:
            _require_series_role(db, name)
        eligible = len(names)
        added = sum(db.execute(
            "INSERT OR IGNORE INTO release_watches (series_name) VALUES (?)", (name,)
        ).rowcount for name in names)
    if not added:
        # rowcount 0 means either nothing qualifies or everything that does is
        # already watched; the second must not send the user off to re-check.
        message = (f"All {eligible} complete series are already watched" if eligible
                   else "No checked, complete series to add — run Check completeness first")
        return Response(status_code=200, headers={"HX-Trigger": items_common._toast_header(
            message, "info")})
    return Response(status_code=200, headers={
        "HX-Trigger": items_common._toast_header(
            f"Watching {added} more series"),
        "HX-Refresh": "true",
    })


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
                                  _=Depends(require_role("editor")),
                                  __=Depends(require_feature("series"))):
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
        _require_series_role(db, name)
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
                              _=Depends(require_role("editor")),
                              __=Depends(require_feature("series"))):
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
        count = len(_require_series_role(db, name))
        if not count:
            return {"ok": False, "message": "Series not found"}

        _upsert_series_complete(db, name, value)

        return {"ok": True, "name": name, "complete": bool(value)}


@router.post("/api/series/{name:path}/watch")
async def set_series_watch(request: Request, name: str, watch: str = Form(...),
                           _=Depends(require_role("editor")),
                           __=Depends(require_feature("series")),
                           ___=Depends(require_feature("release_radar")),
                           ____=Depends(require_feature("hardcover"))):
    """Watch (`watch=1`) or stop watching (`watch=0`) a series for new volumes.

    JSON for the Series card's `fetch`. An HTMX request (the `/upcoming`
    Stop watching button) gets an empty body (`hx-swap="none"` on the button,
    G54), a toast, and a refresh. Stopping a watch deletes its announced rows
    with it (ON DELETE CASCADE).
    """
    is_htmx = request.headers.get("HX-Request") == "true"
    name = name.strip()
    watch = (watch or "").strip()

    def refuse(message):
        if is_htmx:
            return Response(status_code=200, headers={
                "HX-Trigger": items_common._toast_header(message, "error")})
        return {"ok": False, "message": message}

    if not name:
        return refuse("Series name required")
    if watch not in ("0", "1"):
        return refuse("watch must be '0' or '1'")

    with get_db() as db:
        ids = _require_series_role(db, name)
        if watch == "1":
            if not ids:
                return refuse("Series not found")
            db.execute("INSERT OR IGNORE INTO release_watches (series_name) VALUES (?)", (name,))
        else:
            # No live-series guard: an item edit can empty a watched series,
            # and /upcoming still shows its rows, so Stop watching must work.
            db.execute("DELETE FROM release_watches WHERE series_name = ? COLLATE NOCASE", (name,))

    watched = watch == "1"
    if is_htmx:
        message = f"Watching {name}" if watched else f"Stopped watching {name}"
        return Response(status_code=200, headers={
            "HX-Trigger": items_common._toast_header(message),
            "HX-Refresh": "true",
        })
    return {"ok": True, "watched": watched}


@router.post("/api/series/{name:path}/rename")
async def rename_series(name: str, new_name: str = Form(""),
                        _=Depends(require_role("editor")),
                        __=Depends(require_feature("series"))):
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
        _require_series_role(db, name)
        _require_series_role(db, new_name)
        source_rows = db.execute(
            f"SELECT s.item_id, s.position, s.is_primary FROM {LIVE_SERIES_MEMBERSHIPS} s "
            "WHERE s.series_name = ? COLLATE NOCASE ORDER BY s.item_id",
            (name,),
        ).fetchall()
        count = len(source_rows)
        if not count:
            return {"ok": False, "message": "Series not found"}

        # Validate all overlaps before the first rename, not midway through it.
        for source in source_rows:
            target = db.execute(
                f"SELECT position FROM {LIVE_SERIES_MEMBERSHIPS} WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
                (source["item_id"], new_name),
            ).fetchone()
            if target and target["position"] is not None and source["position"] is not None and target["position"] != source["position"]:
                raise HTTPException(409, "Series memberships conflict")

        merged = bool(db.execute(
            f"SELECT 1 FROM {LIVE_SERIES_MEMBERSHIPS} WHERE series_name = ? COLLATE NOCASE LIMIT 1",
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
                f"SELECT position, is_primary FROM {LIVE_SERIES_MEMBERSHIPS} "
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
                    "INSERT INTO item_series (item_id, series_name, position, is_primary) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(item_id, series_name) DO UPDATE SET "
                    "series_name = excluded.series_name",
                    (source["item_id"], new_name, position, is_primary),
                )
                db.execute(
                    "DELETE FROM item_series WHERE item_id = ? AND series_name = ? COLLATE NOCASE",
                    (source["item_id"], name),
                )

            if is_primary:
                db.execute(
                    "UPDATE item_series SET is_primary = CASE "
                    "WHEN series_name = ? COLLATE NOCASE THEN 1 ELSE 0 END "
                    "WHERE item_id = ?",
                    (new_name, source["item_id"]),
                )
                update_item_fields(db, source["item_id"], {"series_name": new_name, "series_position": position})

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

        # The watch follows the series. Onto an already-watched name the
        # UPDATE is a no-op and the DELETE drops the source watch (its rows
        # cascade), so the target's snapshot survives.
        db.execute(
            "UPDATE OR IGNORE release_watches SET series_name = ? "
            "WHERE series_name = ? COLLATE NOCASE",
            (new_name, name),
        )
        db.execute("DELETE FROM release_watches WHERE series_name = ? COLLATE NOCASE", (name,))

        gc_orphaned_series_meta(db, name)
        return {"ok": True, "name": new_name, "merged": merged, "count": count}


@router.post("/api/series/{name:path}/remove-all")
async def remove_all_from_series(name: str, _=Depends(require_role("editor")),
                                 __=Depends(require_feature("series"))):
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
        _require_series_role(db, name)
        rows = db.execute(
            f"SELECT item_id, is_primary FROM {LIVE_SERIES_MEMBERSHIPS} "
            "WHERE series_name = ? COLLATE NOCASE",
            (name,),
        ).fetchall()
        count = len(rows)
        if not count:
            return {"ok": False, "message": "Series not found"}

        primary_item_ids = [row["item_id"] for row in rows if row["is_primary"]]
        for item_id in primary_item_ids:
            replacement = db.execute(
                "SELECT series_name FROM item_series WHERE item_id = ? "
                "AND series_name != ? COLLATE NOCASE "
                "ORDER BY created_at, series_name COLLATE NOCASE LIMIT 1", (item_id, name),
            ).fetchone()
            if replacement:
                _require_series_role(db, replacement["series_name"])
        db.execute(
            "DELETE FROM item_series WHERE series_name = ? COLLATE NOCASE",
            (name,),
        )
        for item_id in primary_item_ids:
            _promote_primary_membership(db, item_id)
        db.execute("DELETE FROM release_watches WHERE series_name = ? COLLATE NOCASE", (name,))

        gc_orphaned_series_meta(db, name)
        return {"ok": True, "count": count}


@router.post("/api/series/{name:path}/fetch-description")
async def fetch_series_description(name: str, _=Depends(require_role("editor")),
                                   __=Depends(require_feature("series"))):
    """Fetch a series' synopsis from Hardcover and persist it (source='hardcover').

    Mirrors check_series's token lookup. Never writes a row on failure — a
    missing token, an unfound series, or an empty/failed lookup all just
    return {"ok": False, ...}.
    """
    name = name.strip()
    if not name:
        return {"ok": False, "message": "Series name required"}

    with get_db() as db:
        authorized_ids = _require_series_role(db, name)
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
        if _require_series_role(db, name) != authorized_ids:
            return {"ok": False, "message": "Series changed; check again"}
        _upsert_series_description(db, name, description, "hardcover")

    return {"ok": True, "description": description}
