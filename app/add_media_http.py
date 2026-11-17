"""Finite native Add Media transport hooks; no authentication or catalogue writes."""

import re
from urllib.parse import parse_qsl

from starlette._utils import get_route_path
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app import i18n
from app.http_body import BodyTooLarge, bounded_body


_SEARCH = frozenset({"_csrf", "context", "mode", "media_type", "method", "provider",
                     "q", "artist", "title_hint", "platform", "country"})
_PREVIEW = frozenset({"_csrf", "context", "selection"})
_SAVE = frozenset({"_csrf", "context", "ticket", "location_id", "tags", "reading_status", "fields"})
_MANUAL = frozenset({"_csrf", "context", "title", "authors", "publisher", "publish_year",
                     "platform", "series_name", "language", "identifier_kind", "identifier",
                     "location_id", "tags", "reading_status"})
_POSTS = {
    "/add/search": (8192, _SEARCH),
    "/add/preview": (8192, _PREVIEW),
    "/add/save": (130000, _SAVE),
    "/add/manual": (32768, _MANUAL),
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


def _pairs(data: bytes, allowed: frozenset, *, maximum: int) -> list[tuple[str, str]]:
    # Browser native forms encode Unicode as UTF-8 percent escapes. Restrict the
    # wire to ASCII so downstream form parsing cannot reinterpret raw UTF-8 bytes.
    text = data.decode("ascii")
    if _BAD_ESCAPE.search(text):
        raise ValueError
    pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True,
                      encoding="utf-8", errors="strict", max_num_fields=maximum)
    for key, value in pairs:
        if key not in allowed or _CONTROL.search(key) or _CONTROL.search(value):
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
        pairs = _pairs(raw, frozenset({"mode", "media_type", "receipt"}), maximum=3)
        _single(pairs)
        values = dict(pairs)
        if "receipt" in values:
            if len(values) != 1 or not _TOKEN.fullmatch(values["receipt"]):
                raise ValueError
        if len(values.get("mode", "")) > 16 or len(values.get("media_type", "")) > 32:
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
    parts = [part.strip().casefold() for part in content_type.split(";")]
    if (not parts or parts[0] != "application/x-www-form-urlencoded"
            or len(parts) > 2
            or (len(parts) == 2 and parts[1] not in {"charset=utf-8", 'charset="utf-8"'})):
        return error("unsupported_media_type", 415)
    return None


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
    try:
        body = await bounded_body(request, limit)
    except BodyTooLarge:
        return error("body_too_large", 413)
    length = request.headers.get("content-length")
    if length is not None and (length.lstrip("0") or "0") != str(len(body)):
        return error("invalid_form", 400)
    try:
        pairs = _pairs(body, allowed, maximum=32)
        _single(pairs, list_field="fields" if effective_path(request.scope) == "/add/save" else None)
        values = FormData(pairs)
        for name, maximum in (("context", 4096), ("selection", 6000), ("ticket", 120000)):
            if name in values and (len(values[name]) > maximum or not _TOKEN.fullmatch(values[name])):
                raise ValueError
    except (ValueError, UnicodeError):
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
