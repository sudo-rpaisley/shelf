"""Real shared-WAL publication/file recovery controls; authored under runtime hold.

These tests are not an extracted model: when gates open they use the actual
managed connection, publisher and OS publication fence. Provider edges alone are
doubled; each contender connects to the SAME on-disk database and final filename.
"""

import asyncio
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _bytes(label):
    # A real JPEG, with a harmless trailing label distinguishing every writer.
    return (Path(__file__).parent / "fixtures/intake/eleven_books.jpg").read_bytes() + label.encode()


def _connect(path, *, timeout=10, factory=None):
    from app.database import _Connection

    conn = sqlite3.connect(path, timeout=timeout, factory=factory or _Connection)
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    return conn


def _observe(path):
    with closing(sqlite3.connect(path)) as observed:
        return observed.execute("SELECT cover_path, marker FROM items WHERE id=51").fetchone()


@pytest.fixture
def publication_namespace(tmp_path, monkeypatch):
    from app.services import covers, manual_cover

    directory = tmp_path / "publication-namespace" / "covers"
    directory.mkdir(parents=True)
    monkeypatch.setattr(covers, "COVERS_DIR", directory)
    monkeypatch.setattr(manual_cover, "COVERS_DIR", directory)
    destination = directory / "51.jpg"
    destination.write_bytes(_bytes("original"))
    path = tmp_path / "shared-publications.db"
    with closing(sqlite3.connect(path)) as setup:
        assert setup.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        setup.executescript(
            "CREATE TABLE parents(id INTEGER PRIMARY KEY); INSERT INTO parents VALUES(1);"
            "CREATE TABLE items(id INTEGER PRIMARY KEY, updated_at TEXT, cover_path TEXT, marker TEXT,"
            " parent_id INTEGER REFERENCES parents(id) DEFERRABLE INITIALLY DEFERRED);"
            "INSERT INTO items VALUES(51, 'original', 'covers/51.jpg', 'original', 1);"
        )
    return path, destination


@contextmanager
def _pending(label):
    from app.services import covers

    with covers.CoverStage(51) as stage:
        relative = covers.stage_uploaded_cover(stage, _bytes(label), content_type="image/jpeg")
        assert relative == f"covers/{stage.download_id}.jpg"
        yield stage, relative


def _store(conn, relative, label):
    assert conn.in_transaction and relative == "covers/51.jpg"
    conn.execute("UPDATE items SET cover_path=?, marker=? WHERE id=51", (relative, label))


def _publish(conn, label):
    from app.services import covers

    def authorize(db):
        assert db is conn and db.in_transaction
        assert db.execute("SELECT parent_id FROM items WHERE id=51").fetchone() == (1,)

    with _pending(label) as (stage, relative):
        assert covers.publish_staged_cover(conn, stage, relative, authorize=authorize,
            store=lambda db, final: _store(db, final, label)) == "covers/51.jpg"


def _thread(work, name):
    errors = []

    def run():
        try:
            work()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread, errors


def _join(worker):
    thread, errors = worker
    thread.join(15)
    assert not thread.is_alive(), f"publication worker did not finish: {thread.name}"
    if errors:
        raise errors[0]


def _pause_actual_restore(monkeypatch, destination, arrived, resume, state):
    real_replace = Path.replace

    def replace(path, target):
        if (threading.current_thread().name == "cover-publisher-A"
                and Path(target) == destination
                and path.name.startswith((".cover-backup-", ".cover-restore-"))):
            # Pause BEFORE the real destination restoration, not after undo.
            state["a_native_writer_at_restore"] = state["a_conn"].in_transaction
            arrived.set()
            assert resume.wait(10), "test did not release A's actual restoration"
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace)


@pytest.mark.parametrize("outcome", ["rollback", "close", "deferred-fk-commit"])
def test_actual_undo_keeps_native_writer_then_b_commits_same_row_and_bytes(
    publication_namespace, monkeypatch, outcome,
):
    path, destination = publication_namespace
    arrived, resume, b_requested, b_committed = (threading.Event() for _ in range(4))
    state = {}
    _pause_actual_restore(monkeypatch, destination, arrived, resume, state)

    def writer_a():
        with closing(_connect(path)) as conn:
            state["a_conn"] = conn
            _publish(conn, "A")
            assert destination.read_bytes() == _bytes("A")
            if outcome == "deferred-fk-commit":
                conn.execute("UPDATE items SET parent_id=999 WHERE id=51")
                with pytest.raises(sqlite3.IntegrityError):
                    conn.commit()  # REAL deferred-FK failure, not an injected throw.
            else:
                getattr(conn, outcome)()

    def writer_b():
        with closing(_connect(path)) as conn:
            conn.set_trace_callback(lambda sql: b_requested.set() if sql == "BEGIN IMMEDIATE" else None)
            _publish(conn, "B")
            conn.commit()
            b_committed.set()

    a = _thread(writer_a, "cover-publisher-A")
    b = None
    try:
        assert arrived.wait(10), "A never reached its actual file restoration"
        assert state["a_native_writer_at_restore"] is True
        assert destination.read_bytes() == _bytes("A")
        assert _observe(path) == ("covers/51.jpg", "original")
        # A property flag alone is insufficient: a REAL rival native writer
        # must still fail before A's restore. All connections use this WAL DB.
        with closing(_connect(path, timeout=0)) as probe:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                probe.execute("BEGIN IMMEDIATE")
            assert not probe.in_transaction
        b = _thread(writer_b, "cover-publisher-B")
        assert b_requested.wait(10), "B did not actually attempt the SQLite writer lock"
        assert not b_committed.wait(0.2)
        assert destination.read_bytes() == _bytes("A")
    finally:
        resume.set()
        _join(a)
        if b is not None:
            _join(b)
    # Observe B independently AFTER both outcomes, not via B's own connection.
    assert b_committed.is_set()
    assert _observe(path) == ("covers/51.jpg", "B")
    assert destination.read_bytes() == _bytes("B")
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("writer", ["write-upload", "save-upload", "provider", "legacy-manual", "pinned-manual"])
def test_positive_id_legacy_writer_waits_for_retained_fence_and_writes_after_recovery(
    publication_namespace, monkeypatch, writer,
):
    from app.services import covers, manual_cover, transaction_resources

    path, destination = publication_namespace
    arrived, resume, requested, entered, exited, completed = (threading.Event() for _ in range(6))
    state = {}
    _pause_actual_restore(monkeypatch, destination, arrived, resume, state)
    real_guard = transaction_resources.cover_publication_guard

    @contextmanager
    def guard(directory, *, conn=None):
        assert directory == destination.parent and conn is None
        if writer in {"provider", "legacy-manual", "pinned-manual"}:
            assert state.get("network_finished") is True
        requested.set()
        with real_guard(directory, conn=conn):
            assert destination.read_bytes() == _bytes("original")
            entered.set()
            yield
        exited.set()

    monkeypatch.setattr(covers, "cover_publication_guard", guard)
    monkeypatch.setattr(manual_cover, "cover_publication_guard", guard)

    async def response(*args, **kwargs):
        state["network_finished"] = True
        return SimpleNamespace(status_code=200, url="https://covers.openlibrary.org/51.jpg",
            content=_bytes("legacy-B"), headers={})

    async def pinned_response(url):
        state["network_finished"] = True
        return 200, None, _bytes("legacy-B")

    monkeypatch.setattr(covers.outbound, "fetch", response)
    monkeypatch.setattr(covers, "is_public_cover_url", AsyncMock(return_value=True))
    monkeypatch.setattr(manual_cover, "_fetch_once", pinned_response)

    def writer_a():
        with closing(_connect(path)) as conn:
            state["a_conn"] = conn
            _publish(conn, "A")
            conn.rollback()

    def writer_b():
        if writer == "write-upload":
            result = covers.write_uploaded_cover(51, _bytes("legacy-B"))
        elif writer == "save-upload":
            result = covers.save_uploaded_cover(51, _bytes("legacy-B"))
        elif writer == "provider":
            assert asyncio.run(covers._download("https://covers.openlibrary.org/51.jpg", destination, object())) is True
            result = "covers/51.jpg"
        elif writer == "legacy-manual":
            result = asyncio.run(covers.download_manual_cover(51, "https://public.example.test/51.jpg", object()))
        else:
            result = asyncio.run(manual_cover.download(51, "https://public.example.test/51.jpg"))
        assert result == "covers/51.jpg" and exited.is_set()
        # This legacy helper is intentionally not a transaction journal: SQL
        # begins only AFTER its file-only guard has exited, never fence -> SQL.
        with closing(_connect(path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _store(conn, result, "legacy-B")
            conn.commit()
        completed.set()

    a = _thread(writer_a, "cover-publisher-A")
    b = None
    try:
        assert arrived.wait(10)
        b = _thread(writer_b, "legacy-cover-B")
        assert requested.wait(10), "legacy writer did not reach the actual guard"
        assert not entered.wait(0.2), "legacy writer bypassed A's retained publication fence"
        assert not completed.is_set()
        assert destination.read_bytes() == _bytes("A")
    finally:
        resume.set()
        _join(a)
        if b is not None:
            _join(b)
    assert completed.is_set() and entered.is_set() and exited.is_set()
    assert _observe(path) == ("covers/51.jpg", "legacy-B")
    assert destination.read_bytes() == _bytes("legacy-B")
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("writer", ["write-upload", "save-upload", "provider", "legacy-manual", "pinned-manual"])
def test_private_negative_id_writers_do_not_take_shared_publication_fence(publication_namespace, monkeypatch, writer):
    from app.services import covers, manual_cover

    _, destination = publication_namespace
    private = destination.with_name("-61.jpg")

    def forbidden_guard(*args, **kwargs):
        pytest.fail("private negative-ID stage must not acquire a shared publication fence")

    monkeypatch.setattr(covers, "cover_publication_guard", forbidden_guard)
    monkeypatch.setattr(manual_cover, "cover_publication_guard", forbidden_guard)
    monkeypatch.setattr(covers.outbound, "fetch", AsyncMock(return_value=SimpleNamespace(
        status_code=200, url="https://covers.openlibrary.org/51.jpg", content=_bytes("private"), headers={})))
    monkeypatch.setattr(covers, "is_public_cover_url", AsyncMock(return_value=True))
    monkeypatch.setattr(manual_cover, "_fetch_once", AsyncMock(return_value=(200, None, _bytes("private"))))
    if writer == "write-upload":
        result = covers.write_uploaded_cover(-61, _bytes("private"))
    elif writer == "save-upload":
        result = covers.save_uploaded_cover(-61, _bytes("private"))
    elif writer == "provider":
        assert asyncio.run(covers._download("https://covers.openlibrary.org/51.jpg", private, object())) is True
        result = "covers/-61.jpg"
    elif writer == "legacy-manual":
        result = asyncio.run(covers.download_manual_cover(-61, "https://public.example.test/51.jpg", object()))
    else:
        result = asyncio.run(manual_cover.download(-61, "https://public.example.test/51.jpg"))
    assert result == "covers/-61.jpg"
    assert private.read_bytes() == _bytes("private")
    assert destination.read_bytes() == _bytes("original")


@pytest.mark.parametrize("writer", ["write-upload", "save-upload", "provider", "legacy-manual", "pinned-manual"])
def test_direct_writer_can_bootstrap_absent_lock_parent_only_after_network_validation(tmp_path, monkeypatch, writer):
    from app.services import covers, manual_cover

    directory = tmp_path / "not-created-data" / "covers"
    monkeypatch.setattr(covers, "COVERS_DIR", directory)
    monkeypatch.setattr(manual_cover, "COVERS_DIR", directory)
    assert not directory.parent.exists()

    async def response(*args, **kwargs):
        assert not directory.parent.exists(), "filesystem bootstrap ran before network/validation"
        return SimpleNamespace(status_code=200, url="https://covers.openlibrary.org/51.jpg",
            content=_bytes("bootstrap"), headers={})

    async def pinned_response(url):
        assert not directory.parent.exists(), "filesystem bootstrap ran before network/validation"
        return 200, None, _bytes("bootstrap")

    monkeypatch.setattr(covers.outbound, "fetch", response)
    monkeypatch.setattr(covers, "is_public_cover_url", AsyncMock(return_value=True))
    monkeypatch.setattr(manual_cover, "_fetch_once", pinned_response)
    if writer == "write-upload":
        result = covers.write_uploaded_cover(51, _bytes("bootstrap"))
    elif writer == "save-upload":
        result = covers.save_uploaded_cover(51, _bytes("bootstrap"))
    elif writer == "provider":
        assert asyncio.run(covers._download("https://covers.openlibrary.org/51.jpg", directory / "51.jpg", object())) is True
        result = "covers/51.jpg"
    elif writer == "legacy-manual":
        result = asyncio.run(covers.download_manual_cover(51, "https://public.example.test/51.jpg", object()))
    else:
        result = asyncio.run(manual_cover.download(51, "https://public.example.test/51.jpg"))
    assert result == "covers/51.jpg"
    assert (directory / "51.jpg").read_bytes() == _bytes("bootstrap")
    assert list(directory.iterdir()) == [directory / "51.jpg"]


@pytest.mark.parametrize("helper", ["write_uploaded_cover", "save_uploaded_cover"])
def test_upload_passes_explicit_owner_and_can_borrow_existing_publication_fence(
    publication_namespace, monkeypatch, helper,
):
    from app.services import covers, transaction_resources

    path, destination = publication_namespace
    real_guard = transaction_resources.cover_publication_guard
    owners = []
    with closing(_connect(path)) as conn:
        _publish(conn, "A")

        @contextmanager
        def guard(directory, *, conn=None):
            owners.append(conn)
            with real_guard(directory, conn=conn):
                yield

        monkeypatch.setattr(covers, "cover_publication_guard", guard)
        assert getattr(covers, helper)(51, _bytes("owned-upload"), db=conn) == "covers/51.jpg"
        assert owners == [conn]
        assert destination.read_bytes() == _bytes("owned-upload")
        conn.rollback()
    assert _observe(path) == ("covers/51.jpg", "original")
    assert destination.read_bytes() == _bytes("original")


@pytest.mark.parametrize("outcome", ["rollback", "close", "commit"])
def test_local_cancel_preserves_earlier_and_later_same_path_resources_for_outer_outcome(
    publication_namespace, monkeypatch, outcome,
):
    from app.services import covers

    path, destination = publication_namespace
    real_replace = Path.replace
    with closing(_connect(path)) as conn:
        _publish(conn, "A")
        with _pending("locally-refused-B") as (stage, relative):
            def replace(path, target):
                result = real_replace(path, target)
                if path == stage.path:
                    raise OSError("local B failure after actual rename")
                return result

            monkeypatch.setattr(Path, "replace", replace)
            with pytest.raises(OSError, match="local B failure"):
                covers.publish_staged_cover(conn, stage, relative, authorize=lambda db: None,
                    store=lambda db, final: _store(db, final, "locally-refused-B"))
        assert destination.read_bytes() == _bytes("A")
        assert conn.execute("SELECT marker FROM items WHERE id=51").fetchone() == ("A",)
        _publish(conn, "C")
        assert destination.read_bytes() == _bytes("C")
        getattr(conn, outcome)()
    expected = "C" if outcome == "commit" else "original"
    assert _observe(path) == ("covers/51.jpg", expected)
    assert destination.read_bytes() == _bytes(expected)
    assert list(destination.parent.iterdir()) == [destination]


def test_resource_enrollment_precedes_every_destination_and_preimage_inspection(publication_namespace, monkeypatch):
    from app.services import transaction_resources

    path, destination = publication_namespace
    real_register = transaction_resources.register_transaction_resource
    enrolled, inspected = [], []

    def register(conn, **kwargs):
        assert kwargs["publication_dir"] == destination.parent
        resource = real_register(conn, **kwargs)
        enrolled.append(resource)
        return resource

    monkeypatch.setattr(transaction_resources, "register_transaction_resource", register)
    for name in ("exists", "is_file", "is_symlink"):
        real_method = getattr(Path, name)

        def inspect(path, *args, _method=real_method, _name=name, **kwargs):
            if path == destination:
                assert enrolled, f"destination {_name} ran before fence enrollment"
                inspected.append(_name)
            return _method(path, *args, **kwargs)

        monkeypatch.setattr(Path, name, inspect)
    with closing(_connect(path)) as conn:
        _publish(conn, "A")
        assert inspected and len(enrolled) == 1
        conn.rollback()
    assert destination.read_bytes() == _bytes("original")


@pytest.mark.parametrize("kind", ["raw", "list-only"])
def test_unprotected_connections_refuse_before_destination_inspection_store_or_backup(
    publication_namespace, monkeypatch, kind,
):
    from app.services import covers
    from app.services.transaction_resources import TransactionResourceError

    class ListOnly(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._after_commit = []
            self._after_rollback = []

    path, destination = publication_namespace
    real_symlink = Path.is_symlink
    stores, inspected = [], []

    def symlink(path):
        if path == destination:
            inspected.append(path)
        return real_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", symlink)
    with closing(_connect(path, factory=ListOnly if kind == "list-only" else sqlite3.Connection)) as conn:
        with _pending("A") as (stage, relative):
            with pytest.raises(TransactionResourceError):
                covers.publish_staged_cover(conn, stage, relative, authorize=lambda db: None,
                    store=lambda db, final: stores.append(final))
            assert stage.path.read_bytes() == _bytes("A")
        assert not inspected and not stores
        conn.rollback()
    assert _observe(path) == ("covers/51.jpg", "original")
    assert destination.read_bytes() == _bytes("original")
    assert list(destination.parent.iterdir()) == [destination]


def _assert_fence_released(directory):
    from app.services.transaction_resources import cover_publication_guard

    entered = threading.Event()

    def contender():
        with cover_publication_guard(directory):
            entered.set()

    worker = _thread(contender, "local-cancel-fence-probe")
    _join(worker)
    assert entered.is_set(), "successful local undo/cancel did not release its fence"


@pytest.mark.parametrize("failure", ["registration", "preimage-copy"])
def test_registration_or_partial_preimage_failure_preserves_bytes_and_allows_unrelated_commit(
    publication_namespace, monkeypatch, failure,
):
    from app.services import covers, transaction_resources

    path, destination = publication_namespace
    if failure == "registration":
        def register(conn, **kwargs):
            # Empty closure state is safe even if acquisition/registration fails.
            raise OSError("injected registration failure")

        monkeypatch.setattr(transaction_resources, "register_transaction_resource", register)
    else:
        real_copy = covers.shutil.copyfile

        def copy(source, target, *args, **kwargs):
            if Path(source) == destination:
                Path(target).write_bytes(b"partial preimage")
                raise OSError("injected preimage-copy failure")
            return real_copy(source, target, *args, **kwargs)

        monkeypatch.setattr(covers.shutil, "copyfile", copy)
    with closing(_connect(path)) as conn:
        with pytest.raises(OSError, match="injected"):
            _publish(conn, "A")
        assert destination.read_bytes() == _bytes("original")
        assert list(destination.parent.iterdir()) == [destination]
        _assert_fence_released(destination.parent)
        conn.execute("UPDATE items SET marker='unrelated' WHERE id=51")
        conn.commit()
    assert _observe(path) == ("covers/51.jpg", "unrelated")
    assert destination.read_bytes() == _bytes("original")


@pytest.mark.parametrize("had_file", [False, True])
@pytest.mark.parametrize("boundary", ["before-rename", "after-real-rename"])
def test_final_rename_failure_locally_recovers_before_later_outer_commit(
    publication_namespace, monkeypatch, had_file, boundary,
):
    from app.services import covers

    path, destination = publication_namespace
    if not had_file:
        destination.unlink()
    real_replace = Path.replace
    with closing(_connect(path)) as conn:
        with _pending("A") as (stage, relative):
            def replace(path, target):
                if path == stage.path:
                    if boundary == "after-real-rename":
                        real_replace(path, target)
                    raise OSError(f"injected {boundary}")
                return real_replace(path, target)

            monkeypatch.setattr(Path, "replace", replace)
            with pytest.raises(OSError, match=boundary):
                covers.publish_staged_cover(conn, stage, relative, authorize=lambda db: None,
                    store=lambda db, final: _store(db, final, "A"))
        _assert_fence_released(destination.parent)
        conn.execute("UPDATE items SET marker='unrelated' WHERE id=51")
        conn.commit()
    assert _observe(path) == ("covers/51.jpg", "unrelated")
    assert destination.exists() is had_file
    if had_file:
        assert destination.read_bytes() == _bytes("original")
    assert list(destination.parent.iterdir()) == ([destination] if had_file else [])


@pytest.mark.parametrize("restore_boundary", ["before-restore", "after-real-restore"])
def test_failed_local_undo_keeps_original_backup_and_later_commit_cannot_finalize_it(
    publication_namespace, monkeypatch, restore_boundary,
):
    from app.services import covers
    from app.services.transaction_resources import TransactionResourceError

    path, destination = publication_namespace
    real_replace = Path.replace
    failing = True
    with closing(_connect(path)) as conn:
        conn.execute("UPDATE items SET marker='unrelated-before-publication' WHERE id=51")
        with _pending("A") as (stage, relative):
            def replace(path, target):
                if path == stage.path:
                    real_replace(path, target)
                    raise OSError("injected publication failure after real rename")
                if path.name.startswith(".cover-restore-") and failing:
                    if restore_boundary == "after-real-restore":
                        real_replace(path, target)
                    raise OSError(f"injected {restore_boundary}")
                return real_replace(path, target)

            monkeypatch.setattr(Path, "replace", replace)
            with pytest.raises(OSError, match=restore_boundary):
                covers.publish_staged_cover(conn, stage, relative, authorize=lambda db: None,
                    store=lambda db, final: _store(db, final, "A"))
        backups = list(destination.parent.glob(".cover-backup-*"))
        assert len(backups) == 1 and backups[0].read_bytes() == _bytes("original")
        assert _observe(path) == ("covers/51.jpg", "original")
        assert destination.read_bytes() == _bytes("original" if restore_boundary == "after-real-restore" else "A")
        # Permit recovery now, but success must remain poisoned: native COMMIT
        # must not make the unrelated row durable and discard this journal.
        failing = False
        with pytest.raises(TransactionResourceError):
            conn.commit()
        assert not conn.in_transaction
    assert _observe(path) == ("covers/51.jpg", "original")
    assert destination.read_bytes() == _bytes("original")
    assert list(destination.parent.iterdir()) == [destination]
