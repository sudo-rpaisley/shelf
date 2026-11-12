"""Offline upgrade check on a disposable database copy. Stop Shelf first.

Run ``python -m app.verify_upgrade`` in the image with the stopped container's
volumes mounted read-only and networking disabled. No keys, covers, external
services, or original database files are modified or included in the output.
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


_CHECK = """
from app.database import get_db, init_db

init_db()
init_db()
with get_db() as db:
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise SystemExit('Upgrade verification failed: foreign-key violation')
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise SystemExit('Upgrade verification failed: SQLite integrity check')
print('Upgrade verification passed: two initializations, foreign keys and SQLite integrity OK. Original database unchanged.')
"""


def verify_database(source: Path) -> None:
    """Copy a stopped database and its WAL; execute migrations in a subprocess.

    A subprocess gives all module-level config/path bindings the temporary
    DATA_DIR from their first import. It also avoids changing the caller's
    cached paths. The real database is never opened through SQLite.
    """
    if not source.is_file():
        raise FileNotFoundError(f"Shelf database not found: {source}")
    with tempfile.TemporaryDirectory(prefix="shelf-upgrade-check-") as directory:
        target = Path(directory) / "shelf.db"
        for suffix in ("", "-wal", "-shm"):
            original = Path(str(source) + suffix)
            if original.is_file():
                shutil.copyfile(original, str(target) + suffix)
        env = {**os.environ, "DATA_DIR": directory}
        subprocess.run([sys.executable, "-c", _CHECK], env=env, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a temporary copy only. Stop Shelf before running.")
    parser.add_argument("--database", type=Path,
                        default=Path(os.environ.get("DATA_DIR", "/data")) / "shelf.db")
    args = parser.parse_args()
    try:
        verify_database(args.database)
    except (OSError, subprocess.CalledProcessError, sqlite3.Error) as exc:
        print(f"Upgrade check failed ({type(exc).__name__}); original database was not modified.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
