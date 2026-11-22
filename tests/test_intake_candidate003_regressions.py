"""Candidate003 intake regressions; execute only after the parent freezes writers.

Provider stubs are local. Constraint probes pass real INSERTs to SQLite and
record their actual failures, rather than raising a fabricated IntegrityError.
"""

import sqlite3
from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest


ISBN = "9780441172719"
MARKER = "intake-c003-row-attempt"


def _snapshot(db):
    tables = ("items", "item_copies", "library_items", "user_item_state",
              "user_reading_log", "item_series", "tags", "item_tags",
              "lists", "list_items", "settings")
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in tables}


def _identity(db, item_id):
    """Stable catalogue/copy/private identities, independent of Trash liveness."""
    return {
        "item": tuple(db.execute(
            "SELECT id, title, media_type, isbn, source, cover_path FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()),
        "copies": [tuple(row) for row in db.execute(
            "SELECT id, item_id, copy_barcode, location_id, copy_number, is_primary, condition, "
            "acquired_date, acquisition_price FROM item_copies WHERE item_id = ? ORDER BY id",
            (item_id,),
        )],
        "library": [tuple(row) for row in db.execute(
            "SELECT library_id, item_id FROM library_items WHERE item_id = ?", (item_id,))],
        "state": [tuple(row) for row in db.execute(
            "SELECT * FROM user_item_state WHERE item_id = ? ORDER BY user_id", (item_id,))],
        "history": [tuple(row) for row in db.execute(
            "SELECT * FROM user_reading_log WHERE item_id = ? ORDER BY id", (item_id,))],
    }


def _constraint_probe(monkeypatch, *, blind_hidden=False):
    """Blind one preflight; observe real SQL faults and locked classification.

    blind_hidden additionally blinds the core's privacy probe, solely to reach
    the router's independent constraint fallback with a hidden live collision.
    No constraint, INSERT, permission checker or savepoint is replaced.
    """
    from app import config
    from app.routers import intake

    real_get_db = intake.get_db
    seen = {"blinded": 0, "faults": [], "classifications": [], "hidden_blinded": 0}

    class EmptyCursor:
        def fetchall(self):
            return []

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args, **kwargs):
            if (not seen["blinded"] and sql.startswith("SELECT id, deleted_at, media_type FROM items")
                    and args[0][0] == ISBN):
                seen["blinded"] += 1
                return EmptyCursor()
            if (blind_hidden and sql.startswith("SELECT id FROM items WHERE isbn = ? AND media_type = ? AND id != ?")
                    and args[0][0] == ISBN):
                seen["hidden_blinded"] += 1
                return EmptyCursor()
            if seen["faults"] and sql.startswith("SELECT id, deleted_at FROM items WHERE isbn = ?"):
                marker = self.connection.execute("SELECT 1 FROM settings WHERE key = ?", (MARKER,)).fetchone()
                rival = sqlite3.connect(str(config.DATABASE_PATH), timeout=0)
                try:
                    rival.execute("BEGIN IMMEDIATE")
                    lock = "acquired"
                    rival.rollback()
                except sqlite3.OperationalError as exc:
                    assert str(exc) == "database is locked"
                    lock = "locked"
                finally:
                    rival.close()
                seen["classifications"].append((self.connection, self.connection.in_transaction, marker, lock))
            try:
                return self.connection.execute(sql, *args, **kwargs)
            except sqlite3.IntegrityError as exc:
                seen["faults"].append((exc, self.connection))
                raise

    @contextmanager
    def get_db():
        with real_get_db() as connection:
            yield Connection(connection)

    monkeypatch.setattr(intake, "get_db", get_db)
    return seen


@pytest.mark.parametrize("path", ["strong", "weak"])
def test_real_isbn_constraint_rolls_back_row_then_imports_next_row(admin_client, db, monkeypatch, path):
    from app.database import get_db
    from app.routers import intake, items_common
    from app.services import item_write, openlibrary, tags

    rival = {}

    def seed(connection):
        # A rival request does not inherit this intake's pending default tags.
        with tags.default_tags(None):
            item_id = item_write.insert_item(connection, title="Dune (rival)", isbn=ISBN)
        rival.update(id=item_id, identity=_identity(connection, item_id))

    if path == "weak":
        seed(db)
        db.commit()

    async def lookup(isbn, token, client, *, google_api_key=None):
        assert path == "strong"
        # A real rival commit in the provider window, after the advisory read.
        with get_db() as connection:
            seed(connection)
        return {"title": "Dune", "authors": "Frank Herbert"}, "openlibrary", {}, False

    async def search(title, author, client, *, lang="en"):
        return ([{"title": "Dune", "authors": "Frank Herbert", "isbn": ISBN}]
                if title == "Dune" else [])

    monkeypatch.setattr(items_common, "_lookup_metadata", lookup)
    monkeypatch.setattr(openlibrary, "search_by_title_author", search)
    seen = _constraint_probe(monkeypatch)
    real_write = items_common._save_item if path == "strong" else intake.insert_item

    def observed_write(*args, **kwargs):
        connection = kwargs["db"] if path == "strong" else args[0]
        title = args[0]["title"] if path == "strong" else kwargs["title"]
        if title == "Dune":
            # This effect precedes the core's nested savepoint. Only the
            # router's row scope can remove it before duplicate classification.
            connection.execute("INSERT INTO settings (key, value) VALUES (?, 'attempt')", (MARKER,))
        return real_write(*args, **kwargs)

    if path == "strong":
        monkeypatch.setattr(items_common, "_save_item", observed_write)
    else:
        monkeypatch.setattr(intake, "insert_item", observed_write)
    first = {"title": "Dune", "authors": "Frank Herbert"}
    if path == "strong":
        first["isbn"] = ISBN
    response = admin_client.post("/api/intake/confirm", json={
        "books": [first, {"title": "Solaris"}], "tags": ["C003 imported"],
    })
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["skipped"] == [{"title": "Dune", "reason": "ISBN already in library"}]
    assert [entry["title"] for entry in data["added"]] == ["Solaris"]
    assert data["added"][0]["restored"] is False
    assert seen["blinded"] == 1
    assert len(seen["faults"]) == 1
    error, connection = seen["faults"][0]
    assert error.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE
    assert str(error) == "UNIQUE constraint failed: items.isbn, items.media_type"
    assert seen["classifications"] == [(connection, True, None, "locked")]
    assert db.execute("SELECT 1 FROM settings WHERE key = ?", (MARKER,)).fetchone() is None
    assert _identity(db, rival["id"]) == rival["identity"]
    assert db.execute("SELECT COUNT(*) FROM items WHERE isbn = ?", (ISBN,)).fetchone()[0] == 1
    imported = data["added"][0]["id"]
    assert db.execute("SELECT library_id FROM library_items WHERE item_id = ?", (imported,)).fetchone()[0] == 1
    assert [row[0] for row in db.execute(
        "SELECT t.name FROM tags t JOIN item_tags it ON it.tag_id = t.id WHERE it.item_id = ?",
        (imported,),
    )] == ["C003 imported"]
    assert db.execute("SELECT 1 FROM item_tags WHERE item_id = ?", (rival["id"],)).fetchone() is None


@pytest.mark.parametrize("fault", ["other_unique", "foreign_key", "check", "operational"])
def test_unknown_sql_fault_rolls_back_the_entire_batch_generically(admin_client, db, monkeypatch, fault):
    from app.routers import intake
    from app.services import openlibrary

    monkeypatch.setattr(openlibrary, "search_by_title_author", AsyncMock(return_value=[]))
    db.execute("INSERT INTO settings (key, value) VALUES (?, 'original')", (MARKER,))
    db.commit()
    before = _snapshot(db)
    real_insert = intake.insert_item
    faults = []

    def insert(connection, *args, **kwargs):
        item_id = real_insert(connection, *args, **kwargs)
        if kwargs["title"] == "Faulting row":
            try:
                if fault == "other_unique":
                    connection.execute("INSERT INTO settings (key, value) VALUES (?, 'collision')", (MARKER,))
                elif fault == "foreign_key":
                    connection.execute("INSERT INTO library_items (library_id, item_id) VALUES (-999, -999)")
                elif fault == "check":
                    connection.execute("CREATE TEMP TABLE c003_check (value INTEGER CHECK (value > 0))")
                    connection.execute("INSERT INTO c003_check VALUES (0)")
                else:
                    connection.execute("SELECT * FROM c003_nonexistent_table")
            except sqlite3.DatabaseError as exc:
                faults.append(exc)
                raise
        return item_id

    monkeypatch.setattr(intake, "insert_item", insert)
    response = admin_client.post("/api/intake/confirm", json={
        "books": [{"title": "Earlier good row"}, {"title": "Faulting row"}, {"title": "Later row"}],
        "tags": ["Must roll back"],
    })
    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "Import conflicts with existing catalogue data",
                               "added": [], "skipped": []}
    assert len(faults) == 1
    assert faults[0].sqlite_errorcode == {
        "other_unique": sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
        "foreign_key": sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY,
        "check": sqlite3.SQLITE_CONSTRAINT_CHECK,
        "operational": sqlite3.SQLITE_ERROR,
    }[fault]
    assert _snapshot(db) == before


def test_unrelated_constraint_is_not_mislabeled_by_a_visible_isbn_collision(admin_client, db, monkeypatch):
    from app.routers import intake
    from app.services import item_write, openlibrary

    item_write.insert_item(db, title="Visible rival", isbn=ISBN)
    db.execute("INSERT INTO settings (key, value) VALUES (?, 'original')", (MARKER,))
    db.commit()
    before = _snapshot(db)

    async def search(title, author, client, *, lang="en"):
        return [{"title": title, "isbn": ISBN}] if title == "Faulting row" else []

    monkeypatch.setattr(openlibrary, "search_by_title_author", search)
    seen = _constraint_probe(monkeypatch)
    real_insert = intake.insert_item

    def insert(connection, *args, **kwargs):
        if kwargs["title"] == "Faulting row":
            # There really IS an authorized ISBN twin, but THIS real SQLite
            # error belongs to another constraint and must not become a skip.
            connection.execute("INSERT INTO settings (key, value) VALUES (?, 'collision')", (MARKER,))
        return real_insert(connection, *args, **kwargs)

    monkeypatch.setattr(intake, "insert_item", insert)
    response = admin_client.post("/api/intake/confirm", json={
        "books": [{"title": "Faulting row"}, {"title": "Later good row"}],
    })
    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "Import conflicts with existing catalogue data",
                               "added": [], "skipped": []}
    assert len(seen["faults"]) == 1
    assert seen["faults"][0][0].sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY
    assert seen["classifications"] == []
    assert _snapshot(db) == before


def test_real_constraint_hidden_isbn_fallback_refuses_without_disclosure(editor_client, db, monkeypatch):
    from app.routers import intake
    from app.services import item_write, libraries, openlibrary

    private = libraries.create_library(db, "PRIVATE LIBRARY C003")
    hidden = item_write.insert_item(db, title="PRIVATE TITLE C003", isbn=ISBN, library_id=private["id"])
    db.commit()
    before = _snapshot(db)
    async def search(title, author, client, *, lang="en"):
        return ([{"title": "Dune", "isbn": ISBN, "authors": "Frank Herbert"}]
                if title == "Dune" else [])

    monkeypatch.setattr(openlibrary, "search_by_title_author", search)
    seen = _constraint_probe(monkeypatch, blind_hidden=True)
    real_insert = intake.insert_item

    def insert(connection, *args, **kwargs):
        connection.execute("INSERT INTO settings (key, value) VALUES (?, 'attempt')", (MARKER,))
        return real_insert(connection, *args, **kwargs)

    monkeypatch.setattr(intake, "insert_item", insert)
    response = editor_client.post("/api/intake/confirm", json={
        "books": [{"title": "Dune", "authors": "Frank Herbert"}, {"title": "Later row"}],
    })
    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "This identifier is unavailable", "added": [], "skipped": []}
    assert len(seen["faults"]) == 1
    error, connection = seen["faults"][0]
    assert error.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE
    assert seen["classifications"] == [(connection, True, None, "locked")]
    assert seen["hidden_blinded"] == 1
    assert str(hidden) not in response.json()["message"]
    assert _snapshot(db) == before


@pytest.mark.parametrize("access", ["hidden", "viewer"])
@pytest.mark.parametrize("trashed", [False, True])
def test_all_collision_authority_is_preflighted_before_first_write(editor_client, editor_user, db, monkeypatch, access, trashed):
    from app.routers import intake
    from app.services import item_write, libraries, openlibrary

    private = libraries.create_library(db, "PRIVATE PREFLIGHT LIBRARY")
    hidden = item_write.insert_item(db, title="PRIVATE PREFLIGHT TITLE", isbn=ISBN, library_id=private["id"])
    if trashed:
        item_write.trash_item(db, hidden)
    if access == "viewer":
        libraries.set_membership(db, private["id"], editor_user["id"], "viewer")
    db.commit()
    before = _snapshot(db)
    writes = []
    real_insert = intake.insert_item

    def insert(*args, **kwargs):
        writes.append(kwargs["title"])
        return real_insert(*args, **kwargs)

    async def search(title, author, client, *, lang="en"):
        return [{"title": title, "isbn": ISBN}] if title == "Second provider title" else []

    monkeypatch.setattr(intake, "insert_item", insert)
    monkeypatch.setattr(openlibrary, "search_by_title_author", search)
    response = editor_client.post("/api/intake/confirm", json={
        "books": [{"title": "First new row"}, {"title": "Second provider title"}],
        "tags": ["No partial work"],
    })
    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "This identifier is unavailable", "added": [], "skipped": []}
    assert writes == []
    assert _snapshot(db) == before


def test_advisory_duplicate_authority_is_rechecked_after_provider_await(editor_client, editor_user, db, monkeypatch):
    from app.database import get_db
    from app.services import item_write, libraries, openlibrary

    other = libraries.create_library(db, "Initially editable duplicate library")
    libraries.set_membership(db, other["id"], editor_user["id"], "editor")
    item_write.insert_item(db, title="Prechecked duplicate", library_id=other["id"])
    db.commit()

    async def search(title, author, client, *, lang="en"):
        # Revoke only the duplicate's parent, not the requested target library.
        with get_db() as connection:
            connection.execute("DELETE FROM library_memberships WHERE library_id = ? AND user_id = ?",
                               (other["id"], editor_user["id"]))
        return []

    monkeypatch.setattr(openlibrary, "search_by_title_author", search)
    response = editor_client.post("/api/intake/confirm", json={
        "books": [{"title": "Prechecked duplicate"}, {"title": "New after await"}],
    })
    assert response.status_code == 200
    assert response.json() == {"ok": False, "message": "This identifier is unavailable", "added": [], "skipped": []}
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


@pytest.mark.parametrize("trashed", [False, True])
def test_legacy_kids_identity_type_and_children_survive_intake(admin_client, db, monkeypatch, trashed):
    from app.services import item_write, openlibrary
    from tests.conftest import _insert_personal_history, _seed_personal_state

    target = item_write.insert_item(db, title="Dune (legacy)", isbn=ISBN, cover_path="covers/retained.jpg")
    db.execute("UPDATE items SET media_type = 'kids_book' WHERE id = ?", (target,))
    db.execute("UPDATE item_copies SET copy_barcode = 'C003-legacy', condition = 'good', "
               "acquired_date = '2026-11-20', acquisition_price = 3.5 WHERE item_id = ?", (target,))
    actor = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
    _seed_personal_state(db, actor, target, reading_status="reading", personal_notes="Keep private")
    _insert_personal_history(db, actor, target, notes="Keep history")
    before = _identity(db, target)
    assert len(before["copies"]) == 1
    if trashed:
        item_write.trash_item(db, target)
    db.commit()
    monkeypatch.setattr(openlibrary, "search_by_title_author", AsyncMock(return_value=[
        {"title": "Dune", "isbn": ISBN},
    ]))
    response = admin_client.post("/api/intake/confirm", json={"books": [{"title": "Dune", "media_type": "kids_book"}]})
    assert response.status_code == 200
    if trashed:
        assert response.json()["added"] == [{"title": "Dune", "matched": True, "lookup": "matched",
                                            "id": target, "restored": True}]
    else:
        assert response.json()["added"] == []
        assert response.json()["skipped"] == [{"title": "Dune", "reason": "ISBN already in library"}]
    assert _identity(db, target) == before
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (target,)).fetchone()[0] is None
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert admin_client.get(f"/item/{target}").status_code == 200


@pytest.mark.parametrize("trashed", [False, True])
def test_existing_book_kids_twins_remain_distinct_without_retyping_or_consolidation(admin_client, db, monkeypatch, trashed):
    from app.services import item_write, openlibrary

    legacy = item_write.insert_item(db, title="Legacy twin", isbn=ISBN)
    db.execute("UPDATE items SET media_type = 'kids_book' WHERE id = ?", (legacy,))
    canonical = item_write.insert_item(db, title="Canonical twin", isbn=ISBN)
    before = {item_id: _identity(db, item_id) for item_id in (legacy, canonical)}
    if trashed:
        item_write.trash_item(db, legacy)
        item_write.trash_item(db, canonical)
    db.commit()
    monkeypatch.setattr(openlibrary, "search_by_title_author", AsyncMock(return_value=[{"title": "Dune", "isbn": ISBN}]))
    response = admin_client.post("/api/intake/confirm", json={"books": [{"title": "Dune", "media_type": "kids_book"}]})
    assert response.status_code == 200
    if trashed:
        assert response.json()["added"][0]["id"] == canonical
        assert response.json()["added"][0]["restored"] is True
    else:
        assert response.json()["added"] == []
        assert response.json()["skipped"] == [{"title": "Dune", "reason": "ISBN already in library"}]
    assert {item_id: _identity(db, item_id) for item_id in (legacy, canonical)} == before
    assert [tuple(row) for row in db.execute("SELECT id, media_type FROM items ORDER BY id")] == [
        (legacy, "kids_book"), (canonical, "book"),
    ]
    assert (db.execute("SELECT deleted_at FROM items WHERE id = ?", (legacy,)).fetchone()[0] is not None) is trashed
    assert db.execute("SELECT deleted_at FROM items WHERE id = ?", (canonical,)).fetchone()[0] is None


def test_later_batch_plan_rechecks_newly_inserted_isbn(admin_client, db, monkeypatch):
    from app.services import openlibrary

    async def search(title, author, client, *, lang="en"):
        return [{"title": title, "isbn": ISBN}]

    monkeypatch.setattr(openlibrary, "search_by_title_author", search)
    response = admin_client.post("/api/intake/confirm", json={
        "books": [{"title": "First edition"}, {"title": "Second edition"}],
    })
    assert response.status_code == 200
    assert [entry["title"] for entry in response.json()["added"]] == ["First edition"]
    assert response.json()["skipped"] == [{"title": "Second edition", "reason": "ISBN already in library"}]
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


@pytest.mark.parametrize("module_name", ["tests.test_intake", "tests.test_intake_fork"])
def test_inline_task_double_honors_context_and_rejects_unexpected_await(monkeypatch, module_name):
    import asyncio
    import importlib
    from contextvars import Context, ContextVar

    module = importlib.import_module(module_name)
    module._run_background_tasks_inline(monkeypatch)
    marker = ContextVar("intake-c003-task-test", default="empty")
    marker.set("request")
    seen = []

    async def completed():
        seen.append(marker.get())

    asyncio.create_task(completed(), context=Context())
    assert seen == ["empty"]

    class Suspends:
        def __await__(self):
            yield "unexpected await"

    async def suspended():
        await Suspends()

    with pytest.raises(AssertionError, match="background coroutine unexpectedly suspended"):
        asyncio.create_task(suspended(), context=Context())
