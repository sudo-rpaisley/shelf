"""Independent literal PREF-STORE-1 ABI/validation/preservation tests.

No app module is imported at collection. PreferenceActor instances here are
server-capture-shaped TEST DATA, not cookies, authentication, or authority proof.
The frozen public recent helper may be fault-injected, never used as an expected
value generator. The new domain/schema writers' implementation is not consulted.
"""

import copy
import dataclasses
import importlib
import inspect
import json
import logging
import re
import sqlite3
import sys
import time
import typing
from collections import UserDict
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests.test_account_preferences_schema_backup import _PREFERENCES_DDL, _USERS_DDL, _logical_state


_MAX = 9_223_372_036_854_775_807
_EXPIRY = 253_402_300_799
_KEY_A = "recent:11111111111111111111111111111111"
_KEY_B = "recent:22222222222222222222222222222222"
_UNSET = object()
_FORMATS = (
    "book", "audiobook", "ebook", "magazine", "dvd", "digital_video", "tv_series",
    "vinyl", "cassette", "cd", "digital_music", "comic", "manga", "video_game",
)
_DIAGNOSTICS = {
    "preferences_absent", "stored_revision_corrupt", "stored_document_corrupt",
    "recent_absent", "recent_corrupt", "recent_unsupported_version",
    "item_size_corrupt", "default_saved_view_corrupt", "default_saved_view_unavailable",
}
_ERRORS = {
    "transaction_required", "invalid_actor", "actor_changed", "invalid_expected_revision",
    "invalid_changes", "invalid_reset_fields", "invalid_recent", "invalid_item_size_percent",
    "invalid_default_saved_view", "default_saved_view_unavailable", "stored_revision_corrupt",
    "stored_document_corrupt", "revision_conflict", "revision_exhausted", "payload_too_large",
    "json_too_deep", "group_key_collision", "cas_invariant_failed",
}


class _Dict(dict):
    pass


class _List(list):
    pass


class _Str(str):
    pass


class _Int(int):
    pass


class _Tuple(tuple):
    pass


class _Float(float):
    pass


class _ObservedCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        self.connection.executions.append((sql, tuple(parameters), self.connection.in_transaction))
        hook = self.connection.on_execute
        if hook is not None:
            hook(self.connection, sql, parameters)
        return super().execute(sql, parameters)


class _ObservedConnection(sqlite3.Connection):
    """Native execution with observable calls, NOT swallowed-exception bombs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls, self.sql, self.executions, self.fetched_text_sizes = [], [], [], []
        self.on_execute = None
        self.set_trace_callback(self.sql.append)

        def row(cursor, values):
            for description, value in zip(cursor.description, values):
                if type(value) is str:
                    self.fetched_text_sizes.append((description[0], len(value.encode("utf-8"))))
                elif type(value) is bytes:
                    self.fetched_text_sizes.append((description[0], len(value)))
            return sqlite3.Row(cursor, values)

        self.row_factory = row

    def cursor(self, factory=_ObservedCursor):
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executescript(self, sql):
        self.calls.append("executescript")
        return super().executescript(sql)

    def commit(self):
        self.calls.append("commit")
        return super().commit()

    def rollback(self):
        self.calls.append("rollback")
        return super().rollback()

    def close(self):
        self.calls.append("close")
        return super().close()


def _clear_observations(conn):
    conn.calls.clear()
    conn.sql.clear()
    conn.executions.clear()
    conn.fetched_text_sizes.clear()


def _assert_no_lifecycle(conn, external=()):
    assert conn.calls == [], f"store managed caller connection: {conn.calls!r}"
    assert list(external) == [], f"store called external lifecycle/logging: {external!r}"
    lifecycle = re.compile(r"^\s*(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.I)
    assert not [sql for sql in conn.sql if lifecycle.match(sql)]
    assert not [sql for sql, _parameters, _active in conn.executions if lifecycle.match(sql)], (
        "store attempted transaction SQL, including an untraced prepare-time failure"
    )
    assert conn.in_transaction, "the caller's transaction must still be open"


def _store_statement_allowed(sql, parameters=()):
    """Keep the three existing kinds; admit only the fixed readonly encoding PRAGMA."""
    if re.match(r"\s*(?:SELECT|INSERT|UPDATE)\b", sql, re.I):
        return True
    main = r'''(?:main|"main"|'main'|\[main\]|`main`)'''
    encoding = r'''(?:encoding|"encoding"|'encoding'|\[encoding\]|`encoding`)'''
    return type(parameters) is tuple and parameters == () and re.fullmatch(
        rf"\s*PRAGMA\b\s*(?:{main}\s*\.\s*)?{encoding}\s*;?\s*", sql, re.I | re.ASCII,
    ) is not None


def _assert_store_statement_kinds(executions, traces):
    """Pure policy over BOTH attempted triples and successful SQL traces."""
    for sql, parameters, active in executions:
        assert active is True, "store statement attempted outside the caller transaction"
        assert _store_statement_allowed(sql, parameters), "forbidden attempted store statement"
    for sql in traces:
        assert _store_statement_allowed(sql), "forbidden traced store statement"


def _preference_read_queries(executions):
    table = r'(?:library_preferences|"library_preferences"|\[library_preferences\]|`library_preferences`)'
    main = r'(?:main|"main"|\[main\]|`main`)'
    return [sql for sql, _parameters, _active in executions if re.search(
        rf"\bFROM\s+(?:{main}\s*\.\s*)?{table}(?!\w)", sql, re.I,
    )]


def _assert_metadata_only_preference_reads(queries):
    """Recognize the bounded metadata expressions, not one alias spelling.

    This is a small oracle for this fixed table's reads, not a general SQL parser.
    A qualifier (or quoted identifier) does not turn metadata into a raw fetch.
    """
    identifier = r'(?:[A-Za-z_]\w*|"(?:[^"]|"")+"|\[[^\]]+\]|`(?:[^`]|``)+`)'
    column = r'(?:values_json|"values_json"|\[values_json\]|`values_json`)'
    reference = rf"(?:{identifier}\s*\.\s*){{0,2}}{column}(?!\w)"
    kind = rf"\btypeof\s*\(\s*{reference}\s*\)"
    length = rf"\blength\s*\(\s*CAST\s*\(\s*{reference}\s+AS\s+BLOB\s*\)\s*\)"
    assert queries
    assert any(re.search(kind, sql, re.I) and re.search(length, sql, re.I) for sql in queries), (
        "bounded type and physical length metadata must be read"
    )
    for sql in queries:
        assert not re.search(r"\b(?:substr|substring)\s*\(", sql, re.I), "no prefix parsing/truncation"
        projection = re.split(r"\bFROM\b", sql, maxsplit=1, flags=re.I)[0]
        assert not re.search(rf"(?:\bSELECT|,)\s*(?:{identifier}\s*\.\s*)?\*", projection, re.I), (
            "full values must not be selected through a wildcard"
        )
        without_metadata = re.sub(kind, "", sql, flags=re.I)
        without_metadata = re.sub(length, "", without_metadata, flags=re.I)
        assert not re.search(r"\bvalues_json\b", without_metadata, re.I), "huge full text must not be selected"


def _assert_metadata_first(executions):
    queries = _preference_read_queries(executions)
    assert queries, "a bounded original read must occur on the caller connection"
    _assert_metadata_only_preference_reads(queries[:1])


def _late_recent_merge_hook(conn, original_merge, submitted_recent, change, now, phases):
    """Change the actor ONLY after a successful nonempty recent validation."""
    def merge_after_validation(existing, changes):
        merged = original_merge(existing, changes)
        if changes == {}:
            phases.append("original_read_validated")
        elif "recent" in changes and "submitted_recent_validated" not in phases:
            assert type(changes) is dict and changes
            assert changes["recent"] == submitted_recent
            phases.append("submitted_recent_validated")
            if change == "expiry":
                now[0] = 2_000_000_001
            elif change == "delete":
                conn.execute("DELETE FROM users WHERE id=1")
            else:
                value = {"username": "changed late", "role": "admin", "token_version": 2}[change]
                conn.execute(f"UPDATE users SET {change}=? WHERE id=1", (value,))
            phases.append("actor_changed_after_submitted_recent")
        return merged

    return merge_after_validation


def _assert_late_recent_refusal(conn, phases, error):
    assert "original_read_validated" in phases
    assert "submitted_recent_validated" in phases, "nonempty submitted recent phase was not reached"
    assert "actor_changed_after_submitted_recent" in phases
    assert phases.index("original_read_validated") < phases.index("submitted_recent_validated")
    assert phases.index("submitted_recent_validated") < phases.index("actor_changed_after_submitted_recent")
    assert phases.count("actor_changed_after_submitted_recent") == 1
    assert error is not None, "late actor change was not refused after submitted recent validation"
    _assert_error(error, "actor_changed")
    preference_dml = re.compile(r"\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+library_preferences\b", re.I)
    assert not [sql for sql, _parameters, _active in conn.executions if preference_dml.match(sql)]
    assert not [sql for sql in conn.sql if preference_dml.match(sql)]
    _assert_no_lifecycle(conn)


def _make_database(path, *, permissive=False, encoding="UTF-8"):
    conn = sqlite3.connect(str(path), factory=_ObservedConnection)
    conn.execute(f"PRAGMA encoding='{encoding}'")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_USERS_DDL + """
        CREATE TABLE library_saved_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL COLLATE NOCASE,
            filters_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, name)
        );
    """)
    if permissive:
        # Deliberately damaged/imported-table harness, never an accepted schema
        # or a historical-upgrade fixture. It reaches defensive SQL NULL/types.
        conn.execute("CREATE TABLE library_preferences "
                     "(user_id INTEGER PRIMARY KEY, values_json, revision, updated_at TEXT)")
    else:
        conn.execute(_PREFERENCES_DDL)
    conn.execute("INSERT INTO users (id, username, password, role) "
                 "VALUES (1, 'Account A', 'test-data-not-auth', 'viewer')")
    conn.execute("INSERT INTO users (id, username, password, role) "
                 "VALUES (2, 'Account B', 'test-data-not-auth', 'admin')")
    conn.commit()
    _clear_observations(conn)
    return conn


def _actor(store, **updates):
    return store.PreferenceActor(**{
        "id": 1, "username": "Account A", "role": "viewer",
        "token_version": 1, "expires_at": _EXPIRY, **updates,
    })


def _recent(mode="mixed", groups=_UNSET, **extra):
    return {"version": 1, "mode": mode, "groups": [] if groups is _UNSET else groups, **extra}


def _group(name="My raw row", selectors=_UNSET, **extra):
    return {"name": name, "selectors": [] if selectors is _UNSET else selectors, **extra}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _seed(conn, document, revision=1, uid=1):
    raw = document if type(document) in (str, bytes) or document is None else _json(document)
    conn.execute("INSERT INTO library_preferences (user_id, values_json, revision, updated_at) "
                 "VALUES (?, ?, ?, 'unchanged-timestamp')", (uid, raw, revision))
    conn.commit()


def _raw_row(conn, uid=1):
    row = conn.execute("SELECT user_id, values_json, revision, updated_at "
                       "FROM library_preferences WHERE user_id=?", (uid,)).fetchone()
    return None if row is None else tuple(row)


def _begin(conn, *, write=True):
    assert not conn.in_transaction
    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    _clear_observations(conn)


def _save(store, conn, expected, changes, actor=None):
    _begin(conn)
    try:
        result = store.save_preferences(conn, _actor(store) if actor is None else actor, expected, changes)
        _assert_no_lifecycle(conn)
        assert type(result) is int
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise


def _load(store, conn, actor=None):
    before, count = _raw_row(conn), conn.total_changes
    _begin(conn, write=False)
    try:
        value = store.load_preferences(conn, _actor(store) if actor is None else actor)
        _assert_no_lifecycle(conn)
        assert type(value) is store.PreferenceSnapshot
        assert type(value.recent) is store.PreferenceRecent
        assert all(type(group) is store.PreferenceGroup for group in value.recent.groups)
        assert conn.total_changes == count
        assert _raw_row(conn) == before
        return value
    finally:
        conn.rollback()


def _assert_error(error, code):
    assert code in _ERRORS
    assert error.code == code
    assert str(error) == code
    assert error.args == (code,)
    assert isinstance(error, ValueError)


def _refuse(store, conn, code, *, changes=None, fields=None, expected=1, actor=None):
    before, count = _raw_row(conn), conn.total_changes
    _begin(conn)
    try:
        with pytest.raises(store.PreferenceStoreError) as error:
            if fields is None:
                store.save_preferences(conn, _actor(store) if actor is None else actor, expected, changes)
            else:
                store.reset_preferences(conn, _actor(store) if actor is None else actor, expected, fields)
        _assert_no_lifecycle(conn)
        _assert_error(error.value, code)
        assert conn.total_changes == count
        assert _raw_row(conn) == before
    finally:
        conn.rollback()


def _assert_snapshot(snapshot, expected):
    assert dataclasses.is_dataclass(snapshot)
    assert tuple(field.name for field in dataclasses.fields(snapshot)) == (
        "revision", "recent", "recent_hidden", "item_size_percent", "default_saved_view", "diagnostics",
    )
    assert snapshot.__dataclass_params__.frozen
    assert snapshot.revision is None or type(snapshot.revision) is int
    assert type(snapshot.recent_hidden) is bool
    assert type(snapshot.item_size_percent) is int
    assert type(snapshot.default_saved_view) is str
    assert type(snapshot.diagnostics) is tuple
    assert all(type(code) is str and code in _DIAGNOSTICS for code in snapshot.diagnostics)
    recent = snapshot.recent
    assert tuple(field.name for field in dataclasses.fields(recent)) == ("version", "mode", "groups")
    assert recent.__dataclass_params__.frozen
    assert type(recent.version) is int and type(recent.mode) is str
    assert type(recent.groups) is tuple
    groups = []
    for group in recent.groups:
        assert tuple(field.name for field in dataclasses.fields(group)) == ("key", "name", "selectors")
        assert group.__dataclass_params__.frozen
        assert type(group.key) is type(group.name) is str
        assert type(group.selectors) is tuple and all(type(s) is str for s in group.selectors)
        groups.append((group.key, group.name, group.selectors))
    actual = (snapshot.revision, (recent.version, recent.mode, tuple(groups)), snapshot.recent_hidden,
              snapshot.item_size_percent, snapshot.default_saved_view, snapshot.diagnostics)
    assert actual == expected
    for record in (snapshot, recent, *recent.groups):
        assert not any(hasattr(record, name) for name in (
            "values", "values_json", "raw_document", "extras", "owner_id", "filters_json", "view_name",
        ))


@pytest.fixture
def store():
    # Base deliberately lacks this feature; import failure is absence RED, not
    # a demonstrated productive implementation bug or a reason to skip/xfail.
    return importlib.import_module("app.services.preferences")


@pytest.fixture
def unit_db(tmp_path):
    conn = _make_database(tmp_path / "domain-native.db")
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def damaged_db(tmp_path):
    conn = _make_database(tmp_path / "damaged-native.db", permissive=True)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_public_abi_records_fields_types_frozen_tuples_and_no_clock_parameter(store):
    expected = {
        "PreferenceActor": {"id": int, "username": str, "role": str, "token_version": int,
                            "expires_at": int | float},
        "PreferenceGroup": {"key": str, "name": str, "selectors": tuple[str, ...]},
        "PreferenceRecent": {"version": int, "mode": str, "groups": tuple[store.PreferenceGroup, ...]},
        "PreferenceSnapshot": {"revision": int | None, "recent": store.PreferenceRecent,
                               "recent_hidden": bool, "item_size_percent": int,
                               "default_saved_view": str, "diagnostics": tuple[str, ...]},
    }
    for name, annotations in expected.items():
        record = getattr(store, name)
        assert dataclasses.is_dataclass(record) and record.__dataclass_params__.frozen
        assert tuple(field.name for field in dataclasses.fields(record)) == tuple(annotations)
        assert typing.get_type_hints(record) == annotations
        assert all(parameter.default is inspect.Parameter.empty for parameter in
                   inspect.signature(record).parameters.values())
    for name, names in (
        ("load_preferences", ("db", "actor")),
        ("save_preferences", ("db", "actor", "expected_revision", "changes")),
        ("reset_preferences", ("db", "actor", "expected_revision", "fields")),
    ):
        signature = inspect.signature(getattr(store, name))
        assert tuple(signature.parameters) == names
        assert all(parameter.default is inspect.Parameter.empty
                   for parameter in signature.parameters.values())
    assert issubclass(store.PreferenceStoreError, ValueError)
    row = store.PreferenceGroup(_KEY_A, "raw", ("format:book",))
    recent = store.PreferenceRecent(1, "custom", (row,))
    snapshot = store.PreferenceSnapshot(1, recent, False, 100, "", ())
    _assert_snapshot(snapshot, (1, (1, "custom", ((_KEY_A, "raw", ("format:book",)),)), False, 100, "", ()))
    for record, attribute in ((row, "name"), (recent, "mode"), (snapshot, "revision"),
                              (_actor(store), "username")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(record, attribute, "changed")
    with pytest.raises(TypeError):
        snapshot.recent.groups[0] = row
    with pytest.raises(TypeError):
        row.selectors[0] = "all"


def test_absence_is_exclusive_revision_zero_defaults_and_never_inserts(store, unit_db):
    _assert_snapshot(_load(store, unit_db), (0, (1, "mixed", ()), False, 100, "", ("preferences_absent",)))
    assert _raw_row(unit_db) is None


def test_supported_only_projection_immutable_raw_names_and_literal_selector_order(store, unit_db):
    name = '  <row> & e\u0301 %(title)s  '
    original = {
        "recent": _recent("custom", [_group(name, ["format:vinyl", "family:books", "format:vinyl"],
                                           key=_KEY_A, secret_group="must not leak")], secret_recent=[0, False]),
        "item_size_percent": 60, "default_saved_view": "7", "shelves": [],
        "opaque": {"private": "must not leak", "values": [False, 0, "", None]},
    }
    unit_db.execute("INSERT INTO library_saved_views (id,user_id,name,filters_json) "
                    "VALUES (7,1,'private view name','not even executable JSON')")
    unit_db.commit()
    _seed(unit_db, original, revision=5)
    snapshot = _load(store, unit_db)
    _assert_snapshot(snapshot, (5, (1, "custom", ((_KEY_A, name, ("format:vinyl", "family:books")),)),
                                True, 60, "7", ()))
    assert "must not leak" not in repr(snapshot)
    assert "private view name" not in repr(snapshot)
    assert json.loads(_raw_row(unit_db)[1]) == original


def test_same_revision_accounts_load_and_mutate_only_captured_owner(store, unit_db):
    _seed(unit_db, {"recent": _recent("family"), "item_size_percent": 50, "opaque": "A"}, revision=4)
    _seed(unit_db, {"recent": _recent("format"), "item_size_percent": 70, "opaque": "B"}, revision=4, uid=2)
    actor_b = _actor(store, id=2, username="Account B", role="admin")
    before_a = _raw_row(unit_db)
    _assert_snapshot(_load(store, unit_db, actor_b), (4, (1, "format", ()), False, 70, "", ()))
    assert _save(store, unit_db, 4, {"item_size_percent": 90}, actor_b) == 5
    assert _raw_row(unit_db) == before_a
    assert json.loads(_raw_row(unit_db, 2)[1]) == {
        "recent": _recent("format"), "item_size_percent": 90, "opaque": "B",
    }


def test_all_original_reads_use_the_supplied_database_not_a_default_or_cached_connection(store, tmp_path):
    first = _make_database(tmp_path / "first.db")
    second = _make_database(tmp_path / "second.db")
    try:
        _seed(first, {"recent": _recent("family"), "item_size_percent": 50}, revision=4)
        _seed(second, {"recent": _recent("format"), "item_size_percent": 70}, revision=4)
        _assert_snapshot(_load(store, first), (4, (1, "family", ()), False, 50, "", ()))
        _assert_snapshot(_load(store, second), (4, (1, "format", ()), False, 70, "", ()))
        assert _save(store, first, 4, {"item_size_percent": 90}) == 5
        assert _raw_row(second)[2] == 4
        assert json.loads(_raw_row(second)[1]) == {"recent": _recent("format"), "item_size_percent": 70}
    finally:
        first.rollback()
        first.close()
        second.rollback()
        second.close()


@pytest.mark.parametrize("document,diagnostics", [
    ({}, ("recent_absent",)),
    ({"recent": None}, ("recent_corrupt",)),
    ({"recent": False}, ("recent_corrupt",)),
    ({"recent": 0}, ("recent_corrupt",)),
    ({"recent": ""}, ("recent_corrupt",)),
    ({"recent": []}, ("recent_corrupt",)),
    ({"recent": {}}, ("recent_corrupt",)),
    ({"recent": _recent(version=True)}, ("recent_corrupt",)),
    ({"recent": _recent(version=2)}, ("recent_unsupported_version",)),
    ({"recent": _recent(version=0)}, ("recent_unsupported_version",)),
    ({"recent": _recent("custom", [_group(key=_KEY_A, selectors=["format:unknown"])])}, ("recent_corrupt",)),
    ({"recent": _recent("custom", [_group(selectors=["format:book"])])}, ("recent_corrupt",)),
])
def test_readable_recent_fallbacks_have_literal_diagnostics_and_no_repair(store, unit_db, document, diagnostics):
    _seed(unit_db, {**document, "opaque": [False, 0, "", None]}, revision=6)
    _assert_snapshot(_load(store, unit_db), (6, (1, "mixed", ()), False, 100, "", diagnostics))
    assert json.loads(_raw_row(unit_db)[1]) == {**document, "opaque": [False, 0, "", None]}


@pytest.mark.parametrize("shelves,hidden", [
    ([], True), (["continue"], True), (["saved:7"], True), (["recent"], False),
    (False, False), (0, False), (None, False), ("", False), ({}, False),
    ([1], False), (["x"] * 13, False),
])
def test_explicit_shelf_hide_is_not_reinterpreted_or_reset_by_fallback(store, unit_db, shelves, hidden):
    _seed(unit_db, {"shelves": shelves, "recent": False})
    _assert_snapshot(_load(store, unit_db), (1, (1, "mixed", ()), hidden, 100, "", ("recent_corrupt",)))


@pytest.mark.parametrize("size", [False, 0, 55, 101, 60.0, "60", None, [], {}])
def test_stored_invalid_size_falls_back_without_coercion_or_repair(store, unit_db, size):
    _seed(unit_db, {"recent": _recent(), "item_size_percent": size})
    _assert_snapshot(_load(store, unit_db), (1, (1, "mixed", ()), False, 100, "", ("item_size_corrupt",)))


@pytest.mark.parametrize("view,code", [
    (None, "default_saved_view_corrupt"), (0, "default_saved_view_corrupt"),
    (False, "default_saved_view_corrupt"), ("01", "default_saved_view_corrupt"),
    ("0", "default_saved_view_corrupt"), ("\u0661", "default_saved_view_corrupt"),
    ("7", "default_saved_view_unavailable"),
])
def test_stored_view_syntax_and_unavailable_are_distinct_generic_fallbacks(store, unit_db, view, code):
    _seed(unit_db, {"recent": _recent(), "default_saved_view": view})
    _assert_snapshot(_load(store, unit_db), (1, (1, "mixed", ()), False, 100, "", (code,)))


@pytest.mark.parametrize("revision", [None, 0, -1, 1.5, "1", b"1", str(_MAX + 1)])
def test_invalid_present_revision_is_none_not_absent_and_fields_still_project(store, damaged_db, revision):
    _seed(damaged_db, {"recent": _recent("family"), "item_size_percent": 70, "shelves": []}, revision=revision)
    _assert_snapshot(_load(store, damaged_db), (None, (1, "family", ()), True, 70, "", ("stored_revision_corrupt",)))
    _refuse(store, damaged_db, "stored_revision_corrupt", changes={}, expected=0)
    _refuse(store, damaged_db, "stored_revision_corrupt", fields=("recent",), expected=0)


def test_diagnostic_order_revision_document_recent_size_view_is_closed(store, damaged_db):
    _seed(damaged_db, {"recent": False, "item_size_percent": True, "default_saved_view": "+7"}, revision=None)
    _assert_snapshot(_load(store, damaged_db), (None, (1, "mixed", ()), False, 100, "", (
        "stored_revision_corrupt", "recent_corrupt", "item_size_corrupt", "default_saved_view_corrupt",
    )))
    damaged_db.execute("UPDATE library_preferences SET values_json='null'")
    damaged_db.commit()
    _assert_snapshot(_load(store, damaged_db), (None, (1, "mixed", ()), False, 100, "", (
        "stored_revision_corrupt", "stored_document_corrupt",
    )))


@pytest.mark.parametrize("raw", [
    None, 0, 1.5, b"{}", b"\xff", "", "{", "null", "false", "0", "[]", '"object?"',
    '{"x":0,"x":false}', '{"recent":{"version":1,"version":1}}',
    '{"x":{"nested":0,"nested":null}}', '{"x":NaN}', '{"x":Infinity}',
    " " * 65_537 + "{}", '{"x":"' + "\u754c" * 22_000 + '"}',
    '{"x":' + "[" * 12 + "0" + "]" * 12 + "}",
])
def test_present_whole_corruption_is_never_absent_or_destructively_recovered(store, damaged_db, raw):
    # Bind the storage value directly: SQL NULL/numbers/BLOB must reach typeof,
    # not be turned into a JSON string by the fixture itself.
    damaged_db.execute("INSERT INTO library_preferences VALUES (1, ?, 9, 'keep')", (raw,))
    damaged_db.commit()
    _assert_snapshot(_load(store, damaged_db), (9, (1, "mixed", ()), False, 100, "", ("stored_document_corrupt",)))
    _refuse(store, damaged_db, "stored_document_corrupt", changes={}, expected=9)
    _refuse(store, damaged_db, "stored_document_corrupt", fields=("recent",), expected=9)


@pytest.mark.parametrize("operation", ["load", "save", "reset"])
def test_every_operation_requires_explicit_caller_transaction(store, unit_db, operation):
    _clear_observations(unit_db)
    with pytest.raises(store.PreferenceStoreError) as error:
        if operation == "load":
            store.load_preferences(unit_db, _actor(store))
        elif operation == "save":
            store.save_preferences(unit_db, _actor(store), 0, {})
        else:
            store.reset_preferences(unit_db, _actor(store), 0, ("recent",))
    _assert_error(error.value, "transaction_required")
    assert unit_db.calls == [] and not unit_db.in_transaction
    assert unit_db.total_changes == 2
    assert not unit_db.executions, "reject before protected reads, not after doing work"


@pytest.mark.parametrize("expected", [True, False, -1, _MAX + 1, 0.0, "0", None, _Int(0)])
def test_expected_revision_is_strict_builtin_bounded_integer(store, unit_db, expected):
    _refuse(store, unit_db, "invalid_expected_revision", changes={}, expected=expected)
    _refuse(store, unit_db, "invalid_expected_revision", fields=("recent",), expected=expected)


@pytest.mark.parametrize("changes", [
    None, [], (), "{}", UserDict(), _Dict(), {"unknown": False}, {"shelves": []},
    {"owner_id": 2}, {1: 100}, {_Str("item_size_percent"): 100},
])
def test_changes_only_exact_dict_supported_builtin_keys(store, unit_db, changes):
    _refuse(store, unit_db, "invalid_changes", changes=changes, expected=0)


@pytest.mark.parametrize("value", [True, False, 49, 55, 101, 50.0, "50", None, [], _Int(50)])
def test_submitted_size_refuses_alias_types_null_and_clamping(store, unit_db, value):
    _refuse(store, unit_db, "invalid_item_size_percent", changes={"item_size_percent": value}, expected=0)


@pytest.mark.parametrize("value", [50, 60, 70, 80, 90, 100])
def test_each_supported_size_roundtrips_without_materializing_other_defaults(store, unit_db, value):
    assert _save(store, unit_db, 0, {"item_size_percent": value}) == 1
    assert json.loads(_raw_row(unit_db)[1]) == {"item_size_percent": value}
    _assert_snapshot(_load(store, unit_db), (1, (1, "mixed", ()), False, value, "", ("recent_absent",)))


@pytest.mark.parametrize("value", [
    "0", "00", "01", "+1", "-1", " 1", "1 ", "1\n", "1.0", "1e0",
    "\u0661", "\uff11", str(_MAX + 1), 7, True, None, [], _Str("7"),
])
def test_submitted_view_requires_ascii_canonical_bounded_positive_decimal(store, unit_db, value):
    _refuse(store, unit_db, "invalid_default_saved_view", changes={"default_saved_view": value}, expected=0)


@pytest.mark.parametrize("view_id", [1, _MAX])
def test_view_integer_endpoints_are_valid_only_as_owned_canonical_strings(store, unit_db, view_id):
    unit_db.execute("INSERT INTO library_saved_views (id,user_id,name,filters_json) "
                    "VALUES (?,1,'private name','not a filter to execute')", (view_id,))
    unit_db.commit()
    assert _save(store, unit_db, 0, {"default_saved_view": str(view_id)}) == 1
    _assert_snapshot(_load(store, unit_db), (1, (1, "mixed", ()), False, 100, str(view_id), ("recent_absent",)))


@pytest.mark.parametrize("value", [
    None, False, 0, "", [], _Dict(version=1, mode="mixed", groups=[]),
    {"mode": "mixed", "groups": []}, {"version": 1, "groups": []},
    {"version": 1, "mode": "mixed"}, _recent(version=True), _recent(version="1"),
    _recent(version=2), _recent(version=_Int(1)), _recent(mode="CUSTOM"),
    _recent(mode=_Str("mixed")), _recent(groups=None), _recent(groups=()),
    _recent(groups=_List()), _recent(extra=False),
    {_Str("version"): 1, "mode": "mixed", "groups": []},
])
def test_submitted_recent_requires_exact_builtin_shapes_and_closed_keys(store, unit_db, value):
    _refuse(store, unit_db, "invalid_recent", changes={"recent": value}, expected=0)


@pytest.mark.parametrize("value", [
    None, [], _Dict(name="x", selectors=[]), {"selectors": []}, {"name": "x"},
    _group(extra=False), {_Str("name"): "x", "selectors": []},
])
def test_group_requires_builtin_object_name_selectors_and_only_optional_key(store, unit_db, value):
    _refuse(store, unit_db, "invalid_recent", changes={"recent": _recent("custom", [value])}, expected=0)


@pytest.mark.parametrize("selectors", [
    None, "format:book", (), _List(), [None], [False], [1], [_Str("format:book")],
    ["format:kids_book"], ["format:BOOK"], [" format:book"], ["format:book "],
    ["family:book"], ["family:video"], ["format:unknown"], [""], ["all", "all"],
    ["all", "format:book"], ["format:book"] * 33,
])
def test_selectors_are_exact_registry_values_not_aliases_or_broadening_fallback(store, unit_db, selectors):
    _refuse(store, unit_db, "invalid_recent", changes={
        "recent": _recent("custom", [_group(selectors=selectors)]),
    }, expected=0)


@pytest.mark.parametrize("name", [
    "", "  ", None, False, 0, 50.0, b"raw", [], _Str("raw"), "x" * 81,
    "a\x00b", "a\nb", "a\x7fb", "a\x85b", "a\ud800b",
])
def test_names_are_nonblank_at_most_80_raw_characters_without_controls(store, unit_db, name):
    _refuse(store, unit_db, "invalid_recent", changes={"recent": _recent("custom", [_group(name)])}, expected=0)


@pytest.mark.parametrize("name", ["\u754c" * 80, "  e\u0301 & <row>  ", "a\u200db"])
def test_accepted_names_retain_exact_unicode_whitespace_and_spelling(store, unit_db, name):
    changes = {"recent": _recent("custom", [_group(name, ["family:books"])])}
    before = copy.deepcopy(changes)
    assert _save(store, unit_db, 0, changes) == 1
    assert changes == before
    _assert_snapshot(_load(store, unit_db), (1, (1, "custom", ((
        "recent:9038336c6d6b5d9d409252bb492ef629", name, ("family:books",),
    ),)), False, 100, "", ()))


@pytest.mark.parametrize("mode", ["mixed", "family", "format", "custom"])
def test_all_modes_including_intentionally_empty_custom_are_exact(store, unit_db, mode):
    assert _save(store, unit_db, 0, {"recent": _recent(mode)}) == 1
    _assert_snapshot(_load(store, unit_db), (1, (1, mode, ()), False, 100, "", ()))
    assert json.loads(_raw_row(unit_db)[1]) == {"recent": _recent(mode)}


@pytest.mark.parametrize("mode", ["mixed", "family", "format"])
def test_noncustom_modes_refuse_groups_instead_of_dropping_them(store, unit_db, mode):
    _refuse(store, unit_db, "invalid_recent", changes={"recent": _recent(mode, [_group()])}, expected=0)


def test_12_groups_32_prededupe_selectors_and_13th_group_boundary(store, unit_db):
    groups = [_group(str(i), ["format:" + name]) for i, name in enumerate(_FORMATS[:12])]
    assert _save(store, unit_db, 0, {"recent": _recent("custom", groups)}) == 1
    assert len(_load(store, unit_db).recent.groups) == 12
    _refuse(store, unit_db, "invalid_recent", changes={
        "recent": _recent("custom", groups + [_group("13", ["format:manga"])])}, expected=1)
    assert _save(store, unit_db, 1, {"recent": _recent("custom", [
        _group("32", ["format:book"] * 32),
    ])}) == 2
    assert _load(store, unit_db).recent.groups[0].selectors == ("format:book",)
    _refuse(store, unit_db, "invalid_recent", changes={"recent": _recent("custom", [
        _group("33", ["format:book"] * 33),
    ])}, expected=2)


@pytest.mark.parametrize("selectors", [[], ["all"]])
def test_unfiltered_selectors_canonicalize_to_empty_not_all_known_formats(store, unit_db, selectors):
    assert _save(store, unit_db, 0, {"recent": _recent("custom", [_group("Mixed", selectors)])}) == 1
    _assert_snapshot(_load(store, unit_db), (1, (1, "custom", ((
        "recent:78717d42196e9ac51424f90ff58ab17c", "Mixed", (),
    ),)), False, 100, "", ()))


def test_selector_dedupe_first_occurrence_and_registry_expansion_literal_key(store, unit_db):
    selectors = ["format:vinyl", "family:books", "format:book", "family:comics", "format:vinyl"]
    assert _save(store, unit_db, 0, {"recent": _recent("custom", [_group("Union", selectors)])}) == 1
    _assert_snapshot(_load(store, unit_db), (1, (1, "custom", ((
        "recent:708c99b27ad0f6e1c51956cc74d9aab9", "Union",
        ("format:vinyl", "family:books", "format:book", "family:comics"),
    ),)), False, 100, "", ()))


@pytest.mark.parametrize("first,second", [
    ([], ["all"]),
    (["family:music"], ["format:vinyl", "format:cassette", "format:cd", "format:digital_music"]),
    (["format:vinyl", "format:book"], ["format:book", "format:vinyl", "format:vinyl"]),
])
def test_duplicate_effective_groups_refuse_even_with_different_names(store, unit_db, first, second):
    _refuse(store, unit_db, "invalid_recent", changes={"recent": _recent("custom", [
        _group("First", first), _group("Second", second),
    ])}, expected=0)


def test_distinct_overlapping_groups_are_allowed_and_ordered(store, unit_db):
    assert _save(store, unit_db, 0, {"recent": _recent("custom", [
        _group("Mixed", []), _group("Vinyl", ["format:vinyl"]), _group("Music", ["family:music"]),
    ])}) == 1
    rows = _load(store, unit_db).recent.groups
    assert tuple(row.name for row in rows) == ("Mixed", "Vinyl", "Music")
    assert tuple(row.selectors for row in rows) == ((), ("format:vinyl",), ("family:music",))
    assert len({row.key for row in rows}) == 3


@pytest.mark.parametrize("key", [None, True, 1, "", "recent:1", "recent:" + "A" * 32,
                                  _Str(_KEY_A), "item:1", _KEY_B])
def test_keys_require_exact_shape_and_membership_in_fresh_original(store, unit_db, key):
    _seed(unit_db, {"recent": _recent("custom", [_group("old", ["format:book"], key=_KEY_A)])})
    _refuse(store, unit_db, "invalid_recent", changes={"recent": _recent("custom", [
        _group("new", ["format:vinyl"], key=key),
    ])})


@pytest.mark.parametrize("old", [None, False, _recent(version=2, groups=[
    _group("future", ["format:book"], key=_KEY_A),
]), _recent("custom", [_group("bad", ["format:unknown"], key=_KEY_A)])])
def test_corrupt_or_future_recent_proves_no_retained_keys(store, unit_db, old):
    _seed(unit_db, {"recent": old})
    _refuse(store, unit_db, "invalid_recent", changes={"recent": _recent("custom", [
        _group("new", ["format:book"], key=_KEY_A),
    ])})


def test_membership_is_reread_not_taken_from_preceding_snapshot(store, unit_db):
    _seed(unit_db, {"recent": _recent("custom", [_group("old", ["format:book"], key=_KEY_A)])})
    old_snapshot = _load(store, unit_db)
    assert old_snapshot.recent.groups[0].key == _KEY_A
    unit_db.execute("UPDATE library_preferences SET values_json=?, revision=2",
                    (_json({"recent": _recent("custom")}),))
    unit_db.commit()
    _refuse(store, unit_db, "invalid_recent", changes={"recent": _recent("custom", [
        _group("stale", ["format:book"], key=old_snapshot.recent.groups[0].key),
    ])}, expected=2)


def test_duplicate_keys_refuse_independently_of_effective_selector_duplicates(store, unit_db):
    _seed(unit_db, {"recent": _recent("custom", [_group("old", ["format:book"], key=_KEY_A)])})
    _refuse(store, unit_db, "invalid_recent", changes={"recent": _recent("custom", [
        _group("Books", ["family:books"], key=_KEY_A), _group("Vinyl", ["format:vinyl"], key=_KEY_A),
    ])})


def test_explicit_recent_preserves_retained_extras_but_removes_deleted_group_intentionally(store, unit_db):
    original = {"opaque": {"false": False, "zero": 0, "empty": "", "null": None}, "shelves": [],
                "recent": _recent("custom", [
                    _group("A", ["format:book"], key=_KEY_A, extra_a=[0, False, "", None]),
                    _group("B", ["format:vinyl"], key=_KEY_B, extra_b="intentionally removed"),
                ], future={"raw": "  e\u0301 <row>  ", "empty": []})}
    _seed(unit_db, original, revision=3)
    changes = {"recent": _recent("custom", [_group("A renamed", ["family:comics"], key=_KEY_A)])}
    before = copy.deepcopy(changes)
    assert _save(store, unit_db, 3, changes) == 4
    assert changes == before
    assert json.loads(_raw_row(unit_db)[1]) == {
        "opaque": {"false": False, "zero": 0, "empty": "", "null": None}, "shelves": [],
        "recent": _recent("custom", [_group("A renamed", ["family:comics"], key=_KEY_A,
                                            extra_a=[0, False, "", None])],
                          future={"raw": "  e\u0301 <row>  ", "empty": []}),
    }
    assert _load(store, unit_db).recent_hidden is True


@pytest.mark.parametrize("old_recent", [None, False, 0, "", [], {}, _recent(version=2, opaque=False)])
def test_omitted_recent_is_preserved_verbatim_even_when_fallback_was_loaded(store, unit_db, old_recent):
    original = {"recent": old_recent, "future": {"false": False, "zero": 0, "empty": [], "null": None},
                "shelves": [], "default_saved_view": "999"}
    _seed(unit_db, original)
    _load(store, unit_db)
    assert _save(store, unit_db, 1, {"item_size_percent": 80}) == 2
    assert json.loads(_raw_row(unit_db)[1]) == {**original, "item_size_percent": 80}


def test_omitted_absent_recent_and_dangling_view_are_not_materialized_or_repaired(store, unit_db):
    original = {"opaque": False, "shelves": [], "default_saved_view": "999"}
    _seed(unit_db, original)
    _load(store, unit_db)
    assert _save(store, unit_db, 1, {}) == 2
    assert json.loads(_raw_row(unit_db)[1]) == original


@pytest.mark.parametrize("fields", [
    (), [], "recent", {"recent"}, _Tuple(("recent",)), ("recent", "recent"),
    ("unknown",), ("shelves",), (None,), (_Str("recent"),),
    ("recent", "item_size_percent", "default_saved_view", "recent"),
])
def test_reset_requires_nonempty_exact_tuple_of_distinct_supported_builtin_names(store, unit_db, fields):
    _refuse(store, unit_db, "invalid_reset_fields", fields=fields, expected=0)


def test_reset_none_is_refused_not_an_optional_reset_all_bypass(store, unit_db):
    _begin(unit_db)
    try:
        with pytest.raises(store.PreferenceStoreError) as error:
            store.reset_preferences(unit_db, _actor(store), 0, None)
        _assert_error(error.value, "invalid_reset_fields")
        _assert_no_lifecycle(unit_db)
        assert _raw_row(unit_db) is None
    finally:
        unit_db.rollback()


@pytest.mark.parametrize("fields,document", [
    (("recent",), {"recent": _recent()}),
    (("item_size_percent",), {"item_size_percent": 100}),
    (("default_saved_view",), {"default_saved_view": ""}),
    (("default_saved_view", "recent", "item_size_percent"), {
        "recent": _recent(), "item_size_percent": 100, "default_saved_view": "",
    }),
])
def test_reset_absence_creates_revision_one_with_only_selected_defaults(store, unit_db, fields, document):
    _begin(unit_db)
    try:
        result = store.reset_preferences(unit_db, _actor(store), 0, fields)
        assert type(result) is int and result == 1
        _assert_no_lifecycle(unit_db)
        unit_db.commit()
    except BaseException:
        unit_db.rollback()
        raise
    assert json.loads(_raw_row(unit_db)[1]) == document
    assert _raw_row(unit_db)[2] == 1


@pytest.mark.parametrize("fields", [("recent",), ("item_size_percent",), ("default_saved_view",),
                                    ("recent", "default_saved_view", "item_size_percent")])
def test_scoped_reset_preserves_opaque_extras_shelves_and_unselected_supported_values(store, unit_db, fields):
    original = {"recent": _recent("custom", [_group("old", ["format:book"], key=_KEY_A, row_extra=False)],
                                  future={"null": None, "zero": 0}),
                "item_size_percent": 50, "default_saved_view": "999", "shelves": [],
                "opaque": [False, 0, "", None]}
    _seed(unit_db, original, revision=7)
    _begin(unit_db)
    try:
        assert store.reset_preferences(unit_db, _actor(store), 7, fields) == 8
        _assert_no_lifecycle(unit_db)
        unit_db.commit()
    except BaseException:
        unit_db.rollback()
        raise
    expected = copy.deepcopy(original)
    if "recent" in fields:
        expected["recent"] = _recent(future={"null": None, "zero": 0})
    if "item_size_percent" in fields:
        expected["item_size_percent"] = 100
    if "default_saved_view" in fields:
        expected["default_saved_view"] = ""
    assert json.loads(_raw_row(unit_db)[1]) == expected
    assert _load(store, unit_db).recent_hidden is True


@pytest.mark.parametrize("recent", [None, False, _recent(version=2, future=[False, 0, None])])
def test_explicit_recent_reset_is_allowed_without_repairing_unrelated_data(store, unit_db, recent):
    _seed(unit_db, {"recent": recent, "opaque": None, "shelves": []})
    _begin(unit_db)
    try:
        assert store.reset_preferences(unit_db, _actor(store), 1, ("recent",)) == 2
        _assert_no_lifecycle(unit_db)
        unit_db.commit()
    except BaseException:
        unit_db.rollback()
        raise
    result = json.loads(_raw_row(unit_db)[1])
    expected_recent = _recent(future=[False, 0, None]) if type(recent) is dict else _recent()
    assert result == {"recent": expected_recent, "opaque": None, "shelves": []}


def test_absence_create_noop_and_already_default_reset_each_increment_once(store, unit_db):
    assert _save(store, unit_db, 0, {}) == 1
    assert json.loads(_raw_row(unit_db)[1]) == {}
    assert _save(store, unit_db, 1, {}) == 2
    for expected in (2, 3):
        _begin(unit_db)
        assert store.reset_preferences(unit_db, _actor(store), expected, ("item_size_percent",)) == expected + 1
        _assert_no_lifecycle(unit_db)
        unit_db.commit()
    assert _raw_row(unit_db)[2] == 4
    assert json.loads(_raw_row(unit_db)[1]) == {"item_size_percent": 100}


@pytest.mark.parametrize("stored,expected", [(None, 1), (1, 0), (1, 2), (_MAX, _MAX - 1)])
def test_revision_conflict_has_no_dml_and_absence_alone_means_zero(store, unit_db, stored, expected):
    if stored is not None:
        _seed(unit_db, {}, revision=stored)
    _refuse(store, unit_db, "revision_conflict", changes={}, expected=expected)
    _refuse(store, unit_db, "revision_conflict", fields=("recent",), expected=expected)


def test_maximum_revision_exhaustion_does_not_wrap_delete_or_recreate(store, unit_db):
    _seed(unit_db, {"opaque": [False, 0, None]}, revision=_MAX - 1)
    assert _save(store, unit_db, _MAX - 1, {}) == _MAX
    _refuse(store, unit_db, "revision_exhausted", changes={}, expected=_MAX)
    _refuse(store, unit_db, "revision_exhausted", fields=("recent",), expected=_MAX)
    assert _raw_row(unit_db)[2] == _MAX


@pytest.mark.parametrize("which", ["id", "username", "role", "token_version", "deleted", "expired"])
def test_structured_actor_changed_is_not_invalid_actor_and_does_not_write(store, unit_db, monkeypatch, which):
    actor = _actor(store, expires_at=2_000_000_001)
    monkeypatch.setattr(time, "time", lambda: 2_000_000_000)
    if which == "id":
        actor = _actor(store, id=99)
    elif which == "username":
        actor = _actor(store, username="account a")  # DB NOCASE is not an exact identity match.
    elif which == "role":
        actor = _actor(store, role="admin")
    elif which == "token_version":
        actor = _actor(store, token_version=2)
    elif which == "deleted":
        unit_db.execute("DELETE FROM users WHERE id=1")
        unit_db.commit()
    else:
        monkeypatch.setattr(time, "time", lambda: 2_000_000_001)
    _refuse(store, unit_db, "actor_changed", changes={}, expected=0, actor=actor)
    _begin(unit_db, write=False)
    try:
        with pytest.raises(store.PreferenceStoreError) as error:
            store.load_preferences(unit_db, actor)
        _assert_error(error.value, "actor_changed")
        _assert_no_lifecycle(unit_db)
    finally:
        unit_db.rollback()


@pytest.mark.parametrize("updates", [
    {"id": None}, {"id": True}, {"id": 0}, {"id": _MAX + 1}, {"id": _Int(1)},
    {"username": ""}, {"username": None}, {"username": _Str("Account A")},
    {"role": "owner"}, {"role": "Viewer"}, {"role": _Str("viewer")},
    {"token_version": True}, {"token_version": 0}, {"token_version": _MAX + 1},
    {"token_version": 1.0}, {"token_version": _Int(1)},
    {"expires_at": None}, {"expires_at": True}, {"expires_at": 0}, {"expires_at": -1},
    {"expires_at": float("nan")}, {"expires_at": float("inf")},
    {"expires_at": float("-inf")}, {"expires_at": _EXPIRY + 1},
    {"expires_at": "253402300799"}, {"expires_at": _Float(_EXPIRY)},
])
def test_mandatory_actor_fields_are_strict_builtin_finite_and_bounded(store, unit_db, updates):
    actor = _actor(store)
    for key, value in updates.items():
        # Reach store validation even if a record constructor also validates.
        object.__setattr__(actor, key, value)
    _refuse(store, unit_db, "invalid_actor", changes={}, expected=0, actor=actor)
    _refuse(store, unit_db, "invalid_actor", fields=("recent",), expected=0, actor=actor)
    _begin(unit_db, write=False)
    try:
        with pytest.raises(store.PreferenceStoreError) as error:
            store.load_preferences(unit_db, actor)
        _assert_error(error.value, "invalid_actor")
        _assert_no_lifecycle(unit_db)
    finally:
        unit_db.rollback()


@pytest.mark.parametrize("kind", ["none", "dict", "duck", "subclass"])
def test_actor_requires_exact_record_not_optional_or_duck_typed_bypass(store, unit_db, kind):
    if kind == "none":
        # _refuse's convenience None means its default actor, so call directly.
        actor = None
    elif kind == "dict":
        actor = dataclasses.asdict(_actor(store))
    elif kind == "duck":
        actor = SimpleNamespace(**dataclasses.asdict(_actor(store)))
    else:
        class ActorSubclass(store.PreferenceActor):
            pass
        actor = ActorSubclass(1, "Account A", "viewer", 1, _EXPIRY)
    _begin(unit_db)
    try:
        with pytest.raises(store.PreferenceStoreError) as error:
            store.save_preferences(unit_db, actor, 0, {})
        _assert_error(error.value, "invalid_actor")
        _assert_no_lifecycle(unit_db)
        assert _raw_row(unit_db) is None
    finally:
        unit_db.rollback()


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
@pytest.mark.parametrize("expiry", [_EXPIRY, float(_EXPIRY)])
def test_all_roles_can_own_preferences_with_default_token_version_one(store, unit_db, role, expiry):
    unit_db.execute("UPDATE users SET role=? WHERE id=1", (role,))
    unit_db.commit()
    assert unit_db.execute("SELECT token_version FROM users WHERE id=1").fetchone()[0] == 1
    actor = _actor(store, role=role, expires_at=expiry)
    assert _save(store, unit_db, 0, {"item_size_percent": 90}, actor) == 1
    _assert_snapshot(_load(store, unit_db, actor), (1, (1, "mixed", ()), False, 90, "", ("recent_absent",)))
    _begin(unit_db)
    try:
        assert store.reset_preferences(unit_db, actor, 1, ("item_size_percent",)) == 2
        _assert_no_lifecycle(unit_db)
        unit_db.commit()
    except BaseException:
        unit_db.rollback()
        raise


@pytest.mark.parametrize("username", [" ", "  Account A  ", "e\u0301\u754c"])
def test_nonempty_captured_username_is_retained_exactly_not_stripped_or_normalized(store, unit_db, username):
    unit_db.execute("UPDATE users SET username=? WHERE id=1", (username,))
    unit_db.commit()
    assert _save(store, unit_db, 0, {}, _actor(store, username=username)) == 1


def test_maximum_actor_id_and_token_version_are_valid_capture_data(store, unit_db):
    unit_db.execute("UPDATE users SET id=?, token_version=? WHERE id=1", (_MAX, _MAX))
    unit_db.commit()
    actor = _actor(store, id=_MAX, token_version=_MAX)
    assert _save(store, unit_db, 0, {}, actor) == 1
    assert _raw_row(unit_db) is None
    assert _raw_row(unit_db, _MAX)[2] == 1


def test_mutation_order_rejects_stale_and_exhausted_before_field_reference_validation(store, unit_db):
    _seed(unit_db, {"recent": _recent()}, revision=4)
    _refuse(store, unit_db, "revision_conflict", changes={"default_saved_view": "999"}, expected=3)
    _refuse(store, unit_db, "revision_conflict", changes={"item_size_percent": True}, expected=3)
    unit_db.execute("UPDATE library_preferences SET revision=?", (_MAX,))
    unit_db.commit()
    _refuse(store, unit_db, "revision_exhausted", changes={"default_saved_view": "999"}, expected=_MAX)
    _refuse(store, unit_db, "revision_exhausted", changes={"recent": None}, expected=_MAX)


def test_current_actor_check_precedes_stored_corruption_and_corruption_precedes_conflict(store, damaged_db):
    _seed(damaged_db, "null", revision=4)
    _refuse(store, damaged_db, "actor_changed", changes={}, expected=0, actor=_actor(store, token_version=2))
    _refuse(store, damaged_db, "stored_document_corrupt", changes={}, expected=0)
    damaged_db.execute("UPDATE library_preferences SET revision=NULL")
    damaged_db.commit()
    _refuse(store, damaged_db, "stored_revision_corrupt", changes={}, expected=0)


@pytest.mark.parametrize("change", ["expiry", "username", "role", "token_version", "delete"])
@pytest.mark.parametrize("operation", ["save", "reset"])
def test_final_actor_recheck_after_validation_before_any_preference_dml(
    store, unit_db, monkeypatch, change, operation,
):
    now = [2_000_000_000]
    monkeypatch.setattr(time, "time", lambda: now[0])
    actor = _actor(store, expires_at=2_000_000_001)
    unit_db.execute("INSERT INTO library_saved_views (id,user_id,name,filters_json) VALUES (7,1,'raw','{}')")
    unit_db.commit()
    _seed(unit_db, {"recent": _recent(), "item_size_percent": 50})
    from app.services import recent_preferences
    original_merge = recent_preferences.merge_preferences
    reached = []

    def validate_then_change(original, changes):
        result = original_merge(original, changes)
        # The mandatory bounded original read uses {}. Inject only after that
        # public helper has actually completed successfully, so no unrelated
        # malformed-input failure can masquerade as the final actor check.
        if not reached:
            reached.append(True)
            if change == "expiry":
                now[0] = 2_000_000_001
            elif change == "delete":
                unit_db.execute("DELETE FROM users WHERE id=1")
            else:
                value = {"username": "changed", "role": "admin", "token_version": 2}[change]
                unit_db.execute(f"UPDATE users SET {change}=? WHERE id=1", (value,))
        return result

    monkeypatch.setattr(recent_preferences, "merge_preferences", validate_then_change)
    before = _raw_row(unit_db)
    _begin(unit_db)
    try:
        with pytest.raises(store.PreferenceStoreError) as error:
            if operation == "save":
                changes = {"item_size_percent": 80} if change == "delete" else {"default_saved_view": "7"}
                store.save_preferences(unit_db, actor, 1, changes)
            else:
                store.reset_preferences(unit_db, actor, 1, ("item_size_percent",))
        assert reached == [True]
        _assert_error(error.value, "actor_changed")
        _assert_no_lifecycle(unit_db)
        # Account deletion cascades the existing row, so compare DML statements,
        # not a fiction that the injected caller-side deletion left it present.
        preference_dml = [sql for sql in unit_db.sql
                          if re.match(r"\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+library_preferences\b", sql, re.I)]
        assert preference_dml == []
    finally:
        unit_db.rollback()
    assert _raw_row(unit_db) == before


@pytest.mark.parametrize("change", ["expiry", "username", "role", "token_version", "delete"])
@pytest.mark.parametrize("operation", ["save", "reset"])
def test_late_actor_recheck_after_successful_nonempty_submitted_recent_validation(
    store, unit_db, monkeypatch, change, operation,
):
    from app.services import recent_preferences

    now = [2_000_000_000]
    monkeypatch.setattr(time, "time", lambda: now[0])
    actor = _actor(store, expires_at=2_000_000_001)
    original = {"recent": _recent("custom", [_group("old", ["format:book"], key=_KEY_A)]),
                "opaque": [False, 0, "", None], "shelves": []}
    _seed(unit_db, original)
    submitted = (_recent("custom", [_group("edited", ["format:vinyl"], key=_KEY_A)])
                 if operation == "save" else _recent())
    phases = []
    hook = _late_recent_merge_hook(
        unit_db, recent_preferences.merge_preferences, submitted, change, now, phases,
    )
    monkeypatch.setattr(recent_preferences, "merge_preferences", hook)
    before = _logical_state(unit_db)
    _begin(unit_db)
    try:
        error = None
        try:
            if operation == "save":
                store.save_preferences(unit_db, actor, 1, {"recent": submitted})
            else:
                store.reset_preferences(unit_db, actor, 1, ("recent",))
        except store.PreferenceStoreError as caught:
            error = caught
        _assert_late_recent_refusal(unit_db, phases, error)
    finally:
        # The test's deliberate actor mutation (including an account cascade)
        # belongs to this caller transaction too. The store must not end it.
        unit_db.rollback()
    assert _logical_state(unit_db) == before


@pytest.mark.parametrize("recent_size", [32_768, 32_769])
def test_recent_byte_boundary_preserved_extras_and_explicit_merge_limit(store, unit_db, recent_size):
    base = _recent(pad="")
    base["pad"] = "x" * (recent_size - len(_json(base).encode("utf-8")))
    assert len(_json(base).encode("utf-8")) == recent_size
    _seed(unit_db, {"recent": base})
    expected_diagnostics = () if recent_size == 32_768 else ("recent_corrupt",)
    _assert_snapshot(_load(store, unit_db), (1, (1, "mixed", ()), False, 100, "", expected_diagnostics))
    assert _save(store, unit_db, 1, {}) == 2
    assert json.loads(_raw_row(unit_db)[1]) == {"recent": base}
    if recent_size == 32_768:
        assert _save(store, unit_db, 2, {"recent": _recent()}) == 3
        assert json.loads(_raw_row(unit_db)[1]) == {"recent": base}
    else:
        _refuse(store, unit_db, "payload_too_large", changes={"recent": _recent()}, expected=2)


@pytest.mark.parametrize("unicode_pad", [False, True])
def test_whole_json_65536_utf8_boundary_and_merged_not_just_input_bound(store, unit_db, unicode_pad):
    document = {"opaque": ""}
    remaining = 65_536 - len(_json(document).encode("utf-8"))
    document["opaque"] = ("\u754c" * (remaining // 3) + "x" * (remaining % 3)
                          if unicode_pad else "x" * remaining)
    assert len(_json(document).encode("utf-8")) == 65_536
    _seed(unit_db, document)
    assert _load(store, unit_db).diagnostics == ("recent_absent",)
    assert _save(store, unit_db, 1, {}) == 2
    assert json.loads(_raw_row(unit_db)[1]) == document
    _refuse(store, unit_db, "payload_too_large", changes={"item_size_percent": 50}, expected=2)


@pytest.mark.parametrize("depth", [12, 13])
def test_whole_document_exact_12_container_depth_and_string_brackets(store, unit_db, depth):
    nested = "[[[ literal \\\" brackets ]]]"
    for _ in range(depth - 1):
        nested = [nested]
    document = {"opaque": nested}
    _seed(unit_db, document)
    if depth == 12:
        assert _load(store, unit_db).diagnostics == ("recent_absent",)
        assert _save(store, unit_db, 1, {}) == 2
        assert json.loads(_raw_row(unit_db)[1]) == document
    else:
        assert _load(store, unit_db).diagnostics == ("stored_document_corrupt",)
        _refuse(store, unit_db, "stored_document_corrupt", changes={})


@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le"])
def test_bounded_physical_read_allows_utf16_but_never_fetches_huge_text(store, tmp_path, encoding):
    conn = _make_database(tmp_path / "physical-bound.db", encoding=encoding)
    try:
        _seed(conn, {"opaque": "x" * 65_500})
        physical = conn.execute("SELECT length(CAST(values_json AS BLOB)) FROM library_preferences").fetchone()[0]
        assert physical <= 131_072
        if encoding == "UTF-16le":
            assert physical > 65_536
        assert _load(store, conn).diagnostics == ("recent_absent",)
        assert _save(store, conn, 1, {}) == 2
        conn.execute("UPDATE library_preferences SET values_json=?", ("x" * 140_000,))
        conn.commit()
        _begin(conn, write=False)
        snapshot = store.load_preferences(conn, _actor(store))
        _assert_snapshot(snapshot, (2, (1, "mixed", ()), False, 100, "", ("stored_document_corrupt",)))
        assert not [size for _name, size in conn.fetched_text_sizes if size > 131_072]
        preference_queries = _preference_read_queries(conn.executions)
        _assert_metadata_first(conn.executions)
        _assert_metadata_only_preference_reads(preference_queries)
        _assert_no_lifecycle(conn)
        conn.rollback()
    finally:
        conn.rollback()
        conn.close()


def test_exact_utf16_131072_physical_byte_endpoint_loads_and_noop_preserves(store, tmp_path):
    conn = _make_database(tmp_path / "exact-utf16-endpoint.db", encoding="UTF-16le")
    try:
        raw = _json({"opaque": "x" * (65_536 - len(_json({"opaque": ""})))})
        assert len(raw.encode("ascii")) == len(raw.encode("utf-8")) == 65_536
        _seed(conn, raw)
        assert conn.execute("SELECT length(CAST(values_json AS BLOB)) FROM library_preferences").fetchone()[0] == 131_072
        before = _logical_state(conn)
        _assert_snapshot(_load(store, conn), (1, (1, "mixed", ()), False, 100, "", ("recent_absent",)))
        _assert_metadata_first(conn.executions)
        assert any(size == 65_536 for _name, size in conn.fetched_text_sizes)
        assert _logical_state(conn) == before
        assert _save(store, conn, 1, {}) == 2
        _assert_metadata_first(conn.executions)
        assert _raw_row(conn)[1:3] == (raw, 2)
        assert conn.execute("SELECT length(CAST(values_json AS BLOB)) FROM library_preferences").fetchone()[0] == 131_072
        after_schema, after_rows = _logical_state(conn)
        assert after_schema == before[0]
        assert tuple(row for row in after_rows if row[0] != "library_preferences") == tuple(
            row for row in before[1] if row[0] != "library_preferences"
        )
    finally:
        conn.rollback()
        conn.close()


def test_group_key_collision_maps_fixed_error_with_bounded_work_no_mutation(store, unit_db, monkeypatch):
    from app.services import recent_preferences

    _seed(unit_db, {"recent": _recent("custom", [_group("old", ["format:book"], key=_KEY_A)])})
    calls = []

    class Digest:
        def hexdigest(self):
            return "1" * 64

    def colliding_digest(value):
        calls.append(value)
        return Digest()

    monkeypatch.setattr(recent_preferences.hashlib, "sha256", colliding_digest)
    _refuse(store, unit_db, "group_key_collision", changes={"recent": _recent("custom", [
        _group("retained", ["format:vinyl"], key=_KEY_A), _group("new", ["format:book"]),
    ])})
    assert len(calls) == 2


@pytest.mark.parametrize("operation", ["load", "save", "reset"])
def test_store_never_owns_transactions_connections_callbacks_or_logging(
    store, unit_db, monkeypatch, operation,
):
    from app import database

    _seed(unit_db, {"recent": _recent()})
    external = []
    original_connect, original_get_db = sqlite3.connect, database.get_db
    forbidden_code = {
        database.get_db.__wrapped__.__code__: "get_db_bound_alias",
        database.after_commit.__code__: "after_commit_bound_alias",
    }
    previous_profile = sys.getprofile()

    def profile(frame, event, argument):
        # Observe pre-bound imports too; monkeypatching only the module name
        # cannot see `from ... import after_commit` captured before this test.
        if event == "call" and frame.f_code in forbidden_code:
            external.append(forbidden_code[frame.f_code])
        elif event == "c_call" and argument is original_connect:
            external.append("connect_bound_alias")

    def connect(*args, **kwargs):
        external.append("connect")
        return original_connect(*args, **kwargs)

    @contextmanager
    def get_db(*args, **kwargs):
        external.append("get_db")
        with original_get_db(*args, **kwargs) as conn:
            yield conn

    def callback(*args, **kwargs):
        external.append("after_commit")

    def logged(*args, **kwargs):
        external.append("log")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(database, "get_db", get_db)
    monkeypatch.setattr(database, "after_commit", callback)
    monkeypatch.setattr(logging.Logger, "_log", logged)
    _begin(unit_db, write=operation != "load")
    try:
        sys.setprofile(profile)
        try:
            if operation == "load":
                store.load_preferences(unit_db, _actor(store))
            elif operation == "save":
                assert store.save_preferences(unit_db, _actor(store), 1, {}) == 2
            else:
                assert store.reset_preferences(unit_db, _actor(store), 1, ("recent",)) == 2
        finally:
            sys.setprofile(previous_profile)
            # Observations are checked even if the store swallows a forbidden
            # call's exception or another assertion would otherwise mask it.
            _assert_no_lifecycle(unit_db, external)
            _assert_store_statement_kinds(unit_db.executions, unit_db.sql)
    finally:
        sys.setprofile(previous_profile)
        unit_db.rollback()


def test_oracle_control_payload_builders_preserve_null_and_exact_subclass_types():
    assert _recent(groups=None) == {"version": 1, "mode": "mixed", "groups": None}
    assert _group(selectors=None) == {"name": "My raw row", "selectors": None}
    assert type(_recent(groups=_List())["groups"]) is _List
    assert type(_group(name=_Str("raw"))["name"]) is _Str
    assert type(next(iter({_Str("recent"): None}))) is _Str


@pytest.mark.parametrize("mutation", ["commit", "rollback", "begin", "savepoint", "log", "connect", "callback"])
def test_oracle_control_observable_lifecycle_catches_swallowed_mutants(unit_db, mutation):
    """Synthetic mutants only: these controls never stand in for store GREEN."""
    _begin(unit_db)
    external = []
    try:
        if mutation == "commit":
            unit_db.commit()
        elif mutation == "rollback":
            unit_db.rollback()
        elif mutation == "begin":
            try:
                unit_db.execute("BEGIN")
            except sqlite3.OperationalError:
                pass
            # Failed nested BEGIN is not traced by all SQLite versions; the
            # explicit observable execute attempt is still an oracle input.
            assert ("BEGIN", (), True) in unit_db.executions
        elif mutation == "savepoint":
            unit_db.execute("SAVEPOINT forbidden")
        else:
            external.append(mutation)
        with pytest.raises(AssertionError):
            _assert_no_lifecycle(unit_db, external)
    finally:
        unit_db.rollback()


@pytest.mark.parametrize("mutation", ["zero", "diagnostic", "leak", "mutable-children", "bool-size"])
def test_oracle_control_snapshot_literal_oracle_rejects_separate_mutants(mutation):
    """Independent synthetic records keep missing-feature failures out of controls."""
    @dataclasses.dataclass(frozen=True)
    class Recent:
        version: int
        mode: str
        groups: tuple

    @dataclasses.dataclass(frozen=True)
    class Snapshot:
        revision: int | None
        recent: Recent
        recent_hidden: bool
        item_size_percent: int
        default_saved_view: str
        diagnostics: tuple

    value = Snapshot(0, Recent(1, "mixed", ()), False, 100, "", ("preferences_absent",))
    expected = (0, (1, "mixed", ()), False, 100, "", ("preferences_absent",))
    _assert_snapshot(value, expected)  # positive control reaches the same oracle
    if mutation == "zero":
        value = dataclasses.replace(value, revision=None)
    elif mutation == "diagnostic":
        value = dataclasses.replace(value, diagnostics=("recent_absent",))
    elif mutation == "leak":
        object.__setattr__(value, "values", {"opaque": "leaked"})
    elif mutation == "mutable-children":
        value = dataclasses.replace(value, recent=Recent(1, "mixed", []))
    else:
        value = dataclasses.replace(value, item_size_percent=True)
    with pytest.raises(AssertionError):
        _assert_snapshot(value, expected)


@pytest.mark.parametrize("operation", ["save", "reset"])
@pytest.mark.parametrize("mutant", ["original-read-only-recheck", "early-refusal"])
def test_oracle_control_late_recent_phase_rejects_original_read_only_checks(unit_db, operation, mutant):
    """Synthetic phase mutants, never a substitute for composed store GREEN."""
    from app.services import recent_preferences

    class SyntheticActorChanged(ValueError):
        code = "actor_changed"

        def __init__(self):
            super().__init__(self.code)

    _seed(unit_db, {"recent": _recent("custom", [_group("old", ["format:book"], key=_KEY_A)]),
                    "opaque": [False, 0, "", None], "shelves": []})
    submitted = (_recent("custom", [_group("edited", ["format:vinyl"], key=_KEY_A)])
                 if operation == "save" else _recent())
    before = _logical_state(unit_db)

    def exercise(variant):
        now, phases = [2_000_000_000], []
        merge = _late_recent_merge_hook(
            unit_db, recent_preferences.merge_preferences, submitted, "expiry", now, phases,
        )
        _begin(unit_db)
        try:
            error = None
            try:
                original = merge(_raw_row(unit_db)[1], {})
                assert now == [2_000_000_000], "decoding alone must not expire the actor"
                # This is the mutant's sole recheck: it passes BEFORE the late
                # change and therefore cannot fence the subsequent DML.
                if now[0] >= 2_000_000_001:
                    raise SyntheticActorChanged()
                if variant == "early-refusal":
                    raise SyntheticActorChanged()
                merged = merge(original, {"recent": submitted})
                if variant == "correct-final-check" and now[0] >= 2_000_000_001:
                    raise SyntheticActorChanged()
                assert unit_db.execute(
                    "UPDATE library_preferences SET values_json=?, revision=2 WHERE user_id=1 AND revision=1",
                    (_json(merged),),
                ).rowcount == 1
            except SyntheticActorChanged as caught:
                error = caught
            _assert_late_recent_refusal(unit_db, phases, error)
        finally:
            unit_db.rollback()

    exercise("correct-final-check")  # positive control reaches and passes the same oracle
    message = ("late actor change was not refused" if mutant == "original-read-only-recheck"
               else "nonempty submitted recent phase was not reached")
    with pytest.raises(AssertionError, match=message):
        exercise(mutant)
    assert _logical_state(unit_db) == before


@pytest.mark.parametrize("sql", [
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT forbidden", "RELEASE forbidden", "END",
])
def test_oracle_control_denied_prepare_lifecycle_attempt_is_seen_without_trace_or_marker(unit_db, sql):
    _begin(unit_db)
    denials = []

    def authorize(action, first, second, database, context):
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            denials.append((action, first, second))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    unit_db.set_authorizer(authorize)
    try:
        swallowed = None
        try:
            unit_db.execute(sql)
        except sqlite3.DatabaseError as error:
            swallowed = error
        assert swallowed is not None and swallowed.sqlite_errorcode == sqlite3.SQLITE_AUTH
        assert denials, "a genuine native prepare-time authorization denial must occur"
        assert unit_db.calls == [] and unit_db.sql == []
        assert unit_db.executions == [(sql, (), True)]
        assert unit_db.in_transaction
        with pytest.raises(AssertionError, match="attempted transaction SQL"):
            _assert_no_lifecycle(unit_db)  # no fabricated external marker
    finally:
        unit_db.set_authorizer(None)
        unit_db.rollback()


@pytest.mark.parametrize("sql", [
    pytest.param("SELECT revision, typeof(values_json), length(CAST(values_json AS BLOB)) "
                 "FROM library_preferences WHERE user_id=?", id="unqualified"),
    pytest.param("SELECT p.revision, typeof(p.values_json), length(CAST(p.values_json AS BLOB)) "
                 "FROM library_preferences AS p WHERE p.user_id=?", id="qualified"),
    pytest.param("SELECT p.revision, TYPEOF( p . values_json ), LENGTH( CAST( p . values_json AS BLOB ) ) "
                 "FROM main.library_preferences AS p WHERE p.user_id=?", id="qualified-spaced-main"),
    pytest.param('SELECT "p"."revision", typeof("p"."values_json"), length(CAST("p"."values_json" AS BLOB)) '
                 'FROM main."library_preferences" AS "p" WHERE "p"."user_id"=?', id="quoted-identifiers"),
    pytest.param("SELECT [p].[revision], typeof([p].[values_json]), length(CAST([p].[values_json] AS BLOB)) "
                 "FROM [library_preferences] AS [p] WHERE [p].[user_id]=?", id="bracket-identifiers"),
])
def test_oracle_control_bounded_metadata_accepts_equivalent_qualified_sql(unit_db, sql):
    raw = _json({"opaque": "x" * 140_000})
    _seed(unit_db, raw)
    _begin(unit_db, write=False)
    try:
        assert tuple(unit_db.execute(sql, (1,)).fetchone()) == (1, "text", len(raw.encode("utf-8")))
        queries = _preference_read_queries(unit_db.executions)
        assert queries == [sql]
        _assert_metadata_first(unit_db.executions)
        _assert_metadata_only_preference_reads(queries)
        assert not [size for _name, size in unit_db.fetched_text_sizes if size > 131_072]
        _assert_no_lifecycle(unit_db)
    finally:
        unit_db.rollback()


@pytest.mark.parametrize("sql,message", [
    pytest.param("SELECT values_json FROM library_preferences WHERE user_id=?", "huge full text", id="full-unqualified"),
    pytest.param("SELECT p.values_json FROM library_preferences AS p WHERE p.user_id=?", "huge full text", id="full-qualified"),
    pytest.param("SELECT CAST(p.values_json AS BLOB) FROM library_preferences AS p WHERE p.user_id=?", "huge full text", id="full-blob"),
    pytest.param("SELECT * FROM library_preferences WHERE user_id=?", "full values", id="full-star"),
    pytest.param("SELECT p.* FROM library_preferences AS p WHERE p.user_id=?", "full values", id="full-qualified-star"),
    pytest.param("SELECT substr(p.values_json,1,64) FROM library_preferences AS p WHERE p.user_id=?", "prefix", id="prefix-substr"),
    pytest.param("SELECT substring(values_json,1,64) FROM library_preferences WHERE user_id=?", "prefix", id="prefix-substring"),
    pytest.param("SELECT p.values_json, typeof(p.values_json), length(CAST(p.values_json AS BLOB)) "
                 "FROM library_preferences AS p WHERE p.user_id=?", "huge full text", id="full-with-metadata"),
])
def test_oracle_control_bounded_metadata_rejects_real_full_text_and_prefix_reads(unit_db, sql, message):
    _seed(unit_db, {"opaque": "x" * 140_000})
    metadata = ("SELECT p.revision, typeof(p.values_json), length(CAST(p.values_json AS BLOB)) "
                "FROM library_preferences AS p WHERE p.user_id=?")
    _begin(unit_db, write=False)
    try:
        unit_db.execute(metadata, (1,)).fetchone()
        _assert_metadata_only_preference_reads(_preference_read_queries(unit_db.executions))
        unit_db.execute(sql, (1,)).fetchone()  # native synthetic fault, not a store call
        _assert_metadata_first(unit_db.executions)
        assert _preference_read_queries(unit_db.executions) == [metadata, sql]
        with pytest.raises(AssertionError, match=message):
            _assert_metadata_only_preference_reads(_preference_read_queries(unit_db.executions))
        _assert_no_lifecycle(unit_db)
    finally:
        unit_db.rollback()


def test_oracle_control_bounded_metadata_requires_metadata_before_full_text(unit_db):
    _seed(unit_db, {"opaque": "small readable control"})
    _begin(unit_db, write=False)
    try:
        unit_db.execute("SELECT values_json FROM library_preferences WHERE user_id=?", (1,)).fetchone()
        unit_db.execute("SELECT revision, typeof(values_json), length(CAST(values_json AS BLOB)) "
                        "FROM library_preferences WHERE user_id=?", (1,)).fetchone()
        assert len(_preference_read_queries(unit_db.executions)) == 2
        with pytest.raises(AssertionError, match="bounded type and physical length metadata"):
            _assert_metadata_first(unit_db.executions)
        _assert_no_lifecycle(unit_db)
    finally:
        unit_db.rollback()


def test_oracle_control_exact_utf16_endpoint_is_native_readable_and_inclusive(tmp_path):
    conn = _make_database(tmp_path / "utf16-endpoint-control.db", encoding="UTF-16le")
    try:
        raw = _json({"opaque": "x" * (65_536 - len(_json({"opaque": ""})))})
        assert len(raw.encode("ascii")) == len(raw.encode("utf-8")) == 65_536
        _seed(conn, raw)
        _begin(conn, write=False)
        metadata = conn.execute("SELECT revision, typeof(values_json), length(CAST(values_json AS BLOB)) "
                                "FROM library_preferences WHERE user_id=?", (1,)).fetchone()
        assert tuple(metadata) == (1, "text", 131_072)
        _assert_metadata_first(conn.executions)
        _assert_metadata_only_preference_reads(_preference_read_queries(conn.executions))
        assert metadata[2] <= 131_072
        with pytest.raises(AssertionError):
            assert metadata[2] < 131_072  # explicit off-by-one mutant
        assert conn.execute("SELECT values_json FROM library_preferences WHERE user_id=?", (1,)).fetchone()[0] == raw
        _assert_no_lifecycle(conn)
        conn.rollback()
        _begin(conn)
        assert conn.execute("UPDATE library_preferences SET values_json=?, revision=2 "
                            "WHERE user_id=1 AND revision=1", (raw,)).rowcount == 1
        conn.commit()
        assert _raw_row(conn)[1:3] == (raw, 2)
        assert conn.execute("SELECT length(CAST(values_json AS BLOB)) FROM library_preferences").fetchone()[0] == 131_072
    finally:
        conn.rollback()
        conn.close()


_ENCODING_READ_SYNTAX = (
    "PRAGMA encoding",
    "PRAGMA main.encoding",
    " \n\tpRaGmA   MaIn . EnCoDiNg  ; \n",
    'PRAGMA "main"."encoding";',
    "PRAGMA [main].[encoding];",
    "PRAGMA `main`.`encoding`;",
    "PRAGMA 'main'.'encoding';",
    'PRAGMA "encoding";',
    'PRAGMA"main"."encoding";',
)


def _encoding_fixture_configuration(conn):
    """Caller-only readonly checks, outside the observed store/control phase."""
    # Do not prime the exact "PRAGMA main.encoding" statement under test: a
    # cached statement's binding-count check can precede authorizer reprepare.
    return tuple(conn.execute(sql).fetchone()[0] for sql in (
        "PRAGMA encoding", "PRAGMA main.foreign_keys", "PRAGMA main.journal_mode",
    ))


@pytest.mark.parametrize("sql", _ENCODING_READ_SYNTAX)
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le"])
def test_oracle_control_readonly_encoding_native_syntax_preserves_data_configuration_and_transaction(
    tmp_path, encoding, sql,
):
    conn = _make_database(tmp_path / "readonly-encoding.db", encoding=encoding)
    try:
        _seed(conn, {"item_size_percent": 80, "opaque": [False, 0, "", None]})
        before, configuration = _logical_state(conn), _encoding_fixture_configuration(conn)
        assert configuration[0] == encoding and configuration[1] == 1
        count = conn.total_changes
        _begin(conn, write=False)
        assert conn.execute(sql).fetchone()[0] == encoding
        assert conn.executions == [(sql, (), True)]
        assert len(conn.sql) == 1
        assert conn.total_changes == count
        _assert_no_lifecycle(conn)
        _assert_store_statement_kinds(conn.executions, conn.sql)
        conn.rollback()  # caller completion, after all observed-phase assertions
        assert _logical_state(conn) == before
        assert _encoding_fixture_configuration(conn) == configuration
        assert conn.total_changes == count
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("sql,parameters,trace", [
    ("SELECT id FROM users WHERE id=?", (1,), "SELECT id FROM users WHERE id=1"),
    ("INSERT INTO library_preferences (user_id,values_json,revision) VALUES (?,?,1)",
     (1, "{}"), "INSERT INTO library_preferences (user_id,values_json,revision) VALUES (1,'{}',1)"),
    ("UPDATE library_preferences SET values_json=?,revision=? WHERE user_id=? AND revision=?",
     ("{}", 2, 1, 1), "UPDATE library_preferences SET values_json='{}',revision=2 WHERE user_id=1 AND revision=1"),
])
def test_oracle_control_statement_policy_retains_three_existing_kinds(sql, parameters, trace):
    # Pure policy admission, not execution or persistence/authority evidence.
    _assert_store_statement_kinds([(sql, parameters, True)], [trace])


@pytest.mark.parametrize("channel", ["attempted", "traced"])
@pytest.mark.parametrize("sql", [
    "PRAGMA encoding='UTF-8'",
    "PRAGMA main.encoding = 'UTF-16le'",
    "PRAGMA encoding('UTF-8')",
    "PRAGMA main.encoding()",
    "PRAGMA encoding ?",
    "PRAGMA encoding UTF8",
    "PRAGMA main.encoding + 0",
    "PRAGMA foreign_keys",
    "PRAGMA main.foreign_keys=OFF",
    "PRAGMA journal_mode",
    "PRAGMA journal_mode=MEMORY",
    "PRAGMA temp.encoding",
    "PRAGMA other.encoding",
    "PRAGMA ma\u0131n.encoding",
    "PRAGMA main.encod\u0131ng",
    'PRAGMA "mainly"."encoding"',
    "PRAGMA main.encoding_extra",
    "PRAGMA main.encoding; SELECT 1",
    "PRAGMA encoding; PRAGMA foreign_keys=OFF",
    "PRAGMA encoding;;",
    "PRAGMA encoding unexpected",
    'PRAGMA "main"."foreign_keys"',
    "DELETE FROM library_preferences WHERE user_id=1",
    "CREATE TABLE forbidden (id INTEGER)",
    "",
])
def test_oracle_control_statement_policy_rejects_forbidden_pragma_and_trailing_sql_in_both_channels(sql, channel):
    # Pure classification ONLY: no setter/configuration-changing SQL is executed.
    attempts, traces = ([(sql, (), True)], []) if channel == "attempted" else ([], [sql])
    with pytest.raises(AssertionError, match=f"forbidden {channel} store statement"):
        _assert_store_statement_kinds(attempts, traces)


@pytest.mark.parametrize("parameters", [(1,), ("UTF-8",), (None,), (1, 2)])
def test_oracle_control_encoding_statement_policy_refuses_bound_arguments(parameters):
    with pytest.raises(AssertionError, match="forbidden attempted store statement"):
        _assert_store_statement_kinds([("PRAGMA main.encoding", parameters, True)], ["PRAGMA main.encoding"])


@pytest.mark.parametrize("sql", ["SELECT 1", "PRAGMA main.encoding"])
def test_oracle_control_statement_policy_requires_caller_transaction_in_attempt_triples(sql):
    with pytest.raises(AssertionError, match="outside the caller transaction"):
        _assert_store_statement_kinds([(sql, (), False)], [sql])


@pytest.mark.parametrize("sql,parameters", [
    ("PRAGMA main.encoding='UTF-16le'", ()),
    ("PRAGMA encoding('UTF-8')", ()),
    ("PRAGMA foreign_keys", ()),
    ("PRAGMA foreign_keys=OFF", ()),
    ("PRAGMA journal_mode", ()),
    ("PRAGMA journal_mode=MEMORY", ()),
    ("PRAGMA temp.encoding", ()),
    ("PRAGMA main.encoding", ("extra",)),
])
def test_oracle_control_denied_pragma_prepare_is_rejected_from_actual_attempt_without_trace_or_marker(
    unit_db, sql, parameters,
):
    before, configuration = _logical_state(unit_db), _encoding_fixture_configuration(unit_db)
    count = unit_db.total_changes
    _begin(unit_db, write=False)
    denials = []

    def authorize(action, first, second, database, context):
        if action == sqlite3.SQLITE_PRAGMA:
            denials.append((action, first, second, database))
            return sqlite3.SQLITE_DENY  # prevent configuration changes, not just observe them
        return sqlite3.SQLITE_OK

    unit_db.set_authorizer(authorize)
    try:
        swallowed = None
        try:
            unit_db.execute(sql, parameters)
        except sqlite3.DatabaseError as error:
            swallowed = error
        assert swallowed is not None and swallowed.sqlite_errorcode == sqlite3.SQLITE_AUTH
        assert denials, "native SQLITE_PRAGMA denial must precede execution/binding"
        assert unit_db.calls == [] and unit_db.sql == []
        assert unit_db.executions == [(sql, parameters, True)]
        assert unit_db.total_changes == count
        _assert_no_lifecycle(unit_db)  # lifecycle remains a separate, unchanged oracle
        with pytest.raises(AssertionError, match="forbidden attempted store statement"):
            _assert_store_statement_kinds(unit_db.executions, unit_db.sql)
    finally:
        unit_db.set_authorizer(None)
        unit_db.rollback()
    assert _logical_state(unit_db) == before
    assert _encoding_fixture_configuration(unit_db) == configuration
    assert unit_db.total_changes == count
