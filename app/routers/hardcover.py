import asyncio
import json
import logging
import re
from contextvars import Context

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import StreamingResponse

from app.auth import require_role
from app.features import require_feature
from app.config import HTTP_TIMEOUT
from app.database import get_db, get_setting
from app.routers import items_common
from app.services import series_memberships as series_memberships_svc
from app.services import hardcover, covers, lists, restore_report
from app.services import isbn as isbn_svc
from app.services import catalogue_security, item_write, libraries, user_state
from app.services.catalogue_security import require_catalogue_role, require_item_role
from app.services.item_write import (IdentifierInTrash, ItemValueError,
                                     insert_item, update_item_fields)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hardcover")

# Hardcover status IDs
HC_STATUSES = {
    1: "Want to Read",
    2: "Currently Reading",
    3: "Read",
    4: "Paused",
    5: "Did Not Finish",
}
_UNSET = object()


def _capture_user():
    actor = catalogue_security.current_actor()
    return dict(actor) if actor is not None else None


def _private_link_key(user_id, item_id):
    # No import-time schema/adapter: settings already supports namespaced,
    # non-secret integration identifiers. Never fall back to another account's
    # legacy items.hardcover_user_book_id in an HTTP request.
    return f"hardcover_user_book:{int(user_id)}:{int(item_id)}"


def _account_item(db, item, user):
    result = dict(item)
    if user is not None:
        state = user_state.get_state(db, user["id"], item["id"])
        for field in ("reading_status", "date_started", "date_finished"):
            result[field] = state[field]
        value = get_setting(db, _private_link_key(user["id"], item["id"]))
        result["hardcover_user_book_id"] = int(value) if value else None
    return result


def _save_hc_ids(db, item_id, result, *, user=None, library_ids=None):
    with item_write._write_scope(db):
        items_common.authorize_item_targets(db, [item_id], user=user, library_ids=library_ids)
        updates = {key: result[key] for key in ("hardcover_book_id", "hardcover_edition_id")
                   if result.get(key) is not None}
        private_id = result.get("hardcover_user_book_id")
        if user is None and private_id is not None:
            updates["hardcover_user_book_id"] = private_id
        if updates:
            update_item_fields(db, item_id, updates)
        if user is not None and private_id is not None:
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_private_link_key(user["id"], item_id), str(int(private_id))),
            )


def _check_hc_identifiers(db, book, *, user=None):
    """Physical, narrow collision probes; never disclose a hidden identity."""
    rows = {}
    for field in ("hardcover_book_id", "isbn"):
        if not book.get(field):
            continue
        for row in db.execute(f"SELECT * FROM items WHERE {field} = ?", (book[field],)):
            try:
                require_item_role(db, row["id"], "editor", user=user, include_deleted=True)
            except HTTPException as exc:
                if exc.status_code == 404:
                    raise item_write.IdentifierUnavailable("This identifier is unavailable") from None
                raise
            rows[row["id"]] = row
    return list(rows.values())


def _error_stream(message):
    async def events():
        yield f"data: {json.dumps({'type': 'error', 'message': message})}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/test")
async def test_hardcover(request: Request, _=Depends(require_role("admin"))):
    """Test a Hardcover API token."""
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "message": "Invalid request body"}
    if not isinstance(data, dict):
        return {"ok": False, "message": "Invalid request body"}

    raw_token = data.get("token")
    if raw_token is not None and not isinstance(raw_token, str):
        return {"ok": False, "message": "Invalid request body"}
    token = (raw_token or "").strip()
    if not token:
        # Masked field posts empty — test the stored token instead
        with get_db() as db:
            token = get_setting(db, "hardcover_token")
    if not token:
        return {"ok": False, "message": "No token provided"}
    return await hardcover.test_connection(token)


@router.get("/search")
async def search_hardcover(request: Request, q: str = "", _=Depends(require_role("viewer")),
                           __=Depends(require_feature("hardcover"))):
    """Search Hardcover catalog. Returns HTMX fragment with results."""
    templates = request.app.state.templates
    with get_db() as db:
        token = get_setting(db, "hardcover_token")

    results = []
    if q.strip() and token:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            results = await hardcover.search_books(q.strip(), client, token=token)

        # Mark which books are already in Shelf
        if results:
            hc_ids = [r["hardcover_book_id"] for r in results if r.get("hardcover_book_id")]
            if hc_ids:
                placeholders = ",".join("?" for _ in hc_ids)
                with get_db() as db:
                    existing = {
                        row["hardcover_book_id"]
                        for row in db.execute(
                            f"SELECT hardcover_book_id FROM items_live WHERE hardcover_book_id IN ({placeholders})",
                            hc_ids,
                        ).fetchall()
                    }
                for r in results:
                    r["in_shelf"] = r.get("hardcover_book_id") in existing

    return templates.TemplateResponse(
        request, "fragments/hardcover_search_results.html",
        {"results": results, "query": q},
    )


@router.post("/add-to-shelf")
async def add_hardcover_to_shelf(request: Request, _=Depends(require_catalogue_role("editor")),
                                 __=Depends(require_feature("hardcover"))):
    """Add a book from Hardcover search to Shelf as a wishlist item."""
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "message": "Invalid request body"}
    if not isinstance(data, dict):
        return {"ok": False, "message": "Invalid request body"}

    raw_title = data.get("title")
    if raw_title is not None and not isinstance(raw_title, str):
        return {"ok": False, "message": "Invalid request body"}
    for key in ("authors", "isbn", "publisher", "description", "series_name", "cover_url"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            return {"ok": False, "message": "Invalid request body"}
    for key in ("hardcover_book_id", "year", "pages"):
        value = data.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            return {"ok": False, "message": "Invalid request body"}
    library_id = data.get("library_id")
    if library_id is not None and (isinstance(library_id, bool) or not isinstance(library_id, int)):
        return {"ok": False, "message": "Invalid request body"}
    library_id = library_id or libraries.DEFAULT_LIBRARY_ID
    series_position = data.get("series_position")
    if series_position is not None and (
        isinstance(series_position, bool) or not isinstance(series_position, (int, float))
    ):
        return {"ok": False, "message": "Invalid request body"}
    series_memberships = data.get("series_memberships")
    if series_memberships is not None and not isinstance(series_memberships, list):
        return {"ok": False, "message": "Invalid request body"}

    title = (raw_title or "").strip()
    if not title:
        return {"ok": False, "message": "Title required"}

    isbn = data.get("isbn")
    hc_book_id = data.get("hardcover_book_id")

    # Download cover
    cover_path = None
    cover_url = data.get("cover_url")

    # A Hardcover ISBN is a provider value: pre-cleaned per #54 rather than
    # refused — a bad check digit is dropped, not rejected outright, since
    # this endpoint has no scan card to show the user a refusal on.
    #
    # This pre-clean (and its logger.warning below) stays here, above the
    # write-lock block, rather than moving down to sit next to the insert.
    # SQLiteHandler opens its own connection to write log_entries, so a log
    # call made from inside a BEGIN IMMEDIATE block blocks on the lock that
    # same block holds and waits out SQLite's 5s busy timeout (G3). The guard
    # moves down to the insert; the insert never moves up to the log call.
    isbn_pair = isbn_svc.canonical_isbn_pair(isbn) if isbn else None
    if isbn and isbn_pair is None:
        logger.warning(
            "%s: %r is not a valid ISBN — stored without one",
            f"Hardcover add-to-shelf ({title})", isbn,
        )
    isbn_clean, isbn10 = isbn_pair or (None, None)

    # --- Duplicate check, under the write lock (G18).
    #
    # This used to be a standalone pre-check block, read before the insert
    # block ever took the lock — a rival Add click had the whole gap between
    # the two blocks to commit the same hardcover_book_id or isbn row. BEGIN
    # IMMEDIATE must be this block's first statement, above the guard
    # queries, so the check and the insert see one consistent state.
    #
    # The isbn arm is guarded on `not existing`, not a bare `if`: a request
    # can carry a matching hardcover_book_id *and* a non-matching isbn, and a
    # bare second `if` would overwrite the first arm's hit with the second
    # arm's None — inserting exactly the duplicate this guard exists to stop.
    #
    # It is `if not existing`, though, and NOT `elif`. The two arms fall
    # through today: a hardcover_book_id that misses still lets the isbn arm
    # run, which is how a book added by barcode scan (isbn, no
    # hardcover_book_id) is recognised when it is added again from Hardcover
    # search (both fields sent, the id missing). An `elif` skips that arm and
    # the request reaches the insert, where `UNIQUE(isbn, media_type)` raises
    # an uncaught IntegrityError — a 500 where the duplicate body belongs.
    existing = None
    item_id = None
    value_error = None
    promoted_to_wishlist = False
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        catalogue_security.require_library_role(db, library_id, "editor")
        try:
            _check_hc_identifiers(db, {"hardcover_book_id": hc_book_id, "isbn": isbn_clean})
        except ItemValueError as exc:
            return {"ok": False, "message": str(exc)}
        if hc_book_id:
            existing = db.execute(
                f"SELECT id, owned, {lists.WISHLISTED_SQL} AS wishlisted FROM items_live i "
                "WHERE hardcover_book_id = ?", (hc_book_id,)
            ).fetchone()
        if not existing and isbn_clean:
            existing = db.execute(
                f"SELECT id, owned, {lists.WISHLISTED_SQL} AS wishlisted FROM items_live i "
                "WHERE isbn = ?", (isbn_clean,)
            ).fetchone()
        # Both live guards missed. The funnel keys on isbn/upc only, and the
        # series "missing books" button sends a hardcover_book_id and no
        # isbn — so a trashed book is found by its Hardcover id here, or it
        # would be filed a second time beside the trashed one.
        trashed_id = (
            _trashed_by_hardcover_id(db, hc_book_id)
            if existing is None and hc_book_id else None
        )
        if existing:
            require_item_role(db, existing["id"], "editor")
        if trashed_id is not None and libraries.item_library_id(db, trashed_id) != library_id:
            return {"ok": False, "message": "This identifier is unavailable in the requested library"}
        if trashed_id is not None:
            try:
                with item_write._write_scope(db):
                    if not item_write.restore_item(db, trashed_id):
                        raise ItemValueError("This identifier is unavailable")
                    # Wishlist intent and restore are one savepoint: a refusal
                    # must not commit a restore before returning ok=False.
                    if not db.execute("SELECT owned FROM items_live WHERE id = ?",
                                      (trashed_id,)).fetchone()["owned"]:
                        update_item_fields(db, trashed_id, {"wishlisted": True})
                    item_id = item_write.ItemId(trashed_id, restored=True)
            except ItemValueError as exc:
                value_error = str(exc)
        elif existing is None:
            try:
                item_id = insert_item(
                    db,
                    library_id=library_id,
                    title=title,
                    authors=data.get("authors"),
                    isbn=isbn_clean,
                    isbn10=isbn10,
                    media_type="book",
                    publisher=data.get("publisher"),
                    publish_year=data.get("year"),
                    page_count=data.get("pages"),
                    description=data.get("description"),
                    series_name=data.get("series_name"),
                    series_position=data.get("series_position"),
                    series_memberships=series_memberships,
                    reading_status="want_to_read",
                    source="hardcover",
                    owned=0,
                    wishlisted=True,
                    hardcover_book_id=hc_book_id,
                )
            except ItemValueError as e:
                value_error = str(e)
        elif not existing["owned"] and not existing["wishlisted"]:
            # A neither row: this book is already tracked (by hardcover_book_id
            # or isbn) but is on no list and not owned. Adding it from
            # Hardcover here means "put it on the wishlist", not "insert a
            # second row" — join the existing one to the wishlist instead.
            try:
                update_item_fields(db, existing["id"], {"wishlisted": True})
                promoted_to_wishlist = True
            except ItemValueError as exc:
                value_error = str(exc)

    if value_error:
        return {"ok": False, "message": value_error}
    if promoted_to_wishlist:
        return {"ok": True, "message": "Added to wishlist", "item_id": existing["id"]}
    if existing:
        return {"ok": False, "message": "Already in your library", "item_id": existing["id"]}

    # Download cover — skipped entirely on a restored row that already has
    # one, so the user's own cover survives the re-add (and no outbound call
    # is made for a cover we would then discard, claude-R8).
    if cover_url and not restore_report.keeps_stored_cover(item_id):
        with items_common.CoverStage(item_id) as stage:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                cover_path = await covers.download_cover(stage.download_id, isbn, None, None, client, hardcover_cover_url=cover_url)
            if cover_path:
                with get_db() as db:
                    cover_path = stage.store(db, cover_path, library_ids={item_id: library_id})

    return {
        "ok": True, "item_id": item_id, "title": title,
        "restored": item_write.was_restored(item_id),
    }


@router.post("/schedule")
async def set_hardcover_schedule(interval: str = Form("off"), _=Depends(require_role("admin"))):
    """Set the Hardcover sync schedule."""
    if interval not in ("off", "daily", "weekly"):
        return JSONResponse(
            {"ok": False, "message": "Invalid sync interval"}, status_code=400
        )
    with get_db() as db, item_write._write_scope(db):
        try:
            hardcover.set_schedule_owner(db, _, enabled=interval != "off")
        except ValueError:
            raise HTTPException(403, "Forbidden") from None
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('hc_sync_interval', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = ?",
            (interval, interval),
        )
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/push/{item_id}")
async def push_to_hardcover(item_id: int, _=Depends(require_catalogue_role("editor")),
                            __=Depends(require_feature("hardcover"))):
    """Push a single item to Hardcover. Returns JSON result."""
    user = _capture_user()
    with get_db() as db, item_write._write_scope(db):
        library_ids = items_common.authorize_item_targets(db, [item_id])
        token = get_setting(db, "hardcover_token")
        item = db.execute("SELECT * FROM items_live WHERE id = ?", (item_id,)).fetchone()
        item = _account_item(db, item, user)

    if not token:
        return {"ok": False, "message": "Hardcover API token required"}
    if not item:
        return {"ok": False, "message": "Item not found"}

    async with httpx.AsyncClient(timeout=30) as client:
        result = await hardcover.push_item_to_hardcover(token, dict(item), client)

    if result.get("ok"):
        # Store Hardcover IDs back on the item
        with get_db() as db:
            _save_hc_ids(db, item_id, result, user=user, library_ids=library_ids)

    return result


@router.get("/export/stream")
async def export_hardcover_stream(request: Request, _=Depends(require_catalogue_role("editor")),
                                  __=Depends(require_feature("hardcover"))):
    """SSE endpoint for bulk exporting items to Hardcover."""
    user = _capture_user()
    with get_db() as db:
        token = get_setting(db, "hardcover_token")

    if not token:
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Hardcover API token required'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    # Filter: only items with ISBN, optionally filter by owned
    owned_filter = request.query_params.get("owned", "")

    with get_db() as db, item_write._write_scope(db):
        conditions = ["isbn IS NOT NULL"]
        if owned_filter == "1":
            conditions.append("owned = 1")
        where = " AND ".join(conditions)
        items = db.execute(
            f"SELECT * FROM items_live WHERE {where} ORDER BY title"
        ).fetchall()
        library_ids = items_common.authorize_item_targets(db, [item["id"] for item in items])
        items = [_account_item(db, item, user) for item in items]

    queue: asyncio.Queue = asyncio.Queue()

    async def run_export():
        stats = {"pushed": 0, "updated": 0, "skipped": 0, "not_found": 0, "errors": 0, "total": len(items)}
        try:
            with get_db() as db, item_write._write_scope(db):
                items_common.authorize_item_targets(
                    db, [item["id"] for item in items], user=user, library_ids=library_ids)
            async with httpx.AsyncClient(timeout=30) as client:
                for i, item in enumerate(items, 1):
                    title = item["title"]
                    try:
                        with get_db() as db, item_write._write_scope(db):
                            items_common.authorize_item_targets(
                                db, [item["id"]], user=user, library_ids=library_ids)
                        result = await hardcover.push_item_to_hardcover(token, dict(item), client)
                        if result.get("ok"):
                            status = result.get("status", "pushed")
                            if status == "added":
                                stats["pushed"] += 1
                            else:
                                stats["updated"] += 1
                            # Store IDs
                            with get_db() as db:
                                _save_hc_ids(db, item["id"], result, user=user, library_ids=library_ids)
                        else:
                            if "not found" in result.get("message", "").lower():
                                stats["not_found"] += 1
                                status = "not found"
                            else:
                                stats["errors"] += 1
                                status = result.get("message", "error")
                        await queue.put({
                            "type": "progress", "current": i, "total": len(items),
                            "title": title, "status": status,
                        })
                    except HTTPException:
                        raise
                    except Exception:
                        logger.exception("Error processing item during Hardcover export")
                        stats["errors"] += 1
                        await queue.put({
                            "type": "progress", "current": i, "total": len(items),
                            "title": title, "status": "error",
                        })

            await queue.put({"type": "done", **stats})
        except Exception:
            logger.exception("Hardcover export failed")
            await queue.put({"type": "error", "message": "Export failed — check server logs"})

    async def event_stream():
        task = asyncio.create_task(run_export(), context=Context())
        try:
            while True:
                msg = await queue.get()
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["type"] in ("done", "error"):
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/import/stream")
async def import_hardcover_stream(request: Request, _=Depends(require_catalogue_role("editor")),
                                  __=Depends(require_feature("hardcover"))):
    """SSE endpoint for importing books from Hardcover with progress updates."""
    user = _capture_user()
    try:
        library_id = int(request.query_params.get("library_id") or libraries.DEFAULT_LIBRARY_ID)
    except ValueError:
        return _error_stream("Invalid library")
    with get_db() as db, item_write._write_scope(db):
        catalogue_security.require_library_role(db, library_id, "editor")
        token = get_setting(db, "hardcover_token")

    if not token:
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Hardcover API token required'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    # Parse filter params from query string
    status_ids = request.query_params.get("statuses", "")
    overwrite = request.query_params.get("overwrite", "false") == "true"

    selected_statuses = []
    if status_ids:
        selected_statuses = [int(s) for s in status_ids.split(",") if s.isdigit()]

    # Fetch and match in the HTTP scope. Clearing the context before this point
    # would let fuzzy matching select somebody else's library/private state.
    async def prepare_import():
        hc_user_id = await hardcover.get_user_id(token)
        if not hc_user_id:
            raise ItemValueError("Could not get Hardcover user ID")
        async with httpx.AsyncClient(timeout=30) as client:
            books = await hardcover.get_user_books(
                token, hc_user_id, status_ids=selected_statuses or None, client=client)
        books = [_clean_import_book(book) for book in books]
        with get_db() as db, item_write._write_scope(db):
            catalogue_security.require_library_role(db, library_id, "editor")
            title_index = _build_title_index(db)
            expected_ids = []
            library_ids = {}
            for book in books:
                existing = _find_existing_item(db, book, title_index, user=user)
                expected_ids.append(existing["id"] if existing is not None else None)
                if existing is not None:
                    library_ids[existing["id"]] = libraries.item_library_id(db, existing["id"])
        return books, title_index, expected_ids, library_ids

    queue: asyncio.Queue = asyncio.Queue()

    async def run_import(books, title_index, expected_ids, library_ids):
        stats = {"added": 0, "updated": 0, "skipped": 0, "in_trash": 0,
                 "errors": 0, "total": len(books)}
        try:
            cover_jobs = []
            outcomes = []
            with get_db() as db, item_write._write_scope(db):
                catalogue_security.require_library_role(db, library_id, "editor", user=user)
                # ALL identities and parent bindings are checked before the
                # first mutation. The worker never builds a catalogue index.
                for book, expected in zip(books, expected_ids):
                    existing = _find_existing_item(db, book, title_index, user=user)
                    actual = existing["id"] if existing is not None else None
                    if actual != expected:
                        raise item_write.IdentifierUnavailable("This identifier is unavailable")
                    if actual is not None and libraries.item_library_id(db, actual) != library_ids[actual]:
                        raise HTTPException(404, "Item not found")
                created_ids = set()
                for book, expected in zip(books, expected_ids):
                    # Repeated provider entries may refer to a row created by
                    # this same authorized transaction. They must not widen
                    # authority to a previously unplanned existing target.
                    if expected is None and created_ids:
                        now = _find_existing_item(db, book, title_index, user=user)
                        if now is not None and now["id"] in created_ids:
                            expected = now["id"]
                    result, cover_job = _import_single_book_metadata(
                        book, overwrite, title_index, db=db, user=user,
                        library_id=library_id, expected_item_id=expected)
                    stats[result] += 1
                    outcomes.append((book["title"], result))
                    if cover_job:
                        cover_jobs.append(cover_job)
                        library_ids[cover_job["item_id"]] = libraries.item_library_id(db, cover_job["item_id"])
                        if result == "added":
                            created_ids.add(cover_job["item_id"])
            for i, (title, result) in enumerate(outcomes, 1):
                await queue.put({"type": "progress", "current": i, "total": len(books),
                                 "title": title, "status": result})

            # Phase 2: Download covers in parallel batches
            if cover_jobs:
                await queue.put({"type": "progress", "current": 0, "total": len(cover_jobs), "title": f"Downloading {len(cover_jobs)} covers...", "status": "loading"})
                batch_size = 5
                done_covers = 0
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    for batch_start in range(0, len(cover_jobs), batch_size):
                        batch = cover_jobs[batch_start:batch_start + batch_size]
                        with get_db() as db, item_write._write_scope(db):
                            items_common.authorize_item_targets(
                                db, [job["item_id"] for job in batch], user=user, library_ids=library_ids)
                        tasks = [_download_cover_with_fallback(job, client, user=user,
                                                                library_ids=library_ids) for job in batch]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        for job, result in zip(batch, results):
                            done_covers += 1
                            if isinstance(result, HTTPException):
                                raise result
                        await queue.put({
                            "type": "progress", "current": done_covers, "total": len(cover_jobs),
                            "title": f"Covers: {done_covers}/{len(cover_jobs)}", "status": "covers",
                        })

            await queue.put({"type": "done", **stats})
        except Exception:
            logger.exception("Hardcover import failed")
            await queue.put({"type": "error", "message": "Import failed — check server logs"})

    async def event_stream():
        # Keep the native loading frame before the remote fetch. This generator
        # still has the live HTTP actor (pure-ASGI lifetime middleware); only the
        # fully preflighted, explicit plan below is handed to a trusted task.
        loading = {"type": "progress", "current": 0, "total": 0,
                   "title": "Fetching library from Hardcover...", "status": "loading"}
        yield f"data: {json.dumps(loading)}\n\n"
        try:
            plan = await prepare_import()
        except ItemValueError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
            return
        except HTTPException:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Import is not authorized'})}\n\n"
            return
        except Exception:
            logger.exception("Hardcover import preparation failed")
            yield f"data: {json.dumps({'type': 'error', 'message': 'Import failed — check server logs'})}\n\n"
            return
        task = asyncio.create_task(run_import(*plan), context=Context())
        try:
            while True:
                msg = await queue.get()
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["type"] in ("done", "error"):
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _normalize_title(title: str) -> str:
    """Normalize a title for fuzzy matching."""
    t = title.lower().strip()
    t = re.sub(r"^(the|a|an)\s+", "", t)
    t = re.sub(r"\s*[:—–\-]\s.*$", "", t)  # strip subtitle
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _build_title_index(db=None) -> dict:
    """Build a lookup dict of normalized_title -> [(id, authors, cover_path, title)] for fuzzy matching."""
    index: dict[str, list] = {}
    if db is None:
        with get_db() as connection:
            return _build_title_index(connection)
    rows = db.execute("SELECT id, title, authors, cover_path FROM items_live WHERE title IS NOT NULL").fetchall()
    for row in rows:
        norm = _normalize_title(row["title"])
        if norm not in index:
            index[norm] = []
        index[norm].append({"id": row["id"], "title": row["title"], "authors": row["authors"], "cover_path": row["cover_path"]})
    return index


async def _download_cover_with_fallback(job: dict, client: httpx.AsyncClient, *,
                                      user=None, library_ids=None) -> str | None:
    """Download cover using full pipeline: Hardcover URL -> Open Library -> Amazon."""
    item_id = job["item_id"]
    isbn = job.get("isbn")
    hc_url = job.get("cover_url")

    # Use the full cover pipeline with all fallback sources
    with get_db() as db, item_write._write_scope(db):
        library_ids = items_common.authorize_item_targets(
            db, [item_id], user=user, library_ids=library_ids)
    with items_common.CoverStage(item_id) as stage:
        cover_path = await covers.download_cover(
            stage.download_id, isbn, None, None, client, hardcover_cover_url=hc_url)
        if cover_path:
            with get_db() as db:
                return stage.store(db, cover_path, user=user, library_ids=library_ids)
    return None


def _trashed_by_hardcover_id(db, hc_book_id) -> int | None:
    """The id of a trashed item carrying this Hardcover book id, or None.

    Called only after every live strategy has missed — that ordering is the
    whole of the live-wins rule for a non-unique key. Physical by definition:
    it exists to find the row `items_live` hides. The sync skips on a hit;
    add-to-shelf restores it.
    """
    rows = _check_hc_identifiers(db, {"hardcover_book_id": hc_book_id})
    return next((row["id"] for row in rows if row["deleted_at"] is not None), None)


def _find_existing_item(db, book: dict, title_index: dict, *, user=None):
    """Find an existing item matching a Hardcover book. Returns sqlite3.Row or None."""
    hc_book_id = book.get("hardcover_book_id")
    isbn = book.get("isbn")
    hits = _check_hc_identifiers(db, book, user=user)

    if hc_book_id:
        existing = next((row for row in hits if row["deleted_at"] is None
                         and row["hardcover_book_id"] == hc_book_id), None)
        if existing:
            return existing

    if isbn:
        existing = next((row for row in hits if row["deleted_at"] is None and row["isbn"] == isbn), None)
        if existing:
            return existing

    # Fuzzy match using pre-built title index
    if book.get("authors"):
        norm_title = _normalize_title(book["title"])
        candidates = title_index.get(norm_title, [])
        hc_first = book["authors"].split(",")[0].strip().split()[-1].lower()
        for row in candidates:
            if row["authors"]:
                shelf_first = row["authors"].split(",")[0].strip().split()[-1].lower()
                if shelf_first == hc_first:
                    return require_item_role(db, row["id"], "editor", user=user)

    return next((row for row in hits if row["deleted_at"] is not None), None)


_MERGE_FIELDS = ("subtitle", "description", "series_name", "series_position",
                 "publisher", "page_count", "publish_year", "authors")


def _build_hc_id_updates(book: dict) -> dict:
    """Build Hardcover ID fields to always update on an existing item."""
    updates = {}
    for key in ("hardcover_book_id", "hardcover_edition_id", "hardcover_user_book_id"):
        if book.get(key):
            updates[key] = book[key]
    return updates


def _apply_updates(db, item_id: int, updates: dict, *, user=None):
    """Apply a dict of field updates to an item, through the value funnel."""
    user = user if user is not None else _capture_user()
    with item_write._write_scope(db):
        items_common.authorize_item_targets(db, [item_id], user=user)
        updates = dict(updates)
        ids = {key: updates.pop(key) for key in
               ("hardcover_book_id", "hardcover_edition_id", "hardcover_user_book_id") if key in updates}
        personal = {key: updates.pop(key) for key in user_state.PERSONAL_FIELDS
                    if user is not None and key in updates}
        if updates:
            update_item_fields(db, item_id, updates)
        if ids:
            _save_hc_ids(db, item_id, ids, user=user)
        if personal:
            user_state.save_state(db, user["id"], item_id, **personal)


def _cover_job(item_id: int, book: dict) -> dict:
    return {"item_id": item_id, "isbn": book.get("isbn"), "cover_url": book.get("cover_url")}


def _clean_import_book(book):
    """Keep provider-value cleanup/logging outside the write transaction."""
    book = dict(book)
    raw_isbn = book.get("isbn")
    pair = isbn_svc.canonical_isbn_pair(raw_isbn) if raw_isbn else None
    if raw_isbn and pair is None:
        logger.warning("Hardcover import (%s): %r is not a valid ISBN — stored without one",
                       book.get("title"), raw_isbn)
    book["isbn"], book["isbn10"] = pair or (None, None)
    return book


def _import_single_book_metadata(book: dict, overwrite: bool, title_index: dict, *,
                                 db=None, user=None, library_id=None,
                                 expected_item_id=_UNSET) -> tuple[str, dict | None]:
    """Import metadata for a single book (no network). Returns (status, cover_job_or_None)."""
    user = user if user is not None else _capture_user()
    library_id = library_id or libraries.DEFAULT_LIBRARY_ID
    if db is None:
        book = _clean_import_book(book)
        with get_db() as connection, item_write._write_scope(connection):
            return _import_single_book_metadata(
                book, overwrite, title_index, db=connection, user=user,
                library_id=library_id, expected_item_id=expected_item_id)
    with item_write._write_scope(db):
        catalogue_security.require_library_role(db, library_id, "editor", user=user)
        existing = _find_existing_item(db, book, title_index, user=user)
        actual_id = existing["id"] if existing is not None else None
        if expected_item_id is not _UNSET and actual_id != expected_item_id:
            raise item_write.IdentifierUnavailable("This identifier is unavailable")

        # Live wins (claude-R4 / codex-R1). `hardcover_book_id` is a plain
        # index, not unique, so a trashed row must never beat a live one:
        # every live strategy above runs first, and only when all of them
        # miss does a trashed row sharing the Hardcover id count. A machine
        # re-syncing leaves it alone — skipped before any write.
        if existing is not None and existing["deleted_at"] is not None:
            return ("in_trash", None)

        if existing:
            require_item_role(db, existing["id"], "editor", user=user)
            if not overwrite:
                updates = _build_hc_id_updates(book)
                item = db.execute("SELECT * FROM items_live WHERE id = ?", (existing["id"],)).fetchone()
                item = _account_item(db, item, user)
                for field in _MERGE_FIELDS:
                    if not item[field] and book.get(field):
                        updates[field] = book[field]
                if not item["reading_status"] and book.get("reading_status"):
                    updates["reading_status"] = book["reading_status"]

                _apply_updates(db, existing["id"], updates, user=user)
                series_added = series_memberships_svc.add_metadata_memberships(
                    db, existing["id"], book.get("series_memberships")
                )
                cover_job = None if existing["cover_path"] else _cover_job(existing["id"], book)
                return ("updated" if updates or series_added else "skipped", cover_job)
            else:
                updates = _build_hc_id_updates(book)
                for field in (*_MERGE_FIELDS, "reading_status"):
                    if book.get(field) is not None:
                        updates[field] = book[field]
                _apply_updates(db, existing["id"], updates, user=user)
                series_memberships_svc.add_metadata_memberships(
                    db, existing["id"], book.get("series_memberships")
                )
                return ("updated", _cover_job(existing["id"], book))

        # New item — insert. A Hardcover ISBN is a provider value: pre-clean
        # it per #54 rather than passing it to the funnel raw, which would
        # refuse to insert the whole book over a bad check digit.
        isbn, isbn10 = book.get("isbn"), book.get("isbn10")

        is_owned = 0 if book.get("reading_status") == "want_to_read" else 1

        private = {} if user is not None else {
            "reading_status": book.get("reading_status"),
            "hardcover_user_book_id": book.get("hardcover_user_book_id"),
        }
        try:
            item_id = insert_item(
                db,
                **private,
                library_id=library_id,
                title=book["title"],
                subtitle=book.get("subtitle"),
                authors=book.get("authors"),
                isbn=isbn,
                isbn10=isbn10,
                media_type="book",
                publisher=book.get("publisher"),
                publish_year=book.get("publish_year"),
                page_count=book.get("page_count"),
                description=book.get("description"),
                series_name=book.get("series_name"),
                series_position=book.get("series_position"),
                series_memberships=book.get("series_memberships"),
                source="hardcover",
                owned=is_owned,
                wishlisted=(is_owned == 0),
                hardcover_book_id=book.get("hardcover_book_id"),
                hardcover_edition_id=book.get("hardcover_edition_id"),
                restore_trashed=False,
            )
        except IdentifierInTrash:
            # A trashed ISBN twin. Counted as its own outcome and refused
            # before any write — this arm must sit ahead of the caller's
            # broad `except Exception`, which would count it an error.
            return ("in_trash", None)
        if user is not None:
            user_state.save_state(db, user["id"], item_id,
                                  reading_status=book.get("reading_status"), wishlist=int(not is_owned))
            _save_hc_ids(db, item_id, book, user=user)

    return ("added", _cover_job(item_id, book))
