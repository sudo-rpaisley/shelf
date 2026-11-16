"""Dormant ME2 wire, stored-state and JSON data adapters.

Nothing here authenticates a caller, reads a request body, renders a fragment or
writes an item. Actor tags, form assessments, fingerprints and write knowledge
are untrusted caller reports, not capabilities or transaction receipts. Record
construction deliberately does not validate; the three entry points do.
"""

from dataclasses import dataclass as _dataclass
from typing import Literal as _Literal
import hashlib
import json
import math


Op = _Literal["form", "probe", "apply"]
SqlAtom = str | int | float | None
NativeReason = _Literal["invalid_state", "state_too_large", "context_too_large"]
WireProblem = _Literal[
    "pairing", "multiplicity", "metadata", "query", "path",
    "method_operation", "representation",
]
RefusalCode = _Literal[
    "invalid_protocol", "invalid_form", "auth_required", "forbidden",
    "csrf_required", "csrf_failed", "unavailable", "actor_changed", "stale",
    "body_too_large", "fields_too_large", "unsupported_media", "rate_limited",
    "invalid_isbn", "invalid_upc", "invalid_asin", "invalid_number",
    "invalid_owned", "invalid_wishlisted", "invalid_reading_status",
    "unknown_media_type", "unknown_location", "unknown_platform",
    "upc_conflict", "identifier_in_trash",
]
Field = _Literal[
    "title", "subtitle", "authors", "isbn", "upc", "media_type", "publisher",
    "publish_year", "page_count", "description", "series_name", "series_position",
    "narrator", "duration_mins", "location_id", "notes", "reading_status",
    "date_started", "date_finished", "owned", "wishlisted", "platform",
    "manual_value", "language", "asin", "cover", "_csrf", "from",
]


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class ActorTag:
    id: str
    token_version: str


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class WireScope:
    asgi_type: str
    method: str
    path: str
    raw_path: bytes | None
    root_path: str
    headers: tuple[tuple[bytes, bytes], ...]
    query_string: bytes


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class Echo:
    operation: Op | None
    generation: str | None
    request: str | None
    item_id: str | None
    actor: ActorTag | None


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class WireFields:
    operation: Op
    generation: str
    request: str
    item_id: str
    actor: ActorTag
    state: str | None
    from_raw: bytes | None
    root_path: str


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class NativeWire:
    pass


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class CandidateWire:
    echo: Echo
    fields: WireFields | None
    problem: WireProblem | None


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class WireInputError:
    code: _Literal["input_type", "input_bound"]


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class CoverFingerprint:
    sha256: str
    byte_length: int


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class StoredValues:
    item: tuple[SqlAtom, ...]
    wishlisted: int
    primary: tuple[SqlAtom, ...] | None
    cover: CoverFingerprint | None
    caller_form_assessment: _Literal["supported", "invalid_state", "state_too_large", "context_too_large"]


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class Projection:
    canonical_json: bytes
    baseline: str


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class NeedsNative:
    reason: NativeReason


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class FormData:
    baseline: str
    html: str


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class ProbeData:
    comparison: _Literal["same", "different"]


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class SavedData:
    baseline: str
    cover: _Literal["none", "applied", "ignored"]


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class RefusedData:
    code: RefusalCode
    field: Field | None
    blocking_item_id: str | None


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class NativeRequiredData:
    reason: NativeReason


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class FailureData:
    code: _Literal["internal", "busy"]
    reported_write_knowledge: _Literal["unknown", "pre_write"]


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class ReplySpec:
    status: int
    body: bytes


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class ReplyError:
    code: _Literal["invalid_input", "invalid_combination", "invalid_text", "reply_too_large"]


ReplyData = FormData | ProbeData | SavedData | RefusedData | NativeRequiredData | FailureData

_OPS = ("form", "probe", "apply")
_REASONS = ("invalid_state", "state_too_large", "context_too_large")
_PROBLEMS = ("pairing", "multiplicity", "metadata", "query", "path",
             "method_operation", "representation")
_HEX = "0123456789abcdef"
_HEX_BYTES = b"0123456789abcdefABCDEF"
_UNRESERVED = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
_I64_MIN, _I64_MAX = -9223372036854775808, 9223372036854775807
_ID_MAX = "9223372036854775807"
_PROJECTION_LIMIT, _REPLY_LIMIT = 131072, 1048576
_RESERVED_HEADERS = (
    b"x-shelf-me2", b"x-shelf-me2-op", b"x-shelf-me2-generation",
    b"x-shelf-me2-request", b"x-shelf-me2-actor", b"x-shelf-me2-state",
)
_STATUSES = {
    "invalid_protocol": 400, "invalid_form": 400, "auth_required": 401,
    "forbidden": 403, "csrf_required": 403, "csrf_failed": 403, "unavailable": 404,
    "actor_changed": 409, "stale": 409, "body_too_large": 413,
    "fields_too_large": 413, "unsupported_media": 415, "rate_limited": 429,
    "invalid_isbn": 422, "invalid_upc": 422, "invalid_asin": 422,
    "invalid_number": 422, "invalid_owned": 422, "invalid_wishlisted": 422,
    "invalid_reading_status": 422, "unknown_media_type": 422,
    "unknown_location": 422, "unknown_platform": 422, "upc_conflict": 422,
    "identifier_in_trash": 422,
}
_REFUSAL_FIELDS = {
    "invalid_isbn": ("isbn",), "invalid_upc": ("upc",), "invalid_asin": ("asin",),
    "invalid_number": ("publish_year", "page_count", "duration_mins", "location_id",
                       "series_position", "manual_value"),
    "invalid_owned": ("owned",), "invalid_wishlisted": ("wishlisted",),
    "invalid_reading_status": ("reading_status",), "unknown_media_type": ("media_type",),
    "unknown_location": ("location_id",), "unknown_platform": ("platform",),
    "upc_conflict": ("upc",), "identifier_in_trash": ("isbn", "upc"),
}
_TEXT_SLOTS = (1, 2, 3, 4, 5, 6, 7, 10, 11, 13, 16, 17, 18, 19, 21, 23, 24)
_INT_SLOTS = (8, 9, 14, 15, 20)
_REAL_SLOTS = (12, 22)


def _record(value, cls):
    # Exact slotted records cannot supply attribute hooks. Missing slots still
    # occur via object.__new__/object.__delattr__; do not trust construction.
    return type(value) is cls and all(hasattr(value, name) for name in cls.__slots__)


def _literal(value, choices):
    return type(value) is str and len(value) <= 128 and value in choices


def _hex(value, length):
    return type(value) is str and len(value) == length and all(c in _HEX for c in value)


def _id(value):
    return (type(value) is str and 1 <= len(value) <= 19 and "1" <= value[0] <= "9"
            and all("0" <= c <= "9" for c in value)
            and (len(value) < 19 or value <= _ID_MAX))


def _baseline(value):
    return (type(value) is str and len(value) == 71 and value.startswith("sha256:")
            and _hex(value[7:], 64))


def _actor(value):
    return _record(value, ActorTag) and _id(value.id) and _id(value.token_version)


def _root(value):
    if type(value) is not str or len(value) > 256 or not value.isascii():
        return False
    if value == "":
        return True
    if not value.startswith("/"):
        return False
    return all(part not in ("", ".", "..")
               and all(ord(c) in _UNRESERVED for c in part) for part in value[1:].split("/"))


def _from(value):
    if value is None:
        return True
    if type(value) is not bytes or len(value) > 1011:
        return False
    i = 0
    while i < len(value):
        c = value[i]
        if c in _UNRESERVED or c == 43:
            i += 1
        elif (c == 37 and i + 2 < len(value)
              and value[i + 1] in _HEX_BYTES and value[i + 2] in _HEX_BYTES):
            i += 3
        else:
            return False
    return True


def _scope_error(scope):
    if not _record(scope, WireScope):
        return "input_type"
    if (type(scope.asgi_type) is not str or type(scope.method) is not str
            or type(scope.path) is not str or type(scope.root_path) is not str
            or (scope.raw_path is not None and type(scope.raw_path) is not bytes)
            or type(scope.headers) is not tuple or type(scope.query_string) is not bytes):
        return "input_type"
    if len(scope.headers) > 256 or len(scope.query_string) > 65536 or len(scope.root_path) > 256:
        return "input_bound"
    total = 0
    for pair in scope.headers:
        if (type(pair) is not tuple or len(pair) != 2
                or type(pair[0]) is not bytes or type(pair[1]) is not bytes):
            return "input_type"
        total += len(pair[0]) + len(pair[1])
        if len(pair[0]) > 128 or total > 65536:
            return "input_bound"
    for segment in scope.query_string.split(b"&"):
        if len(segment.partition(b"=")[0]) > 128:
            return "input_bound"
    return None


def _reserved_key(key):
    # Recognition only: each changing pass shortens this already bounded key.
    # Query values (especially from) never go through this operation.
    while True:
        if key.lower().startswith(b"__me2"):
            return True
        decoded = bytearray()
        i = 0
        while i < len(key):
            if (key[i] == 37 and i + 2 < len(key)
                    and key[i + 1] in _HEX_BYTES and key[i + 2] in _HEX_BYTES):
                decoded.append(int(key[i + 1:i + 3], 16))
                i += 3
            else:
                decoded.append(key[i])
                i += 1
        changed = bytes(decoded)
        if changed == key:
            return False
        key = changed


def _single(headers, name):
    values = headers.get(name, ())
    return values[0] if len(values) == 1 else None


def _ascii(value):
    if type(value) is bytes and len(value) <= 128 and value.isascii():
        return value.decode("ascii")
    return None


def _raw_actor(value):
    text = _ascii(value)
    if text is None:
        return None
    parts = text.split(":")
    if len(parts) != 2 or not _id(parts[0]) or not _id(parts[1]):
        return None
    return ActorTag(id=parts[0], token_version=parts[1])


def _path_item(root, raw):
    if not _root(root) or type(raw) is not bytes or len(raw) > 4096 or not raw.isascii():
        return None, None
    path = raw.decode("ascii")
    edit, apply = root + "/item/", root + "/api/items/"
    if path.startswith(edit) and path.endswith("/edit"):
        item_id = path[len(edit):-5]
        return (item_id, "edit") if _id(item_id) else (None, None)
    if path.startswith(apply):
        item_id = path[len(apply):]
        return (item_id, "apply") if _id(item_id) else (None, None)
    return None, None


def decode_wire(scope: WireScope) -> NativeWire | CandidateWire | WireInputError:
    """Recognize raw opt-in markers before validating any candidate semantics."""
    error = _scope_error(scope)
    if error is not None:
        return WireInputError(code=error)
    headers = {}
    hx = False
    for name, value in scope.headers:
        name = name.lower()
        if name.startswith(b"x-shelf-me2") or name == b"accept":
            headers.setdefault(name, []).append(value)
        if name.startswith(b"hx-"):
            hx = True
    segments = ([segment.partition(b"=") for segment in scope.query_string.split(b"&")]
                if scope.query_string else [])
    if not (any(name.startswith(b"x-shelf-me2") for name in headers)
            or any(_reserved_key(key) for key, _, _ in segments)):
        return NativeWire()
    if len(scope.path) > 4096 or (scope.raw_path is not None and len(scope.raw_path) > 4096):
        return WireInputError(code="input_bound")

    operation = _ascii(_single(headers, b"x-shelf-me2-op"))
    generation = _ascii(_single(headers, b"x-shelf-me2-generation"))
    request = _ascii(_single(headers, b"x-shelf-me2-request"))
    actor = _raw_actor(_single(headers, b"x-shelf-me2-actor"))
    item_id, path_kind = _path_item(scope.root_path, scope.raw_path)
    echo = Echo(operation=operation if _literal(operation, _OPS) else None,
                generation=generation if _hex(generation, 32) else None,
                request=request if _hex(request, 32) else None, item_id=item_id, actor=actor)
    query = {}
    query_bad = len(scope.query_string) > 1024
    duplicate = any(len(values) != 1 for values in headers.values())
    for key, separator, value in segments:
        if key in query:
            duplicate = True
        query[key] = value
        if separator != b"=" or key not in (b"__me2", b"from"):
            query_bad = True
    from_raw = query.get(b"from")
    if not _from(from_raw) or (b"__me2" in query and query[b"__me2"] != b"2"):
        query_bad = True
    state = _ascii(_single(headers, b"x-shelf-me2-state"))
    problem = None
    if duplicate:
        problem = "multiplicity"
    elif query_bad:
        problem = "query"
    elif b"__me2" not in query or b"x-shelf-me2" not in headers:
        problem = "pairing"
    elif (any(name not in _RESERVED_HEADERS and name != b"accept" for name in headers)
          or _single(headers, b"x-shelf-me2") != b"2"
          or echo.operation is None or echo.generation is None or echo.request is None
          or actor is None
          or (operation == "form" and b"x-shelf-me2-state" in headers)
          or (operation != "form" and not _baseline(state))):
        problem = "metadata"
    elif hx or _ascii(_single(headers, b"accept")) is None or headers[b"accept"][0].lower() != b"application/json":
        problem = "representation"
    elif (scope.asgi_type != "http" or item_id is None
          or scope.path != scope.raw_path.decode("ascii")):
        problem = "path"
    elif ((operation in ("form", "probe") and (scope.method != "GET" or path_kind != "edit"))
          or (operation == "apply" and (scope.method != "POST" or path_kind != "apply"))):
        problem = "method_operation"
    if problem is not None:
        return CandidateWire(echo=echo, fields=None, problem=problem)
    fields = WireFields(operation=operation, generation=generation, request=request,
                        item_id=item_id, actor=actor, state=state, from_raw=from_raw,
                        root_path=scope.root_path)
    return CandidateWire(echo=echo, fields=fields, problem=None)


def _text_measure(value, cap):
    """UTF-8 bytes and JSON-string bytes; -1 surrogate, -2 character bound.

    Scan only bounded exact strings, including their tail: a surrogate does not
    become valid merely because escaped byte cost crossed the cap first.
    """
    if len(value) > cap:
        return -2, -2
    raw, escaped = 0, 2
    for char in value:
        c = ord(char)
        if 0xD800 <= c <= 0xDFFF:
            return -1, -1
        width = 1 if c < 0x80 else 2 if c < 0x800 else 3 if c < 0x10000 else 4
        raw += width
        escaped += (2 if c in (8, 9, 10, 12, 13, 34, 92) else 6 if c < 32 else width)
    return raw, escaped


def _json_cost(value, cap, depth=0):
    # Only internally built finite documents reach this helper, never caller
    # containers. The explicit depth guard also bounds accidental future changes.
    if depth > 6:
        return -1
    if value is None:
        return 4
    if type(value) is str:
        return _text_measure(value, cap)[1]
    if type(value) is int:
        return len(str(value))
    if type(value) is list:
        cost = 2 + max(0, len(value) - 1)
        children = value
    elif type(value) is dict:
        cost = 2 + max(0, len(value) - 1) + len(value)
        children = []
        for key, child in value.items():
            children.extend((key, child))
    else:
        return -1
    for child in children:
        size = _json_cost(child, cap, depth + 1)
        if size < 0:
            return size
        cost += size
        if cost > cap:
            return -2
    return cost


def _json_bytes(document):
    return json.dumps(document, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=False).encode("utf-8")


def _tag(value):
    if value is None:
        return ["null"]
    if type(value) is str:
        raw, escaped = _text_measure(value, _PROJECTION_LIMIT)
        if raw == -1:
            return NeedsNative(reason="invalid_state")
        if raw == -2 or raw > _PROJECTION_LIMIT or escaped > _PROJECTION_LIMIT:
            return NeedsNative(reason="state_too_large")
        return ["text", value]
    if type(value) is int and _I64_MIN <= value <= _I64_MAX:
        return ["int", str(value)]
    if type(value) is float and math.isfinite(value):
        return ["real", value.hex()]
    return NeedsNative(reason="invalid_state")


def encode_projection(values: StoredValues) -> Projection | NeedsNative:
    """Hash exact stored scalars, without accepting an assessment as authority."""
    if not _record(values, StoredValues):
        return NeedsNative(reason="invalid_state")
    assessment = values.caller_form_assessment
    if not _literal(assessment, ("supported",) + _REASONS):
        return NeedsNative(reason="invalid_state")
    if assessment != "supported":
        return NeedsNative(reason=assessment)
    item, primary = values.item, values.primary
    if (type(item) is not tuple or len(item) != 31 or type(item[0]) is not int
            or not 1 <= item[0] <= _I64_MAX or type(values.wishlisted) is not int
            or values.wishlisted not in (0, 1)):
        return NeedsNative(reason="invalid_state")
    for i in _TEXT_SLOTS:
        if item[i] is not None and type(item[i]) is not str:
            return NeedsNative(reason="invalid_state")
    for i in _INT_SLOTS:
        if item[i] is not None and type(item[i]) is not int:
            return NeedsNative(reason="invalid_state")
    for i in _REAL_SLOTS:
        if item[i] is not None and type(item[i]) is not float:
            return NeedsNative(reason="invalid_state")
    if primary is not None:
        if type(primary) is not tuple or len(primary) != 8:
            return NeedsNative(reason="invalid_state")
        if (any(type(primary[i]) is not int or not 1 <= primary[i] <= _I64_MAX for i in (0, 1, 2))
                or primary[1] != item[0] or type(primary[4]) is not int or primary[4] != 1
                or any(primary[i] is not None and type(primary[i]) is not int for i in (3, 5))):
            return NeedsNative(reason="invalid_state")
    item_tags, primary_tags, text_bytes = [], None, 0
    for i, atom in enumerate(item):
        tag = _tag(atom)
        if type(tag) is NeedsNative:
            return tag
        if i in _TEXT_SLOTS and atom is not None:
            raw = _text_measure(atom, 65536)[0]
            if raw < 0 or raw > 65536:
                return NeedsNative(reason="state_too_large")
            text_bytes += raw
        item_tags.append(tag)
    if text_bytes > 131072:
        return NeedsNative(reason="state_too_large")
    if primary is not None:
        primary_tags = []
        for atom in primary:
            tag = _tag(atom)
            if type(tag) is NeedsNative:
                return tag
            primary_tags.append(tag)
    cover = None
    absent_cover = item[26] is None or (type(item[26]) is str and item[26] == "")
    if absent_cover:
        if values.cover is not None:
            return NeedsNative(reason="invalid_state")
    elif (not _record(values.cover, CoverFingerprint) or not _hex(values.cover.sha256, 64)
          or type(values.cover.byte_length) is not int or not 0 <= values.cover.byte_length <= 10485760):
        return NeedsNative(reason="invalid_state")
    else:
        cover = ["sha256", values.cover.sha256, str(values.cover.byte_length)]
    document = ["shelf.me2.state", 2, item_tags, ["int", str(values.wishlisted)], primary_tags, cover]
    cost = _json_cost(document, _PROJECTION_LIMIT)
    if cost == -1:
        return NeedsNative(reason="invalid_state")
    if cost == -2 or cost > _PROJECTION_LIMIT:
        return NeedsNative(reason="state_too_large")
    canonical = _json_bytes(document)
    return Projection(canonical_json=canonical, baseline="sha256:" + hashlib.sha256(canonical).hexdigest())


def _echo(value):
    return (_record(value, Echo)
            and (value.operation is None or _literal(value.operation, _OPS))
            and (value.generation is None or _hex(value.generation, 32))
            and (value.request is None or _hex(value.request, 32))
            and (value.item_id is None or _id(value.item_id))
            and (value.actor is None or _actor(value.actor)))


def _fields(value):
    return (_record(value, WireFields) and _literal(value.operation, _OPS)
            and _hex(value.generation, 32) and _hex(value.request, 32) and _id(value.item_id)
            and _actor(value.actor) and _root(value.root_path) and _from(value.from_raw)
            and (value.state is None if value.operation == "form" else _baseline(value.state)))


def _reply_wire(wire):
    if _record(wire, WireInputError):
        if _literal(wire.code, ("input_type", "input_bound")):
            return Echo(operation=None, generation=None, request=None, item_id=None, actor=None), None
        return None
    if not _record(wire, CandidateWire) or not _echo(wire.echo):
        return None
    if wire.fields is None:
        return (wire.echo, None) if _literal(wire.problem, _PROBLEMS) else None
    if wire.problem is not None or not _fields(wire.fields):
        return None
    for name in ("operation", "generation", "request", "item_id"):
        echoed = getattr(wire.echo, name)
        if echoed is not None and echoed != getattr(wire.fields, name):
            return None
    actor = wire.echo.actor
    if actor is not None and (actor.id != wire.fields.actor.id
                              or actor.token_version != wire.fields.actor.token_version):
        return None
    return wire.echo, wire.fields


def _reply_parts(data):
    if _record(data, FormData):
        if _baseline(data.baseline) and type(data.html) is str:
            return 200, "form", "not_applicable", {"baseline": data.baseline, "html": data.html}, "form"
    elif _record(data, ProbeData):
        if _literal(data.comparison, ("same", "different")):
            return 200, "probe", "not_applicable", {"comparison": data.comparison}, "probe"
    elif _record(data, SavedData):
        if _baseline(data.baseline) and _literal(data.cover, ("none", "applied", "ignored")):
            return 200, "saved", "saved", {"baseline": data.baseline, "cover": data.cover}, "apply"
    elif _record(data, NativeRequiredData):
        if _literal(data.reason, _REASONS):
            return 409, "native_required", "not_saved", {"reason": data.reason, "native_href": None}, "valid"
    elif _record(data, FailureData):
        if (_literal(data.code, ("internal", "busy"))
                and _literal(data.reported_write_knowledge, ("unknown", "pre_write"))):
            knowledge = "unknown" if data.reported_write_knowledge == "unknown" else "not_saved"
            return (500 if data.code == "internal" else 503), "refused", knowledge, {
                "code": data.code, "field": None, "blocking_item_id": None,
            }, "any"
    elif _record(data, RefusedData):
        if not _literal(data.code, _STATUSES):
            return None
        if data.field is not None and not _literal(data.field, _REFUSAL_FIELDS.get(data.code, ())):
            return None
        if data.blocking_item_id is not None and (
                not _id(data.blocking_item_id) or data.code not in ("upc_conflict", "identifier_in_trash")):
            return None
        allowed = "any" if data.code in ("invalid_protocol", "auth_required", "forbidden", "rate_limited") else "valid"
        return _STATUSES[data.code], "refused", "not_saved", {
            "code": data.code, "field": data.field, "blocking_item_id": data.blocking_item_id,
        }, allowed
    return None


def make_reply(wire: CandidateWire | WireInputError, outcome: ReplyData) -> ReplySpec | ReplyError:
    """Serialize finite caller reports; never repair unknown write knowledge."""
    checked = _reply_wire(wire)
    parts = _reply_parts(outcome)
    if checked is None or parts is None:
        return ReplyError(code="invalid_input")
    echo, fields = checked
    status, result, knowledge, payload, allowed = parts
    if (allowed != "any" and (fields is None
            or (allowed != "valid" and fields.operation != allowed))):
        return ReplyError(code="invalid_combination")
    if result == "native_required":
        href = fields.root_path + "/item/" + fields.item_id + "/edit"
        if fields.from_raw is not None:
            href += "?from=" + fields.from_raw.decode("ascii")
        payload["native_href"] = href
    actor = None if echo.actor is None else {"id": echo.actor.id, "token_version": echo.actor.token_version}
    document = {
        "protocol": "shelf.me2", "version": 2, "operation": echo.operation,
        "generation": echo.generation, "request": echo.request, "item_id": echo.item_id,
        "actor": actor, "result": result, "outcome": knowledge, "payload": payload,
    }
    cost = _json_cost(document, _REPLY_LIMIT)
    if cost == -1:
        return ReplyError(code="invalid_text")
    if cost == -2 or cost > _REPLY_LIMIT:
        return ReplyError(code="reply_too_large")
    return ReplySpec(status=status, body=_json_bytes(document))
