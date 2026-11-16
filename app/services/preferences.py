"""Unwired, account-owned preferences on the caller's SQLite connection.

Actors are server-captured data, not authentication by construction. The caller
must verify the entry session and capture its user and expiry before waiting.
Reads require an explicit transaction; mutations require the caller to acquire
BEGIN IMMEDIATE before any protected reads. ``in_transaction`` can reject a
missing transaction, but cannot prove that writer-lock precondition.

Only supported immutable projections leave this module. Mutations reread and
merge the original whole document, never a display fallback. Returned revisions
belong to the caller's still-uncommitted transaction; the caller owns commit and
must roll back failures. No connection or transaction lifecycle is managed here.
Ordinary SQLite errors propagate. Account rechecks create no restore epoch or
general cross-restore fencing guarantee.
"""

import json as _json
import math as _math
import re as _re
import time as _time
from dataclasses import dataclass as _dataclass

from app.services import recent_preferences as _recent_preferences


_MAX_INTEGER = 9_223_372_036_854_775_807
_MAX_EXPIRY = 253_402_300_799
_MAX_STORAGE_BYTES = 131_072
_FIELDS = frozenset({"recent", "item_size_percent", "default_saved_view"})
_SIZES = frozenset({50, 60, 70, 80, 90, 100})
_ROLES = frozenset({"viewer", "editor", "admin"})
_VIEW_ID = _re.compile(r"[1-9][0-9]{0,18}\Z", flags=_re.ASCII)
_ERRORS = frozenset({
    "transaction_required", "invalid_actor", "actor_changed",
    "invalid_expected_revision", "invalid_changes", "invalid_reset_fields",
    "invalid_recent", "invalid_item_size_percent", "invalid_default_saved_view",
    "default_saved_view_unavailable", "stored_revision_corrupt",
    "stored_document_corrupt", "revision_conflict", "revision_exhausted",
    "payload_too_large", "json_too_deep", "group_key_collision",
    "cas_invariant_failed",
})
_RECENT_DIAGNOSTICS = {
    "absent": "recent_absent",
    "corrupt": "recent_corrupt",
    "unsupported_version": "recent_unsupported_version",
}
_MERGE_LIMIT_ERRORS = frozenset({
    "payload_too_large", "json_too_deep", "group_key_collision",
})


class PreferenceStoreError(ValueError):
    """A closed, fixed code/message, without SQL or user-supplied content."""

    def __init__(self, code: str):
        if type(code) is not str or code not in _ERRORS:
            raise ValueError("invalid_preference_store_error_code")
        self.code = code
        super().__init__(code)


@_dataclass(frozen=True)
class PreferenceActor:
    id: int
    username: str
    role: str
    token_version: int
    expires_at: int | float


@_dataclass(frozen=True)
class PreferenceGroup:
    key: str
    name: str
    selectors: tuple[str, ...]


@_dataclass(frozen=True)
class PreferenceRecent:
    version: int
    mode: str
    groups: tuple[PreferenceGroup, ...]


@_dataclass(frozen=True)
class PreferenceSnapshot:
    revision: int | None
    recent: PreferenceRecent
    recent_hidden: bool
    item_size_percent: int
    default_saved_view: str
    diagnostics: tuple[str, ...]


@_dataclass(frozen=True)
class _StoredPreferences:
    present: bool
    revision: int | None
    document: dict | None


def _require_transaction(db) -> None:
    if not db.in_transaction:
        raise PreferenceStoreError("transaction_required")


def _positive_integer(value) -> bool:
    return type(value) is int and 1 <= value <= _MAX_INTEGER


def _validate_actor(actor) -> None:
    if (type(actor) is not PreferenceActor
            or not _positive_integer(actor.id)
            or type(actor.username) is not str or not actor.username
            or type(actor.role) is not str or actor.role not in _ROLES
            or not _positive_integer(actor.token_version)
            or type(actor.expires_at) not in (int, float)
            or not 0 < actor.expires_at <= _MAX_EXPIRY
            or not _math.isfinite(actor.expires_at)):
        raise PreferenceStoreError("invalid_actor")


def _check_actor(db, actor: PreferenceActor) -> None:
    row = db.execute(
        "SELECT id, username, role, token_version FROM users WHERE id = ?",
        (actor.id,),
    ).fetchone()
    if (row is None
            or type(row[0]) is not int or row[0] != actor.id
            or type(row[1]) is not str or row[1] != actor.username
            or type(row[2]) is not str or row[2] != actor.role
            or type(row[3]) is not int or row[3] != actor.token_version
            or actor.expires_at <= _time.time()):
        raise PreferenceStoreError("actor_changed")


def _read_original(db, actor: PreferenceActor) -> _StoredPreferences:
    metadata = db.execute(
        "SELECT CASE WHEN typeof(revision) = 'integer' THEN revision ELSE NULL END, "
        "typeof(values_json), length(CAST(values_json AS BLOB)) "
        "FROM library_preferences WHERE user_id = ?",
        (actor.id,),
    ).fetchone()
    if metadata is None:
        return _StoredPreferences(False, 0, {})
    revision = metadata[0] if _positive_integer(metadata[0]) else None
    if (metadata[1] != "text" or type(metadata[2]) is not int
            or not 0 <= metadata[2] <= _MAX_STORAGE_BYTES):
        # SQL NULL is corrupt presence, not the helper's None/absence sentinel.
        return _StoredPreferences(True, revision, None)

    # Fetch bounded storage bytes so malformed TEXT does not fail inside sqlite3's
    # implicit text decoder. Strictly decode the database's UTF-8/UTF-16 encoding;
    # the public helper then enforces the actual UTF-8 document/depth limits.
    row = db.execute(
        "SELECT CAST(values_json AS BLOB) FROM library_preferences WHERE user_id = ?",
        (actor.id,),
    ).fetchone()
    if row is None or type(row[0]) is not bytes:
        return _StoredPreferences(True, revision, None)
    encoding = db.execute("PRAGMA main.encoding").fetchone()[0]
    try:
        document = _recent_preferences.merge_preferences(row[0].decode(encoding), {})
    except (UnicodeError, _recent_preferences.PreferenceValidationError):
        return _StoredPreferences(True, revision, None)
    return _StoredPreferences(True, revision, document)


def _valid_size(value) -> bool:
    return type(value) is int and value in _SIZES


def _valid_view(value) -> bool:
    return (type(value) is str
            and (value == "" or (_VIEW_ID.fullmatch(value) is not None
                                and int(value) <= _MAX_INTEGER)))


def _view_available(db, actor: PreferenceActor, value: str) -> bool:
    return db.execute(
        "SELECT 1 FROM library_saved_views WHERE id = ? AND user_id = ?",
        (int(value), actor.id),
    ).fetchone() is not None


def _default_snapshot(revision, diagnostics) -> PreferenceSnapshot:
    return PreferenceSnapshot(
        revision, PreferenceRecent(1, "mixed", ()), False, 100, "", tuple(diagnostics),
    )


def load_preferences(db, actor: PreferenceActor) -> PreferenceSnapshot:
    """Project supported fields without repairing or inserting any stored data."""
    _require_transaction(db)
    _validate_actor(actor)
    _check_actor(db, actor)
    stored = _read_original(db, actor)
    if not stored.present:
        return _default_snapshot(0, ("preferences_absent",))
    diagnostics = []
    if stored.revision is None:
        diagnostics.append("stored_revision_corrupt")
    if stored.document is None:
        diagnostics.append("stored_document_corrupt")
        return _default_snapshot(stored.revision, diagnostics)

    normalized = _recent_preferences.normalize_preferences(stored.document)
    if normalized.fallback_reason is not None:
        diagnostics.append(_RECENT_DIAGNOSTICS[normalized.fallback_reason])
    recent = normalized.values["recent"]
    projection = PreferenceRecent(
        recent["version"], recent["mode"],
        tuple(PreferenceGroup(group.key, group.name, group.selectors)
              for group in normalized.groups),
    )
    size = stored.document.get("item_size_percent", 100)
    if not _valid_size(size):
        size = 100
        diagnostics.append("item_size_corrupt")
    view = stored.document.get("default_saved_view", "")
    if not _valid_view(view):
        view = ""
        diagnostics.append("default_saved_view_corrupt")
    elif view and not _view_available(db, actor, view):
        view = ""
        diagnostics.append("default_saved_view_unavailable")
    return PreferenceSnapshot(
        stored.revision, projection, normalized.recent_hidden, size, view,
        tuple(diagnostics),
    )


def _validate_write_arguments(db, actor, expected_revision) -> None:
    _require_transaction(db)
    _validate_actor(actor)
    if type(expected_revision) is not int or not 0 <= expected_revision <= _MAX_INTEGER:
        raise PreferenceStoreError("invalid_expected_revision")


def _validate_changes(changes) -> None:
    if (type(changes) is not dict or len(changes) > len(_FIELDS)
            or any(type(key) is not str or key not in _FIELDS for key in changes)):
        raise PreferenceStoreError("invalid_changes")


def _validate_reset_fields(fields) -> None:
    if (type(fields) is not tuple or not 1 <= len(fields) <= len(_FIELDS)
            or any(type(field) is not str or field not in _FIELDS for field in fields)
            or len(set(fields)) != len(fields)):
        raise PreferenceStoreError("invalid_reset_fields")


def _validate_recent_shape(value) -> None:
    # Extension data is preserved only from the original, never accepted as a
    # client-writable extension point. All remaining policy belongs to the helper.
    if (type(value) is not dict or len(value) != 3
            or any(type(key) is not str for key in value)
            or set(value) != {"version", "mode", "groups"}):
        raise PreferenceStoreError("invalid_recent")
    groups = value["groups"]
    if type(groups) is not list or len(groups) > _recent_preferences.MAX_GROUPS:
        raise PreferenceStoreError("invalid_recent")
    for group in groups:
        if (type(group) is not dict or not 2 <= len(group) <= 3
                or any(type(key) is not str for key in group)
                or not {"name", "selectors"} <= group.keys()
                or not group.keys() <= {"key", "name", "selectors"}):
            raise PreferenceStoreError("invalid_recent")


def _next_revision(stored: _StoredPreferences, expected_revision: int) -> int:
    if stored.revision is None:
        raise PreferenceStoreError("stored_revision_corrupt")
    if stored.document is None:
        raise PreferenceStoreError("stored_document_corrupt")
    if stored.revision != expected_revision:
        raise PreferenceStoreError("revision_conflict")
    if stored.revision == _MAX_INTEGER:
        raise PreferenceStoreError("revision_exhausted")
    return stored.revision + 1


def _mutate(db, actor: PreferenceActor, expected_revision: int, changes: dict) -> int:
    _check_actor(db, actor)
    stored = _read_original(db, actor)
    revision = _next_revision(stored, expected_revision)
    if "recent" in changes:
        _validate_recent_shape(changes["recent"])
    if "item_size_percent" in changes and not _valid_size(changes["item_size_percent"]):
        raise PreferenceStoreError("invalid_item_size_percent")
    if "default_saved_view" in changes:
        view = changes["default_saved_view"]
        if not _valid_view(view):
            raise PreferenceStoreError("invalid_default_saved_view")
        if view and not _view_available(db, actor, view):
            raise PreferenceStoreError("default_saved_view_unavailable")
    try:
        candidate = _recent_preferences.merge_preferences(stored.document, changes)
    except _recent_preferences.PreferenceValidationError as exc:
        code = exc.code if exc.code in _MERGE_LIMIT_ERRORS else "invalid_recent"
        raise PreferenceStoreError(code) from None
    serialized = _json.dumps(candidate, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":"))
    if len(serialized.encode("utf-8")) > _recent_preferences.MAX_PREFERENCES_BYTES:
        raise PreferenceStoreError("payload_too_large")

    if stored.present:
        statement = (
            "UPDATE library_preferences "
            "SET values_json = ?, revision = ?, updated_at = datetime('now') "
            "WHERE user_id = ? AND revision = ?"
        )
        parameters = (serialized, revision, actor.id, expected_revision)
    else:
        statement = (
            "INSERT INTO library_preferences (user_id, values_json, revision) VALUES (?, ?, 1)"
        )
        parameters = (actor.id, serialized)
    _check_actor(db, actor)
    if db.execute(statement, parameters).rowcount != 1:
        raise PreferenceStoreError("cas_invariant_failed")
    return revision


def save_preferences(db, actor: PreferenceActor, expected_revision: int, changes: dict) -> int:
    """CAS-merge submitted fields; even an empty accepted save increments once."""
    _validate_write_arguments(db, actor, expected_revision)
    _validate_changes(changes)
    return _mutate(db, actor, expected_revision, changes)


def reset_preferences(db, actor: PreferenceActor, expected_revision: int,
                      fields: tuple[str, ...]) -> int:
    """CAS-reset selected fields, preserving opaque data and independent shelf hides."""
    _validate_write_arguments(db, actor, expected_revision)
    _validate_reset_fields(fields)
    changes = {}
    for field in fields:
        if field == "recent":
            changes[field] = {"version": 1, "mode": "mixed", "groups": []}
        elif field == "item_size_percent":
            changes[field] = 100
        else:
            changes[field] = ""
    return _mutate(db, actor, expected_revision, changes)
