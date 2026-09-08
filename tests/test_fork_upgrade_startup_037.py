from contextlib import contextmanager

from app import database
from app.services import fork_upgrade


class _DummyDb:
    def __init__(self, events):
        self.events = events

    def executescript(self, sql):
        assert sql is database.SCHEMA
        self.events.append("schema")

    def commit(self):
        self.events.append("prepare-commit")


def test_init_db_prepares_before_migrations_and_finishes_after(monkeypatch, tmp_path):
    events = []
    db = _DummyDb(events)

    @contextmanager
    def fake_get_db():
        yield db

    def fake_prepare(connection):
        assert connection is db
        events.append("prepare")
        return True

    def fake_run_migrations(connection):
        assert connection is db
        events.append("migrate")
        return ["normal migration"]

    def fake_finish(connection):
        assert connection is db
        events.append("finish")
        return ["fork bridge"]

    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "data" / "shelf.db")
    monkeypatch.setattr(database, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(database, "get_db", fake_get_db)
    monkeypatch.setattr(database, "_run_migrations", fake_run_migrations)
    monkeypatch.setattr(fork_upgrade, "prepare_pre_037_fork", fake_prepare)
    monkeypatch.setattr(fork_upgrade, "finish_pre_037_fork", fake_finish)

    database.init_db()

    assert events == ["schema", "prepare", "prepare-commit", "migrate", "finish"]


def test_init_db_finishes_an_already_prepared_retry(monkeypatch, tmp_path):
    events = []
    db = _DummyDb(events)

    @contextmanager
    def fake_get_db():
        yield db

    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "data" / "shelf.db")
    monkeypatch.setattr(database, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(database, "get_db", fake_get_db)
    monkeypatch.setattr(
        fork_upgrade,
        "prepare_pre_037_fork",
        lambda connection: events.append("prepare") or False,
    )
    monkeypatch.setattr(
        database,
        "_run_migrations",
        lambda connection: events.append("migrate") or [],
    )
    monkeypatch.setattr(
        fork_upgrade,
        "is_pre_037_fork_database",
        lambda connection: events.append("detect-retry") or True,
    )
    monkeypatch.setattr(
        fork_upgrade,
        "finish_pre_037_fork",
        lambda connection: events.append("finish") or ["fork bridge"],
    )

    database.init_db()

    assert events == ["schema", "prepare", "migrate", "detect-retry", "finish"]
