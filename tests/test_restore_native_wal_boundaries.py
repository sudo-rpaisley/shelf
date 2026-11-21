"""Native SQLite/WAL failure and process-interruption restore boundaries.

Only disposable databases and a test-owned subprocess are used. None of these
tests substitutes a fake backup implementation for SQLite's actual page copy.
"""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from tests.conftest import _insert_item


def _stage_and_mark_live(db, tmp_path):
    stage = tmp_path / "native-prepared-stage.db"
    _insert_item(db, title="Before restore snapshot", isbn=None)
    db.commit()
    with closing(sqlite3.connect(stage)) as staged:
        db.backup(staged)
    marker = _insert_item(db, title="Live-only WAL marker", isbn=None)
    db.commit()
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    return stage, marker, tuple(db.iterdump())


@pytest.mark.parametrize("failure", ["recovery-fsync", "stage-fsync", "directory-fsync", "sqlite-page-copy"])
def test_restore_installation_fault_preserves_live_wal_and_exact_logical_database(
    db, admin_user, tmp_path, monkeypatch, failure,
):
    from app.routers import settings

    stage, marker, before = _stage_and_mark_live(db, tmp_path)
    filesystem_calls = []
    real_fsync = settings.os.fsync

    def fsync(fd):
        filesystem_calls.append(fd)
        index = {"recovery-fsync": 1, "stage-fsync": 2, "directory-fsync": 3}.get(failure)
        if len(filesystem_calls) == index:
            raise OSError("injected durable-restore fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(settings.os, "fsync", fsync)

    class ActualSQLiteWithPageFailure(sqlite3.Connection):
        def backup(self, destination, *, pages=-1, progress=None, **kwargs):
            def after_real_step(status, remaining, total):
                if progress:
                    progress(status, remaining, total)
                if failure == "sqlite-page-copy" and status == sqlite3.SQLITE_OK:
                    assert remaining > 0
                    raise OSError("injected failure after a real SQLite page-copy step")

            return super().backup(destination, pages=pages, progress=after_real_step, **kwargs)

    with closing(sqlite3.connect(stage, factory=ActualSQLiteWithPageFailure)) as staged:
        staged.row_factory = sqlite3.Row
        with pytest.raises(OSError, match="injected"):
            settings._install_restored_database(staged, stage)
    assert tuple(db.iterdump()) == before
    assert db.execute("SELECT title FROM items WHERE id=?", (marker,)).fetchone()[0] == "Live-only WAL marker"
    assert [row[0] for row in db.execute("PRAGMA integrity_check")] == ["ok"]
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
    recovery_paths = list(settings.DATA_DIR.glob("shelf_pre_restore_*.db"))
    assert len(recovery_paths) == 1
    with closing(sqlite3.connect(recovery_paths[0])) as recovery:
        assert tuple(recovery.iterdump()) == before


def test_process_exit_during_real_page_copy_rolls_back_destination_and_keeps_durable_recovery(
    db, admin_user, tmp_path,
):
    from app.routers import settings

    stage, marker, before = _stage_and_mark_live(db, tmp_path)
    program = r'''
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
from app.routers import settings

class InterruptDuringActualSQLiteBackup(sqlite3.Connection):
    def backup(self, destination, *, pages=-1, progress=None, **kwargs):
        def after_real_step(status, remaining, total):
            if progress:
                progress(status, remaining, total)
            if status == sqlite3.SQLITE_OK and remaining > 0:
                os._exit(73)  # Only this deliberately spawned test process.
        return super().backup(destination, pages=pages, progress=after_real_step, **kwargs)

with closing(sqlite3.connect(sys.argv[1], factory=InterruptDuringActualSQLiteBackup)) as staged:
    staged.row_factory = sqlite3.Row
    settings._install_restored_database(staged, Path(sys.argv[1]))
raise AssertionError("the real copy was not interrupted before commit")
'''
    environment = dict(os.environ, DATA_DIR=str(settings.DATA_DIR))
    result = subprocess.run([sys.executable, "-B", "-c", program, str(stage)],
                            cwd=Path(__file__).resolve().parents[1], env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 73, result.stdout + result.stderr
    assert tuple(db.iterdump()) == before
    assert db.execute("SELECT title FROM items WHERE id=?", (marker,)).fetchone()[0] == "Live-only WAL marker"
    # Reopen the real serving file after the process died with its destination
    # transaction open: recovery, not a mocked rollback callback, must work.
    with closing(sqlite3.connect(settings.DATABASE_PATH)) as reopened:
        assert tuple(reopened.iterdump()) == before
        assert [row[0] for row in reopened.execute("PRAGMA integrity_check")] == ["ok"]
        assert not reopened.execute("PRAGMA foreign_key_check").fetchall()
    recovery_paths = list(settings.DATA_DIR.glob("shelf_pre_restore_*.db"))
    assert len(recovery_paths) == 1
    assert recovery_paths[0].stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(recovery_paths[0])) as recovery:
        assert tuple(recovery.iterdump()) == before
