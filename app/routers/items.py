import asyncio
import logging
import sqlite3
from contextvars import Context
from contextlib import suppress
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app import browse_filters
from app import i18n
from app import nav
from app.auth import require_role

logger = logging.getLogger(__name__)
from app.config import MEDIA_TYPES, BOOK_MEDIA_TYPES, HTTP_TIMEOUT, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MAX_PAGE, canonical_media_type, status_labels
from app.database import get_db, get_setting, gc_orphaned_series_meta
from app.features import feature_enabled
from app.routers.series import MAX_SERIES_NAME, _require_series_role
from app.routers import checkouts, items_common, item_barcode_edit, library_access, user_state_items, items_magazines
from app.routers import items_scan_modes, scan_inventory
from app.routers import periodicals as periodicals_router
from app.routers.items_common import SORT_OPTIONS  # re-exported for pages.py
from app.services import isbn as isbn_svc
from app.services import browse_counts
from app.services.item_write import (IdentifierInTrash, ItemValueError,
                                     insert_item, update_item_fields,
                                     update_items_fields, validate_item_fields,
                                     validated_location_id)
from app.services import item_write  # _coerce_owned (bulk update), promote_wishlisted (scan), trash_item (delete)
from app.services import hardcover, covers
from app.services import detect
from app.services import cover_queue
from app.services import legacy_book, librarything
from app.services import periodicals as periodicals_svc
from app.services.catalogue_security import require_item_role, require_catalogue_role
from app.services import scan_history
from app.services import user_state
from app.services import scan_outcome
from app.services import item_copies, item_merge
from app.services import restore_report
from app.services import tags as tags_svc
from app.services import item_template
from app.services import upc as upc_svc, igdb

router = APIRouter(prefix="/api")



def _find_duplicate_item(db, isbn13: str | None, upc_code: str | None, media_type: str) -> dict | None:
    """Patchable barcode boundary shared by native scan/manual-add paths."""
    return item_barcode_edit.find_duplicate_item(db, isbn13, upc_code, media_type)


async def _verify_legacy_book_barcode(raw: str) -> legacy_book.LegacyBookResolution:
    """Resolve every supported legacy scan through the normal book cascade."""

    with get_db() as db:
        hc_token = get_setting(db, "hardcover_token") or None
        google_api_key = get_setting(db, "google_books_api_key") or None

    candidates = legacy_book.isbn13_candidates(raw)
    logger.info(
        "Resolving legacy UPC+5 %s through ISBN candidates %s",
        raw,
        ", ".join(candidates),
    )
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        async def lookup_candidate(candidate: str):
            return await items_common._lookup_metadata(
                candidate,
                hc_token,
                client,
                google_api_key=google_api_key,
            )

        return await legacy_book.resolve(raw, lookup_candidate)


def _get_confirmed_legacy_mapping(raw: str) -> str | None:
    """Use a learned ISBN only when it is valid for this exact barcode."""

    key = legacy_book.mapping_key(raw)
    candidates = legacy_book.isbn13_candidates(raw)
    if key is None or not candidates:
        return None

    with get_db() as db:
        row = db.execute(
            "SELECT isbn13 FROM legacy_book_mappings WHERE barcode = ?", (key,)
        ).fetchone()
    if not row:
        return None

    isbn13 = row["isbn13"]
    pair = isbn_svc.canonical_isbn_pair(isbn13)
    if pair is None or pair[0] != isbn13 or isbn13 not in candidates:
        # A mapping is identity evidence only for the candidate set that
        # produced it. Corruption, an old weak constraint, or a future parser
        # change must fall back to fresh verification rather than widening
        # that trust.
        logger.error(
            "Ignoring invalid legacy barcode mapping %s -> %s; candidates=%s",
            key,
            isbn13,
            ", ".join(candidates),
        )
        return None
    return isbn13


def _save_confirmed_legacy_mapping(raw: str, isbn13: str) -> None:
    """Persist one explicitly selected, positively verified ISBN candidate."""

    key = legacy_book.mapping_key(raw)
    candidates = legacy_book.isbn13_candidates(raw)
    pair = isbn_svc.canonical_isbn_pair(isbn13)
    if key is None or pair is None or pair[0] != isbn13 or isbn13 not in candidates:
        raise ValueError("Legacy barcode confirmation is not a valid candidate")

    with get_db() as db:
        db.execute(
            """INSERT INTO legacy_book_mappings (barcode, isbn13)
               VALUES (?, ?)
               ON CONFLICT(barcode) DO UPDATE SET
                   isbn13 = excluded.isbn13,
                   confirmed_at = datetime('now')""",
            (key, isbn13),
        )


def _legacy_choice_context(
    resolution: legacy_book.LegacyBookResolution,
) -> list[dict[str, str | None]]:
    """Return presentation-safe choices from verified matches only."""

    choices = []
    for match in resolution.matches:
        authors = match.metadata.get("authors")
        choices.append(
            {
                "isbn13": match.isbn13,
                "isbn10": isbn_svc.isbn13_to_isbn10(match.isbn13),
                "title": str(match.metadata.get("title") or match.isbn13),
                "authors": str(authors) if authors else None,
            }
        )
    return choices


def _legacy_resolution_message(resolution: legacy_book.LegacyBookResolution) -> str:
    if resolution.outcome == "ambiguous":
        return i18n.gettext("Older book barcode matches more than one book — scan the printed ISBN")
    if resolution.outcome == "inconclusive":
        return i18n.gettext(
            "Couldn’t safely verify this older book barcode right now — "
            "try again later or scan the printed ISBN"
        )
    return i18n.gettext(
        "Older book barcode recognized, but no unique book match was found — "
        "scan the printed ISBN instead"
    )


# Modes that operate on existing items (not add/wishlist)
_EXISTING_ITEM_MODES = {"lend", "return", "move", "inventory", "lookup", "quick_rate"}


@router.post("/scan")
async def scan_isbn(
    request: Request, isbn: str = Form(...), media_type: str = Form("book"),
    location_id: int | None = Form(None), platform: str = Form(""),
    mode: str = Form("add"), borrower_id: int | None = Form(None),
    legacy_confirm_isbn13: str = Form(""),
    legacy_supplement: str = Form(""),
    tags: str = Form(""), reading_status: str = Form(""),
    audit_id: str = Form(""),
    _=Depends(require_catalogue_role("editor")),
):
    """Thin wrapper: whatever `_scan_isbn_inner` files takes the default tags and reading status in its own transaction (G118)."""
    # Raw text, parsed only in Inventory mode: every mode submits the Scan
    # page's persisted value, and an int field would 422 on a corrupt one
    # before the handler could ignore it.
    raw_audit = audit_id.strip() if mode == "inventory" else ""
    audit_id = int(raw_audit) if raw_audit.isascii() and raw_audit.isdigit() and int(raw_audit) > 0 else None
    try:
        with tags_svc.default_tags(tags), item_write.default_reading_status(reading_status):
            response = await _scan_isbn_inner(request, isbn, media_type, location_id, platform, mode,
                                              borrower_id, legacy_confirm_isbn13, legacy_supplement, tags, reading_status, audit_id)
        user_state_items.remember_wishlist_result(request, response, mode)
        return response
    except ItemValueError as e:
        return request.app.state.templates.TemplateResponse(
            request, "fragments/scan_result.html", {"status": "error", "isbn": isbn, "message": str(e)})


async def _scan_isbn_inner(request, isbn, media_type, location_id, platform, mode, borrower_id,
                            legacy_confirm_isbn13, legacy_supplement, tags, reading_status, audit_id=None):
    """Scan a barcode: mode-aware dispatch for add, lend, return, move, inventory, lookup, quick_rate."""
    templates = request.app.state.templates
    raw = isbn.strip()
    media_type = canonical_media_type(media_type)  # above every mode's guards

    # #90: a bare legacy UPC carries no title, so the card below asks for the
    # five digits printed beside it.  Typing them turns the scan into the
    # 17-digit form #88 already resolves, so this rewrite happens *above* mode
    # dispatch — `_get_confirmed_legacy_mapping` does too, and a completed
    # value has to reach it.  Nothing else in this route reads the supplement.
    completed = legacy_book.complete(raw, legacy_supplement)
    if completed:
        raw = completed

    # A legacy price-point barcode is an identity problem, not an ordinary UPC
    # lookup. Resolve it before any mode can act on an existing item, so an
    # ambiguous or incompletely verified scan cannot mutate a guessed row.
    legacy_candidates = legacy_book.isbn13_candidates(raw)
    legacy_isbn13 = None
    legacy_metadata = None
    legacy_source = "manual"
    legacy_hc_ids = {}
    legacy_cascade = None

    if legacy_candidates:
        legacy_isbn13 = _get_confirmed_legacy_mapping(raw)
        if legacy_isbn13:
            logger.info(
                "Using confirmed legacy UPC+5 mapping %s -> %s",
                legacy_book.mapping_key(raw),
                legacy_isbn13,
            )
        else:
            resolution = await _verify_legacy_book_barcode(raw)
            confirmed = isbn_svc.normalize_isbn(legacy_confirm_isbn13)
            selected = None
            if confirmed and isbn_svc.validate_isbn13(confirmed):
                selected = next(
                    (match for match in resolution.matches if match.isbn13 == confirmed),
                    None,
                )
            confirmation_accepted = (
                bool(legacy_confirm_isbn13)
                and resolution.outcome in {"found", "ambiguous"}
                and selected is not None
            )

            if resolution.outcome == "ambiguous" and not confirmation_accepted:
                if legacy_confirm_isbn13:
                    logger.warning(
                        "Rejected unverified legacy UPC+5 confirmation %s -> %s",
                        raw,
                        legacy_confirm_isbn13,
                    )
                # The operation is paused for a deliberate identity choice;
                # log and mutate only after that choice completes the mode.
                return templates.TemplateResponse(
                    request,
                    "fragments/scan_result.html",
                    {
                        "status": "legacy_ambiguous",
                        "isbn": raw,
                        "message": _legacy_resolution_message(resolution),
                        "legacy_candidates": _legacy_choice_context(resolution),
                        "media_type": media_type,
                        "location_id": location_id,
                        "platform": platform,
                        "mode": mode,
                        "borrower_id": borrower_id,
                        "tags": tags,
                        "reading_status": reading_status,
                        "audit_id": audit_id if mode == "inventory" else None,
                    },
                )

            if legacy_confirm_isbn13 and not confirmation_accepted:
                logger.warning(
                    "Rejected stale or unverifiable legacy UPC+5 confirmation %s -> %s",
                    raw,
                    legacy_confirm_isbn13,
                )
                items_common._log_scan(raw, "book", "error", mode=mode)
                return templates.TemplateResponse(
                    request,
                    "fragments/scan_result.html",
                    {
                        "status": "error",
                        "isbn": raw,
                        "message": (
                            "Couldn’t safely verify the selected book right now — "
                            "scan the printed ISBN or try again"
                        ),
                    },
                )

            if confirmation_accepted:
                assert selected is not None
                _save_confirmed_legacy_mapping(raw, selected.isbn13)
                legacy_isbn13 = selected.isbn13
                legacy_metadata = selected.metadata
                legacy_source = selected.source
                legacy_hc_ids = selected.hc_ids
                legacy_cascade = selected.cascade
                logger.info(
                    "Learned legacy UPC+5 mapping %s -> %s",
                    legacy_book.mapping_key(raw),
                    legacy_isbn13,
                )
            elif resolution.outcome == "found":
                legacy_isbn13 = resolution.isbn13
                legacy_metadata = resolution.metadata
                legacy_source = resolution.source
                legacy_hc_ids = resolution.hc_ids or {}
                legacy_cascade = resolution.cascade
            else:
                items_common._log_scan(raw, "book", "error", mode=mode)
                return templates.TemplateResponse(
                    request,
                    "fragments/scan_result.html",
                    {
                        "status": "error",
                        "isbn": raw,
                        "message": _legacy_resolution_message(resolution),
                    },
                )

    # --- Modes that operate on existing items ---
    if mode in _EXISTING_ITEM_MODES:
        lookup_barcode = legacy_isbn13 if legacy_candidates else raw
        item = items_scan_modes._find_item_by_barcode(lookup_barcode)
        if item and item.get("deleted_at"):  # in Trash: report, never act
            return items_scan_modes._scan_mode_in_trash(
                request, templates, item, raw, mode, location_id=location_id,
                audit_id=audit_id if mode == "inventory" else None)
        # inventory mode handles not-found specially
        if mode == "inventory":
            return scan_inventory._scan_mode_inventory(
                request,
                templates,
                item,
                location_id,
                raw,
                audit_id=audit_id,
            )
        if not item:
            items_common._log_scan(raw, "", "not_owned", None, mode)
            return templates.TemplateResponse(
                request, "fragments/scan_result.html",
                {"status": "not_owned", "isbn": raw, "message": i18n.gettext("Not in your collection")},
            )
        if mode == "lend":
            return checkouts.scan_lend(request, templates, item, borrower_id, raw)
        if mode == "return":
            return checkouts.scan_return(request, templates, item, raw)
        if mode == "move":
            return items_scan_modes._scan_mode_move(request, templates, item, location_id, raw)
        if mode == "lookup":
            return items_scan_modes._scan_mode_lookup(request, templates, item, raw)
        if mode == "quick_rate":
            return user_state_items.quick_rate(request, templates, item, raw)

    # --- Add / Wishlist modes (create new items) ---
    if (labelled := items_scan_modes._copy_label_duplicate(request, templates, raw, mode)):
        return labelled
    if legacy_candidates:
        # The legacy resolver already produced a canonical ISBN and metadata
        # when it had to ask providers.  A learned mapping still needs the
        # ordinary lookup below so its current title/edition data is used.
        barcode_type = "isbn"
        assert legacy_isbn13 is not None
        isbn13 = legacy_isbn13
    else:
        # A 977 is GS1's ISSN prefix: the barcode decides, whatever the hint (G57).
        if serial := periodicals_svc.parse_barcode(raw):
            if serial.ean13.startswith("977"):
                return await periodicals_router.render_scan_candidate(request, templates, raw, location_id, mode)
            return await items_magazines.scan_magazine(
                request, templates, raw, serial, media_type, location_id, mode)
        # #90: a *known* legacy publisher prefix scanned without its supplement
        # is a book whose title nobody knows yet — never an ordinary product.
        # Stop here and ask for the five digits rather than spend a UPC lookup
        # that would file a confident wrong answer (a DVD).  This sits below
        # mode dispatch on purpose: rows already misfiled under the bare UPC
        # must stay findable by lend/return/move/inventory/lookup/quick_rate.
        bare_upc = legacy_book.incomplete(raw)
        if bare_upc:
            return templates.TemplateResponse(
                request,
                "fragments/scan_result.html",
                {
                    "status": "legacy_incomplete",
                    "isbn": bare_upc,
                    # Anything the user actually submitted and that `complete`
                    # refused gets an explanation; only an untouched field
                    # (the first render after the scan) is silent.
                    "supplement_rejected": bool(legacy_supplement),
                    "media_type": media_type,
                    "location_id": location_id,
                    "platform": platform,
                    "mode": mode,
                    "borrower_id": borrower_id,
                    "tags": tags,
                    "reading_status": reading_status,
                },
            )

        # Detect barcode type — route ordinary UPC barcodes to DVD/product
        # lookup. Legacy UPC+5 values never reach this branch, and a bare
        # legacy UPC is stopped just above it.
        barcode_type = upc_svc.detect_barcode_type(raw)
        if barcode_type == "upc":
            return await items_common._scan_upc(
                request,
                templates,
                raw,
                media_type,
                location_id,
                platform or None,
                mode=mode,
            )

        # Normalize the ISBN, then check its digit — before any provider call,
        # so a mistyped ISBN never costs a lookup (#54). `to_isbn13` stays
        # permissive for lookup compatibility; add uses the canonical pair.
        normalized_isbn13 = isbn_svc.to_isbn13(raw)
        pair = (
            isbn_svc.canonical_isbn_pair(normalized_isbn13)
            if normalized_isbn13
            else None
        )
        if pair is None:
            items_common._log_scan(isbn, media_type, "error", mode=mode)
            return templates.TemplateResponse(
                request,
                "fragments/scan_result.html",
                # This branch is also what a *title* typed into the scan box
                # reaches, which is the whole reason it offers the manual
                # panel (#120). The offer is gated on this flag and NOT on
                # `status == 'error'`: manual_add's own four validation
                # errors render the same arm, and a link back into the form
                # they came from is a loop. Exactly one branch sets it.
                {"status": "error", "isbn": isbn, "message": i18n.gettext("Invalid ISBN"),
                 "offer_manual": True, "manual_add_text": raw},
            )
        isbn13 = pair[0]

    # §1 — the barcode outranks the dropdown when it is certain. A 978/979
    # prefix is certain, so a stale "DVD" or "Video Game" in the picker is
    # overridden to Book here rather than filing a novel as a disc; the
    # book-family distinctions the barcode genuinely cannot make
    # (audiobook / ebook / comic) are left to the user.
    #
    # There is no product record on this branch — an ISBN never reaches UPC
    # Item DB — so tiers 2 and 3 have nothing to read and tier 1 decides
    # alone. Everything below this line uses the *resolved* type: the
    # duplicate check keys on it, and so does the row that gets saved.
    hint = media_type
    detection = detect.detect_media_type(barcode_type, hint, None, None)
    media_type = detection.media_type
    detect_reason = detection.reason
    detect_overrode = media_type != hint

    # Check duplicate. An Add-mode scan of a wishlisted book is the purchase:
    # it becomes owned (#125). The read and that write share one write lock,
    # taken first (G18); the scan is logged after the block (G3).
    promoted = False
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = _find_duplicate_item(db, isbn13, None, media_type)
        restored_id = item_barcode_edit.restore_legacy_book(db, isbn13, media_type, mode, reading_status) if not existing else None
        if existing and mode != "wishlist":
            require_item_role(db, existing["id"], "editor")
            promoted = item_write.promote_wishlisted(db, existing["id"])
    if existing:
        status = "promoted" if promoted else "duplicate"
        items_common._log_scan(isbn13, media_type, status, existing["id"], mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": status, "isbn": isbn13, "title": existing["title"], "item_id": existing["id"]},
        )
    if restored_id is not None:
        items_common._log_scan(isbn13, media_type, "restored", restored_id, mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "restored", "isbn": isbn13, "item_id": restored_id,
             **restore_report.restored_card(restored_id)},
        )

    # Get optional metadata-provider credentials — and refuse a stale location
    # id here rather than after the cascade: the funnel would raise on the
    # save, but not before the lookup had been paid for.
    location_error = None
    with get_db() as db:
        try:
            location_id = validated_location_id(db, location_id)
        except ItemValueError as e:
            location_error = str(e)
        hc_token = get_setting(db, "hardcover_token") or None
        google_api_key = get_setting(db, "google_books_api_key") or None
    if location_error:
        items_common._log_scan(isbn13, media_type, "error", mode=mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "error", "isbn": isbn13, "message": location_error},
        )

    logger.info("Scanning ISBN %s (type=%s, mode=%s)", isbn13, media_type, mode)
    # No try/except around this block any more: every leg returns
    # `transport_failed` rather than raising, and `_fetch_preview_cover`
    # swallows everything of its own, so the old `httpx.TimeoutException` /
    # `NetworkError` arms had become dead code that reads as live (G47). The
    # connectivity card is rendered from the cascade outcome below; the
    # separate "timed out — try again" wording collapses into it.
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        if legacy_metadata is not None:
            # Reuse the exact positively verified result that resolved an
            # ambiguous/unique legacy scan. This avoids a second provider
            # lookup whose answer could differ from the identity decision.
            metadata = legacy_metadata
            source = legacy_source
            hc_ids = legacy_hc_ids
            cascade = legacy_cascade
        else:
            metadata, source, hc_ids, cascade = await items_common._lookup_metadata(
                isbn13, hc_token, client, google_api_key=google_api_key
            )

        if not metadata:
            # G47 applied to the book path: a cascade that could not reach
            # anybody is not a book that does not exist. Only an Open
            # Library exception used to reach this card; the other three
            # legs swallowed their transport failures into `not_found`.
            if cascade.outcome == "transport_failed":
                logger.warning("Network error looking up ISBN %s", isbn13)
                items_common._log_scan(isbn13, media_type, "error", mode=mode)
                return templates.TemplateResponse(
                    request, "fragments/scan_result.html",
                    {"status": "error", "isbn": isbn13,
                     "message": i18n.gettext("Network error during lookup — check connectivity")},
                )

            preview_cover = await items_common._fetch_preview_cover(isbn13, client)
            items_common._log_scan(isbn13, media_type, "not_found", mode=mode)
            # The same vocabulary the added-card notice uses, projected
            # from the cascade record — so `rejected` reaches the arm
            # `fragments/scan_result.html` has always carried for it and
            # the card can name the credential that was refused.
            # `not_found_status`, not `enrich_status`: this card's own
            # message already says "Not found", so a `no_match` notice
            # would repeat it. Both UPC branches project the same way.
            return templates.TemplateResponse(
                request, "fragments/scan_result.html",
                {
                    "status": "not_found", "isbn": isbn13, "media_type": media_type,
                    "message": i18n.gettext("Not found — add manually below"),
                    "preview_cover": preview_cover,
                    "enrich_status": scan_outcome.not_found_status(cascade),
                    "enrich_provider": scan_outcome.provider_label(cascade),
                    "locations": items_common._manual_form_locations(),
                },
            )

        try:
            item_id = items_common._save_item(metadata, isbn13, media_type, location_id,
                                              source, hc_ids, owned=mode != "wishlist")
        except sqlite3.IntegrityError:
            # A second scan of the same ISBN — a double submit, or two
            # devices — can win the insert during this request's own
            # metadata lookup: the same race the duplicate check above
            # defends against under its write lock, just a wider window.
            # Re-run that exact check rather than let the UNIQUE violation
            # reach the caller as a 500 (F-BUG-1).
            with get_db() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = _find_duplicate_item(db, isbn13, None, media_type)
                if existing and mode != "wishlist":
                    require_item_role(db, existing["id"], "editor")
                promoted = bool(existing) and mode != "wishlist" and item_write.promote_wishlisted(db, existing["id"])
            if existing is None:
                raise
            status = "promoted" if promoted else "duplicate"
            items_common._log_scan(isbn13, media_type, status, existing["id"], mode)
            return templates.TemplateResponse(
                request, "fragments/scan_result.html",
                {"status": status, "isbn": isbn13, "title": existing["title"], "item_id": existing["id"]},
            )

        # Queue the cover instead of downloading it in-request. The
        # hints are the exact three inputs the download used to take, so
        # the worker runs the same chain this request would have.
        hc_cover = metadata.get("cover_url") if source == "hardcover" else hc_ids.get("cover_url")
        cover_queue.enqueue(item_id, hints={
            "cover_url": metadata.get("cover_url") if source != "hardcover" else None,
            "cover_id": metadata.get("cover_id"),
            "hardcover_cover_url": hc_cover,
        })


    # Read the restore flag before the id is used for anything else.
    status = restore_report.restored_status(
        item_id, "wishlisted" if mode == "wishlist" else "added")
    items_common._log_scan(isbn13, media_type, status, item_id, mode)

    card = ({"title": metadata["title"], "authors": metadata.get("authors"),
             "cover_path": None, "cover_pending": True}
            if status != "restored" else restore_report.restored_card(item_id))
    resp = templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {
            "status": status,
            "isbn": isbn13,
            **card,
            "item_id": item_id,
            "source": source,
            "media_type_label": i18n.gettext(MEDIA_TYPES.get(media_type, media_type)), "media_type": media_type,
            # T5 renders these; T4 only has to carry them.
            "detect_reason": detect_reason,
            "detect_overrode": detect_overrode,
        },
    )
    return resp
@router.post("/items/manual")
async def manual_add(request: Request, _=Depends(require_catalogue_role("editor"))):
    """Manually add an item with optional cover upload. Returns HTMX fragment."""
    templates = request.app.state.templates
    form = await request.form()

    title = form.get("title", "").strip()
    if not title:
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "error", "isbn": form.get("isbn", ""), "message": i18n.gettext("Title is required")},
        )

    isbn = form.get("isbn", "").strip()
    media_type = canonical_media_type(form.get("media_type", "book"))
    # Defaulted to "add" to match /api/scan's own Form("add") at :233 — the
    # value reaches scan_log.mode, so an absent field must log what the scan
    # route would log for the same submission.
    mode = form.get("mode", "add")

    # A UPC belongs in items.upc, never in items.isbn (#20). to_isbn13()
    # will happily zero-pad a 12-digit UPC-A into something ISBN-shaped, so
    # every manually-added disc and game used to be filed in the wrong
    # column: the UPC scan path (which reads items.upc) could never find it
    # again, and scanning the same barcode a second time offered the manual
    # form again and then tripped UNIQUE(isbn, media_type) with a 500.
    barcode_type = upc_svc.detect_barcode_type(isbn) if isbn else "unknown"
    if barcode_type == "upc":
        upc_code = upc_svc.normalize_upc(isbn)
        isbn13 = None
    else:
        upc_code = None
        # The check digit, before anything else runs (#54). The funnel would
        # refuse it too; refusing here keeps the card's wording and keys the
        # duplicate check on a real ISBN. The funnel derives isbn10.
        pair = isbn_svc.canonical_isbn_pair(isbn) if isbn else None
        if isbn and pair is None:
            return templates.TemplateResponse(
                request, "fragments/scan_result.html",
                {"status": "error", "isbn": isbn, "message": i18n.gettext("Invalid ISBN")},
            )
        isbn13 = pair[0] if pair else None

    pub_year_raw = (form.get("publish_year") or "").strip()
    if pub_year_raw:
        try:
            pub_year = int(pub_year_raw)
        except (TypeError, ValueError):
            return templates.TemplateResponse(
                request, "fragments/scan_result.html",
                {"status": "error", "isbn": isbn, "message": i18n.gettext("Invalid publish year")},
            )
    else:
        pub_year = None

    platform = (form.get("platform") or "").strip() or None
    language = form.get("language", "").strip() or None

    # #19 "copy from" prefill: series_name + location_id are optional and
    # only reach here if the picker filled them in (or the user typed them).
    series_name = form.get("series_name", "").strip() or None
    if series_name and len(series_name) > MAX_SERIES_NAME:
        series_name = series_name[:MAX_SERIES_NAME]
    location_id_raw = form.get("location_id")
    location_id = None
    if location_id_raw:
        try:
            location_id = int(location_id_raw)
        except (TypeError, ValueError):
            location_id = None

    # Media type, platform and location are the funnel's to check (#54):
    # `insert_item` raises `ItemValueError` and the card carries its message.
    # Rendered after the block so nothing runs under the write.
    value_error = None
    promoted = False
    item_id = None
    with get_db() as db:
        # The duplicate guard and the insert (or the promotion below) share
        # one write lock, taken before the guard reads (G18).
        db.execute("BEGIN IMMEDIATE")
        try:
            existing = _find_duplicate_item(db, isbn13, upc_code, media_type)
            if not existing:
                with tags_svc.default_tags(form.get("tags") or ""), item_write.default_reading_status(form.get("reading_status") or ""):
                    item_id = item_barcode_edit.restore_legacy_book(db, isbn13, media_type, mode, form.get("reading_status") or "")
        except ItemValueError as e:
            db.rollback()
            return templates.TemplateResponse(request, "fragments/scan_result.html",
                {"status": "error", "isbn": isbn, "message": str(e)})
        if existing is None and item_id is None:
            if series_name:
                _require_series_role(db, series_name)
            # Wishlist intent rides the insert, not a second transaction: a
            # restoring funnel has to be told it, and the demote this
            # replaced also sat outside the lock above.
            wishlist = {"owned": 0, "wishlisted": True} if mode == "wishlist" else {}
            try:
                with tags_svc.default_tags(form.get("tags") or ""), \
                        item_write.default_reading_status(form.get("reading_status") or ""):
                    item_id = insert_item(
                        db,
                        **wishlist,
                        title=title,
                        authors=form.get("authors"),
                        isbn=isbn13,
                        upc=upc_code,
                        media_type=media_type,
                        publisher=form.get("publisher"),
                        publish_year=pub_year,
                        platform=platform,
                        series_name=series_name,
                        location_id=location_id,
                        language=language,
                        source="manual",
                    )
            except ItemValueError as e:
                value_error = str(e)
            except sqlite3.IntegrityError:
                # Lost a race with a concurrent add, or the barcode is on a row
                # filed before the #20 re-file migration. Either way the user
                # gets the duplicate card rather than a 500.
                existing = _find_duplicate_item(db, isbn13, upc_code, media_type)
                if existing is None:
                    raise
        # Adding a wishlisted item in Add mode is buying it (#125).
        if existing and mode != "wishlist":
            require_item_role(db, existing["id"], "editor")
            promoted = item_write.promote_wishlisted(db, existing["id"])

    if value_error:
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": "error", "isbn": isbn, "message": value_error},
        )

    if existing:
        code = isbn13 or upc_code or ""
        status = "promoted" if promoted else "duplicate"
        items_common._log_scan(code, media_type, status, existing["id"], mode)
        return templates.TemplateResponse(
            request, "fragments/scan_result.html",
            {"status": status, "isbn": code, "title": existing["title"],
             "item_id": existing["id"]},
        )


    status = restore_report.restored_status(
        item_id, "wishlisted" if mode == "wishlist" else "added")

    # Handle cover upload.
    #
    # Everything from here to the UPDATE is enrichment over a row that is
    # already committed. A filesystem or network
    # failure used to escape as a 500 over an add that had in fact
    # succeeded, and the user's natural retry filed a *second* row: a
    # title-only manual add carries no identifier, so `_find_duplicate_item`
    # cannot recognise the one already stored and nothing else can either.
    # The item is the thing the user asked for; the cover is not worth
    # losing it over, and rolling the row back instead would only trade
    # this failure for an orphaned file.
    cover_path = None
    cover_failed = False
    staged_key = f"pending_{uuid4().hex}"
    staged_path = covers.COVERS_DIR / f"{staged_key}.jpg"
    try:
        cover_content = None
        cover_file = form.get("cover")
        if cover_file and hasattr(cover_file, "read"):
            content = await cover_file.read(covers.MAX_COVER_SIZE + 1)
            if content and covers.validate_uploaded_cover(content):
                cover_content = content

        # If no upload, check for preview cover from scan, then try Amazon —
        # unless this restored a row that keeps its stored cover.
        if cover_content is None and isbn13 and not restore_report.keeps_stored_cover(item_id):
            preview_path = covers.COVERS_DIR / f"preview_{isbn13}.jpg"
            if preview_path.exists():
                cover_content = preview_path.read_bytes()
            else:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    downloaded = await covers.download_cover(staged_key, isbn13, None, None, client)
                if downloaded and staged_path.is_file():
                    cover_content = staged_path.read_bytes()

        if cover_content is not None:
            with get_db() as db:
                db.execute("BEGIN IMMEDIATE")
                require_item_role(db, item_id, "editor")
                cover_path = covers.save_uploaded_cover(item_id, cover_content, db=db)
                if cover_path:
                    update_item_fields(db, item_id, {"cover_path": cover_path, "cover_edition_isbn": None, "cover_edition_language": None})
    except HTTPException:
        raise
    except Exception:
        logger.warning(
            "manual add %s: cover could not be saved", item_id, exc_info=True
        )
        cover_path = None
        cover_failed = True
    finally:
        with suppress(OSError):
            staged_path.unlink(missing_ok=True)

    items_common._log_scan(isbn13 or upc_code or "", media_type, status, item_id, mode)

    resp = templates.TemplateResponse(
        request, "fragments/scan_result.html",
        {
            "status": status,
            "isbn": isbn13 or upc_code or "",
            # A restored row shows its stored title and authors; an
            # explicitly uploaded cover still wins over the stored one.
            **({"title": title, "authors": form.get("authors"),
                "cover_path": cover_path}
               if status != "restored"
               else {**restore_report.restored_card(item_id),
                     **({"cover_path": cover_path} if cover_path else {})}),
            "item_id": item_id,
            "source": "manual",
            "media_type_label": i18n.gettext(MEDIA_TYPES.get(media_type, media_type)), "media_type": media_type,
        },
    )
    toast_message = f"Added to wishlist: {title[:50]}" if mode == "wishlist" else f"Added: {title[:50]}"
    # Say what actually happened. Silently dropping the cover would leave the
    # user with a coverless item and no idea why; the item page can retry it.
    if cover_failed:
        toast_message += " — cover could not be saved"
    resp.headers["HX-Trigger"] = items_common._toast_header(
        toast_message, "warning" if cover_failed else "success"
    )
    user_state_items.remember_wishlist_result(request, resp, mode)
    return resp


@router.get("/items/suggest")
async def suggest_items(q: str = "", _=Depends(require_catalogue_role("editor"))):
    """Title-prefix suggestions for the manual-add "copy from" picker (#19).

    JSON only (no HTMX fragment) — the picker is a small Alpine dropdown, not
    a server-rendered list.
    """
    q = q.strip()[:200]
    if not q:
        return JSONResponse([])
    with get_db() as db:
        rows = db.execute(
            "SELECT id, title, authors FROM items_live WHERE title LIKE ? "
            "ORDER BY title COLLATE NOCASE LIMIT 10",
            (f"{q}%",),
        ).fetchall()
    return JSONResponse([{"id": r["id"], "title": r["title"], "authors": r["authors"]} for r in rows])


@router.get("/items/{item_id}/copy-template")
async def copy_template(item_id: int, _=Depends(require_catalogue_role("editor"))):
    """Copyable-field subset of an item for manual-add prefill (#19).

    The field set is `item_template.COPYABLE_FIELDS`, which /scan's `?from={id}`
    panel prefill also reads (#120) — one declaration, two consumers, so they
    cannot drift.
    """
    with get_db() as db:
        fields = item_template.copyable_fields(db, item_id)
    if fields is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(fields)


@router.get("/search")
async def search_items(
    request: Request,
    page: int = Query(1, le=MAX_PAGE),
    per_page: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    _=Depends(require_catalogue_role("viewer")),
):
    """Search/filter items. Returns HTMX fragment of item cards.

    Filter values are read from the query string via the registry rather than
    declared as parameters here — a filter added to `app/browse_filters.py`
    then needs no change in this signature. Everything is a plain string; the
    registry owns the per-filter parsing (`location_filter` casts to int, and
    `owned` is tri-state).
    """
    templates = request.app.state.templates

    values = browse_filters.values_from(request.query_params)
    # Truncate search query to prevent slow LIKE scans
    values["q"] = values["q"][:200]
    sort = values["sort"]
    view = values["view"]

    where, params = browse_filters.build_where(values)
    _, order_clause = SORT_OPTIONS.get(sort, SORT_OPTIONS["newest"])
    offset = (max(page, 1) - 1) * per_page

    with get_db() as db:
        items, total, display_total = library_access.fetch_page(
            db, int(request.state.user["id"]), where, params, order_clause,
            limit=per_page, offset=offset, values=values)
        tags_by_item = tags_svc.tags_for_items(db, [row["id"] for row in items])

        # Cross-filter counts for dropdowns (page 1 only). Each group is the
        # same where-clause with its own filter excluded, so the number beside
        # an option says what selecting it would yield. Shared with /browse so
        # the two routes cannot disagree.
        counts = browse_counts.filter_counts(db, values, total) if page <= 1 else None

    has_more = (offset + per_page) < display_total

    load_more_url = "/api/search?" + browse_filters.querystring(
        values, extra=[f"page={page + 1}"]
    )

    # Page 1: full grid wrapper. Page 2+: just cards/rows (appended via outerHTML swap on load-more).
    if page <= 1:
        template = "fragments/item_grid.html"
    elif view == "list":
        template = "fragments/item_rows_page.html"
    else:
        template = "fragments/item_cards_page.html"

    from datetime import datetime, timedelta
    ctx = {
        "items": items,
        "tags_by_item": tags_by_item,
        "media_types": MEDIA_TYPES,
        "has_more": has_more,
        "load_more_url": load_more_url,
        "page": page,
        "total": total,
        "has_filters": browse_filters.has_active_filters(values), "active_sort": sort,
        "seven_days_ago": (datetime.now(tz=None) - timedelta(days=7)).strftime("%Y-%m-%d"),
    }
    if counts:
        ctx.update(counts)
        ctx["render_oob_counts"] = True

    return templates.TemplateResponse(request, template, ctx)


def _require_bulk_item_roles(db, item_ids, user):
    """Only native account admins retain absent-ID actual-count semantics.

    This physical existence probe never makes an extant hidden, unmapped or
    trashed row disappear. Every such target still authorizes under the caller's
    write lock; all other accounts must authorize the complete selection.
    """
    native_admin = user.get("role") == "admin" and not user.get("api_token_id")
    for item_id in item_ids:
        if native_admin and not db.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone():
            continue
        require_item_role(db, item_id, "editor")


@router.post("/items/bulk-update")
async def bulk_update(request: Request, _=Depends(require_catalogue_role("editor"))):
    """Bulk update multiple items with the same field values."""
    try:
        data = await request.json()
    except (ValueError, TypeError):
        return {"ok": False, "message": "Invalid request body"}
    if not isinstance(data, dict):
        return {"ok": False, "message": "Invalid request body"}
    raw_ids = data.get("item_ids", [])
    updates = data.get("updates", {})

    if not isinstance(raw_ids, list) or not isinstance(updates, dict):
        return {"ok": False, "message": "Invalid request body"}
    if not raw_ids or not updates:
        return {"ok": False, "message": i18n.gettext("No items or updates specified")}

    try:
        item_ids = list(dict.fromkeys(int(i) for i in raw_ids))
    except (ValueError, TypeError):
        return {"ok": False, "message": i18n.gettext("Invalid item IDs")}
    if any(i <= 0 for i in item_ids):
        return {"ok": False, "message": i18n.gettext("Invalid item IDs")}

    allowed = {"media_type", "location_id", "reading_status", "owned", "wishlisted", "series_name", "language"}
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return {"ok": False, "message": i18n.gettext("No valid fields to update")}

    if "series_name" in filtered:
        if filtered["series_name"] == "__clear__":
            filtered["series_name"] = None
        elif not str(filtered["series_name"]).strip():
            return {"ok": False, "message": i18n.gettext("Series name cannot be empty")}
        elif len(str(filtered["series_name"]).strip()) > MAX_SERIES_NAME:
            return {"ok": False, "message": "Series name is too long"}
        else:
            filtered["series_name"] = str(filtered["series_name"]).strip()

    for name in ("location_id", "reading_status"):
        if filtered.get(name) in ("", "__clear__"):
            filtered[name] = None

    if "language" in filtered:
        lang = str(filtered["language"] or "").strip()
        filtered["language"] = None if lang in ("", "__clear__") else lang

    placeholders = ",".join("?" for _ in item_ids)

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _require_bulk_item_roles(db, item_ids, request.state.user)
        item_ids = [row["id"] for row in db.execute(
            f"SELECT id FROM items_live WHERE id IN ({placeholders})", item_ids)]
        if not item_ids:
            return {"ok": False, "message": "No matching items found", "updated": 0}
        placeholders = ",".join("?" for _ in item_ids)
        old_series_names = []
        if "series_name" in filtered:
            old_series_names = [
                r["series_name"] for r in db.execute(
                    f"SELECT DISTINCT series_name FROM items_live WHERE id IN ({placeholders})",
                    item_ids,
                ).fetchall()
            ]
            for name in {*old_series_names, filtered["series_name"]}:
                if name:
                    _require_series_role(db, name)

        try:
            if "owned" in filtered:
                # Same coercion the funnel applies to "owned" itself (issue
                # #125) — an invalid value raises InvalidOwned here, caught
                # below exactly as it would be if raised inside the funnel,
                # so the error contract is unchanged. A submitted
                # "wishlisted" is passed straight through to the funnel
                # rather than derived from "owned" — `_pop_wishlisted`
                # judges it and `_refuse_owned_wishlist` refuses the
                # combination (and a wishlisted=true over a selection that
                # includes an owned row) whole, before any write (G96).
                coerced = item_write._coerce_owned(filtered["owned"])
                filtered["owned"] = coerced
            update_items_fields(db, item_ids, filtered)
        except ItemValueError as e:
            db.rollback()
            message = "Invalid reading status" if e.code == "invalid_reading_status" else str(e)
            return {"ok": False, "message": message}
        except sqlite3.IntegrityError:
            db.rollback()
            return {"ok": False, "message": "Update conflicts with existing catalogue data", "updated": 0}

        if old_series_names:
            gc_orphaned_series_meta(db, *old_series_names)

    return {"ok": True, "updated": len(item_ids)}


@router.post("/items/merge")
async def merge_items(request: Request, _=Depends(require_catalogue_role("editor"))):
    """Merge multiple items into one, keeping the first as primary."""
    try:
        data = await request.json()
    except (ValueError, TypeError):
        return {"ok": False, "message": "Invalid request body", "merged": 0}
    if not isinstance(data, dict) or not isinstance(data.get("merge_ids", []), list):
        return {"ok": False, "message": "Invalid request body", "merged": 0}
    try:
        keep_id = int(data.get("keep_id", 0))
        merge_ids = [int(i) for i in data.get("merge_ids", [])]
    except (ValueError, TypeError):
        return {"ok": False, "message": "Invalid item IDs"}

    if keep_id <= 0 or not merge_ids or any(mid <= 0 for mid in merge_ids):
        return {"ok": False, "message": "Specify keep_id and merge_ids"}
    if keep_id in merge_ids:
        return {"ok": False, "message": "Cannot merge an item into itself", "merged": 0}

    # Drop the kept row and repeats before anything is written. Both end in
    # `DELETE FROM items WHERE id = keep_id` further down, destroying the row
    # the caller asked to keep (#86).
    targets = list(dict.fromkeys(mid for mid in merge_ids if mid != keep_id))
    if not targets:
        return {"ok": False, "message": "Cannot merge an item into itself"}

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        require_item_role(db, keep_id, "editor")
        _require_bulk_item_roles(db, targets, request.state.user)
        primary = db.execute("SELECT * FROM items_live WHERE id = ?", (keep_id,)).fetchone()
        if not primary:
            return {"ok": False, "message": "Primary item not found"}

        # Refuse before writing anything: the merge is one transaction, and a
        # kept row holding two open loans is a state no surface can show.
        on_loan = item_merge.active_loan_ids(db, [keep_id, *targets])
        if len(on_loan) > 1:
            titles = [
                row["title"]
                for row in db.execute(
                    "SELECT title FROM items_live WHERE id IN "
                    f"({','.join('?' for _ in on_loan)}) ORDER BY id",
                    sorted(on_loan),
                ).fetchall()
            ]
            return {
                "ok": False,
                "message": "Cannot merge items that are both on loan: "
                           + ", ".join(f'"{t}"' for t in titles)
                           + ". Check one in first.",
            }

        merged = 0
        for mid in targets:
            other = db.execute("SELECT * FROM items_live WHERE id = ?", (mid,)).fetchone()
            if not other:
                continue
            fill = {f: other[f] for f in item_merge.MERGE_FILLABLE
                    if f not in user_state.PERSONAL_FIELDS
                    and (primary[f] is None or primary[f] == "")
                    and other[f] is not None and other[f] != ""}
            # Ownership is monotonic across a merge; zero is otherwise real
            # metadata and must not be treated as an empty price/position.
            if primary["owned"] == 0 and other["owned"] == 1:
                fill["owned"] = 1
            if fill:
                # Validate *before* the DELETE: a merge must not delete a row
                # whose values it failed to copy (#54). The write itself
                # comes after the delete, because a filled isbn is still on
                # the other row until then and UNIQUE(isbn, media_type)
                # would refuse the copy.
                try:
                    fill = validate_item_fields(db, fill)
                    # Collision preflight here, not at the write below: that
                    # runs after the DELETE, so a refusal there would arrive
                    # with the husk gone and roll the merge back. The husk is
                    # live, so only a third, trashed row can collide.
                    item_write.refuse_trash_collision(db, "id = ?", [keep_id], fill)
                except ItemValueError as e:
                    # Undo the targets already merged: a plain return commits,
                    # and the client is told nothing was (duplicate-audit B1).
                    db.rollback()
                    # Name the row: the loop stops on the first bad one, and a
                    # multi-row merge otherwise reports a value with no way to
                    # tell which item carried it.
                    return {
                        "ok": False,
                        "message": f"Cannot merge \"{other['title']}\" (#{mid}): {e}",
                        "item_id": mid,
                    }

            # Every child table of `items` is ON DELETE CASCADE, so this has to
            # happen before the DELETE or the cascade takes the merged row's
            # loans, tags, links and physical copies with it (#86).
            try:
                item_merge.reparent_children(db, keep_id, mid)
                db.execute("DELETE FROM items WHERE id = ?", (mid,))
                retained_primary = db.execute(
                    "SELECT location_id FROM copies_live WHERE item_id = ? AND is_primary = 1",
                    (keep_id,),
                ).fetchone()
                if retained_primary is not None:
                    # Reparenting retained physical evidence, not a flat-location
                    # edit. Project the FINAL primary NODE before a metadata or
                    # ownership update can project the keeper's old flat seam.
                    item_copies._sync_legacy_seam(db, keep_id, retained_primary["location_id"])
                    fill.pop("location_id", None)
                if fill:
                    update_item_fields(db, keep_id, fill, create_copy_on_acquisition=False)
                primary = db.execute("SELECT * FROM items_live WHERE id = ?", (keep_id,)).fetchone()
            except (ItemValueError, ValueError, sqlite3.IntegrityError):
                db.rollback()
                return {"ok": False, "message": "Merge conflicts with existing catalogue data", "merged": 0}
            merged += 1

    # The count is what was actually merged, not what was asked for: an id that
    # named no row used to be reported as a success.
    return {"ok": True, "merged": merged}


def _edit_authority(db, item_id, principal, library_ids=None):
    """A provider await may not transfer an editor Save to another parent/account."""
    current = user_state_items.job_principal(db, principal)
    if current is None or any(current[key] != principal[key] for key in ("username", "role")):
        raise HTTPException(403, "Forbidden")
    return items_common.authorize_item_targets(
        db, [item_id], user=principal, library_ids=library_ids)


@router.post("/items/{item_id}/edit")
@router.post("/items/{item_id}")
async def update_item(request: Request, item_id: int, _=Depends(require_catalogue_role("editor"))):
    form = await request.form()
    principal = dict(request.state.user)
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            library_ids = _edit_authority(db, item_id, principal)
        choice_url = (form.get("cover_choice_url") or "").strip()
        cover_file = form.get("cover")
        # The manual URL button applies immediately through its own guarded
        # route. Do not guess which source a mixed Save intended to replace.
        if sum((bool(choice_url), bool(getattr(cover_file, "filename", "")), bool(form.get("url")))) > 1:
            return PlainTextResponse("Choose one cover source", status_code=400)
        if not choice_url and any(form.get(key) for key in ("cover_choice_edition_isbn", "cover_choice_edition_language")):
            return PlainTextResponse("Choose a cover before its edition", status_code=400)
        with items_common.CoverStage(item_id) as stage:
            downloaded = None
            if choice_url:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    downloaded = await covers._download_to_item(stage.download_id, choice_url, client)
                if not downloaded or not stage.owns(downloaded):
                    return PlainTextResponse("Cover could not be downloaded", status_code=400)
            return await _save_item_form(
                request, item_id, form, stage, downloaded, principal, library_ids)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return PlainTextResponse("Not found", status_code=404)


async def _save_item_form(request, item_id, form, stage, downloaded, principal, library_ids):
    """Validate and save dirty fields plus a pending choice in ONE transaction."""
    back_key = nav.back_target(form.get("from"))["key"]
    redirect_url = f"/item/{item_id}" + (f"?from={back_key}" if back_key else "")
    from fastapi.responses import RedirectResponse
    # The edit form's error surface: back to the form with `?error=<code>`,
    # rendered as a banner by the template (copy lives there, keyed on the
    # code — G58). Nothing is saved on any refusal.
    edit_url = f"/item/{item_id}/edit" + (f"?from={back_key}" if back_key else "")

    def _refused(code, *, field=None, **extra):
        # Preserve the fork's strict partial-write boundary, while full forms
        # retain native error banners. Native UPC/ASIN banners also stay intact.
        partial_errors = {
            "invalid_title": "Title is required", "unknown_media_type": "Invalid media type",
            "unknown_platform": "Unknown platform", "invalid_owned": "Owned must be 0 or 1",
            "invalid_reading_status": "Invalid reading status", "invalid_isbn": "Invalid ISBN",
            "unknown_location": "Location not found", "invalid_location": "Location not found",
            "invalid_number": {
                "publish_year": "Invalid publish year", "page_count": "Invalid page count",
                "duration_mins": "Invalid duration mins", "series_position": "Invalid series position",
                "manual_value": "Invalid manual value", "location_id": "Location not found",
                "owned": "Owned must be 0 or 1",
            }.get(field, "Invalid number"),
        }
        if not {"title", "media_type", "owned"}.issubset(form.keys()) and code in partial_errors:
            return PlainTextResponse(partial_errors[code], status_code=400)
        url = f"{edit_url}{'&' if '?' in edit_url else '?'}error={code}"
        # `trashed=<id>` only: the template resolves the title itself, so no
        # user text is ever echoed back through the query string (G58).
        for key, value in extra.items():
            url += f"&{key}={value}"
        return RedirectResponse(url=url, status_code=303)

    fields = {}
    try:
        for key in ("title", "subtitle", "authors", "isbn", "upc", "media_type", "publisher",
                    "publish_year", "page_count", "description", "series_name",
                    "series_position", "narrator", "duration_mins", "location_id", "notes",
                    "reading_status", "date_started", "date_finished", "owned", "wishlisted",
                    "platform", "manual_value", "language", "asin"):
            val = form.get(key)
            if val is not None:
                if key == "wishlisted":
                    # Independent of `owned` (#125): the form posts it on its
                    # own, and an absent key leaves membership alone (G87).
                    if val not in ("0", "1"):
                        return _refused("invalid_wishlisted")
                    fields[key] = val == "1"
                elif val == "" and key != "owned":
                    fields[key] = None
                elif key in ("publish_year", "page_count", "duration_mins", "location_id"):
                    fields[key] = int(val) if val else None
                elif key in ("series_position", "manual_value"):
                    fields[key] = float(val) if val else None
                elif key == "owned":
                    fields[key] = int(val) if val else 0
                else:
                    fields[key] = val
    except (TypeError, ValueError):
        # A non-numeric year/count/value used to be a 500.
        return _refused("invalid_number", field=key)

    # Cover upload: validated here, written only after every refusal below
    # has had its chance — a refused save must not touch the file (#127).
    cover_content = None
    cover_file = form.get("cover")
    if cover_file and hasattr(cover_file, "read"):
        content = await cover_file.read(covers.MAX_COVER_SIZE + 1)
        if content and len(content) > 100 and covers.validate_uploaded_cover(content):
            cover_content = content
            fields.update(cover_path=f"covers/{item_id}.jpg",
                          cover_edition_isbn=None, cover_edition_language=None)

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        _edit_authority(db, item_id, principal, library_ids)
        row = db.execute(
            "SELECT isbn, upc, media_type, series_name FROM items_live WHERE id = ?",
            (item_id,),
        ).fetchone()
        # Missing or in Trash — a stale edit form must not write to either.
        if row is None:
            return PlainTextResponse("Not found", status_code=404)
        old_series_name = row["series_name"]
        if {"series_name", "series_position"} & fields.keys():
            for name in {old_series_name, fields.get("series_name")}:
                if name:
                    _require_series_role(db, name)
        try:
            periodical = item_barcode_edit.prepare_periodical_update(db, item_id, form)
        except LookupError as exc:
            return HTMLResponse(str(exc), status_code=404)
        except ValueError as exc:
            return HTMLResponse(str(exc), status_code=400)
        if "title" in fields and not str(fields["title"] or "").strip():
            return _refused("invalid_title")

        # #87: the edit form re-posts every named control on every save,
        # including a stored ISBN/UPC that predates the validator — which
        # otherwise bounces *every* save on that row, even a title-only fix.
        # Exempt an identifier from validation only when it is BOTH
        # unchanged from the stored value AND the validator would refuse
        # it. Plain "unchanged" is too broad: a valid-but-unchanged ISBN
        # still has to reach the funnel below, because canonical_isbn_pair
        # rewrites isbn10 from isbn, and 8 rows in the real DB hold a valid
        # isbn13 with a stale/wrong stored isbn10 that this repairs. Only
        # dropping the key when the value is also refused keeps that repair
        # intact. Dropping the key here also means the upc_conflict lookup
        # and the funnel's UNIQUE(isbn, media_type) path never see it —
        # correct, since an unchanged identifier cannot newly collide, but
        # worth re-reading if a future branch is added below this guard
        # that answers the same "did this identifier change" question (G68).
        if (fields.get("isbn") is not None and fields["isbn"] == row["isbn"]
                and isbn_svc.canonical_isbn_pair(fields["isbn"]) is None):
            del fields["isbn"]
        if (fields.get("upc") is not None and fields["upc"] == row["upc"]
                and not upc_svc.canonical_retail_barcode(fields["upc"])[0]):
            del fields["upc"]

        if not fields and periodical is None and downloaded is None:
            return RedirectResponse(url=redirect_url, status_code=303)

        # Retail barcodes are normal edit fields, but their storage identity is
        # canonical EAN-13. Bookland 978/979 carriers remain ISBN-only.
        if "upc" in fields:
            valid_upc, canonical_upc = upc_svc.canonical_retail_barcode(fields["upc"])
            if not valid_upc:
                return _refused("invalid_upc")
            fields["upc"] = canonical_upc

        # The user's value: refuse, never drop (G72). Clearing stores NULL.
        if fields.get("asin") is not None:
            cleaned_asin = isbn_svc.clean_asin(fields["asin"])
            if cleaned_asin is None:
                return _refused("invalid_asin")
            fields["asin"] = cleaned_asin

        if fields.get("media_type"):
            # A stored kids_book is not a new-input alias. Reposting an old
            # form must not silently retype its existing identity.
            if fields["media_type"] == row["media_type"] and canonical_media_type(row["media_type"]) != row["media_type"]:
                del fields["media_type"]
            else:
                fields["media_type"] = canonical_media_type(fields["media_type"])

        # Keep the same duplicate identity rule used by normal scan/add:
        # a retail barcode may repeat across media types, never within one.
        if fields.get("upc"):
            effective_media_type = fields.get("media_type")
            if effective_media_type is None:
                effective_media_type = row["media_type"]
            conflict = db.execute(
                "SELECT id FROM items_live WHERE upc = ? AND media_type = ? AND id != ? LIMIT 1",
                (fields["upc"], effective_media_type, item_id),
            ).fetchone()
            if conflict:
                return _refused("upc_conflict")

        # The form posts `isbn` every time, so an edit that changes it now
        # rewrites isbn10 too — #54's second half.
        try:
            if fields:
                update_item_fields(db, item_id, fields)
            if periodical is not None:
                db.execute(
                    "UPDATE periodical_issues SET barcode_ean = ?, barcode_supplement = ?, "
                    "updated_at = datetime('now') WHERE item_id = ?", (*periodical, item_id))
        except IdentifierInTrash as e:
            db.rollback()
            return _refused(e.code, trashed=e.item_id)
        except ItemValueError as e:
            db.rollback()
            return _refused(e.code, field=e.field)
        except sqlite3.IntegrityError:
            db.rollback()
            # Close the race between the explicit duplicate lookup and write.
            if "upc" in fields or periodical is not None:
                return _refused("upc_conflict")
            return HTMLResponse("Update conflicts with existing catalogue data", status_code=409)

        # Guarded: `fields` only carries series_name when the form submitted it
        # (a cover-only or partial POST omits it entirely).
        if "series_name" in fields and old_series_name:
            new_series_name = fields["series_name"]
            if old_series_name.strip().casefold() != (new_series_name or "").strip().casefold():
                gc_orphaned_series_meta(db, old_series_name)

        # Inside the block: a failed write rolls the row update back (G118).
        if cover_content is not None:
            covers.write_uploaded_cover(item_id, cover_content, db=db)
        if downloaded is not None:
            provenance = librarything.selected_provenance(
                form.get("cover_choice_edition_isbn"), form.get("cover_choice_edition_language"))
            stage.store(db, downloaded, edition=provenance,
                        user=principal, library_ids=library_ids)

    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/items/{item_id}/reading-status")
async def set_reading_status(request: Request, item_id: int, status: str = Form(""), _=Depends(require_catalogue_role("viewer"))):
    """Quick-toggle reading status from detail or browse page."""
    templates = request.app.state.templates
    # `""` is the clear action; anything else is the funnel's to judge (#54).
    uid = int(request.state.user["id"])
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            require_item_role(db, item_id, "viewer")
            personal_state = user_state.set_reading_status(db, uid, item_id, status or None)
            item = db.execute(
                "SELECT i.*, l.name AS location_name FROM items_live i "
                "LEFT JOIN locations l ON i.location_id = l.id WHERE i.id = ?", (item_id,)
            ).fetchone()
            reading_history = user_state.get_reading_history(db, uid, item_id)
            # A household integration token is not a per-user account. Keep
            # native single-user sync, never publish one person's private
            # status into a shared Hardcover account on a multi-user install.
            push_token = get_setting(db, "hardcover_token") if feature_enabled("hardcover") and db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1 else None
            private_link = get_setting(db, f"hardcover_user_book:{uid}:{item_id}") if push_token else ""
            push_id = int(private_link) if private_link.isascii() and private_link.isdigit() and int(private_link) > 0 else None
    except (ValueError, TypeError) as exc:
        return PlainTextResponse(str(exc), status_code=400)
    except LookupError:
        return HTMLResponse("Not found", status_code=404)

    # Fire-and-forget: push to Hardcover if linked; a retyped disc or game keeps its IDs
    if push_token and push_id and item["media_type"] in BOOK_MEDIA_TYPES:
        asyncio.create_task(_push_status_to_hardcover(
            item_id, status, user=dict(request.state.user), token=push_token,
            hardcover_user_book_id=push_id), context=Context())

    label = status_labels(item["media_type"]).label(status) or "Cleared"
    resp = templates.TemplateResponse(
        request, "fragments/personal_state.html",
        {"item": item, "personal_state": personal_state, "reading_history": reading_history,
         "personal_status_labels": user_state.status_labels(item["media_type"])},
    )
    resp.headers["HX-Trigger"] = items_common._toast_header(f"Status: {label}")
    return resp


async def _push_status_to_hardcover(item_id: int, status: str, *, user, token, hardcover_user_book_id):
    """Fresh-context job with an explicit user and exact preflighted target."""
    try:
        with get_db() as db:
            principal = user_state_items.job_principal(db, user)
            if not token or not feature_enabled("hardcover") or principal is None or db.execute("SELECT COUNT(*) FROM users").fetchone()[0] != 1:
                return
            if get_setting(db, "hardcover_token") != token:
                return
            require_item_role(db, item_id, "viewer", user=principal)
            item = db.execute(
                "SELECT media_type FROM items_live WHERE id = ?", (item_id,)
            ).fetchone()
            private_link = get_setting(db, f"hardcover_user_book:{principal['id']}:{item_id}")
            state = user_state.get_state(db, int(principal["id"]), item_id)
            if (not item or item["media_type"] not in BOOK_MEDIA_TYPES
                    or private_link != str(hardcover_user_book_id)
                    or state["reading_status"] != (status or None)):
                return

        hc_status_id = hardcover.STATUS_TO_HC.get(status)
        await hardcover.update_user_book(token, hardcover_user_book_id, status_id=hc_status_id)
        logger.debug("Pushed status '%s' to Hardcover for item %d", status, item_id)
    except Exception:
        logger.warning("Failed to push status to Hardcover for item %d", item_id, exc_info=True)


@router.delete("/items/{item_id}")
async def delete_item(item_id: int, _=Depends(require_catalogue_role("editor"))):
    """Move one item to Trash. Reversible from the Trash page: its copies,
    loans, tags, links and scan history stay attached and return with it.
    A second call on the same id is a no-op and still answers 200."""
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            require_item_role(db, item_id, "editor", include_deleted=True)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return JSONResponse({"ok": False, "message": "Item not found"}, status_code=404)
        row = db.execute("SELECT title FROM items_live WHERE id = ?", (item_id,)).fetchone()
        title = row["title"] if row else "Item"
        item_write.trash_item(db, item_id)
    resp = HTMLResponse('{"ok": true}', headers={"Content-Type": "application/json"})
    resp.headers["HX-Trigger"] = items_common._toast_header(f"Moved to Trash: {title[:50]}")
    return resp


@router.get("/recent-scans")
async def recent_scans(
    request: Request,
    mode: str = "add",
    _=Depends(require_catalogue_role("editor")),
):
    """Return recent scan results filtered by mode. Returns HTMX fragment."""
    templates = request.app.state.templates
    with get_db() as db:
        visible, params = scan_history.visibility_condition(request.state.user)
        scans = db.execute(
            "SELECT sl.*, i.id AS live_item_id, i.title, i.authors, i.cover_path "
            "FROM scan_log sl LEFT JOIN items_live i ON sl.item_id = i.id "
            f"WHERE sl.mode = ? AND ({visible}) "
            "ORDER BY sl.created_at DESC LIMIT 20",
            (mode, *params),
        ).fetchall()
    return templates.TemplateResponse(
        request, "fragments/recent_scans.html", {"recent_scans": scans},
    )


@router.post("/igdb/test-key")
async def test_igdb_key(request: Request, _=Depends(require_role("admin"))):
    """Test IGDB (Twitch) credentials."""
    try:
        data = await request.json()
    except (TypeError, ValueError):
        return {"ok": False, "message": "Invalid request body"}
    if not isinstance(data, dict) or any(data.get(key) is not None and not isinstance(data[key], str) for key in ("client_id", "client_secret")):
        return {"ok": False, "message": "Invalid request body"}
    client_id = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        # Masked fields post empty — fall back to the stored credentials
        with get_db() as db:
            client_id = client_id or get_setting(db, "igdb_client_id")
            client_secret = client_secret or get_setting(db, "igdb_client_secret")
    if not client_id or not client_secret:
        return {"ok": False, "message": "Both Client ID and Client Secret are required"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        return await igdb.test_credentials(client_id, client_secret, client)
