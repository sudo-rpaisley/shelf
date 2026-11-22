"""Real-connection regression sources; execution is held until parent freeze.

The cross-writer cases use the actual archive journal and canonical publisher,
one on-disk WAL database, and a separate native observer. Minimal SQL fixtures
isolate publication ordering; they are not whole-import/schema/HTTP qualification.
"""

import json
import sqlite3
import struct
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest


ORIGINAL = b"original hand-picked archive fence bytes"
A_BYTES = b"writer A pending archive fence bytes"
B_BYTES = b"writer B committed archive fence bytes"
SIBLING_BYTES = b"writer B committed sibling cover bytes"


def _reader(content):
    return SimpleNamespace(read_cover=lambda name: content)


def _publish(conn, kind, item_id, content):
    from app.services import archive, covers

    if kind == "archive":
        @archive._atomic_archive_apply
        def apply(db):
            db.execute("UPDATE items SET title=?,cover_path=? WHERE id=?",
                       ("A" if content == A_BYTES else "B", f"covers/{item_id}.jpg", item_id))
            archive._install_cover(_reader(content), item_id, "covers/source.jpg")

        apply(conn)
    else:
        with covers.CoverStage(item_id) as stage:
            stage.path.write_bytes(content)

            def store(db, published):
                db.execute("UPDATE items SET title=?,cover_path=? WHERE id=?",
                           ("A" if content == A_BYTES else "B", published, item_id))

            covers.publish_staged_cover(conn, stage, f"covers/{stage.download_id}.jpg",
                                        authorize=lambda db: None, store=store)


def _wal_fixture(tmp_path, monkeypatch):
    from app import config
    from app.database import _Connection
    from app.services import covers

    directory = tmp_path / "archive-publication"
    directory.mkdir()
    monkeypatch.setattr(config, "COVERS_DIR", directory)
    monkeypatch.setattr(covers, "COVERS_DIR", directory)
    path = tmp_path / "archive-publication.db"
    conn = sqlite3.connect(path, factory=_Connection, check_same_thread=False)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        "CREATE TABLE items(id INTEGER PRIMARY KEY,title TEXT,cover_path TEXT,updated_at TEXT);"
        "CREATE TABLE child(parent INTEGER REFERENCES items(id) DEFERRABLE INITIALLY DEFERRED);"
        "INSERT INTO items VALUES(1,'original','covers/1.jpg',NULL);"
        "INSERT INTO items VALUES(2,'original sibling','covers/2.jpg',NULL);"
    )
    conn.commit()
    (directory / "1.jpg").write_bytes(ORIGINAL)
    (directory / "2.jpg").write_bytes(b"original sibling")
    return conn, path, directory


@pytest.mark.parametrize("a_kind", ["archive", "canonical"])
@pytest.mark.parametrize("outcome", ["rollback", "auto_abort", "deferred_commit_failure", "close",
                                     "injected_real_unlock_commit_failure"])
def test_archive_and_canonical_cross_writer_retains_fence_until_undo_finishes(
    tmp_path, monkeypatch, a_kind, outcome,
):
    from app.database import _Connection
    from app.services import archive

    a, database, directory = _wal_fixture(tmp_path, monkeypatch)
    paused, release, restored, b_attempted, b_locked, b_done = (Event() for _ in range(6))
    native_active = []
    _publish(a, a_kind, 1, A_BYTES)
    assert a.in_transaction and (directory / "1.jpg").read_bytes() == A_BYTES
    assert a._after_commit == a._after_rollback == []
    fence = a._transaction_resources.fence
    auto_unlocked = outcome in ("auto_abort", "injected_real_unlock_commit_failure")
    if outcome == "injected_real_unlock_commit_failure":
        def injected_commit_boundary_fault():
            # Injected fault, NOT a natural COMMIT failure. It releases the
            # REAL native SQLite writer before the managed failure boundary.
            sqlite3.Connection.rollback(a)
            assert not a.in_transaction
            raise sqlite3.OperationalError("injected commit-boundary failure after real native unlock")

        monkeypatch.setattr(a, "_native_commit", injected_commit_boundary_fault)

    def pause():
        native_active.append(a.in_transaction)
        paused.set()
        assert release.wait(10), "parent did not release archive recovery barrier"

    if a_kind == "archive":
        undo = archive._undo_covers

        def paused_undo(journal):
            pause()
            undo(journal)
            assert (directory / "1.jpg").read_bytes() == ORIGINAL
            assert fence.fd is not None
            restored.set()

        monkeypatch.setattr(archive, "_undo_covers", paused_undo)
    else:
        replace = Path.replace

        def paused_replace(source, destination):
            if source.name.startswith(".cover-restore-") and Path(destination) == directory / "1.jpg":
                pause()
                result = replace(source, destination)
                assert (directory / "1.jpg").read_bytes() == ORIGINAL
                assert fence.fd is not None
                restored.set()
                return result
            return replace(source, destination)

        monkeypatch.setattr(Path, "replace", paused_replace)

    def finish_a():
        if outcome == "auto_abort":
            # Actual native SQLite auto-abort, not a mocked commit/rollback.
            with pytest.raises(sqlite3.IntegrityError):
                a.execute("INSERT OR ROLLBACK INTO items(id,title) VALUES(1,'duplicate')")
            assert not a.in_transaction
            a.rollback()
        elif outcome == "deferred_commit_failure":
            a.execute("INSERT INTO child VALUES(-1)")
            with pytest.raises(sqlite3.IntegrityError):
                a.commit()
        elif outcome == "injected_real_unlock_commit_failure":
            with pytest.raises(sqlite3.OperationalError, match="injected commit-boundary failure after real native unlock"):
                a.commit()
        elif outcome == "close":
            a.close()
        else:
            a.rollback()

    def writer_b():
        b = sqlite3.connect(database, factory=_Connection, timeout=10)
        try:
            b_attempted.set()
            b.execute("BEGIN IMMEDIATE")
            b_locked.set()
            other = "canonical" if a_kind == "archive" else "archive"
            _publish(b, other, 1, B_BYTES)
            _publish(b, other, 2, SIBLING_BYTES)
            b.commit()
            assert b._transaction_resources is None
            assert b._after_commit == b._after_rollback == []
            b_done.set()
        finally:
            b.close()

    observer = sqlite3.connect(database, timeout=0)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(finish_a)
            second = None
            try:
                assert paused.wait(10), "A did not enter real filesystem recovery"
                assert native_active == [not auto_unlocked]
                second = pool.submit(writer_b)
                assert b_attempted.wait(10)
                if auto_unlocked:
                    # Proves the fence, not a still-owned SQLite writer lock,
                    # blocks B's file publication after A's native auto-unlock.
                    assert b_locked.wait(5), "B could not obtain the auto-released SQLite lock"
                else:
                    assert not b_locked.wait(0.2), "A released SQLite before critical undo"
                assert not b_done.wait(0.2), "B committed while A still owned recovery"
                assert observer.execute("SELECT title FROM items WHERE id=1").fetchone()[0] == "original"
                assert (directory / "1.jpg").read_bytes() == A_BYTES
                assert (directory / "2.jpg").read_bytes() == b"original sibling"
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    observer.execute("BEGIN IMMEDIATE")
            finally:
                release.set()
            first.result(timeout=10)
            assert restored.is_set() and fence.fd is None
            assert second is not None
            second.result(timeout=10)
        # The independent native observer, not either managed writer's cache,
        # must see B's rows and exact destination/sibling bytes after A is done.
        assert b_done.is_set()
        assert observer.execute("SELECT id,title,cover_path FROM items ORDER BY id").fetchall() == [
            (1, "B", "covers/1.jpg"), (2, "B", "covers/2.jpg"),
        ]
        assert observer.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
        assert (directory / "1.jpg").read_bytes() == B_BYTES
        assert (directory / "2.jpg").read_bytes() == SIBLING_BYTES
        assert sorted(path.name for path in directory.iterdir()) == ["1.jpg", "2.jpg"]
    finally:
        release.set()
        observer.close()
        a.close()


def test_archive_local_undo_failure_retains_all_preimages_and_attempts_other_files(tmp_path, monkeypatch):
    from app.services import archive

    conn, _database, directory = _wal_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(archive, "_cover_journals", ContextVar("archive_failed_local_undo", default=()))
    conn.execute("BEGIN IMMEDIATE")
    root = archive._CoverJournal(conn)
    first, second = directory / "1.jpg", directory / "2.jpg"
    for path in (first, second):
        root.remember(path)
        path.write_bytes(A_BYTES)
    resource = root.resource
    replace = archive._replace_cover
    attempts = []

    def fail_one(path, content):
        attempts.append(path)
        if path == second:
            raise OSError("injected archive local undo failure")
        replace(path, content)

    try:
        monkeypatch.setattr(archive, "_replace_cover", fail_one)
        with pytest.raises(OSError, match="injected archive local undo failure"):
            root.rollback()
        assert attempts == [second, first]
        assert first.read_bytes() == ORIGINAL and second.read_bytes() == A_BYTES
        assert root.entries == {first: ORIGINAL, second: b"original sibling"}
        assert root.resource is resource and conn._transaction_resources is not None
        assert root.pending_undo == {root}
        with pytest.raises(RuntimeError, match="recovery is still pending"):
            root.finish()
        assert root.entries and root.resource is resource
        monkeypatch.setattr(archive, "_replace_cover", replace)
        root.rollback()
        resource.cancel()
        resource.cancel()  # successful local recovery/cancel is idempotent
        assert root.entries == {} and root.pending_undo == set()
        assert root.resource is None
        assert conn._transaction_resources.entries == [] and conn._transaction_resources.fence is None
        assert first.read_bytes() == ORIGINAL and second.read_bytes() == b"original sibling"
        conn.commit()
        assert conn._transaction_resources is None
        conn.rollback()
        assert first.read_bytes() == ORIGINAL and second.read_bytes() == b"original sibling"
    finally:
        monkeypatch.setattr(archive, "_replace_cover", replace)
        conn.close()


def test_caught_nested_archive_undo_failure_cannot_escape_as_success_or_commit_caller_work(tmp_path, monkeypatch):
    from app.services import archive
    from app.services.transaction_resources import TransactionResourceError

    conn, database, directory = _wal_fixture(tmp_path, monkeypatch)
    conn.execute("UPDATE items SET title='uncommitted caller work' WHERE id=2")
    replace = archive._replace_cover
    roots, calls = [], []

    def fail_restoration(path, content):
        if content in (ORIGINAL, A_BYTES):
            raise OSError("injected nested archive recovery failure")
        replace(path, content)

    @archive._atomic_archive_apply
    def inner(db):
        archive._install_cover(_reader(B_BYTES), 1, "covers/inner.jpg")
        raise ValueError("nested publication refused")

    @archive._atomic_archive_apply
    def outer(db):
        roots.append(archive._cover_journals.get()[-1])
        _publish(db, "archive", 1, A_BYTES)
        monkeypatch.setattr(archive, "_replace_cover", fail_restoration)
        with pytest.raises(OSError, match="nested archive recovery failure"):
            inner(db)
        # Swallowing the inner error must not make the outer savepoint a
        # successful publication or allow the caller's later native commit.
        return "must not be returned"

    observer = sqlite3.connect(database)
    try:
        with pytest.raises(OSError, match="nested archive recovery failure"):
            outer(conn)
        root = roots[0]
        assert root.entries == {directory / "1.jpg": ORIGINAL}
        assert root.pending_undo and root.resource is not None
        assert conn._transaction_resources.fence.fd is not None
        assert archive._cover_journals.get() == ()
        assert (directory / "1.jpg").read_bytes() == B_BYTES
        # The whole apply's SQL work was undone; unrelated caller work still
        # belongs to the outer transaction until the refused commit rolls back.
        assert conn.execute("SELECT title FROM items WHERE id=1").fetchone()[0] == "original"
        assert conn.execute("SELECT title FROM items WHERE id=2").fetchone()[0] == "uncommitted caller work"
        assert observer.execute("SELECT title FROM items WHERE id=2").fetchone()[0] == "original sibling"
        conn._after_commit.append(lambda: calls.append("wrong success"))
        conn._after_rollback.append(lambda: calls.append("rollback"))
        monkeypatch.setattr(archive, "_replace_cover", replace)
        with pytest.raises(TransactionResourceError, match="outer rollback is required"):
            conn.commit()
        assert calls == ["rollback"] and not conn.in_transaction
        assert observer.execute("SELECT title FROM items ORDER BY id").fetchall() == [("original",), ("original sibling",)]
        assert (directory / "1.jpg").read_bytes() == ORIGINAL
        assert (directory / "2.jpg").read_bytes() == b"original sibling"
        assert root.entries == {} and root.pending_undo == set() and root.resource is None
        assert conn._transaction_resources is None
        conn.rollback()
        assert calls == ["rollback"]
    finally:
        monkeypatch.setattr(archive, "_replace_cover", replace)
        observer.close()
        conn.close()


@pytest.mark.parametrize("existing_cover", [False, True])
def test_archive_replace_error_after_real_rename_restores_row_then_retry_and_outer_undo(
    db, monkeypatch, existing_cover,
):
    from app import config
    from app.services import archive
    from tests.conftest import _insert_item

    title = "Archive real rename retry fence"
    target = _insert_item(db, title=title, isbn=None) if existing_cover else None
    if target is not None:
        (config.COVERS_DIR / f"{target}.jpg").write_bytes(ORIGINAL)
        db.execute("UPDATE items SET cover_path=? WHERE id=?", (f"covers/{target}.jpg", target))
    db.commit()
    before = "\n".join(db.iterdump())
    reader = SimpleNamespace(
        library={"items": [{"id": ref, "title": title, "media_type": "book", "owned": 1,
                            "cover": "covers/1.jpg", "tags": ["Archive retry fence tag"]}
                           for ref in (1, 2)]},
        cover_names={"covers/1.jpg"}, read_cover=lambda name: B_BYTES,
    )
    plan = archive.plan_archive(db, reader, mode="update")
    replace = archive.os.replace
    failed = []

    def fail_after_rename(source, destination):
        replace(source, destination)
        if not failed and ".archive-" in Path(source).name:
            failed.append(Path(destination))
            raise OSError("injected archive error after actual replace")

    monkeypatch.setattr(archive.os, "replace", fail_after_rename)
    report = archive.apply_plan(db, reader, plan, {"replace_covers": True})
    assert report["updated" if existing_cover else "imported"] == 1
    assert report["covers_replaced" if existing_cover else "covers_installed"] == 1
    assert len(report["errors"]) == 1 and "after actual replace" in report["errors"][0]
    assert db.execute("SELECT COUNT(*) FROM items WHERE title=?", (title,)).fetchone()[0] == 1
    assert failed[0].read_bytes() == B_BYTES
    assert db._transaction_resources is not None
    db.rollback()
    assert "\n".join(db.iterdump()) == before
    if existing_cover:
        assert failed[0].read_bytes() == ORIGINAL
    else:
        assert not failed[0].exists()
    assert db._transaction_resources is None


def _decision_png(seed):
    """A complete, distinct RGB PNG, not just image magic or padded fake bytes."""
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    rows = b"".join(b"\0" + bytes((seed + x * 7 + y * 11) % 256 for x in range(48)) for y in range(16))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _decision_archive(tmp_path, title, image):
    from app.services import archive

    item = {"id": 1, "title": title, "media_type": "book", "owned": 1, "publisher": "archive decision metadata"}
    if image is not None:
        item["cover"] = "covers/1.jpg"
    path = tmp_path / "cover-decision.zip"
    with zipfile.ZipFile(path, "w") as exported:
        exported.writestr("manifest.json", json.dumps({"format": archive.FORMAT_NAME, "version": archive.FORMAT_VERSION}))
        exported.writestr("library.json", json.dumps({"items": [item]}))
        if image is not None:
            exported.writestr("covers/1.jpg", image)
    return path


def _decision_target(db):
    from app import config
    from tests.conftest import _insert_item

    title = "Archive preservation decision fence"
    target = _insert_item(db, title=title, isbn=None, publisher="original decision metadata")
    db.execute("UPDATE items SET cover_path=?,cover_edition_isbn='9780441172719',cover_edition_language='de' WHERE id=?",
               (f"covers/{target}.jpg", target))
    db.commit()
    path = config.COVERS_DIR / f"{target}.jpg"
    assert not path.exists()  # an actual initial gap, despite the remembered DB path
    return target, title, path


def test_full_archive_apply_default_preserves_legacy_writer_arriving_before_cover_decision(
    db, tmp_path, monkeypatch,
):
    from app import config
    from app.database import get_db
    from app.services import archive, covers
    from app.services.transaction_resources import cover_publication_guard

    target, title, path = _decision_target(db)
    archive_bytes, legacy_bytes = _decision_png(17), _decision_png(83)
    assert covers.validate_uploaded_cover(archive_bytes) and covers.validate_uploaded_cover(legacy_bytes)
    assert archive_bytes != legacy_bytes
    upload = _decision_archive(tmp_path, title, archive_bytes)
    with archive.read_archive(upload) as reader:
        assert reader.read_cover("covers/1.jpg") == archive_bytes
        plan = archive.plan_archive(db, reader, mode="update")
    assert plan["items"][0]["verdict"] == "update" and plan["items"][0]["cover"] == "install"
    guarded, release_legacy, sql_owned, enrolling, applied, commit_allowed = (Event() for _ in range(6))
    roots, owners = [], []
    register = archive.register_transaction_resource

    @contextmanager
    def paused_final_guard(publication_dir, *, conn=None):
        # Instrument the real legacy helper's guard, not its write or result.
        # No SQL starts inside this file-only guard or in the legacy worker.
        with cover_publication_guard(publication_dir, conn=conn):
            guarded.set()
            assert release_legacy.wait(10), "legacy final-write guard was not released"
            yield

    def observe_enrollment(conn, **kwargs):
        roots.append(kwargs["after_commit"].__self__)
        enrolling.set()
        return register(conn, **kwargs)

    monkeypatch.setattr(covers, "cover_publication_guard", paused_final_guard)
    monkeypatch.setattr(archive, "register_transaction_resource", observe_enrollment)

    def apply_and_commit():
        with get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            sql_owned.set()
            with archive.read_archive(upload) as reader:
                report = archive.apply_plan(conn, reader, plan, {"replace_covers": False})
            owners.append(conn._transaction_resources)
            applied.set()
            assert commit_allowed.wait(10), "archive commit boundary was not released"
        return report  # get_db performed the actual successful outer COMMIT

    observer = sqlite3.connect(config.DATABASE_PATH, timeout=0)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            legacy = pool.submit(covers.save_uploaded_cover, target, legacy_bytes)
            pending = None
            try:
                assert guarded.wait(10) and not path.exists()
                pending = pool.submit(apply_and_commit)
                assert sql_owned.wait(10) and enrolling.wait(10)
                assert not applied.wait(0.2) and not path.exists()
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    observer.execute("BEGIN IMMEDIATE")
                release_legacy.set()
                assert legacy.result(timeout=10) == f"covers/{target}.jpg"
                assert applied.wait(10)
                assert path.read_bytes() == legacy_bytes
                assert len(roots) == len(owners) == 1
                assert roots[0].entries == {}  # preservation did not read/copy a preimage
                assert roots[0].resource is not None and owners[0].fence.fd is not None
            finally:
                release_legacy.set()
                commit_allowed.set()
            assert pending is not None
            report = pending.result(timeout=10)
        assert report["errors"] == [] and report["updated"] == 1
        assert report["covers_installed"] == 0 and report["covers_replaced"] == 0
        assert observer.execute("SELECT publisher,cover_path,cover_edition_isbn,cover_edition_language FROM items WHERE id=?",
                                (target,)).fetchone() == ("archive decision metadata", f"covers/{target}.jpg", "9780441172719", "de")
        assert path.read_bytes() == legacy_bytes
        assert sorted(file.name for file in config.COVERS_DIR.iterdir()) == [f"{target}.jpg"]
        assert roots[0].entries == {} and roots[0].resource is None and owners[0].fence is None
    finally:
        release_legacy.set()
        commit_allowed.set()
        observer.close()


@pytest.mark.parametrize("existing,replace,installed,replaced", [(False, False, 1, 0), (True, False, 0, 0), (True, True, 0, 1)])
def test_full_archive_cover_decision_true_gap_and_explicit_replace_controls(db, tmp_path, existing, replace, installed, replaced):
    from app import config
    from app.services import archive, covers

    target, title, path = _decision_target(db)
    archive_bytes, legacy_bytes = _decision_png(17), _decision_png(83)
    if existing:
        assert covers.save_uploaded_cover(target, legacy_bytes) == f"covers/{target}.jpg"
    upload = _decision_archive(tmp_path, title, archive_bytes)
    with archive.read_archive(upload) as reader:
        assert reader.read_cover("covers/1.jpg") == archive_bytes
        plan = archive.plan_archive(db, reader, mode="update")
        report = archive.apply_plan(db, reader, plan, {"replace_covers": replace})
    db.commit()
    assert report["errors"] == [] and report["updated"] == 1
    assert (report["covers_installed"], report["covers_replaced"]) == (installed, replaced)
    assert path.read_bytes() == (legacy_bytes if existing and not replace else archive_bytes)
    with sqlite3.connect(config.DATABASE_PATH) as observer:
        row = observer.execute("SELECT cover_path,cover_edition_isbn,cover_edition_language FROM items WHERE id=?", (target,)).fetchone()
    expected = (f"covers/{target}.jpg", "9780441172719", "de") if existing and not replace else (f"covers/{target}.jpg", None, None)
    assert row == expected
    assert sorted(file.name for file in config.COVERS_DIR.iterdir()) == [f"{target}.jpg"]
    assert db._transaction_resources is None


def test_full_archive_metadata_only_update_does_not_enroll_a_cover_decision(db, tmp_path, monkeypatch):
    from app.services import archive

    _target, title, path = _decision_target(db)
    enrolled = []
    register = archive.register_transaction_resource
    def observe_registration(*args, **kwargs):
        enrolled.append((args, kwargs))
        return register(*args, **kwargs)
    monkeypatch.setattr(archive, "register_transaction_resource", observe_registration)
    upload = _decision_archive(tmp_path, title, None)
    with archive.read_archive(upload) as reader:
        plan = archive.plan_archive(db, reader, mode="update")
        report = archive.apply_plan(db, reader, plan)
    assert report["errors"] == [] and report["updated"] == 1
    assert report["covers_installed"] == report["covers_replaced"] == 0
    assert enrolled == []
    assert getattr(db, "_transaction_resources", None) is None and not path.exists()
    db.commit()
    assert enrolled == [] and not path.exists()
