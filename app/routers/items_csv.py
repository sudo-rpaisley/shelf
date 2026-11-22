"""CSV export and import.

Split out of `app/routers/items.py` (Lever 5). Shared helpers live in
`items_common`; import the module and call through it so tests can patch.
"""

import asyncio
import csv
import io
import logging
import os
from contextvars import Context

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import require_role
from app.database import get_db
from app.routers import items_common
from app.services import libraries, user_state
from app.services import audiobookshelf, cover_queue, csv_import
from app.services import item_copies, item_write
from app.services import lists
from app.services import tags as tags_svc
from app.services.catalogue_security import (
    access_condition, item_projection, require_item_role, require_library_role,
)
from app.services.item_write import insert_item, update_item_fields

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# 50 MB, chosen deliberately: there is no MAX_COVER_SIZE equivalent to reuse
# for a text upload. Read this many bytes + 1 (never the whole body
# unbounded) so the existing `> MAX_CSV_UPLOAD_SIZE` branch below still
# fires on an oversized file (G55).
MAX_CSV_UPLOAD_SIZE = 50 * 1024 * 1024

# Both Audible tools export the same library, so they share one source label.
_FORMAT_SOURCE = {"audible_cli": "audible_import", "libation": "audible_import"}

@router.get("/export/csv")
async def export_csv(user=Depends(require_role("viewer"))):
    from fastapi.responses import StreamingResponse

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["title", "authors", "isbn", "media_type", "platform", "publisher", "publish_year", "page_count", "language", "series_name", "series_position", "location", "source", "estimated_value", "manual_value", "owned", "wishlisted", "deleted", "tags"])

    # Trash follows the same per-library editor boundary as the Trash page,
    # never the account's global role. Read-only membership/bearer caps cannot
    # expose deleted rows; editing one library never grants another's history.
    with get_db() as db:
        condition, params = access_condition(item_alias="i", user=user)
        trash_access, trash_params = access_condition(item_alias="i", role="editor", user=user)
        projection, private_join, private_params = item_projection(db)
        rows = db.execute(
            f"SELECT {projection}, l.name as location_name, {lists.WISHLISTED_SQL} AS wishlisted "
            f"FROM items i {private_join} "
            "LEFT JOIN locations l ON i.location_id = l.id "
            f"WHERE ({condition}) AND (i.deleted_at IS NULL OR ({trash_access})) ORDER BY i.title",
            [*private_params, *params, *trash_params],
        ).fetchall()
        # One grouped query for the whole result set — never a per-row query.
        tags_by_item = tags_svc.tags_for_items(db, [row["id"] for row in rows])
        locations_by_item = csv_import.export_locations(db, rows, user=user)

    for row in rows:
        writer.writerow([
            row["title"], row["authors"], row["isbn"], row["media_type"],
            row["platform"], row["publisher"], row["publish_year"], row["page_count"],
            row["language"], row["series_name"], row["series_position"],
            locations_by_item[row["id"]], row["source"],
            row["estimated_value"], row["manual_value"],
            row["owned"], 1 if row["wishlisted"] else 0,
            1 if row["deleted_at"] else 0,
            "; ".join(tags_by_item.get(row["id"], [])),
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=shelf_export.csv"},
    )


@router.post("/import/csv")
async def import_csv(request: Request, _=Depends(require_role("admin"))):
    """Import items from a CSV file upload.

    Accepts Shelf's own CSV format plus Goodreads and StoryGraph exports —
    the source is auto-detected from the header row (see
    services/reading_imports.py for the column mappings). Reading status and
    wishlist intent belong to the importing account; catalogue metadata and
    physical ownership remain shared.
    """
    from app.services import reading_imports

    form = await request.form()
    mode = form.get("mode", "skip")  # skip or update; anything else is rejected below
    if mode not in ("skip", "update"):
        return {
            "error": "Invalid import mode",
            "imported": 0,
            "skipped": 0,
            "errors": [],
        }
    to_read_wishlist = form.get("to_read_wishlist") in ("1", "true", "on")
    enrich_covers = form.get("enrich_covers") in ("1", "true", "on")
    csv_file = form.get("file")
    if not csv_file or not hasattr(csv_file, "read"):
        return {"error": "No file uploaded", "imported": 0, "skipped": 0, "errors": []}

    raw = await csv_file.read(MAX_CSV_UPLOAD_SIZE + 1)
    if len(raw) > MAX_CSV_UPLOAD_SIZE:  # 50 MB cap
        return {"error": "File too large (max 50 MB)", "imported": 0, "skipped": 0, "errors": []}
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # A whole-file error, in the same shape as "No file uploaded" above.
        # Guessing the encoding instead would import mojibake titles
        # silently, which is worse than refusing (design plan-imports-lt-libib
        # §"Reading the file").
        return {
            "error": "The file is not UTF-8 text. Re-export it as UTF-8 "
                      "(in a spreadsheet app: Save As → CSV UTF-8) and "
                      "upload it again.",
            "imported": 0,
            "skipped": 0,
            "errors": [],
        }
    delimiter = reading_imports.choose_delimiter(content.split("\n", 1)[0])
    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)

    if reader.fieldnames:
        reader.fieldnames = [f.strip().lower().replace(" ", "_") for f in reader.fieldnames]

    fmt = reading_imports.detect_format(reader.fieldnames)
    normalize = reading_imports.NORMALIZERS[fmt]
    import_user = dict(request.state.user)
    user_id = int(import_user["id"])
    source = _FORMAT_SOURCE.get(fmt) or (
        "csv_import" if fmt == reading_imports.GENERIC else f"{fmt}_import")

    imported = 0
    skipped = 0
    restored = 0
    trashed = 0
    # Ids of existing audiobooks an Audible row matched by title and stamped
    # with its ASIN — the response's `matched`.
    matched_ids: list[int] = []
    errors = []
    new_item_ids: list[int] = []
    cover_hints: list[tuple[int, str]] = []  # (id, audible-cli cover URL)
    # Audible-format rows with an ASIN and no cover URL (Libation): the
    # resolver's Audnexus leg fetches their cover by ASIN.
    asin_rows: list[int] = []
    # Rows an Audible-format import created — keyed on the format, not the
    # ASIN, so a row whose ASIN failed validation is still linked.
    audible_created: list[int] = []
    # Keyed ('isbn', isbn, media), ('upc', upc, media) or ('title', title,
    # authors, media) — the tag keeps the key spaces from colliding.
    seen_in_file: set[tuple] = set()
    # For the `dropped` report: every column that held data in some row, and
    # the (column, reason) pairs rows reported themselves.
    nonblank: set[str] = set()
    row_drops: set[tuple[str, str]] = set()
    cover_library_ids: dict[int, int] = {}

    _CSV_MAX_TEXT = 1000

    with get_db() as db:
        user_state.ensure_schema(db)
        db.execute("BEGIN IMMEDIATE")
        locs = csv_import.RowLocations(db)
        for i, row in enumerate(reader, start=2):
            # Every mutation (including a restore, tags, locations, personal
            # state and all writer projections) belongs to ONE row savepoint.
            # Roll back request-side caches/reports too, so a failed row cannot
            # poison a later retry of its own identifier/location.
            db.execute("SAVEPOINT csv_import_row")
            file_key, file_key_added = None, False
            counts_before = imported, skipped, restored, trashed
            queues = (matched_ids, new_item_ids, cover_hints, asin_rows, audible_created)
            queue_lengths = [len(queue) for queue in queues]
            locations_before = locs.checkpoint()
            drops_before = set(row_drops)
            try:
                # DictReader files overflow fields under the key None.
                nonblank.update(
                    k for k, v in row.items()
                    if k is not None and isinstance(v, str) and v.strip()
                )
                norm = normalize(row)
                row_drops.update(norm.get("dropped") or [])
                # LibraryThing acquisition columns that held data on this row
                # — they land only on a newly created item's copy.
                acq_given = [
                    c for c in csv_import.LT_ACQUISITION_COLUMNS if (row.get(c) or "").strip()
                ] if fmt == reading_imports.LIBRARYTHING else []

                title = norm["title"]
                if not title:
                    errors.append(f"Row {i}: missing title")
                    continue
                if len(title) > _CSV_MAX_TEXT:
                    errors.append(f"Row {i}: title too long (max {_CSV_MAX_TEXT} chars)")
                    continue
                if norm["authors"] and len(norm["authors"]) > _CSV_MAX_TEXT:
                    errors.append(f"Row {i}: authors too long (max {_CSV_MAX_TEXT} chars)")
                    continue
                if norm["publisher"] and len(norm["publisher"]) > _CSV_MAX_TEXT:
                    errors.append(f"Row {i}: publisher too long (max {_CSV_MAX_TEXT} chars)")
                    continue
                if norm["series_name"] and len(norm["series_name"]) > _CSV_MAX_TEXT:
                    errors.append(f"Row {i}: series_name too long (max {_CSV_MAX_TEXT} chars)")
                    continue
                # Gate on the normalizer *emitting* a `tags` key, not on the
                # format. StoryGraph's export has its own `Tags` column that
                # normalize_storygraph never reads and never emits, and the
                # header lowercasing above means row["tags"] exists for a
                # StoryGraph row too — length-checking it unconditionally
                # would start failing rows a StoryGraph file used to import
                # fine, for a cell its normalizer never reads. Checking the
                # emitted key instead of the format also covers LibraryThing
                # and Libib, whose normalizers do read and emit tags.
                if "tags" in norm:
                    # Check the raw cell the normalizer actually read: the
                    # generic and Libib formats read `tags`, LibraryThing
                    # reads `tags` or `your_tags` — check whichever is
                    # present rather than tracking which alias each format
                    # used.
                    raw_tags = row.get("tags") or row.get("your_tags")
                    if raw_tags and len(raw_tags) > _CSV_MAX_TEXT:
                        errors.append(f"Row {i}: tags too long (max {_CSV_MAX_TEXT} chars)")
                        continue

                # Use the same read-only validator and diagnostics as the
                # authoritative writer, before locations, restores or stamps.
                validated = csv_import.precheck_row(db, norm)
                isbn_val, isbn10_val = validated["isbn"], validated["isbn10"]
                media = validated["media_type"]
                if norm.get("location"):
                    locs.check(norm["location"])

                owned, wishlisted, owned_given, wishlisted_given = csv_import.resolve_state(
                    norm, to_read_wishlist, tracker=fmt != reading_imports.GENERIC
                )

                # Canonical ISBN13/10, then UPC, then title/authors without an
                # ISBN. Probe the exact retained type FIRST (even in Trash);
                # only a miss may use the canonical new-input slot. This keeps
                # distinct legacy/book twins and their private children intact.
                # Each type's ordered physical probe sees hidden/Trash collisions
                # before any mutation; require_item_role below never falls back
                # to a more convenient visible twin. The final matched/new-slot
                # type also keys in-file dedup, so aliases cannot double-insert.
                authors_val = norm["authors"] or ""
                isbn_pair = (isbn_val, isbn10_val) if isbn_val else None
                asin = norm.get("asin")
                title_hit = False
                for identity_media in csv_import.identity_media_types(norm, media):
                    if asin:
                        file_key = ("asin", asin, media)
                        existing, rung = csv_import.match_audiobook(db, asin, title, authors_val)
                        title_hit = rung == "title"
                    elif isbn_pair:
                        isbn13, isbn10 = isbn_pair
                        file_key = ("isbn", isbn13, identity_media)
                        existing = db.execute(
                            "SELECT id, deleted_at FROM items WHERE media_type = ? "
                            "AND isbn IN (?, ?) "
                            "ORDER BY deleted_at IS NOT NULL, id LIMIT 1",
                            (identity_media, isbn13, isbn10),
                        ).fetchone()
                    elif norm.get("upc"):
                        # Normalized EAN13, including historical UPC-A input.
                        upc = norm["upc"]
                        file_key = ("upc", upc, identity_media)
                        existing = db.execute(
                            "SELECT id, deleted_at FROM items WHERE upc = ? AND media_type = ? "
                            "ORDER BY deleted_at IS NOT NULL, id LIMIT 1",
                            (upc, identity_media),
                        ).fetchone()
                    else:
                        file_key = ("title", title.strip().lower(), authors_val.strip().lower(), identity_media)
                        existing = db.execute(
                            "SELECT id, deleted_at FROM items WHERE "
                            "TRIM(title) = TRIM(?) COLLATE NOCASE AND "
                            "TRIM(COALESCE(authors, '')) = TRIM(?) COLLATE NOCASE AND "
                            "media_type = ? AND (isbn IS NULL OR isbn = '') "
                            "ORDER BY deleted_at IS NOT NULL, id LIMIT 1",
                            (title, authors_val, identity_media),
                        ).fetchone()
                    if existing is not None:
                        break

                if file_key is not None:
                    if file_key in seen_in_file:
                        skipped += 1
                        continue
                    seen_in_file.add(file_key)
                    file_key_added = True

                if existing:
                    require_item_role(db, existing["id"], "editor", include_deleted=True)

                if title_hit:
                    # The row's first write (G85): nothing that can raise
                    # sits between the match above and this stamp. It is the
                    # one field skip mode writes, so the next import matches
                    # on the ASIN instead of the title.
                    update_item_fields(db, existing["id"], {"asin": asin})
                    matched_ids.append(existing["id"])

                # Whether the file itself marks this row deleted (the CSV's
                # own `deleted` column, generic-only — None on a
                # reading-tracker row, which has no Trash concept).
                incoming_deleted = bool(norm.get("deleted"))

                if existing:
                    # Acquisition fields cannot identify a copy on an existing
                    # item. Only explicit physical placement moves its primary.
                    row_drops.update((c, "existing_item") for c in acq_given)
                    # Restoration belongs to the same row savepoint as every
                    # later metadata/private/tag/projection mutation.
                    if existing["deleted_at"] is not None:
                        if incoming_deleted:
                            # The file and the database already agree this
                            # row is gone — leave it alone. Restoring it
                            # would fight the very state the column exists
                            # to carry, so this is neither a restore nor an
                            # update; it is counted with the rows that
                            # caused no write at all.
                            skipped += 1
                            continue
                        item_write.restore_item(db, existing["id"])
                        restored += 1
                    # A live hit is never trashed by an import, regardless of
                    # what the incoming row's `deleted` column says (the
                    # safety property) — `incoming_deleted` plays no further
                    # part below; the skip/update path runs exactly as it
                    # does for a row that has no opinion on Trash at all.
                    if mode == "skip":
                        if fmt != reading_imports.GENERIC:
                            csv_import.save_personal_state(db, user_id, existing["id"], norm, bool(owned), to_read_wishlist)
                        # Restored or not, a skip-mode hit leaves the row's
                        # own fields untouched — restore_item only clears
                        # deleted_at. Do not also count a restored row as
                        # `skipped`: something *did* happen to it.
                        if existing["deleted_at"] is None:
                            skipped += 1
                        continue
                    # mode == update: refresh metadata, and reading state
                    # for reading-tracker imports — identical path for a
                    # live hit and a just-restored one.
                    placement = locs.ensure(norm["location"]) if norm.get("location") else None
                    csv_import.update_from_row(
                        db, existing["id"], norm,
                        generic=fmt == reading_imports.GENERIC, location=placement,
                    )
                    # Additive only — import never removes a tag. An absent
                    # `tags` column and an empty `tags` cell are deliberately
                    # the *same* answer here (contrast the owned/wishlisted
                    # flags above, G87, where absence and presence-but-empty
                    # differ): parse_tag_list already turns both into [],
                    # so attach_tags on [] is a no-op either way — there is
                    # no "clear the tags" state for a CSV row to express.
                    if norm.get("tags"):
                        tags_svc.attach_tags(db, existing["id"], norm["tags"])
                    # Change catalogue state only when stated (G87). Tracker
                    # reading/wishlist intent is saved separately for the actor.
                    state_fields = {}
                    if owned_given:
                        state_fields["owned"] = int(owned)
                    if wishlisted_given:
                        state_fields["wishlisted"] = wishlisted
                    if state_fields:
                        update_item_fields(db, existing["id"], state_fields, create_copy_on_acquisition=False)
                    if fmt != reading_imports.GENERIC:
                        csv_import.save_personal_state(db, user_id, existing["id"], norm, bool(owned), to_read_wishlist)
                    # Disjoint counts, in both modes: a restored row is
                    # `restored` and nothing else. Counting it as `imported`
                    # too made "Imported: 5, Restored: 1" unreadable — five
                    # rows or six? — and disagreed with skip mode, which
                    # already keeps them apart.
                    if existing["deleted_at"] is None:
                        imported += 1
                    continue

                pub_year = norm["publish_year"]
                page_count = norm["page_count"]
                require_library_role(db, libraries.DEFAULT_LIBRARY_ID, "editor")

                # Only keys with a value: a None would bypass the SCHEMA
                # default, and location_id=None would still call the seam.
                extra = {k: norm[k] for k in ("language", "platform", "manual_value",
                                              "estimated_value", *csv_import.FILL_EMPTY)
                         if norm.get(k) is not None}
                placement = locs.ensure(norm["location"]) if norm.get("location") else None
                if placement is not None and placement.legacy_id is not None:
                    extra["location_id"] = placement.legacy_id
                new_id = insert_item(
                    db,
                    title=title,
                    authors=norm["authors"],
                    isbn=isbn_val,
                    isbn10=isbn10_val,
                    upc=norm.get("upc"),
                    media_type=media,
                    publisher=norm["publisher"],
                    publish_year=int(pub_year) if pub_year and str(pub_year).isdigit() else None,
                    page_count=int(page_count) if page_count and str(page_count).isdigit() else None,
                    series_name=norm["series_name"],
                    series_position=norm.get("series_position"),
                    owned=int(owned),
                    wishlisted=wishlisted,
                    source=source,
                    library_id=libraries.DEFAULT_LIBRARY_ID,
                    # No hit in the database: a row the file marks deleted
                    # is created directly rather than resurrected, so a
                    # trashed twin that this row's own dedup keys somehow
                    # missed is refused, not restored, on this path.
                    restore_trashed=False,  # all intended restores were explicitly matched under this lock
                    **extra,
                )
                if placement is not None:
                    csv_import.place_location(db, new_id, placement)
                if fmt != reading_imports.GENERIC:
                    csv_import.save_personal_state(db, user_id, new_id, norm, bool(owned), to_read_wishlist)
                if norm.get("tags"):  # additive only — see the update path's comment
                    tags_svc.attach_tags(db, new_id, norm["tags"])
                if incoming_deleted:
                    # Trashed "now": a CSV row carries no deletion timestamp.
                    item_write.trash_item(db, new_id)
                    trashed += 1
                else:
                    if item_write.was_restored(new_id):
                        # insert_item restored a trashed twin, which may
                        # already have copies — same rule as a matched row.
                        row_drops.update((c, "existing_item") for c in acq_given)
                    elif norm.get("copy") and owned:
                        # Enrich the funnel's first copy, rather than adding a
                        # duplicate. All copy failures roll back the whole row.
                        primary = db.execute(
                            "SELECT id FROM copies_live WHERE item_id = ? AND is_primary = 1",
                            (new_id,),
                        ).fetchone()
                        if primary:
                            item_copies.update_copy(db, primary["id"], norm["copy"])
                        else:
                            item_copies.add_copy(db, new_id, norm["copy"])
                    if fmt in _FORMAT_SOURCE:
                        audible_created.append(new_id)
                    if isbn_val:
                        new_item_ids.append(new_id)
                    elif norm.get("cover_url") and not item_write.was_restored(new_id):
                        cover_hints.append((new_id, norm["cover_url"]))
                    elif (
                        fmt in _FORMAT_SOURCE
                        and norm.get("asin")
                        and not item_write.was_restored(new_id)
                    ):
                        # Audible-format rows only: every one is an audiobook,
                        # so the title-search-free hand-off is safe (G29).
                        asin_rows.append(new_id)
                    imported += 1
            except Exception as e:
                db.execute("ROLLBACK TO csv_import_row")
                imported, skipped, restored, trashed = counts_before
                for queue, length in zip(queues, queue_lengths):
                    del queue[length:]
                locs.rollback(locations_before)
                row_drops = drops_before
                if file_key_added:
                    seen_in_file.discard(file_key)
                message = "Item is unavailable or not editable" if isinstance(e, HTTPException) else str(e)
                errors.append(f"Row {i}: {message}")
            finally:
                db.execute("RELEASE csv_import_row")

        if enrich_covers and not os.environ.get("SHELF_DISABLE_COVER_ENRICH"):
            # Capture the exact parents under the import's write lock. Empty
            # background contexts must not turn HTTP work into a trusted job;
            # the producer and resolver revalidate these bindings later.
            cover_library_ids = items_common.authorize_item_targets(
                db, [*new_item_ids, *(hid for hid, _url in cover_hints), *asin_rows],
                user=import_user,
            )

    dropped = sorted(
        {(c, "unmapped") for c in nonblank - reading_imports.CONSUMED_COLUMNS[fmt]}
        | row_drops
    )

    covers_queued = 0
    if enrich_covers and new_item_ids and not os.environ.get("SHELF_DISABLE_COVER_ENRICH"):
        eligible = cover_queue.filter_cover_eligible(new_item_ids)
        covers_queued = len(eligible)
        asyncio.create_task(items_common._enrich_import_covers(
            eligible, user=import_user,
            library_ids={item_id: cover_library_ids[item_id] for item_id in eligible},
        ), context=Context())
    if enrich_covers and cover_hints and not os.environ.get("SHELF_DISABLE_COVER_ENRICH"):
        for hid, url in cover_hints:  # paced by outbound's per-host interval
            cover_queue.enqueue(hid, {
                "cover_url": url, "skip_title_search": True,
                "_cover_authority": items_common._QueuedCoverAuthority(import_user, cover_library_ids[hid]),
            })
        covers_queued += len(cover_hints)
    if enrich_covers and asin_rows and not os.environ.get("SHELF_DISABLE_COVER_ENRICH"):
        for aid in asin_rows:
            cover_queue.enqueue(aid, {
                "skip_title_search": True,
                "_cover_authority": items_common._QueuedCoverAuthority(import_user, cover_library_ids[aid]),
            })
        covers_queued += len(asin_rows)
    # After the block has committed: auto_link opens its own connection (G112).
    # Only Audible-format rows need linking to their print editions.
    if audible_created or matched_ids:
        audiobookshelf.auto_link(audible_created + matched_ids)

    return {
        # `imported`, `skipped`, `restored` and `trashed` partition every row
        # that reached a decision (a row that raised before one is in
        # `errors` instead, never in these four): imported (created live, or
        # a live hit updated), skipped (a live hit in skip mode, an in-file
        # duplicate, or an already-trashed row the file also marks deleted),
        # restored (a trashed hit the file's row says is live again), and
        # trashed (no hit, and the file's own row says it is deleted — see
        # `incoming_deleted` above).
        "imported": imported,
        "skipped": skipped,
        # Disjoint from `imported`/`skipped`: a restored row is counted here
        # only, even when update mode refreshes it after restoring, and it is
        # never `skipped` — something was written to it.
        "restored": restored,
        "trashed": trashed,
        # `errors` is capped for the wire; `error_count` is the true total, so
        # the UI can list twenty of thirty-seven and still say thirty-seven.
        "errors": errors[:20],
        "error_count": len(errors),
        "format": fmt,
        "covers_queued": covers_queued,
        # Existing audiobooks an Audible row matched by title and author and
        # stamped with its ASIN; each is also counted in one of the four above.
        "matched": len(matched_ids),
        # Columns that held data and were not imported, as codes the import
        # card turns into words (G58): `unmapped` (this format does not read
        # the column), `existing_item` (LibraryThing acquisition data on a
        # row that matched an item already in Shelf), `game_platform` (a
        # Libib game's `creators`).
        "dropped": [{"column": c, "reason": r} for c, r in dropped],
        # Locations a `location` cell named that did not exist, each once.
        "locations_created": locs.created,
    }
