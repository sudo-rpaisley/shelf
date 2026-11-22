"""Shelf-photo bulk intake: analyze a photo of spines, confirm rows into items."""

import asyncio
import logging
import os
import sqlite3
from contextvars import Context

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, field_validator

from app import i18n
from app.features import require_feature
from app.config import (
    HTTP_TIMEOUT,
    LOW_RES_LONG_EDGE,
    MEDIA_TYPES,
    TILING_THRESHOLD,
    canonical_media_type,
)
from app.database import get_db, get_all_settings, get_setting
from app.services import cover_queue, covers, openlibrary, restore_report, tiling, title_lookup, vision
from app.services import isbn as isbn_svc
from app.services import authors as authors_svc
from app.services import national
from app.services import item_write
from app.services import catalogue_security, libraries
from app.services.catalogue_security import require_catalogue_role, require_item_role
from app.services import tags as tags_svc
from app.services.title_match import titles_agree, titles_match_exactly
from app.services.item_write import ItemValueError, insert_item
from app.services.write_targets import UnknownLocationError, validated_location_id

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/intake",
    dependencies=[Depends(require_catalogue_role("editor")), Depends(require_feature("intake"))],
)

MAX_PHOTO_DIMENSION = 100_000  # sanity bound on client-reported pixels
_LOCATION_ERROR = "Selected location no longer exists — choose another location"

# Same five types cover enrichment treats as the book catalogue.
BOOK_SEARCH_MEDIA_TYPES = tuple(dict.fromkeys((*cover_queue.COVER_REQUEUE_MEDIA_TYPES, "kids_book")))


class PlanRequest(BaseModel):
    width: int
    height: int


@router.post("/plan")
async def plan_photo(payload: PlanRequest):
    """Pre-upload plan: downscale factor, tile grid, and cost estimates.

    The client sends only the photo dimensions; the grid geometry and the
    per-provider ingest caps stay server-side so no provider logic leaks
    into the UI. Cropping happens in the browser from these rects. The
    low-res judgement is computed here too, for the same reason -- the UI
    renders the flag, it never computes it.
    """
    w, h = payload.width, payload.height
    if not (0 < w <= MAX_PHOTO_DIMENSION and 0 < h <= MAX_PHOTO_DIMENSION):
        return {"ok": False, "message": i18n.gettext("Invalid photo dimensions")}

    with get_db() as db:
        settings = get_all_settings(db)
    if not settings.get("vision_provider"):
        return {"ok": False, "message": i18n.gettext("No vision provider configured")}

    cap = tiling.ingest_cap(settings)
    factor = tiling.downscale_factor(w, h, cap)
    tiles = tiling.compute_grid(w, h, cap)
    books = tiling.expected_books(w, h)
    preview_w, preview_h = tiling.scaled_dims(w, h, cap)
    needs_choice = factor >= TILING_THRESHOLD
    low_res = (not needs_choice) and max(w, h) < LOW_RES_LONG_EDGE
    return {
        "ok": True,
        "factor": round(factor, 2),
        "needs_choice": needs_choice,
        "low_res": low_res,
        "low_res_long_edge": LOW_RES_LONG_EDGE,
        "preview": {"w": preview_w, "h": preview_h},
        "tiles": [{"x": t.x, "y": t.y, "w": t.w, "h": t.h} for t in tiles],
        "grid": {"rows": max(t.row for t in tiles) + 1, "cols": max(t.col for t in tiles) + 1},
        "cost_as_is_usd": tiling.estimate_cost_usd([(w, h)], settings, books),
        "cost_tiled_usd": tiling.estimate_cost_usd(
            [(t.w, t.h) for t in tiles], settings, books),
    }


@router.post("/analyze")
async def analyze_photo(photos: list[UploadFile] = File(...)):
    """Run the configured vision provider over an uploaded shelf photo.

    One file is the normal path; multiple files are overlapping tiles of a
    single photo, cropped client-side in reading order (see /plan).
    """
    images: list[tuple[bytes, str]] = []
    parts: list[str] = []
    total_bytes = 0
    for photo in photos:
        mime = (photo.content_type or "").lower()
        if mime not in vision.ALLOWED_MIME:
            return {"ok": False, "message": i18n.gettext("Please upload a JPEG, PNG, or WebP photo")}
        image_bytes = await photo.read()
        if len(image_bytes) > vision.MAX_IMAGE_BYTES:
            return {"ok": False, "message": i18n.gettext("Photo is too large (max 10 MB)")}
        if not image_bytes:
            return {"ok": False, "message": i18n.gettext("Empty upload")}
        # Stop reading further tiles the moment the combined budget is spent,
        # rather than reading every file first and checking at the end.
        total_bytes += len(image_bytes)
        if total_bytes > vision.MAX_TOTAL_INTAKE_BYTES:
            return {"ok": False, "message": i18n.gettext("This upload is too large to analyze (over 300 MB in total)")}
        images.append((image_bytes, mime))
        parts.append(f"{photo.filename or '-'} {mime} {len(image_bytes)} B")
    if not images:
        return {"ok": False, "message": i18n.gettext("Empty upload")}

    logger.info("Intake analyze: %d photo(s) — %s", len(parts), ", ".join(parts))

    with get_db() as db:
        settings = get_all_settings(db)

    try:
        books = await vision.detect_spines(images, settings)
    except vision.VisionError as e:
        return {"ok": False, "message": str(e)}

    if not books:
        return {"ok": False, "message": i18n.gettext("No books were recognized in this photo")}
    return {"ok": True, "books": books}


class IntakeBook(BaseModel):
    title: str
    authors: str | None = None
    isbn: str | None = None  # parsed and carried only — T6 wires the cascade
    media_type: str = "book"

    @field_validator("media_type")
    @classmethod
    def _known_media_type(cls, v):
        # Canonicalise before the membership test and return the canonical
        # value, so a stale intake plan holding a retired type validates and
        # every later step — the dupe guard included — sees one spelling.
        v = canonical_media_type(v)
        if v not in MEDIA_TYPES:
            raise ValueError(f"Unknown media_type: {v!r}")
        return v


class IntakeConfirm(BaseModel):
    books: list[IntakeBook]
    location_id: int | None = None
    owned: bool = True
    library_id: int | None = None
    tags: list[str] = []


def _isbn_taken(isbn13: str, media_type: str, *, db=None, hit_ids=None) -> bool:
    """True when (isbn13, media_type) is already in the library.

    The advisory pre-provider check opens a read-only connection. Constraint
    classification instead supplies the still-locked batch connection, after
    the failed row's savepoint has rolled back. Authorize ALL retained twins,
    including Trash, before disclosing a live duplicate.
    """
    from app.routers import items_common

    if db is None:
        with get_db() as connection:
            return _isbn_taken(isbn13, media_type, db=connection, hit_ids=hit_ids)
    types = items_common.media_type_candidates(media_type)
    hits = db.execute(
        f"SELECT id, deleted_at FROM items WHERE isbn = ? AND media_type IN ({','.join('?' for _ in types)})",
        (isbn13, *types),
    ).fetchall()
    for hit in hits:
        _authorize_intake_collision(db, hit["id"])
    if hit_ids is not None:
        hit_ids.extend(hit["id"] for hit in hits)
    return any(hit["deleted_at"] is None for hit in hits)


def _authorize_intake_collision(db, item_id):
    try:
        require_item_role(db, item_id, "editor", include_deleted=True)
    except HTTPException as exc:
        if exc.status_code in (403, 404):
            raise item_write.IdentifierUnavailable("This identifier is unavailable") from None
        raise


def _title_taken(db, title: str, authors: str, media_type: str, *, hit_ids=None) -> bool:
    """True when (title, authors, media_type) is already in the library.

    Never acquires a write lock itself: step 1 is an advisory read whose
    connection closes before any provider await. The deciding re-check uses
    the caller's outer write scope (G18), not a second connection.
    """
    from app.routers import items_common

    types = items_common.media_type_candidates(media_type)
    hits = db.execute(
        "SELECT id FROM items_live WHERE title = ? COLLATE NOCASE "
        f"AND IFNULL(authors, '') = ? COLLATE NOCASE AND media_type IN ({','.join('?' for _ in types)})",
        (title, authors, *types),
    ).fetchall()
    for hit in hits:
        _authorize_intake_collision(db, hit["id"])
    if hit_ids is not None:
        hit_ids.extend(hit["id"] for hit in hits)
    return bool(hits)


def _prechecked_plan(book, reason, hit_ids):
    """Spare provider work, but retain skipped identities for locked preflight.

    A later row can await a provider after this advisory read. Re-check even
    skipped candidates before any batch mutation, without turning a vanished
    duplicate into an unresearched insertion.
    """
    title = book.title.strip()
    return {
        "isbn": vision.clean_isbn(book.isbn), "media_type": book.media_type,
        "query_title": title, "query_authors": book.authors or "",
        "entry": {"title": title}, "skip_reason": reason, "cover_url": None,
        "prechecked_ids": hit_ids,
    }


async def _confirm_one(
    book: IntakeBook, client: httpx.AsyncClient, search_lang: str, preferred_marc: str,
    location_id: int | None, owned: bool, hc_token: str | None,
    google_api_key: str | None, creds: dict[str, str | None],
) -> tuple[str, dict, int | None]:
    """Resolve one confirmed row without writing catalogue metadata.

    Returns ("prepared", plan, None); even advisory duplicates join the final
    locked, all-target preflight. No catalogue mutation precedes provider I/O.
    """
    title = book.title.strip()
    media_type = book.media_type

    # 1. Title+authors pre-check, scoped to media type — and *only* a
    # pre-check. It exists to spare a plainly-owned row the two paced outbound
    # lookups below (steps 2 and 3); a photo of thirty owned books would
    # otherwise spend thirty provider calls to learn nothing. It runs on its
    # own connection, outside the write lock, deliberately: taking the lock
    # here would hold SQLite's single writer across up to HTTP_TIMEOUT of
    # network I/O, and the logger.warning calls in that window are the G3 trap.
    # **It decides nothing.** The re-check inside the insert block below runs
    # the same query under BEGIN IMMEDIATE and is the guard that decides
    # (G18). Do not collapse this into that one as a duplicate, and do not
    # promote it back into the decision.
    hit_ids = []
    with get_db() as db:
        if _title_taken(db, title, book.authors or "", media_type, hit_ids=hit_ids):
            return "prepared", _prechecked_plan(book, "already in library", hit_ids), None

    # 2. A printed ISBN, if one survives re-validation, buys the full scan
    # cascade. The client value is re-cleaned server-side — the review field
    # is editable and nothing about it is trusted.
    printed_isbn13 = vision.clean_isbn(book.isbn)
    if printed_isbn13:
        if _isbn_taken(printed_isbn13, media_type, hit_ids=hit_ids):
            return "prepared", _prechecked_plan(book, "ISBN already in library", hit_ids), None

        # Deferred import, the store.py precedent — the item routers never
        # import intake. Call through the module so tests can patch it.
        from app.routers import items_common

        metadata, hc_ids = None, {}
        try:
            # Trailing `_` = the rate-limited flag. Photo Intake's confirm step
            # has no scan card to render it on — deliberate, not an oversight.
            metadata, _, hc_ids, _ = await items_common._lookup_metadata(
                printed_isbn13, hc_token, client, google_api_key=google_api_key
            )
        except Exception:
            logger.warning("Intake: metadata lookup failed for ISBN %s", printed_isbn13)
            metadata, hc_ids = None, {}

        if metadata:
            if titles_agree(title, metadata.get("title")):
                return "prepared", {
                    "metadata": metadata, "isbn": printed_isbn13, "hc_ids": hc_ids,
                    "media_type": media_type, "query_title": title,
                    "query_authors": book.authors or "", "cover_url": None,
                    "entry": {"title": metadata["title"], "matched": True, "lookup": "matched"},
                }, None

            # 6b. The cascade resolved the identifier and it names a different
            # book, so the identifier is known untrusted. Clear it rather than
            # merely declining to use it: resolve_missing_cover tries a stored
            # isbn first and reads its presence as trust, so a persisted one
            # would fetch the other book's cover in the background.
            logger.warning(
                "Intake: printed ISBN %s names %r, row says %r — discarding",
                printed_isbn13, metadata.get("title"), title)
            printed_isbn13 = None
        # 6a. Cascade miss or transport failure: nothing has contradicted the
        # printed digits, so they stay and seed the fallback INSERT below.

    # 3. Weak path, by media type. The book family is searched on Open Library
    # by title+author. A type with a *metadata* provider — DVD and video game,
    # per UPC_METADATA_PROVIDERS — is asked that provider for its one best
    # match, which is trusted only behind a strict exact-title guard. Anything
    # else (a CD, for want of a music provider) still gets a title-only insert
    # with no outbound call at all. `BOOK_SEARCH_MEDIA_TYPES` also aliases the
    # cover-queue hand-off at the bottom of `confirm_books`, which stays
    # book-only on purpose (G29) — widening this branch did not widen that.
    meta = {}
    # Three states, distinct from `matched`: "matched" (a provider answered and
    # we filed it), "declined" (a provider answered and the guard refused it),
    # "not_attempted" (everything else). The template holds the copy (G58).
    lookup = "not_attempted"
    # Set only on an accepted UPC_METADATA_PROVIDERS hit, below. Books keep
    # going through the cover queue (G29) — this is the disc/game path only.
    cover_url = None
    if media_type in BOOK_SEARCH_MEDIA_TYPES:
        # Enrich via Open Library field-scoped search (same guard as
        # imports); prefer the configured search-language works so
        # translated editions don't win just by ranking first
        try:
            results = await openlibrary.search_by_title_author(
                title, (book.authors or "").split(",")[0].strip() or None, client,
                lang=search_lang)
            matches = [r for r in results if authors_svc.matches(book.authors, r.get("authors"))]
            preferred = [r for r in matches if preferred_marc in (r.get("languages") or [])]
            if preferred or matches:
                meta = (preferred or matches)[0]
        except httpx.HTTPError:
            logger.debug("Intake metadata search failed for %r", title)
    elif media_type in title_lookup.UPC_METADATA_PROVIDERS:
        # One paced request per row; no credentials means no call at all. The
        # helper never raises and never hands back a list (G45, G66), so there
        # is nothing to catch here and no 500 to make.
        result = await title_lookup.lookup_by_title(
            title, media_type, client, creds=creds)
        if result.outcome == "rejected":
            # Named provider and row title only — never a URL. TMDb's v3 key
            # rides in `?api_key=`, and this logger carries no redaction
            # filter (G76).
            logger.warning(
                "Intake: %s rejected the configured credentials for %r — filing title only",
                result.provider, title)
        if result.found:
            candidate = result.payload
            # The helper asked with `limit=1`, so this is the provider's first
            # guess, not a record it vouches for — this guard is the only thing
            # between it and the database (G74). Strict on purpose: the book
            # guard `titles_agree` accepts 'Dune' for *Dune: Part Two*.
            if titles_match_exactly(title, candidate.get("title")):
                meta = candidate
                # The catalogue's title is the record; the row's was the query.
                title = candidate.get("title") or title
                cover_url = candidate.get("cover_url")
            else:
                lookup = "declined"
                logger.info(
                    "Intake: %s answered %r for row %r — title does not match, filing title only",
                    result.provider, candidate.get("title"), title)

    # Edition language: preferred-language match wins, else map the
    # chosen result's first language code, else unknown.
    language = None
    if meta:
        meta_langs = meta.get("languages") or []
        if preferred_marc in meta_langs:
            language = national.to_iso639_1(preferred_marc)
        elif meta_langs:
            language = national.to_iso639_1(meta_langs[0])

    # A surviving printed ISBN names the physical copy; Open Library's names
    # its best-known edition. The printed one wins. `isbn10` is no longer
    # tracked here — the funnel derives it from `isbn` on insert.
    isbn13 = None
    if printed_isbn13:
        isbn13 = printed_isbn13
    elif meta.get("isbn"):
        # A provider's (Open Library's) ISBN: pre-cleaned, dropped on a bad
        # check digit rather than refused (#54) — this is a weak-path hint,
        # not a value the user typed.
        pair = isbn_svc.canonical_isbn_pair(meta["isbn"])
        if pair is None:
            logger.info(
                "Intake: Open Library ISBN %r for %r is not a valid ISBN — not stored",
                meta["isbn"], title,
            )
        else:
            isbn13 = pair[0]

    if meta:
        lookup = "matched"
    return "prepared", {
        "fields": {"title": title, "authors": book.authors or meta.get("authors"),
                   "isbn": isbn13, "media_type": media_type,
                   "publisher": meta.get("publisher"), "publish_year": meta.get("publish_year"),
                   "page_count": meta.get("page_count"), "description": meta.get("description"),
                   "series_name": meta.get("series_name"), "language": language},
        "isbn": isbn13, "media_type": media_type, "query_title": book.title.strip(),
        "query_authors": book.authors or "", "cover_url": cover_url,
        "entry": {"title": title, "matched": bool(meta), "lookup": lookup},
    }, None


def _preflight_plan(db, plan, library_id=None):
    """Authorize every identity; return its collision kind, not a boolean.

    Do not short-circuit title/ISBN authority checks after finding a duplicate.
    A hidden or read-only twin must refuse the whole batch before any write.
    """
    from app.routers import items_common

    for item_id in plan.get("prechecked_ids", ()):
        _authorize_intake_collision(db, item_id)
    types = items_common.media_type_candidates(plan["media_type"])
    hits = db.execute(
        f"SELECT id, deleted_at, media_type FROM items WHERE isbn = ? AND media_type IN ({','.join('?' for _ in types)})",
        (plan["isbn"], *types),
    ).fetchall() if plan["isbn"] else []
    for hit in hits:
        _authorize_intake_collision(db, hit["id"])
    if hits:
        # Match the canonical save helper's deterministic preference, without
        # retyping, consolidating or deleting either retained identity.
        selected = min(hits, key=lambda hit: (
            hit["deleted_at"] is not None,
            hit["media_type"] != plan["media_type"], hit["id"]))
        plan["media_type"] = selected["media_type"]
        if "fields" in plan:
            plan["fields"]["media_type"] = selected["media_type"]
    isbn_duplicate = any(hit["deleted_at"] is None for hit in hits)
    if not isbn_duplicate and library_id is not None:
        for hit in hits:
            if libraries.item_library_id(db, hit["id"]) != library_id:
                raise ItemValueError("This identifier is unavailable in the requested library")
    title_duplicate = _title_taken(db, plan["query_title"], plan["query_authors"], plan["media_type"])
    resolved_title = plan["entry"]["title"]
    if resolved_title != plan["query_title"]:
        rows = db.execute(
            f"SELECT id FROM items_live WHERE title = ? COLLATE NOCASE AND media_type IN ({','.join('?' for _ in types)})",
            (resolved_title, *types),
        ).fetchall()
        for row in rows:
            _authorize_intake_collision(db, row["id"])
        title_duplicate |= bool(rows)
    if isbn_duplicate:
        return "ISBN already in library"
    return "already in library" if title_duplicate else None


@router.post("/confirm")
async def confirm_books(payload: IntakeConfirm):
    """Insert confirmed candidates as items via the normal metadata pipeline."""
    try:
        with get_db() as db:
            library_id = payload.library_id or libraries.DEFAULT_LIBRARY_ID
            catalogue_security.require_library_role(db, library_id, "editor")
            location_id = validated_location_id(db, payload.location_id)
    except UnknownLocationError:
        return {"ok": False, "message": _LOCATION_ERROR}

    added, skipped = [], []
    new_item_ids: list[int] = []
    plans = []
    cover_jobs = []

    with get_db() as db:
        search_lang = get_setting(db, "metadata_search_lang") or "en"
        # get_setting, never get_all_settings — the latter drops env-only keys (G15).
        hc_token = get_setting(db, "hardcover_token") or None
        google_api_key = get_setting(db, "google_books_api_key") or None
        # The metadata providers a typed disc or game row is looked up
        # against. Read here, once per confirm, never inside the row loop —
        # and `get_setting` per key, because all three can be env-only.
        creds = {
            "tmdb_api_key": get_setting(db, "tmdb_api_key") or None,
            "igdb_client_id": get_setting(db, "igdb_client_id") or None,
            "igdb_client_secret": get_setting(db, "igdb_client_secret") or None,
        }
    preferred_marc = national.iso_to_marc(search_lang)

    with tags_svc.default_tags(";".join(payload.tags)):
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            for book in payload.books:
                title = book.title.strip()
                if not title:
                    continue

                try:
                    status, entry, item_id = await _confirm_one(
                        book, client, search_lang, preferred_marc, location_id,
                        payload.owned, hc_token, google_api_key, creds)
                except ItemValueError as e:
                    # A stale location is caught by the boundary check above and
                    # never reaches here; this guards a field _confirm_one has
                    # not yet boundary-checked (G47 — see the forced-raise pin).
                    if isinstance(e, item_write.IdentifierUnavailable):
                        return {"ok": False, "message": str(e), "added": [], "skipped": []}
                    skipped.append({"title": title, "reason": str(e)})
                    continue
                if status == "prepared":
                    plans.append(entry)
                else:
                    skipped.append(entry)

            from app.routers import items_common
            try:
                with get_db() as db, item_write._write_scope(db):
                    catalogue_security.require_library_role(db, library_id, "editor")
                    location_id = validated_location_id(db, location_id)
                    duplicates = [_preflight_plan(db, plan, library_id) for plan in plans]
                    for plan, duplicate in zip(plans, duplicates):
                        # Until an earlier row succeeds, nothing can invalidate
                        # this decision: the outer write lock is still held.
                        # Later rows must see identities inserted/restored by
                        # this batch, not just the original preflight snapshot.
                        if new_item_ids:
                            duplicate = _preflight_plan(db, plan, library_id)
                        reason = duplicate or plan.get("skip_reason")
                        if reason:
                            skipped.append({"title": plan["entry"]["title"], "reason": i18n.gettext(reason)})
                            continue
                        try:
                            # Per-row canonical savepoint, still inside the
                            # all-target batch scope. Recoverable failures must
                            # roll back all row effects before classification.
                            with item_write._write_scope(db):
                                if "metadata" in plan or plan["media_type"] == "kids_book":
                                    item_id = items_common._save_item(
                                        plan.get("metadata", plan.get("fields")), plan["isbn"], plan["media_type"], location_id,
                                        "photo_intake", plan.get("hc_ids", {}), owned=payload.owned,
                                        library_id=library_id, db=db)
                                else:
                                    item_id = insert_item(db, **plan["fields"], location_id=location_id,
                                                          library_id=library_id, owned=int(payload.owned),
                                                          wishlisted=not payload.owned, source="photo_intake")
                        except sqlite3.IntegrityError as exc:
                            # Only this exact native constraint can represent a
                            # canonical ISBN duplicate. FK/CHECK/other UNIQUE
                            # faults refuse and roll back the complete batch.
                            if (getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_UNIQUE
                                    and str(exc) == "UNIQUE constraint failed: items.isbn, items.media_type"
                                    and plan["isbn"]
                                    and _isbn_taken(plan["isbn"], plan["media_type"], db=db)):
                                skipped.append({"title": plan["entry"]["title"],
                                                "reason": i18n.gettext("ISBN already in library")})
                                continue
                            raise
                        except item_write.IdentifierUnavailable:
                            raise
                        except ItemValueError as exc:
                            skipped.append({"title": plan["entry"]["title"], "reason": str(exc)})
                            continue
                        new_item_ids.append(item_id)
                        added.append({**plan["entry"], "id": item_id,
                                      "restored": item_write.was_restored(item_id)})
                        if plan["cover_url"]:
                            cover_jobs.append((item_id, plan["cover_url"]))
            except ItemValueError as exc:
                return {"ok": False, "message": str(exc), "added": [], "skipped": []}
            except sqlite3.DatabaseError:
                return {"ok": False, "message": "Import conflicts with existing catalogue data", "added": [], "skipped": []}

            # Covers never download before the complete metadata preflight.
            for item_id, cover_url in cover_jobs:
                if not restore_report.keeps_stored_cover(item_id):
                    with items_common.CoverStage(item_id) as stage:
                        cover_path = await covers._download_to_item(stage.download_id, cover_url, client)
                        if cover_path:
                            with get_db() as db:
                                stage.store(db, cover_path, library_ids={item_id: library_id})

    if new_item_ids and not os.environ.get("SHELF_DISABLE_COVER_ENRICH"):
        from app.routers import items_common
        user = catalogue_security.current_actor()
        user = dict(user) if user is not None else None
        with get_db() as db, item_write._write_scope(db):
            library_ids = items_common.authorize_item_targets(db, new_item_ids)
        asyncio.create_task(items_common._enrich_import_covers(
            new_item_ids, user=user, library_ids=library_ids), context=Context())

    return {"ok": True, "added": added, "skipped": skipped}
