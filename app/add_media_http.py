"""Finite native Add Media transport hooks; no authentication or catalogue writes."""

import re
from urllib.parse import parse_qsl

from starlette._utils import get_route_path
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app import i18n
from app.add_manual_fields import EXTRA_FIELDS, MULTILINE_FIELDS
from app.http_body import BodyTooLarge, bounded_body


_SEARCH = frozenset({"_csrf", "context", "mode", "media_type", "method", "provider",
                     "q", "artist", "title_hint", "platform", "country"})
_PREVIEW = frozenset({"_csrf", "context", "selection"})
_SAVE = frozenset({"_csrf", "context", "ticket", "location_id", "tags", "reading_status", "fields"})
_MANUAL = frozenset({"_csrf", "context", "title", "authors", "publisher", "publish_year",
                     "platform", "series_name", "language", "identifier_kind", "identifier",
                     "location_id", "tags", "reading_status", "mode", "media_type", "isbn", "upc", "cover_url",
                     *EXTRA_FIELDS})
MANUAL_BODY_BYTES = 327680
_POSTS = {
    "/add/search": (8192, _SEARCH),
    "/add/preview": (8192, _PREVIEW),
    "/add/save": (130000, _SAVE),
    "/add/manual": (MANUAL_BODY_BYTES, _MANUAL),
}
_METHODS = {"/add": "GET", **{path: "POST" for path in _POSTS}}
_MESSAGES = {
    "invalid_form": i18n.N_("Submit each control once using the Add media form."),
    "unsupported_media_type": i18n.N_("Submit the form without attachments or compression."),
    "body_too_large": i18n.N_("The Add media form is too large."),
    "query_too_large": i18n.N_("The Add media address is too long."),
    "actor_changed": i18n.N_("Your session or account changed. Open Add media again before continuing."),
    "not_found": i18n.N_("This Add media address was not found."),
    "method_not_allowed": i18n.N_("Use the method shown on the Add media form."),
    "outcome_unknown": i18n.N_("The request could not be completed. If you were saving, check your library before trying again."),
}
_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]+")


def effective_path(scope: dict) -> str:
    """Use exactly the router's root-path interpretation, not a raw URL prefix."""
    return get_route_path(scope)


def owns(scope: dict) -> bool:
    path = effective_path(scope)
    return path == "/add" or path.startswith("/add/")


def error(code: str, status: int, *, headers: dict | None = None) -> Response:
    """Context-free early errors never echo form values, credentials or exceptions."""
    return JSONResponse(
        {"code": code, "message": i18n.gettext(_MESSAGES[code])},
        status_code=status, headers={"Cache-Control": "no-store", **(headers or {})},
    )


def _pairs(data: bytes, allowed: frozenset, *, maximum: int, multiline=frozenset()) -> list[tuple[str, str]]:
    # Browser native forms encode Unicode as UTF-8 percent escapes. Restrict the
    # wire to ASCII so downstream form parsing cannot reinterpret raw UTF-8 bytes.
    text = data.decode("ascii")
    if _BAD_ESCAPE.search(text):
        raise ValueError
    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True,
                      encoding="utf-8", errors="strict", max_num_fields=maximum)
    for key, value in pairs:
        checked = value.translate({ord(char): None for char in "\r\n\t"}) if key in multiline else value
        if key not in allowed or _CONTROL.search(key) or _CONTROL.search(checked):
            raise ValueError
    return pairs


def _single(pairs: list[tuple[str, str]], *, list_field: str | None = None) -> None:
    seen = set()
    repeated_values = set()
    for key, value in pairs:
        if key == list_field:
            if value in repeated_values or len(repeated_values) >= 6:
                raise ValueError
            repeated_values.add(value)
        elif key in seen:
            raise ValueError
        seen.add(key)


def _query(scope: dict) -> Response | None:
    raw = scope.get("query_string", b"")
    if len(raw) > 4096:
        return error("query_too_large", 414)
    if not raw:
        return None
    try:
        pairs = _pairs(raw, frozenset({"mode", "media_type", "receipt", "tool", "scan_mode"}), maximum=5)
        _single(pairs)
        values = dict(pairs)
        if "receipt" in values:
            if len(values) != 1 or not _TOKEN.fullmatch(values["receipt"]):
                raise ValueError
        if (len(values.get("mode", "")) > 16 or len(values.get("media_type", "")) > 32
                or len(values.get("tool", "")) > 8 or len(values.get("scan_mode", "")) > 16):
            raise ValueError
    except (ValueError, UnicodeError):
        return error("invalid_form", 400)
    return None


def route_refusal(scope: dict) -> Response | None:
    """Refuse only owned noncanonical routes/methods; never consume their bodies."""
    if not owns(scope):
        return None
    path = effective_path(scope)
    method = _METHODS.get(path)
    refusal = None
    if method is None:
        refusal = error("not_found", 404)
    elif scope.get("method") != method:
        refusal = error("method_not_allowed", 405, headers={"Allow": method})
    elif method == "GET":
        return _query(scope)
    elif scope.get("query_string", b""):
        return error("invalid_form", 400)
    if refusal is not None and scope.get("method") == "HEAD":
        refusal.body = b""
        refusal.headers["Content-Length"] = "0"
    return refusal


def _headers(request: Request) -> Response | None:
    headers = request.headers
    for name in ("content-type", "content-encoding", "content-length", "x-csrf-token"):
        if len(headers.getlist(name)) > 1:
            return error("invalid_form", 400)
    length = headers.get("content-length")
    if length is not None and not re.fullmatch(r"[0-9]+", length):
        return error("invalid_form", 400)
    if headers.get("content-encoding", "identity").strip().casefold() != "identity":
        return error("unsupported_media_type", 415)
    content_type = headers.get("content-type", "")
    if effective_path(request.scope) == "/add/manual" and re.fullmatch(
        r'multipart/form-data;\s*boundary=(?:[A-Za-z0-9\x27()+_,./:=?\-]{1,70}|"[A-Za-z0-9\x27()+_,./:=?\-]{1,70}")',
        content_type, flags=re.I,
    ):
        return None
    parts = [part.strip().casefold() for part in content_type.split(";")]
    if (not parts or parts[0] != "application/x-www-form-urlencoded"
            or len(parts) > 2
            or (len(parts) == 2 and parts[1] not in {"charset=utf-8", 'charset="utf-8"'})):
        return error("unsupported_media_type", 415)
    return None


async def _multipart_manual(request):
    """Decode one bounded upload, retaining only text and image bytes; close files."""
    from app.services import covers

    class StrictManualParser(MultiPartParser):
        complete = False

        def on_end(self):
            self.complete = True
            super().on_end()

        def on_part_end(self):
            if self._current_part.file is None:
                try:
                    self._current_part.data.decode("utf-8", errors="strict")
                except UnicodeError:
                    # MultiPartParser closes already-spooled files on this
                    # exception; its default Latin-1 fallback is not accepted.
                    raise MultiPartException("Manual fields must be UTF-8") from None
            super().on_part_end()

    form_data = parser = None
    try:
        parser = StrictManualParser(request.headers, request.stream(), max_files=1, max_fields=64, max_part_size=131072)
        form_data = await parser.parse()
        if not parser.complete:
            raise ValueError
        pairs, names, cover = [], [], None
        for name, value in form_data.multi_items():
            names.append((name, ""))
            if isinstance(value, UploadFile):
                if name != "cover":
                    raise ValueError
                data = await value.read(covers.MAX_COVER_SIZE + 1)
                if data:
                    if not covers.validate_uploaded_cover(data):
                        raise ValueError
                    cover = data
            else:
                if name not in _MANUAL or not isinstance(value, str):
                    raise ValueError
                checked = value.translate({ord(char): None for char in "\r\n\t"}) if name in MULTILINE_FIELDS else value
                if _CONTROL.search(name) or _CONTROL.search(checked):
                    raise ValueError
                pairs.append((name, value))
        _single(names)
        if sum(len(value.encode("utf-8")) for _, value in pairs) > MANUAL_BODY_BYTES:
            raise ValueError
        return FormData(pairs), cover
    finally:
        try:
            if form_data is not None:
                await form_data.close()
        finally:
            if parser is not None:
                # Truncated input may leave an unfinished part outside FormData.
                # Own every spool, including parser errors/cancellation and that tail.
                for temporary in parser._files_to_close_on_error:
                    temporary.close()


async def preflight(request: Request) -> Response | None:
    """Bound and strictly decode once, before either native/header CSRF buffering."""
    refusal = route_refusal(request.scope)
    if refusal is not None:
        return refusal
    rule = _POSTS.get(effective_path(request.scope))
    if rule is None:
        return None
    refusal = _headers(request)
    if refusal is not None:
        return refusal
    limit, allowed = rule
    multipart = request.headers.get("content-type", "").lower().startswith("multipart/form-data;")
    if multipart:
        from app.services.covers import MAX_COVER_SIZE
        limit += MAX_COVER_SIZE
    try:
        body = await bounded_body(request, limit)
    except BodyTooLarge:
        return error("body_too_large", 413)
    length = request.headers.get("content-length")
    if length is not None and (length.lstrip("0") or "0") != str(len(body)):
        return error("invalid_form", 400)
    try:
        if multipart:
            values, cover = await _multipart_manual(request)
            request.state.add_media_cover = cover
        else:
            manual = effective_path(request.scope) == "/add/manual"
            pairs = _pairs(body, allowed, maximum=64 if manual else 32,
                           multiline=MULTILINE_FIELDS if manual else frozenset())
            _single(pairs, list_field="fields" if effective_path(request.scope) == "/add/save" else None)
            values = FormData(pairs)
        for name, maximum in (("context", 4096), ("selection", 6000), ("ticket", 120000)):
            if name in values and (len(values[name]) > maximum or not _TOKEN.fullmatch(values[name])):
                raise ValueError
    except (ValueError, UnicodeError, MultiPartException, HTTPException):
        return error("invalid_form", 400)
    request.state.add_media_body = body
    request.state.add_media_form = values
    return None


def form(request: Request) -> FormData:
    """Return only the body approved by the early middleware, never a loose reparse."""
    value = getattr(request.state, "add_media_form", None)
    if not isinstance(value, FormData):
        raise ValueError("Add media form preflight was not completed")
    return value
