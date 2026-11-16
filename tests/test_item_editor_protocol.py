"""Independent, dormant ME2 contract tests; no route, client or provider fixture.

Normative inputs (read before writing this file, not the implementation):
  .temp/implementation-wave-20261114/ME2_PURE_ADAPTER_CONTRACT.md
  .temp/implementation-wave-20261114/ME2_PROTOCOL_DECISION.md
  .temp/feature-completion-20261116/ME2_C1_C2_AMENDMENT.md
The amendment was independently APPROVE_PURE_CONTRACT/ratified. Its 35 outcomes
cross all five wire states below (175 cells), before payload variations.

Projection goldens are literal bytes and literal SHA-256 values prepared with
stdlib JSON/hashlib from the specified positional vectors, independently of the
adapter. No expected state is obtained from an adapter helper or record fields.
Only API-shape tests introspect records. The ordinary global isolation fixture is
unchanged; these tests request no DB/client/account/network/provider fixture.
Import is deliberately local: a source-absent lane collects and fails test calls.
"""

import ast
import dataclasses
import functools
import hashlib
import importlib
import inspect
import json
import math
from pathlib import Path
import typing

import pytest


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "app/services/item_editor_protocol.py"
GENERATION = "0123456789abcdef0123456789abcdef"
REQUEST = "fedcba9876543210fedcba9876543210"
BASELINE = "sha256:" + "a" * 64
MAX_ID = "9223372036854775807"
NATIVE_REASONS = ("invalid_state", "state_too_large", "context_too_large")
WIRE_PROBLEMS = (
    "pairing", "multiplicity", "metadata", "query", "path",
    "method_operation", "representation",
)
ITEM_NAMES = (
    "id", "title", "subtitle", "authors", "isbn", "upc", "media_type",
    "publisher", "publish_year", "page_count", "description", "series_name",
    "series_position", "narrator", "duration_mins", "location_id", "notes",
    "reading_status", "date_started", "date_finished", "owned", "platform",
    "manual_value", "language", "asin", "isbn10", "cover_path",
    "cover_edition_isbn", "cover_edition_language", "created_at", "updated_at",
)
PRIMARY_NAMES = (
    "id", "item_id", "copy_number", "location_id", "is_primary",
    "position_order", "created_at", "updated_at",
)
ITEM = (
    7, "Title é", "sub", "A & B", "9780441172710", "1234567890120", "book",
    "Pub", 2026, 0, 'D\n"\\', "S", -0.0, "N", 17, 23, "Notes", "read",
    "2026-01-01", "2026-01-02", 0, "ps5", 1.5, "tlh", "B0D1234567",
    "old-isbn10", "", "ed", "eo", "created", "updated",
)
PRIMARY = (11, 7, 2, None, 1, -2, "pc", "pu")
TEXT_SLOTS = (1, 2, 3, 4, 5, 6, 7, 10, 11, 13, 16, 17, 18, 19, 21, 23, 24)
INTEGER_SLOTS = (8, 9, 14, 15, 20)
REAL_SLOTS = (12, 22)
EDITABLE_FIELDS = (
    "title", "subtitle", "authors", "isbn", "upc", "media_type", "publisher",
    "publish_year", "page_count", "description", "series_name", "series_position",
    "narrator", "duration_mins", "location_id", "notes", "reading_status",
    "date_started", "date_finished", "owned", "wishlisted", "platform",
    "manual_value", "language", "asin", "cover", "_csrf", "from",
)
RECORD_FIELDS = {
    "ActorTag": ("id", "token_version"),
    "WireScope": ("asgi_type", "method", "path", "raw_path", "root_path", "headers", "query_string"),
    "Echo": ("operation", "generation", "request", "item_id", "actor"),
    "WireFields": ("operation", "generation", "request", "item_id", "actor", "state", "from_raw", "root_path"),
    "NativeWire": (),
    "CandidateWire": ("echo", "fields", "problem"),
    "WireInputError": ("code",),
    "CoverFingerprint": ("sha256", "byte_length"),
    "StoredValues": ("item", "wishlisted", "primary", "cover", "caller_form_assessment"),
    "Projection": ("canonical_json", "baseline"),
    "NeedsNative": ("reason",),
    "FormData": ("baseline", "html"),
    "ProbeData": ("comparison",),
    "SavedData": ("baseline", "cover"),
    "RefusedData": ("code", "field", "blocking_item_id"),
    "NativeRequiredData": ("reason",),
    "FailureData": ("code", "reported_write_knowledge"),
    "ReplySpec": ("status", "body"),
    "ReplyError": ("code",),
}

# Each row pins (HTTP status, result, outcome, F/P/A/M/E permission vector).
# The vectors are literal, not derived from the implementation or code families.
OUTCOME_MATRIX = (
    ("form", 200, "form", "not_applicable", (True, False, False, False, False)),
    ("probe", 200, "probe", "not_applicable", (False, True, False, False, False)),
    ("saved", 200, "saved", "saved", (False, False, True, False, False)),
    ("native:invalid_state", 409, "native_required", "not_saved", (True, True, True, False, False)),
    ("native:state_too_large", 409, "native_required", "not_saved", (True, True, True, False, False)),
    ("native:context_too_large", 409, "native_required", "not_saved", (True, True, True, False, False)),
    ("invalid_protocol", 400, "refused", "not_saved", (True, True, True, True, True)),
    ("invalid_form", 400, "refused", "not_saved", (True, True, True, False, False)),
    ("auth_required", 401, "refused", "not_saved", (True, True, True, True, True)),
    ("forbidden", 403, "refused", "not_saved", (True, True, True, True, True)),
    ("csrf_required", 403, "refused", "not_saved", (True, True, True, False, False)),
    ("csrf_failed", 403, "refused", "not_saved", (True, True, True, False, False)),
    ("unavailable", 404, "refused", "not_saved", (True, True, True, False, False)),
    ("actor_changed", 409, "refused", "not_saved", (True, True, True, False, False)),
    ("stale", 409, "refused", "not_saved", (True, True, True, False, False)),
    ("body_too_large", 413, "refused", "not_saved", (True, True, True, False, False)),
    ("fields_too_large", 413, "refused", "not_saved", (True, True, True, False, False)),
    ("unsupported_media", 415, "refused", "not_saved", (True, True, True, False, False)),
    ("rate_limited", 429, "refused", "not_saved", (True, True, True, True, True)),
    ("invalid_isbn", 422, "refused", "not_saved", (True, True, True, False, False)),
    ("invalid_upc", 422, "refused", "not_saved", (True, True, True, False, False)),
    ("invalid_asin", 422, "refused", "not_saved", (True, True, True, False, False)),
    ("invalid_number", 422, "refused", "not_saved", (True, True, True, False, False)),
    ("invalid_owned", 422, "refused", "not_saved", (True, True, True, False, False)),
    ("invalid_wishlisted", 422, "refused", "not_saved", (True, True, True, False, False)),
    ("invalid_reading_status", 422, "refused", "not_saved", (True, True, True, False, False)),
    ("unknown_media_type", 422, "refused", "not_saved", (True, True, True, False, False)),
    ("unknown_location", 422, "refused", "not_saved", (True, True, True, False, False)),
    ("unknown_platform", 422, "refused", "not_saved", (True, True, True, False, False)),
    ("upc_conflict", 422, "refused", "not_saved", (True, True, True, False, False)),
    ("identifier_in_trash", 422, "refused", "not_saved", (True, True, True, False, False)),
    ("failure:internal:unknown", 500, "refused", "unknown", (True, True, True, True, True)),
    ("failure:busy:unknown", 503, "refused", "unknown", (True, True, True, True, True)),
    ("failure:internal:pre_write", 500, "refused", "not_saved", (True, True, True, True, True)),
    ("failure:busy:pre_write", 503, "refused", "not_saved", (True, True, True, True, True)),
)
DOMAIN_FIELDS = (
    ("invalid_isbn", ("isbn",)),
    ("invalid_upc", ("upc",)),
    ("invalid_asin", ("asin",)),
    ("invalid_number", ("publish_year", "page_count", "duration_mins", "location_id", "series_position", "manual_value")),
    ("invalid_owned", ("owned",)),
    ("invalid_wishlisted", ("wishlisted",)),
    ("invalid_reading_status", ("reading_status",)),
    ("unknown_media_type", ("media_type",)),
    ("unknown_location", ("location_id",)),
    ("unknown_platform", ("platform",)),
    ("upc_conflict", ("upc",)),
    ("identifier_in_trash", ("isbn", "upc")),
)

ENVELOPE_HEAD = (
    b'{"protocol":"shelf.me2","version":2,"operation":"form",'
    b'"generation":"0123456789abcdef0123456789abcdef",'
    b'"request":"fedcba9876543210fedcba9876543210","item_id":"7",'
    b'"actor":{"id":"5","token_version":"1"},'
)
FORM_PREFIX = (
    ENVELOPE_HEAD + b'"result":"form","outcome":"not_applicable","payload":'
    b'{"baseline":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","html":"'
)
FORM_SUFFIX = b'"}}'
NULL_HEAD = (
    b'{"protocol":"shelf.me2","version":2,"operation":null,"generation":null,'
    b'"request":null,"item_id":null,"actor":null,'
)


class _ObservedCall:
    """Assert outside the adapter, including when it swallows bomb exceptions."""

    def __init__(self, function, api_name):
        functools.update_wrapper(self, function)
        self.__globals__ = getattr(function, "__globals__", {})
        self._function = function
        self._api_name = api_name

    def __call__(self, *args, **kwargs):
        before = len(_BOMB_CALLS)
        try:
            return self._function(*args, **kwargs)
        finally:
            # Entries contain only trusted class/method names, never the input
            # object or its repr. A returned rejection cannot erase this ledger.
            calls = tuple(_BOMB_CALLS[before:])
            assert not calls, f"Forbidden bomb calls in {self._api_name}: {calls!r}"


class _ObservedProtocol:
    """Forward records unchanged; observe all three API calls without patching SUT."""

    def __init__(self, module):
        self._module = module
        self.decode_wire = _ObservedCall(module.decode_wire, "decode_wire")
        self.encode_projection = _ObservedCall(module.encode_projection, "encode_projection")
        self.make_reply = _ObservedCall(module.make_reply, "make_reply")

    def __getattr__(self, name):
        return getattr(self._module, name)


def _protocol():
    try:
        module = importlib.import_module("app.services.item_editor_protocol")
    except ModuleNotFoundError as error:
        if error.name == "app.services.item_editor_protocol":
            pytest.fail("Dormant ME2 implementation absent: app/services/item_editor_protocol.py", pytrace=False)
        raise
    return _ObservedProtocol(module)


def _headers(op="form"):
    pairs = (
        (b"x-shelf-me2", b"2"), (b"x-shelf-me2-op", op.encode("ascii")),
        (b"x-shelf-me2-generation", GENERATION.encode("ascii")),
        (b"x-shelf-me2-request", REQUEST.encode("ascii")),
        (b"x-shelf-me2-actor", b"5:1"), (b"accept", b"application/json"),
    )
    return pairs if op == "form" else pairs + ((b"x-shelf-me2-state", BASELINE.encode("ascii")),)


def _scope(p, op="form", **changes):
    path = "/api/items/7" if op == "apply" else "/item/7/edit"
    values = dict(asgi_type="http", method="POST" if op == "apply" else "GET",
                  path=path, raw_path=path.encode("ascii"), root_path="",
                  headers=_headers(op), query_string=b"__me2=2")
    values.update(changes)
    return p.WireScope(**values)


def _wire(p, op="form", *, item_id="7", actor_id="5", token_version="1",
          from_raw=None, root_path=""):
    fields = p.WireFields(operation=op, generation=GENERATION, request=REQUEST,
                          item_id=item_id, actor=p.ActorTag(id=actor_id, token_version=token_version),
                          state=None if op == "form" else BASELINE,
                          from_raw=from_raw, root_path=root_path)
    echo = p.Echo(operation=op, generation=GENERATION, request=REQUEST,
                  item_id=item_id, actor=p.ActorTag(id=actor_id, token_version=token_version))
    return p.CandidateWire(echo=echo, fields=fields, problem=None)


def _wire_state(p, state):
    if state in ("form", "probe", "apply"):
        return _wire(p, state)
    if state == "malformed":
        return p.CandidateWire(echo=p.Echo(operation=None, generation=None, request=None,
                                          item_id=None, actor=None), fields=None, problem="query")
    return p.WireInputError(code="input_bound")


def _outcome(p, name):
    if name == "form":
        return p.FormData(baseline=BASELINE, html="<form>é</form>")
    if name == "probe":
        return p.ProbeData(comparison="same")
    if name == "saved":
        return p.SavedData(baseline=BASELINE, cover="none")
    if name.startswith("native:"):
        return p.NativeRequiredData(reason=name.split(":")[1])
    if name.startswith("failure:"):
        _, code, knowledge = name.split(":")
        return p.FailureData(code=code, reported_write_knowledge=knowledge)
    return p.RefusedData(code=name, field=None, blocking_item_id=None)


def _stored(p, **changes):
    values = dict(item=ITEM, wishlisted=0, primary=PRIMARY, cover=None,
                  caller_form_assessment="supported")
    values.update(changes)
    return p.StoredValues(**values)


def _slot(vector, index, value):
    return vector[:index] + (value,) + vector[index + 1:]


def _sample_kwargs(p):
    wire = _wire(p)
    return {
        "ActorTag": dict(id="5", token_version="1"),
        "WireScope": dict(asgi_type="http", method="GET", path="/item/7/edit", raw_path=b"/item/7/edit",
                          root_path="", headers=_headers(), query_string=b"__me2=2"),
        "Echo": dict(operation="form", generation=GENERATION, request=REQUEST, item_id="7", actor=wire.echo.actor),
        "WireFields": dict(operation="form", generation=GENERATION, request=REQUEST, item_id="7",
                           actor=wire.fields.actor, state=None, from_raw=None, root_path=""),
        "NativeWire": {},
        "CandidateWire": dict(echo=wire.echo, fields=wire.fields, problem=None),
        "WireInputError": dict(code="input_type"),
        "CoverFingerprint": dict(sha256="a" * 64, byte_length=101),
        "StoredValues": dict(item=ITEM, wishlisted=0, primary=PRIMARY, cover=None, caller_form_assessment="supported"),
        "Projection": dict(canonical_json=b"[]", baseline=BASELINE),
        "NeedsNative": dict(reason="invalid_state"),
        "FormData": dict(baseline=BASELINE, html="<form>é</form>"),
        "ProbeData": dict(comparison="same"),
        "SavedData": dict(baseline=BASELINE, cover="none"),
        "RefusedData": dict(code="invalid_protocol", field=None, blocking_item_id=None),
        "NativeRequiredData": dict(reason="invalid_state"),
        "FailureData": dict(code="internal", reported_write_knowledge="unknown"),
        "ReplySpec": dict(status=200, body=b"{}"),
        "ReplyError": dict(code="invalid_input"),
    }


_BOMB_CALLS = []
_BOMB_METHODS = (
    "__str__", "__repr__", "__format__", "__bytes__",
    "__iter__", "__next__", "__len__", "__length_hint__", "__reversed__",
    "__getitem__", "__contains__", "__getattr__", "__bool__",
    "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__", "__hash__",
    "__int__", "__float__", "__index__", "__complex__", "__round__",
    "__trunc__", "__floor__", "__ceil__", "__fspath__",
    "encode", "decode", "lower", "split", "strip", "hex", "_explode",
)


def _bomb_method(name):
    def explode(self, *args, **kwargs):
        _BOMB_CALLS.append((type(self).__name__, name))
        raise AssertionError("Untrusted object's method was invoked")
    return explode


class _Bomb:
    """All hooks record first, then raise; record-subclass callbacks use _explode."""


class _Text(str):
    pass


class _Bytes(bytes):
    pass


class _Integer(int):
    pass


class _Real(float):
    pass


class _Tuple(tuple):
    pass


for _bomb_type in (_Bomb, _Text, _Bytes, _Integer, _Real, _Tuple):
    for _method_name in _BOMB_METHODS:
        setattr(_bomb_type, _method_name, _bomb_method(_method_name))


def _bad(kind):
    return {
        "bomb": lambda: _Bomb(), "text_subclass": lambda: _Text("7"),
        "bytes_subclass": lambda: _Bytes(b"x"), "int_subclass": lambda: _Integer(1),
        "float_subclass": lambda: _Real(1.0), "tuple_subclass": lambda: _Tuple((1,)),
        "bool": lambda: True, "list": lambda: [], "map": lambda: {},
        "blob": lambda: b"x", "bytearray": lambda: bytearray(b"x"),
        "memoryview": lambda: memoryview(b"x"),
        "generator": lambda: (x for x in (_Bomb(),)),
    }[kind]()


def _assert_error(p, value, code="invalid_input"):
    assert type(value) is p.ReplyError
    assert value.code == code


def _assert_native(p, value, reason="invalid_state"):
    assert type(value) is p.NeedsNative
    assert value.reason == reason


def _assert_malformed(p, value):
    assert type(value) is p.CandidateWire
    assert value.fields is None
    assert type(value.problem) is str and value.problem in WIRE_PROBLEMS
    assert type(value.echo) is p.Echo
    response = p.make_reply(value, p.RefusedData(code="invalid_protocol", field=None, blocking_item_id=None))
    assert type(response) is p.ReplySpec and response.status == 400
    _assert_error(p, p.make_reply(value, p.NativeRequiredData(reason="invalid_state")), "invalid_combination")
    assert b"native_href" not in response.body


def _assert_projection(p, values, expected, digest):
    result = p.encode_projection(values)
    assert type(result) is p.Projection
    assert type(result.canonical_json) is bytes and result.canonical_json == expected
    assert type(result.baseline) is str and result.baseline == digest


def _no_serialization(monkeypatch, p):
    """Instrument both import styles; expectations never use adapter serializers."""
    originals = (json.dumps, json.dump, hashlib.sha256)
    for module, name in ((json, "dumps"), (json, "dump"), (hashlib, "sha256")):
        monkeypatch.setattr(module, name, _Bomb()._explode)
    target = p._module if type(p) is _ObservedProtocol else p
    for name, value in tuple(vars(target).items()):
        if any(value is original for original in originals):
            monkeypatch.setattr(target, name, _Bomb()._explode)
    monkeypatch.setattr(json.JSONEncoder, "encode", _Bomb()._explode)
    monkeypatch.setattr(json.JSONEncoder, "iterencode", _Bomb()._explode)


# Literal tagged byte vectors keep 61 complete golden documents readable without
# duplicating the other 30/7 unchanged atoms on every row. Assembly below only
# joins these literals; it does NOT tag values, call a serializer, inspect records,
# hash expected bytes at assertion time, or call any adapter code.
ITEM_ATOMS = (
    b'["int","7"]', b'["text","Title \xc3\xa9"]', b'["text","sub"]', b'["text","A & B"]',
    b'["text","9780441172710"]', b'["text","1234567890120"]', b'["text","book"]', b'["text","Pub"]',
    b'["int","2026"]', b'["int","0"]', b'["text","D\\n\\"\\\\"]', b'["text","S"]',
    b'["real","-0x0.0p+0"]', b'["text","N"]', b'["int","17"]', b'["int","23"]',
    b'["text","Notes"]', b'["text","read"]', b'["text","2026-01-01"]', b'["text","2026-01-02"]',
    b'["int","0"]', b'["text","ps5"]', b'["real","0x1.8000000000000p+0"]', b'["text","tlh"]',
    b'["text","B0D1234567"]', b'["text","old-isbn10"]', b'["text",""]', b'["text","ed"]',
    b'["text","eo"]', b'["text","created"]', b'["text","updated"]',
)
PRIMARY_ATOMS = (
    b'["int","11"]', b'["int","7"]', b'["int","2"]', b'["null"]',
    b'["int","1"]', b'["int","-2"]', b'["text","pc"]', b'["text","pu"]',
)
PROJECTION_GOLDENS = (
    ('base', 'base', 0, None, None, 'sha256:b9ffb99ea1177ac3b35d50e0af7bc2c7557cfee366765d9bce42c1cf74c37b04'),
    ('item-0', 'item', 0, 8, b'["int","8"]', 'sha256:5ac6d3e4cc24895689182a8a1eee4ec56eaa448cb7bd8558c3a33ce7b09b47fb'),
    ('item-1', 'item', 1, 't2', b'["text","t2"]', 'sha256:6be0dffacb145035d0f9767b6d6f721bedb0baa8275cb8a012c3901cbf8c6ddf'),
    ('item-2', 'item', 2, 's2', b'["text","s2"]', 'sha256:4bca2955fe2ba920b8aed8fb911635eb0f4810b92b4f55687a15ce5bada92b99'),
    ('item-3', 'item', 3, 'a2', b'["text","a2"]', 'sha256:3b4550659ac28245eea9bda3b26dbb68521a01bd3c689cfd55ca5db9f8427b0f'),
    ('item-4', 'item', 4, 'new-isbn', b'["text","new-isbn"]', 'sha256:9baa2c8216fd60a1555b0ec6ca227ebfa0a84e120143d94b6b0171b62e7a11d0'),
    ('item-5', 'item', 5, 'new-upc', b'["text","new-upc"]', 'sha256:66d463a1ef0474292fcde6e1315f1351f12ba462fe1d7e3c1d48bbefe2c6a688'),
    ('item-6', 'item', 6, 'audiobook', b'["text","audiobook"]', 'sha256:f6657f2c4722f6c0bbb3820535e455fd56f41ee0a0a52f77a3acde1908558ede'),
    ('item-7', 'item', 7, 'p2', b'["text","p2"]', 'sha256:522780d3cf342fffc6862e8d6c1f68fd1be148822def0aa22e6c821a1a7367fd'),
    ('item-8', 'item', 8, 2027, b'["int","2027"]', 'sha256:388dc8628e826987acc03f8327f7abafac3523fce72f07f87f7b5fbdb8dc497e'),
    ('item-9', 'item', 9, 1, b'["int","1"]', 'sha256:00b9168bda39410a809d187deec4ff2ac1fa15f8fe3b0f4ecc0f2485386b34f6'),
    ('item-10', 'item', 10, 'd2', b'["text","d2"]', 'sha256:de951f183d6f55179e0440782a10e367cccdc81caab88a6d4bb93387b8810074'),
    ('item-11', 'item', 11, 's2', b'["text","s2"]', 'sha256:554e99b1309883de55a6078545f7ce7e5227309d848867f1b8d958b35848410f'),
    ('item-12', 'item', 12, 0.0, b'["real","0x0.0p+0"]', 'sha256:d25a1bee5ce7446aca963e1f50825aded28b3bc2f24ff15d944a28209fca1793'),
    ('item-13', 'item', 13, 'n2', b'["text","n2"]', 'sha256:ede2f65deaeb7dbb435333cd0ed4082690e74dbdaa8a89e4b9bc9fd2d161963a'),
    ('item-14', 'item', 14, 18, b'["int","18"]', 'sha256:82b6b0eba7d5d3bc20a0e119ee3af6eab3a8dd47dca562f06df828d4afd1e16c'),
    ('item-15', 'item', 15, 24, b'["int","24"]', 'sha256:4d3a771f94f9fff199bc9efc07da40be97d9f76c5d2bac49a4354d7fe590d787'),
    ('item-16', 'item', 16, 'n2', b'["text","n2"]', 'sha256:c3c054c3e71e0368811975bf99e74fec293e59840aed8f6302e39c7704437adf'),
    ('item-17', 'item', 17, 'reading', b'["text","reading"]', 'sha256:011bfd5db7989aa95e83c7215a25058c3d093aa05c500136330fc37ce30ac7f0'),
    ('item-18', 'item', 18, '2026-02-01', b'["text","2026-02-01"]', 'sha256:5763c639218cdb352bace8881364e7dd678bb52fe09747117dc9996897e6935e'),
    ('item-19', 'item', 19, '2026-02-02', b'["text","2026-02-02"]', 'sha256:a51f0f4eb11de8e04a81da28414fe22de4174971ea58e9274d98901ad31bc65d'),
    ('item-20', 'item', 20, 1, b'["int","1"]', 'sha256:930719c7c5c379e05c324b5d2343cdc938bc6a58e695a49c7b41ebb8c35a7a1a'),
    ('item-21', 'item', 21, 'switch', b'["text","switch"]', 'sha256:2e1866fa735141e4c1b6e7b160b82db47e615fe3e73814262a236d8a057594f9'),
    ('item-22', 'item', 22, 2.5, b'["real","0x1.4000000000000p+1"]', 'sha256:b675cd9e88f2da1db114e3b2ce7e2b3ef556e6d664e17d80a35b70a86c7e4525'),
    ('item-23', 'item', 23, 'xx-unknown', b'["text","xx-unknown"]', 'sha256:b9941e182ab8110ad46eb969b2c793f464bad8c2355e3e28e3664f0f469aa216'),
    ('item-24', 'item', 24, 'B0D7654321', b'["text","B0D7654321"]', 'sha256:06e2d1e2728fa5a7f02d6f7e00883b53b43b3823dae91705424174db35e64c80'),
    ('item-25', 'item', 25, 'isbn10-2', b'["text","isbn10-2"]', 'sha256:a83fae8d62d4eb02c21bc62c3ef44d8d33672de59a9403168c667aa89d15367a'),
    ('item-26', 'item', 26, 'covers/7.jpg', b'["text","covers/7.jpg"]', 'sha256:80c448427e0cf898b77435228a431d53ed8599a3de9902f7225df4907e0e5dc7'),
    ('item-27', 'item', 27, 'ed2', b'["text","ed2"]', 'sha256:35945441e74bb3d21737a909e55b612eefd4492cf6334713b8288dda81b1f96f'),
    ('item-28', 'item', 28, 'eo2', b'["text","eo2"]', 'sha256:0747b609c28cd466712d59b05fd075ba3cc0af6154fc59d0914d628b8be2af54'),
    ('item-29', 'item', 29, 'c2', b'["text","c2"]', 'sha256:ac12c904338a3c6d7b4109790537ebb517724f59aadebd48d2bd725d1c61e748'),
    ('item-30', 'item', 30, 'u2', b'["text","u2"]', 'sha256:ce1666b5b88f037ea8af0da14d456d5341dc829ef143941cc9124b35d38f39ce'),
    ('wishlist', 'wishlist', 0, 1, b'["int","1"]', 'sha256:2b53cc5b6abf7659e66b6d9f3c8b06becd78f589f0be4d3b0aff3a32473948da'),
    ('primary-0', 'primary', 0, 12, b'["int","12"]', 'sha256:dad78acacbbeb44faa47983c3245941f4b21605d229cac2bb56e4e210a5a512d'),
    ('primary-1', 'primary', 1, 8, b'["int","8"]', 'sha256:5ac6d3e4cc24895689182a8a1eee4ec56eaa448cb7bd8558c3a33ce7b09b47fb'),
    ('primary-2', 'primary', 2, 3, b'["int","3"]', 'sha256:4bb381e988891c378072301b367d6f0a2a734aab2a37530d7e4c1296072e3e99'),
    ('primary-3', 'primary', 3, 0, b'["int","0"]', 'sha256:510047ef675e542d7577fdfc6503024cdc58433b38f0922bb1593f441939cde2'),
    ('primary-4', 'primary', 4, 1, b'["int","1"]', 'sha256:b9ffb99ea1177ac3b35d50e0af7bc2c7557cfee366765d9bce42c1cf74c37b04'),
    ('primary-5', 'primary', 5, -3, b'["int","-3"]', 'sha256:7eff404d5ebf65f6176bde735853c2f85451e7d5e510fd1156bda9d77dde9b38'),
    ('primary-6', 'primary', 6, 'pc2', b'["text","pc2"]', 'sha256:410966b0fbf37c00befb53bc548f61dba35f99a8e53744eb873eda6200e71233'),
    ('primary-7', 'primary', 7, 'pu2', b'["text","pu2"]', 'sha256:29acdf0762f17018109e2a2098e198a6238955b81b8297efe911f1bc062c2aca'),
    ('no-primary', 'no_primary', 0, None, None, 'sha256:fc04b1ccd9cdf2891db536927cef316193d0d70bf5868413dd107b186a61782e'),
    ('cover-digest', 'cover_digest', 0, 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', b'["sha256","bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","101"]', 'sha256:fa026dcff40c5a00fc7ad467cb2ef11520718fc919a39258e950790f62107e13'),
    ('cover-length', 'cover_length', 0, 102, b'["sha256","aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","102"]', 'sha256:e423bd1b4a279e90c011e35dc4381792ea3fca2dcf05e00a406fa095614fab0e'),
    ('int-zero', 'scalar', 29, 0, b'["int","0"]', 'sha256:e07533968b90b6765c3ee18390c6173e33baf179fd69d1f7ed20b0b0e0337f3f'),
    ('real-zero', 'scalar', 29, 0.0, b'["real","0x0.0p+0"]', 'sha256:5f0d6a4aa75829e396f21a07dde6f3c4271b6d8f5e4e78e46d2ae9f472f0db16'),
    ('negative-real-zero', 'scalar', 29, -0.0, b'["real","-0x0.0p+0"]', 'sha256:c98b8607dcad67ad14f38d3471b780ba20831409c367c83f50999e1832fd2dd1'),
    ('null', 'scalar', 29, None, b'["null"]', 'sha256:f56b4735ee69ef0115446c237a46bd9193cff74b757f79061a56d88eee9a72f6'),
    ('empty', 'scalar', 29, '', b'["text",""]', 'sha256:c305585733569071897f39943188392fc83ff066843c9e53df2e3874b9ce7831'),
    ('min-int', 'scalar', 29, -9223372036854775808, b'["int","-9223372036854775808"]', 'sha256:1ebcbfa8bf0a111adba5c4a518632c3326c96e677e16881b10b286617c3d6c11'),
    ('max-int', 'scalar', 29, 9223372036854775807, b'["int","9223372036854775807"]', 'sha256:1957cbebeee95d4e67826edfd9ef68f62daf61c541d2446b46eed988df4c9155'),
    ('max-finite', 'scalar', 29, 1.7976931348623157e+308, b'["real","0x1.fffffffffffffp+1023"]', 'sha256:0f881a0143a950e22205a6e83f13e2586073ce087111088ea45076a291b9c184'),
    ('min-subnormal', 'scalar', 29, 5e-324, b'["real","0x0.0000000000001p-1022"]', 'sha256:ae28e16e564777bcc4cd385740ad794affdcd7994831b80a513bd8373fdb1b4e'),
    ('unicode', 'scalar', 29, 'éé😀', b'["text","e\xcc\x81\xc3\xa9\xf0\x9f\x98\x80"]', 'sha256:f9e01c49c491a8064919ecf15144aa9e732bc95c39c033d955590bae0b7cddb6'),
    ('escaping', 'scalar', 29, '\x00\x08\x0c\n\r\t"\\/', b'["text","\\u0000\\b\\f\\n\\r\\t\\"\\\\/"]', 'sha256:ccb7aa243312465cfb69ab1f04926bd6ca7f42d594ef545c455d78c10c7f93e5'),
    ('primary-int-zero', 'primary', 6, 0, b'["int","0"]', 'sha256:9a286a2499514db85851a2a2df4f088eac89bf4a158f8efcaaa896836a1c32e9'),
    ('primary-real-zero', 'primary', 6, 0.0, b'["real","0x0.0p+0"]', 'sha256:f9e71d368aa52f524197dbc8e03347c4dcf17207d4fbc2d9981f4ba74f0cd63d'),
    ('primary-negative-zero', 'primary', 6, -0.0, b'["real","-0x0.0p+0"]', 'sha256:cf7f168cdc488f2d0797f37b64e52600ebffb6a480da7ae41613eedc9b62d139'),
    ('primary-null', 'primary', 6, None, b'["null"]', 'sha256:f5de6ef9b3267f7ca35164ae7518d56838e6314708576fc1759f0b50765da99a'),
    ('primary-empty', 'primary', 6, '', b'["text",""]', 'sha256:c89caf72428db4636aa9efb95f8d309db0b115d91235db9d823de2f3577eec41'),
    ('valid-unchanged-isbn-no-repair', 'item', 4, '9780441172719', b'["text","9780441172719"]', 'sha256:fbcdf4c5bfc8e0b5d5658419030fa55d39836f737964dbfc1e564bf7ec924992'),
)


def _golden_bytes(target, index, token):
    item, primary = list(ITEM_ATOMS), list(PRIMARY_ATOMS)
    wishlist, cover = b'["int","0"]', b'null'
    if target in ("item", "scalar"):
        item[index] = token
        if target == "item" and index == 0:
            primary[1] = b'["int","8"]'
        if target == "item" and index == 26:
            cover = b'["sha256","aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","101"]'
    elif target == "primary":
        primary[index] = token
        if index == 1:
            item[0] = b'["int","8"]'
    elif target == "wishlist":
        wishlist = token
    elif target in ("cover_digest", "cover_length"):
        item[26] = b'["text","covers/7.jpg"]'
        cover = token
    primary_json = b'null' if target == "no_primary" else b'[' + b','.join(primary) + b']'
    return b'["shelf.me2.state",2,[' + b','.join(item) + b'],' + wishlist + b',' + primary_json + b',' + cover + b']'


def test_expected_fixture_matrix_is_complete_and_literal():
    assert len(OUTCOME_MATRIX) == 35
    assert len({row[0] for row in OUTCOME_MATRIX}) == 35
    assert sum(len(row[4]) for row in OUTCOME_MATRIX) == 175
    assert all(type(cell) is bool for row in OUTCOME_MATRIX for cell in row[4])
    assert {row[0] for row in OUTCOME_MATRIX if row[1] == 422} == {row[0] for row in DOMAIN_FIELDS}
    assert len(ITEM) == len(ITEM_NAMES) == 31
    assert len(PRIMARY) == len(PRIMARY_NAMES) == 8


def test_expected_fixture_literal_goldens_self_check():
    assert len(PROJECTION_GOLDENS) == 61
    assert len(ITEM_ATOMS) == 31 and len(PRIMARY_ATOMS) == 8
    for label, target, index, value, token, digest in PROJECTION_GOLDENS:
        expected = _golden_bytes(target, index, token)
        assert type(expected) is bytes
        document = json.loads(expected)
        assert document[:2] == ["shelf.me2.state", 2], label
        assert len(document[2]) == 31, label
        assert document[4] is None or len(document[4]) == 8, label
        assert digest == "sha256:" + hashlib.sha256(expected).hexdigest(), label
        assert json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") == expected, label
    assert {index for _, target, index, *_ in PROJECTION_GOLDENS if target == "item"} == set(range(31))
    assert {index for _, target, index, *_ in PROJECTION_GOLDENS if target == "primary"} == set(range(8))


def test_expected_fixture_reply_literals_and_byte_costs_self_check():
    expected = json.loads(FORM_PREFIX + FORM_SUFFIX)
    assert expected == {
        "protocol": "shelf.me2", "version": 2, "operation": "form",
        "generation": GENERATION, "request": REQUEST, "item_id": "7",
        "actor": {"id": "5", "token_version": "1"}, "result": "form",
        "outcome": "not_applicable", "payload": {"baseline": BASELINE, "html": ""},
    }
    assert json.dumps(expected, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") == FORM_PREFIX + FORM_SUFFIX
    for raw, encoded in (("é", b"\xc3\xa9"), ("😀", b"\xf0\x9f\x98\x80"), ('"', b'\\"'), ("\\", b"\\\\"), ("\n", b"\\n"), ("\x00", b"\\u0000")):
        assert json.dumps(raw, ensure_ascii=False, allow_nan=False).encode("utf-8") == b'"' + encoded + b'"'


def test_public_api_records_and_exact_parameters():
    p = _protocol()
    for name, expected in (("decode_wire", ("scope",)), ("encode_projection", ("values",)), ("make_reply", ("wire", "outcome"))):
        signature = inspect.signature(getattr(p, name))
        assert tuple(signature.parameters) == expected
        assert all(parameter.default is inspect.Parameter.empty and parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                   for parameter in signature.parameters.values())
    samples = _sample_kwargs(p)
    for name, names in RECORD_FIELDS.items():
        cls = getattr(p, name)
        assert dataclasses.is_dataclass(cls)
        assert tuple(field.name for field in dataclasses.fields(cls)) == names
        assert tuple(cls.__slots__) == names
        params = cls.__dataclass_params__
        assert params.frozen and not params.eq and not params.repr
        for field in dataclasses.fields(cls):
            assert field.kw_only and field.default is dataclasses.MISSING
            assert field.default_factory is dataclasses.MISSING
        record = cls(**samples[name])
        assert not hasattr(record, "__dict__")
        if names:
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(record, names[0], None)
            with pytest.raises(TypeError):
                cls(*samples[name].values())


def test_public_annotations_pin_records_unions_and_literals():
    p = _protocol()
    literal = typing.Literal
    op = literal["form", "probe", "apply"]
    native = literal["invalid_state", "state_too_large", "context_too_large"]
    problem = literal["pairing", "multiplicity", "metadata", "query", "path", "method_operation", "representation"]
    refusal = literal[tuple(row[0] for row in OUTCOME_MATRIX if row[2] == "refused" and not row[0].startswith("failure:"))]
    field = literal[EDITABLE_FIELDS]
    atom = str | int | float | None
    expected = {
        "ActorTag": dict(id=str, token_version=str),
        "WireScope": dict(asgi_type=str, method=str, path=str, raw_path=bytes | None, root_path=str,
                          headers=tuple[tuple[bytes, bytes], ...], query_string=bytes),
        "Echo": dict(operation=op | None, generation=str | None, request=str | None, item_id=str | None, actor=p.ActorTag | None),
        "WireFields": dict(operation=op, generation=str, request=str, item_id=str, actor=p.ActorTag,
                           state=str | None, from_raw=bytes | None, root_path=str),
        "NativeWire": {},
        "CandidateWire": dict(echo=p.Echo, fields=p.WireFields | None, problem=problem | None),
        "WireInputError": dict(code=literal["input_type", "input_bound"]),
        "CoverFingerprint": dict(sha256=str, byte_length=int),
        "StoredValues": dict(item=tuple[atom, ...], wishlisted=int, primary=tuple[atom, ...] | None,
                             cover=p.CoverFingerprint | None, caller_form_assessment=literal["supported", "invalid_state", "state_too_large", "context_too_large"]),
        "Projection": dict(canonical_json=bytes, baseline=str),
        "NeedsNative": dict(reason=native),
        "FormData": dict(baseline=str, html=str),
        "ProbeData": dict(comparison=literal["same", "different"]),
        "SavedData": dict(baseline=str, cover=literal["none", "applied", "ignored"]),
        "RefusedData": dict(code=refusal, field=field | None, blocking_item_id=str | None),
        "NativeRequiredData": dict(reason=native),
        "FailureData": dict(code=literal["internal", "busy"], reported_write_knowledge=literal["unknown", "pre_write"]),
        "ReplySpec": dict(status=int, body=bytes),
        "ReplyError": dict(code=literal["invalid_input", "invalid_combination", "invalid_text", "reply_too_large"]),
    }
    for name, hints in expected.items():
        assert typing.get_type_hints(getattr(p, name)) == hints, name
    reply_data = p.FormData | p.ProbeData | p.SavedData | p.RefusedData | p.NativeRequiredData | p.FailureData
    assert typing.get_type_hints(p.decode_wire) == {"scope": p.WireScope, "return": p.NativeWire | p.CandidateWire | p.WireInputError}
    assert typing.get_type_hints(p.encode_projection) == {"values": p.StoredValues, "return": p.Projection | p.NeedsNative}
    assert typing.get_type_hints(p.make_reply) == {"wire": p.CandidateWire | p.WireInputError, "outcome": reply_data, "return": p.ReplySpec | p.ReplyError}


def test_source_is_data_only_pure_and_dormant():
    _protocol()
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    allowed_imports = {"dataclasses", "typing", "hashlib", "json", "math"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed_imports for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed_imports
            assert all(alias.name != "*" for alias in node.names)
        assert not isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom, ast.Global, ast.Nonlocal, ast.Lambda))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"__import__", "eval", "exec", "compile", "open", "input", "print", "breakpoint"}
    assert {node.name for node in tree.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")} == {"decode_wire", "encode_projection", "make_reply"}
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert {node.name for node in classes if not node.name.startswith("_")} == set(RECORD_FIELDS)
    for node in classes:
        assert not node.bases and not node.keywords
        assert all(isinstance(statement, (ast.AnnAssign, ast.Pass)) or
                   (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str))
                   for statement in node.body)
        assert all(not isinstance(statement, ast.AnnAssign) or statement.value is None for statement in node.body)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            assert not node.decorator_list
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.TypeAlias)):
            value = node.value
            assert not any(isinstance(child, ast.Call) and not
                           (isinstance(child.func, ast.Name) and child.func.id in {"frozenset", "TypeVar"})
                           for child in ast.walk(value)) if value is not None else True
        else:
            assert isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef)) or (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))
    for path in (ROOT / "app").rglob("*.py"):
        if path == SERVICE:
            continue
        other = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(other):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names]
                assert not any("item_editor_protocol" in name for name in names), str(path)
                assert "item_editor_protocol" not in (getattr(node, "module", None) or ""), str(path)
            if isinstance(node, ast.Constant) and type(node.value) is str:
                assert "app.services.item_editor_protocol" not in node.value, str(path)


@pytest.mark.parametrize("wire_state,cell", ((name, index) for index, name in enumerate(("form", "probe", "apply", "malformed", "input_error"))))
@pytest.mark.parametrize("name,status,result,knowledge,permitted", OUTCOME_MATRIX, ids=[row[0] for row in OUTCOME_MATRIX])
def test_all_175_wire_outcome_cells(wire_state, cell, name, status, result, knowledge, permitted):
    p = _protocol()
    response = p.make_reply(_wire_state(p, wire_state), _outcome(p, name))
    if not permitted[cell]:
        _assert_error(p, response, "invalid_combination")
        return
    assert type(response) is p.ReplySpec and type(response.status) is int and response.status == status
    assert type(response.body) is bytes
    body = json.loads(response.body)
    assert tuple(body) == ("protocol", "version", "operation", "generation", "request", "item_id", "actor", "result", "outcome", "payload")
    assert body["protocol"] == "shelf.me2" and type(body["version"]) is int and body["version"] == 2
    assert body["result"] == result and body["outcome"] == knowledge
    if cell >= 3:
        assert [body[key] for key in ("operation", "generation", "request", "item_id", "actor")] == [None] * 5
    else:
        assert body["operation"] == wire_state
        assert body["generation"] == GENERATION and body["request"] == REQUEST and body["item_id"] == "7"
        assert body["actor"] == {"id": "5", "token_version": "1"}
    payload = body["payload"]
    if result == "form":
        assert payload == {"baseline": BASELINE, "html": "<form>é</form>"}
        assert tuple(payload) == ("baseline", "html")
    elif result == "probe":
        assert payload == {"comparison": "same"}
    elif result == "saved":
        assert payload == {"baseline": BASELINE, "cover": "none"}
        assert tuple(payload) == ("baseline", "cover")
    elif result == "native_required":
        assert payload == {"reason": name.split(":")[1], "native_href": "/item/7/edit"}
        assert tuple(payload) == ("reason", "native_href")
    else:
        code = name.split(":")[1] if name.startswith("failure:") else name
        assert payload == {"code": code, "field": None, "blocking_item_id": None}
        assert tuple(payload) == ("code", "field", "blocking_item_id")
    assert json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") == response.body


@pytest.mark.parametrize("op", ("form", "probe", "apply"))
def test_decode_complete_valid_wire_and_root(op):
    p = _protocol()
    for root in ("", "/shelf", "/A-._~9/section", "/" + "a" * 255):
        suffix = "/api/items/7" if op == "apply" else "/item/7/edit"
        result = p.decode_wire(_scope(p, op, path=root + suffix, raw_path=(root + suffix).encode("ascii"), root_path=root))
        assert type(result) is p.CandidateWire and result.problem is None
        fields, echo = result.fields, result.echo
        assert type(fields) is p.WireFields and type(echo) is p.Echo
        for record in (fields, echo):
            assert record.operation == op and record.generation == GENERATION and record.request == REQUEST
            assert record.item_id == "7" and type(record.actor) is p.ActorTag
            assert record.actor.id == "5" and record.actor.token_version == "1"
        assert fields.state == (None if op == "form" else BASELINE)
        assert fields.from_raw is None and fields.root_path == root


@pytest.mark.parametrize("query", (
    b"", b"from", b"from=", b"from=+", b"from=%GG", b"from=%", b"from=a/b?x#y",
    b"from=\xff\x00", b"from=one&from=two", b"q=%GG", b"q=__me2=2", b"q=%5f%5fme2",
    b"=x&&", b"__me=2", b"x__me2=2", b"from=__me2=2",
))
def test_marker_free_native_traffic_is_not_repaired_or_parsed(query):
    p = _protocol()
    result = p.decode_wire(_scope(p, headers=((b"cookie", b"opaque\xff"),), query_string=query,
                                  method="PATCH", path="/elsewhere/", raw_path=None))
    assert type(result) is p.NativeWire


@pytest.mark.parametrize("marker", (
    b"__me2", b"__ME2=2", b"__mE2suffix=2", b"__me2%GG=2", b"__me2_=2",
    b"%5F%5Fme2=2", b"%5f%5fME2=2", b"%255F%255Fme2=2",
    b"%25255f%25255fme2=2", b"%5F_me2-other=2", b"__me2=", b"__me2=02", b"__me2=%32",
))
def test_query_reserved_recognition_precedes_method_path_and_value_validation(marker):
    p = _protocol()
    result = p.decode_wire(_scope(p, headers=(), query_string=marker, asgi_type="websocket",
                                  method="get", path="/bad", raw_path=b"/bad"))
    _assert_malformed(p, result)
    assert result.echo.operation is None and result.echo.actor is None


@pytest.mark.parametrize("name", (b"X-Shelf-ME2", b"x-shelf-me2-extra", b"X-SHELF-ME2suffix", b"x-shelf-me2-"))
def test_every_raw_reserved_header_prefix_selects_candidate(name):
    p = _protocol()
    result = p.decode_wire(_scope(p, headers=((name, b"garbage"),), query_string=b"from=%GG",
                                  method="OPTIONS", path="/bad", raw_path=b"/bad"))
    _assert_malformed(p, result)


@pytest.mark.parametrize("case", ("query_only", "header_only", "wrong_query_value", "wrong_header_value"))
def test_exact_paired_mode_is_mandatory(case):
    p = _protocol()
    headers, query = _headers(), b"__me2=2"
    if case == "query_only":
        headers = tuple(pair for pair in headers if pair[0] != b"x-shelf-me2")
    elif case == "header_only":
        query = b"from=books"
    elif case == "wrong_query_value":
        query = b"__me2=3"
    else:
        headers = tuple((name, b"02" if name == b"x-shelf-me2" else value) for name, value in headers)
    _assert_malformed(p, p.decode_wire(_scope(p, headers=headers, query_string=query)))


@pytest.mark.parametrize("query", (
    b"__me2=2&__me2=2", b"__me2=2&__ME2=2", b"__me2=2&%5f%5fme2=2",
    b"__me2=2&from=a&from=a", b"__me2=2&from=a&%66rom=a", b"__me2=2&FROM=a",
    b"__me2=2&unknown=1", b"__me2=2&from", b"__me2=2&", b"&__me2=2",
    b"__me2=2&&from=a", b"__me2=2;from=a", b"__me2=2&from=a=b",
))
def test_candidate_query_grammar_and_raw_multiplicity(query):
    p = _protocol()
    _assert_malformed(p, p.decode_wire(_scope(p, query_string=query)))


@pytest.mark.parametrize("name,echo_field", (
    (b"x-shelf-me2", None), (b"x-shelf-me2-op", "operation"),
    (b"x-shelf-me2-generation", "generation"), (b"x-shelf-me2-request", "request"),
    (b"x-shelf-me2-actor", "actor"), (b"x-shelf-me2-state", None), (b"accept", None),
))
@pytest.mark.parametrize("variant", ("identical", "case_variant", "coalesced"))
def test_duplicate_headers_are_not_collapsed_and_echo_loses_provenance(name, echo_field, variant):
    p = _protocol()
    headers = _headers("probe")
    value = next(value for key, value in headers if key == name)
    if variant == "coalesced":
        headers = tuple((key, value + b", " + value if key == name else old) for key, old in headers)
    else:
        headers += ((name.upper() if variant == "case_variant" else name, value),)
    result = p.decode_wire(_scope(p, "probe", headers=headers))
    _assert_malformed(p, result)
    if echo_field is not None:
        assert getattr(result.echo, echo_field) is None
    assert result.echo.item_id == "7"
    if echo_field != "generation":
        assert result.echo.generation == GENERATION
    if echo_field != "request":
        assert result.echo.request == REQUEST


@pytest.mark.parametrize("name", (b"x-shelf-me2-op", b"x-shelf-me2-generation", b"x-shelf-me2-request", b"x-shelf-me2-actor"))
def test_missing_metadata_never_infers_operation_or_actor(name):
    p = _protocol()
    result = p.decode_wire(_scope(p, headers=tuple(pair for pair in _headers() if pair[0] != name)))
    _assert_malformed(p, result)
    field = {b"x-shelf-me2-op": "operation", b"x-shelf-me2-generation": "generation",
             b"x-shelf-me2-request": "request", b"x-shelf-me2-actor": "actor"}[name]
    assert getattr(result.echo, field) is None


@pytest.mark.parametrize("accept", (b"application/json", b"APPLICATION/JSON", b"Application/Json"))
def test_accept_media_type_case_only_is_permitted(accept):
    p = _protocol()
    headers = tuple((name, accept if name == b"accept" else value) for name, value in _headers())
    result = p.decode_wire(_scope(p, headers=headers))
    assert type(result) is p.CandidateWire and result.problem is None


@pytest.mark.parametrize("accept", (None, b"", b" application/json", b"application/json ", b"application/json; charset=utf-8",
                                   b"application/json,*/*", b"application/json,application/json", b"text/html", b"\xff"))
def test_accept_is_one_exact_media_type_not_negotiation(accept):
    p = _protocol()
    headers = tuple(pair for pair in _headers() if pair[0] != b"accept")
    if accept is not None:
        headers += ((b"accept", accept),)
    _assert_malformed(p, p.decode_wire(_scope(p, headers=headers)))


@pytest.mark.parametrize("name", (b"HX-Request", b"hx-target", b"hX-", b"HX-Anything"))
def test_hx_request_headers_are_forbidden(name):
    p = _protocol()
    _assert_malformed(p, p.decode_wire(_scope(p, headers=_headers() + ((name, b"false"),))))


@pytest.mark.parametrize("op,method,path", (
    ("form", "POST", "/item/7/edit"), ("probe", "HEAD", "/item/7/edit"),
    ("apply", "GET", "/api/items/7"), ("form", "get", "/item/7/edit"),
    ("apply", "post", "/api/items/7"), ("form", "GET", "/api/items/7"),
    ("apply", "POST", "/item/7/edit"),
))
def test_method_path_operation_mismatch_preserves_only_grammatical_echo(op, method, path):
    p = _protocol()
    result = p.decode_wire(_scope(p, op, method=method, path=path, raw_path=path.encode("ascii")))
    _assert_malformed(p, result)
    assert result.echo.operation == op


@pytest.mark.parametrize("changes", (
    {"asgi_type": "websocket"}, {"raw_path": None}, {"raw_path": b"/item/%37/edit"},
    {"path": "/item/7/edit/", "raw_path": b"/item/7/edit/"},
    {"path": "/item//7/edit", "raw_path": b"/item//7/edit"},
    {"path": "/item/7/edit", "raw_path": b"/item/8/edit"},
    {"path": "/shelf/item/7/edit", "raw_path": b"/item/7/edit", "root_path": "/shelf"},
    {"path": "/item/7/edit", "raw_path": b"/item/7/edit", "root_path": "/shelf"},
    {"path": "/item/7%2f/edit", "raw_path": b"/item/7%2f/edit"},
    {"path": "/item/7/edit?x", "raw_path": b"/item/7/edit?x"},
))
def test_no_path_normalization_redirect_or_alternate_mount_interpretation(changes):
    p = _protocol()
    _assert_malformed(p, p.decode_wire(_scope(p, **changes)))


@pytest.mark.parametrize("root", ("/", "//shelf", "/shelf/", "/a//b", "/.", "/..", "/a/../b", "/%61",
                                 "/a\\b", "/a?b", "/a#b", "/a:b", "/a b", "/é", "/a\x00"))
def test_root_is_canonical_ascii_segments_not_a_url(root):
    p = _protocol()
    path = root + "/item/7/edit"
    _assert_malformed(p, p.decode_wire(_scope(p, root_path=root, path=path, raw_path=path.encode("utf-8"))))
    _assert_error(p, p.make_reply(_wire(p, root_path=root), p.NativeRequiredData(reason="invalid_state")))


@pytest.mark.parametrize("field", ("item_id", "actor_id", "token_version"))
@pytest.mark.parametrize("identifier", ("1", "9223372036854775807"))
def test_canonical_signed64_positive_strings_round_trip(field, identifier):
    p = _protocol()
    changes = {field: identifier}
    wire = _wire(p, **changes)
    reply = p.make_reply(wire, p.RefusedData(code="stale", field=None, blocking_item_id=None))
    assert type(reply) is p.ReplySpec
    body = json.loads(reply.body)
    if field == "item_id":
        assert body["item_id"] == identifier
        scope = _scope(p, path=f"/item/{identifier}/edit", raw_path=f"/item/{identifier}/edit".encode("ascii"))
    else:
        actor = (identifier + ":1") if field == "actor_id" else ("5:" + identifier)
        scope = _scope(p, headers=tuple((key, actor.encode("ascii") if key == b"x-shelf-me2-actor" else value) for key, value in _headers()))
        assert body["actor"]["id" if field == "actor_id" else "token_version"] == identifier
    result = p.decode_wire(scope)
    assert type(result) is p.CandidateWire and result.problem is None


@pytest.mark.parametrize("field", ("item_id", "actor_id", "token_version"))
@pytest.mark.parametrize("identifier", ("", "0", "00", "01", "+1", "-1", " 1", "1 ", "1.0", "1e0", "١", "１",
                                       "9223372036854775808", "10000000000000000000", "1\x00", "1/2"))
def test_identifier_grammar_is_not_numeric_coercion(field, identifier):
    p = _protocol()
    _assert_error(p, p.make_reply(_wire(p, **{field: identifier}), p.RefusedData(code="invalid_protocol", field=None, blocking_item_id=None)))
    if field == "item_id":
        path = f"/item/{identifier}/edit"
        scope = _scope(p, path=path, raw_path=path.encode("utf-8"))
    else:
        actor = (identifier + ":1") if field == "actor_id" else ("5:" + identifier)
        scope = _scope(p, headers=tuple((key, actor.encode("utf-8") if key == b"x-shelf-me2-actor" else value) for key, value in _headers()))
    result = p.decode_wire(scope)
    _assert_malformed(p, result)
    assert getattr(result.echo, "item_id" if field == "item_id" else "actor") is None


@pytest.mark.parametrize("actor", (b"5", b"5:", b":1", b"5:1:1", b"5,1", b"5 :1", b"5:01", b"0:1", b"5:0"))
def test_actor_pair_is_atomic_not_partially_echoed(actor):
    p = _protocol()
    result = p.decode_wire(_scope(p, headers=tuple((key, actor if key == b"x-shelf-me2-actor" else value) for key, value in _headers())))
    _assert_malformed(p, result)
    assert result.echo.actor is None


@pytest.mark.parametrize("field", ("generation", "request"))
@pytest.mark.parametrize("text", ("", "a" * 31, "a" * 33, "A" * 32, "g" * 32, " " + "a" * 31, "é" * 32, "a" * 31 + "\x00"))
def test_generation_and_request_are_exact_lower_hex(field, text):
    p = _protocol()
    name = ("x-shelf-me2-" + field).encode("ascii")
    result = p.decode_wire(_scope(p, headers=tuple((key, text.encode("utf-8") if key == name else value) for key, value in _headers())))
    _assert_malformed(p, result)
    assert getattr(result.echo, field) is None
    wire = _wire(p)
    object.__setattr__(wire.fields, field, text)
    object.__setattr__(wire.echo, field, text)
    _assert_error(p, p.make_reply(wire, _outcome(p, "form")))


def test_equal_generation_request_and_separate_equal_actor_records_are_not_authority():
    p = _protocol()
    scope = _scope(p, headers=tuple((key, GENERATION.encode("ascii") if key == b"x-shelf-me2-request" else value) for key, value in _headers()))
    decoded = p.decode_wire(scope)
    assert type(decoded) is p.CandidateWire and decoded.problem is None
    wire = _wire(p)
    assert wire.echo.actor is not wire.fields.actor
    assert wire.echo.actor != wire.fields.actor  # eq=False; do not rely on record equality.
    object.__setattr__(wire.fields, "request", GENERATION)
    object.__setattr__(wire.echo, "request", GENERATION)
    response = p.make_reply(wire, _outcome(p, "form"))
    assert type(response) is p.ReplySpec and response.status == 200


@pytest.mark.parametrize("op,state", (("form", BASELINE), ("probe", None), ("apply", None), ("probe", ""),
                                     ("apply", "SHA256:" + "a" * 64), ("probe", "sha256:" + "A" * 64),
                                     ("apply", "sha256:" + "a" * 63), ("probe", "sha256:" + "a" * 65)))
def test_state_is_required_or_forbidden_by_operation(op, state):
    p = _protocol()
    headers = tuple(pair for pair in _headers(op) if pair[0] != b"x-shelf-me2-state")
    if state is not None:
        headers += ((b"x-shelf-me2-state", state.encode("ascii")),)
    _assert_malformed(p, p.decode_wire(_scope(p, op, headers=headers)))
    wire = _wire(p, op)
    object.__setattr__(wire.fields, "state", state)
    _assert_error(p, p.make_reply(wire, _outcome(p, "invalid_protocol")))


def test_raw_from_all_256_single_bytes_and_native_preservation():
    p = _protocol()
    allowed = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~+"
    for number in range(256):
        raw = bytes((number,))
        native_query = b"from=" + raw
        assert type(p.decode_wire(_scope(p, headers=(), query_string=native_query))) is p.NativeWire, number
        result = p.decode_wire(_scope(p, query_string=b"__me2=2&from=" + raw))
        if number in allowed:
            assert type(result) is p.CandidateWire and result.problem is None, number
            assert result.fields.from_raw == raw, number
            reply = p.make_reply(result, p.NativeRequiredData(reason="invalid_state"))
            assert type(reply) is p.ReplySpec
            assert json.loads(reply.body)["payload"]["native_href"] == "/item/7/edit?from=" + raw.decode("ascii")
        else:
            _assert_malformed(p, result)
            _assert_error(p, p.make_reply(_wire(p, from_raw=raw), p.NativeRequiredData(reason="invalid_state")))


def test_raw_from_all_484_hex_spellings_are_opaque_and_preserved():
    p = _protocol()
    count = 0
    for first in "0123456789ABCDEFabcdef":
        for second in "0123456789ABCDEFabcdef":
            raw = ("%" + first + second).encode("ascii")
            result = p.decode_wire(_scope(p, query_string=b"__me2=2&from=" + raw))
            assert type(result) is p.CandidateWire and result.problem is None, raw
            assert result.fields.from_raw == raw
            response = p.make_reply(result, p.NativeRequiredData(reason="invalid_state"))
            assert type(response) is p.ReplySpec
            expected = (ENVELOPE_HEAD + b'"result":"native_required","outcome":"not_saved","payload":'
                        b'{"reason":"invalid_state","native_href":"/item/7/edit?from=' + raw + b'"}}')
            assert response.body == expected
            count += 1
    assert count == 484


@pytest.mark.parametrize("raw,suffix", (
    (None, b""), (b"", b"?from="), (b"+", b"?from=+"), (b"a+b", b"?from=a+b"),
    (b"%2B", b"?from=%2B"), (b"%26", b"?from=%26"), (b"%23", b"?from=%23"),
    (b"%2526", b"?from=%2526"), (b"%2f%2F", b"?from=%2f%2F"),
    (b"%C3%A9", b"?from=%C3%A9"), (b"%00", b"?from=%00"), (b"%0A%7F%FF", b"?from=%0A%7F%FF"),
))
def test_raw_component_absence_empty_plus_and_percent_identities(raw, suffix):
    p = _protocol()
    for op in ("form", "probe", "apply"):
        path = "/shelf/api/items/7" if op == "apply" else "/shelf/item/7/edit"
        query = b"__me2=2" if raw is None else b"from=" + raw + b"&__me2=2"
        result = p.decode_wire(_scope(p, op, query_string=query, root_path="/shelf", path=path, raw_path=path.encode("ascii")))
        assert type(result) is p.CandidateWire and result.problem is None
        assert result.fields.from_raw == raw
        response = p.make_reply(result, p.NativeRequiredData(reason="context_too_large"))
        assert type(response) is p.ReplySpec
        payload = json.loads(response.body)["payload"]
        assert payload == {"reason": "context_too_large", "native_href": "/shelf/item/7/edit" + suffix.decode("ascii")}
        assert b"__me2" not in response.body


@pytest.mark.parametrize("raw", (b"%", b"%2", b"%GG", b"%0g", b"%%20", b"series#x", b"a b", b"a\tb", b"a\rb", b"a\nb",
                                 b"a\x00b", b"\x7f", b"\xff", b"a=b", b"?", b"/", b":", b";", b'"', b"'", b"\\", b"&"))
def test_malformed_raw_components_never_become_native_or_href(raw):
    p = _protocol()
    _assert_malformed(p, p.decode_wire(_scope(p, query_string=b"__me2=2&from=" + raw)))
    _assert_error(p, p.make_reply(_wire(p, from_raw=raw), p.NativeRequiredData(reason="invalid_state")))


def test_query_1024_and_raw_from_1011_are_inclusive_not_1012():
    p = _protocol()
    raw = b"x" * 1011
    assert len(b"__me2=2&from=" + raw) == 1024
    result = p.decode_wire(_scope(p, query_string=b"__me2=2&from=" + raw))
    assert type(result) is p.CandidateWire and result.problem is None and result.fields.from_raw == raw
    reply = p.make_reply(_wire(p, from_raw=raw), p.NativeRequiredData(reason="invalid_state"))
    assert type(reply) is p.ReplySpec
    assert json.loads(reply.body)["payload"]["native_href"] == "/item/7/edit?from=" + "x" * 1011
    _assert_malformed(p, p.decode_wire(_scope(p, query_string=b"__me2=2&from=" + raw + b"x")))
    _assert_error(p, p.make_reply(_wire(p, from_raw=raw + b"x"), p.NativeRequiredData(reason="invalid_state")))


def test_input_domain_caps_are_terminal_even_with_markers_after_budget():
    p = _protocol()
    native = _scope(p, headers=(), query_string=b"")
    valid = (
        dataclasses.replace(native, headers=((b"x", b""),) * 256),
        dataclasses.replace(native, headers=((b"x", b"a" * 65535),)),
        dataclasses.replace(native, headers=((b"x" * 128, b""),)),
        dataclasses.replace(native, query_string=b"x=" + b"a" * 65534),
        dataclasses.replace(native, query_string=b"x" * 128 + b"="),
    )
    for scope in valid:
        assert type(p.decode_wire(scope)) is p.NativeWire
    invalid = (
        dataclasses.replace(native, headers=((b"x", b""),) * 256 + ((b"x-shelf-me2", b"2"),)),
        dataclasses.replace(native, headers=((b"x", b"a" * 65536),)),
        dataclasses.replace(native, headers=((b"x" * 129, b""),)),
        dataclasses.replace(native, query_string=b"x=" + b"a" * 65534 + b"&__me2=2"),
        dataclasses.replace(native, query_string=b"x" * 129 + b"=&__me2=2"),
        _scope(p, path="x" * 4097, raw_path=b"x" * 4097),
        _scope(p, root_path="/" + "a" * 256),
    )
    for scope in invalid:
        result = p.decode_wire(scope)
        assert type(result) is p.WireInputError and result.code == "input_bound"
    _assert_malformed(p, p.decode_wire(_scope(p, path="x" * 4096, raw_path=b"x" * 4096)))
    for size in (128, 129):
        headers = tuple((key, b"a" * size if key == b"x-shelf-me2-generation" else value) for key, value in _headers())
        _assert_malformed(p, p.decode_wire(_scope(p, headers=headers)))


def test_candidate_nonreserved_header_bytes_count_towards_aggregate_cap():
    p = _protocol()
    headers = _headers()
    size = sum(len(name) + len(value) for name, value in headers)
    fill = b"x" * (65536 - size - 1)
    result = p.decode_wire(_scope(p, headers=headers + ((b"z", fill),)))
    assert type(result) is p.CandidateWire and result.problem is None
    result = p.decode_wire(_scope(p, headers=headers + ((b"z", fill + b"x"),)))
    assert type(result) is p.WireInputError and result.code == "input_bound"


@pytest.mark.parametrize("kind", ("bomb", "map", "list", "text_subclass", "tuple_subclass"))
def test_decode_rejects_nonexact_scope_without_touching_it(kind):
    p = _protocol()
    result = p.decode_wire(_bad(kind))
    assert type(result) is p.WireInputError and result.code == "input_type"


@pytest.mark.parametrize("field", RECORD_FIELDS["WireScope"])
def test_decode_rejects_missing_and_poisoned_scope_slots(field):
    p = _protocol()
    kwargs = _sample_kwargs(p)["WireScope"]
    for missing in (False, True):
        scope = object.__new__(p.WireScope)
        for name, value in kwargs.items():
            if name != field:
                object.__setattr__(scope, name, value)
            elif not missing:
                object.__setattr__(scope, name, _Bomb())
        result = p.decode_wire(scope)
        assert type(result) is p.WireInputError and result.code == "input_type"


def test_decode_rejects_scope_and_nested_primitive_container_subclasses():
    p = _protocol()
    kwargs = _sample_kwargs(p)["WireScope"]
    subclass = type("ScopeSubclass", (p.WireScope,), {"__slots__": (), "__eq__": _Bomb._explode})
    assert type(p.decode_wire(subclass(**kwargs))) is p.WireInputError
    changes = (
        {"asgi_type": _Text("http")}, {"method": _Text("GET")}, {"path": _Text("/item/7/edit")},
        {"root_path": _Text("")}, {"raw_path": _Bytes(b"/item/7/edit")}, {"query_string": _Bytes(b"__me2=2")},
        {"headers": list(_headers())}, {"headers": _Tuple(_headers())},
        {"headers": (list(_headers()[0]),)}, {"headers": (_Tuple(_headers()[0]),)},
        {"headers": ((b"x",),)}, {"headers": ((b"x", b"y", b"z"),)},
        {"headers": ((_Bytes(b"x"), b"y"),)}, {"headers": ((b"x", _Bytes(b"y")),)},
        {"headers": ((_Bomb(), b"y"),)}, {"headers": ((b"x", _Bomb()),)},
        {"headers": (_Bomb(),)}, {"headers": (pair for pair in _headers())},
        {"query_string": bytearray(b"__me2=2")}, {"raw_path": memoryview(b"/item/7/edit")},
    )
    for change in changes:
        result = p.decode_wire(_scope(p, **change))
        assert type(result) is p.WireInputError and result.code == "input_type"


def test_repeated_percent_recognition_runs_to_fixed_point_within_raw_key_cap():
    p = _protocol()
    key = b"%" + b"25" * 60 + b"5F_me2"
    assert len(key) == 127
    _assert_malformed(p, p.decode_wire(_scope(p, headers=(), query_string=key + b"=2")))
    _assert_malformed(p, p.decode_wire(_scope(p, headers=(), query_string=key + b"x=2")))
    result = p.decode_wire(_scope(p, headers=(), query_string=key + b"xx=2"))
    assert type(result) is p.WireInputError and result.code == "input_bound"


@pytest.mark.parametrize("operation", (b"", b"FORM", b" form", b"form ", b"save", b"form,probe", b"\xff"))
def test_invalid_operation_is_not_inferred_from_method(operation):
    p = _protocol()
    headers = tuple((name, operation if name == b"x-shelf-me2-op" else value) for name, value in _headers())
    result = p.decode_wire(_scope(p, headers=headers))
    _assert_malformed(p, result)
    assert result.echo.operation is None
    for text in ("", "FORM", "save", " form", "form "):
        wire = _wire(p)
        object.__setattr__(wire.fields, "operation", text)
        object.__setattr__(wire.echo, "operation", text)
        _assert_error(p, p.make_reply(wire, _outcome(p, "invalid_protocol")))


def test_reserved_metadata_has_no_trimming_nonascii_or_unknown_name_escape():
    p = _protocol()
    for name, value in _headers("probe"):
        if not name.startswith(b"x-shelf-me2"):
            continue
        for invalid in (b" " + value, value + b" ", value + b"\xff", value + b"\x00", b"a" * 129):
            headers = tuple((key, invalid if key == name else old) for key, old in _headers("probe"))
            _assert_malformed(p, p.decode_wire(_scope(p, "probe", headers=headers)))
    for name in (b"x-shelf-me2-extra", b"x-shelf-me2foo", b"X-SHELF-ME2-\xff"):
        _assert_malformed(p, p.decode_wire(_scope(p, headers=_headers() + ((name, b"2"),))))
    for extra in ({"path": "x" * 4097}, {"raw_path": b"x" * 4097}):
        result = p.decode_wire(_scope(p, **extra))
        assert type(result) is p.WireInputError and result.code == "input_bound"


def _reply_record(p, name, record):
    wire = _wire(p)
    if name in ("CandidateWire", "WireInputError", "NativeWire"):
        return p.make_reply(record, _outcome(p, "invalid_protocol"))
    if name == "Echo":
        object.__setattr__(wire, "echo", record)
    elif name == "WireFields":
        object.__setattr__(wire, "fields", record)
    elif name == "ActorTag":
        object.__setattr__(wire.fields, "actor", record)
    else:
        return p.make_reply(wire, record)
    return p.make_reply(wire, _outcome(p, "form"))


REPLY_INPUT_RECORDS = (
    "CandidateWire", "Echo", "WireFields", "ActorTag", "WireInputError",
    "FormData", "ProbeData", "SavedData", "RefusedData", "NativeRequiredData", "FailureData",
)


@pytest.mark.parametrize("name", REPLY_INPUT_RECORDS)
def test_reply_missing_and_poisoned_slots_are_invalid_before_outcome_dispatch(name):
    p = _protocol()
    kwargs = _sample_kwargs(p)[name]
    for field in RECORD_FIELDS[name]:
        for missing in (False, True):
            record = object.__new__(getattr(p, name))
            for key, value in kwargs.items():
                if key != field:
                    object.__setattr__(record, key, value)
                elif not missing:
                    object.__setattr__(record, key, _Bomb())
            _assert_error(p, _reply_record(p, name, record))


@pytest.mark.parametrize("name", REPLY_INPUT_RECORDS + ("NativeWire",))
def test_reply_record_subclasses_are_not_contract_records(name):
    p = _protocol()
    subclass = type(name + "Subclass", (getattr(p, name),), {
        "__slots__": (), "__eq__": _Bomb._explode, "__repr__": _Bomb._explode,
        "__bool__": _Bomb._explode, "__hash__": _Bomb._explode,
    })
    _assert_error(p, _reply_record(p, name, subclass(**_sample_kwargs(p)[name])))


@pytest.mark.parametrize("kind", ("bomb", "text_subclass", "bytes_subclass", "int_subclass", "float_subclass", "tuple_subclass", "bool", "map", "list", "generator"))
def test_reply_wrong_top_level_types_are_not_inspected(kind):
    p = _protocol()
    _assert_error(p, p.make_reply(_bad(kind), _outcome(p, "form")))
    _assert_error(p, p.make_reply(_wire(p), _bad(kind)))


def test_native_wire_is_never_a_reply_input_for_any_valid_outcome():
    p = _protocol()
    for name, *_ in OUTCOME_MATRIX:
        _assert_error(p, p.make_reply(p.NativeWire(), _outcome(p, name)))


@pytest.mark.parametrize("problem", WIRE_PROBLEMS)
def test_malformed_wire_has_nullable_grammatical_echo_and_no_native_href(problem):
    p = _protocol()
    echo = p.Echo(operation="apply", generation=GENERATION, request=REQUEST, item_id=MAX_ID,
                  actor=p.ActorTag(id=MAX_ID, token_version=MAX_ID))
    wire = p.CandidateWire(echo=echo, fields=None, problem=problem)
    response = p.make_reply(wire, p.RefusedData(code="auth_required", field=None, blocking_item_id=None))
    assert type(response) is p.ReplySpec and response.status == 401
    body = json.loads(response.body)
    assert body["operation"] == "apply" and body["item_id"] == MAX_ID
    assert body["actor"] == {"id": MAX_ID, "token_version": MAX_ID}
    assert body["payload"] == {"code": "auth_required", "field": None, "blocking_item_id": None}
    _assert_error(p, p.make_reply(wire, p.NativeRequiredData(reason="invalid_state")), "invalid_combination")


def test_candidate_requires_exactly_one_fields_or_problem_and_complete_echo():
    p = _protocol()
    valid = _wire(p)
    variants = (
        dataclasses.replace(valid, problem="query"),
        dataclasses.replace(valid, fields=None),
        dataclasses.replace(valid, fields=None, problem="unknown"),
        dataclasses.replace(valid, fields=None, problem=_Text("query")),
        dataclasses.replace(valid, echo=None),
        dataclasses.replace(valid, echo={}),
        dataclasses.replace(valid, fields={}),
    )
    for wire in variants:
        for outcome in (_outcome(p, "form"), _outcome(p, "auth_required"), _outcome(p, "failure:internal:unknown")):
            _assert_error(p, p.make_reply(wire, outcome))


@pytest.mark.parametrize("field,value", (
    ("operation", "probe"), ("generation", "b" * 32), ("request", "b" * 32), ("item_id", "8"),
))
def test_valid_echo_fields_must_agree_with_fields_by_value(field, value):
    p = _protocol()
    wire = _wire(p)
    object.__setattr__(wire.echo, field, value)
    _assert_error(p, p.make_reply(wire, _outcome(p, "auth_required")))


@pytest.mark.parametrize("field", ("id", "token_version"))
def test_echo_actor_disagreement_is_an_error_not_silently_dropped(field):
    p = _protocol()
    wire = _wire(p)
    object.__setattr__(wire.echo.actor, field, "2")
    _assert_error(p, p.make_reply(wire, _outcome(p, "form")))


def test_nullable_echo_is_not_reconstructed_from_valid_fields():
    p = _protocol()
    wire = _wire(p)
    object.__setattr__(wire, "echo", p.Echo(operation=None, generation=None, request=None, item_id=None, actor=None))
    response = p.make_reply(wire, p.FormData(baseline=BASELINE, html=""))
    assert type(response) is p.ReplySpec
    expected = (NULL_HEAD + b'"result":"form","outcome":"not_applicable","payload":'
                b'{"baseline":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","html":""}}')
    assert response.body == expected


@pytest.mark.parametrize("code", ("input_type", "input_bound"))
def test_input_error_has_null_correlation_and_never_exposes_internal_code(code):
    p = _protocol()
    response = p.make_reply(p.WireInputError(code=code), _outcome(p, "invalid_protocol"))
    assert type(response) is p.ReplySpec and response.status == 400
    assert response.body == (NULL_HEAD + b'"result":"refused","outcome":"not_saved","payload":'
                             b'{"code":"invalid_protocol","field":null,"blocking_item_id":null}}')
    assert code.encode("ascii") not in response.body
    _assert_error(p, p.make_reply(p.WireInputError(code="unknown"), _outcome(p, "invalid_protocol")))


@pytest.mark.parametrize("record_name", ("Echo", "WireFields", "ActorTag"))
def test_exact_scalar_types_in_constructed_wire_and_echo_are_revalidated(record_name):
    p = _protocol()
    for field, original in _sample_kwargs(p)[record_name].items():
        if original is None:
            replacements = (_Bomb(), False, _Text(""))
        elif type(original) is str:
            replacements = (_Bomb(), _Text(original), True, 1, b"x")
        else:
            replacements = (_Bomb(), {}, "5:1")
        for replacement in replacements:
            record = getattr(p, record_name)(**_sample_kwargs(p)[record_name])
            object.__setattr__(record, field, replacement)
            _assert_error(p, _reply_record(p, record_name, record))


def test_constructed_from_raw_and_echo_actor_are_not_trusted_by_record_annotation():
    p = _protocol()
    for raw in (_Bytes(b"ok"), _Text("ok"), bytearray(b"ok"), memoryview(b"ok"), "ok", _Bomb()):
        _assert_error(p, p.make_reply(_wire(p, from_raw=raw), p.NativeRequiredData(reason="invalid_state")))
    actor_subclass = type("ActorSubclass", (p.ActorTag,), {"__slots__": (), "__eq__": _Bomb._explode})
    incomplete = object.__new__(p.ActorTag)
    object.__setattr__(incomplete, "id", "5")
    poisoned = p.ActorTag(id="5", token_version=_Bomb())
    for actor in (actor_subclass(id="5", token_version="1"), incomplete, poisoned, p.ActorTag(id="5", token_version="01")):
        for malformed in (False, True):
            wire = _wire_state(p, "malformed") if malformed else _wire(p)
            object.__setattr__(wire.echo, "actor", actor)
            _assert_error(p, p.make_reply(wire, _outcome(p, "invalid_protocol")))


@pytest.mark.parametrize("field", RECORD_FIELDS["Echo"])
def test_malformed_echo_also_rejects_missing_poison_and_noncanonical_fields(field):
    p = _protocol()
    replacements = {"operation": "FORM", "generation": "A" * 32, "request": "a" * 31,
                    "item_id": "01", "actor": p.ActorTag(id="0", token_version="1")}
    wire = _wire_state(p, "malformed")
    object.__setattr__(wire.echo, field, replacements[field])
    _assert_error(p, p.make_reply(wire, _outcome(p, "invalid_protocol")))


@pytest.mark.parametrize("record_name", ("FormData", "ProbeData", "SavedData", "RefusedData", "NativeRequiredData", "FailureData"))
def test_invalid_outcome_records_precede_unsupported_matrix_cells(record_name):
    p = _protocol()
    # A malformed wire supports none of form/probe/saved/native, but malformed
    # records must still report invalid_input, not invalid_combination.
    for field, original in _sample_kwargs(p)[record_name].items():
        replacements = [_Bomb(), True, 1, b"x"]
        if type(original) is str:
            replacements.append(_Text(original))
        for replacement in replacements:
            outcome = getattr(p, record_name)(**_sample_kwargs(p)[record_name])
            object.__setattr__(outcome, field, replacement)
            _assert_error(p, p.make_reply(_wire_state(p, "malformed"), outcome))


@pytest.mark.parametrize("code,fields", DOMAIN_FIELDS)
def test_refusal_domain_field_whitelist_is_exact_and_null_remains_valid(code, fields):
    p = _protocol()
    for field in (None,) + EDITABLE_FIELDS + ("not_a_field",):
        outcome = p.RefusedData(code=code, field=field, blocking_item_id=None)
        response = p.make_reply(_wire(p, "apply"), outcome)
        if field is None or field in fields:
            assert type(response) is p.ReplySpec and response.status == 422
            assert json.loads(response.body)["payload"] == {"code": code, "field": field, "blocking_item_id": None}
        else:
            _assert_error(p, response)


def test_transport_auth_state_and_infrastructure_fields_cannot_be_supplied():
    p = _protocol()
    for name, status, result, _, _ in OUTCOME_MATRIX:
        if result != "refused" or status == 422 or name.startswith("failure:"):
            continue
        for field, blocker in (("title", None), ("cover", None), ("_csrf", None), ("from", None), (None, "1")):
            _assert_error(p, p.make_reply(_wire(p), p.RefusedData(code=name, field=field, blocking_item_id=blocker)))


@pytest.mark.parametrize("code", ("upc_conflict", "identifier_in_trash"))
def test_collision_blocking_id_is_nullable_canonical_positive_signed64(code):
    p = _protocol()
    for identifier in (None, "1", MAX_ID):
        response = p.make_reply(_wire(p), p.RefusedData(code=code, field="upc", blocking_item_id=identifier))
        assert type(response) is p.ReplySpec
        assert json.loads(response.body)["payload"]["blocking_item_id"] == identifier
    for identifier in ("", "0", "01", "+1", "-1", "9223372036854775808", "１", 1, True, _Text("1"), _Bomb()):
        _assert_error(p, p.make_reply(_wire(p), p.RefusedData(code=code, field=None, blocking_item_id=identifier)))


def test_other_domain_codes_cannot_carry_blocking_ids_or_invented_codes():
    p = _protocol()
    for code, _ in DOMAIN_FIELDS:
        if code not in ("upc_conflict", "identifier_in_trash"):
            _assert_error(p, p.make_reply(_wire(p), p.RefusedData(code=code, field=None, blocking_item_id="1")))
    for code in ("isbn_conflict", "internal", "busy", "rate_limited_unknown", "exception: private diagnostic", "invalid_protocol ", "INVALID_FORM", ""):
        _assert_error(p, p.make_reply(_wire(p), p.RefusedData(code=code, field=None, blocking_item_id=None)))


@pytest.mark.parametrize("baseline", ("", "a" * 64, "SHA256:" + "a" * 64, "sha256:" + "A" * 64,
                                    "sha256:" + "g" * 64, "sha256:" + "a" * 63, "sha256:" + "a" * 65,
                                    " " + BASELINE, BASELINE + " "))
def test_form_and_saved_baselines_are_strict_even_on_unsupported_wires(baseline):
    p = _protocol()
    for wire in (_wire(p), _wire_state(p, "malformed")):
        _assert_error(p, p.make_reply(wire, p.FormData(baseline=baseline, html="")))
        _assert_error(p, p.make_reply(wire, p.SavedData(baseline=baseline, cover="none")))


def test_outcome_finite_literals_and_mandatory_failure_knowledge():
    p = _protocol()
    invalid = (
        p.ProbeData(comparison="equal"), p.ProbeData(comparison="Same"),
        p.SavedData(baseline=BASELINE, cover="saved"), p.SavedData(baseline=BASELINE, cover=""),
        p.NativeRequiredData(reason="unsupported"),
        p.FailureData(code="error", reported_write_knowledge="unknown"),
        p.FailureData(code="rate_limited", reported_write_knowledge="unknown"),
        p.FailureData(code="internal", reported_write_knowledge=None),
        p.FailureData(code="busy", reported_write_knowledge="not_saved"),
        p.FailureData(code="internal", reported_write_knowledge="saved"),
    )
    for outcome in invalid:
        _assert_error(p, p.make_reply(_wire(p), outcome))
    with pytest.raises(TypeError):
        p.FailureData(code="internal")
    for cover in ("none", "applied", "ignored"):
        response = p.make_reply(_wire(p, "apply"), p.SavedData(baseline=BASELINE, cover=cover))
        assert type(response) is p.ReplySpec
        assert json.loads(response.body)["payload"] == {"baseline": BASELINE, "cover": cover}


@pytest.mark.parametrize("comparison", ("same", "different"))
def test_probe_is_comparison_only_and_credentials_never_enter_reply(comparison):
    p = _protocol()
    secrets = (b"synthetic-cookie-do-not-serialize", b"synthetic-csrf-do-not-serialize", b"synthetic-bearer-do-not-serialize")
    scope = _scope(p, "probe", headers=_headers("probe") + (
        (b"cookie", secrets[0]), (b"x-csrf-token", secrets[1]), (b"authorization", secrets[2]),
        (b"forwarded", b"host=evil.invalid"), (b"host", b"evil.invalid"),
    ))
    wire = p.decode_wire(scope)
    assert type(wire) is p.CandidateWire and wire.problem is None
    response = p.make_reply(wire, p.ProbeData(comparison=comparison))
    assert type(response) is p.ReplySpec and response.status == 200
    expected = (b'{"protocol":"shelf.me2","version":2,"operation":"probe",'
                b'"generation":"0123456789abcdef0123456789abcdef",'
                b'"request":"fedcba9876543210fedcba9876543210","item_id":"7",'
                b'"actor":{"id":"5","token_version":"1"},"result":"probe","outcome":"not_applicable",'
                b'"payload":{"comparison":"' + comparison.encode("ascii") + b'"}}')
    assert response.body == expected
    for forbidden in secrets + (b"baseline", b"html", b"title", b"draft", b"cookie", b"credential", b"header", b"evil.invalid"):
        assert forbidden not in response.body
    assert not hasattr(response, "headers")
    assert not hasattr(response, "cookies")


def test_html_is_json_data_not_html_escaped_or_fragment_sanitized():
    p = _protocol()
    html = '<form x="é">&</form>\n\\\t\r\b\f\x00\x01\u2028😀'
    expected = (FORM_PREFIX + b'<form x=\\"\xc3\xa9\\">&</form>\\n\\\\\\t\\r\\b\\f\\u0000\\u0001\xe2\x80\xa8\xf0\x9f\x98\x80' + FORM_SUFFIX)
    response = p.make_reply(_wire(p), p.FormData(baseline=BASELINE, html=html))
    assert type(response) is p.ReplySpec and response.body == expected
    assert json.loads(response.body)["payload"]["html"] == html


@pytest.mark.parametrize("unit,encoded,cost", (("x", b"x", 1), ("é", b"\xc3\xa9", 2), ("😀", b"\xf0\x9f\x98\x80", 4),
                                            ('"', b'\\"', 2), ("\\", b"\\\\", 2), ("\n", b"\\n", 2), ("\x00", b"\\u0000", 6)))
def test_reply_exact_mib_escaped_utf8_budget_and_one_byte_over(unit, encoded, cost, monkeypatch):
    p = _protocol()
    budget = 1048576 - len(FORM_PREFIX) - len(FORM_SUFFIX)
    count, remainder = divmod(budget, cost)
    html = unit * count + "x" * remainder
    expected = FORM_PREFIX + encoded * count + b"x" * remainder + FORM_SUFFIX
    assert len(expected) == 1048576
    response = p.make_reply(_wire(p), p.FormData(baseline=BASELINE, html=html))
    assert type(response) is p.ReplySpec and response.status == 200
    assert response.body == expected
    with monkeypatch.context() as guard:
        _no_serialization(guard, p)
        response = p.make_reply(_wire(p), p.FormData(baseline=BASELINE, html=html + "x"))
    _assert_error(p, response, "reply_too_large")


def test_reply_counts_full_metadata_not_just_html_and_preflights_before_serializing(monkeypatch):
    p = _protocol()
    html = "x" * (1048576 - len(FORM_PREFIX) - len(FORM_SUFFIX))
    with monkeypatch.context() as guard:
        _no_serialization(guard, p)
        response = p.make_reply(_wire(p, item_id=MAX_ID, actor_id=MAX_ID, token_version=MAX_ID), p.FormData(baseline=BASELINE, html=html))
    _assert_error(p, response, "reply_too_large")
    # Character cap first: do not scan/encode a huge surrogate-bearing value.
    with monkeypatch.context() as guard:
        _no_serialization(guard, p)
        response = p.make_reply(_wire(p), p.FormData(baseline=BASELINE, html="\ud800" * 1048577))
    _assert_error(p, response, "reply_too_large")


@pytest.mark.parametrize("html", ("\ud800", "\udfff", "before\ud800after", "\ud83d\ude00"))
def test_eligible_html_surrogates_are_invalid_text_not_replacement_characters(html, monkeypatch):
    p = _protocol()
    with monkeypatch.context() as guard:
        _no_serialization(guard, p)
        response = p.make_reply(_wire(p), p.FormData(baseline=BASELINE, html=html))
    _assert_error(p, response, "invalid_text")


def test_public_calls_are_deterministic_and_do_not_touch_io(monkeypatch):
    p = _protocol()
    scope, stored, wire = _scope(p), _stored(p), _wire(p)
    with monkeypatch.context() as guard:
        guard.setattr("builtins.open", _Bomb()._explode)
        guard.setattr("builtins.print", _Bomb()._explode)
        first = p.decode_wire(scope)
        projection = p.encode_projection(stored)
        response = p.make_reply(wire, _outcome(p, "form"))
        second = p.make_reply(wire, _outcome(p, "form"))
    assert type(first) is p.CandidateWire and first.problem is None
    assert type(projection) is p.Projection
    assert type(response) is p.ReplySpec and response.body == second.body
    assert stored.item is ITEM and stored.primary is PRIMARY
    assert scope.headers == _headers() and wire.fields.operation == "form"


@pytest.mark.parametrize("label,target,index,value,token,digest", PROJECTION_GOLDENS, ids=[row[0] for row in PROJECTION_GOLDENS])
def test_projection_literal_bytes_and_hash_goldens(label, target, index, value, token, digest):
    p = _protocol()
    item, primary, wishlist, cover = ITEM, PRIMARY, 0, None
    if target in ("item", "scalar"):
        item = _slot(item, index, value)
        if target == "item" and index == 0:
            primary = _slot(primary, 1, 8)
        if target == "item" and index == 26:
            cover = p.CoverFingerprint(sha256="a" * 64, byte_length=101)
    elif target == "primary":
        primary = _slot(primary, index, value)
        if index == 1:
            item = _slot(item, 0, 8)
    elif target == "wishlist":
        wishlist = 1
    elif target == "no_primary":
        primary = None
    elif target in ("cover_digest", "cover_length"):
        item = _slot(item, 26, "covers/7.jpg")
        cover = p.CoverFingerprint(sha256=value if target == "cover_digest" else "a" * 64,
                                   byte_length=value if target == "cover_length" else 101)
    _assert_projection(p, _stored(p, item=item, primary=primary, wishlisted=wishlist, cover=cover),
                       _golden_bytes(target, index, token), digest)


@pytest.mark.parametrize("kind", ("bomb", "map", "list", "text_subclass", "tuple_subclass", "generator"))
def test_projection_rejects_nonexact_record_without_traversal(kind):
    p = _protocol()
    _assert_native(p, p.encode_projection(_bad(kind)))


@pytest.mark.parametrize("field", RECORD_FIELDS["StoredValues"])
def test_projection_missing_and_poisoned_stored_slots(field, monkeypatch):
    p = _protocol()
    kwargs = _sample_kwargs(p)["StoredValues"]
    for missing in (False, True):
        values = object.__new__(p.StoredValues)
        for name, value in kwargs.items():
            if name != field:
                object.__setattr__(values, name, value)
            elif not missing:
                object.__setattr__(values, name, _Bomb())
        with monkeypatch.context() as guard:
            _no_serialization(guard, p)
            response = p.encode_projection(values)
        _assert_native(p, response)


@pytest.mark.parametrize("name", ("StoredValues", "CoverFingerprint"))
def test_projection_record_subclasses_are_rejected_without_record_equality(name):
    p = _protocol()
    subclass = type(name + "Subclass", (getattr(p, name),), {"__slots__": (), "__eq__": _Bomb._explode, "__repr__": _Bomb._explode})
    record = subclass(**_sample_kwargs(p)[name])
    values = record if name == "StoredValues" else _stored(p, item=_slot(ITEM, 26, "covers/7.jpg"), cover=record)
    _assert_native(p, p.encode_projection(values))


@pytest.mark.parametrize("slot", range(31), ids=ITEM_NAMES)
def test_every_item_slot_rejects_bools_bombs_subclasses_and_blobs(slot):
    p = _protocol()
    for kind in ("bomb", "bool", "text_subclass", "int_subclass", "float_subclass", "blob", "list", "map", "generator"):
        _assert_native(p, p.encode_projection(_stored(p, item=_slot(ITEM, slot, _bad(kind)))))


@pytest.mark.parametrize("slot", range(8), ids=PRIMARY_NAMES)
def test_every_primary_slot_rejects_bools_bombs_subclasses_and_blobs(slot):
    p = _protocol()
    for kind in ("bomb", "bool", "text_subclass", "int_subclass", "float_subclass", "blob", "list", "map", "generator"):
        _assert_native(p, p.encode_projection(_stored(p, primary=_slot(PRIMARY, slot, _bad(kind)))))


def test_projection_exact_tuple_shapes_and_no_secondary_list_are_required():
    p = _protocol()
    item_cases = (list(ITEM), _Tuple(ITEM), ITEM[:-1], ITEM + (_Bomb(),), {"id": 7}, (value for value in ITEM))
    primary_cases = (list(PRIMARY), _Tuple(PRIMARY), PRIMARY[:-1], PRIMARY + (_Bomb(),), (PRIMARY,), {"id": 11}, (value for value in PRIMARY))
    for item in item_cases:
        _assert_native(p, p.encode_projection(_stored(p, item=item)))
    for primary in primary_cases:
        _assert_native(p, p.encode_projection(_stored(p, primary=primary)))


def test_projection_rejects_cycles_depth_decimal_and_unknown_container_atoms_without_descent(monkeypatch):
    from decimal import Decimal

    p = _protocol()
    cycle = []
    cycle.append(cycle)
    deep = _Bomb()
    for _ in range(2000):
        deep = [deep]
    objects = (cycle, deep, Decimal("1"), (_Bomb(),), {"x": _Bomb()}, memoryview(b"x"), bytearray(b"x"), _Tuple((_Bomb(),)))
    for value in objects:
        with monkeypatch.context() as guard:
            _no_serialization(guard, p)
            response = p.encode_projection(_stored(p, item=_slot(ITEM, 29, value)))
        _assert_native(p, response)


def test_nullable_editable_field_types_are_exact_not_coerced():
    p = _protocol()
    for index in TEXT_SLOTS:
        for value in (None, ""):
            assert type(p.encode_projection(_stored(p, item=_slot(ITEM, index, value)))) is p.Projection
        for value in (0, 0.0):
            _assert_native(p, p.encode_projection(_stored(p, item=_slot(ITEM, index, value))))
    for index in INTEGER_SLOTS:
        for value in (None, 0):
            assert type(p.encode_projection(_stored(p, item=_slot(ITEM, index, value)))) is p.Projection
        for value in ("0", 0.0, -0.0):
            _assert_native(p, p.encode_projection(_stored(p, item=_slot(ITEM, index, value))))
    for index in REAL_SLOTS:
        for value in (None, 0.0, -0.0):
            assert type(p.encode_projection(_stored(p, item=_slot(ITEM, index, value)))) is p.Projection
        for value in ("0", 0):
            _assert_native(p, p.encode_projection(_stored(p, item=_slot(ITEM, index, value))))


def test_item_and_primary_identity_invariants_and_signed64_boundaries():
    p = _protocol()
    for identifier in (1, 9223372036854775807):
        item, primary = _slot(ITEM, 0, identifier), _slot(PRIMARY, 1, identifier)
        assert type(p.encode_projection(_stored(p, item=item, primary=primary))) is p.Projection
    for identifier in (None, 0, -1, 1.0, "7", 9223372036854775808, -9223372036854775809):
        _assert_native(p, p.encode_projection(_stored(p, item=_slot(ITEM, 0, identifier))))
    for index in INTEGER_SLOTS + (25, 27, 28, 29, 30):
        for value in (-9223372036854775809, 9223372036854775808):
            _assert_native(p, p.encode_projection(_stored(p, item=_slot(ITEM, index, value))))
    bad_primary = (
        (0, None), (0, 0), (0, -1), (0, 9223372036854775808),
        (1, 8), (1, None), (1, "7"), (1, 7.0),
        (2, None), (2, 0), (2, -1), (2, 9223372036854775808),
        (3, "23"), (3, 23.0), (3, 9223372036854775808),
        (4, None), (4, 0), (4, 2), (4, 1.0), (4, "1"),
        (5, "0"), (5, 0.0), (5, -9223372036854775809),
    )
    for index, value in bad_primary:
        _assert_native(p, p.encode_projection(_stored(p, primary=_slot(PRIMARY, index, value))))
    for index in (3, 5):
        for value in (None, 0, -9223372036854775808, 9223372036854775807):
            assert type(p.encode_projection(_stored(p, primary=_slot(PRIMARY, index, value)))) is p.Projection


def test_wishlist_exact_integer_binary_value_not_truthiness():
    p = _protocol()
    for value in (None, -1, 2, 0.0, 1.0, "0", "1", False, True, _Integer(1), _Bomb()):
        _assert_native(p, p.encode_projection(_stored(p, wishlisted=value)))


def test_nonfinite_floats_are_rejected_in_real_and_all_unrestricted_scalar_slots(monkeypatch):
    p = _protocol()
    for value in (float("nan"), float("inf"), float("-inf")):
        for index in REAL_SLOTS + (25, 27, 28, 29, 30):
            with monkeypatch.context() as guard:
                _no_serialization(guard, p)
                response = p.encode_projection(_stored(p, item=_slot(ITEM, index, value)))
            _assert_native(p, response)
        for index in (6, 7):
            _assert_native(p, p.encode_projection(_stored(p, primary=_slot(PRIMARY, index, value))))


@pytest.mark.parametrize("assessment", NATIVE_REASONS)
def test_caller_native_assessment_is_only_a_report_and_never_partially_projects(assessment, monkeypatch):
    p = _protocol()
    with monkeypatch.context() as guard:
        _no_serialization(guard, p)
        response = p.encode_projection(_stored(p, caller_form_assessment=assessment))
    _assert_native(p, response, assessment)
    assert not hasattr(response, "baseline") and not hasattr(response, "canonical_json")


def test_invalid_caller_assessment_is_not_a_certificate_or_coercible_truth_value():
    p = _protocol()
    for value in (None, True, 1, "SUPPORTED", "", "native", _Text("supported"), _Bomb()):
        _assert_native(p, p.encode_projection(_stored(p, caller_form_assessment=value)))


def test_cover_presence_digest_and_length_are_structural_no_io():
    p = _protocol()
    for path in (None, ""):
        assert type(p.encode_projection(_stored(p, item=_slot(ITEM, 26, path), cover=None))) is p.Projection
        _assert_native(p, p.encode_projection(_stored(p, item=_slot(ITEM, 26, path), cover=p.CoverFingerprint(sha256="a" * 64, byte_length=0))))
    item = _slot(ITEM, 26, "covers/7.jpg")
    _assert_native(p, p.encode_projection(_stored(p, item=item, cover=None)))
    for length in (0, 10485760):
        assert type(p.encode_projection(_stored(p, item=item, cover=p.CoverFingerprint(sha256="f" * 64, byte_length=length)))) is p.Projection
    for length in (-1, 10485761, None, 0.0, True, "101", _Integer(101), _Bomb()):
        _assert_native(p, p.encode_projection(_stored(p, item=item, cover=p.CoverFingerprint(sha256="a" * 64, byte_length=length))))
    for digest in ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64, BASELINE, None, b"a" * 64, _Text("a" * 64), _Bomb()):
        _assert_native(p, p.encode_projection(_stored(p, item=item, cover=p.CoverFingerprint(sha256=digest, byte_length=101))))


@pytest.mark.parametrize("field", RECORD_FIELDS["CoverFingerprint"])
def test_cover_missing_or_poisoned_slots_are_not_formatted_or_hashed(field, monkeypatch):
    p = _protocol()
    for missing in (False, True):
        cover = object.__new__(p.CoverFingerprint)
        for name, value in _sample_kwargs(p)["CoverFingerprint"].items():
            if name != field:
                object.__setattr__(cover, name, value)
            elif not missing:
                object.__setattr__(cover, name, _Bomb())
        with monkeypatch.context() as guard:
            _no_serialization(guard, p)
            response = p.encode_projection(_stored(p, item=_slot(ITEM, 26, "covers/7.jpg"), cover=cover))
        _assert_native(p, response)


def test_editable_text_65536_utf8_bytes_inclusive_and_preallocation_rejection(monkeypatch):
    p = _protocol()
    for text in ("x" * 65536, "é" * 32768, "😀" * 16384):
        assert type(p.encode_projection(_stored(p, item=_slot(ITEM, 10, text)))) is p.Projection
        with monkeypatch.context() as guard:
            _no_serialization(guard, p)
            response = p.encode_projection(_stored(p, item=_slot(ITEM, 10, text + "x")))
        _assert_native(p, response, "state_too_large")
    with monkeypatch.context() as guard:
        _no_serialization(guard, p)
        response = p.encode_projection(_stored(p, item=_slot(ITEM, 29, "\ud800" * 131073)))
    _assert_native(p, response, "state_too_large")


@pytest.mark.parametrize("index", TEXT_SLOTS + (25, 27, 28, 29, 30), ids=lambda index: ITEM_NAMES[index])
def test_projection_surrogates_refuse_without_json_or_hash(index, monkeypatch):
    p = _protocol()
    with monkeypatch.context() as guard:
        _no_serialization(guard, p)
        response = p.encode_projection(_stored(p, item=_slot(ITEM, index, "x\ud800y")))
    _assert_native(p, response)


@pytest.mark.parametrize("index", (6, 7))
def test_primary_timestamp_surrogates_are_not_repaired(index):
    p = _protocol()
    _assert_native(p, p.encode_projection(_stored(p, primary=_slot(PRIMARY, index, "\udfff"))))


PROJECTION_BUDGET_PREFIX = b'["shelf.me2.state",2,[["int","7"],' + b'["null"],' * 28 + b'["text","'
PROJECTION_BUDGET_SUFFIX = b'"],["null"]],["int","0"],null,null]'


@pytest.mark.parametrize("unit,encoded,count,remainder,digest", (
    ("x", b"x", 130742, 0, "sha256:5576c4311505f6b2e69a2a70ba4882ff4ab96a39136a86612c09c7e23d2907b2"),
    ("é", b"\xc3\xa9", 65371, 0, "sha256:2c13468d1c81ecf14c086780c7d649458867acc9a0fc198ba521c645bc2bb5e4"),
    ("\x00", b"\\u0000", 21790, 2, "sha256:be225c15fac8f75232b925402f4ee087baa04c702453ab4eaa321f39b74daa73"),
))
def test_projection_exact_131072_byte_budget_includes_tagging_escaping_and_utf8(unit, encoded, count, remainder, digest, monkeypatch):
    p = _protocol()
    text = unit * count + "x" * remainder
    item = (7,) + (None,) * 28 + (text, None)
    expected = PROJECTION_BUDGET_PREFIX + encoded * count + b"x" * remainder + PROJECTION_BUDGET_SUFFIX
    assert len(expected) == 131072
    _assert_projection(p, _stored(p, item=item, primary=None), expected, digest)
    with monkeypatch.context() as guard:
        _no_serialization(guard, p)
        response = p.encode_projection(_stored(p, item=_slot(item, 29, text + "x"), primary=None))
    _assert_native(p, response, "state_too_large")


@pytest.mark.parametrize("kind", ("bomb", "text_subclass", "bytes_subclass", "int_subclass", "float_subclass", "tuple_subclass"))
def test_bomb_tracker_detects_every_swallowed_operation(kind):
    """Observer self-control, not a synthetic implementation correctness result."""
    value = _bad(kind)
    marker = object()
    for operation in _BOMB_METHODS:
        def swallowing_refusal(argument):
            try:
                getattr(type(argument), operation)(argument)
            except Exception:
                pass
            return marker

        before = len(_BOMB_CALLS)
        # Without the observer the expected return is indistinguishable from a
        # safe rejection. Prove the forbidden operation actually was swallowed.
        assert swallowing_refusal(value) is marker
        expected_call = (type(value).__name__, operation)
        assert _BOMB_CALLS[before:] == [expected_call]
        before = len(_BOMB_CALLS)
        with pytest.raises(AssertionError, match="Forbidden bomb calls"):
            _ObservedCall(swallowing_refusal, "synthetic_refusal")(value)
        assert _BOMB_CALLS[before:] == [expected_call]


def test_observed_protocol_preserves_api_introspection_and_clean_forwarding():
    """A clean synthetic return must pass; original API metadata stays visible."""
    from types import SimpleNamespace

    def decode_wire(scope: "str") -> "str":
        return scope

    def encode_projection(values: "str") -> "str":
        return values

    def make_reply(wire: "str", outcome: "str") -> "str":
        return outcome

    record = object()
    module = SimpleNamespace(decode_wire=decode_wire, encode_projection=encode_projection,
                             make_reply=make_reply, ActorTag=record)
    p = _ObservedProtocol(module)
    before = len(_BOMB_CALLS)
    for name in ("decode_wire", "encode_projection", "make_reply"):
        original, observed = getattr(module, name), getattr(p, name)
        assert inspect.signature(observed) == inspect.signature(original)
        assert typing.get_type_hints(observed) == typing.get_type_hints(original)
        assert inspect.unwrap(observed) is original
    assert p.ActorTag is record
    assert p.decode_wire(record) is record
    assert p.encode_projection(record) is record
    assert p.make_reply(record, record) is record
    assert _BOMB_CALLS[before:] == []


@pytest.mark.parametrize("identifier", (0, -1, -9223372036854775808),
                         ids=("zero_without_primary", "negative_one_without_primary", "signed64_min_without_primary"))
def test_nonpositive_item_identity_refuses_without_primary_mismatch_mask(identifier):
    p = _protocol()
    values = _stored(p, item=_slot(ITEM, 0, identifier), primary=None)
    assert values.primary is None
    response = p.encode_projection(values)
    assert type(response) is p.NeedsNative
    assert response.reason == "invalid_state"


@pytest.mark.parametrize("raw_path,expected_item_id,expected_reply", (
    (
        b"/item/%37/edit", None,
        b'{"protocol":"shelf.me2","version":2,"operation":"form",'
        b'"generation":"0123456789abcdef0123456789abcdef",'
        b'"request":"fedcba9876543210fedcba9876543210","item_id":null,'
        b'"actor":{"id":"5","token_version":"1"},'
        b'"result":"refused","outcome":"not_saved",'
        b'"payload":{"code":"invalid_protocol","field":null,"blocking_item_id":null}}',
    ),
    (
        b"/item/8/edit", "8",
        b'{"protocol":"shelf.me2","version":2,"operation":"form",'
        b'"generation":"0123456789abcdef0123456789abcdef",'
        b'"request":"fedcba9876543210fedcba9876543210","item_id":"8",'
        b'"actor":{"id":"5","token_version":"1"},'
        b'"result":"refused","outcome":"not_saved",'
        b'"payload":{"code":"invalid_protocol","field":null,"blocking_item_id":null}}',
    ),
), ids=("encoded_raw_id_has_no_echo", "different_raw_id_echoes_raw_id"))
def test_raw_path_alone_supplies_malformed_item_echo_and_reply(raw_path, expected_item_id, expected_reply):
    p = _protocol()
    wire = p.decode_wire(_scope(p, path="/item/7/edit", raw_path=raw_path))
    _assert_malformed(p, wire)
    if expected_item_id is None:
        assert wire.echo.item_id is None
    else:
        assert type(wire.echo.item_id) is str and wire.echo.item_id == expected_item_id
    response = p.make_reply(wire, p.RefusedData(code="invalid_protocol", field=None, blocking_item_id=None))
    assert type(response) is p.ReplySpec and response.status == 400
    assert type(response.body) is bytes and response.body == expected_reply
