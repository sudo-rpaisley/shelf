"""Real managed outcome/OS-fence controls, authored while runtime gates are held.

Each pair shares ONE on-disk WAL database; B never initializes a second schema.
Pause hooks are test instrumentation only. Production critical callbacks perform
only filesystem recovery; observers contend for SQL/fcntl outside those hooks.
The injected commit-boundary fault below is NOT an authentic engine I/O fault.
"""

from contextlib import contextmanager
import os
import sqlite3
import stat
import subprocess
import sys
import threading

import pytest

from app.database import _Connection, after_commit, after_rollback
from app.services.transaction_resources import (
    TransactionResourceError,
    cover_publication_guard,
    register_transaction_resource,
)


@pytest.fixture
def wal_pair(tmp_path):
    path = tmp_path / "shared.db"
    covers = tmp_path / "covers"
    covers.mkdir()
    (covers / "1.jpg").write_bytes(b"original")
    a = sqlite3.connect(path, factory=_Connection, timeout=0, check_same_thread=False)
    assert a.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    a.execute("PRAGMA foreign_keys=ON")
    a.executescript(
        "CREATE TABLE parents(id INTEGER PRIMARY KEY); INSERT INTO parents VALUES(1);"
        "CREATE TABLE probe(id INTEGER PRIMARY KEY, value TEXT, parent INTEGER "
        "REFERENCES parents(id) DEFERRABLE INITIALLY DEFERRED);"
        "INSERT INTO probe VALUES(1, 'original', 1);"
    )
    b = sqlite3.connect(path, factory=_Connection, timeout=0)
    b.execute("PRAGMA foreign_keys=ON")
    assert b.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    try:
        yield a, b, path, covers
    finally:
        try:
            a.close()
        finally:
            b.close()


def _fence_path(covers):
    paths = list(covers.parent.glob(".shelf-cover-publication-*.lock"))
    assert len(paths) == 1
    return paths[0]


def _process_probe(covers):
    """Independent interpreter; stdlib only, never an app import in the child."""
    return subprocess.run(
        [sys.executable, "-c", """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOFOLLOW)
try:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('held')
        sys.exit(9)
    print('available')
finally:
    os.close(fd)
""", str(_fence_path(covers))],
        text=True, capture_output=True, timeout=10, check=False,
    )


def _assert_fence(covers, *, held):
    result = _process_probe(covers)
    assert result.returncode == (9 if held else 0), (result.stdout, result.stderr)
    assert result.stdout.strip() == ("held" if held else "available")


def _publish(conn, covers, label, *, undo_pause=None, calls=None, undo_failure=None, finish_failure=None):
    destination = covers / "1.jpg"
    backup = covers / f".preimage-{label}"
    calls = calls if calls is not None else []

    def finish():
        calls.append(f"finish-{label}")
        if finish_failure is not None:
            raise finish_failure
        backup.unlink()

    def undo():
        calls.append(f"undo-{label}")
        if undo_pause is not None:
            undo_pause()
        if undo_failure is not None:
            raise undo_failure
        backup.replace(destination)

    resource = register_transaction_resource(
        conn, publication_dir=covers, after_commit=finish, after_rollback=undo,
    )
    # Registration and fence ownership precede the FIRST preimage read.
    backup.write_bytes(destination.read_bytes())
    destination.write_bytes(label.encode())
    conn.execute("UPDATE probe SET value=? WHERE id=1", (label,))
    return resource, backup


@contextmanager
def _worker(operation):
    failures = []

    def run():
        try:
            operation()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield failures
    finally:
        thread.join(timeout=10)
        assert not thread.is_alive(), "outcome worker did not finish"


@pytest.mark.parametrize("outcome", ["rollback", "deferred-commit", "context-deferred-commit", "close", "context-error"])
def test_reverse_critical_undo_precedes_native_unlock_then_fence_release_then_ordinary(wal_pair, outcome):
    a, b, path, covers = wal_pair
    calls = []
    undo_entered, allow_undo = threading.Event(), threading.Event()
    ordinary_entered, allow_ordinary = threading.Event(), threading.Event()

    def pause_undo():
        undo_entered.set()
        assert allow_undo.wait(10)

    def ordinary():
        calls.append("ordinary")
        ordinary_entered.set()
        assert allow_ordinary.wait(10)

    a.execute("BEGIN IMMEDIATE")
    _publish(a, covers, "A1", calls=calls)
    a.execute("SAVEPOINT nested")
    _publish(a, covers, "A2", undo_pause=pause_undo, calls=calls)
    a.execute("RELEASE nested")
    after_commit(a, lambda: calls.append("must-not-commit"))
    after_rollback(a, ordinary)
    if outcome in ("deferred-commit", "context-deferred-commit"):
        a.execute("UPDATE probe SET parent=999 WHERE id=1")

    def resolve():
        if outcome == "deferred-commit":
            a.commit()
        elif outcome == "context-deferred-commit":
            with a:
                pass
        elif outcome == "context-error":
            with a:
                raise ValueError("body error")
        else:
            getattr(a, outcome)()

    with _worker(resolve) as failures:
        try:
            assert undo_entered.wait(10)
            assert calls == ["undo-A2"]
            assert (covers / "1.jpg").read_bytes() == b"A2"
            # Native A rollback has NOT run. Independent B cannot become writer.
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                b.execute("BEGIN IMMEDIATE")
            _assert_fence(covers, held=True)
            allow_undo.set()
            assert ordinary_entered.wait(10)
            assert calls == ["undo-A2", "undo-A1", "ordinary"]
            assert (covers / "1.jpg").read_bytes() == b"original"
            _assert_fence(covers, held=False)
            # Ordinary callbacks must not retain either SQLite or file ownership.
            b.execute("BEGIN IMMEDIATE")
            _publish(b, covers, "B")
            b.commit()
            with sqlite3.connect(path) as observer:
                assert observer.execute("SELECT value,parent FROM probe").fetchone() == ("B", 1)
            assert (covers / "1.jpg").read_bytes() == b"B"
        finally:
            allow_undo.set()
            allow_ordinary.set()
    if outcome in ("deferred-commit", "context-deferred-commit"):
        assert len(failures) == 1 and isinstance(failures[0], sqlite3.IntegrityError)
    elif outcome == "context-error":
        assert len(failures) == 1 and str(failures[0]) == "body error"
    else:
        assert failures == []
    a.close()
    assert (covers / "1.jpg").read_bytes() == b"B"
    assert sorted(p.name for p in covers.iterdir()) == ["1.jpg"]


@pytest.mark.parametrize("resolver", ["rollback", "close", "context-error", "context-suppressed-autoabort"])
def test_engine_insert_or_rollback_autoabort_blocks_replacement_scope_without_reacquiring_sqlite(wal_pair, resolver):
    a, b, path, covers = wal_pair
    a.execute("BEGIN IMMEDIATE")
    cursor = a.cursor()  # A pre-existing default cursor cannot bypass the fence.
    _publish(a, covers, "A")
    with pytest.raises(sqlite3.IntegrityError):
        a.execute("INSERT OR ROLLBACK INTO probe VALUES(1, 'duplicate', 1)")
    assert not a.in_transaction  # Actual engine auto-abort, not a simulated flag.
    _assert_fence(covers, held=True)
    for work in (
        lambda: a.execute("BEGIN IMMEDIATE"),
        lambda: a.execute("UPDATE probe SET value='lost'"),
        lambda: a.executemany("INSERT INTO parents VALUES(?)", [(2,)]),
        lambda: a.executescript("BEGIN; INSERT INTO parents VALUES(3); COMMIT;"),
        lambda: cursor.execute("BEGIN"),
        lambda: cursor.executemany("INSERT INTO parents VALUES(?)", [(4,)]),
        lambda: cursor.executescript("BEGIN; COMMIT;"),
        a.commit,
    ):
        with pytest.raises(sqlite3.ProgrammingError, match="explicit rollback"):
            work()
        assert not a.in_transaction
    # B already owns SQLite: recovery must only use the retained file fence,
    # not BEGIN IMMEDIATE, which would deadlock behind this writer waiting for it.
    b.execute("BEGIN IMMEDIATE")
    if resolver == "context-error":
        with pytest.raises(ValueError, match="resolve aborted"):
            with a:
                raise ValueError("resolve aborted")
    elif resolver == "context-suppressed-autoabort":
        with pytest.raises(sqlite3.ProgrammingError, match="explicit rollback"):
            with a:
                pass
    else:
        getattr(a, resolver)()
    assert (covers / "1.jpg").read_bytes() == b"original"
    _assert_fence(covers, held=False)
    _publish(b, covers, "B")
    b.commit()
    with sqlite3.connect(path) as observer:
        assert observer.execute("SELECT value,parent FROM probe").fetchone() == ("B", 1)
        assert observer.execute("SELECT id FROM parents").fetchall() == [(1,)]
    if resolver != "close":
        a.execute("BEGIN IMMEDIATE")
        a.rollback()
    a.close()
    assert (covers / "1.jpg").read_bytes() == b"B"


def test_successful_local_savepoint_recovery_releases_last_fence_before_unrelated_commit(wal_pair):
    a, _, path, covers = wal_pair
    calls = []
    a.execute("BEGIN IMMEDIATE")
    a.execute("SAVEPOINT publication")
    resource, _ = _publish(a, covers, "A", calls=calls)
    a.execute("ROLLBACK TO publication")
    a.execute("RELEASE publication")
    _assert_fence(covers, held=True)
    resource.rollback()
    _assert_fence(covers, held=False)
    a.execute("INSERT INTO parents VALUES(2)")
    a.commit()
    assert calls == ["undo-A"]
    assert (covers / "1.jpg").read_bytes() == b"original"
    with sqlite3.connect(path) as observer:
        assert observer.execute("SELECT value FROM probe").fetchone() == ("original",)
        assert observer.execute("SELECT id FROM parents ORDER BY id").fetchall() == [(1,), (2,)]


def test_injected_native_commit_boundary_real_rollback_unlock_is_not_an_engine_io_reproduction(wal_pair, monkeypatch):
    a, b, path, covers = wal_pair
    calls = []
    entered, resume = threading.Event(), threading.Event()

    def pause():
        entered.set()
        assert resume.wait(10)

    a.execute("BEGIN IMMEDIATE")
    _publish(a, covers, "A", undo_pause=pause, calls=calls)
    native_error = sqlite3.OperationalError("injected after real native rollback/unlock")

    def unlocked_commit_boundary():
        sqlite3.Connection.rollback(a)  # Real native unlock, deliberately injected.
        raise native_error

    monkeypatch.setattr(a, "_native_commit", unlocked_commit_boundary)
    with _worker(a.commit) as failures:
        try:
            assert entered.wait(10)
            assert not a.in_transaction
            b.execute("BEGIN IMMEDIATE")  # Native lock really IS available now.
            _assert_fence(covers, held=True)  # File publication remains unavailable.
            assert (covers / "1.jpg").read_bytes() == b"A"
        finally:
            resume.set()
    assert failures == [native_error]
    assert calls == ["undo-A"]
    _assert_fence(covers, held=False)
    _publish(b, covers, "B")
    b.commit()
    with sqlite3.connect(path) as observer:
        assert observer.execute("SELECT value FROM probe").fetchone() == ("B",)
    a.close()
    assert (covers / "1.jpg").read_bytes() == b"B"


def test_process_fence_is_stable_private_outside_served_directory_and_owner_borrows(wal_pair):
    a, _, _, covers = wal_pair
    a.execute("BEGIN IMMEDIATE")
    resource, _ = _publish(a, covers, "A")
    lock = _fence_path(covers)
    inode = lock.stat().st_ino
    assert lock.parent == covers.parent and lock not in covers.iterdir()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert not lock.is_symlink()
    _assert_fence(covers, held=True)
    with cover_publication_guard(covers / ".", conn=a):
        _assert_fence(covers, held=True)
    _assert_fence(covers, held=True)
    resource.rollback()  # Actual local undo, then last cancellation releases.
    resource.cancel()  # Idempotent and cannot unlink the shared lock inode.
    _assert_fence(covers, held=False)
    assert lock.stat().st_ino == inode
    with cover_publication_guard(covers):
        _assert_fence(covers, held=True)
    assert lock.stat().st_ino == inode
    assert sorted(p.name for p in covers.iterdir()) == ["1.jpg"]


@pytest.mark.parametrize("borrower", ["another-connection", "no-connection"])
def test_nonowner_guard_never_borrows_an_ambient_connection_fence(wal_pair, borrower):
    a, b, _, covers = wal_pair
    a.execute("BEGIN IMMEDIATE")
    resource, _ = _publish(a, covers, "A")
    attempting, entered = threading.Event(), threading.Event()

    def contender():
        attempting.set()
        with cover_publication_guard(covers, conn=b if borrower == "another-connection" else None):
            entered.set()
            (covers / "1.jpg").write_bytes(b"legacy-B")

    with _worker(contender) as failures:
        try:
            assert attempting.wait(10)
            _assert_fence(covers, held=True)
            assert not entered.wait(0.1)
            assert (covers / "1.jpg").read_bytes() == b"A"
        finally:
            resource.rollback()
        assert entered.wait(10)
    assert failures == []
    a.rollback()
    assert (covers / "1.jpg").read_bytes() == b"legacy-B"


def test_namespace_refusal_precedes_callbacks_and_last_cancel_does_not_rebind_outer_transaction(wal_pair, tmp_path):
    a, b, _, covers = wal_pair
    other = tmp_path / "other-covers"
    other.mkdir()
    calls = []
    a.execute("BEGIN IMMEDIATE")
    first, _ = _publish(a, covers, "A1")
    second, _ = _publish(a, covers, "A2")
    second.rollback()
    _assert_fence(covers, held=True)
    first.rollback()
    _assert_fence(covers, held=False)
    with pytest.raises(TransactionResourceError, match="one publication namespace"):
        register_transaction_resource(a, publication_dir=other,
            after_commit=lambda: calls.append("commit"), after_rollback=lambda: calls.append("undo"))
    with pytest.raises(TransactionResourceError, match="namespace"):
        with cover_publication_guard(other, conn=a):
            pytest.fail("cannot borrow another namespace")
    assert calls == [] and list(other.iterdir()) == []
    # Explicitly copying an owner's journal cannot confer ownership on B.
    b._transaction_resources = a._transaction_resources
    try:
        with pytest.raises(TransactionResourceError, match="connection"):
            with cover_publication_guard(covers, conn=b):
                pytest.fail("cannot steal another connection's fence")
    finally:
        b._transaction_resources = None
    a.rollback()
    a.execute("BEGIN IMMEDIATE")
    entry = register_transaction_resource(a, publication_dir=other, after_commit=lambda: None, after_rollback=lambda: None)
    entry.cancel()
    a.rollback()


@pytest.mark.parametrize("kind", ["raw", "ordinary-lists-only", "inactive-managed"])
def test_registration_requires_explicit_protected_capability_before_any_publication(wal_pair, kind):
    a, _, path, covers = wal_pair

    class ListsOnly(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._after_commit, self._after_rollback = [], []

    db = a if kind == "inactive-managed" else sqlite3.connect(path, factory=ListsOnly if kind == "ordinary-lists-only" else sqlite3.Connection)
    try:
        if kind != "inactive-managed":
            db.execute("BEGIN IMMEDIATE")
        before = {p.name: p.read_bytes() for p in covers.iterdir()}
        with pytest.raises(TransactionResourceError, match="active outer|protected commit AND rollback"):
            _publish(db, covers, "refused")
        assert {p.name: p.read_bytes() for p in covers.iterdir()} == before
        assert list(covers.parent.glob(".shelf-cover-publication-*.lock")) == []
    finally:
        if db is not a:
            db.close()


@pytest.mark.parametrize("defect", ["symlink", "world-readable", "hardlink"])
def test_unsafe_lockfile_is_refused_without_following_or_replacing_it(wal_pair, tmp_path, defect):
    a, _, _, covers = wal_pair
    with cover_publication_guard(covers):
        pass
    lock = _fence_path(covers)
    lock.unlink()  # Adversarial test setup, NEVER production cleanup.
    target = tmp_path / "not-a-lock"
    target.write_bytes(b"untouched")
    target.chmod(0o600)
    if defect == "symlink":
        lock.symlink_to(target)
    elif defect == "hardlink":
        os.link(target, lock)
    else:
        lock.write_bytes(b"wrong mode")
        lock.chmod(0o644)
    a.execute("BEGIN IMMEDIATE")
    before = (covers / "1.jpg").read_bytes()
    with pytest.raises((OSError, TransactionResourceError)):
        _publish(a, covers, "unsafe")
    assert (covers / "1.jpg").read_bytes() == before
    assert target.read_bytes() == b"untouched"
    assert getattr(a, "_transaction_resources", None) is None
    a.rollback()


def test_local_undo_failure_cannot_be_finalized_by_unrelated_commit(wal_pair):
    a, b, path, covers = wal_pair
    a.execute("BEGIN IMMEDIATE")
    calls = []
    failure = OSError("local restore failed")
    resource, backup = _publish(a, covers, "A", calls=calls, undo_failure=failure)
    with pytest.raises(OSError) as caught:
        resource.rollback()
    assert caught.value is failure
    assert backup.read_bytes() == b"original"
    _assert_fence(covers, held=True)
    a.execute("INSERT INTO parents VALUES(2)")
    with pytest.raises(TransactionResourceError, match="Local resource recovery failed") as caught:
        a.commit()
    assert caught.value.resource_cleanup_errors[0] is failure
    assert calls == ["undo-A", "undo-A"]  # Local retry + one real outer recovery.
    assert not a.in_transaction
    assert backup.read_bytes() == b"original"  # Never ran successful cleanup.
    assert (covers / "1.jpg").read_bytes() == b"A"
    _assert_fence(covers, held=False)
    with sqlite3.connect(path) as observer:
        assert observer.execute("SELECT value FROM probe").fetchone() == ("original",)
        assert observer.execute("SELECT id FROM parents").fetchall() == [(1,)]
    b.execute("BEGIN IMMEDIATE")
    b.rollback()


def test_success_cleanup_failure_attempts_all_callbacks_never_runs_undo(wal_pair):
    a, _, path, covers = wal_pair
    calls = []
    a.execute("BEGIN IMMEDIATE")
    failure = OSError("keep committed preimage on cleanup failure")
    _, backup = _publish(a, covers, "A1", calls=calls, finish_failure=failure)
    _publish(a, covers, "A2", calls=calls)

    def bad_cache():
        calls.append("bad-cache")
        raise RuntimeError("cache error")

    after_commit(a, bad_cache)
    after_commit(a, lambda: calls.append("last-cache"))
    after_rollback(a, lambda: calls.append("must-not-undo"))
    with pytest.raises(OSError) as caught:
        with a:
            pass
    assert caught.value is failure
    assert str(caught.value.resource_cleanup_errors[0]) == "cache error"
    assert calls == ["finish-A1", "finish-A2", "bad-cache", "last-cache"]
    assert backup.read_bytes() == b"original"
    _assert_fence(covers, held=False)
    a.rollback()
    a.close()
    with sqlite3.connect(path) as observer:
        assert observer.execute("SELECT value FROM probe").fetchone() == ("A2",)
    assert (covers / "1.jpg").read_bytes() == b"A2"
    assert calls == ["finish-A1", "finish-A2", "bad-cache", "last-cache"]


@pytest.mark.parametrize("outcome", ["rollback", "deferred-commit", "close", "context-error"])
def test_all_recovery_failures_still_finalize_native_outcome_and_expose_each_error(wal_pair, outcome):
    a, b, path, covers = wal_pair
    calls = []
    a.execute("BEGIN IMMEDIATE")
    _publish(a, covers, "A1", calls=calls)
    recovery_error = OSError("latest recovery failed")
    _, backup = _publish(a, covers, "A2", calls=calls, undo_failure=recovery_error)

    def bad_ordinary():
        calls.append("bad-ordinary")
        raise RuntimeError("ordinary failure")

    after_rollback(a, lambda: calls.append("last-ordinary"))
    after_rollback(a, bad_ordinary)
    body_error = ValueError("body stays primary")
    if outcome == "deferred-commit":
        a.execute("UPDATE probe SET parent=999")
        operation = a.commit
        expected = sqlite3.IntegrityError
    elif outcome == "context-error":
        def operation():
            with a:
                raise body_error
        expected = ValueError
    else:
        operation = getattr(a, outcome)
        expected = OSError
    with pytest.raises(expected) as caught:
        operation()
    if outcome == "context-error":
        assert caught.value is body_error
    elif outcome != "deferred-commit":
        assert caught.value is recovery_error
    # All callbacks attempted; native rollback/close was not skipped by errors.
    assert calls == ["undo-A2", "undo-A1", "bad-ordinary", "last-ordinary"]
    assert backup.read_bytes() == b"A1"
    assert (covers / "1.jpg").read_bytes() == b"original"
    errors = getattr(caught.value, "resource_cleanup_errors", ())
    all_errors = [caught.value, *errors]
    all_errors.extend(error for parent in errors for error in getattr(parent, "resource_cleanup_errors", ()))
    assert recovery_error in all_errors
    assert any(str(error) == "ordinary failure" for error in all_errors)
    _assert_fence(covers, held=False)
    b.execute("BEGIN IMMEDIATE")
    b.rollback()
    with sqlite3.connect(path) as observer:
        assert observer.execute("SELECT value,parent FROM probe").fetchone() == ("original", 1)
    if outcome == "close":
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            a.execute("SELECT 1")
    a.close()
    a.close()
    assert calls == ["undo-A2", "undo-A1", "bad-ordinary", "last-ordinary"]


@pytest.mark.parametrize("outcome", ["rollback", "commit"])
def test_critical_callbacks_cannot_execute_sql_or_reenter_outcomes(wal_pair, outcome):
    a, _, _, covers = wal_pair
    a.execute("BEGIN IMMEDIATE")
    calls = []

    def invalid_critical():
        # A filesystem helper may borrow its own still-held fence in this phase;
        # borrowing does NOT grant permission to run SQL or finalize recursively.
        with cover_publication_guard(covers, conn=a):
            for operation in (lambda: a.execute("SELECT 1"), a.commit, a.rollback, a.close):
                with pytest.raises(sqlite3.ProgrammingError, match="critical resource"):
                    operation()
        calls.append("critical")

    register_transaction_resource(a, publication_dir=covers,
        after_commit=invalid_critical, after_rollback=invalid_critical)
    getattr(a, outcome)()
    assert calls == ["critical"]
    _assert_fence(covers, held=False)


def test_unsupported_process_locking_fails_closed_before_preimage_or_mutation(wal_pair, monkeypatch):
    from app.services import transaction_resources

    a, _, _, covers = wal_pair
    a.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(transaction_resources, "fcntl", None)
    with pytest.raises(TransactionResourceError, match="unavailable"):
        _publish(a, covers, "unsupported")
    assert sorted(p.name for p in covers.iterdir()) == ["1.jpg"]
    assert (covers / "1.jpg").read_bytes() == b"original"
    assert getattr(a, "_transaction_resources", None) is None
    assert list(covers.parent.glob(".shelf-cover-publication-*.lock")) == []


def test_managed_native_autocommit_noop_outcomes_cannot_claim_resource_ownership(wal_pair):
    _, _, path, covers = wal_pair
    db = sqlite3.connect(path, factory=_Connection, autocommit=True)
    try:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.ProgrammingError, match="effective native commit/rollback"):
            _publish(db, covers, "refused-noop")
        assert (covers / "1.jpg").read_bytes() == b"original"
        assert sorted(p.name for p in covers.iterdir()) == ["1.jpg"]
        assert list(covers.parent.glob(".shelf-cover-publication-*.lock")) == []
    finally:
        db.close()
