"""Item helpers shared by the item routers and by services.

`app/routers/items.py` was 2,481 lines and 30 routes — 38% of all router code,
flagged for splitting in two separate code reviews and growing +135 lines
between them. It is now four modules: this one plus `items_covers.py`,
`items_csv.py` and `items_catalog.py`.

What lives here is what more than one of them needs — metadata lookup, the
save path, cover resolution, the scan log — plus the pieces other packages
already reached into `items.py` for (`SORT_OPTIONS` for `pages.py`,
`resolve_missing_cover` for `services/cover_queue.py`, `_log_scan` and
`_save_item` for `store.py` and `intake.py`).

Import the *module* and call through it (`resolve_missing_cover(...)`)
rather than `from ... import resolve_missing_cover`. Tests patch these by
attribute, and a from-import binds a copy that patching cannot reach.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass

import httpx

from fastapi import HTTPException, Request

from app import i18n
from app.config import HTTP_TIMEOUT, MEDIA_TYPES
from app.database import get_db, get_setting
from app.i18n import N_
from app.services import audnexus, covers, detect, metadata_lookup, openlibrary, provider_result
from app.services import cover_queue
from app.services import authors as authors_svc
from app.services import scan_outcome, title_lookup, upcitemdb
from app.services import upc as upc_svc
from app.services import isbn as isbn_svc
from app.services.item_write import ItemValueError, insert_item, update_item_fields
from app.services import item_write  # promote_wishlisted (scan) — call through the module (patchable)
from app.services import catalogue_security, libraries, restore_report
from app.services import tags as tags_svc
from app.services.catalogue_security import require_item_role

logger = logging.getLogger(__name__)
COVER_MEDIA_TYPES = tuple(dict.fromkeys((*cover_queue.COVER_REQUEUE_MEDIA_TYPES, "kids_book")))

SORT_OPTIONS = {
    "newest": (N_("Most Recent"), "i.created_at DESC"),
    "oldest": (N_("Oldest First"), "i.created_at ASC"),
    "title_asc": (N_("Title A\u2013Z"), "i.title COLLATE NOCASE ASC"),
    "title_desc": (N_("Title Z\u2013A"), "i.title COLLATE NOCASE DESC"),
    "author": (N_("Author"), "i.authors COLLATE NOCASE ASC, i.title COLLATE NOCASE ASC"),
    "year_desc": (N_("Year (Newest)"), "(i.publish_year IS NULL), i.publish_year DESC, i.title COLLATE NOCASE ASC"),
    "year_asc": (N_("Year (Oldest)"), "(i.publish_year IS NULL), i.publish_year ASC, i.title COLLATE NOCASE ASC"),
}

def _toast_header(message: str, toast_type: str = "success") -> str:
    return json.dumps({"showToast": {"message": message, "type": toast_type}})


def authorize_item_targets(db, item_ids, *, user=None, library_ids=None):
    """Preflight the COMPLETE selection under the caller's write lock.

    Background callers pass their captured principal and parent bindings, never
    rediscover a broader selection after dropping the request context. A moved,
    hidden, deleted or missing target refuses the job, not a partial success.
    """
    if not db.in_transaction:
        raise RuntimeError("Item authority requires a write scope")
    actor = catalogue_security.current_actor()
    captured = user if user is not None else actor
    if captured is not None:
        if actor is not None and int(captured["id"]) != int(actor["id"]):
            raise HTTPException(403, "Forbidden")
        # A captured HTTP principal is not a permanent administrator grant.
        # Recheck it on this locked connection at producer admission AND after
        # provider awaits, even while the same HTTP actor is installed. Retain the original
        # capability dictionary (especially its bearer cap), never rehydrate a
        # viewer token into an unrestricted account or turn a missing user into
        # trusted service work. Internal explicit-account calls may have no
        # session version; real queued HTTP principals always carry one.
        account = db.execute(
            "SELECT username, role, token_version FROM users WHERE id = ?", (captured["id"],)
        ).fetchone()
        if (account is None
                or captured.get("account_role", captured.get("role")) != account["role"]
                or ("username" in captured and str(captured["username"]).casefold() != account["username"].casefold())
                or ("token_version" in captured and captured["token_version"] != account["token_version"])):
            raise HTTPException(403, "Forbidden")
    bindings = {}
    for item_id in dict.fromkeys(item_ids):
        row = require_item_role(db, item_id, "editor", user=user)
        if row is None or row["deleted_at"] is not None:
            raise HTTPException(404, "Item not found")
        parent = libraries.item_library_id(db, item_id)
        if library_ids is not None and library_ids.get(item_id) != parent:
            raise HTTPException(404, "Item not found")
        bindings[item_id] = parent
    return bindings


def store_item_cover(db, item_id, cover_path, *, edition=None, dismissed=None,
                     user=None, library_ids=None):
    """Keep cover provenance/managed fields in the authoritative item writer."""
    with item_write._write_scope(db):
        authorize_item_targets(db, [item_id], user=user, library_ids=library_ids)
        fields = {"cover_path": cover_path, "cover_edition_isbn": None,
                  "cover_edition_language": None}
        if edition:
            fields.update(cover_edition_isbn=edition[0], cover_edition_language=edition[1])
        if dismissed is not None:
            fields["cover_review_dismissed"] = dismissed
        update_item_fields(db, item_id, fields)


class CoverStage(covers.CoverStage):
    """HTTP adapter: retain exact principal/parent checks at publication."""
    def __init__(self, item_id):
        super().__init__(item_id)
        actor = catalogue_security.current_actor()
        self._captured_user = dict(actor) if actor is not None else None

    def store(self, db, cover_path, **kwargs):
        if kwargs.get("user") is None and self._captured_user is not None:
            kwargs["user"] = self._captured_user
        if not self.owns(cover_path):
            # Established service adapters may return an already stored path.
            # This is a DB-only handoff, never a second filesystem publisher.
            store_item_cover(db, self.item_id, cover_path, **kwargs)
            return cover_path
        return covers.publish_staged_cover(
            db, self, cover_path,
            authorize=lambda conn: authorize_item_targets(
                conn, [self.item_id], user=kwargs.get("user"), library_ids=kwargs.get("library_ids")),
            store=lambda conn, path: store_item_cover(conn, self.item_id, path, **kwargs),
        )


@dataclass(frozen=True)
class _QueuedCoverAuthority:
    user: dict | None
    library_id: int | None


def media_type_candidates(media_type):
    """Recognise retained children's-book identities without retyping them."""
    return ("book", "kids_book") if media_type in ("book", "kids_book") else (media_type,)


async def _lookup_metadata(isbn13: str, hc_token: str | None, client: httpx.AsyncClient,
                           *, google_api_key: str | None = None
                           ) -> tuple[dict | None, str, dict, provider_result.ProviderResult]:
    """Patchable router seam; the coherent cascade is service-owned."""
    return await metadata_lookup.lookup_metadata(
        isbn13, hc_token, client, google_api_key=google_api_key)

def _save_item(metadata: dict, isbn13: str, media_type: str, location_id: int | None,
               source: str, hc_ids: dict, *, owned: bool = True, library_id=None,
               db=None) -> int:
    """Save boundary-validated metadata; derive ISBN10 in the item funnel.

    Ownership/wishlist intent rides the insert/restore, never a later demotion.
    A supplied db keeps the caller's batch transaction; legacy IDs stay intact.
    """
    extra = {} if owned else {"owned": 0, "wishlisted": True}
    if db is None:
        with get_db() as connection:
            return _save_item(metadata, isbn13, media_type, location_id, source, hc_ids,
                              owned=owned, library_id=library_id, db=connection)
    with item_write._write_scope(db):
        # A modern "book" request can name a retained kids_book identity. Do
        # not insert a second row or canonicalise the stored type on restore.
        types = media_type_candidates(media_type)
        matches = db.execute(
            f"SELECT id, media_type, deleted_at FROM items WHERE isbn = ? "
            f"AND media_type IN ({','.join('?' for _ in types)}) "
            "ORDER BY deleted_at IS NOT NULL, (media_type = ?) DESC, id",
            (isbn13, *types, media_type),
        ).fetchall()
        for match in matches:
            try:
                require_item_role(db, match["id"], "editor", include_deleted=True)
            except HTTPException as exc:
                if exc.status_code == 404:
                    raise item_write.IdentifierUnavailable("This identifier is unavailable") from None
                raise
        if matches:
            media_type = matches[0]["media_type"]
            if media_type == "kids_book":
                match = matches[0]
                # The core's new-input alias must not turn a retained identity
                # into a new canonical book or restore a different twin.
                if match["deleted_at"] is None:
                    raise ItemValueError("This identifier is already in the catalogue")
                if library_id is not None and libraries.item_library_id(db, match["id"]) != library_id:
                    raise ItemValueError("This identifier is unavailable in the requested library")
                intent, wishlisted = item_write.restore_intent(extra)
                if not item_write.restore_item(db, match["id"]):
                    raise ItemValueError("This identifier is unavailable")
                item_write.apply_restored_ownership(db, match["id"], intent, wishlisted)
                status = item_write._pending_status()
                if status is not None:
                    item_write._restore_default_status(db, match["id"], status)
                tags_svc.attach_pending(db, match["id"])
                return item_write.ItemId(match["id"], restored=True)
        return insert_item(
            db,
            **extra,
            library_id=library_id,
            title=metadata["title"],
            subtitle=metadata.get("subtitle"),
            authors=metadata.get("authors"),
            isbn=isbn13,
            media_type=media_type,
            publisher=metadata.get("publisher"),
            publish_year=metadata.get("publish_year"),
            page_count=metadata.get("page_count"),
            description=metadata.get("description"),
            series_name=metadata.get("series_name"),
            series_position=metadata.get("series_position"),
            series_memberships=metadata.get("series_memberships"),
            location_id=location_id,
            source=source,
            language=metadata.get("language"),
            hardcover_book_id=hc_ids.get("hardcover_book_id"),
            hardcover_edition_id=hc_ids.get("hardcover_edition_id"),
        )

async def _fetch_preview_cover(isbn13: str, client: httpx.AsyncClient) -> str | None:
    """Try to grab an Amazon cover preview for manual-add fallback."""
    from app.services import outbound

    isbn10 = isbn_svc.isbn13_to_isbn10(isbn13)
    if not isbn10:
        return None
    preview_url = f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01._SCLZZZZZZZ_SX500_.jpg"
    try:
        resp = await outbound.fetch(client, "GET", preview_url, follow_redirects=True)
        if resp.status_code == 200 and len(resp.content) > 1000:
            tmp_path = covers.COVERS_DIR / f"preview_{isbn13}.jpg"
            covers.COVERS_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path.write_bytes(resp.content)
            return f"covers/preview_{isbn13}.jpg"
    except Exception:
        pass
    return None

async def _search_isbn_for_item(title: str, authors: str | None, client) -> tuple[str | None, str | None]:
    """Recover (ISBN, cover URL) by field-scoped title/author/alternate-title search."""
    with get_db() as db:
        search_lang = get_setting(db, "metadata_search_lang") or "en"

    first_author = (authors or "").split(",")[0].strip() or None
    results = await openlibrary.search_by_title_author(title, first_author, client, lang=search_lang)
    for res in results:
        if authors_svc.matches(authors, res.get("authors")):
            return res.get("isbn"), res.get("cover_url")
    return None, None

async def resolve_missing_cover(
    item_id: int, client: httpx.AsyncClient, hints: dict | None = None,
    *, user=None, library_ids=None, _stage=None,
) -> str | None:
    """Find and store a cover for one item that has none.

    An item with an ISBN tries the standard cover chain first (Open Library
    covers -> Amazon -> Google Books). If that fails — or there is no ISBN —
    a title/author search finds the work's best-known edition instead.
    LibraryThing tries verified English related editions only if that fails
    (imported and print-on-demand edition ISBNs often have no cover
    anywhere). A recovered ISBN is stored on ISBN-less items unless another
    item already holds it. An audiobook with an ASIN asks Audnexus for the
    Audible edition's cover after any hint and before the ISBN chain.

    `hints` carries a caller's own cover inputs (`cover_url`, `cover_id`,
    `hardcover_cover_url`) — the scan path passes the ones it already looked
    up, so queueing its download does not change which sources get tried.
    With hints the first attempt runs even for an ISBN-less item, since a
    hinted `cover_url` alone can resolve it. `skip_title_search` is for exact
    provider images on non-book media: if that hinted image fails, the generic
    Open Library title/author recovery path must not guess a book cover.

    Returns the stored cover path, or None if nothing was found. Items that
    already have a cover are left alone.
    """
    hints = dict(hints) if hints else None
    if hints and "_cover_authority" in hints:
        authority = hints.pop("_cover_authority")
        if not isinstance(authority, _QueuedCoverAuthority):
            raise ValueError("Invalid cover job authority")
        user = authority.user if user is None else user
        library_ids = {item_id: authority.library_id} if library_ids is None else library_ids
        hints = hints or None
    if user is None:
        actor = catalogue_security.current_actor()
        user = dict(actor) if actor is not None else None
    if _stage is None:
        with CoverStage(item_id) as stage:
            return await resolve_missing_cover(item_id, client, hints,
                                               user=user, library_ids=library_ids, _stage=stage)
    with get_db() as db:
        with item_write._write_scope(db):
            # Trusted queue work may encounter a since-deleted item. HTTP and
            # explicitly captured jobs must still refuse hidden/moved parents.
            if catalogue_security.current_actor() is None and user is None:
                if not db.execute("SELECT 1 FROM items_live WHERE id = ?", (item_id,)).fetchone():
                    return None
            library_ids = authorize_item_targets(db, [item_id], user=user, library_ids=library_ids)
        row = db.execute(
            "SELECT title, authors, isbn, asin, cover_path, media_type FROM items_live WHERE id = ?",
            (item_id,),
        ).fetchone()
    if not row or row["cover_path"]:
        return None

    cover_path = None
    edition = None
    if hints:
        cover_path = await covers.download_cover(
            _stage.download_id,
            row["isbn"],
            hints.get("cover_url"),
            hints.get("cover_id"),
            client,
            hardcover_cover_url=hints.get("hardcover_cover_url"),
        )
    if not cover_path and row["asin"] and row["media_type"] == "audiobook":
        cover_path = await audnexus.cover_for_item(_stage.download_id, row["asin"], client)
    if not cover_path and not hints and row["isbn"]:
        cover_path = await covers.download_cover(
            _stage.download_id, row["isbn"], None, None, client)

    if not cover_path and hints and hints.get("skip_title_search"):
        return None

    if not cover_path and row["media_type"] in COVER_MEDIA_TYPES:
        found_isbn, cover_url = await _search_isbn_for_item(
            row["title"], row["authors"], client)
        if found_isbn and not row["isbn"]:
            # A provider's ISBN: pre-cleaned, dropped on failure (#54). The
            # `taken` check stays — UNIQUE(isbn, media_type) would raise.
            pair = isbn_svc.canonical_isbn_pair(found_isbn)
            if pair is None:
                logger.info("Recovered ISBN %r for item %s is not a valid ISBN — not stored",
                            found_isbn, item_id)
            else:
                skipped_in_trash = False
                with get_db() as db:
                    with item_write._write_scope(db):
                        authorize_item_targets(db, [item_id], user=user, library_ids=library_ids)
                        # Physical collision probe: never claim a hidden or
                        # trashed identifier, even in an actor-free worker.
                        taken = db.execute("SELECT id FROM items WHERE isbn = ? AND id != ?",
                                           (pair[0], item_id)).fetchone()
                        if not taken:
                            try:
                                update_item_fields(db, item_id, {"isbn": pair[0]})
                            except ItemValueError:
                                skipped_in_trash = True
                if skipped_in_trash:
                    # Logged outside the `with` block — a log handler opening
                    # its own connection would wait on the write lock (G3).
                    logger.info(
                        "Recovered ISBN %r for item %s is held by an item in "
                        "Trash — not stored", pair[0], item_id)
        if cover_url:
            cover_path = await covers.download_cover(
                _stage.download_id, None, cover_url, None, client)
        elif found_isbn and not row["isbn"]:
            cover_path = await covers.download_cover(
                _stage.download_id, found_isbn, None, None, client)

    from app.services import librarything
    if not cover_path and row["media_type"] in librarything.BOOK_TYPES and row["isbn"]:
        with get_db() as db:
            token = get_setting(db, "librarything_token")
            google_key = get_setting(db, "google_books_api_key")
        if token:
            cover_path, edition = await librarything.download_cover(
                _stage.download_id, row["isbn"], token, client, google_key)

    if cover_path:
        with get_db() as db:
            cover_path = _stage.store(
                db, cover_path, edition=(edition["edition_isbn"], edition["edition_language"]) if edition else None,
                user=user, library_ids=library_ids)
    return cover_path

async def _enrich_import_covers(item_ids: list[int], *, user=None, library_ids=None) -> None:
    """Authorize exact import IDs, filter book media (G29), enqueue bound jobs.

    Async/fire-and-forget producer API retained; the queue worker owns downloads.
    No whole-catalogue selection or request ContextVar is handed to that worker.
    """
    user = user if user is not None else catalogue_security.current_actor()
    user = dict(user) if user is not None else None
    eligible = []
    with get_db() as db, item_write._write_scope(db):
        bindings = authorize_item_targets(db, item_ids, user=user, library_ids=library_ids)
        for start in range(0, len(item_ids), cover_queue.FILTER_CHUNK_SIZE):
            chunk = item_ids[start:start + cover_queue.FILTER_CHUNK_SIZE]
            rows = db.execute(
                f"SELECT id, media_type FROM items_live WHERE id IN ({','.join('?' for _ in chunk)})", chunk)
            allowed = {row["id"] for row in rows if row["media_type"] in COVER_MEDIA_TYPES}
            eligible.extend(item_id for item_id in chunk if item_id in allowed)
        # The real queue worker forwards hints to resolve_missing_cover. Carry
        # server-typed authority with each exact ID rather than losing its
        # library binding when a second background handoff occurs.
        for item_id in eligible:
            cover_queue.enqueue(item_id, hints={"_cover_authority": _QueuedCoverAuthority(user, bindings[item_id])})
    queued = len(eligible)
    if queued != len(item_ids):
        logger.info(
            "Queued %d of %d items for cover enrichment (non-book rows skipped)",
            queued, len(item_ids),
        )
    else:
        logger.info("Queued %d items for cover enrichment", queued)

_SCAN_LOG_RETENTION_DAYS = 90

_SCAN_LOG_PRUNE_INTERVAL = 3600  # seconds between prune checks

_scan_log_last_prune: float = float("-inf")  # -inf triggers prune on first call

def _log_scan(isbn: str, media_type: str, result: str, item_id: int | None = None, mode: str = "add"):
    import time
    from app.services import scan_history

    global _scan_log_last_prune
    with get_db() as db:
        scan_history.record_scan(db, isbn, media_type, result, item_id, mode)
        now = time.monotonic()
        if now - _scan_log_last_prune >= _SCAN_LOG_PRUNE_INTERVAL:
            _scan_log_last_prune = now
            db.execute(
                "DELETE FROM scan_log WHERE created_at < datetime('now', ?)",
                (f"-{_SCAN_LOG_RETENTION_DAYS} days",),
            )


# Values are the funnel's job: `item_write.validate_item_fields` refuses an
# unknown media_type on every insert and update, including CSV and archive
# import. This helper survives for the two boundaries that do provider work
# *before* the insert and must refuse `auto` (a scan-form option, never a
# stored type) before the lookup is paid for — `/api/title-search` and
# `/api/books/add`. Written against `MEDIA_TYPES` membership rather than the
# string "auto", so a tampered form is caught by the same line.
def is_valid_media_type(value: str | None) -> bool:
    return value in MEDIA_TYPES


# Re-exported from `services/title_lookup.py`, which carries the reasoning.
UPC_METADATA_PROVIDERS = title_lookup.UPC_METADATA_PROVIDERS


def _find_upc_row(db, upc_key: str, media_type: str):
    """Patchable media-type duplicate guard; races still hit IntegrityError (G31)."""
    return db.execute(
        "SELECT id, title FROM items_live WHERE upc = ? AND media_type = ?",
        (upc_key, media_type),
    ).fetchone()


def _upc_lookup_error(request, templates, upc_norm: str, media_type: str, mode: str, result):
    """Transport failure is an error, not an absent barcode/manual-add result (G47)."""
    logger.warning("Network error looking up UPC %s (%s)", upc_norm, result.outcome)
    _log_scan(upc_norm, media_type, "error", mode=mode)
    return templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {"status": "error", "isbn": upc_norm, "media_type": media_type,
         "message": i18n.gettext("Metadata lookup failed — check connectivity"), "preview_cover": None},
    )


async def _scan_upc(request: Request, templates, upc_code: str, media_type: str, location_id: int | None, platform: str | None = None, mode: str = "add"):
    """Handle UPC barcode scan — look up via UPC Item DB + TMDb (or IGDB for games)."""
    upc_norm = upc_svc.normalize_barcode(upc_code)
    # upc_norm goes to UPC Item DB / TMDb as scanned; upc_key is the canonical
    # EAN-13 form everything in the database is stored and matched on, so the
    # same disc scanned as UPC-A and as EAN-13 dedupes to one row (#20).
    upc_key = upc_svc.normalize_upc(upc_code)

    # Barcode-only short circuit before provider work. Detection has not chosen
    # a type yet. Promotion/authority share the lock (G18); logs stay outside (G3).
    promoted = False
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT id, title, media_type FROM items_live WHERE upc = ?", (upc_key,)
        ).fetchone()
        if existing:
            require_item_role(db, existing["id"], "editor")
        if existing and mode != "wishlist":
            promoted = item_write.promote_wishlisted(db, existing["id"])
    if existing:
        status = "promoted" if promoted else "duplicate"
        _log_scan(upc_norm, existing["media_type"], status, existing["id"], mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": status, "isbn": upc_norm, "title": existing["title"], "item_id": existing["id"]},
        )

    # One shared product lookup before dispatch; transport failure is distinct
    # from an unknown barcode (G47).
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        product_result = await upcitemdb.lookup(upc_norm, client)
    if product_result.outcome == "transport_failed":
        return _upc_lookup_error(request, templates, upc_norm, media_type, mode, product_result)
    product = product_result.payload

    # Detect from the RAW title/category, not a marker-stripped ladder rung (G46).
    hint = media_type
    barcode_type = upc_svc.detect_barcode_type(upc_norm)
    detection = detect.detect_media_type(
        barcode_type,
        hint,
        (product or {}).get("title"),
        (product or {}).get("category"),
    )
    media_type = detection.media_type
    detect_reason = detection.reason
    detect_overrode = media_type != hint

    # Video games: the record above, then IGDB for metadata.
    if media_type == "video_game":
        return await _scan_upc_game(
            request, templates, upc_norm, product, location_id, platform, mode,
            detect_reason=detect_reason, detect_overrode=detect_overrode,
            product_result=product_result,
        )

    # Hardware resolves to DVD but must not ask TMDb to guess a film (G46).
    is_hardware = detection.signal == "hardware"

    # Non-provider media (e.g. CD) is filed title-only, never searched as a film.
    no_metadata_provider = media_type not in UPC_METADATA_PROVIDERS

    # Get TMDb API key
    with get_db() as db:
        tmdb_key = get_setting(db, "tmdb_api_key")

    metadata = None
    # Preserve the no-credential state when no ladder runs.
    tmdb_result = provider_result.no_credential("tmdb")
    # A 200 can still carry a missing, blank or format-only title ("[DVD]"),
    # which normalises to no queries at all. That is a not_found, not an index
    # error on queries[0].
    queries: list[str] = upcitemdb.search_queries((product or {}).get("title") or "")
    if queries and tmdb_key and not no_metadata_provider and not is_hardware:
        # Metadata outages are ProviderResult states, not product-lookup errors.
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            tmdb_result, _matched = await _first_hit(
                queries,
                lambda q: title_lookup.lookup_by_title(
                    q, "dvd", client, creds={"tmdb_api_key": tmdb_key}),
            )
            if tmdb_result.outcome == "rejected":
                # The item is still filed, title-only, as before.
                logger.warning(
                    "TMDb rejected the configured key for UPC %s — filing title only",
                    upc_norm,
                )
            if tmdb_result.found:
                metadata = tmdb_result.payload

    if not queries:
        # Project the PRODUCT outcome here: its 429 yielded no title/ladder.
        _log_scan(upc_norm, media_type, "not_found", mode=mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "not_found", "isbn": upc_norm, "media_type": media_type,
             "message": i18n.gettext("Not found — add manually below"), "preview_cover": None,
             "enrich_status": scan_outcome.not_found_status(product_result),
             "locations": _manual_form_locations()},
        )

    # Do NOT combine the found product into this enrichment state: found-wins
    # would hide TMDb misses/rejections/429s (#42/#44). Hardware has no lookup.
    enrich_status = "no_lookup" if is_hardware else scan_outcome.enrich_status(
        tmdb_result, has_provider=not no_metadata_provider
    )
    # Never name a provider that was not applicable.
    enrich_provider = None if (no_metadata_provider or is_hardware) else "TMDb"

    # Provider provenance claims a real hit, not a UPC title-only fallback.
    source = "tmdb" if metadata else "upc"

    if metadata is None:
        # No provider hit, or no key: file the *cleaned* title rather than the
        # raw retail string. The item is still created when enrichment yields
        # nothing — that has always been the contract.
        metadata = {
            "title": queries[0], "description": None,
            "publish_year": None, "cover_url": None,
        }

    # --- Duplicate check, part 2 of 2: media_type-keyed, under the write lock.
    #
    # `G18` — this is a guard-then-write route. The barcode-alone pre-check at
    # the top ran *before* the whole lookup window, so a rival scan of the same
    # barcode has had every one of those milliseconds to commit. BEGIN
    # IMMEDIATE takes the write lock before the guard query, so the check and
    # the insert see one consistent state, and the IntegrityError catch turns
    # a lost race into the duplicate card rather than a 500 — matching
    # `add_manual_item` (`items.py`, `TestIntegrityErrorGuard`).
    existing = None
    item_id = None
    value_error = None
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = _find_upc_row(db, upc_key, media_type)
        if existing:
            require_item_role(db, existing["id"], "editor")
        if existing is None:
            try:
                item_id = insert_item(
                    db,
                    title=metadata["title"],
                    description=metadata.get("description"),
                    media_type=media_type,
                    publish_year=metadata.get("publish_year"),
                    location_id=location_id,
                    upc=upc_key,
                    source=source,
                    # Was a follow-up UPDATE in a second transaction; owned is
                    # an item-creation field, so it belongs in the insert.
                    owned=0 if mode == "wishlist" else 1,
                    wishlisted=(mode == "wishlist"),
                )
            except ItemValueError as e:  # a stale location (#54)
                value_error = str(e)
            except sqlite3.IntegrityError:
                existing = _find_upc_row(db, upc_key, media_type)
                if existing is None:
                    raise
                require_item_role(db, existing["id"], "editor")
        promoted = bool(existing) and mode != "wishlist" and item_write.promote_wishlisted(db, existing["id"])

    # _log_scan opens its own connection, so it must run outside the write
    # transaction above or it blocks on the lock that block still holds.
    if value_error:
        _log_scan(upc_norm, media_type, "error", None, mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "error", "isbn": upc_norm, "message": value_error},
        )
    if existing:
        status = "promoted" if promoted else "duplicate"
        _log_scan(upc_norm, media_type, status, existing["id"], mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": status, "isbn": upc_norm, "title": existing["title"],
             "item_id": existing["id"]},
        )

    with get_db() as db, item_write._write_scope(db):
        cover_bindings = authorize_item_targets(db, [item_id])
    # Download cover — skipped entirely on a restored row that already has
    # one, so the user's own cover survives the re-add (and no outbound call
    # is made for a cover we would then discard).
    cover_path = None
    if metadata.get("cover_url") and not restore_report.keeps_stored_cover(item_id):
        with CoverStage(item_id) as stage:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                cover_path = await covers._download_to_item(stage.download_id, metadata["cover_url"], client)
            if cover_path:
                with get_db() as db:
                    cover_path = stage.store(db, cover_path, library_ids=cover_bindings)

    status = restore_report.restored_status(item_id, "wishlisted" if mode == "wishlist" else "added")
    _log_scan(upc_norm, media_type, status, item_id, mode)

    resp = templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {
            "status": status, "isbn": upc_norm,
            **({"title": metadata["title"], "authors": None,
                "cover_path": cover_path}
               if status != "restored" else restore_report.restored_card(item_id)),
            "item_id": item_id,
            "source": source, "media_type_label": i18n.gettext(MEDIA_TYPES.get(media_type, media_type)),
            "media_type": media_type,
            # T5 renders these; T4 only has to carry them.
            "detect_reason": detect_reason, "detect_overrode": detect_overrode,
            "enrich_status": enrich_status, "enrich_provider": enrich_provider,
        },
    )
    return resp

async def _first_hit(queries, search):
    """Return (ProviderResult, matched query), using metadata-dict payloads (G45).

    Rejection, quota and transport failures stop the shared ladder; retrying
    shorter queries cannot repair credentials/quota/network (G46/G47). Misses
    combine without obscuring provider outcomes.
    """
    results = []
    for query in queries:
        result = await search(query)
        if result.found:
            return result, query
        results.append(result)
        if result.outcome in ("rejected", "rate_limited", "transport_failed"):
            return result, None
    return provider_result.combine(results, provider="ladder"), None


async def _scan_upc_game(request: Request, templates, upc_norm: str, product: dict | None, location_id: int | None, platform: str | None = None, mode: str = "add", detect_reason: str = "", detect_overrode: bool = False, product_result: provider_result.ProviderResult | None = None):
    """Use the caller's product/outcome (no second lookup), then IGDB's ladder."""
    queries = upcitemdb.search_queries((product or {}).get("title") or "")

    if not queries:
        # A product 429 leaves no title; report its state, not a metadata miss.
        _log_scan(upc_norm, "video_game", "not_found", mode=mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "not_found", "isbn": upc_norm, "media_type": "video_game",
             "message": i18n.gettext("Not found — add manually below"), "preview_cover": None,
             "enrich_status": scan_outcome.not_found_status(product_result),
             "locations": _manual_form_locations()},
        )

    # Step 2: Search IGDB for metadata using that title
    with get_db() as db:
        igdb_id = get_setting(db, "igdb_client_id")
        igdb_secret = get_setting(db, "igdb_client_secret")

    metadata = None
    # The ladder requires BOTH credentials (G49).
    igdb_result = provider_result.no_credential("igdb")
    if igdb_id and igdb_secret:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            # IGDB's list payload is unwrapped inside the helper now (G45).
            # `platform` stays unforwarded: this ladder never filtered on it.
            igdb_result, _matched = await _first_hit(
                queries,
                lambda q: title_lookup.lookup_by_title(
                    q, "video_game", client,
                    creds={"igdb_client_id": igdb_id, "igdb_client_secret": igdb_secret}),
            )
            if igdb_result.outcome == "rejected":
                logger.warning(
                    "IGDB rejected the configured credentials for UPC %s — filing title only",
                    upc_norm,
                )
            if igdb_result.found:
                metadata = igdb_result.payload

    # Do not fold a found product into enrichment: it hides IGDB failures/429s.
    enrich_status = scan_outcome.enrich_status(igdb_result)

    # Save item — with IGDB metadata if found, otherwise the cleaned UPC title
    source = "igdb" if metadata else "upc"
    game_title = metadata["title"] if metadata else queries[0]

    # Locked duplicate/authority guard, with the IntegrityError backstop (G18).
    upc_key = upc_svc.normalize_upc(upc_norm)
    existing = None
    item_id = None
    value_error = None
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = _find_upc_row(db, upc_key, "video_game")
        if existing:
            require_item_role(db, existing["id"], "editor")
        if existing is None:
            try:
                item_id = insert_item(
                    db,
                    title=game_title,
                    description=metadata.get("description") if metadata else None,
                    media_type="video_game",
                    publisher=metadata.get("publisher") if metadata else None,
                    publish_year=metadata.get("publish_year") if metadata else None,
                    series_name=metadata.get("series_name") if metadata else None,
                    platform=platform,
                    location_id=location_id,
                    upc=upc_key,
                    source=source,
                    owned=0 if mode == "wishlist" else 1,
                    wishlisted=(mode == "wishlist"),
                )
            except ItemValueError as e:  # unknown platform or stale location (#54)
                value_error = str(e)
            except sqlite3.IntegrityError:
                existing = _find_upc_row(db, upc_key, "video_game")
                if existing is None:
                    raise
                require_item_role(db, existing["id"], "editor")
        promoted = bool(existing) and mode != "wishlist" and item_write.promote_wishlisted(db, existing["id"])

    # Outside the write transaction — _log_scan opens its own connection.
    if value_error:
        _log_scan(upc_norm, "video_game", "error", None, mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "error", "isbn": upc_norm, "message": value_error},
        )
    if existing:
        status = "promoted" if promoted else "duplicate"
        _log_scan(upc_norm, "video_game", status, existing["id"], mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": status, "isbn": upc_norm, "title": existing["title"],
             "item_id": existing["id"]},
        )

    with get_db() as db, item_write._write_scope(db):
        cover_bindings = authorize_item_targets(db, [item_id])
    # Download cover — skipped on a restored row that already has one, the
    # same rule as the film branch above.
    cover_path = None
    cover_url = metadata.get("cover_url") if metadata else None
    if cover_url and not restore_report.keeps_stored_cover(item_id):
        with CoverStage(item_id) as stage:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                cover_path = await covers._download_to_item(stage.download_id, cover_url, client)
            if cover_path:
                with get_db() as db:
                    cover_path = stage.store(db, cover_path, library_ids=cover_bindings)

    status = restore_report.restored_status(item_id, "wishlisted" if mode == "wishlist" else "added")
    _log_scan(upc_norm, "video_game", status, item_id, mode)

    resp = templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {
            "status": status, "isbn": upc_norm,
            **({"title": game_title,
                "authors": metadata.get("developer") if metadata else None,
                "cover_path": cover_path}
               if status != "restored" else restore_report.restored_card(item_id)),
            "item_id": item_id,
            "source": source, "media_type_label": i18n.gettext("Video Game"), "media_type": "video_game",
            # T5 renders these; T4 only has to carry them.
            "detect_reason": detect_reason, "detect_overrode": detect_overrode,
            "enrich_status": enrich_status, "enrich_provider": "IGDB",
        },
    )
    return resp


def _manual_form_locations():
    """Location options only for scan_result's not_found/manual-add form (#19)."""
    with get_db() as db:
        return db.execute("SELECT id, name FROM locations ORDER BY sort_order, name").fetchall()
