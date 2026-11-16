"""Real SQLite files, separate connections, barriers/events, CAS and rollback.

No sleeps, provider/auth stubs, retry-until-green loops, or in-memory substitute
for concurrency. Captured actors are test records, not authentication evidence.
The caller takes BEGIN IMMEDIATE before invoking the store and owns completion.
"""

import importlib
import json
import queue
import re
import sqlite3
import threading
import time

import pytest

from tests.test_account_preferences_schema_backup import _logical_state
from tests.test_account_preferences_store import (
    _EXPIRY, _ObservedConnection, _ObservedCursor, _actor, _assert_error,
    _assert_no_lifecycle, _assert_snapshot, _begin, _clear_observations, _json,
    _make_database, _raw_row, _recent, _seed,
)


@pytest.fixture
def store():
    return importlib.import_module("app.services.preferences")


@pytest.fixture
def native_file(db):
    """Reuse the untouched isolated application DB fixture; no feature seeding."""
    from app import database
    from tests.conftest import _insert_item

    db.execute("INSERT INTO users (id,username,password,role) "
               "VALUES (101,'Native A','capture-test-data','viewer')")
    db.execute("INSERT INTO users (id,username,password,role) "
               "VALUES (202,'Native B','capture-test-data','editor')")
    item = _insert_item(db, title="Catalogue sentinel", isbn=None)
    db.execute("INSERT INTO item_copies (item_id,copy_number,is_primary,notes) "
               "VALUES (?,1,1,'physical copy sentinel')", (item,))
    db.execute("INSERT INTO reading_log (item_id,status,notes) VALUES (?,'read','shared history')", (item,))
    db.execute("INSERT INTO user_item_events (user_id,item_id,event_type,snapshot_json) "
               "VALUES (101,?,'test-sentinel','{\"private\":false}')", (item,))
    db.execute("INSERT INTO user_item_state (user_id,item_id,notes) VALUES (101,?,'private state')", (item,))
    db.execute("INSERT INTO settings (key,value) VALUES ('preferences.test.sentinel','0')")
    db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('feature.connected_library','0')")
    db.commit()
    return database.DATABASE_PATH


def _open(path, factory=_ObservedConnection):
    conn = sqlite3.connect(str(path), timeout=10, factory=factory)
    conn.execute("PRAGMA foreign_keys=ON")
    _clear_observations(conn)
    return conn


def _capture(store, conn, uid=101, expiry=_EXPIRY):
    row = conn.execute("SELECT id,username,role,token_version FROM users WHERE id=?", (uid,)).fetchone()
    return store.PreferenceActor(row["id"], row["username"], row["role"], row["token_version"], expiry)


def _without_preferences(conn):
    schema, data = _logical_state(conn)
    return schema, tuple((table, rows) for table, rows in data if table != "library_preferences")


def _safe_state_with_undecodable_revision(conn):
    """Never SELECT * or raw revision from the intentionally corrupt table."""
    schema = tuple(tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM main.sqlite_master ORDER BY type,name"
    ))
    other_rows = []
    for kind, name, _table, _sql in schema:
        if kind == "table" and name != "library_preferences":
            quoted = '"' + name.replace('"', '""') + '"'
            other_rows.append((name, tuple(sorted((tuple(row) for row in conn.execute(
                f"SELECT * FROM {quoted}"
            )), key=repr))))
    preferences = tuple(tuple(row) for row in conn.execute(
        "SELECT user_id, typeof(values_json), hex(CAST(values_json AS BLOB)), "
        "typeof(revision), hex(CAST(revision AS BLOB)), typeof(updated_at), "
        "hex(CAST(updated_at AS BLOB)) FROM library_preferences ORDER BY user_id"
    ))
    return schema, tuple(other_rows), preferences


def _mutate(store, conn, actor, expected, kind, size):
    if kind == "save":
        return store.save_preferences(conn, actor, expected, {"item_size_percent": size})
    return store.reset_preferences(conn, actor, expected, ("item_size_percent",))


def _join(thread):
    thread.join(15)
    assert not thread.is_alive(), "native SQLite thread failed to terminate"


def _thread_error(results):
    outcome = results.get_nowait()
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


@pytest.mark.parametrize("initial,winner_kind,loser_kind", [
    (0, "save", "save"), (4, "save", "save"), (4, "save", "reset"), (4, "reset", "reset"),
])
def test_native_concurrent_create_save_reset_has_one_winner_and_unchanged_stale_loser(
    store, native_file, initial, winner_kind, loser_kind,
):
    winner = _open(native_file)
    if initial:
        _seed(winner, {"item_size_percent": 50, "opaque": [False, 0, None]}, revision=initial, uid=101)
        _seed(winner, {"item_size_percent": 60}, revision=initial, uid=202)
    actor = _capture(store, winner)
    other_before, surrounding = _raw_row(winner, 202), _without_preferences(winner)
    barrier, attempting, acquired = threading.Barrier(2), threading.Event(), threading.Event()
    results = queue.Queue()
    _begin(winner)

    def stale_writer():
        loser = _open(native_file)
        try:
            def trace(sql):
                loser.sql.append(sql)
                if sql.strip().upper() == "BEGIN IMMEDIATE":
                    attempting.set()
            loser.set_trace_callback(trace)
            barrier.wait(10)
            loser.execute("BEGIN IMMEDIATE")
            acquired.set()
            before = _raw_row(loser, 101)
            _clear_observations(loser)
            count = loser.total_changes
            with pytest.raises(store.PreferenceStoreError) as error:
                _mutate(store, loser, actor, initial, loser_kind, 80)
            _assert_error(error.value, "revision_conflict")
            _assert_no_lifecycle(loser)
            assert loser.total_changes == count
            assert _raw_row(loser, 101) == before
            results.put((before, _raw_row(loser, 202)))
        except BaseException as error:
            results.put(error)
        finally:
            loser.rollback()
            loser.close()

    thread = threading.Thread(target=stale_writer, name="stale-preference-writer")
    thread.start()
    try:
        barrier.wait(10)
        assert attempting.wait(10), "second writer never reached native BEGIN IMMEDIATE"
        assert not acquired.is_set(), "caller lock must exclude the stale writer"
        assert _mutate(store, winner, actor, initial, winner_kind, 70) == initial + 1
        _assert_no_lifecycle(winner)
        winning_row = _raw_row(winner, 101)
        winner.commit()
    finally:
        winner.rollback()
        _join(thread)
    try:
        assert acquired.is_set()
        assert _thread_error(results) == (winning_row, other_before)
        assert _raw_row(winner, 101) == winning_row
        assert _raw_row(winner, 202) == other_before
        assert _without_preferences(winner) == surrounding
        assert winning_row[2] == initial + 1
        expected_size = 70 if winner_kind == "save" else 100
        assert json.loads(winning_row[1])["item_size_percent"] == expected_size
    finally:
        winner.close()


@pytest.mark.parametrize("change", ["username", "role", "token_version", "delete", "expiry"])
def test_native_waiting_writer_rechecks_actor_after_acquiring_lock(store, native_file, monkeypatch, change):
    holder = _open(native_file)
    _seed(holder, {"opaque": [False, 0, None]}, revision=4, uid=101)
    now = [2_000_000_000]
    monkeypatch.setattr(time, "time", lambda: now[0])
    captured = _capture(store, holder, expiry=2_000_000_001)
    before = _raw_row(holder, 101)
    barrier, attempting, acquired = threading.Barrier(2), threading.Event(), threading.Event()
    results = queue.Queue()
    _begin(holder)

    def request_captured_before_waiting():
        conn = _open(native_file)
        try:
            def trace(sql):
                conn.sql.append(sql)
                if sql.strip().upper() == "BEGIN IMMEDIATE":
                    attempting.set()
            conn.set_trace_callback(trace)
            barrier.wait(10)
            conn.execute("BEGIN IMMEDIATE")
            acquired.set()
            _clear_observations(conn)
            with pytest.raises(store.PreferenceStoreError) as error:
                store.save_preferences(conn, captured, 4, {})
            _assert_error(error.value, "actor_changed")
            _assert_no_lifecycle(conn)
            assert conn.total_changes == 0
            results.put(_raw_row(conn, 101))
        except BaseException as error:
            results.put(error)
        finally:
            conn.rollback()
            conn.close()

    thread = threading.Thread(target=request_captured_before_waiting, name="actor-change-waiter")
    thread.start()
    try:
        barrier.wait(10)
        assert attempting.wait(10)
        assert not acquired.is_set()
        if change == "expiry":
            now[0] = 2_000_000_001
        elif change == "delete":
            holder.execute("DELETE FROM users WHERE id=101")
        else:
            value = {"username": "renamed while waiting", "role": "admin", "token_version": 2}[change]
            holder.execute(f"UPDATE users SET {change}=? WHERE id=101", (value,))
        holder.commit()
    finally:
        holder.rollback()
        _join(thread)
    try:
        expected = None if change == "delete" else before
        assert _thread_error(results) == expected
        assert _raw_row(holder, 101) == expected
    finally:
        holder.close()


def test_validation_time_expiry_uses_dynamic_stdlib_clock_and_refuses_pre_dml(store, native_file, monkeypatch):
    conn = _open(native_file)
    try:
        conn.execute("INSERT INTO library_saved_views (id,user_id,name,filters_json) "
                     "VALUES (7,101,'owned view','{}')")
        conn.commit()
        _seed(conn, {"recent": _recent()}, revision=4, uid=101)
        before = _raw_row(conn, 101)
        clock = [2_000_000_000]
        monkeypatch.setattr(time, "time", lambda: clock[0])
        actor = _capture(store, conn, expiry=2_000_000_001)
        validation_reached = []

        def expire_during_owned_view_validation(connection, sql, parameters):
            if re.search(r"\bFROM\s+library_saved_views\b", sql, re.I):
                validation_reached.append((str(parameters[0]), parameters[1]))
                clock[0] = 2_000_000_001

        conn.on_execute = expire_during_owned_view_validation
        _begin(conn)
        with pytest.raises(store.PreferenceStoreError) as error:
            store.save_preferences(conn, actor, 4, {"default_saved_view": "7"})
        _assert_error(error.value, "actor_changed")
        assert validation_reached == [("7", 101)]
        assert conn.total_changes == 2  # fixture view + preference row, no store DML
        assert not [sql for sql in conn.sql if re.match(r"\s*(?:INSERT|UPDATE|DELETE)\b", sql, re.I)]
        _assert_no_lifecycle(conn)
        assert _raw_row(conn, 101) == before
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("kind,initial", [("save", 0), ("save", 4), ("reset", 4)])
def test_returned_revision_is_not_a_commit_receipt_and_caller_rollback_restores_everything(
    store, native_file, kind, initial,
):
    writer, reader = _open(native_file), _open(native_file)
    try:
        if initial:
            _seed(writer, {"item_size_percent": 50, "opaque": False}, revision=initial, uid=101)
        actor = _capture(store, writer)
        before, surrounding = _raw_row(reader, 101), _without_preferences(reader)
        _begin(writer)
        revision = _mutate(store, writer, actor, initial, kind, 70)
        assert type(revision) is int and revision == initial + 1
        _assert_no_lifecycle(writer)
        assert _raw_row(writer, 101)[2] == revision
        assert _raw_row(reader, 101) == before, "uncommitted return is not durable success"
        writer.rollback()
        assert _raw_row(reader, 101) == before
        assert _raw_row(writer, 101) == before
        assert _without_preferences(reader) == surrounding
    finally:
        writer.rollback()
        writer.close()
        reader.close()


def test_native_caller_commit_can_fail_after_returned_revision_without_durable_success(store, tmp_path):
    # A rollback-journal file gives a genuine native commit failure while a
    # reader holds SHARED. This is not a fabricated exception or WAL claim.
    path = tmp_path / "commit-failure.db"
    writer = _make_database(path)
    reader = _open(path)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        _seed(writer, {"opaque": [False, 0, None]}, revision=4)
        before = _raw_row(reader)
        reader.execute("BEGIN")
        assert _raw_row(reader) == before
        writer.execute("PRAGMA busy_timeout=0")
        _begin(writer)
        assert store.save_preferences(writer, _actor(store), 4, {"item_size_percent": 70}) == 5
        _assert_no_lifecycle(writer)
        assert _raw_row(writer)[2] == 5
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            writer.commit()  # caller completion, outside the store
        assert writer.in_transaction
        assert _raw_row(reader) == before
        writer.rollback()
        reader.rollback()
        assert _raw_row(reader) == before
        assert _raw_row(writer) == before
    finally:
        writer.rollback()
        writer.close()
        reader.rollback()
        reader.close()


def test_equal_revisions_never_retarget_captured_a_to_later_browser_account_b(store, native_file):
    conn = _open(native_file)
    try:
        _seed(conn, {"opaque": "A"}, revision=4, uid=101)
        _seed(conn, {"opaque": "B"}, revision=4, uid=202)
        dispatched_a = _capture(store, conn, 101)
        later_browser_account_b = _capture(store, conn, 202)
        assert dispatched_a.id != later_browser_account_b.id
        before_b = _raw_row(conn, 202)
        _begin(conn)
        assert store.save_preferences(conn, dispatched_a, 4, {"item_size_percent": 70}) == 5
        _assert_no_lifecycle(conn)
        conn.commit()
        assert json.loads(_raw_row(conn, 101)[1]) == {"opaque": "A", "item_size_percent": 70}
        assert _raw_row(conn, 202) == before_b
        # This does not assert that logout revokes A, or that a later consumer
        # can use view ownership as catalogue authority.
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("initial", [0, 4])
def test_mutation_is_one_literal_conditional_statement_not_upsert_or_fallback(store, native_file, initial):
    conn = _open(native_file)
    try:
        if initial:
            _seed(conn, {"opaque": False}, revision=initial, uid=101)
        actor = _capture(store, conn)
        _begin(conn)
        assert store.save_preferences(conn, actor, initial, {"item_size_percent": 70}) == initial + 1
        _assert_no_lifecycle(conn)
        writes = [(sql, parameters) for sql, parameters, active in conn.executions
                  if re.match(r"\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b", sql, re.I)]
        assert len(writes) == 1
        sql, parameters = writes[0]
        compact = re.sub(r"\s+", "", sql).removesuffix(";").upper()
        if initial:
            assert compact == (
                "UPDATElibrary_preferencesSETvalues_json=?,revision=?,updated_at=datetime('now')"
                "WHEREuser_id=?ANDrevision=?"
            ).upper()
            assert parameters[1:] == (5, 101, 4)
        else:
            assert compact == "INSERTINTOlibrary_preferences(user_id,values_json,revision)VALUES(?,?,1)".upper()
            assert parameters[0] == 101 and len(parameters) == 2
        assert all(active for sql, _parameters, active in conn.executions)
        conn.commit()
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("initial", [0, 4])
@pytest.mark.parametrize("failure", ["ignore", "abort", "rowcount-two"])
def test_native_write_failure_and_exactly_one_row_invariant_require_caller_rollback(
    store, native_file, initial, failure,
):
    class WrongCountCursor(_ObservedCursor):
        @property
        def rowcount(self):
            if self.description is None:
                return 2
            return super().rowcount

    class WrongCountConnection(_ObservedConnection):
        def cursor(self, factory=WrongCountCursor):
            return super().cursor(factory)

    conn = _open(native_file, factory=WrongCountConnection if failure == "rowcount-two" else _ObservedConnection)
    witness = _open(native_file)
    try:
        if initial:
            _seed(conn, {"opaque": [False, 0, None]}, revision=initial, uid=101)
        actor = _capture(store, conn)
        if failure != "rowcount-two":
            operation = "UPDATE" if initial else "INSERT"
            action = "IGNORE" if failure == "ignore" else "ABORT,'injected_native_write_failure'"
            conn.execute(f"CREATE TRIGGER injected_write_fault BEFORE {operation} ON library_preferences "
                         f"BEGIN SELECT RAISE({action}); END")
            conn.commit()
        before = _raw_row(witness, 101)
        _begin(conn)
        expected_type = sqlite3.IntegrityError if failure == "abort" else store.PreferenceStoreError
        with pytest.raises(expected_type) as error:
            store.save_preferences(conn, actor, initial, {"item_size_percent": 70})
        if failure == "abort":
            assert str(error.value) == "injected_native_write_failure"
            assert not isinstance(error.value, store.PreferenceStoreError)
        else:
            _assert_error(error.value, "cas_invariant_failed")
        _assert_no_lifecycle(conn)
        assert _raw_row(witness, 101) == before
        if failure == "rowcount-two":
            assert _raw_row(conn, 101)[2] == initial + 1, "fault occurred after real native DML"
        else:
            assert _raw_row(conn, 101) == before
        conn.rollback()
        assert _raw_row(conn, 101) == before
        assert _raw_row(witness, 101) == before
    finally:
        conn.rollback()
        conn.close()
        witness.close()


def test_schema_and_native_busy_errors_propagate_not_defaults_or_success(store, native_file):
    holder, caller = _open(native_file), _open(native_file)
    try:
        actor = _capture(store, caller)
        holder.execute("BEGIN IMMEDIATE")
        caller.execute("PRAGMA busy_timeout=0")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            caller.execute("BEGIN IMMEDIATE")
        assert not caller.in_transaction
        holder.rollback()
        caller.execute("DROP TABLE library_preferences")
        caller.commit()
        for operation in ("load", "save"):
            _begin(caller, write=operation == "save")
            with pytest.raises(sqlite3.OperationalError, match="no such table") as error:
                if operation == "load":
                    store.load_preferences(caller, actor)
                else:
                    store.save_preferences(caller, actor, 0, {})
            assert not isinstance(error.value, store.PreferenceStoreError)
            _assert_no_lifecycle(caller)
            caller.rollback()
        # Dropping here is disposable fault injection, not upgrade history or
        # permission for a store to create a missing table.
    finally:
        holder.rollback()
        holder.close()
        caller.rollback()
        caller.close()


@pytest.mark.parametrize("operation", ["load", "save", "reset"])
def test_undecodable_text_revision_is_classified_without_masking_readable_fields_or_writing(
    store, native_file, operation,
):
    conn = _open(native_file)
    try:
        assert conn.text_factory is str, "ordinary default SQLite TEXT decoding is required"
        actor = _capture(store, conn)
        # Deliberate defensive-corruption fixture, NOT evidence that canonical
        # constraints permit this through ordinary writes. Restore enforcement
        # before the store call; only the revision is corrupt, not actor/JSON.
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("INSERT INTO library_preferences (user_id,values_json,revision,updated_at) "
                     "VALUES (101, '{\"item_size_percent\":80}', CAST(X'ff' AS TEXT), 'raw-timestamp')")
        conn.commit()
        conn.execute("PRAGMA ignore_check_constraints=OFF")
        assert conn.execute("PRAGMA ignore_check_constraints").fetchone()[0] == 0
        assert tuple(conn.execute(
            "SELECT typeof(revision), hex(CAST(revision AS BLOB)), values_json "
            "FROM library_preferences WHERE user_id=101"
        ).fetchone()) == ("text", "FF", '{"item_size_percent":80}')
        before = _safe_state_with_undecodable_revision(conn)
        count = conn.total_changes
        _begin(conn, write=operation != "load")
        if operation == "load":
            _assert_snapshot(store.load_preferences(conn, actor), (None, (1, "mixed", ()), False, 80, "", (
                "stored_revision_corrupt", "recent_absent",
            )))
        else:
            with pytest.raises(store.PreferenceStoreError) as error:
                if operation == "save":
                    store.save_preferences(conn, actor, 0, {"item_size_percent": 70})
                else:
                    store.reset_preferences(conn, actor, 0, ("item_size_percent",))
            _assert_error(error.value, "stored_revision_corrupt")
        assert conn.text_factory is str
        _assert_no_lifecycle(conn)
        assert conn.total_changes == count
        assert not [sql for sql in conn.sql if re.match(r"\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b", sql, re.I)]
        assert _safe_state_with_undecodable_revision(conn) == before
        conn.rollback()
        assert _safe_state_with_undecodable_revision(conn) == before
    finally:
        conn.rollback()
        conn.close()


def test_oracle_control_bad_revision_fixture_uses_default_decoder_and_safe_byte_snapshot(tmp_path):
    conn = _make_database(tmp_path / "bad-revision-control.db", permissive=True)
    try:
        assert conn.text_factory is str
        conn.execute("INSERT INTO library_preferences VALUES "
                     "(1, '{\"item_size_percent\":80}', CAST(X'ff' AS TEXT), 'raw-timestamp')")
        conn.commit()
        before = _safe_state_with_undecodable_revision(conn)
        assert before[2] == ((
            1, "text", "7B226974656D5F73697A655F70657263656E74223A38307D", "text", "FF", "text",
            "7261772D74696D657374616D70",
        ),)
        with pytest.raises(sqlite3.OperationalError, match="decode"):
            conn.execute("SELECT revision FROM library_preferences WHERE user_id=1").fetchone()
        assert _safe_state_with_undecodable_revision(conn) == before
        assert conn.execute("SELECT values_json FROM library_preferences WHERE user_id=1").fetchone()[0] == (
            '{"item_size_percent":80}'
        )
    finally:
        conn.rollback()
        conn.close()


def test_read_snapshot_remains_on_caller_connection_across_other_committed_writer(store, native_file):
    reader, writer = _open(native_file), _open(native_file)
    try:
        actor = _capture(store, reader)
        _seed(writer, {"recent": _recent("family"), "item_size_percent": 50}, revision=4, uid=101)
        _begin(reader, write=False)
        first = store.load_preferences(reader, actor)
        _assert_snapshot(first, (4, (1, "family", ()), False, 50, "", ()))
        _begin(writer)
        assert store.save_preferences(writer, actor, 4, {"item_size_percent": 70}) == 5
        _assert_no_lifecycle(writer)
        writer.commit()
        second = store.load_preferences(reader, actor)
        _assert_snapshot(second, (4, (1, "family", ()), False, 50, "", ()))
        _assert_no_lifecycle(reader)
        reader.rollback()
        _begin(reader, write=False)
        _assert_snapshot(store.load_preferences(reader, actor), (5, (1, "family", ()), False, 70, "", ()))
        _assert_no_lifecycle(reader)
    finally:
        reader.rollback()
        reader.close()
        writer.rollback()
        writer.close()


def test_saved_view_owner_query_is_literal_and_never_reads_names_or_executes_filters(store, native_file):
    conn = _open(native_file)
    try:
        conn.execute("INSERT INTO library_saved_views (id,user_id,name,filters_json) "
                     "VALUES (7,101,'private owned name','SELECT destructive garbage, not JSON')")
        conn.commit()
        actor = _capture(store, conn)
        reads = []

        def authorize(action, table, column, database, context):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        before = _without_preferences(conn)
        _begin(conn)
        reads.clear()
        assert store.save_preferences(conn, actor, 0, {"default_saved_view": "7"}) == 1
        view_queries = [(sql, parameters) for sql, parameters, _active in conn.executions
                        if re.search(r"\bFROM\s+library_saved_views\b", sql, re.I)]
        assert len(view_queries) == 1
        sql, parameters = view_queries[0]
        assert re.sub(r"\s+", "", sql).removesuffix(";").upper() == (
            "SELECT1FROMlibrary_saved_viewsWHEREid=?ANDuser_id=?"
        ).upper()
        assert parameters in ((7, 101), ("7", 101))
        assert not {(table, column) for table, column in reads
                    if table == "library_saved_views" and column in {"name", "filters_json"}}
        assert not {table for table, _column in reads} & {"items", "item_copies", "user_item_events"}
        _assert_no_lifecycle(conn)
        conn.commit()
        assert _without_preferences(conn) == before
        _begin(conn, write=False)
        _assert_snapshot(store.load_preferences(conn, actor), (1, (1, "mixed", ()), False, 100, "7", ("recent_absent",)))
        _assert_no_lifecycle(conn)
    finally:
        conn.rollback()
        conn.close()


def test_foreign_and_absent_view_refusals_are_identical_no_names_no_revision_writes(store, native_file, caplog):
    conn = _open(native_file)
    try:
        conn.execute("INSERT INTO library_saved_views (id,user_id,name,filters_json) "
                     "VALUES (8,202,'FOREIGN_PRIVATE_NAME','{\"q\":\"private filter\"}')")
        conn.commit()
        _seed(conn, {"opaque": False}, revision=4, uid=101)
        actor, before = _capture(store, conn), _logical_state(conn)
        errors = []
        for view in ("8", "9"):
            _begin(conn)
            count = conn.total_changes
            with pytest.raises(store.PreferenceStoreError) as error:
                store.save_preferences(conn, actor, 4, {"default_saved_view": view})
            _assert_error(error.value, "default_saved_view_unavailable")
            _assert_no_lifecycle(conn)
            assert conn.total_changes == count
            errors.append((type(error.value), error.value.args, vars(error.value)))
            conn.rollback()
            assert _logical_state(conn) == before
        assert errors[0] == errors[1]
        assert "FOREIGN_PRIVATE_NAME" not in caplog.text
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize("reference", ["8", "9"])
def test_stored_foreign_and_absent_view_have_same_effective_read_and_no_fallback_repair(store, native_file, reference):
    conn = _open(native_file)
    try:
        conn.execute("INSERT INTO library_saved_views (id,user_id,name,filters_json) "
                     "VALUES (8,202,'foreign private name','private nonexecuted filters')")
        conn.commit()
        document = {"default_saved_view": reference, "opaque": [False, 0, None]}
        _seed(conn, document, revision=4, uid=101)
        actor, before = _capture(store, conn), _raw_row(conn, 101)
        _begin(conn, write=False)
        _assert_snapshot(store.load_preferences(conn, actor), (4, (1, "mixed", ()), False, 100, "", (
            "recent_absent", "default_saved_view_unavailable",
        )))
        _assert_no_lifecycle(conn)
        assert conn.total_changes == 2
        conn.rollback()
        assert _raw_row(conn, 101) == before
        _begin(conn)
        assert store.save_preferences(conn, actor, 4, {"item_size_percent": 70}) == 5
        _assert_no_lifecycle(conn)
        conn.commit()
        assert json.loads(_raw_row(conn, 101)[1]) == {**document, "item_size_percent": 70}
    finally:
        conn.rollback()
        conn.close()


def test_saved_view_deletion_has_no_hidden_preference_revision_or_json_cleanup(store, native_file):
    conn, deleting = _open(native_file), _open(native_file)
    try:
        conn.execute("INSERT INTO library_saved_views (id,user_id,name,filters_json) VALUES (7,101,'owned','{}')")
        conn.commit()
        _seed(conn, {"default_saved_view": "7", "opaque": [False, 0, None]}, revision=4, uid=101)
        actor, before = _capture(store, conn), _raw_row(conn, 101)
        deleting.execute("BEGIN IMMEDIATE")
        assert deleting.execute("DELETE FROM library_saved_views WHERE id=7 AND user_id=101").rowcount == 1
        assert deleting.total_changes == 1, "view deletion must not trigger hidden preference writes"
        deleting.commit()
        assert _raw_row(conn, 101) == before
        _begin(conn, write=False)
        _assert_snapshot(store.load_preferences(conn, actor), (4, (1, "mixed", ()), False, 100, "", (
            "recent_absent", "default_saved_view_unavailable",
        )))
        _assert_no_lifecycle(conn)
        conn.rollback()
        assert _raw_row(conn, 101) == before
        _begin(conn)
        assert store.save_preferences(conn, actor, 4, {"item_size_percent": 70}) == 5
        _assert_no_lifecycle(conn)
        conn.commit()
        assert json.loads(_raw_row(conn, 101)[1]) == {
            "default_saved_view": "7", "opaque": [False, 0, None], "item_size_percent": 70,
        }
    finally:
        conn.rollback()
        conn.close()
        deleting.rollback()
        deleting.close()


@pytest.mark.parametrize("view_owner", [101, 202])
def test_view_removed_while_request_waits_is_unavailable_under_caller_lock(store, native_file, view_owner):
    holder = _open(native_file)
    holder.execute("INSERT INTO library_saved_views (id,user_id,name,filters_json) VALUES (7,?,'private','{}')", (view_owner,))
    holder.commit()
    _seed(holder, {"opaque": False}, revision=4, uid=101)
    actor, before = _capture(store, holder), _raw_row(holder, 101)
    barrier, attempting, acquired = threading.Barrier(2), threading.Event(), threading.Event()
    results = queue.Queue()
    _begin(holder)

    def waiting_save():
        conn = _open(native_file)
        try:
            def trace(sql):
                conn.sql.append(sql)
                if sql.strip().upper() == "BEGIN IMMEDIATE":
                    attempting.set()
            conn.set_trace_callback(trace)
            barrier.wait(10)
            conn.execute("BEGIN IMMEDIATE")
            acquired.set()
            _clear_observations(conn)
            with pytest.raises(store.PreferenceStoreError) as error:
                store.save_preferences(conn, actor, 4, {"default_saved_view": "7"})
            _assert_error(error.value, "default_saved_view_unavailable")
            _assert_no_lifecycle(conn)
            assert conn.total_changes == 0
            results.put(_raw_row(conn, 101))
        except BaseException as error:
            results.put(error)
        finally:
            conn.rollback()
            conn.close()

    thread = threading.Thread(target=waiting_save, name="view-deletion-waiter")
    thread.start()
    try:
        barrier.wait(10)
        assert attempting.wait(10)
        assert not acquired.is_set()
        holder.execute("DELETE FROM library_saved_views WHERE id=7")
        holder.commit()
    finally:
        holder.rollback()
        _join(thread)
    try:
        assert _thread_error(results) == before
        assert _raw_row(holder, 101) == before
    finally:
        holder.close()


def test_committed_save_and_reset_leave_all_catalogue_copy_history_settings_rows_unchanged(store, native_file):
    conn = _open(native_file)
    try:
        _seed(conn, {"opaque": [False, 0, None], "shelves": []}, revision=4, uid=101)
        _seed(conn, {"opaque": "other account"}, revision=4, uid=202)
        actor, other, surrounding = _capture(store, conn), _raw_row(conn, 202), _without_preferences(conn)
        _begin(conn)
        assert store.save_preferences(conn, actor, 4, {"recent": _recent("custom"), "item_size_percent": 70}) == 5
        _assert_no_lifecycle(conn)
        conn.commit()
        _begin(conn)
        assert store.reset_preferences(conn, actor, 5, ("recent", "item_size_percent", "default_saved_view")) == 6
        _assert_no_lifecycle(conn)
        conn.commit()
        assert _raw_row(conn, 202) == other
        assert _without_preferences(conn) == surrounding
        assert json.loads(_raw_row(conn, 101)[1]) == {
            "opaque": [False, 0, None], "shelves": [], "recent": _recent(),
            "item_size_percent": 100, "default_saved_view": "",
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.rollback()
        conn.close()


def test_oracle_control_native_lock_wait_and_conditional_cas_are_real(tmp_path):
    """Native harness control without importing an absent implementation."""
    path = tmp_path / "lock-control.db"
    holder = _make_database(path)
    holder.execute("PRAGMA journal_mode=WAL")
    _seed(holder, {"opaque": False}, revision=1)
    barrier, attempting, acquired = threading.Barrier(2), threading.Event(), threading.Event()
    results = queue.Queue()
    holder.execute("BEGIN IMMEDIATE")

    def stale_native_sql():
        conn = _open(path)
        try:
            def trace(sql):
                if sql.strip().upper() == "BEGIN IMMEDIATE":
                    attempting.set()
            conn.set_trace_callback(trace)
            barrier.wait(10)
            conn.execute("BEGIN IMMEDIATE")
            acquired.set()
            before = _raw_row(conn)
            cursor = conn.execute(
                "UPDATE library_preferences SET values_json=?, revision=? "
                "WHERE user_id=? AND revision=?", ('{"opaque":true}', 2, 1, 1),
            )
            results.put((cursor.rowcount, before, _raw_row(conn), conn.total_changes))
        except BaseException as error:
            results.put(error)
        finally:
            conn.rollback()
            conn.close()

    thread = threading.Thread(target=stale_native_sql, name="native-cas-control")
    thread.start()
    try:
        barrier.wait(10)
        assert attempting.wait(10) and not acquired.is_set()
        assert holder.execute("UPDATE library_preferences SET revision=2 WHERE user_id=1 AND revision=1").rowcount == 1
        winning = _raw_row(holder)
        holder.commit()
    finally:
        holder.rollback()
        _join(thread)
    try:
        assert acquired.is_set()
        assert _thread_error(results) == (0, winning, winning, 0)
    finally:
        holder.close()
