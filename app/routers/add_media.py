"""Native, explicitly submitted Add/Wishlist orchestration.

Search and preview only read. Native operations own the catalogue mappings;
this host owns the real transaction and discloses receipts only after its exit.
Transport/CSRF/namespace policy and registration belong to the parent HTTP hook.
"""

import asyncio
from dataclasses import asdict
from html import escape
import math
import re

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import add_media_context as context
from app import i18n
from app.add_media_types import AddDetails, AddReceipt, CapturedAddActor, ManualValues
from app.auth import require_role
from app.config import BOOK_MEDIA_TYPES, HTTP_TIMEOUT, MEDIA_TYPES, MUSIC_MEDIA_TYPES, STATUS_MEDIA_TYPES
from app.database import get_db, get_game_platforms, get_setting
from app.features import feature_enabled
from app.routers import items, items_catalog, items_common
from app.routers import music_matching as music_routes
from app.services import discogs, igdb, lists, music_matching, music_review, openlibrary, tmdb, upc, upcitemdb
from app.services.item_write import ItemValueError

router = APIRouter(prefix="/add", dependencies=[Depends(require_role("editor"))])

_SEARCH_KEYS = ("mode", "media_type", "method", "provider", "q", "artist", "title_hint", "platform", "country")
_SUMMARY_KEYS = {"title", "authors", "publish_year", "country", "label", "catalog_number", "format_summary", "description"}
_BOOK_KEYS = {"title", "subtitle", "authors", "publisher", "publish_year", "page_count", "description",
              "series_name", "series_position", "language", "media_type", "isbn"}
_GAME_KEYS = {"title", "description", "publisher", "publish_year", "series_name", "media_type", "platform"}
_DVD_KEYS = {"title", "description", "publish_year", "media_type"}
_MANUAL_KEYS = ("title", "authors", "publisher", "publish_year", "platform", "series_name", "language",
                "identifier_kind", "identifier", "location_id", "tags", "reading_status")
_ERROR_TEXT = {
    "invalid_form": i18n.N_("The submitted form could not be read. Reload Add Media and try again."),
    "invalid_input": i18n.N_("Check the selected format, method and entered values."),
    "actor_changed": i18n.N_("Your session changed or expired. Reload Add Media before continuing."),
    "stale_review": i18n.N_("That preview expired or changed. Search and preview the selection again."),
    "intent_changed": i18n.N_("The add intent changed. Search and preview the selection again."),
    "format_conflict": i18n.N_("The selected format contradicts the provider's release details. Choose the correct pressing."),
    "music_disabled": i18n.N_("Music matching is turned off. You can still add music manually."),
    "outcome_unknown": i18n.N_("The save outcome is unknown. Check your collection before submitting again."),
}


def _entry(request) -> CapturedAddActor:
    # Never resolve a replacement actor after an await. AuthMiddleware captures
    # this immutable value before body buffering, including the cookie expiry.
    actor = getattr(request.state, "add_media_actor", None)
    if not isinstance(actor, CapturedAddActor):
        raise context.ActorChanged()
    with get_db() as db:
        context.revalidate_actor(db, actor)
    return actor


def _revalidate(actor, *, music=False):
    with get_db() as db:
        if music:
            music_routes._check_add_actor(db, actor)
        else:
            context.revalidate_actor(db, actor)


async def _submitted(request):
    actor = _entry(request)
    # The parent supplies the strict, bounded, cached FormData. Do not run
    # request.form()/parse_qs again and accidentally accept different bytes.
    from app.add_media_http import form
    data = form(request)
    _revalidate(actor)
    actor, intent = context.verify_context(data.get("context", ""), actor)
    _revalidate(actor)
    return data, actor, intent


def _input(intent=None):
    if intent is None:
        return dict(mode="add", media_type="", method="title", provider="auto", q="", artist="",
                    title_hint="", platform="", country="")
    values = asdict(intent)
    return {key: values[{"q": "query", "provider": "music_provider"}.get(key, key)] for key in _SEARCH_KEYS}


def _render(request, actor, *, intent=None, inputs=None, results=(), preview=None, manual=None,
            receipt=None, error_code=None, provider_outcome=None, host_warning=None, status_code=200):
    _revalidate(actor)
    with get_db() as db:
        platforms = get_game_platforms(db)
    add = {
        "input": inputs if inputs is not None else _input(intent),
        "context": context.sign_context(actor), "intent": intent,
        "intent_context": context.sign_context(actor, intent) if intent is not None else "",
        "results": tuple(results), "preview": preview, "manual": manual, "receipt": receipt,
        "error_code": error_code, "provider_outcome": provider_outcome, "host_warning": host_warning,
        "media_types": MEDIA_TYPES, "platforms": platforms, "music_enabled": feature_enabled("music"),
        "status_types": STATUS_MEDIA_TYPES,
    }
    return request.app.state.templates.TemplateResponse(
        request, "add_media.html", {"add": add}, status_code=status_code, headers={"Cache-Control": "no-store"})


def _error(request, error, *, actor=None, intent=None, inputs=None, manual=None):
    code = error.code if isinstance(error, context.AddMediaError) else "stale_review"
    status = error.status_code if isinstance(error, context.AddMediaError) else 409
    if code in {"actor_changed", "outcome_unknown"}:
        return _fixed_error(code, status)
    try:
        actor = actor or _entry(request)
        return _render(request, actor, intent=intent, inputs=inputs, manual=manual,
                       error_code=code, status_code=status)
    except context.ActorChanged as changed:
        return _error(request, changed)
    except context.AddMediaError:
        # An otherwise bounded Unicode intent may be unrepresentable in the
        # signed marker. Refuse it without enlarging any native ticket limit or
        # recursively trying to sign the same oversized context in an error page.
        if intent is not None:
            try:
                return _render(request, actor, inputs=inputs if inputs is not None else _input(intent),
                               error_code="invalid_input", status_code=422)
            except context.ActorChanged:
                return _fixed_error("actor_changed", 401)
            except context.AddMediaError:
                pass
        return _fixed_error("invalid_input", 422)


def _fixed_error(code, status):
    return HTMLResponse(
        f'<p role="alert" data-testid="add-media-error" data-error-code="{code}">'
        f'{escape(i18n.gettext(_ERROR_TEXT[code]))}</p>', status_code=status, headers={"Cache-Control": "no-store"})


def _intent(data):
    return context.intent_from_dict({
        "mode": data.get("mode", ""), "media_type": data.get("media_type", ""),
        "method": data.get("method", ""), "query": data.get("q", ""),
        "artist": data.get("artist", ""), "title_hint": data.get("title_hint", ""),
        "platform": data.get("platform", ""), "country": data.get("country", ""),
        "music_provider": data.get("provider", ""),
    })


def _platform(intent):
    if intent.media_type == "video_game" and intent.platform:
        with get_db() as db:
            configured = get_game_platforms(db)
        if intent.platform not in configured or (intent.method != "manual" and intent.platform not in igdb.PLATFORM_IDS):
            raise context.AddMediaError("invalid_input", 422)


def _text(value, maximum, *, required=False):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value)
            or (required and not value.strip())):
        raise context.AddMediaError("invalid_input", 422)
    return value


def _details(data, media_type):
    raw = data.get("location_id", "")
    if raw:
        if not re.fullmatch(r"[0-9]{1,19}", raw) or not 0 < int(raw) <= 2**63 - 1:
            raise context.AddMediaError("invalid_input", 422)
        location = int(raw)
    else:
        location = None
    tags = _text(data.get("tags", ""), 256)
    status = data.get("reading_status", "")
    if status not in {"", "want_to_read", "reading", "read"} or (status and media_type not in STATUS_MEDIA_TYPES):
        raise context.AddMediaError("invalid_input", 422)
    return AddDetails(location, tags, status)


def _manual_prefill(intent, metadata=None):
    values = {key: "" for key in _MANUAL_KEYS}
    values["identifier_kind"] = "none"
    values["platform"] = intent.platform
    if intent.method in {"title", "manual"}:
        values["title"] = intent.query
    elif intent.media_type in MUSIC_MEDIA_TYPES:
        values["title"] = intent.title_hint
    if intent.media_type in MUSIC_MEDIA_TYPES:
        values["authors"] = intent.artist
    if intent.method in {"isbn", "barcode"}:
        values.update(identifier_kind=intent.method, identifier=intent.query)
    for key in ("title", "authors", "publisher", "publish_year", "series_name", "language"):
        if metadata and metadata.get(key) is not None:
            values[key] = str(metadata[key])
    return values


def _manual_values(data, intent):
    values = {key: _text(data.get(key, ""), 200, required=key == "title")
              for key in ("title", "authors", "publisher", "series_name")}
    language = _text(data.get("language", ""), 35)
    platform = _text(data.get("platform", ""), 100)
    if platform and intent.media_type != "video_game":
        raise context.AddMediaError("invalid_input", 422)
    year = data.get("publish_year", "")
    if year and (not re.fullmatch(r"[0-9]{4}", year) or int(year) == 0):
        raise context.AddMediaError("invalid_input", 422)
    kind, identifier = data.get("identifier_kind", ""), data.get("identifier", "")
    isbn13 = barcode = None
    if kind == "none":
        if identifier:
            raise context.AddMediaError("invalid_input", 422)
    elif kind == "isbn":
        isbn13 = context.strict_isbn(identifier)
    elif kind == "barcode":
        barcode = upc.normalize_upc(context.strict_barcode(identifier))
    else:
        raise context.AddMediaError("invalid_input", 422)
    return ManualValues(values["title"].strip(), values["authors"] or None, intent.media_type, isbn13, barcode,
                        values["publisher"] or None, int(year) if year else None, platform or None,
                        values["series_name"] or None, language or None)


def _summary(row, *, music=False):
    return {
        "title": row.get("title") or "", "authors": row.get("artist_credit" if music else "authors") or "",
        "publish_year": row.get("publish_year") if not music else (row.get("release_date") or ""),
        "country": row.get("country") or "", "label": row.get("label") or "",
        "catalog_number": row.get("catalog_number") or "", "format_summary": row.get("format_summary") or "",
        "description": row.get("description") or row.get("disambiguation") or "",
    }


def _check_summary(value):
    if not isinstance(value, dict) or set(value) != _SUMMARY_KEYS or not isinstance(value["title"], str) or not value["title"].strip():
        raise context.ProviderUnavailable("unresolved")
    for key, scalar in value.items():
        if key == "publish_year" and (scalar is None or type(scalar) is int):
            continue
        if not isinstance(scalar, str):
            raise context.ProviderUnavailable("unresolved")
    return value


def _sign_selection(actor, intent, provider, identifier, summary):
    candidate = {"actor": asdict(actor), "intent": context.intent_dict(intent), "provider": provider,
                 "identifier": identifier, "summary": _check_summary(summary)}
    try:
        token = music_review.sign(candidate, user_id=actor.id, item_id=0, fingerprint="new", purpose=context.SELECTION_PURPOSE)
    except music_review.ReviewError:
        raise context.AddMediaError("invalid_input", 422) from None
    if len(token) > context.MAX_SELECTION:
        raise context.AddMediaError("invalid_input", 422)
    return token


def _identifier(provider, identifier, intent):
    if not isinstance(identifier, str) or not isinstance(provider, str):
        raise context.AddMediaError()
    if provider == "musicbrainz" and intent.media_type in MUSIC_MEDIA_TYPES:
        valid = music_matching.release_id(identifier)
        if valid and valid == identifier and intent.music_provider in {"auto", provider}:
            return identifier
    elif provider == "discogs" and intent.media_type in MUSIC_MEDIA_TYPES:
        valid = discogs._public_release_id(identifier)
        if valid and str(valid) == identifier and intent.music_provider in {"auto", provider}:
            return identifier
    elif provider == "igdb" and intent.media_type == "video_game":
        if re.fullmatch(r"[0-9]{1,19}", identifier) and 0 < int(identifier) <= 2**63 - 1:
            return identifier
    elif provider == "tmdb" and intent.media_type == "dvd":
        if re.fullmatch(r"[0-9]{1,19}", identifier) and 0 < int(identifier) <= 2**63 - 1:
            return identifier
    elif intent.media_type in BOOK_MEDIA_TYPES and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", provider):
        valid = context.strict_isbn(identifier)
        if valid == identifier:
            return identifier
    raise context.AddMediaError()


def _selection(token, actor, intent):
    candidate = context.verify_host_ticket(token, actor=actor, purpose=context.SELECTION_PURPOSE, limit=context.MAX_SELECTION)
    if set(candidate) != {"actor", "intent", "provider", "identifier", "summary"}:
        raise context.AddMediaError()
    context.bound_actor(candidate["actor"], actor)
    if context.intent_from_dict(candidate["intent"]) != intent:
        raise context.AddMediaError("intent_changed")
    _identifier(candidate["provider"], candidate["identifier"], intent)
    _check_summary(candidate["summary"])
    return candidate


def _outcome(answer, *, music=False):
    if answer.outcome in {"no_credential", "rejected", "rate_limited", "transport_failed"}:
        return answer.outcome
    return "no_match" if music and answer.outcome in {"found", "no_match"} else "unresolved"


async def _search_provider(intent):
    """One selected family; existing helpers retain their own pacing/retries."""
    music = intent.media_type in MUSIC_MEDIA_TYPES
    with get_db() as db:
        if music:
            token = get_setting(db, "discogs_token") or ""
        elif intent.method == "isbn":
            hc_token = get_setting(db, "hardcover_token") or None
            google_key = get_setting(db, "google_books_api_key") or None
        elif intent.media_type == "video_game":
            client_id, secret = get_setting(db, "igdb_client_id"), get_setting(db, "igdb_client_secret")
        elif intent.media_type == "dvd":
            tmdb_key = get_setting(db, "tmdb_api_key")
        search_lang = get_setting(db, "metadata_search_lang") or "en"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        if music:
            title = intent.query if intent.method == "title" else intent.title_hint
            options = dict(title=title, artist=intent.artist,
                           barcode=context.strict_barcode(intent.query) if intent.method == "barcode" else "",
                           catalog_number=intent.query if intent.method == "catalog_number" else "",
                           format=intent.media_type, country=intent.country)
            if intent.music_provider == "auto":
                answer = await music_matching.search_catalogue(client, **options, discogs_token=token)
            elif intent.music_provider == "musicbrainz":
                answer = music_matching._catalogue_summaries(music_matching._catalogue_search_result(
                    await music_matching.search(client, **options)))
            else:
                answer = music_matching._catalogue_search_result(await discogs.search_public_releases(
                    title, client, token=token, artist=intent.artist or None, barcode=options["barcode"] or None,
                    catalog_number=options["catalog_number"] or None, media_type=intent.media_type,
                    country=intent.country or None, limit=20))
            cap = 20 if intent.music_provider == "discogs" or answer.provider == "discogs" else 30
        elif intent.method == "barcode":
            answer = await upcitemdb.lookup(context.strict_barcode(intent.query), client)
            return answer, [answer.payload] if answer.found and isinstance(answer.payload, dict) else []
        elif intent.method == "isbn":
            metadata, source, _, answer = await items_common._lookup_metadata(
                context.strict_isbn(intent.query), hc_token, client, google_api_key=google_key)
            return answer, [{**metadata, "isbn": context.strict_isbn(intent.query), "provider": source}] if metadata else []
        elif intent.media_type in BOOK_MEDIA_TYPES:
            answer = await openlibrary.search_books(intent.query, client, limit=10, lang=search_lang)
            cap = 10
        elif intent.media_type == "video_game":
            if not client_id or not secret:
                raise context.ProviderUnavailable("no_credential")
            answer = await igdb.search_games(intent.query, client_id, secret, client, platform=intent.platform or None, limit=10)
            cap = 10
        else:
            if not tmdb_key:
                raise context.ProviderUnavailable("no_credential")
            answer = await tmdb.search_movies(intent.query, tmdb_key, client, limit=10)
            cap = 10
    rows = answer.payload if answer.found and isinstance(answer.payload, list) else []
    return answer, rows[:cap]


def _search_results(rows, answer, actor, intent):
    results = []
    manual = _manual_prefill(intent)
    for row in rows:
        if not isinstance(row, dict):
            raise context.ProviderUnavailable("unresolved")
        summary = _check_summary(_summary(row, music=intent.media_type in MUSIC_MEDIA_TYPES))
        provider = answer.provider
        identifier = ""
        if intent.media_type in MUSIC_MEDIA_TYPES:
            provider = row.get("provider", answer.provider)
            identifier = str(row.get("provider_release_id") or row.get(
                "discogs_release_id" if provider == "discogs" else "musicbrainz_release_id") or "")
        elif intent.method == "barcode":
            # One product-facts lookup, never a mutating scan or hidden title ladder.
            manual = _manual_prefill(intent, {"title": row.get("title")})
            summary["description"] = i18n.gettext("Retail product facts only. Search by title explicitly or add manually.")
            results.append({"provider": "upcitemdb", "identifier": context.strict_barcode(intent.query), **summary, "selection": ""})
            continue
        elif intent.media_type in BOOK_MEDIA_TYPES:
            provider = row.get("provider", "openlibrary")
            try:
                identifier = context.strict_isbn(row.get("isbn", ""))
            except context.AddMediaError:
                # An Open Library work without a valid edition ISBN is manual,
                # not an invented digital/audiobook/physical edition identity.
                manual = _manual_prefill(intent, row)
        else:
            summary["authors"] = ""  # native game insertion does not store developer
            value = row.get("igdb_id" if intent.media_type == "video_game" else "tmdb_id")
            identifier = str(value) if type(value) is int else ""
        selection = ""
        if identifier:
            _identifier(provider, identifier, intent)
            selection = _sign_selection(actor, intent, provider, identifier, summary)
        results.append({"provider": provider, "identifier": identifier, **summary, "selection": selection})
    return tuple(results), manual


def _native_metadata(raw, intent, identifier):
    if not isinstance(raw, dict) or not isinstance(raw.get("title"), str) or not raw["title"].strip():
        raise context.ProviderUnavailable("unresolved")
    keys = _BOOK_KEYS if intent.media_type in BOOK_MEDIA_TYPES else (_GAME_KEYS if intent.media_type == "video_game" else _DVD_KEYS)
    metadata = {key: raw.get(key) for key in keys}
    metadata["media_type"] = intent.media_type
    if intent.media_type in BOOK_MEDIA_TYPES:
        metadata["isbn"] = context.strict_isbn(identifier)
    elif intent.media_type == "video_game":
        metadata["platform"] = intent.platform or None
    for key, value in metadata.items():
        if value is None or isinstance(value, str):
            continue
        if key in {"publish_year", "page_count", "series_position"} and type(value) in {int, float}:
            try:
                if math.isfinite(value):
                    continue
            except OverflowError:
                pass
        raise context.ProviderUnavailable("unresolved")
    return metadata


async def _catalogue_preview(selection, actor, intent):
    identifier, provider = selection["identifier"], selection["provider"]
    hc_ids = {}
    if intent.media_type == "dvd":
        # TMDb has no native exact-details helper: the signed server search
        # snapshot IS this selected movie, not a new first-title lookup.
        metadata = _native_metadata(selection["summary"], intent, identifier)
        source = "tmdb"
    else:
        with get_db() as db:
            context.revalidate_actor(db, actor)
            if intent.media_type in BOOK_MEDIA_TYPES:
                hc_token, google_key = get_setting(db, "hardcover_token") or None, get_setting(db, "google_books_api_key") or None
            else:
                client_id, secret = get_setting(db, "igdb_client_id"), get_setting(db, "igdb_client_secret")
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            if intent.media_type in BOOK_MEDIA_TYPES:
                raw, source, hc_ids, answer = await items_common._lookup_metadata(
                    identifier, hc_token, client, google_api_key=google_key)
                _revalidate(actor)
                if not raw:
                    raise context.ProviderUnavailable(_outcome(answer))
            else:
                if not client_id or not secret:
                    raise context.ProviderUnavailable("no_credential")
                raw = await igdb.lookup_game(int(identifier), client_id, secret, client)
                _revalidate(actor)
                if not isinstance(raw, dict) or type(raw.get("igdb_id")) is not int or str(raw["igdb_id"]) != identifier:
                    raise context.ProviderUnavailable("unresolved")
                source = "igdb"
        metadata = _native_metadata(raw, intent, identifier)
    _revalidate(actor)
    hc_ids = {key: hc_ids.get(key) for key in ("hardcover_book_id", "hardcover_edition_id")} if intent.media_type in BOOK_MEDIA_TYPES else {}
    candidate = {"actor": asdict(actor), "intent": context.intent_dict(intent), "provider": provider,
                 "identifier": identifier, "metadata": metadata, "source": source, "hc_ids": hc_ids}
    _catalogue_candidate(candidate, actor, intent)
    try:
        ticket = music_review.sign(candidate, user_id=actor.id, item_id=0, fingerprint="new", purpose=context.CATALOGUE_PURPOSE)
    except music_review.ReviewError:
        raise context.AddMediaError("invalid_input", 422) from None
    return {"kind": "book" if intent.media_type in BOOK_MEDIA_TYPES else intent.media_type, "provider": source,
            "candidate": candidate, "ticket": ticket, "context": context.sign_context(actor, intent), "intent": intent}


def _catalogue_candidate(candidate, actor, intent):
    if intent.media_type not in BOOK_MEDIA_TYPES | {"video_game", "dvd"}:
        raise context.AddMediaError("intent_changed")
    if not isinstance(candidate, dict) or set(candidate) != {"actor", "intent", "provider", "identifier", "metadata", "source", "hc_ids"}:
        raise context.AddMediaError()
    context.bound_actor(candidate["actor"], actor)
    if context.intent_from_dict(candidate["intent"]) != intent:
        raise context.AddMediaError("intent_changed")
    _identifier(candidate["provider"], candidate["identifier"], intent)
    if not isinstance(candidate["source"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", candidate["source"]):
        raise context.AddMediaError()
    keys = _BOOK_KEYS if intent.media_type in BOOK_MEDIA_TYPES else (_GAME_KEYS if intent.media_type == "video_game" else _DVD_KEYS)
    metadata = candidate["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != keys:
        raise context.AddMediaError()
    try:
        if _native_metadata(metadata, intent, candidate["identifier"]) != metadata:
            raise context.AddMediaError()
    except context.ProviderUnavailable:
        raise context.AddMediaError() from None
    hc_ids = candidate["hc_ids"]
    if intent.media_type in BOOK_MEDIA_TYPES:
        if not isinstance(hc_ids, dict) or set(hc_ids) != {"hardcover_book_id", "hardcover_edition_id"}:
            raise context.AddMediaError()
        if any(value is not None and (type(value) is not int or not 0 < value <= 2**63 - 1) for value in hc_ids.values()):
            raise context.AddMediaError()
    elif hc_ids or candidate["source"] != candidate["provider"]:
        raise context.AddMediaError()
    return candidate


def _stored_receipt(db, result):
    row = db.execute(f"SELECT i.*, {lists.WISHLISTED_SQL} AS wishlisted FROM items_live i WHERE i.id = ?",
                     (result.item_id,)).fetchone()
    if not row:
        raise RuntimeError("Native add returned no live row")
    mode = "add" if row["owned"] else ("wishlist" if row["wishlisted"] else None)
    return AddReceipt(result.status, mode, row["media_type"], row["id"], row["source"],
                      row["title"], row["authors"], row["cover_path"])


def _finish(request, actor, intent, receipt, *, log_code=None):
    warning = None
    if log_code is not None:
        try:
            # Native nonmusic logging is postcommit and outside the writer lock.
            items_common._log_scan(log_code, intent.media_type, receipt.status, receipt.item_id, intent.mode)
        except Exception:
            warning = "log_failed"
    _revalidate(actor)
    candidate = {"actor": asdict(actor), "status": receipt.status, "item_id": receipt.item_id,
                 "mode": receipt.actual_mode, "media_type": receipt.actual_media_type, "source": receipt.source,
                 "cover_warning": receipt.cover_warning, "host_warning": warning}
    token = music_review.sign(candidate, user_id=actor.id, item_id=0, fingerprint="new", purpose=context.RECEIPT_PURPOSE)
    if len(token) > context.MAX_CONTEXT:
        raise context.AddMediaError("outcome_unknown", 503)
    return RedirectResponse(str(request.url_for("add_media_page").include_query_params(receipt=token)),
                            status_code=303, headers={"Cache-Control": "no-store"})


def _receipt(token, entry_actor):
    candidate = context.verify_host_ticket(token, actor=entry_actor, purpose=context.RECEIPT_PURPOSE, limit=context.MAX_CONTEXT)
    if set(candidate) != {"actor", "status", "item_id", "mode", "media_type", "source", "cover_warning", "host_warning"}:
        raise context.AddMediaError()
    actor = context.original_actor(candidate["actor"], entry_actor)
    if (not isinstance(candidate["status"], str) or candidate["status"] not in {"added", "wishlisted", "promoted", "duplicate", "restored"}
            or type(candidate["item_id"]) is not int or not 0 < candidate["item_id"] <= 2**63 - 1
            or (candidate["mode"] is not None and candidate["mode"] not in ("add", "wishlist"))
            or not isinstance(candidate["media_type"], str) or candidate["media_type"] not in MEDIA_TYPES
            or (candidate["host_warning"] is not None and candidate["host_warning"] != "log_failed")
            or type(candidate["cover_warning"]) is not bool
            or (candidate["source"] is not None and not isinstance(candidate["source"], str))):
        raise context.AddMediaError()
    with get_db() as db:
        context.revalidate_actor(db, actor)
        row = db.execute("SELECT title, authors, cover_path FROM items_live WHERE id = ?", (candidate["item_id"],)).fetchone()
    receipt = AddReceipt(candidate["status"], candidate["mode"], candidate["media_type"], candidate["item_id"], candidate["source"],
                         row["title"] if row else None, row["authors"] if row else None,
                         row["cover_path"] if row else None, candidate["cover_warning"])
    return actor, receipt, candidate["host_warning"]


@router.get("", name="add_media_page")
async def add_media_page(request: Request):
    try:
        actor = _entry(request)
        query = request.query_params
        if set(query) - {"mode", "media_type", "receipt"} or any(len(query.getlist(key)) != 1 for key in query):
            raise context.AddMediaError("invalid_form", 400)
        if "receipt" in query:
            if len(query) != 1:
                raise context.AddMediaError("invalid_form", 400)
            actor, receipt, warning = _receipt(query["receipt"], actor)
            return _render(request, actor, receipt=receipt, host_warning=warning)
        inputs = _input()
        inputs.update({key: query[key] for key in ("mode", "media_type") if key in query})
        if inputs["mode"] not in {"add", "wishlist"} or ("media_type" in query and inputs["media_type"] not in MEDIA_TYPES):
            raise context.AddMediaError("invalid_input", 422)
        return _render(request, actor, inputs=inputs)
    except context.AddMediaError as error:
        return _error(request, error)


@router.post("/search", name="add_media_search")
async def add_media_search(request: Request):
    actor = intent = inputs = None
    try:
        data, actor, marker_intent = await _submitted(request)
        if marker_intent is not None:
            raise context.AddMediaError("intent_changed")
        inputs = {key: data.get(key, "") for key in _SEARCH_KEYS}
        intent = _intent(data)
        _platform(intent)
        manual = _manual_prefill(intent)
        if intent.method == "manual":
            return _render(request, actor, intent=intent, manual=manual)
        if intent.media_type in {"tv_series", "digital_video", "magazine"}:
            return _render(request, actor, intent=intent, manual=manual, provider_outcome="unsupported_lookup")
        _revalidate(actor, music=intent.media_type in MUSIC_MEDIA_TYPES)
        try:
            answer, rows = await asyncio.wait_for(_search_provider(intent), timeout=30)
        except TimeoutError:
            raise context.ProviderUnavailable("search_timeout") from None
        _revalidate(actor, music=intent.media_type in MUSIC_MEDIA_TYPES)
        results, manual = _search_results(rows, answer, actor, intent)
        return _render(request, actor, intent=intent, results=results, manual=manual,
                       provider_outcome=None if results else _outcome(answer, music=intent.media_type in MUSIC_MEDIA_TYPES))
    except context.ProviderUnavailable as error:
        try:
            _revalidate(actor, music=intent.media_type in MUSIC_MEDIA_TYPES)
            return _render(request, actor, intent=intent, manual=_manual_prefill(intent), provider_outcome=error.outcome)
        except context.AddMediaError as changed:
            return _error(request, changed)
    except (context.AddMediaError, music_review.ReviewError) as error:
        return _error(request, error, actor=actor, intent=intent, inputs=inputs)
    except httpx.HTTPError:
        try:
            _revalidate(actor, music=intent.media_type in MUSIC_MEDIA_TYPES)
            return _render(request, actor, intent=intent, manual=_manual_prefill(intent), provider_outcome="transport_failed")
        except context.AddMediaError as error:
            return _error(request, error)


@router.post("/preview", name="add_media_preview")
async def add_media_preview(request: Request):
    actor = intent = None
    try:
        data, actor, intent = await _submitted(request)
        if intent is None or intent.method == "manual" or intent.media_type in {"tv_series", "digital_video", "magazine"}:
            raise context.AddMediaError("intent_changed")
        selection = _selection(data.get("selection", ""), actor, intent)
        _revalidate(actor, music=intent.media_type in MUSIC_MEDIA_TYPES)
        if intent.media_type in MUSIC_MEDIA_TYPES:
            result = await music_routes._preview_release_operation(
                user_id=actor.id, release_id=selection["identifier"], provider=selection["provider"],
                requested_format=intent.media_type, intent=intent, actor=actor)
            _revalidate(actor, music=True)
            candidate = result.candidates[0]
            preview = {"kind": "music", "provider": selection["provider"], "candidate": candidate,
                       "ticket": candidate["ticket"], "context": context.sign_context(actor, intent), "intent": intent}
            manual = _manual_prefill(intent, {"title": candidate["fields"].get("title"), "authors": candidate["fields"].get("authors")})
        else:
            preview = await _catalogue_preview(selection, actor, intent)
            manual = _manual_prefill(intent, preview["candidate"]["metadata"])
        return _render(request, actor, intent=intent, preview=preview, manual=manual)
    except context.ProviderUnavailable as error:
        try:
            _revalidate(actor, music=intent.media_type in MUSIC_MEDIA_TYPES)
            return _render(request, actor, intent=intent, manual=_manual_prefill(intent), provider_outcome=error.outcome)
        except context.AddMediaError as changed:
            return _error(request, changed)
    except (context.AddMediaError, music_review.ReviewError) as error:
        return _error(request, error, actor=actor, intent=intent)
    except httpx.HTTPError:
        try:
            _revalidate(actor, music=intent.media_type in MUSIC_MEDIA_TYPES)
            return _render(request, actor, intent=intent, manual=_manual_prefill(intent), provider_outcome="unresolved")
        except context.AddMediaError as changed:
            return _error(request, changed)


@router.post("/save", name="add_media_save")
async def add_media_save(request: Request):
    actor = intent = None
    committed = False
    try:
        data, actor, intent = await _submitted(request)
        if intent is None or intent.method == "manual":
            raise context.AddMediaError("intent_changed")
        details = _details(data, intent.media_type)
        ticket = data.get("ticket", "")
        if intent.media_type in MUSIC_MEDIA_TYPES:
            receipt = await music_routes._apply_review_operation(
                user_id=actor.id, ticket=ticket, context_item_id=0, fields=tuple(data.getlist("fields")),
                image=-1, media_type=intent.media_type, location_id=details.location_id, owned=None,
                intent=intent, tags=details.tags, reading_status=details.reading_status, actor=actor)
            committed = True
            return _finish(request, actor, intent, receipt)
        if data.getlist("fields"):
            raise context.AddMediaError("invalid_input", 422)
        candidate = _catalogue_candidate(context.verify_host_ticket(ticket, actor=actor, purpose=context.CATALOGUE_PURPOSE), actor, intent)
        metadata = candidate["metadata"]
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            context.revalidate_actor(db, actor)
            if intent.media_type in BOOK_MEDIA_TYPES:
                result = items_catalog._commit_book_selection(
                    db, metadata, isbn13=candidate["identifier"], media_type=intent.media_type,
                    source=candidate["source"], hc_ids=candidate["hc_ids"], intent=intent, details=details)
            elif intent.media_type == "video_game":
                result = items_catalog._commit_game_selection(db, metadata, platform=intent.platform or None, details=details, intent=intent)
            elif intent.media_type == "dvd":
                result = items_catalog._commit_dvd_selection(db, title=metadata["title"], description=metadata["description"],
                                                           publish_year=metadata["publish_year"], details=details, intent=intent)
            else:
                raise context.AddMediaError("intent_changed")
            receipt = _stored_receipt(db, result)
            committed = True  # tentative until the context manager exits successfully
        return _finish(request, actor, intent, receipt,
                       log_code=candidate["identifier"] if intent.media_type in BOOK_MEDIA_TYPES else "")
    except (context.AddMediaError, music_review.ReviewError) as error:
        if committed and not isinstance(error, context.ActorChanged):
            return _error(request, context.AddMediaError("outcome_unknown", 503))
        return _error(request, error, actor=actor, intent=intent)
    except ItemValueError:
        return _error(request, context.AddMediaError("outcome_unknown", 503) if committed else context.AddMediaError("invalid_input", 422), actor=actor, intent=intent)
    except Exception:
        # Commit/acknowledgement failures are not a proven refusal. No POST retry.
        return _error(request, context.AddMediaError("outcome_unknown", 503))


@router.post("/manual", name="add_media_manual")
async def add_media_manual(request: Request):
    actor = intent = manual = None
    committed = False
    try:
        data, actor, intent = await _submitted(request)
        if intent is None:
            raise context.AddMediaError("intent_changed")
        manual = {key: data.get(key, "") for key in _MANUAL_KEYS}
        values = _manual_values(data, intent)
        details = _details(data, intent.media_type)
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            context.revalidate_actor(db, actor)
            result = items._commit_manual_add(db, values, mode=intent.mode, details=details, intent=intent)
            receipt = _stored_receipt(db, result)
            committed = True
        return _finish(request, actor, intent, receipt, log_code=values.isbn13 or values.upc_code or "")
    except (context.AddMediaError, music_review.ReviewError) as error:
        if committed and not isinstance(error, context.ActorChanged):
            return _error(request, context.AddMediaError("outcome_unknown", 503))
        return _error(request, error, actor=actor, intent=intent, manual=manual)
    except ItemValueError:
        return _error(request, context.AddMediaError("outcome_unknown", 503) if committed else context.AddMediaError("invalid_input", 422),
                      actor=actor, intent=intent, manual=manual)
    except Exception:
        return _error(request, context.AddMediaError("outcome_unknown", 503))
