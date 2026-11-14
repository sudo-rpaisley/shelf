"""Pure recent-row extension of the existing F2 *whole* preference document.

The JSON field is ``recent: {version: 1, mode: mixed|family|format|custom,
groups: [...]}``. Non-custom modes have an empty groups list; their preset rows
belong to the presentation consumer. Custom ``groups: []`` deliberately means
zero custom rows, not mixed. Each custom row has a nonblank, at-most-80-character
raw user ``name``, a ``selectors`` list, and a server-issued ``key``. Names are
neither translated nor HTML-escaped here: the eventual template must escape them.

Limits: 12 groups, 32 selectors per group (before deduplication), 32 KiB of UTF-8
JSON for recent, 64 KiB for the complete document, and 12 nested JSON containers.
Raw JSON additionally counts whitespace against the body limit and rejects
duplicate object keys. Mapping inputs must contain only actual JSON types.
These are in-memory bounds, not an HTTP streaming/body-limit implementation.

``normalize_preferences`` is a detached display projection, never a repair write.
It reports absent/corrupt/unsupported_version and shows mixed on fallback, but
honors an explicit F2 shelves list without ``recent`` (including ``shelves: []``).
Never feed that fallback projection to an old writer as the stored document.

``merge_preferences`` overlays already-F2-validated changes on the ORIGINAL stored
document. It validates an explicitly submitted recent strictly. Omission preserves
the old recent value without normalizing it, including unknown versions/fields and
corrupt-but-JSON values. Unrelated fields are opaque, not re-defaulted or filtered;
explicit false/0/empty/null values survive. JSON string contents and JSON semantics
are retained, not the whitespace/escape spelling of a raw serialized document.

New row keys must be OMITTED, not supplied by a client. Issuance is deterministic:
``recent:`` plus 32 lowercase SHA-256 hex digits over the public effective selector
set and a collision counter. No account/item/source/name/activity enters the hash.
Issuance tries at most len(reserved keys) + 1 counters (at most 24 here), then
refuses rather than retrying indefinitely if digests keep colliding.
An edited/reordered row retains its key only by membership in the trusted existing
recent document. Keys are presentation IDs, NEVER permission or ownership tokens.

There is no database/actor/revision API here. The eventual EXISTING-F2 adapter must
resolve the authenticated account independently, reread its ORIGINAL whole document
and recheck the expected revision under its writer lock, then merge and save that
document atomically. A supplied dict, key membership, or successful validation is
not evidence of database authority or a CAS implementation. Saved-view/shelf-key
ownership and non-recent field validation still belong to that adapter.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass

from app import media_presentation


MAX_GROUPS = 12
MAX_SELECTORS = 32
MAX_NAME_CHARS = 80
MAX_RECENT_BYTES = 32_768
MAX_PREFERENCES_BYTES = 65_536
MAX_JSON_DEPTH = 12
_MODES = frozenset({"mixed", "family", "format", "custom"})
_KEY = re.compile(r"recent:[0-9a-f]{32}\Z")


class PreferenceValidationError(ValueError):
    """Stable diagnostic code, not a translated or user-data-bearing UI message."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RecentGroup:
    key: str
    name: str
    selectors: tuple[str, ...]
    media_types: tuple[str, ...]

    @property
    def mixed(self) -> bool:
        """No media filter: includes unknown/legacy types, not just known formats."""
        return not self.selectors


@dataclass(frozen=True)
class NormalizedPreferences:
    values: dict
    fallback_reason: str | None
    recent_hidden: bool
    # Custom-row expansions only; family/format presets are presentation-owned.
    groups: tuple[RecentGroup, ...] = ()


def _mixed() -> dict:
    return {"version": 1, "mode": "mixed", "groups": []}


def _raw_depth(text: str) -> None:
    """Bound parser nesting before json.loads, ignoring quoted/escaped brackets."""
    depth = 0
    quoted = escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise PreferenceValidationError("json_too_deep")
        elif character in "]}":
            depth -= 1


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreferenceValidationError("duplicate_key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise PreferenceValidationError("invalid_json")


def _bounded_copy(value, maximum: int):
    """Reject non-JSON/cyclic/deep/oversize trees before making a detached copy."""
    remaining = maximum

    def visit(node, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise PreferenceValidationError("payload_too_large")
        kind = type(node)
        if kind is str:
            if len(node) > maximum:
                raise PreferenceValidationError("payload_too_large")
        elif kind is float:
            if not math.isfinite(node):
                raise PreferenceValidationError("invalid_json")
        elif kind in (dict, list):
            if depth >= MAX_JSON_DEPTH:
                raise PreferenceValidationError("json_too_deep")
            if len(node) > maximum:
                raise PreferenceValidationError("payload_too_large")
            if kind is dict:
                for key, child in node.items():
                    if type(key) is not str:
                        raise PreferenceValidationError("invalid_json")
                    visit(key, depth + 1)
                    visit(child, depth + 1)
            else:
                for child in node:
                    visit(child, depth + 1)
        elif kind not in (int, bool, type(None)):
            raise PreferenceValidationError("invalid_json")

    visit(value)
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    try:
        # Unlike dumps, this stops before allocating an arbitrarily large result.
        for chunk in encoder.iterencode(value):
            size += len(chunk.encode("utf-8"))
            if size > maximum:
                raise PreferenceValidationError("payload_too_large")
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, PreferenceValidationError):
            raise
        raise PreferenceValidationError("invalid_json") from exc
    return copy.deepcopy(value)


def _document(value, *, absent=False) -> dict:
    if value is None and absent:
        return {}
    if type(value) in (str, bytes):
        if len(value) > MAX_PREFERENCES_BYTES:
            raise PreferenceValidationError("payload_too_large")
        try:
            text = value.decode("utf-8") if type(value) is bytes else value
            if len(text.encode("utf-8")) > MAX_PREFERENCES_BYTES:
                raise PreferenceValidationError("payload_too_large")
            _raw_depth(text)
            value = json.loads(text, object_pairs_hook=_unique_object,
                               parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, PreferenceValidationError):
                raise
            raise PreferenceValidationError("invalid_json") from exc
    if type(value) is not dict:
        raise PreferenceValidationError("invalid_document")
    return _bounded_copy(value, MAX_PREFERENCES_BYTES)


def _hidden(document) -> bool:
    if type(document) is not dict:
        return False
    shelves = document.get("shelves")
    # Do not reinterpret absent/false/null as the explicitly empty shelves list.
    return (type(shelves) is list and len(shelves) <= MAX_GROUPS
            and all(type(key) is str for key in shelves) and "recent" not in shelves)


def _selectors(value) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if (type(value) is not list or len(value) > MAX_SELECTORS
            or any(type(selector) is not str for selector in value)):
        raise PreferenceValidationError("invalid_selectors")
    if "all" in value and value != ["all"]:
        raise PreferenceValidationError("invalid_selectors")
    try:
        # The sole production taxonomy/expansion authority, with no SQL callback.
        expanded = media_presentation.expand_selectors(tuple(value))
    except (TypeError, ValueError) as exc:
        raise PreferenceValidationError("invalid_selectors") from exc
    selectors = () if not value or value == ["all"] else tuple(dict.fromkeys(value))
    return selectors, expanded


def _issue_key(effective: tuple, reserved: set[str]) -> str:
    for counter in range(len(reserved) + 1):
        public = json.dumps([effective, counter], separators=(",", ":")).encode("ascii")
        key = "recent:" + hashlib.sha256(public).hexdigest()[:32]
        if key not in reserved:
            return key
    raise PreferenceValidationError("group_key_collision")


def _recent(value, *, existing=None, saving=False) -> tuple[dict, tuple[RecentGroup, ...]]:
    value = _bounded_copy(value, MAX_RECENT_BYTES)
    if type(value) is not dict:
        raise PreferenceValidationError("invalid_recent")
    if type(value.get("version")) is not int:
        raise PreferenceValidationError("invalid_version")
    if value["version"] != 1:
        raise PreferenceValidationError("unsupported_version")
    if type(value.get("mode")) is not str or value["mode"] not in _MODES:
        raise PreferenceValidationError("invalid_mode")
    rows = value.get("groups")
    if type(rows) is not list or len(rows) > MAX_GROUPS:
        raise PreferenceValidationError("invalid_groups")
    if value["mode"] != "custom" and rows:
        raise PreferenceValidationError("unexpected_groups")

    previous_groups = {}
    if saving and type(existing) is dict:
        try:
            previous, _ = _recent(existing)
            previous_groups = {row["key"]: row for row in previous["groups"]}
        except PreferenceValidationError:
            # A corrupt/unknown-version document proves no retained row keys.
            pass

    prepared = []
    effective_groups, submitted_keys = set(), set()
    for row in rows:
        if type(row) is not dict:
            raise PreferenceValidationError("invalid_group")
        name = row.get("name")
        if (type(name) is not str or not name.strip() or len(name) > MAX_NAME_CHARS
                or any(unicodedata.category(c) == "Cc" for c in name)):
            raise PreferenceValidationError("invalid_name")
        key = row.get("key")
        if "key" in row:
            if type(key) is not str or _KEY.fullmatch(key) is None:
                raise PreferenceValidationError("invalid_group_key")
            if saving and key not in previous_groups:
                raise PreferenceValidationError("unknown_group_key")
            if key in submitted_keys:
                raise PreferenceValidationError("duplicate_group_key")
            submitted_keys.add(key)
        elif not saving:
            raise PreferenceValidationError("missing_group_key")
        selectors, media_types = _selectors(row.get("selectors"))
        # Mixed includes unknown legacy types; the union of all known formats
        # does not, so those are intentionally different effective selections.
        effective = ("filtered", media_types) if selectors else ("mixed", ())
        if effective in effective_groups:
            raise PreferenceValidationError("duplicate_group")
        effective_groups.add(effective)
        prepared.append((row, key, selectors, media_types, effective))

    reserved = set(previous_groups) | submitted_keys
    normalized_rows, groups = [], []
    for row, key, selectors, media_types, effective in prepared:
        if key is None:
            key = _issue_key(effective, reserved)
            reserved.add(key)
        normalized = copy.deepcopy(previous_groups.get(key, {}))
        normalized.update(row)
        normalized.update(key=key, selectors=list(selectors))
        normalized_rows.append(normalized)
        groups.append(RecentGroup(key, row["name"], selectors, media_types))
    result = copy.deepcopy(existing) if saving and type(existing) is dict else {}
    result.update(value)
    result["groups"] = normalized_rows
    return _bounded_copy(result, MAX_RECENT_BYTES), tuple(groups)


def normalize_preferences(document=None) -> NormalizedPreferences:
    """Read-only, detached projection and an explicit fallback diagnostic.

    ``None`` means no stored whole document. Present false/0/empty recent values
    are corruption, not absence. Unparseable/oversize raw JSON cannot establish
    an explicit shelf hide; callers should retain the original for later writes.
    """
    hidden = _hidden(document)
    try:
        values = _document(document, absent=True)
    except PreferenceValidationError:
        return NormalizedPreferences({"recent": _mixed()}, "corrupt", hidden)
    hidden = _hidden(values)
    if "recent" not in values:
        return NormalizedPreferences({**values, "recent": _mixed()}, "absent", hidden)
    try:
        normalized, groups = _recent(values["recent"])
    except PreferenceValidationError as exc:
        reason = "unsupported_version" if exc.code == "unsupported_version" else "corrupt"
        return NormalizedPreferences({**values, "recent": _mixed()}, reason, hidden)
    return NormalizedPreferences({**values, "recent": normalized}, None, hidden, groups)


def merge_preferences(existing, changes) -> dict:
    """Pure candidate merge, NEVER authorization, persistence or revision checking.

    Non-recent changes must already have passed the existing F2 writer's field
    and ownership checks. Supply the original stored document, not a load fallback
    or a client-selected account document. Invalid existing JSON refuses the save
    instead of silently discarding fields it cannot preserve.
    """
    previous = _document(existing, absent=True)
    updates = _document(changes)
    result = {**previous, **updates}
    if "recent" in updates:
        result["recent"], _ = _recent(updates["recent"], existing=previous.get("recent"), saving=True)
    return _bounded_copy(result, MAX_PREFERENCES_BYTES)
